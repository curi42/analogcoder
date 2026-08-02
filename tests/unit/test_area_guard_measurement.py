"""`scripts/area_guard_measurement.py`의 `_safe_state` - Task 4 리뷰(I-2)가
지적한 빈 자리.

B-1이 도입한 `safe` 축(True/False/void)과 그 리뷰가 잡은 unjudged 네 번째
값은 이 그리드(14조합)에서 `void` 하나만 실제로 발화했다 - `unjudged`와
P1의 "가드가 엄격해 아무것도 안 함"(void 아님) 경로는 이 스크립트에
`ngspice`를 다시 돌리지 않고는 확인할 방법이 없었다. 이 파일이 그 확인을
대신한다: "이 게이트가 아무것도 안 할 때 로그가 어떻게 보이는가"라는 이
저장소의 표준 질문에, 시뮬레이션 없이 답한다."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from area_guard_measurement import _safe_state  # noqa: E402


def test_baseline_already_failing_is_void_not_false():
    """P2의 실제 7개 조합이 이 모양이었다: 수락 0, 착지 덱(=기준선)의
    tightest_slack이 음수(이미 실패 중인 기준이 있다) - overall_pass를
    그대로 옮기면 "하한이 측정됐고 안전하지 않았다"는 거짓 주장이 된다."""
    safe, reason = _safe_state(
        accepted=0,
        tightest_slack={"criterion": "phase_margin", "value": -0.42394},
        overall_pass=False,
    )
    assert safe == "void"
    assert reason is not None
    assert "phase_margin" in reason
    assert "-0.42394" in reason


def test_a_guard_too_strict_to_take_any_step_is_not_void():
    """P1의 F1 0.05/0.10/0.20이 이 모양이었다: 수락 0이지만 착지 덱은
    기준선에서부터 통과 중(tightest_slack이 양수)이다 - "가드가 너무
    엄격해 아무것도 안 했다"는 그 자체로 하한에 대한 실측이지, 잴 수
    없었던 것이 아니다. 이 구별이 docstring이 말하는 요지다."""
    safe, reason = _safe_state(
        accepted=0,
        tightest_slack={"criterion": "vbgout_min", "value": 0.0314},
        overall_pass=True,
    )
    assert safe is True
    assert reason is None


def test_a_deck_that_broke_a_criterion_after_steps_is_a_real_false():
    """P1의 F1 0.02/F2 세 값이 이 모양이었다: 수락 스텝이 있고(탐색이
    실제로 움직였다), 코너 스윕이 진짜로 실패했다 - 이것은 하한에 대한
    진짜 부정 결과이지 void도 unjudged도 아니다."""
    safe, reason = _safe_state(
        accepted=16,
        tightest_slack={"criterion": "buf0_phase_margin", "value": 0.0231},
        overall_pass=False,
    )
    assert safe is False
    assert reason is None


def test_no_tightest_slack_at_all_is_unjudged_not_false():
    """이 그리드에서는 발화하지 않았지만(REFUSED/준비 구간 예외/진입 스윕
    실패 셋 다 tightest_slack=None을 낸다), 발화하면 이전 코드는 void와
    같은 모양으로 False에 접었을 것이다 - void는 "쟀고 이미 깨져 있었다"는
    사실이 있고, unjudged는 그 사실 자체가 없다."""
    safe, reason = _safe_state(accepted=0, tightest_slack=None, overall_pass=False)
    assert safe == "unjudged"
    assert reason is not None
    assert "tightest_slack" in reason


def test_tightest_slack_present_but_value_missing_is_also_unjudged():
    """`tightest_slack`이 dict로 오더라도 그 안의 `value`가 없으면 판단할
    수 없기는 마찬가지다 - `dict | None`의 두 "없음" 모양을 하나로
    다룬다."""
    safe, reason = _safe_state(
        accepted=0, tightest_slack={"criterion": "x", "value": None}, overall_pass=True
    )
    assert safe == "unjudged"
    assert reason is not None


def test_verify_instrument_refuses_a_baseline_missing_a_criterion_s_measurement(tmp_path):
    """아래 `_safe_state` 사각이 **오늘 발화하지 않는 유일한 이유**를 못박는다.

    사각 자체는 아래 테스트가 재현한다: 기준 하나의 측정값이 안 나오면
    `_tightest_slack`이 그 기준을 빼고 다른 기준의 양수 여유를 돌려주므로
    `_safe_state`가 `void` 대신 `False`를 낸다. 그 조합이 실제 그리드에서
    나오지 않는 것은 `verify_instrument`가 **재기 전에** 기준선 시뮬레이션을
    돌려보고 요구된 측정값이 하나라도 없으면 거절하기 때문이다.

    아래 테스트는 이름으로 그 의존을 주장했지만 `verify_instrument`를 한 번도
    부르지 않았다 - 사전 거절을 느슨하게 해도 초록이었다. 그 절반이 이
    테스트다. 여기가 깨지면 아래 사각이 살아난다."""
    import asyncio
    from types import SimpleNamespace

    from area_guard_measurement import verify_instrument

    from analogcoder.spec import Criterion

    netlist = tmp_path / "tb.cir"
    netlist.write_text("* tb\n.end\n", encoding="utf-8")
    tb = SimpleNamespace(name="tb", netlist_path=str(netlist), control_block=".control\n.endc\n")
    spec = SimpleNamespace(
        testbenches=[tb],
        all_criteria=[
            Criterion(name="iq", measurement="iq_ua", operator="<=", threshold=300.0),
            Criterion(name="gain", measurement="gain_db", operator=">=", threshold=60.0),
        ],
    )

    class _Backend:
        def __init__(self, measurements):
            self._measurements = measurements

        def run(self, _path, _options):
            return SimpleNamespace(measurements=dict(self._measurements))

    # gain_db 가 안 나온다 - 정확히 아래 사각을 만드는 기준선이다.
    refused = asyncio.run(
        verify_instrument("p", spec, _Backend({"iq_ua": 200.0}), tmp_path)
    )
    assert refused["ok"] is False
    assert refused["missing"] == ["gain_db"]
    assert refused["wanted"] == 2

    # 대조군: 둘 다 나오면 통과한다(거절이 무조건 참인 게이트가 아니다).
    accepted = asyncio.run(
        verify_instrument("p", spec, _Backend({"iq_ua": 200.0, "gain_db": 70.0}), tmp_path)
    )
    assert accepted["ok"] is True
    assert accepted["missing"] == []


def test_safe_state_is_blind_to_a_nan_criterion_and_depends_on_verify_instrument():
    """**void 판정의 알려진 사각을 못박는다.**

    `optimizer._tightest_slack`은 `actual`이 NaN인 기준을 최솟값 경쟁에서
    뺀다(그 함수의 docstring이 이유를 적는다). 그래서 어떤 기준의 측정이
    아예 안 나온 기준선은 `overall_pass=False`로 0스텝을 수락하면서도
    `tightest_slack`은 **다른 기준의 양수 값**을 들고 온다 - 그 조합에서
    `_safe_state`는 `value < 0.0`을 못 보고 `False`를 돌려준다. 즉 "쟀고
    안전하지 않았다"는 거짓 주장이 void가 막으려던 문이 아니라 **다른
    문으로** 나온다.

    이 테스트는 그 결함을 고치는 것이 아니라 **사각 자체**를 고정한다: 오늘
    발화하지 않는 유일한 이유는 `verify_instrument`가 조합을 돌리기 전에
    두 스펙의 기준이 전부 측정되는지 확인하고 거절하기 때문이다. 그
    사전 거절을 느슨하게 하는 사람이 여기도 같이 고쳐야 한다는 것을 알게
    하는 것이 목적이다.

    **그 의존 쪽 절반은 바로 위
    `test_verify_instrument_refuses_a_baseline_missing_a_criterion_s_measurement`
    가 맡는다** - 이 테스트는 `verify_instrument`를 부르지 않으므로 사전
    거절을 느슨하게 해도 초록이다. 이름이 약속하는 링크를 실제로 거는 것은
    저쪽이고, 둘을 짝으로 읽어야 한다.

    실제 수치로 재현한다: 기준 둘 중 `gain`이 NaN(측정 실패, `pass=False`)
    이고 `iq`는 200/300으로 여유 +0.333. `_tightest_slack`은 `iq`만 보고
    +0.333을 돌려주므로, 기준선이 실제로는 깨져 있는데도 `< 0.0`이
    거짓이다."""
    import math

    from analogcoder.optimizer import _tightest_slack
    from analogcoder.spec import Criterion

    criteria = [
        Criterion(name="iq", measurement="iq_ua", operator="<=", threshold=300.0),
        Criterion(name="gain", measurement="gain_db", operator=">=", threshold=60.0),
    ]
    # evaluate_criteria가 측정 실패에 쓰는 모양 그대로: actual=NaN, pass=False.
    baseline_verdict = [
        {"name": "iq", "actual": 200.0, "pass": True},
        {"name": "gain", "actual": math.nan, "pass": False},
    ]

    tightest = _tightest_slack(criteria, baseline_verdict)
    # NaN 기준이 경쟁에서 빠지므로 최솟값은 통과 중인 기준의 **양수**다.
    assert tightest == {"criterion": "iq", "value": 100.0 / 300.0}

    safe, reason = _safe_state(accepted=0, tightest_slack=tightest, overall_pass=False)

    # 옳은 답은 "void"다(기준선이 이미 깨져 있었다). 실제로는 False가 나온다.
    assert safe is False
    assert reason is None
