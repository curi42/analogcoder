import types

from analogcoder.corner_selection import NOMINAL, CornerSet
from analogcoder.corner_sim import CornerState, build_corner_simulate
from analogcoder.pvt import CornerPoint
from analogcoder.simulators.base import RawSimResult
from analogcoder.spec import CornerReduction, Criterion
from analogcoder.state import RunState

FS = CornerPoint(process="fs", voltage=1.98, temperature=125.0)

DECK = """\
* deck
.include "pdk_corner.inc"
Vdd vdd 0 DC 1.8
.end
"""

SPEC_CONTROL_BLOCK = ".ac dec 10 1 1G"


def _state(tmp_path, texts=None):
    state = RunState(run_dir=str(tmp_path / "run"), testbench_names=["tb"])
    state.push_netlist_version(texts or {"tb": DECK})
    return state


def _spec_ge_40(criteria=None, corner_reduction=None):
    """A spec double carrying only what corner_sim reads: the canonical
    testbench's netlist_path (for benchmark_dir), each testbench's name /
    control_block, and all_criteria. Same SimpleNamespace pattern as
    test_pvt.py / test_corner_selection.py. benchmark_dir need not exist -
    render_corner_netlist only builds strings out of it."""
    criteria = criteria or [Criterion(name="gain", measurement="g", operator=">=", threshold=40.0)]
    tb = types.SimpleNamespace(
        name="tb",
        netlist_path="/benchmarks/x/netlist.cir",
        control_block=SPEC_CONTROL_BLOCK,
        criteria=list(criteria),
    )
    return types.SimpleNamespace(
        circuit_name="x",
        testbenches=[tb],
        canonical=tb,
        all_criteria=list(criteria),
        corner_reduction=corner_reduction,
        pvt_corners=None,
    )


def _agent(measurements=None, control_block=None, status="success"):
    """A stand-in for agents.simulator_agent.simulate already bound to its
    sim/agent backends - corner_sim calls it as (netlist_path, control_block)."""

    async def fake(netlist_path, control_block_arg):
        return {
            "status": status,
            "measurements": dict(measurements or {}),
            "warnings": [],
            "control_block": control_block if control_block is not None else control_block_arg,
        }

    return fake


class _FakeBackend:
    """Returns the queued measurement dicts in call order and records every
    call, so a test can assert both what came back and what went in."""

    def __init__(self, results, raise_on_call=None, statuses=None):
        self._results = [dict(r) for r in results]
        self._statuses = list(statuses or [])
        self._raise_on_call = raise_on_call
        self.calls: list[dict] = []

    def run(self, netlist_path, testbench_config):
        self.calls.append({"netlist_path": netlist_path, **testbench_config})
        if self._raise_on_call is not None and len(self.calls) == self._raise_on_call:
            raise RuntimeError("ngspice exploded")
        status = self._statuses.pop(0) if self._statuses else "success"
        return RawSimResult(status=status, measurements=self._results.pop(0), raw_log="")


def _backend(results, statuses=None):
    return _FakeBackend(results, statuses=statuses)


def _backend_raising_on_call(call_number, results):
    return _FakeBackend(results, raise_on_call=call_number)


def _noop_log(step, data):
    return None


def _recording_log(events):
    def log(step, data):
        events.append((step, data))

    return log


# --- 선택 집합의 최악값 -------------------------------------------------------


async def test_the_judge_sees_the_worst_across_the_selected_corners(tmp_path):
    # nominal 50, 코너 41 -> judge는 41을 봐야 한다. nominal만 넘기는 변형,
    # 혹은 평균/최대를 취하는 변형을 이 단언이 잡는다.
    state = _state(tmp_path)
    cs = CornerSet(corners=(NOMINAL, FS), probe_order=())
    sim = build_corner_simulate(
        _agent(measurements={"g": 999.0}),  # 에이전트 값은 쓰이지 않는다
        _backend([{"g": 50.0}, {"g": 41.0}]),  # 직접 nominal, 코너
        state,
        CornerState(cs),
        _noop_log,
    )

    result = await sim({"tb": DECK}, _spec_ge_40())

    assert result["measurements"]["g"] == 41.0
    # 진단도 같은 코너를 가리켜야 한다 - 측정값과 코너를 어긋나게 zip하는
    # 변형은 41.0을 (deck)의 값이라고 보고한다.
    assert result["corner_worst"]["gain"]["process"] == "fs"
    assert result["corner_worst"]["gain"]["value"] == 41.0


async def test_the_agent_s_own_measurements_are_not_used(tmp_path):
    # 에이전트 경로와 직접 경로의 키 집합이 다를 수 있다는 것이 이 설계가
    # nominal을 직접 경로로 한 번 더 도는 이유다. 에이전트 값을 섞는 변형은
    # 그 이유를 무효로 만든다. agent_only에도 기준을 걸어 두어, 에이전트
    # 측정값을 코너 측정값 **앞에** 병합하는(그래서 g는 덮여 보이지 않는)
    # 변형까지 잡는다.
    state = _state(tmp_path)
    criteria = [
        Criterion(name="gain", measurement="g", operator=">=", threshold=40.0),
        Criterion(name="agent_only", measurement="agent_only", operator=">=", threshold=1.0),
    ]
    sim = build_corner_simulate(
        _agent(measurements={"g": 1.0, "agent_only": 7.0}),
        _backend([{"g": 50.0}]),
        state,
        CornerState(CornerSet(corners=(NOMINAL,), probe_order=())),
        _noop_log,
    )

    result = await sim({"tb": DECK}, _spec_ge_40(criteria=criteria))

    assert result["measurements"] == {"g": 50.0}
    assert "agent_only" not in result["measurements"]


async def test_a_measurement_missing_at_any_selected_corner_is_withheld(tmp_path):
    # V1의 규칙. 한 코너에서 값이 안 나오면 다른 코너가 그것을 가려서는 안 된다.
    state = _state(tmp_path)
    cs = CornerSet(corners=(NOMINAL, FS), probe_order=())
    sim = build_corner_simulate(
        _agent(), _backend([{"g": 50.0}, {}]), state, CornerState(cs), _noop_log
    )

    result = await sim({"tb": DECK}, _spec_ge_40())

    assert "g" not in result["measurements"]


async def test_a_worst_case_at_the_deck_itself_is_reported_as_the_deck(tmp_path):
    # NOMINAL은 None이라 CornerPoint의 필드가 없다. 그것을 좌표처럼 읽으려는
    # 변형은 AttributeError로 터지고, tt/27 같은 실제 코너 이름을 지어내는
    # 변형은 이 단언이 잡는다 - 덱 그대로는 어떤 코너도 아니다.
    state = _state(tmp_path)
    sim = build_corner_simulate(
        _agent(),
        _backend([{"g": 50.0}]),
        state,
        CornerState(CornerSet(corners=(NOMINAL,), probe_order=())),
        _noop_log,
    )

    result = await sim({"tb": DECK}, _spec_ge_40())

    assert result["corner_worst"]["gain"]["process"] == "(deck)"
    assert result["corner_worst"]["gain"]["value"] == 50.0


async def test_the_corners_reuse_the_control_block_the_agent_settled_on(tmp_path):
    # 코너가 스펙 원문을 쓰면 수렴 재시도의 이득을 못 받는다.
    state = _state(tmp_path)
    cs = CornerSet(corners=(NOMINAL, FS), probe_order=())
    backend = _backend([{"g": 50.0}, {"g": 41.0}])
    sim = build_corner_simulate(
        _agent(control_block=".options gmin=1e-10\n.ac dec 10 1 1G"),
        backend,
        state,
        CornerState(cs),
        _noop_log,
    )

    await sim({"tb": DECK}, _spec_ge_40())

    assert len(backend.calls) == 2
    assert all(c["control_block"].startswith(".options gmin=1e-10") for c in backend.calls)


async def test_the_spec_s_control_block_is_used_when_the_agent_supplies_none(tmp_path):
    # 폴백은 에이전트가 아무것도 안 줬을 때만이다. 빈 문자열/누락을 그대로
    # 코너에 넘기는 변형은 코너가 아무 분석도 없이 도는 것이라 조용히 측정값이
    # 사라진다.
    state = _state(tmp_path)

    async def agent_without_control_block(netlist_path, control_block_arg):
        return {"status": "success", "measurements": {}, "warnings": []}

    backend = _backend([{"g": 50.0}])
    sim = build_corner_simulate(
        agent_without_control_block,
        backend,
        state,
        CornerState(CornerSet(corners=(NOMINAL,), probe_order=())),
        _noop_log,
    )

    await sim({"tb": DECK}, _spec_ge_40())

    assert backend.calls[0]["control_block"] == SPEC_CONTROL_BLOCK


# --- 탐침 -------------------------------------------------------------------


async def test_a_failing_probe_does_not_change_what_the_judge_sees(tmp_path):
    # 탐침이 판정에 섞이면 축소 집합의 낙관성 논증이 흐려진다. 탐침 값을
    # worst_case에 넣는 변형을 이 단언이 잡는다.
    state = _state(tmp_path)
    cs = CornerSet(corners=(NOMINAL,), probe_order=(FS,))
    sim = build_corner_simulate(
        _agent(),
        _backend([{"g": 50.0}, {"g": 10.0}]),  # nominal, 탐침
        state,
        CornerState(cs),
        _noop_log,
    )

    result = await sim({"tb": DECK}, _spec_ge_40())

    assert result["measurements"]["g"] == 50.0


async def test_a_failing_probe_is_promoted_into_the_selected_set(tmp_path):
    state = _state(tmp_path)
    holder = CornerState(CornerSet(corners=(NOMINAL,), probe_order=(FS,)))
    sim = build_corner_simulate(
        _agent(), _backend([{"g": 50.0}, {"g": 10.0}]), state, holder, _noop_log
    )

    await sim({"tb": DECK}, _spec_ge_40())

    assert FS in holder.corner_set.corners


async def test_a_passing_probe_is_not_promoted(tmp_path):
    state = _state(tmp_path)
    holder = CornerState(CornerSet(corners=(NOMINAL,), probe_order=(FS,)))
    sim = build_corner_simulate(
        _agent(), _backend([{"g": 50.0}, {"g": 49.0}]), state, holder, _noop_log
    )

    await sim({"tb": DECK}, _spec_ge_40())

    assert FS not in holder.corner_set.corners
    # 통과한 탐침은 회전 순서에도 그대로 남는다 - 다음 반복에 같은 코너를
    # 다시 고르지 않도록 index만 진행한다.
    assert holder.corner_set.probe_order == (FS,)
    assert holder.corner_set.probe_index == 1


async def test_a_probe_that_raises_does_not_stop_the_iteration(tmp_path):
    # 탐침은 판정에 참여하지 않으므로 실패가 루프를 멈출 이유가 없다.
    state = _state(tmp_path)
    holder = CornerState(CornerSet(corners=(NOMINAL,), probe_order=(FS,)))
    events: list = []
    sim = build_corner_simulate(
        _agent(),
        _backend_raising_on_call(2, [{"g": 50.0}]),
        state,
        holder,
        _recording_log(events),
    )

    result = await sim({"tb": DECK}, _spec_ge_40())

    assert result["measurements"]["g"] == 50.0
    assert FS not in holder.corner_set.corners
    # 조용히 삼키면 탐침이 매 반복 터지고 있어도 아무 데도 안 남는다 -
    # 이 저장소가 반복해서 당한 "조용히 무력한 게이트"와 같은 모양이다.
    assert [d for step, d in events if step == "corner_probe" and d.get("error")]


async def test_the_probe_is_logged_with_its_corner_and_outcome(tmp_path):
    state = _state(tmp_path)
    holder = CornerState(CornerSet(corners=(NOMINAL,), probe_order=(FS,)))
    events: list = []
    sim = build_corner_simulate(
        _agent(), _backend([{"g": 50.0}, {"g": 10.0}]), state, holder, _recording_log(events)
    )

    await sim({"tb": DECK}, _spec_ge_40())

    probe_events = [d for step, d in events if step == "corner_probe"]
    assert probe_events == [{"corner": "fs/1.98/125.0", "failed": True, "promoted": True}]


async def test_the_probe_is_skipped_when_the_spec_turns_it_off(tmp_path):
    # probe: false는 "탐침을 돌지 마라"이다. 플래그를 무시하는 변형은 코너를
    # 하나 더 돈다.
    state = _state(tmp_path)
    holder = CornerState(CornerSet(corners=(NOMINAL,), probe_order=(FS,)))
    backend = _backend([{"g": 50.0}])
    sim = build_corner_simulate(_agent(), backend, state, holder, _noop_log)

    result = await sim(
        {"tb": DECK},
        _spec_ge_40(corner_reduction=CornerReduction(enabled=True, retry_budget=2, probe=False)),
    )

    assert len(backend.calls) == 1
    assert result["probe"] is None
    assert holder.corner_set.probe_index == 0


# --- 기존 simulate_fn 계약 ---------------------------------------------------


async def test_the_status_folds_every_testbench_and_every_corner(tmp_path):
    # 전부 성공했을 때만 성공이다. 코너의 status를 무시하는 변형은 수렴하지
    # 못한 코너의 결과를 성공으로 보고하고, optimizer가 그 측정값으로 마진을
    # 태운다.
    state = _state(tmp_path)
    cs = CornerSet(corners=(NOMINAL, FS), probe_order=())
    backend = _backend(
        [{"g": 50.0}, {"g": 41.0}], statuses=["success", "convergence_failure"]
    )
    sim = build_corner_simulate(_agent(), backend, state, CornerState(cs), _noop_log)

    result = await sim({"tb": DECK}, _spec_ge_40())

    assert result["status"] == "convergence_failure"


async def test_by_testbench_carries_the_agent_result(tmp_path):
    state = _state(tmp_path)
    sim = build_corner_simulate(
        _agent(measurements={"g": 999.0}, status="success"),
        _backend([{"g": 50.0}]),
        state,
        CornerState(CornerSet(corners=(NOMINAL,), probe_order=())),
        _noop_log,
    )

    result = await sim({"tb": DECK}, _spec_ge_40())

    assert result["by_testbench"]["tb"]["measurements"] == {"g": 999.0}
    assert set(result) >= {"status", "measurements", "by_testbench", "corner_worst", "probe"}


async def test_a_corner_run_gets_the_corner_rendered_deck_and_nominal_the_deck_itself(tmp_path):
    # nominal은 덱 그대로여야 한다 - 코너 렌더링을 거치면 그것은 더 이상
    # "덱 그대로"가 아니고, 임계값이 정해진 기준점이 사라진다.
    state = _state(tmp_path)
    cs = CornerSet(corners=(NOMINAL, FS), probe_order=())
    backend = _backend([{"g": 50.0}, {"g": 41.0}])
    sim = build_corner_simulate(_agent(), backend, state, CornerState(cs), _noop_log)

    await sim({"tb": DECK}, _spec_ge_40())

    nominal_call, corner_call = backend.calls
    assert nominal_call["netlist_path"] == state.current_netlist_paths()["tb"]
    with open(nominal_call["netlist_path"]) as f:
        assert ".temp" not in f.read()
    assert corner_call["netlist_path"] != nominal_call["netlist_path"]
