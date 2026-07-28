"""큐레이션 소스 C의 유일한 LLM 호출 - 기법 이름 하나로 슬롯의 기존(이미
사이징된) 블록 본문을 **국소 수정**하는 저술 에이전트.

CLAUDE.md의 확립된 관례대로 `agents/*.py`는 시스템 프롬프트·스키마·(있다면)
도구 선언만 담고, 재시도/오케스트레이션은 게이트 옆의 결정론적 모듈에 둔다
(파라미터 튜닝의 `orchestrator.py`, 최적화의 `optimizer.py` vs
`agents/optimizer.py`의 순위-매기기 분리와 같은 자리). 그래서 이 파일은
`author_variant` - **한 번의 LLM 호출**(`agents.tuner.propose_tuning`/
`agents.curator.render_description`과 같은 모양) - 만 담는다. 거부-재시도
루프(`author_and_verify_variant`, 1단 `check_structure`·2단
`reproduce_characteristics`와 실제로 맞물리는 오케스트레이션)는
`curation.py`에 있다 - 그 함수가 게이트를 직접 부르므로, 게이트 옆에
둔다는 같은 관례를 따른다.

이 모듈이 튜너와 달리 LLM이 SPICE를 저술해도 되는 이유는 `curation.py`의
모듈 docstring과 `docs/superpowers/specs/2026-07-28-topology-curation-design.md`
의 "소스 C" 절에 있다."""

from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend
from analogcoder.schemas import VARIANT_AUTHOR_SCHEMA

VARIANT_AUTHOR_SYSTEM_PROMPT = """You are an analog circuit topology specialist
authoring a LOCAL MODIFICATION of an existing, already-sized amplifier block for
a curated topology library. You are NOT designing from scratch: the block you
are given is the slot's current incumbent body, already sized in this PDK and
simulated as part of a working deck. (This pipeline does NOT know whether that
body was ever corner-verified, and makes no such claim - your variant will be
corner-swept by the gate before it can be admitted.) Inherit its existing
component sizing wherever the technique does not require you to add, remove, or
resize a device - do not re-derive sizes for devices you are not touching, and
do not restyle or reorder the netlist beyond what the technique requires.

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
body text and is required; "rationale" is an optional short explanation of what
you changed and why. Never omit "subckt_body" - it is the whole output."""


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
    재시도는 호출자(`curation.author_and_verify_variant`)의 몫이다.

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
