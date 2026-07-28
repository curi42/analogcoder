"""큐레이션의 소스 C - 기법 이름 하나로 슬롯의 기존(이미 사이징된) 블록
본문을 **국소 수정**하는 저술 에이전트.

이 모듈이 튜너와 다른 이유(왜 여기서는 LLM이 SPICE를 저술해도 되는가)는
`curation.py`의 모듈 docstring과 `docs/superpowers/specs/
2026-07-28-topology-curation-design.md`의 "소스 C" 절에 있다. 요약: 튜너의
제안은 시뮬레이션 없이 적용되지만(위험한 것은 저술이 아니라 검증 없는
적용이다), 이 모듈이 낸 본문은 그 사이에 1단(구조 검사)·2단(특성 재현)·
2.5단(코너 검증, 저술본 전용)·사람의 커밋이 낀다.

`author_variant`는 **한 번의 LLM 호출**이다(`agents.tuner.propose_tuning`과
같은 모양 - 재시도 루프는 호출자가 돈다). `author_and_verify_variant`가 그
재시도 루프다: 1단이나 2단이 거부하면 그 사유를 그대로 다음 시도의
`rejection_feedback`으로 돌려주고, `MAX_VARIANT_AUTHOR_RETRIES`번까지
다시 시도한다 - 튜닝 제안의 재시도 루프(`orchestrator.MAX_TUNING_RETRIES`)와
같은 모양, 같은 상한값이지만 이 모듈 자신의 상수로 둔다(다른 서브시스템의
상수를 임포트해 결합을 만들지 않는다).

세 판정이 절대 섞이면 안 된다:

- 상한을 다 쓰고도 통과하는 본문을 못 만들었다 -> `REJECT`(재보지 못한 것이
  아니라 재봤는데 실패했다).
- 시뮬레이터가 재현 단계에서 예외로 죽었다(1단/2단이 아니라 시뮬레이터 자체가
  답을 못 냈다) -> `INCONCLUSIVE`(재시도해도 회로가 아니라 시뮬레이터의
  문제이므로 즉시 멈춘다).
- 백엔드가 죽었거나 스키마를 못 맞췄다(`AgentExecutionError`) -> `INCONCLUSIVE`
  (LLM이 죽은 것은 회로의 문제가 아니다 - 이 역시 즉시 멈추고 남은 재시도
  예산을 쓰지 않는다, 백엔드가 죽었다면 다시 불러도 대개 또 죽기 때문이다).
"""

from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend, AgentExecutionError
from analogcoder.curation import Slot, candidate_from_technique, check_structure, reproduce_characteristics
from analogcoder.schemas import VARIANT_AUTHOR_SCHEMA

# 브리프 규칙 3: "상한은 기존 MAX_TUNING_RETRIES와 같은 값을 쓰되 이 모듈의
# 상수로 둔다" - orchestrator.MAX_TUNING_RETRIES를 임포트하지 않고 같은 값
# (3)을 이 모듈 자신의 상수로 복제한다.
MAX_VARIANT_AUTHOR_RETRIES = 3

VARIANT_AUTHOR_SYSTEM_PROMPT = """You are an analog circuit topology specialist
authoring a LOCAL MODIFICATION of an existing, already-sized amplifier block for
a curated topology library. You are NOT designing from scratch: the block you
are given already works and passed a full corner sweep in this PDK. Inherit its
existing component sizing wherever the technique does not require you to add,
remove, or resize a device - do not re-derive sizes for devices you are not
touching, and do not restyle or reorder the netlist beyond what the technique
requires.

The technique names a single, well-known modification (e.g. "add a nulling
resistor in series with the compensation capacitor", "move the compensation
capacitor's connection point", "add a cascode device", "invert the input
pair's polarity"). Apply exactly that technique to the given body.

You may reference ONLY the device/subckt model names in the "models this deck
instantiates" list below - the deck that will host this block does not define
any model outside that set, and using one that isn't there will make the
candidate fail before it is ever simulated. Do not invent a device model.

The body must keep exactly the given port list (same names, same order) - it is
a body only (no ".subckt"/".ends" header), so ports are declared by whoever
calls it, not by you.

Respond via the structured output schema: "subckt_body" is the modified SPICE
body text, "rationale" is a short explanation of what you changed and why."""


async def author_variant(
    base_body: str,
    technique: str,
    ports: list[str],
    available_models: set[str],
    scale: float,
    rejection_feedback: str | None,
    backend: AgentBackend,
) -> dict:
    """한 번의 LLM 호출. `propose_tuning`/`render_description`과 같은 모양 -
    재시도는 호출자(`author_and_verify_variant`)의 몫이다.

    프롬프트에 반드시 실리는 넷(브리프 규칙 1): 기존 본문(사이징된 채로),
    `technique` 문자열, 포트 목록, 그 덱이 인스턴스화하는 모델 이름 집합,
    `.option scale`. 모델 집합이 빠지면 에이전트가 덱에 없는 소자를 쓰고
    1단(`check_structure`)에서 튕긴다 - `miller_basic`이 `sky130_fd_pr__
    cap_mim_m3_1`을 써서 bandgap 덱에 못 들어가는 것과 정확히 같은 모양."""
    user_prompt = (
        f"Existing block body (already sized in this PDK - inherit this sizing "
        f"for any device the technique does not touch):\n{base_body}\n"
        f"Technique to apply as a LOCAL modification (not a from-scratch design): "
        f"{technique!r}\n"
        f"Ports (the body must keep exactly these, same order): {ports}\n"
        f"Models this deck actually instantiates (use ONLY these - any other model "
        f"name will be rejected before simulation): {sorted(available_models)}\n"
        f".option scale: {scale!r}\n"
        f"Rejection feedback from the previous attempt (if retrying, fix exactly "
        f"this): {rejection_feedback}"
    )
    return await run_agent(
        system_prompt=VARIANT_AUTHOR_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=VARIANT_AUTHOR_SCHEMA,
        backend=backend,
    )


def _stage_rejection_reason(stage_name: str, detail: dict) -> str:
    """`StageResult.detail`에서 다음 시도로 그대로 돌려줄 사유 문자열을
    뽑는다. `check_structure`(1단)의 detail은 `reason`/`detail` 키를 쓰고
    (`compatible_swaps`가 낸 그대로), `reproduce_characteristics`(2단)의
    실패 detail은 `missing` 키를 쓴다 - 두 단계의 detail 모양이 다르므로
    사유를 뽑는 규칙도 단계별로 다르다. 어느 쪽이든 여기서 새 문장을 짓지
    않는다: 단계가 이미 낸 사실을 그대로 이어붙일 뿐이다(브리프 규칙 3의
    "그대로")."""
    if stage_name == "structure":
        return f"stage 1 (structure) rejected: {detail.get('reason')}: {detail.get('detail')}"
    return f"stage 2 (reproduce) rejected: missing measurements: {detail.get('missing')}"


async def author_and_verify_variant(
    base_body: str,
    technique: str,
    ports: list[str],
    available_models: set[str],
    scale: float,
    topology_id: str,
    slot: Slot,
    netlist_texts: dict[str, str],
    sim_backend,
    backend: AgentBackend,
) -> dict:
    """`author_variant`를 거부-재시도 루프로 감싸고, 통과한 본문을 1단
    (`check_structure`)·2단(`reproduce_characteristics`)에 실제로 통과시킨다.
    2.5단(코너)·3단(범위 밝힌 비교)은 여기서 돌지 않는다 - 그 둘은 저술
    자체를 재시도할 이유가 없는, 이미 통과한 후보에 대한 별도 판정이고
    (설계 문서 "2.5단"/"3단" 절), 이 함수가 그 후보를 다음 단계로 넘기는
    자리다.

    반환값은 항상 다음 키를 채운 dict다: `verdict`("PASS" | "REJECT" |
    "INCONCLUSIVE"), `reason`(REJECT/INCONCLUSIVE일 때 마지막 거부/오류
    사유, PASS일 때 None), `attempts`(실제로 LLM을 호출한 횟수), `candidate`
    (PASS일 때만 `Candidate`, 그 외 None), `rationale`(에이전트가 낸 설명,
    PASS일 때만), `structure`/`reproduce`(각 단계의 `StageResult` - 도달하지
    못한 단계는 None), `addresses`(2단이 측정한, 후보가 기존 본문보다 나은
    기준 이름들 - PASS일 때만).

    `AgentExecutionError`는 즉시 `INCONCLUSIVE`로 끝난다(재시도하지 않는다) -
    백엔드가 죽었다면 다시 불러도 대개 또 죽고, 남은 재시도 예산을 태우는
    것은 "회로가 나쁘다"는 사실을 하나도 더 밝히지 못한다. 2단이 시뮬레이터
    예외로 `inconclusive`를 낼 때도 같은 이유로 즉시 멈춘다 - 그것은 이
    저술본이 나쁘다는 증거가 아니라 시뮬레이터가 답하지 못했다는 사실이고,
    다시 저술을 시켜도 시뮬레이터가 죽는 이유는 바뀌지 않는다."""
    rejection_feedback: str | None = None
    last_reason: str | None = None

    for attempt in range(1, MAX_VARIANT_AUTHOR_RETRIES + 1):
        try:
            authored = await author_variant(
                base_body=base_body,
                technique=technique,
                ports=ports,
                available_models=available_models,
                scale=scale,
                rejection_feedback=rejection_feedback,
                backend=backend,
            )
        except AgentExecutionError as exc:
            return {
                "verdict": "INCONCLUSIVE",
                "reason": str(exc),
                "attempts": attempt,
                "candidate": None,
                "rationale": None,
                "structure": None,
                "reproduce": None,
                "addresses": [],
            }

        candidate = candidate_from_technique(
            subckt_body=authored["subckt_body"],
            ports=ports,
            assumes_scale=scale,
            topology_id=topology_id,
        )

        structure_result = check_structure(candidate, slot, netlist_texts)
        if structure_result.status != "pass":
            rejection_feedback = _stage_rejection_reason("structure", structure_result.detail)
            last_reason = rejection_feedback
            continue

        reproduce_result, addresses = reproduce_characteristics(candidate, slot, netlist_texts, sim_backend)
        if reproduce_result.status == "inconclusive":
            return {
                "verdict": "INCONCLUSIVE",
                "reason": reproduce_result.detail.get("error", "stage 2 (reproduce) was inconclusive"),
                "attempts": attempt,
                "candidate": None,
                "rationale": authored.get("rationale"),
                "structure": structure_result,
                "reproduce": reproduce_result,
                "addresses": [],
            }
        if reproduce_result.status != "pass":
            rejection_feedback = _stage_rejection_reason("reproduce", reproduce_result.detail)
            last_reason = rejection_feedback
            continue

        return {
            "verdict": "PASS",
            "reason": None,
            "attempts": attempt,
            "candidate": candidate,
            "rationale": authored.get("rationale"),
            "structure": structure_result,
            "reproduce": reproduce_result,
            "addresses": addresses,
        }

    return {
        "verdict": "REJECT",
        "reason": last_reason,
        "attempts": MAX_VARIANT_AUTHOR_RETRIES,
        "candidate": None,
        "rationale": None,
        "structure": None,
        "reproduce": None,
        "addresses": [],
    }
