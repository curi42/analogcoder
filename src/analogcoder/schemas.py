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
        # Declared, deliberately not required: a weak model that omits it would
        # otherwise hard-FAIL every spec (this repo hit exactly that with
        # `control_block` in a previous sub-project). The orchestrator (Task 5)
        # resolves an omitted block_path from the candidate list it offered.
        "block_path": {"type": "string"},
        "reasoning": {"type": "string"},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": ["topology_id", "reasoning", "confidence"],
}

# 큐레이션 파이프라인의 유일한 LLM 호출(agents/curator.py)이 쓰는 스키마.
# 필드가 정확히 하나다 - `addresses`(어느 기준을 개선했는가)는 게이트가 실제
# 시뮬레이션에서 측정하는 값이지 에이전트가 선언할 값이 아니므로, 여기 없는
# 것이 사고가 아니라 규칙이다. `additionalProperties: False`가 이를
# 강제한다: 에이전트가 스키마에 없는 `addresses` 같은 필드를 끼워 넣어도
# 스키마 검증에서 걸러지므로("description"만 있어야 함), 잘못된 산출물이
# 조용히 통과해 튜너 프롬프트에 검증되지 않은 주장을 흘리는 일이 없다 -
# 그런 응답은 `AgentExecutionError`가 되어 결정론적 템플릿으로 폴백한다.
CURATOR_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
    },
    "required": ["description"],
    "additionalProperties": False,
}

# 큐레이션 소스 C(agents/variant_author.py)가 쓰는 스키마. `addresses`나
# `topology_id` 같은 판정/식별 필드가 없는 것은 CURATOR_SCHEMA와 같은 이유다 -
# 이 에이전트는 본문을 저술할 뿐, 그 본문이 슬롯에 맞는지(1단)나 기존 것보다
# 나은지(2단)는 결정론적 게이트가 시뮬레이션으로 잰다. `additionalProperties:
# False`가 에이전트 스스로의 판정 주장이 스키마를 통과해 다음 단계로 새는
# 것을 막는다.
VARIANT_AUTHOR_SCHEMA = {
    "type": "object",
    "properties": {
        "subckt_body": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["subckt_body", "rationale"],
    "additionalProperties": False,
}
