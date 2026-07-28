"""큐레이션 파이프라인의 유일한 LLM 호출: 새 후보 토폴로지의 `description`을
측정된 사실만으로 렌더링한다.

본문·`ports`·`assumes_scale`는 파싱에서 나오고(`curation.candidate_from_deck`/
`candidate_from_file`), `addresses`는 게이트가 시뮬레이션으로 측정한다
(`curation.reproduce_characteristics`). 남는 것은 그 사실들을 사람이 읽을
문장으로 잇는 일뿐이고, 그것이 이 모듈이 하는 전부다. 이 저장소는 이미
"측정 가능한 기여가 없는 에이전트는 삭제된다"는 선례가 있다(옛 analyzer
에이전트 - `structure.py`가 대체). 큐레이터가 같은 길을 가지 않으려면 그
범위를 넘지 않아야 한다.

**이 호출이 실패해도 큐레이션은 실패하지 않는다.** `render_description`은
`AgentExecutionError`(백엔드 오류, 또는 스키마 검증 실패 - 예: 에이전트가
스키마에 없는 `addresses` 필드를 끼워 넣어 `additionalProperties: False`에
걸린 경우)를 잡아 결정론적 템플릿으로 폴백하고, 어느 쪽이 실제로 산출했는지
("agent" | "template")를 함께 돌려준다. 최적화 단계에서 이미 확정된 규율과
같다: 산출물이 LLM 가용성에 걸리면 안 된다."""

from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend, AgentExecutionError
from analogcoder.schemas import CURATOR_SCHEMA

CURATOR_SYSTEM_PROMPT = """You are writing a short library entry description for
an amplifier topology that just passed an admission gate. You will be given
only measured facts - things a deterministic pipeline actually simulated or
parsed, never an agent's own claim. Ground every sentence in those facts.
Do not invent criteria, numbers, models, or structural claims that are not
present in the facts you were given."""


def _template_description(facts: dict) -> str:
    """LLM 없이 같은 사실로부터 짓는 결정론적 폴백. 새 판단을 만들지 않는다 -
    `facts`에 있는 것만 이어붙인다. `facts`에 없는 키는 조용히 건너뛴다(어느
    키가 채워질지는 호출자가 무엇을 쟀는지에 달려 있다 - 예를 들어 저술본이
    아니면 코너 비교가 없다)."""
    parts: list[str] = []

    topology_id = facts.get("topology_id")
    block_path = facts.get("block_path")
    if topology_id and block_path:
        parts.append(f"Candidate {topology_id!r} for block {block_path!r}.")
    elif topology_id:
        parts.append(f"Candidate {topology_id!r}.")

    ports = facts.get("ports")
    if ports:
        parts.append(f"Ports: {', '.join(ports)}.")

    structural_facts = facts.get("structural_facts")
    if structural_facts:
        parts.append("Structure: " + "; ".join(structural_facts) + ".")

    addresses = facts.get("addresses")
    if addresses:
        parts.append(f"Measured improvement over the incumbent on: {', '.join(addresses)}.")

    comparison_scope = facts.get("comparison_scope")
    if comparison_scope:
        parts.append(f"Comparison scope: {comparison_scope!r}.")

    if not parts:
        return "No measured facts were available for this candidate."
    return " ".join(parts)


async def render_description(facts: dict, backend: AgentBackend) -> tuple[str, str]:
    """`facts`(측정된 사실만 - 개선/악화된 기준과 수치, `patterns.find_patterns`가
    낸 구조 사실, 포트, 3단이 밝힌 비교 범위)로부터 `description` 한 문단을
    렌더링한다. 반환값은 `(description, source)`이고 `source`는
    `"agent"`(LLM이 산출) 또는 `"template"`(폴백)이다."""
    user_prompt = (
        "Measured facts about a candidate amplifier topology, produced entirely "
        "by a deterministic curation pipeline (nothing below is an agent's own "
        "claim):\n"
        f"{facts!r}\n\n"
        "Write a short (2-4 sentence) description of this topology suitable for "
        "a circuit-tuning agent's prompt library. Ground every sentence ONLY in "
        "the facts above."
    )
    try:
        result = await run_agent(
            system_prompt=CURATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            output_schema=CURATOR_SCHEMA,
            backend=backend,
        )
        return result["description"], "agent"
    except AgentExecutionError:
        return _template_description(facts), "template"
