import jsonschema
import pytest

from analogcoder.schemas import (
    ANALYZER_SCHEMA,
    JUDGE_SCHEMA,
    SIMULATION_SCHEMA,
    TOPOLOGY_SCHEMA,
    TUNER_SCHEMA,
    VERIFIER_POST_SCHEMA,
    VERIFIER_PRE_SCHEMA,
)


def test_analyzer_schema_accepts_valid_payload():
    payload = {
        "circuit_type": "inverting amplifier",
        "stages": [{"name": "feedback stage", "role": "sets closed-loop gain", "components": ["Rin", "Rf"]}],
        "component_roles": {"Rin": "input resistor", "Rf": "feedback resistor"},
        "tunable_params": [{"refdes": "Rf", "param": "value", "role_in_circuit": "sets gain magnitude"}],
    }
    jsonschema.validate(payload, ANALYZER_SCHEMA)


def test_simulation_schema_accepts_valid_payload():
    payload = {"measurements": {"gain_db": 20.0}, "status": "success", "warnings": []}
    jsonschema.validate(payload, SIMULATION_SCHEMA)


def test_judge_schema_accepts_valid_payload():
    payload = {
        "overall_pass": True,
        "criteria": [{"name": "gain", "target": ">=19.5", "actual": 20.0, "pass": True, "margin": 0.5}],
        "summary": "all criteria passed",
    }
    jsonschema.validate(payload, JUDGE_SCHEMA)


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
