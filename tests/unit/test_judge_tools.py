import pytest

from analogcoder.judge_tools import baseline_ratio_allowances, evaluate_criteria, relative_slack
from analogcoder.spec import Criterion


def test_evaluate_criteria_all_pass():
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=19.5, unit="dB")]
    result = evaluate_criteria({"gain_db": 20.0}, criteria)
    assert result["overall_pass"] is True
    assert result["criteria"][0]["pass"] is True
    assert result["criteria"][0]["margin"] == 0.5


def test_evaluate_criteria_one_fails():
    criteria = [
        Criterion(name="gain", measurement="gain_db", operator=">=", threshold=19.5, unit="dB"),
        Criterion(name="power", measurement="power_mw", operator="<=", threshold=5.0, unit="mW"),
    ]
    result = evaluate_criteria({"gain_db": 18.0, "power_mw": 4.0}, criteria)
    assert result["overall_pass"] is False
    gain_result = next(c for c in result["criteria"] if c["name"] == "gain")
    assert gain_result["pass"] is False
    assert gain_result["margin"] == -1.5


def test_evaluate_criteria_missing_measurement_fails():
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=19.5, unit="dB")]
    result = evaluate_criteria({}, criteria)
    assert result["overall_pass"] is False
    assert result["criteria"][0]["pass"] is False


def test_f2_allowance_is_a_ratio_of_the_baseline_distance_to_the_threshold():
    """여유분은 절대량이므로 기준선의 임계값까지 거리에 r 을 곱한 값이다."""
    criteria = [
        Criterion(name="gain", measurement="gain_db", operator=">=", threshold=60.0),
        Criterion(name="iq", measurement="iq_ua", operator="<=", threshold=300.0),
    ]
    allowances, excluded = baseline_ratio_allowances(
        {"gain_db": 80.0, "iq_ua": 200.0}, criteria, 0.5
    )
    assert allowances == {"gain": 10.0, "iq": 50.0}
    assert excluded == []


def test_f2_excludes_a_criterion_that_is_already_failing():
    """음수 여유에 r 을 곱하면 하한이 **위로** 올라가 규칙이 뒤집힌다.

    psrr_dc <= -25 에 비율을 곱했다가 <= -20 이 되어 더 느슨해졌던 사고와 같은
    모양이고, pvt.py 는 그것으로 두 번 대가를 치렀다. 제외하고 이름을 남긴다 -
    그 기준은 overall_pass 가 이미 판정한다."""
    criteria = [
        Criterion(name="psrr", measurement="psrr_db", operator="<=", threshold=-25.0),
    ]
    allowances, excluded = baseline_ratio_allowances({"psrr_db": -20.0}, criteria, 0.5)
    assert allowances == {}
    assert excluded == ["psrr"]


def test_f2_excludes_a_criterion_sitting_exactly_on_its_threshold():
    """여유 0 에는 어떤 r 을 곱해도 0 이다 - 침묵을 규칙인 척하지 않는다."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=60.0)]
    allowances, excluded = baseline_ratio_allowances({"gain_db": 60.0}, criteria, 0.5)
    assert allowances == {}
    assert excluded == ["gain"]


def test_f2_excludes_a_criterion_with_no_measurement():
    """측정값이 없으면 거리를 잴 수 없다. 0 으로 읽지 않는다."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=60.0)]
    allowances, excluded = baseline_ratio_allowances({}, criteria, 0.5)
    assert allowances == {}
    assert excluded == ["gain"]


def test_f2_allowance_is_positive_regardless_of_threshold_sign():
    """임계값이 음수여도 여유분은 양수 절대량이다 - guard_band_violations 가
    부호 문제를 만나지 않게 하는 것이 ratio_allowances 와 같은 이유다."""
    criteria = [Criterion(name="psrr", measurement="psrr_db", operator="<=", threshold=-25.0)]
    allowances, excluded = baseline_ratio_allowances({"psrr_db": -35.0}, criteria, 0.5)
    assert allowances == {"psrr": 5.0}
    assert excluded == []


# --- relative_slack: scripts/area_guard_measurement.py 에서 옮겨와 공유 ------


def test_relative_slack_is_positive_when_passing_and_scaled_by_the_larger_magnitude():
    """`<=` 기준에서 통과 중이면 부호가 양수다. 스케일은
    max(|threshold|, |actual|) - 여기서는 threshold(10)가 더 크다."""
    criterion = Criterion(name="x", measurement="m", operator="<=", threshold=10.0)
    assert relative_slack(criterion, 8.0) == pytest.approx((10.0 - 8.0) / 10.0)


def test_relative_slack_is_negative_when_failing():
    """실패 중이면 부호가 음수다 - "얼마나 초과했는가"를 같은 스케일로 잰다."""
    criterion = Criterion(name="x", measurement="m", operator=">=", threshold=10.0)
    assert relative_slack(criterion, 8.0) == pytest.approx((8.0 - 10.0) / 10.0)


def test_relative_slack_is_none_without_a_measurement():
    """actual이 None이면 거리를 잴 수 없다 - 0으로 읽지 않는다."""
    criterion = Criterion(name="x", measurement="m", operator="<=", threshold=10.0)
    assert relative_slack(criterion, None) is None


def test_relative_slack_at_a_zero_threshold_and_actual_is_an_exact_tie_not_a_fallback():
    """`scale == 0.0`은 나눗셈을 피하려는 임의 대체값이 아니다 - 임계값과
    실측이 둘 다 0일 때만 나오는 값이고, 그때 여유는 정확히 0이다(기준이
    자기 임계값 위에 정확히 서 있다)."""
    criterion = Criterion(name="x", measurement="m", operator=">=", threshold=0.0)
    assert relative_slack(criterion, 0.0) == 0.0


# --- 개선량 (정규화 위반량 합) ------------------------------------------------
# `relative_slack`과 스케일이 다르다. 저쪽은 `max(|threshold|, |actual|)`로
# **점마다** 계산하고, 여기는 적용 전과 후가 **같은 자**를 써야 하므로
# `max(|threshold|, |before|, |after|)`다. 두 점을 다른 자로 재면 개선량이
# 뜻을 잃는다.

def _crit(op=">=", threshold=60.0, name="gain", meas="g"):
    from analogcoder.spec import Criterion
    return Criterion(name=name, measurement=meas, operator=op, threshold=threshold, unit="dB")


def test_violation_sum_uses_one_scale_for_before_and_after():
    from analogcoder.judge_tools import violation_sum

    # scale = max(60, 40, 50) = 60.  before=(60-40)/60, after=(60-50)/60
    r = violation_sum([_crit()], {"g": 40.0}, {"g": 50.0})
    assert r.total_before == pytest.approx(20.0 / 60.0)
    assert r.total_after == pytest.approx(10.0 / 60.0)
    assert r.improvement == pytest.approx(10.0 / 60.0)
    assert r.zero_scale_count == 0 and r.unmeasured_count == 0


def test_a_passing_criterion_contributes_zero_violation_not_a_negative_one():
    """통과한 기준의 여유는 개선량이 아니다. `max(0, ...)`가 그것을 자른다 -
    자르지 않으면 이미 통과한 기준을 더 통과시키는 변경이, 실패한 기준을
    실제로 고치는 변경을 이긴다."""
    from analogcoder.judge_tools import violation_sum

    r = violation_sum([_crit()], {"g": 70.0}, {"g": 90.0})
    assert r.total_before == 0.0 and r.total_after == 0.0 and r.improvement == 0.0


def test_an_upper_bound_criterion_violates_in_the_other_direction():
    from analogcoder.judge_tools import violation_sum

    c = _crit(op="<=", threshold=-25.0, name="psrr", meas="p")
    # scale = max(|-25|, |-20|, |-24|) = 25.
    # slack = threshold - actual:  전 -25-(-20) = -5,  후 -25-(-24) = -1.
    # 위반은 max(0, -slack/scale) 이므로 5/25 -> 1/25.
    r = violation_sum([c], {"p": -20.0}, {"p": -24.0})
    assert r.total_before == pytest.approx(5.0 / 25.0)
    assert r.total_after == pytest.approx(1.0 / 25.0)


def test_a_zero_scale_criterion_contributes_zero_and_is_counted():
    """근거 없는 상수로 나누지 않는다. 대신 몇 건인지 센다 - '기여 0'과
    '스케일이 없어 잴 수 없었다'가 구별되어야 한다."""
    from analogcoder.judge_tools import violation_sum

    r = violation_sum([_crit(threshold=0.0, meas="z")], {"z": 0.0}, {"z": 0.0})
    assert r.total_before == 0.0 and r.zero_scale_count == 1


def test_a_missing_or_nan_measurement_is_counted_not_read_as_zero():
    """측정이 없는 것은 위반 0이 아니다. LLM judge가 없는 측정을 `actual=0`으로
    쓴 것이 그 에이전트를 제거한 이유이고, 같은 실수를 여기서 반복하지 않는다."""
    import math

    from analogcoder.judge_tools import violation_sum

    r = violation_sum([_crit()], {}, {"g": 50.0})
    assert r.unmeasured_count == 1 and r.total_before == 0.0 and r.total_after == 0.0

    r2 = violation_sum([_crit()], {"g": math.nan}, {"g": 50.0})
    assert r2.unmeasured_count == 1

    r3 = violation_sum([_crit()], {"g": 40.0}, {"g": math.nan})
    assert r3.unmeasured_count == 1


def test_improvement_is_negative_when_the_change_made_things_worse():
    """부호가 살아 있어야 '가장 개선한 대안'이 뜻을 갖는다."""
    from analogcoder.judge_tools import violation_sum

    r = violation_sum([_crit()], {"g": 50.0}, {"g": 40.0})
    assert r.improvement < 0.0


def test_normalized_violation_returns_zero_on_a_zero_scale_rather_than_dividing():
    from analogcoder.judge_tools import normalized_violation

    assert normalized_violation(_crit(threshold=0.0), 0.0, 0.0) == 0.0
