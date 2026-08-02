"""면적 최소화 단계(`run_area_optimization`)의 벤치마크 실측. ngspice가 PATH에
있다고 가정한다(CLAUDE.md, "Setup").

수치를 못 박지 않는 이유: 이 값이 2단계(게이트 강등 + 대안 정렬)의 기준선이
되어야 하고, 2단계의 목적이 바로 그것을 **바꾸는 것**이다. 값을 핀하면 2단계가
성공할 때마다 이 테스트가 깨진다. 핀하는 것은 방향과 부작용 없음, 그리고 이
단계 자체가 낸 약속(무방비 기준을 전부 `unguarded_criteria`에 기록한다)뿐이다.

배선은 새로 발명하지 않았다 - `tests/unit/test_optimizer_bandgap_ngspice.py`의
`_load`/`_simulate_fn`/`_events` 패턴을 그대로 옮겼다. 브리프의 Step 1 스니펫은
`NgspiceBackend.run`에 텍스트를 직접 넘기지만 실제 시그니처는
`run(netlist_path: str, testbench_config: dict)`로 **경로**를 받는다 - 그래서
텍스트를 임시 파일로 써서 넘긴다. 같은 이유로 브리프의 `result.get('accepted')`도
쓰지 않는다 - 실제 키는 `steps_accepted`/`steps_rejected`다(컨트롤러 추가 지시
A).
"""

import asyncio
import json
import os
import time

import pytest

from analogcoder.area import total_area
from analogcoder.netlist import resolve_includes
from analogcoder.optimizer import OptimizerAgents, run_area_optimization
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.state import RunState

pytestmark = pytest.mark.slow

BENCHMARKS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks")
)


def _load(spec_path):
    spec = load_spec(spec_path)
    texts = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            # `.include`가 스펙 파일 기준 상대경로일 수 있다 - RunState가 덱을
            # run_dir로, NgspiceBackend가 다시 임시 디렉터리로 옮기므로 원본
            # 디렉터리 기준으로 미리 절대화해 둔다
            # (test_optimizer_bandgap_ngspice.py와 동일한 이유).
            texts[tb.name] = resolve_includes(f.read(), os.path.dirname(tb.netlist_path))
    return spec, texts


def _simulate_fn(backend, sim_dir, calls):
    async def simulate(netlist_texts, spec_arg):
        merged = {}
        for tb in spec_arg.testbenches:
            path = os.path.join(sim_dir, f"{tb.name}.cir")
            with open(path, "w") as f:
                f.write(netlist_texts[tb.name])
            merged.update(
                backend.run(path, {"control_block": tb.control_block}).measurements
            )
        calls.append(merged)
        return {"measurements": merged}

    return simulate


def _events(state, step):
    with open(state.history_path) as f:
        return [json.loads(line) for line in f if json.loads(line)["step"] == step]


def _run_area_phase(spec_path, tmp_path):
    """한 스펙에 대해 면적 단계를 실행하고 표 한 행을 채우는 데 필요한 값을
    전부 담은 dict를 돌려준다."""
    spec, texts = _load(spec_path)
    state = RunState(run_dir=str(tmp_path), testbench_names=list(texts))
    state.push_netlist_version(texts)
    backend = NgspiceBackend(timeout=180)
    sim_dir = str(tmp_path / "sim")
    os.makedirs(sim_dir, exist_ok=True)
    calls = []

    before = total_area(texts[spec.canonical.name]).area
    agents = OptimizerAgents(propose=None, simulate=_simulate_fn(backend, sim_dir, calls))

    started = time.monotonic()
    result = asyncio.run(run_area_optimization(texts, spec, state, agents))
    elapsed = time.monotonic() - started

    after = total_area(state.current_netlist_texts()[spec.canonical.name]).area

    ranking_events = _events(state, "optimize_area_ranking")
    baseline_events = _events(state, "optimize_area_baseline")

    return {
        "spec": spec,
        "result": result,
        "before": before,
        "after": after,
        "elapsed": elapsed,
        "ranking_events": ranking_events,
        "baseline_events": baseline_events,
        "calls": calls,
    }


def _print_row(spec_path, run):
    result = run["result"]
    before, after = run["before"], run["after"]
    ranking = run["ranking_events"][-1] if run["ranking_events"] else None
    baseline = run["baseline_events"][-1] if run["baseline_events"] else None
    total_criteria = len(run["spec"].all_criteria)
    reduction = (1 - after / before) * 100 if before else float("nan")
    print(f"\n=== {spec_path} ===")
    print(f"status={result['status']}")
    print(f"AREA {before:.6g} -> {after:.6g}  ({reduction:.2f}% 감소)")
    print(f"steps_accepted={result['steps_accepted']} steps_rejected={result['steps_rejected']}")
    if ranking is not None:
        print(
            f"zero_gain={ranking['zero_gain']} unknown={ranking['unknown']} "
            f"counted={ranking['counted']} skipped={ranking['skipped']}"
        )
    else:
        print("optimize_area_ranking: 이벤트 없음 (REFUSED 경로에서만 가능)")
    if baseline is not None:
        unguarded = baseline["unguarded_criteria"]
        print(f"unguarded_criteria={len(unguarded)}/{total_criteria}: {unguarded}")
    else:
        print("optimize_area_baseline: 이벤트 없음")
    print(f"elapsed={run['elapsed']:.1f}s ({len(run['calls'])} simulations)")


def test_the_area_phase_reduces_area_on_bandgap_without_breaking_criteria(tmp_path):
    """bandgap(5 테스트벤치, 22 기준)에서 면적 단계가 실제로 면적을 줄이는가,
    줄인다면 모든 기준을 통과한 채인가. 2단계의 기준선을 만드는 실행이다."""
    spec_path = os.path.join(BENCHMARKS_DIR, "bandgap", "spec.yaml")
    run = _run_area_phase(spec_path, tmp_path)
    result = run["result"]

    assert result["status"] in {"OPTIMIZED", "UNCHANGED"}
    # 방향만 핀한다. 커지는 일은 절대 없어야 한다 - 수락 규칙이 목적의 하강을
    # 요구하므로, 커졌다면 규칙이 우회된 것이다.
    assert run["after"] <= run["before"]
    if result["status"] == "OPTIMIZED":
        assert run["after"] < run["before"]
        assert all(c["pass"] for c in result["final_criteria"])

    # 이벤트는 실행 하나에서 반드시 남는다 - REFUSED가 아닌 한(면적 모델이
    # 아무것도 못 읽은 경우) 둘 다 정확히 한 번씩.
    assert len(run["ranking_events"]) == 1
    assert len(run["baseline_events"]) >= 1
    baseline = run["baseline_events"][-1]
    # 빈 리스트와 키 부재는 다른 사실이다 - 키가 있는지부터 확인한다.
    assert "unguarded_criteria" in baseline

    _print_row(spec_path, run)


def test_the_area_phase_reduces_area_on_two_stage_opamp_without_breaking_criteria(tmp_path):
    """two_stage_opamp(4 테스트벤치, 7 기준)에서 같은 것을 잰다 - 표의 둘째
    행."""
    spec_path = os.path.join(BENCHMARKS_DIR, "two_stage_opamp", "spec.yaml")
    run = _run_area_phase(spec_path, tmp_path)
    result = run["result"]

    assert result["status"] in {"OPTIMIZED", "UNCHANGED"}
    assert run["after"] <= run["before"]
    if result["status"] == "OPTIMIZED":
        assert run["after"] < run["before"]
        assert all(c["pass"] for c in result["final_criteria"])

    assert len(run["ranking_events"]) == 1
    assert len(run["baseline_events"]) >= 1
    baseline = run["baseline_events"][-1]
    assert "unguarded_criteria" in baseline

    _print_row(spec_path, run)
