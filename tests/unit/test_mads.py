"""MADS 전략의 **행동**을 시뮬레이터 없이 못박는다.

여기서 재는 것은 회로가 아니라 탐색기다: 반경이 언제 늘고 줄는가, 복합 이동이
정말 여러 노브를 동시에 옮기는가, 정수 노브가 어떻게 다뤄지는가, 예산이
떨어졌을 때 무엇을 하는가. 실 ngspice는 A/B 실행이 쓴다 - 그쪽은 한 판에
약 15분이라 탐색기의 분기를 하나씩 확인하는 자리가 될 수 없다.

가짜 SearchRun을 쓰는 이유도 같다. 진짜 SearchRun은 덱과 게이트와 수락 규칙을
전부 들고 오는데, 이 파일이 묻는 것은 "전략이 무엇을 내미는가"뿐이다. 전략이
수락을 정하지 못한다는 반대편 주장은 tests/unit/test_optimizer_search_seam.py가
이미 진짜 이음매로 박아 두었고, 이 파일 마지막의 통합 테스트가 MADS로도 그것이
성립함을 확인한다.
"""

import math

import pytest

from analogcoder.mads import MADS_INITIAL_POLL_SIZE, MADS_STEP_FACTOR, mads
from analogcoder.optimizer import (
    SEARCH_STRATEGIES,
    Knob,
    KnobState,
    OptimizerAgents,
    StepOutcome,
    coordinate_descent,
)


class FakeRun:
    """SearchRun이 전략에게 보여 주는 면만 흉내 낸다.

    수락되면 값이 실제로 움직인다 - 진짜 실행에서 다음 이터레이션의
    `knob_state`가 덱에서 다시 읽는 것과 같은 모양이라, 이것이 없으면 반경
    변화가 값에 어떻게 반영되는지를 잴 수 없다."""

    def __init__(self, knobs, values, integers=(), budget=40, accept=None, blocked=()):
        self.knobs = list(knobs)
        self._values = dict(values)
        self._integers = set(integers)
        self._budget = budget
        self._accept = accept if accept is not None else (lambda steps: True)
        self._blocked = set(blocked)
        self.attempts = []
        self.events = []
        self.exhaustions = []
        self.state_queries = []
        self.spent = 0

    # --- SearchRun의 면 -----------------------------------------------------

    def spend_step(self, knob) -> bool:
        if self.spent >= self._budget:
            return False
        self.spent += 1
        return True

    def knob_state(self, knob):
        key = (knob.refdes, knob.param)
        self.state_queries.append(key)
        if key in self._blocked:
            return None
        return KnobState(
            token=knob.param, value=self._values[key], integer=key in self._integers
        )

    async def attempt(self, steps) -> StepOutcome:
        candidate = {(s.knob.refdes, s.knob.param): s.value for s in steps}
        self.attempts.append(candidate)
        accepted = self._accept(steps)
        if accepted:
            self._values.update(candidate)
        return StepOutcome(accepted=accepted, reason=None if accepted else "no", objective=1.0)

    def exhausted(self, knob, state, reason) -> None:
        self.exhaustions.append((knob.refdes, knob.param, reason))

    def log_event(self, step, payload) -> None:
        self.events.append((step, payload))

    # --- 읽기 도우미 --------------------------------------------------------

    def polls(self):
        return [payload for step, payload in self.events if step == "mads_poll"]

    def summary(self):
        return [payload for step, payload in self.events if step == "mads_summary"][-1]

    def values_of(self, knob):
        return [c[(knob.refdes, knob.param)] for c in self.attempts if (knob.refdes, knob.param) in c]


W = Knob(refdes="TRIMAMP.Xt", param="W", direction="decrease")
L = Knob(refdes="TRIMAMP.X7", param="L", direction="increase")
M = Knob(refdes="AMP.M1", param="m", direction="decrease")


def _reject_all(steps):
    return False


# --- 첫 걸음은 좌표 하강의 첫 걸음과 같아야 한다 ----------------------------


@pytest.mark.asyncio
async def test_the_first_poll_point_is_exactly_coordinate_descents_first_point():
    """A/B의 두 팔이 **같은 점**에서 갈라져야 한다. 첫 점이 다르면 이긴 쪽이
    알고리즘 덕인지 출발점 덕인지 갈리지 않는다 - 이 저장소가 D1에서 겪은
    "측정이 부정이 아니라 무효였다"와 같은 부류의 실패다."""
    from analogcoder.optimizer import _next_value

    run = FakeRun([W], {("TRIMAMP.Xt", "W"): 8.0}, accept=_reject_all, budget=1)
    await mads(run)

    assert run.attempts[0] == {("TRIMAMP.Xt", "W"): 7.2}
    assert run.attempts[0][("TRIMAMP.Xt", "W")] == _next_value(8.0, False, "decrease")


# --- 적응 스텝: 확대와 축소 -------------------------------------------------


@pytest.mark.asyncio
async def test_a_successful_poll_doubles_the_radius_so_the_next_step_is_bigger():
    """성공하면 Δ ← τΔ. 로그좌표에서 반경이 2배면 비율이 제곱이 된다:
    ×0.9 다음이 ×0.81이다. 고정 비율 좌표 하강은 언제나 ×0.9다."""
    run = FakeRun([W], {("TRIMAMP.Xt", "W"): 8.0}, budget=3)
    await mads(run)

    values = run.values_of(W)
    assert values[0] == pytest.approx(7.2)
    assert values[1] == pytest.approx(7.2 * 0.81)
    assert values[2] == pytest.approx(7.2 * 0.81 * 0.81**2)
    polls = run.polls()
    assert [p["mesh"] for p in polls[:3]] == ["expand", "expand", "expand"]
    assert polls[1]["delta_before"] == pytest.approx(MADS_INITIAL_POLL_SIZE * MADS_STEP_FACTOR)


@pytest.mark.asyncio
async def test_a_completely_failed_poll_halves_the_radius_so_the_next_step_is_smaller():
    """실패하면 Δ ← Δ/τ. 좌표 하강은 여기서 **후보를 버린다** - 한 번 거절된
    방향을 더 밀지 않는 것이 그쪽의 규칙이라, 실측에서 탐색이 멈춘 자리가
    바로 그것이었다. MADS는 같은 방향을 더 짧게 다시 시도한다."""
    run = FakeRun([W], {("TRIMAMP.Xt", "W"): 8.0}, accept=_reject_all, budget=2)
    await mads(run)

    values = run.values_of(W)
    assert values[0] == pytest.approx(8.0 * 0.9)
    assert values[1] == pytest.approx(8.0 * math.exp(-MADS_INITIAL_POLL_SIZE / 2))
    assert values[1] > values[0]  # 덜 내려간다 - 반경이 줄었다는 뜻이다
    assert [p["mesh"] for p in run.polls()[:2]] == ["contract", "contract"]


@pytest.mark.asyncio
async def test_the_mesh_gets_finer_as_the_radius_shrinks():
    """δ/Δ = 1/ρ 가 함께 0으로 가는 것이 MADS를 GPS와 가르는 성질이다.
    이벤트에 ρ를 남기는 이유는 그것이 로그에서 읽혀야 하기 때문이다."""
    run = FakeRun([W], {("TRIMAMP.Xt", "W"): 8.0}, accept=_reject_all, budget=4)
    await mads(run)

    rhos = [p["rho"] for p in run.polls()[:4]]
    assert rhos == sorted(rhos)
    assert rhos[0] == 9 and rhos[-1] > rhos[0]


# --- 결합: 한 후보가 여러 노브를 동시에 옮긴다 ------------------------------


@pytest.mark.asyncio
async def test_a_poll_direction_moves_several_knobs_at_once():
    """노브 간 결합을 보는 방식은 이것 하나다. `run.attempt`가 ProposedStep의
    **목록**을 받으므로 이음매 변경이 필요 없다 - 좌표 하강이 한 번에 하나만
    쓰고 있었을 뿐이다."""
    run = FakeRun(
        [W, L],
        {("TRIMAMP.Xt", "W"): 8.0, ("TRIMAMP.X7", "L"): 1.0},
        accept=_reject_all,
        budget=10,
    )
    await mads(run)

    assert any(len(candidate) == 2 for candidate in run.attempts)
    assert max(p["composite_arity"] for p in run.polls()) == 2
    assert run.summary()["coupling_observable"] is True
    assert run.summary()["composite_evaluated"] >= 1


@pytest.mark.asyncio
async def test_one_knob_makes_coupling_structurally_unobservable_and_says_so():
    """노브가 하나면 폴 집합이 방향 하나이고 복합 이동이 **원리적으로**
    존재하지 않는다. 기존 A/B 실행이 전부 `TRIMAMP.Xt:W:decrease` 하나였으므로,
    그 기록으로 결합을 판정하면 부정이 아니라 무효다 - 그 사실을 사후에
    재구성하지 않고 요약에 적는다."""
    run = FakeRun([W], {("TRIMAMP.Xt", "W"): 8.0}, accept=_reject_all, budget=3)
    await mads(run)

    assert all(p["composite_arity"] == 1 for p in run.polls() if p["evaluated"])
    assert run.summary()["coupling_observable"] is False
    assert run.summary()["widest_poll_knobs"] == 1
    # 그리고 한 방향뿐이므로 실패하는 폴이 시뮬레이션 하나만 쓴다.
    assert run.polls()[0]["poll_size"] == 1


@pytest.mark.asyncio
async def test_the_poll_always_contains_the_feasible_coordinate_directions():
    """복합 방향은 원뿔에 스냅되면서 경계 방향을 잃을 수 있다. 그래서 실행가능
    좌표 방향이 **언제나** 폴에 들어간다 - 그것이 없으면 "완결된 폴이
    실패했으니 줄인다"가 보지 않은 방향을 실패로 단정하는 추론이 된다."""
    run = FakeRun(
        [W, L],
        {("TRIMAMP.Xt", "W"): 8.0, ("TRIMAMP.X7", "L"): 1.0},
        accept=_reject_all,
        budget=10,
    )
    await mads(run)

    first_poll = [c for c in run.attempts[: run.polls()[0]["evaluated"]]]
    single = [c for c in first_poll if len(c) == 1]
    assert {tuple(c)[0] for c in single} == {("TRIMAMP.Xt", "W"), ("TRIMAMP.X7", "L")}
    # 선언된 방향 밖으로는 나가지 않는다 - 좌표 하강과 **같은 상자**를 받는다.
    for candidate in run.attempts:
        if ("TRIMAMP.Xt", "W") in candidate:
            assert candidate[("TRIMAMP.Xt", "W")] < 8.0
        if ("TRIMAMP.X7", "L") in candidate:
            assert candidate[("TRIMAMP.X7", "L")] > 1.0


# --- 정수 노브 --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_count_knob_moves_by_whole_numbers_and_starts_at_plus_or_minus_one():
    """개수 노브(m/nf에 도달하는 것)는 값 자체가 좌표이고 granularity가 1이다.
    Δ0에서 ±1 - 좌표 하강과 같은 첫 걸음이고, 확대하면 ±2가 된다.

    정수 여부는 `KnobState.integer`에서 온다. 전략은 **이름으로 판단하지
    않는다** - `is_count_param`이 래퍼 셀의 `mult=4`가 본문 `m=`에 도달하는
    경우까지 이미 처리하고 있고, 갈라지면 후보가 첫 단계에서 죽는다."""
    run = FakeRun([M], {("AMP.M1", "m"): 20.0}, integers=[("AMP.M1", "m")], budget=3)
    await mads(run)

    assert run.values_of(M) == [19.0, 17.0, 13.0]


@pytest.mark.asyncio
async def test_a_count_knob_floors_at_one_mesh_unit_and_the_log_says_so():
    """반경을 줄여도 정수는 1보다 잘게 나눌 수 없다. 그것은 추측한 허용오차가
    아니라 격자의 구조적 사실이고, 바닥에 눌린 걸음이 로그에 없으면 "축소했는데
    아무 일도 없었다"의 원인을 읽을 수 없다."""
    run = FakeRun(
        [M], {("AMP.M1", "m"): 20.0}, integers=[("AMP.M1", "m")], accept=_reject_all, budget=4
    )
    await mads(run)

    # 첫 걸음 19가 거절된 뒤, 축소해도 다시 19가 나온다 - 그 점은 결과를 이미
    # 알고 있으므로 **재지 않는다**. 그래서 시뮬레이션은 한 번만 쓰인다.
    assert run.values_of(M) == [19.0]
    assert run.polls()[1]["repeated_directions"] >= 1
    assert run.polls()[1]["evaluated"] == 0
    assert run.summary()["stopped"] == "all_knobs_exhausted"
    assert run.exhaustions and "already rejected" in run.exhaustions[0][2]


@pytest.mark.asyncio
async def test_a_count_knob_never_goes_below_one():
    """1 미만은 소자가 아니다 - `_next_value`가 개수에 대해 이미 두고 있는
    경계와 같다. 여기가 무너지면 `m=0`이 덱에 들어간다."""
    run = FakeRun([M], {("AMP.M1", "m"): 1.0}, integers=[("AMP.M1", "m")], budget=5)
    await mads(run)

    assert run.attempts == []
    assert run.summary()["stopped"] == "all_knobs_exhausted"
    assert run.exhaustions[0][:2] == ("AMP.M1", "m")


# --- 예산 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_incomplete_poll_never_contracts_and_stops_the_whole_search():
    """예산이 폴 중간에 떨어지면 반경을 **건드리지 않는다.** 축소하면 재지
    않은 방향을 실패로 단정하는 것이고, 그것이 MADS 수렴 논증의 전제를 깬다.

    그리고 통째로 멈춘다 - 예산은 전역이라 다음 방향으로 넘어가도 달라지는
    것이 없다(`spend_step`의 계약)."""
    run = FakeRun(
        [W, L],
        {("TRIMAMP.Xt", "W"): 8.0, ("TRIMAMP.X7", "L"): 1.0},
        accept=_reject_all,
        budget=2,
    )
    await mads(run)

    last = run.polls()[-1]
    assert last["poll_complete"] is False
    assert last["mesh"] == "hold"
    assert last["delta_after"] == last["delta_before"]
    assert run.spent == 2
    assert run.summary()["stopped"] == "budget"
    assert run.summary()["contracts"] == 0


@pytest.mark.asyncio
async def test_the_strategy_spends_exactly_one_step_per_attempt():
    """공정한 A/B가 여기에 걸려 있다. `attempt`는 예산을 확인하지 않으므로,
    전략이 attempt당 정확히 1회 `spend_step`을 부르지 않으면 두 팔이 다른
    예산에서 돈다."""
    run = FakeRun([W], {("TRIMAMP.Xt", "W"): 8.0}, accept=_reject_all, budget=5)
    await mads(run)

    assert run.spent == len(run.attempts) == 5


# --- 막힌 노브 --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_blocked_knob_is_dropped_permanently_and_asked_only_once():
    """게이트가 막는 것은 값이 아니라 **주소**다(SearchRun.knob_state의
    독스트링). 다른 값으로 다시 물어도 같은 자리에서 막히고, 물을 때마다
    거절이 1건씩 세어져 이력이 같은 사실을 반복해서 적는다."""
    run = FakeRun(
        [W, L],
        {("TRIMAMP.Xt", "W"): 8.0, ("TRIMAMP.X7", "L"): 1.0},
        accept=_reject_all,
        budget=6,
        blocked=[("TRIMAMP.X7", "L")],
    )
    await mads(run)

    assert run.state_queries.count(("TRIMAMP.X7", "L")) == 1
    assert all(("TRIMAMP.X7", "L") not in candidate for candidate in run.attempts)
    assert run.polls()[0]["n"] == 1
    assert run.summary()["coupling_observable"] is False


@pytest.mark.asyncio
async def test_every_knob_blocked_stops_the_search_and_says_which_way():
    """노브가 전부 막히면 잴 것이 없다. 그 사태가 "예산을 다 썼다"와 같은
    모양으로 보이면 안 된다."""
    run = FakeRun(
        [W], {("TRIMAMP.Xt", "W"): 8.0}, budget=6, blocked=[("TRIMAMP.Xt", "W")]
    )
    await mads(run)

    assert run.attempts == []
    assert run.summary()["stopped"] == "no_live_knobs"
    assert run.summary()["polls"] == 1


# --- 아무것도 안 할 때의 모양 -----------------------------------------------


@pytest.mark.asyncio
async def test_a_run_where_nothing_is_ever_accepted_reports_that_the_adaptive_step_never_fired():
    """「이것이 아무것도 안 할 때 로그가 어떻게 보이는가」 - 이 저장소가 열 번
    값을 치른 질문이다.

    전부 거절되면 축소만 있고 확대가 0이다. 그 실행은 "MADS가 졌다"가 아니라
    **"적응 스텝이 한쪽으로만 발화했다"**이고, 판정에 넣으면 D1이 반복된다.
    자격 조건을 사후에 history.jsonl에서 재구성하지 않고 여기서 적는다."""
    run = FakeRun([W], {("TRIMAMP.Xt", "W"): 8.0}, accept=_reject_all, budget=4)
    await mads(run)

    summary = run.summary()
    assert summary["expands"] == 0
    assert summary["contracts"] == 4
    assert summary["adaptive_step_exercised"] is False
    assert summary["delta_final"] < summary["delta_initial"]


@pytest.mark.asyncio
async def test_a_run_where_everything_is_accepted_also_fails_the_qualification():
    """반대쪽 무력화. 매 폴 첫 방향에서 성공하면 확대만 있고 축소가 0이다 -
    한쪽만 발화한 것이므로 역시 판정 자격이 없다."""
    run = FakeRun([W], {("TRIMAMP.Xt", "W"): 8.0}, budget=4)
    await mads(run)

    summary = run.summary()
    assert summary["contracts"] == 0
    assert summary["adaptive_step_exercised"] is False


@pytest.mark.asyncio
async def test_a_run_that_both_expands_and_contracts_qualifies():
    """양쪽이 다 발화한 실행만 판정에 쓴다."""
    seen = {"n": 0}

    def accept(steps):
        seen["n"] += 1
        return seen["n"] == 1  # 첫 후보만 수락 - 확대 1회, 이후 축소

    run = FakeRun([W], {("TRIMAMP.Xt", "W"): 8.0}, accept=accept, budget=4)
    await mads(run)

    summary = run.summary()
    assert summary["expands"] >= 1 and summary["contracts"] >= 1
    assert summary["adaptive_step_exercised"] is True


@pytest.mark.asyncio
async def test_coordinate_descent_leaves_no_mads_events_at_all():
    """좌표 하강이 아무것도 못 한 실행과 MADS가 아무것도 못 한 실행은
    **이벤트 이름으로** 갈린다. 이것이 없으면 두 무력화가 로그에서 같은
    모양이 된다."""
    run = FakeRun([W], {("TRIMAMP.Xt", "W"): 8.0}, accept=_reject_all, budget=4)
    await coordinate_descent(run)

    assert run.events == []


# --- 등록 -------------------------------------------------------------------


def test_the_strategy_is_registered_under_a_name_the_harness_can_select():
    """scripts/search_ab.py는 이 표만 읽는다. 여기 없으면 A/B에서 전략 이름이
    조용히 사라지고, 하니스가 `unknown strategy`로 거부한다."""
    assert SEARCH_STRATEGIES["mads"] is mads
    # 기준선은 그대로다 - 바꾸면 A/B가 무의미해진다.
    assert SEARCH_STRATEGIES["coordinate_descent"] is coordinate_descent


def test_importing_either_module_first_still_registers_the_strategy():
    """`mads`가 optimizer의 타입을 쓰고 optimizer가 등록을 위해 mads를
    부르므로 순환이 있다. 어느 쪽을 먼저 import해도 표가 채워져야 한다 -
    안 그러면 import 순서에 따라 전략이 있다 없다 한다."""
    import subprocess
    import sys

    for first in ("analogcoder.optimizer", "analogcoder.mads"):
        proc = subprocess.run(
            [sys.executable, "-c",
             f"import {first}; from analogcoder.optimizer import SEARCH_STRATEGIES;"
             f" print(sorted(SEARCH_STRATEGIES))"],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert "'mads'" in proc.stdout


# --- 진짜 이음매를 통과하는가 ----------------------------------------------


@pytest.mark.asyncio
async def test_mads_composes_with_the_real_accept_rule_and_gates(tmp_path):
    """가짜 SearchRun이 아니라 진짜 실행으로 한 번 통과시킨다. 여기서 확인하는
    것은 알고리즘이 아니라 **계약**이다: 전략이 수락을 정하지 못하고, 게이트가
    그대로 돌며, 결과의 집계가 실제로 돌려주는 넷리스트를 설명한다."""
    from tests.unit.test_optimizer_search_seam import (  # noqa: PLC0415
        DECK,
        KNOB,
        _spec,
        _state,
        _simulate,
    )
    from analogcoder.optimizer import run_optimization

    async def forbidden(*args):
        raise AssertionError("no agent call expected with an injected ranking")

    # 기준선 235 -> 후보마다 200: m 4 -> 3은 면적이 줄고 기준도 통과한다.
    simulate, _ = _simulate([235.0, 200.0, 200.0, 200.0, 200.0])
    agents = OptimizerAgents(
        propose=forbidden,
        simulate=simulate,
        knob_ranking=[KNOB],
        search_strategy=SEARCH_STRATEGIES["mads"],
    )
    state = _state(tmp_path)

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["status"] == "OPTIMIZED"
    assert result["steps_accepted"] >= 1
    # m은 개수 노브다 - 정수로 쓰여야 한다(m=3.0은 정수성 검사가 거부한다).
    assert "m=3" in state.current_netlist_texts()["tb"]
    # 그리고 자기 폴 기록을 남겼다.
    import json

    with open(state.history_path) as f:
        steps = [json.loads(line)["step"] for line in f]
    assert "mads_poll" in steps and "mads_summary" in steps
