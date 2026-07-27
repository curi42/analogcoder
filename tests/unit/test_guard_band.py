import pytest

from analogcoder.judge_tools import guard_band_violations
from analogcoder.spec import Criterion


def _c(name, measurement, operator, threshold):
    return Criterion(name=name, measurement=measurement, operator=operator, threshold=threshold)


def test_a_comfortable_measurement_does_not_violate():
    crit = [_c("iq", "iq_ua", "<=", 300.0)]

    assert guard_band_violations({"iq_ua": 200.0}, crit, {"iq": 60.0}) == []


def test_a_measurement_inside_the_allowance_violates_even_though_it_passes():
    # 250은 기준을 통과하지만 240이라는 여유선 안에 있다.
    crit = [_c("iq", "iq_ua", "<=", 300.0)]

    violations = guard_band_violations({"iq_ua": 250.0}, crit, {"iq": 60.0})

    assert len(violations) == 1 and "iq" in violations[0]


def test_an_allowance_tightens_a_negative_threshold_instead_of_loosening_it():
    # psr <= -10, 여유분 2 이면 허용선은 -12 이다. 비율을 곱하는 형태였다면
    # -8 이 되어 원래보다 느슨해졌을 것이다.
    crit = [_c("psr", "psr_db", "<=", -10.0)]

    assert guard_band_violations({"psr_db": -11.0}, crit, {"psr": 2.0}) != []
    assert guard_band_violations({"psr_db": -13.0}, crit, {"psr": 2.0}) == []


def test_a_lower_bound_tightens_upward():
    crit = [_c("gain", "gain_db", ">=", 20.0)]

    assert guard_band_violations({"gain_db": 22.0}, crit, {"gain": 4.0}) != []
    assert guard_band_violations({"gain_db": 25.0}, crit, {"gain": 4.0}) == []


def test_both_sides_of_a_two_sided_window_are_judged_separately():
    # 같은 measurement에 걸린 두 기준을 하나로 뭉개면 한쪽이 사라진다 -
    # pvt.py에서 이 모양의 결함이 두 번 있었다.
    crit = [
        _c("vbg_min", "vbg", ">=", 1.20),
        _c("vbg_max", "vbg", "<=", 1.28),
    ]

    violations = guard_band_violations(
        {"vbg": 1.21}, crit, {"vbg_min": 0.02, "vbg_max": 0.02}
    )

    assert len(violations) == 1 and "vbg_min" in violations[0]


def test_a_missing_measurement_is_a_violation_not_a_pass():
    crit = [_c("iq", "iq_ua", "<=", 300.0)]

    assert guard_band_violations({}, crit, {"iq": 60.0}) != []


def test_a_criterion_without_an_allowance_only_has_to_pass():
    crit = [_c("iq", "iq_ua", "<=", 300.0)]

    assert guard_band_violations({"iq_ua": 299.0}, crit, {}) == []


def test_corner_allowances_are_the_measured_spread_per_criterion():
    # 스윕의 criteria[].actual 은 기준별 최악 코너 값이다. nominal 과의 거리가
    # 그 기준이 코너에서 밀려나는 양이고, 그것이 곧 남겨야 할 여유분이다.
    from analogcoder.judge_tools import corner_allowances

    nominal = {"iq_ua": 235.0, "vbg": 1.24}
    sweep = {
        "criteria": [
            {"name": "iq", "actual": 268.0},
            {"name": "vbg_min", "actual": 1.196},
        ]
    }
    crit = [_c("iq", "iq_ua", "<=", 300.0), _c("vbg_min", "vbg", ">=", 1.20)]

    allowances = corner_allowances(nominal, sweep, crit)

    assert allowances["iq"] == pytest.approx(33.0)
    assert allowances["vbg_min"] == pytest.approx(0.044)


def test_a_corner_value_that_is_missing_yields_no_allowance_rather_than_zero():
    # 0 을 넣으면 "코너가 이 기준을 전혀 안 움직인다"는 거짓 사실이 된다.
    # 없는 것은 없는 채로 둔다 - 호출부가 그것을 구분할 수 있어야 한다.
    from analogcoder.judge_tools import corner_allowances

    crit = [_c("iq", "iq_ua", "<=", 300.0)]

    assert corner_allowances({"iq_ua": 235.0}, {"criteria": []}, crit) == {}


def test_a_corner_actual_that_is_nan_yields_no_allowance_rather_than_zero():
    # run_full_pvt_sweep은 값이 없는 경우 항목을 통째로 빼지 않는다 - 모든
    # criterion에 대해 evaluate_criteria를 호출하고, 값을 못 구한 코너는
    # actual=NaN인 항목으로 채운다 (pvt.py, evaluate_criteria의
    # missing-measurement 경로). corner_allowances가 실제로 마주치는 "값 없음"
    # 모양은 빈 리스트가 아니라 이 NaN 항목이다.
    from analogcoder.judge_tools import corner_allowances

    crit = [_c("iq", "iq_ua", "<=", 300.0)]
    sweep = {"criteria": [{"name": "iq", "actual": float("nan")}]}

    allowances = corner_allowances({"iq_ua": 235.0}, sweep, crit)

    assert "iq" not in allowances


def test_a_nan_nominal_value_yields_no_allowance_rather_than_zero():
    # 대칭 케이스: 코너 값은 멀쩡한데 nominal 쪽이 NaN인 경우도 "0" 이 아니라
    # 부재로 처리되어야 한다.
    from analogcoder.judge_tools import corner_allowances

    crit = [_c("iq", "iq_ua", "<=", 300.0)]
    sweep = {"criteria": [{"name": "iq", "actual": 268.0}]}

    allowances = corner_allowances({"iq_ua": float("nan")}, sweep, crit)

    assert "iq" not in allowances


def test_the_allowance_is_measured_from_whatever_reference_it_is_given():
    # 같은 스윕에 대해, 기준점이 최악에 가까울수록 여유분이 작아야 한다.
    # 기준점을 무시하고 nominal을 어딘가에서 다시 읽는 변형을 이 단언이 잡는다.
    from analogcoder.judge_tools import corner_allowances

    crit = [_c("gain", "g", ">=", 40.0)]
    sweep = {"criteria": [{"name": "gain", "actual": 41.0}]}

    from_nominal = corner_allowances({"g": 50.0}, sweep, crit)
    from_reduced = corner_allowances({"g": 43.0}, sweep, crit)

    assert from_nominal["gain"] == pytest.approx(9.0)
    assert from_reduced["gain"] == pytest.approx(2.0)
    assert from_reduced["gain"] < from_nominal["gain"]


def test_ratio_allowances_are_the_fallback_when_corners_cannot_be_measured():
    from analogcoder.judge_tools import ratio_allowances

    crit = [_c("iq", "iq_ua", "<=", 300.0), _c("psr", "psr_db", "<=", -10.0)]

    allowances = ratio_allowances(crit, 0.2)

    assert allowances["iq"] == pytest.approx(60.0)
    assert allowances["psr"] == pytest.approx(2.0)  # |T| 를 쓰므로 부호와 무관
