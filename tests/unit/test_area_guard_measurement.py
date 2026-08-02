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
