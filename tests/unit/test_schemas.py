import jsonschema

from analogcoder.schemas import (
    ANALYZER_SCHEMA,
    JUDGE_SCHEMA,
    SIMULATION_SCHEMA,
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


def test_verifier_pre_schema_accepts_valid_payload():
    jsonschema.validate({"approved": True, "concerns": [], "feedback": "looks reasonable"}, VERIFIER_PRE_SCHEMA)


def test_verifier_post_schema_accepts_valid_payload():
    jsonschema.validate(
        {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "gain now passes"},
        VERIFIER_POST_SCHEMA,
    )
