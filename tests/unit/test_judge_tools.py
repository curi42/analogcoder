from analogcoder.judge_tools import evaluate_criteria
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
