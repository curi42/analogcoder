import pytest

from analogcoder.attempt_log import (
    ATTEMPT_RENDER_LIMIT,
    Attempt,
    deltas_between,
    regressed_between,
    render_attempts,
)


def judge(*criteria):
    return {
        "overall_pass": all(c["pass"] for c in criteria),
        "criteria": list(criteria),
        "summary": "",
    }


def crit(name, actual, passing):
    return {"name": name, "target": ">=0", "actual": actual, "pass": passing, "margin": 0.0}


def applied(refdes="TRIMAMP.XRz", param="l", outcome="kept", **kw):
    base = dict(
        outer_iter=1, retry=1, refdes=refdes, param=param,
        old_value="15", new_value="45", outcome=outcome,
    )
    base.update(kw)
    return Attempt(**base)


def test_deltas_cover_only_criteria_present_in_both_judgements():
    """어느 변형을 잡는가: 한쪽에만 있는 기준을 0.0으로 채워 넣는 구현.
    없는 측정을 0으로 읽는 것은 corner_allowances에서 이미 값을 치른 모양이다."""
    before = judge(crit("pm", 60.0, True), crit("gone", 1.0, True))
    after = judge(crit("pm", 78.4, True), crit("fresh", 2.0, True))

    assert deltas_between(before, after) == (("pm", pytest.approx(18.4)),)


def test_regression_is_pass_to_fail_only():
    """어느 변형을 잡는가: 'after에서 실패한 것 전부'로 계산하는 구현.
    fail -> fail 은 이미 실패하고 있던 것이지 이 시도가 망친 것이 아니다."""
    before = judge(crit("pm", 60.0, True), crit("ugbw", 1.0, False))
    after = judge(crit("pm", 40.0, False), crit("ugbw", 1.1, False))

    assert regressed_between(before, after) == ("pm",)


def test_an_empty_history_renders_to_nothing_rather_than_an_empty_table():
    """어느 변형을 잡는가: 항목이 없어도 머리글을 그리는 구현.
    빈 표는 튜너에게 '시도가 없었다'가 아니라 '무언가 있었다'로 읽힌다."""
    assert render_attempts([]) == ""


def test_an_applied_attempt_renders_its_measured_deltas():
    text = render_attempts([applied(deltas=(("pm", 18.4), ("ugbw", -1.2e6)))])

    assert "TRIMAMP.XRz l" in text
    assert "15 -> 45" in text
    assert "kept" in text
    assert "pm +18.4" in text
    assert "ugbw -1.2e+06" in text


def test_a_rolled_back_attempt_renders_the_criteria_it_regressed():
    text = render_attempts([applied(outcome="rolled_back", regressed=("pm",))])

    assert "rolled_back" in text
    assert "regressed [pm]" in text


def test_a_rejected_attempt_renders_its_gate_reason_and_detail():
    """어느 변형을 잡는가: 사유 코드를 버리고 detail만 쓰는 구현.
    '6.00x exceeds the limit'만으로는 어느 게이트였는지 복원되지 않는다."""
    text = render_attempts([
        applied(outcome="rejected", reason="area", detail="6.00x exceeds the 3.0x limit")
    ])

    assert "rejected" in text
    assert "area:" in text
    assert "6.00x exceeds the 3.0x limit" in text


def test_the_renderer_keeps_the_most_recent_attempts_and_says_how_many_it_dropped():
    """어느 변형을 잡는가: 앞에서부터 자르는 구현(--max-knobs가 알파벳순으로
    결정적 노브를 잘라 낸 것과 같은 모양), 그리고 조용히 자르는 구현."""
    attempts = [applied(refdes=f"R{i}") for i in range(ATTEMPT_RENDER_LIMIT + 5)]

    text = render_attempts(attempts)

    assert "R0 " not in text
    assert f"R{ATTEMPT_RENDER_LIMIT + 4} " in text
    assert "5" in text and "omitted" in text
