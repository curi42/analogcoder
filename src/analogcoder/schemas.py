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

# JUDGE_SCHEMA는 없다. 판정은 LLM이 아니라 `judge_tools.evaluate_criteria`이고,
# 그 반환 모양(`{overall_pass, criteria: [{name, target, actual, pass, margin}],
# summary}`)은 스키마로 검증할 LLM 출력이 아니라 함수의 계약이다. 검증할 것이
# 없는 스키마를 남겨 두면 "judge 출력은 검증된다"는 인상만 남는다.

# 1차 제안과 대안이 **같은 객체를 참조**한다. 손으로 두 번 쓰면 갈라지고,
# 느슨한 쪽을 통과한 변경은 뒤의 게이트가 잡아 대안 하나가 재시도를 태운다.
# `compose.py`가 `netlist.py`의 include 규칙을 베껴 양방향으로 갈라진 전례가 있다.
_TUNING_CHANGE_SCHEMA = {
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
}

TUNER_SCHEMA = {
    "type": "object",
    "properties": {
        "proposed_changes": {"type": "array", "items": _TUNING_CHANGE_SCHEMA},
        # `alternatives`는 **required가 아니다.** 약한 모델이 빠뜨린 필수 필드가
        # 스펙 전체를 하드 FAIL시키는 것을 TOPOLOGY_SCHEMA의 `block_path`에서
        # 이미 겪었다. 없거나 1개면 오늘 동작과 바이트 동일해야 한다.
        # 상한 3은 스키마가 막는다 - 여기서 막지 않으면 정규화가 조용히 자르게
        # 되고, 조용한 절단은 "전부 봤다"로 읽힌다.
        "alternatives": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "changes": {"type": "array", "items": _TUNING_CHANGE_SCHEMA},
                    "reasoning": {"type": "string"},
                },
                "required": ["changes", "reasoning"],
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
#
# `rationale`은 **required가 아니다.** 이유는 두 가지이고 둘 다 실측이다:
# (1) 이 저장소의 코드 어디도 그것을 읽지 않았다 - `grep rationale src/`가
#     dataclass 필드 하나만 찾는다. 판정에도, 산출물에도 쓰이지 않는 필드를
#     required로 두면 모델이 그것 하나를 빠뜨렸다는 이유로 실행이 끝난다.
# (2) 실제로 끝났다. 유효한 `subckt_body`를 내면서 `rationale`을 빠뜨린 모델은
#     `jsonschema` 검증 실패 -> `AgentExecutionError`가 되고,
#     `author_and_verify_variant`는 그것을 **재시도하지 않고** 즉시
#     `INCONCLUSIVE`로 끝낸다 - 3회의 재시도 예산을 손도 대지 않은 채
#     시도 1에서. CLAUDE.md의 약한 모델 절은 형식 불량 structured output을
#     로컬 모델의 **예상된** 실패로 적어 두고 있다.
# 필드 자체는 남긴다 - 있으면 유용하고, 이제 버리지 않고 기록한다
# (`VariantAuthorResult.rationale` -> `curation.json`/`curation_report.md`).
# "스키마 불일치를 재시도해야 하는가"라는 더 넓은 질문은 여기서 건드리지
# 않는다(후속 과제).
VARIANT_AUTHOR_SCHEMA = {
    "type": "object",
    "properties": {
        "subckt_body": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["subckt_body"],
    "additionalProperties": False,
}
