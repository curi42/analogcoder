import re

import jsonschema
import pytest

from analogcoder.schemas import (
    SIMULATION_SCHEMA,
    TOPOLOGY_SCHEMA,
    TUNER_SCHEMA,
    VERIFIER_POST_SCHEMA,
    VERIFIER_PRE_SCHEMA,
)

REFDES_PATTERN = TUNER_SCHEMA["properties"]["proposed_changes"]["items"]["properties"][
    "refdes"
]["pattern"]


def test_simulation_schema_accepts_valid_payload():
    payload = {
        "measurements": {"gain_db": 20.0},
        "status": "success",
        "warnings": [],
        "control_block": ".control\nac dec 10 1 1meg\n.endc",
    }
    jsonschema.validate(payload, SIMULATION_SCHEMA)


def test_there_is_no_judge_schema_because_judging_is_not_an_llm_call():
    # 판정은 `judge_tools.evaluate_criteria`가 낸다. 검증할 LLM 출력이 없으니
    # 스키마도 없다 - 남겨 두면 "judge 출력은 검증된다"는 인상만 남는다.
    import analogcoder.schemas as schemas

    assert not hasattr(schemas, "JUDGE_SCHEMA")


def test_tuner_schema_accepts_valid_payload():
    payload = {
        "proposed_changes": [
            {"refdes": "Rf", "param": "value", "old_value": "10k", "new_value": "11k", "reasoning": "increase gain"}
        ],
        "overall_reasoning": "gain was slightly under target",
        "confidence": 0.8,
    }
    jsonschema.validate(payload, TUNER_SCHEMA)


def test_tuner_schema_accepts_named_param_and_scientific_notation_value():
    payload = {
        "proposed_changes": [
            {"refdes": "M1", "param": "W", "old_value": "10u", "new_value": "1.5e-5", "reasoning": "widen device"}
        ],
        "overall_reasoning": "gain was slightly under target",
        "confidence": 0.8,
    }
    jsonschema.validate(payload, TUNER_SCHEMA)


# Note: "M1.W" and "Cc.kappa" are deliberately NOT in this list. They are
# syntactically valid <scope>.<refdes> forms per TUNER_SCHEMA's pattern (a
# weak model writing "M1.W" probably meant to set M1's W param but put it in
# the refdes field instead) - schema validation can't tell that from a
# legitimate "BUF_N.Xcc" scoped refdes. Rejecting them is
# netlist.check_refdes_resolution's job (a scope that names no subckt is
# treated as "matches nothing"), exercised in tests/unit/test_netlist.py's
# test_check_refdes_resolution_rejects_a_dotted_refdes_whose_scope_names_no_subckt
# and test_check_refdes_resolution_rejects_cc_dot_kappa_shaped_refdes.
@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("refdes", "1Cc"),
        ("refdes", "Cc kappa"),
        ("param", "resistance value"),
        ("old_value", "unknown"),
        ("new_value", "increase Rf to 15k"),
    ],
)
def test_tuner_schema_rejects_non_literal_values(field, bad_value):
    change = {"refdes": "Rf", "param": "value", "old_value": "10k", "new_value": "11k", "reasoning": "x"}
    change[field] = bad_value
    payload = {
        "proposed_changes": [change],
        "overall_reasoning": "gain was slightly under target",
        "confidence": 0.8,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, TUNER_SCHEMA)


def test_tuner_schema_accepts_a_subckt_scoped_refdes():
    proposal = {
        "proposed_changes": [
            {
                "refdes": "BUF_N.Xcc",
                "param": "W",
                "old_value": "20",
                "new_value": "30",
                "reasoning": "widen the vbg1 buffer's compensation cap",
            }
        ],
        "overall_reasoning": "improve vbg1 settling",
        "confidence": 0.8,
    }

    jsonschema.validate(proposal, TUNER_SCHEMA)


def test_tuner_schema_rejects_a_malformed_scoped_refdes():
    proposal = {
        "proposed_changes": [
            {
                "refdes": "BUF_N.",
                "param": "W",
                "old_value": "20",
                "new_value": "30",
                "reasoning": "trailing dot is not a refdes",
            }
        ],
        "overall_reasoning": "x",
        "confidence": 0.8,
    }

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(proposal, TUNER_SCHEMA)


def test_the_refdes_pattern_accepts_any_nesting_depth():
    regex = re.compile(REFDES_PATTERN)

    assert regex.match("Rf")
    assert regex.match("BUF_P.X6")
    assert regex.match("OUTER.INNER.M1")
    assert regex.match("A.B.C.D.M1")


def test_the_refdes_pattern_still_rejects_malformed_names():
    regex = re.compile(REFDES_PATTERN)

    assert not regex.match("")
    assert not regex.match(".M1")
    assert not regex.match("M1.")
    assert not regex.match("A..M1")
    assert not regex.match("1M.X")
    assert not regex.match("A B")


def test_verifier_pre_schema_accepts_valid_payload():
    jsonschema.validate({"approved": True, "concerns": [], "feedback": "looks reasonable"}, VERIFIER_PRE_SCHEMA)


def test_verifier_post_schema_accepts_valid_payload():
    jsonschema.validate(
        {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "gain now passes"},
        VERIFIER_POST_SCHEMA,
    )


def test_topology_schema_accepts_valid_payload():
    payload = {"topology_id": "miller_nulling_resistor", "reasoning": "fixes phase margin", "confidence": 90}
    jsonschema.validate(payload, TOPOLOGY_SCHEMA)


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("topology_id", "Miller.Nulling"),
        ("topology_id", "miller nulling resistor"),
        ("topology_id", "123_starts_with_digit"),
        ("confidence", -1),
        ("confidence", 101),
    ],
)
def test_topology_schema_rejects_invalid_values(field, bad_value):
    payload = {"topology_id": "miller_nulling_resistor", "reasoning": "x", "confidence": 90}
    payload[field] = bad_value
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, TOPOLOGY_SCHEMA)


def _valid_tuner_payload():
    return {
        "proposed_changes": [
            {"refdes": "M1", "param": "W", "old_value": "8",
             "new_value": "10", "reasoning": "x"}
        ],
        "overall_reasoning": "x",
        "confidence": 90,
    }


def test_tuner_schema_accepts_alternatives_and_does_not_require_them():
    # 오늘의 모양이 그대로 유효해야 한다 - `alternatives`는 required가 아니다.
    # 약한 모델이 빠뜨린 필수 필드가 스펙 전체를 하드 FAIL시키는 것을
    # TOPOLOGY_SCHEMA의 `block_path`에서 이미 겪었다.
    jsonschema.validate(_valid_tuner_payload(), TUNER_SCHEMA)

    with_alts = dict(_valid_tuner_payload(), alternatives=[
        {"changes": [{"refdes": "M2", "param": "W", "old_value": "4",
                      "new_value": "5", "reasoning": "y"}],
         "reasoning": "대안 1"},
    ])
    jsonschema.validate(with_alts, TUNER_SCHEMA)


def test_an_alternative_change_obeys_the_same_refdes_and_param_patterns():
    """대안이 느슨한 문법을 통과하면 게이트가 뒤에서 잡아야 하고, 그러면 대안
    하나가 재시도를 태운다. 1차 제안과 **같은 객체**를 참조해야 갈라지지 않는다."""
    bad = dict(_valid_tuner_payload(), alternatives=[
        # param에 점이 들어간 형태 - 오늘 proposed_changes가 거절하는 것
        {"changes": [{"refdes": "M2", "param": "X.W", "old_value": "4",
                      "new_value": "5", "reasoning": "y"}],
         "reasoning": "대안 1"},
    ])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, TUNER_SCHEMA)


def test_the_alternative_change_schema_is_the_same_object_as_the_primary_one():
    """손으로 두 번 쓰면 갈라진다 - compose.py가 netlist.py의 include 규칙을
    베껴 양방향으로 갈라진 전례가 있다. 동일성을 직접 못박는다."""
    primary = TUNER_SCHEMA["properties"]["proposed_changes"]["items"]
    alt = TUNER_SCHEMA["properties"]["alternatives"]["items"]["properties"]["changes"]["items"]
    assert alt is primary


def test_more_than_three_alternatives_is_refused_by_the_schema():
    """상한은 3이다. 스키마가 막지 않으면 정규화가 조용히 자르게 된다."""
    four = dict(_valid_tuner_payload(), alternatives=[
        {"changes": [{"refdes": f"M{i}", "param": "W", "old_value": "1",
                      "new_value": "2", "reasoning": "y"}], "reasoning": str(i)}
        for i in range(4)
    ])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(four, TUNER_SCHEMA)
