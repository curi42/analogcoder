ANALYZER_SCHEMA = {
    "type": "object",
    "properties": {
        "circuit_type": {"type": "string"},
        "stages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "components": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "role", "components"],
            },
        },
        "component_roles": {"type": "object", "additionalProperties": {"type": "string"}},
        "tunable_params": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "refdes": {"type": "string"},
                    "param": {"type": "string"},
                    "role_in_circuit": {"type": "string"},
                },
                "required": ["refdes", "param", "role_in_circuit"],
            },
        },
    },
    "required": ["circuit_type", "stages", "component_roles", "tunable_params"],
}

SIMULATION_SCHEMA = {
    "type": "object",
    "properties": {
        "measurements": {"type": "object", "additionalProperties": {"type": "number"}},
        "status": {"enum": ["success", "convergence_failure", "error"]},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["measurements", "status", "warnings"],
}

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_pass": {"type": "boolean"},
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "target": {"type": "string"},
                    "actual": {"type": "number"},
                    "pass": {"type": "boolean"},
                    "margin": {"type": "number"},
                },
                "required": ["name", "target", "actual", "pass", "margin"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["overall_pass", "criteria", "summary"],
}

TUNER_SCHEMA = {
    "type": "object",
    "properties": {
        "proposed_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "refdes": {
                        "type": "string",
                        "pattern": r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$",
                    },
                    "param": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
                    "old_value": {"type": "string", "pattern": r"^-?[0-9]*\.?[0-9]+([eE][+-]?[0-9]+)?[a-zA-Z]*$"},
                    "new_value": {"type": "string", "pattern": r"^-?[0-9]*\.?[0-9]+([eE][+-]?[0-9]+)?[a-zA-Z]*$"},
                    "reasoning": {"type": "string"},
                },
                "required": ["refdes", "param", "old_value", "new_value", "reasoning"],
            },
        },
        "overall_reasoning": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["proposed_changes", "overall_reasoning", "confidence"],
}

VERIFIER_PRE_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "concerns": {"type": "array", "items": {"type": "string"}},
        "feedback": {"type": "string"},
    },
    "required": ["approved", "concerns", "feedback"],
}

VERIFIER_POST_SCHEMA = {
    "type": "object",
    "properties": {
        "improved": {"type": "boolean"},
        "regressed_criteria": {"type": "array", "items": {"type": "string"}},
        "recommendation": {"enum": ["keep", "rollback"]},
        "feedback": {"type": "string"},
    },
    "required": ["improved", "regressed_criteria", "recommendation", "feedback"],
}

TOPOLOGY_SCHEMA = {
    "type": "object",
    "properties": {
        "topology_id": {"type": "string", "pattern": "^[a-z_][a-z0-9_]*$"},
        "reasoning": {"type": "string"},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": ["topology_id", "reasoning", "confidence"],
}
