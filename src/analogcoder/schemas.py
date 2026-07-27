SIMULATION_SCHEMA = {
    "type": "object",
    "properties": {
        "measurements": {"type": "object", "additionalProperties": {"type": "number"}},
        "status": {"enum": ["success", "convergence_failure", "error"]},
        "warnings": {"type": "array", "items": {"type": "string"}},
        # Corner simulations inherit this and run it directly. If a
        # convergence retry adjusted .options, this must be the adjusted
        # control block, not the one the caller originally supplied - that's
        # the whole point of reporting it back.
        #
        # **Declared, deliberately not required.** A weak model that emits
        # measurements/status/warnings but drops this field would otherwise
        # fail validation, exhaust the repair loop, raise AgentExecutionError
        # and end the whole run as FAIL - including on every spec with no
        # pvt_corners and no corner_reduction, where nothing ever reads it.
        # The simulator is the tool-calling agent this repo has documented as
        # the fragile one on the local-model path, so the cost of requiring it
        # falls exactly where the field is least likely to arrive.
        # corner_sim.py already carries the right fallback
        # (`agent_result.get("control_block") or tb.control_block`); listing it
        # in `required` was the only thing making that fallback unreachable.
        "control_block": {"type": "string"},
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

OPTIMIZER_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "refdes": {
                        "type": "string",
                        "pattern": r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$",
                    },
                    "param": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
                    "direction": {"enum": ["increase", "decrease"]},
                    "reasoning": {"type": "string"},
                },
                "required": ["refdes", "param", "direction", "reasoning"],
                "additionalProperties": False,
            },
        },
        "overall_reasoning": {"type": "string"},
    },
    "required": ["candidates", "overall_reasoning"],
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
