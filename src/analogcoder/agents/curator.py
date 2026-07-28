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
`DESCRIPTION_FALLBACK_ERRORS`(백엔드 오류·스키마 검증 실패를 포함한, 이 호출에
실제로 도달 가능한 예외들 - 그 상수의 주석에 타입별 출처가 있다)를 잡아
결정론적 템플릿으로 폴백하고, 어느 쪽이 실제로 산출했는지("agent" |
"template")를 함께 돌려준다. 최적화 단계에서 이미 확정된 규율과 같다:
산출물이 LLM 가용성에 걸리면 안 된다. 그리고 이 설계의 명시적 규칙 - **LLM이
판정을 결정하지 않는다** - 도 같은 것을 요구한다: 네 단계가 전부 통과한
실행이 이 한 호출 때문에 `INCONCLUSIVE`가 되면 LLM이 판정을 뒤집은 것이다."""

import logging

from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend, AgentExecutionError
from analogcoder.schemas import CURATOR_SCHEMA

logger = logging.getLogger(__name__)

# `render_description`의 폴백이 잡는 예외들. **이 목록은 실제로 도달 가능한
# 것들만 담는다** - `run_optimization`이 자기 예외 타입을 하나씩 이름 붙여
# 적어 둔 것과 같은 규율이고, 맨 `except Exception`을 쓰지 않는 이유다.
#
# 왜 `AgentExecutionError` 하나로는 부족했는가(리뷰가 실행으로 확인):
# 네 단계가 전부 통과한 실행이 이 한 호출 때문에 INCONCLUSIVE로 끝났다.
# 설계 문서의 "이 호출이 실패해도 큐레이션은 실패하지 않는다"와, 이 저장소가
# 최적화 단계에서 확정한 "산출물이 LLM 가용성에 걸리면 안 된다"를 둘 다
# 어긴다 - 게다가 LLM이 판정을 바꾸면 안 된다는 것이 이 설계의 명시적 규칙이다.
#
# 각 타입이 어디서 오는가:
#   AgentExecutionError - 두 백엔드가 정상 경로에서 내는 실패(스키마 검증
#     실패, 에러 ResultMessage, HTTP 오류, 그리고 이제 SDK 전송 실패와
#     OpenAI 호환 서버의 형식 불량 응답까지 여기로 정규화된다).
#   KeyError  - `os.environ[api_key_env]`(토큰 환경변수 미설정), 그리고
#     `result["description"]`(스키마 검증을 통과했다면 도달하지 않지만,
#     검증기를 우회하는 백엔드가 생기면 도달한다).
#   ValueError - `json.JSONDecodeError`의 상위 타입. 백엔드 밖에서 응답을
#     파싱하는 어떤 경로든 이것으로 나온다.
#   IndexError - 위와 같은 계열의 인덱싱 실패.
#   OSError    - 소켓/파일 디스크립터 수준의 I/O 실패. httpx가 감싸지 못한
#     저수준 오류나, CLI 서브프로세스를 띄우는 백엔드의 spawn 실패가 여기다.
#
# **`TypeError`는 일부러 빼 둔다.** 그것은 백엔드가 아니라 호출자가 잘못된
# `facts` 모양을 넘겼을 때의 모양이고, 그것을 조용한 템플릿 폴백 뒤에 숨기면
# 이 저장소의 진짜 버그가 "LLM이 안 됐나 보다"로 읽힌다. 폴백은 "LLM이
# 답하지 못했다"를 위한 것이지 이 파이프라인 자신의 결함을 위한 것이 아니다.
DESCRIPTION_FALLBACK_ERRORS = (AgentExecutionError, KeyError, ValueError, IndexError, OSError)

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
    except DESCRIPTION_FALLBACK_ERRORS as exc:
        logger.warning(
            "description rendering failed (%s: %s) - falling back to the deterministic "
            "template; the curation verdict is unaffected",
            type(exc).__name__,
            exc,
        )
        return _template_description(facts), "template"
