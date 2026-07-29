import types

import pytest

from analogcoder.corner_selection import NOMINAL, CornerSet
from analogcoder.corner_sim import CornerState, build_corner_simulate
from analogcoder.pvt import CornerPoint
from analogcoder.simulators.base import RawSimResult
from analogcoder.spec import CornerReduction, Criterion
from analogcoder.state import RunState

FS = CornerPoint(process="fs", voltage=1.98, temperature=125.0)
SS = CornerPoint(process="ss", voltage=1.62, temperature=-40.0)

DECK = """\
* deck
.include "pdk_corner.inc"
Vdd vdd 0 DC 1.8
.end
"""

DECK2 = """\
* second testbench deck
.include "pdk_corner.inc"
Vdd vdd 0 DC 1.8
Vin in 0 DC 0 AC 1
.end
"""

SPEC_CONTROL_BLOCK = ".ac dec 10 1 1G"


@pytest.fixture(autouse=True)
def _sequential_points(monkeypatch):
    """이 파일의 대역은 **호출 순서로 점을 식별한다** - `_FakeBackend`는 큐에서
    측정값을 pop하고 `raise_on_call`은 몇 번째 호출인지로 터진다. corner_sim이
    한 테스트벤치의 코너들을 풀에 던지므로, 순서를 고정하지 않으면 이 파일
    전체가 산발적으로 깨진다(실제로 깨졌다).

    대역을 내용으로 색인하도록 고치는 길은 여기서 막혀 있다: 결과가 위치
    리스트로 주어지므로 어느 결과가 어느 점의 것인지는 순서 말고 표현할 자리가
    없다. 그래서 한계를 정직하게 적고 순차로 고정한다.

    **병렬 경로가 값을 바꾸지 않는다는 주장은 이 파일이 지지 않는다** -
    `test_sweep_cache_parallel_ngspice.py`가 진짜 ngspice로 순차와 병렬을
    붙여 놓고, 탐침을 포함한 결과 전체가 같은지 확인한다."""
    monkeypatch.setenv("ANALOGCODER_SIM_WORKERS", "1")


def _state(tmp_path, texts=None):
    state = RunState(run_dir=str(tmp_path / "run"), testbench_names=["tb"])
    state.push_netlist_version(texts or {"tb": DECK})
    return state


def _two_tb_state(tmp_path):
    state = RunState(run_dir=str(tmp_path / "run"), testbench_names=["tb1", "tb2"])
    state.push_netlist_version({"tb1": DECK, "tb2": DECK2})
    return state


def _two_tb_texts():
    return {"tb1": DECK, "tb2": DECK2}


def _spec_two_testbenches():
    """두 테스트벤치, 각각 자기 기준. all_criteria가 둘을 합치므로 탐침이
    테스트벤치 하나 분량의 측정값으로 판정되면 나머지 기준이 "측정값 없음"으로
    실패한다 - 테스트벤치가 하나뿐인 스펙으로는 그 차이가 아예 안 보인다."""
    tb1 = types.SimpleNamespace(
        name="tb1",
        netlist_path="/benchmarks/x/netlist.cir",
        control_block=SPEC_CONTROL_BLOCK,
        criteria=[Criterion(name="gain", measurement="g", operator=">=", threshold=40.0)],
    )
    tb2 = types.SimpleNamespace(
        name="tb2",
        netlist_path="/benchmarks/x/netlist_psr.cir",
        control_block=".ac dec 10 1 1meg",
        criteria=[Criterion(name="psr", measurement="h", operator=">=", threshold=10.0)],
    )
    return types.SimpleNamespace(
        circuit_name="x",
        testbenches=[tb1, tb2],
        canonical=tb1,
        all_criteria=[*tb1.criteria, *tb2.criteria],
        corner_reduction=None,
        pvt_corners=None,
    )


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


def _agent_sequence(results, seen_paths=None):
    """호출 순서대로 결과를 주는 에이전트 대역. 테스트벤치마다 다른 status를
    돌려주려면 하나의 고정 결과로는 부족하다."""
    queue = list(results)

    async def fake(netlist_path, control_block_arg):
        if seen_paths is not None:
            seen_paths.append(netlist_path)
        result = dict(queue.pop(0))
        result.setdefault("status", "success")
        result.setdefault("measurements", {})
        result.setdefault("warnings", [])
        result.setdefault("control_block", control_block_arg)
        return result

    return fake


class _FakeBackend:
    """Returns the queued measurement dicts in call order and records every
    call, so a test can assert both what came back and what went in.

    It reads the deck **at call time** and keeps the text. A double that only
    records the path cannot tell a corner-rendered deck from the nominal one -
    and the corner file lives in a TemporaryDirectory that is gone before the
    assertions run, so reading it later is not an option either. Replacing
    render_corner_netlist with `rendered = netlist_text` (i.e. every corner
    silently simulating the unrendered deck, which makes this whole
    sub-project a no-op) was invisible to the path-only version of this
    double."""

    def __init__(self, results, raise_on_call=None, statuses=None):
        self._results = [dict(r) for r in results]
        self._statuses = list(statuses or [])
        self._raise_on_call = raise_on_call
        self.calls: list[dict] = []

    def run(self, netlist_path, testbench_config):
        with open(netlist_path) as f:
            deck = f.read()
        self.calls.append({"netlist_path": netlist_path, "deck": deck, **testbench_config})
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
    holder = CornerState(CornerSet(corners=(NOMINAL,), probe_order=(FS, SS)))
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
    probe_events = [d for step, d in events if step == "corner_probe"]
    assert len(probe_events) == 1
    # 터진 탐침의 기록은 정상 기록과 **같은 모양**이어야 한다. failed/promoted가
    # 없으면 record["failed"]를 읽는 소비자는 KeyError, record.get("failed")를
    # 읽는 소비자는 터진 탐침을 "통과한 탐침"으로 읽는다.
    assert set(probe_events[0]) == {"corner", "failed", "promoted", "error"}
    assert probe_events[0]["failed"] is False
    assert probe_events[0]["promoted"] is False
    assert probe_events[0]["error"].startswith("RuntimeError:")
    assert result["probe"] == probe_events[0]
    # 터진 탐침도 **회전은 진행된 채 커밋된다** - 그래야 매 반복 같은 코너에서
    # 계속 터지지 않고 한 바퀴 뒤에 다시 온다. 결과만 없던 것으로 한다.
    assert holder.corner_set.probe_index == 1
    assert holder.corner_set.probe_order == (FS, SS)


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


async def test_a_non_success_corner_status_folds_in_and_names_the_corner(tmp_path):
    # 전부 성공했을 때만 성공이다. 코너의 status를 무시하는 변형은 수렴하지
    # 못한 코너의 결과를 성공으로 보고하고, optimizer가 그 측정값으로 마진을
    # 태운다. optimizer._run_simulation은 이 문자열을 그대로 사유에 적으므로
    # 좌표가 실려 있지 않으면 어느 코너였는지가 사라진다.
    state = _two_tb_state(tmp_path)
    cs = CornerSet(corners=(NOMINAL, FS), probe_order=())
    backend = _backend(
        [{"g": 50.0}, {"g": 41.0}, {"h": 20.0}, {"h": 15.0}],
        statuses=["success", "convergence_failure", "success", "success"],
    )
    sim = build_corner_simulate(
        _agent_sequence([{}, {}]), backend, state, CornerState(cs), _noop_log
    )

    result = await sim(_two_tb_texts(), _spec_two_testbenches())

    assert result["status"] == "convergence_failure at fs/1.98/125.0 in testbench tb1"


async def test_a_non_success_agent_status_folds_across_testbenches(tmp_path):
    # 두 번째 테스트벤치의 **에이전트**가 실패를 보고한 경우. 테스트벤치가
    # 하나뿐인 스펙에서는 이 접기가 코너 쪽 접기와 구분되지 않아, 에이전트
    # 쪽을 통째로 지우는 변형(`if False and ...`)이 그대로 통과한다.
    state = _two_tb_state(tmp_path)
    seen_paths: list = []
    backend = _backend([{"g": 50.0}, {"h": 20.0}])
    sim = build_corner_simulate(
        _agent_sequence([{"status": "success"}, {"status": "error"}], seen_paths),
        backend,
        state,
        CornerState(CornerSet(corners=(NOMINAL,), probe_order=())),
        _noop_log,
    )

    result = await sim(_two_tb_texts(), _spec_two_testbenches())

    assert result["status"] == "error"
    # 에이전트 쪽 값은 cli.py의 기존 simulate_fn과 글자 그대로 같다 - 좌표가
    # 붙어 있다는 것 자체가 "코너가 낸 실패"라는 표시이므로, 여기 붙이면 그
    # 구분이 사라진다.
    assert seen_paths == [state.current_netlist_paths()[n] for n in ("tb1", "tb2")]


# --- 여러 테스트벤치를 가로지르는 탐침 ----------------------------------------


async def test_the_probe_is_judged_once_after_every_testbench_has_run(tmp_path):
    # 탐침 시뮬레이션은 테스트벤치마다 돌지만, 판정·승격·기록은 **전부 돈 뒤
    # 한 번**이다. 판정 블록을 테스트벤치 루프 안으로 옮기는 변형은 tb1만 돈
    # 시점에 tb2의 기준을 "측정값 없음"으로 실패시켜, 크래시가 아니라 절반짜리
    # 측정값을 근거로 코너를 승격시킨다 - 기록도 두 번 남는다.
    state = _two_tb_state(tmp_path)
    holder = CornerState(CornerSet(corners=(NOMINAL,), probe_order=(FS,)))
    events: list = []
    backend = _backend([{"g": 50.0}, {"g": 45.0}, {"h": 20.0}, {"h": 15.0}])
    sim = build_corner_simulate(
        _agent_sequence([{}, {}]), backend, state, holder, _recording_log(events)
    )

    result = await sim(_two_tb_texts(), _spec_two_testbenches())

    assert len(backend.calls) == 4  # 테스트벤치마다 nominal + 탐침
    probe_events = [d for step, d in events if step == "corner_probe"]
    assert probe_events == [{"corner": "fs/1.98/125.0", "failed": False, "promoted": False}]
    assert FS not in holder.corner_set.corners
    assert result["measurements"] == {"g": 50.0, "h": 20.0}


async def test_a_probe_that_raises_skips_the_remaining_testbenches(tmp_path):
    # 한 테스트벤치의 탐침이 터진 뒤 나머지 테스트벤치의 탐침을 계속 돌면,
    # 판정에 들어가는 측정값이 절반짜리가 되어 나머지 기준이 "측정값 없음"으로
    # 실패한다 - 크래시를 근거로 코너가 승격된다.
    state = _two_tb_state(tmp_path)
    holder = CornerState(CornerSet(corners=(NOMINAL,), probe_order=(FS,)))
    events: list = []
    backend = _backend_raising_on_call(2, [{"g": 50.0}, {"h": 20.0}])
    sim = build_corner_simulate(
        _agent_sequence([{}, {}]), backend, state, holder, _recording_log(events)
    )

    result = await sim(_two_tb_texts(), _spec_two_testbenches())

    assert len(backend.calls) == 3  # tb1 nominal, tb1 탐침(터짐), tb2 nominal
    assert FS not in holder.corner_set.corners
    assert result["measurements"] == {"g": 50.0, "h": 20.0}
    assert [d for step, d in events if step == "corner_probe"] == [result["probe"]]
    assert result["probe"]["error"].startswith("RuntimeError:")


# --- 넷리스트 출처의 불변식 --------------------------------------------------


async def test_a_netlist_argument_that_lags_the_run_state_is_rejected(tmp_path):
    # nominal은 state의 **파일**을 돌고 코너는 **인자**를 렌더링한다. 둘이
    # 어긋나면 nominal은 새 덱을, 코너는 옛 덱을 도는데 키 집합은 여전히 같아
    # 아무 데서도 티가 나지 않는다. 검사를 지우는 변형은 이 실행을 조용히
    # 통과시킨다.
    state = _state(tmp_path)
    cs = CornerSet(corners=(NOMINAL, FS), probe_order=())
    sim = build_corner_simulate(
        _agent(), _backend([{"g": 50.0}, {"g": 41.0}]), state, CornerState(cs), _noop_log
    )

    with pytest.raises(ValueError, match="'tb'"):
        await sim({"tb": DECK + "* tuned\n"}, _spec_ge_40())


async def test_a_second_testbench_mismatch_costs_no_agent_call(tmp_path):
    """결정론적 게이트는 LLM 호출보다 **먼저** 돈다 - 이 저장소의 문서화된 순서다.

    검사가 에이전트 루프 안에 있으면 tb2의 불일치는 tb1의 LLM 호출을 이미
    쓴 뒤에야 발견된다. 면적 게이트와 refdes 게이트가 `verify_pre` 앞에
    있는 것과 같은 이유다.

    **어떤 변형을 잡는가.** 검사를 다시 에이전트 루프 안으로 되돌리는 변형.
    그러면 agent 호출 수가 0이 아니라 1이 된다.
    """
    state = _two_tb_state(tmp_path)
    calls: list = []
    sim = build_corner_simulate(
        _agent_sequence([{}, {}], seen_paths=calls),
        _backend([{"g": 50.0}, {"h": 20.0}]),
        state,
        CornerState(CornerSet(corners=(NOMINAL,), probe_order=())),
        _noop_log,
    )

    # tb1은 일치하고 tb2만 어긋난다.
    with pytest.raises(ValueError, match="'tb2'"):
        await sim({"tb1": DECK, "tb2": DECK2 + "* tuned\n"}, _spec_two_testbenches())

    assert calls == []


async def test_the_rotation_is_committed_even_when_a_selected_corner_raises(tmp_path):
    # 선택 코너의 실패는 판정 경로라 삼킬 수 없고 그대로 올라간다. 그런데
    # optimizer._run_simulation이 그 예외를 삼키고 계속 돌기 때문에, 회전을
    # 성공 경로에서만 커밋하면 상자는 조용히 같은 탐침 코너에 영원히 머문다.
    state = _state(tmp_path)
    holder = CornerState(CornerSet(corners=(NOMINAL, FS), probe_order=(SS,)))
    sim = build_corner_simulate(
        _agent(), _backend_raising_on_call(2, [{"g": 50.0}]), state, holder, _noop_log
    )

    with pytest.raises(RuntimeError):
        await sim({"tb": DECK}, _spec_ge_40())

    assert holder.corner_set.probe_index == 1


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
    # 코너가 렌더링되지 않으면 축소 집합은 **똑같은 덱 여러 개**가 되고 이
    # 하위 프로젝트 전체가 조용히 아무 일도 하지 않는다. 경로만 비교하는
    # 단언은 `rendered = netlist_text` 변형을 통과시킨다 - 덱 본문을 본다.
    # nominal은 반대로 덱 그대로여야 한다: 렌더링을 거치면 그것은 더 이상
    # 임계값이 정해진 그 덱이 아니다.
    state = _state(tmp_path)
    cs = CornerSet(corners=(NOMINAL, FS), probe_order=())
    backend = _backend([{"g": 50.0}, {"g": 41.0}])
    sim = build_corner_simulate(_agent(), backend, state, CornerState(cs), _noop_log)

    await sim({"tb": DECK}, _spec_ge_40())

    nominal_call, corner_call = backend.calls
    assert nominal_call["netlist_path"] == state.current_netlist_paths()["tb"]
    assert nominal_call["deck"] == DECK
    assert ".temp" not in nominal_call["deck"]
    assert "pdk_corner_fs.inc" not in nominal_call["deck"]

    # fs/1.98/125.0의 세 축이 전부 덱에 실려야 한다 - 하나만 보면 공정만
    # 갈아끼우고 온도를 빼먹는 변형을 놓친다.
    assert "pdk_corner_fs.inc" in corner_call["deck"]
    assert ".temp 125.0" in corner_call["deck"]
    assert "Vdd vdd 0 DC 1.98" in corner_call["deck"]
    assert corner_call["netlist_path"] != nominal_call["netlist_path"]


# --- 최적화 동안의 회전 얼림 ---------------------------------------------------


async def test_a_frozen_box_runs_no_probe_and_promotes_nothing(tmp_path):
    """`probe_frozen`이면 탐침도, 승격도, 회전도 없다.

    최적화기는 이 상자를 메인 루프와 **일부러** 공유하므로, 얼리지 않으면
    `_search` 안의 매 시뮬레이션이 탐침을 하나 돌리고 실패 시 코너를 승격시킨다.
    그러면 `records`의 목적값들이 서로 다른 코너 집합에서 잰 값이 되고, 승격이
    최악값 목적을 악화시킨 뒤의 모든 단계가 원인이 아닌 knob을 지목하는 사유로
    거부된다.

    **어떤 변형을 잡는가.** `_probe_enabled(spec) and not corner_state.probe_frozen`
    에서 얼림 항을 지우는 변형. 그러면 백엔드 호출이 1이 아니라 2가 되고 FS가
    선택 집합으로 올라간다.
    """
    state = _state(tmp_path)
    # 얼리지 않았다면 실패했을 탐침(g=10.0 < 40)을 준비해 둔다.
    holder = CornerState(CornerSet(corners=(NOMINAL,), probe_order=(FS,)), probe_frozen=True)
    backend = _backend([{"g": 50.0}, {"g": 10.0}])
    events: list = []
    sim = build_corner_simulate(
        _agent(), backend, state, holder, lambda step, data: events.append((step, data))
    )

    result = await sim({"tb": DECK}, _spec_ge_40())

    assert len(backend.calls) == 1                    # nominal 하나뿐
    assert result["probe"] is None
    assert FS not in holder.corner_set.corners
    assert holder.corner_set.probe_index == 0         # 회전도 진행되지 않는다
    assert [step for step, _ in events] == []


async def test_unfreezing_the_box_resumes_the_rotation(tmp_path):
    # 반대 방향 고정. probe_frozen을 항상 True로 박는 변형(= 탐침을 통째로
    # 죽이는 것)은 위 테스트만으로는 살아남는다.
    state = _state(tmp_path)
    holder = CornerState(CornerSet(corners=(NOMINAL,), probe_order=(FS,)), probe_frozen=True)
    backend = _backend([{"g": 50.0}, {"g": 50.0}, {"g": 10.0}])
    sim = build_corner_simulate(_agent(), backend, state, holder, _noop_log)

    await sim({"tb": DECK}, _spec_ge_40())
    holder.probe_frozen = False
    await sim({"tb": DECK}, _spec_ge_40())

    assert len(backend.calls) == 3                    # nominal, nominal + 탐침
    assert FS in holder.corner_set.corners


def test_a_box_is_not_frozen_by_default():
    # 기본값이 True로 뒤집히면 탐침이 프로덕션에서 통째로 사라지는데, 그것은
    # 이 저장소가 반복해서 당한 "조용히 아무것도 안 함"이다.
    assert CornerState(CornerSet(corners=(NOMINAL,), probe_order=())).probe_frozen is False
