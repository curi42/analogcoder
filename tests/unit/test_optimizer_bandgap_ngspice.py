"""The optimization phase against real ngspice, on benchmarks/bandgap.

Everything Tasks 1-7 built was verified against mocks. Here only the
candidate-PROPOSING agent is faked - one knob is pinned, with no value and no
step size - and the simulation, the addressing gates, the area computation,
the accept rule, the corner sweep and the bisection are all production code
driven by a real simulator.

Like the repo's other *_ngspice.py tests this one assumes ngspice is on PATH
(CLAUDE.md, "Setup") rather than inventing a skip.
"""

import asyncio
import json
import os
import tempfile
import time

from analogcoder.area import total_area
from analogcoder.area_limits import index_baseline_components
from analogcoder.netlist import (
    check_param_applicability,
    check_refdes_resolution,
    check_stimulus_untouched,
    resolve_includes,
)
from analogcoder.optimizer import OptimizerAgents, run_optimization
from analogcoder.pvt import run_full_pvt_sweep
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.state import RunState

BENCHMARK_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "bandgap")
)

# TRIMAMP.Xt is netlist.cir's `Xt tail nbias vss vss ...nfet_01v8 L=1 W=8` -
# the trim amplifier's tail current source, mirrored off nbias. Narrowing it
# lowers that stage's bias current, so it moves iq_ua directly. It is a real
# device in a real subckt, which is the point: a refdes that no gate can
# resolve produces a green test that never simulated anything.
KNOB = {"refdes": "TRIMAMP.Xt", "param": "W"}


def _load(spec_name):
    spec = load_spec(os.path.join(BENCHMARK_DIR, spec_name))
    texts = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            # cli.py absolutizes includes at exactly this point, and it has to
            # happen here too: RunState stages the deck into the run dir and
            # NgspiceBackend stages that into a temp dir, so `.include
            # "pdk_corner.inc"` stops resolving the moment the text moves.
            texts[tb.name] = resolve_includes(f.read(), os.path.dirname(tb.netlist_path))
    return spec, texts


def _simulate_fn(spec, backend, calls):
    async def simulate(netlist_texts, spec_arg):
        # 인자로 받은 텍스트를 쓴다 - state에서 다시 읽지 않는다. 둘이 어긋나면
        # 최적화가 자기가 만든 덱이 아닌 것을 재게 되고, 그 결함은 state를
        # 읽는 구현에서는 보이지 않는다.
        merged = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            for tb in spec_arg.testbenches:
                path = os.path.join(tmpdir, f"{tb.name}.cir")
                with open(path, "w") as f:
                    f.write(netlist_texts[tb.name])
                merged.update(
                    backend.run(path, {"control_block": tb.control_block}).measurements
                )
        calls.append(merged)
        return {"measurements": merged}

    return simulate


async def _propose(structure_view, margins, objective, netlist_view):
    # 값도 단계 폭도 싣지 않는다 - OPTIMIZER_SCHEMA가 그것을 구조적으로
    # 금하고, 얼마나 움직일지는 production의 결정론적 탐색이 정한다.
    return {
        "candidates": [{**KNOB, "direction": "decrease",
                        "reasoning": "tail current source of the trim amplifier"}],
        "overall_reasoning": "cut a bias current before touching anything else",
    }


def _knob_width(netlist_text):
    """그 소자 줄에 실제로 적힌 W. 텍스트 포함 검사는 못 쓴다 - 이 덱에는
    W=8인 소자가 여럿이라 되돌아가지 않아도 `"W=8" in text`가 참이다."""
    return index_baseline_components(netlist_text)[KNOB["refdes"]].params["W"]


def _events(state, step):
    with open(state.history_path) as f:
        return [json.loads(line) for line in f if json.loads(line)["step"] == step]


def test_the_pinned_knob_passes_every_addressing_gate():
    # _gate_addressing runs these three before a value is even read, and a
    # failure `break`s out of the candidate loop without simulating. So a
    # knob that fails any of them would make the end-to-end tests below pass
    # while verifying nothing at all. Pin the gates on the knob itself.
    with open(os.path.join(BENCHMARK_DIR, "netlist.cir")) as f:
        text = f.read()

    assert check_refdes_resolution(text, [KNOB]) == (True, None)
    assert check_param_applicability(text, [KNOB]) == (True, None)
    assert check_stimulus_untouched(text, [KNOB]) == (True, None)


def test_without_corners_the_ratio_guard_band_alone_admits_no_step(tmp_path):
    """spec.yaml declares no corners, so the guard band falls back to the
    declared ratio - and on this circuit the ratio is not usable. 0.2*|T| on
    the two-sided output windows wants vbgout_v >= 1.44 and <= 1.024 at once,
    which the 1.2389V baseline already violates before a single step.

    This is the measured case for "the allowance is derived, not guessed":
    the step below really does lower iq_ua and really does keep all 22
    criteria passing, and the guessed guard still throws it away."""
    spec, texts = _load("spec.yaml")
    state = RunState(run_dir=str(tmp_path), testbench_names=list(texts))
    state.push_netlist_version(texts)
    calls = []
    agents = OptimizerAgents(
        propose=_propose, simulate=_simulate_fn(spec, NgspiceBackend(timeout=180), calls)
    )

    result = asyncio.run(run_optimization(texts, spec, state, agents))

    assert result["status"] == "UNCHANGED"
    assert result["steps_accepted"] == 0
    assert result["steps_rejected"] == 1
    assert result["corner_confirmed"] is False
    assert result["pvt_sweep"] is None
    # measured 193..235uA, the range spec.yaml's comment quotes
    assert 193.0 <= result["objective_before"] <= 235.0
    assert result["objective_after"] == result["objective_before"]

    steps = _events(state, "optimize_step")
    assert len(steps) == 1
    step = steps[0]
    # 게이트가 아니라 가드밴드가 막았다는 것이 이 테스트의 전부다. gate가
    # None이 아니면 애초에 시뮬레이션도 돌지 않았고, 그러면 UNCHANGED는
    # 최적화가 아니라 주소 지정 실패를 증명하는 것이 된다.
    assert step["gate"] is None
    assert step["before"] == 8.0 and step["after"] == 7.2
    assert step["objective"] < result["objective_before"]
    assert "guarded limit" in step["reason"] and "vbgout_min" in step["reason"]
    assert len(calls) == 2  # baseline + the one step, both real ngspice
    # 거절된 단계는 되돌려진다 - 통과했던 덱 그대로 끝난다. 그 소자를 직접
    # 색인해서 본다: `"W=8" in text`는 다른 소자 여럿이 W=8이라 되돌아가지
    # 않아도 통과한다.
    assert _knob_width(state.current_netlist_texts()[spec.canonical.name]) == "8"


def test_the_optimizer_lowers_iq_while_every_criterion_still_passes(tmp_path):
    """The one place the whole phase meets real ngspice at once.

    **SLOW: measured 1790s (~30 min).** Six 45-corner sweeps at ~286s each
    plus one 5-testbench nominal simulation (~5.9s) per search step. It is by
    a wide margin the longest test in this suite. The cost is not incidental -
    it IS the finding, see below - and there is no cheaper way to exercise the
    corner anchor, which is what makes the guard band measured rather than
    guessed.

    Measured trace:
      - entry sweep passes (286.1s); allowances come from it, e.g.
        trim_loop_gain 10.6532dB, vbgout_min 0.0051V.
      - the nominal search accepts 10 steps, TRIMAMP.Xt W 8 -> 2.78943,
        iq_ua 212.9881 -> 211.6764uA. Step 11 is rejected by trim_loop_gain's
        MEASURED allowance (70.5517 against a guarded limit of 70.6532 = 60 +
        10.6532) - not by a criterion failing, and not by a gate.
      - **the confirmation sweep FAILS** on 6 criteria. A guard band derived
        from the corner spread at the STARTING point does not hold once the
        circuit has moved: draining the trim tail widens the spread it was
        measured against. This is the case bisection exists for, and it is
        real, not hypothetical.
      - bisection probes v5 (fail), v2, v3, v4 (pass) and lands on v4:
        W=5.2488, iq_ua 212.2517uA, corner-confirmed. 4 of the 10 nominal
        steps survive; the other 6 are re-counted as rejections.

    So the honest headline is that the nominal search overshoots by more than
    half and the confirmation is what makes the phase safe. The saving itself
    is small (-0.74uA of 213) because exactly one knob is pinned here - a real
    agent ranks several.

    **The failed confirmation is a measurement, not the contract.** The
    contract is: iq falls, every criterion still passes, corners confirm the
    deck that is returned, and the reported numbers describe THAT deck. Whether
    bisection had to run depends on ngspice and the PDK, and a benign shift
    that makes the confirmation pass would otherwise fail this test after 30
    minutes and read as a correctness regression. So the value assertions are
    unconditional and the bisection block is conditional, with the pass
    outcome's own (equally real) contract asserted in the else branch."""
    spec, texts = _load("spec_pvt.yaml")
    backend = NgspiceBackend(timeout=180)
    state = RunState(run_dir=str(tmp_path), testbench_names=list(texts))
    state.push_netlist_version(texts)
    calls, sweeps = [], []

    def verify_corners(netlist_texts):
        # 동기 콜러블이어야 한다 - run_optimization은 await 없이 부른다.
        sweeps.append(run_full_pvt_sweep(netlist_texts, spec, backend))
        return sweeps[-1]

    agents = OptimizerAgents(
        propose=_propose,
        simulate=_simulate_fn(spec, backend, calls),
        verify_corners=verify_corners,
    )

    started = time.monotonic()
    result = asyncio.run(run_optimization(texts, spec, state, agents))
    elapsed = time.monotonic() - started

    # elapsed는 이 테스트가 실패했을 때 "얼마나 걸려서 실패했는가"를 남긴다 -
    # 이분 탐색이 도는지 아닌지로 비용이 배로 갈리므로 그 자체가 진단이다.
    assert result["status"] == "OPTIMIZED", (round(elapsed), result)
    assert result["objective_after"] < result["objective_before"]
    assert result["steps_accepted"] >= 1
    assert result["corner_confirmed"] is True
    assert result["corner_failure"] is None  # 스윕은 전부 돌았다 - 터진 것이 없다
    assert result["pvt_sweep"]["overall_pass"] is True

    # 소자를 좁히는 방향이므로 면적은 줄어든다 - 예산은 이 실행을 묶은 것이
    # 아니다. 그래도 예산 관계를 함께 확인해 둔다.
    assert result["area_after"] < result["area_before"]
    assert result["area_after"] <= result["area_before"] * spec.optimize.area_budget

    steps = _events(state, "optimize_step")
    accepted = [s for s in steps if s["accepted"]]
    probes = _events(state, "optimize_bisect_probe")
    landing = _events(state, "optimize_bisect_result")
    # 돌려주는 덱을 만든 단계. 되돌아온 실행에서는 마지막 수락 단계가 아니다.
    survivor = accepted[result["steps_accepted"] - 1]

    # **여기부터가 조건부다.** 위의 값 단언들 - iq가 내려갔고, 모든 기준이
    # 여전히 통과하고, 코너가 확인되었다 - 이 이 테스트의 계약이고 그것들은
    # 무조건 참이어야 한다. 반면 "이분 탐색이 돌았다"는 계약이 아니라 **측정
    # 결과**다(2026-07-27 실측: 확인 스윕이 6개 기준에서 실패, 10단계 중 4단계
    # 생존). ngspice나 PDK가 조금 움직여 확인 스윕이 통과하게 되면 계약은
    # 그대로인데 이 30분짜리 테스트가 실패하고, 그것이 정확성 회귀로 읽힌다.
    # 그래서 양쪽 결말을 각각 고정한다 - 어느 쪽이든 "돌려주는 덱을 결과가
    # 설명한다"는 같은 사실을 검사한다.
    if landing:
        # 확인 스윕이 실패했다. 시작점에서 잰 가드밴드는 회로가 움직인 뒤에도
        # 유효하지 않고, 이것이 이분 탐색이 존재하는 이유다.
        assert len(landing) == 1
        assert len(probes) >= 1
        assert landing[0]["steps_kept"] == result["steps_accepted"]
        assert landing[0]["steps_walked_back"] > 0
        assert landing[0]["steps_kept"] + landing[0]["steps_walked_back"] == len(accepted)
        # 착지 지점은 통과가 **관측된** 버전이다. 앵커는 진입 스윕이, 나머지는
        # 프로브가 확인한 것뿐이다 - 확인되지 않은 버전에 착지하는 경로는 없다.
        verified = {p["version"] for p in probes if p["overall_pass"]} | {landing[0]["anchor"]}
        assert landing[0]["version"] in verified
        assert survivor["objective"] > accepted[-1]["objective"]  # 마지막 단계가 아니다
        walked_back = landing[0]["steps_walked_back"]
    else:
        # 확인 스윕이 통과했다. 되돌린 단계가 없으므로 돌려주는 덱은 마지막
        # 수락 단계의 것이고, 수락 수도 그대로다. 이분 탐색은 아예 돌지 않는다.
        assert not probes
        assert result["corner_confirmed"] is True
        assert result["steps_accepted"] == len(accepted)
        assert survivor is accepted[-1]
        assert result["objective_after"] == accepted[-1]["objective"]
        walked_back = 0

    # 스윕 예산: 진입 + 확인 + 이분 탐색 프로브. 다른 경로로 스윕을 더 돌면
    # 이 테스트의 30분이 조용히 더 늘어난다.
    assert len(sweeps) == 2 + len(probes)
    assert result["pvt_sweep"] in sweeps

    # 보고된 수치가 **돌려주는 덱**을 설명하는가. 되돌아온 실행에서 결과가
    # 착지하지 않은(마지막) 버전의 값을 들고 있으면 여기서 깨진다.
    final_texts = state.current_netlist_texts()
    assert result["final_netlist_paths"] == state.current_netlist_paths()
    assert total_area(final_texts[spec.canonical.name]).area == result["area_after"]
    assert float(_knob_width(final_texts[spec.canonical.name])) == survivor["after"]
    assert result["objective_after"] == survivor["objective"]
    # 리포트가 쓰는 기준 판정도 **착지한 버전**에서 잰 것이어야 한다 - 최적화
    # 전 덱의 판정을 실어 보내면 리포트가 서로 다른 두 회로를 나란히 적는다.
    reported = {c["name"]: c for c in result["final_criteria"]}
    assert reported and all(c["pass"] for c in reported.values())
    objective_criteria = [
        c.name for c in spec.all_criteria if c.measurement == spec.optimize.objective
    ]
    assert objective_criteria  # 목적값에 걸린 기준이 없으면 아래 검사가 무의미하다
    assert all(reported[name]["actual"] == survivor["objective"] for name in objective_criteria)

    # 수락된 단계마다 목적값이 실제로 내려갔다 - 단조 감소.
    objectives = [s["objective"] for s in accepted]
    assert objectives == sorted(objectives, reverse=True)
    # 각 단계는 앞 단계가 넷리스트에 쓴 값에서 이어진다. 어긋나면 탐색이
    # 매번 원본에서 다시 출발하고 있다는 뜻이다.
    assert [s["before"] for s in steps[1:]] == [s["after"] for s in steps[:-1]]

    # nominal 탐색을 끝낸 것은 게이트가 아니라 가드밴드다. gate가 None이 아니면
    # 주소 지정이 무너진 것이고, 그러면 이 테스트는 최적화가 아니라 그것을
    # 측정한 것이 된다.
    last = steps[-1]
    assert last["accepted"] is False and last["gate"] is None
    assert "guarded limit" in last["reason"]
    # 되돌린 단계는 거절로 다시 센다 - 보고하는 수가 돌려주는 덱을 설명해야 한다.
    assert result["steps_rejected"] == (len(steps) - len(accepted)) + walked_back
    # baseline + 한 단계당 하나. 스윕은 sim_backend를 직접 부르므로 여기 안 센다.
    assert len(calls) == len(steps) + 1
