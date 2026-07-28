from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.backend import AgentExecutionError
from analogcoder.agents.curator import DESCRIPTION_FALLBACK_ERRORS, render_description
from analogcoder.schemas import CURATOR_SCHEMA


def test_the_description_schema_has_no_addresses_field():
    """addresses는 게이트가 시뮬레이션에서 측정하는 값이지 에이전트가
    선언할 값이 아니다 - 스키마에 그 필드가 없어야 에이전트가 애초에 그것을
    쓸 수 없다(additionalProperties: False가 있으니 쓰더라도 검증에서
    걸린다). 스키마에 그 필드를 몰래 추가하는 변형을 잡는다."""
    assert "addresses" not in CURATOR_SCHEMA["properties"]
    assert CURATOR_SCHEMA["properties"].keys() == {"description"}
    assert CURATOR_SCHEMA["required"] == ["description"]


@pytest.mark.asyncio
async def test_the_description_prompt_contains_only_measured_facts():
    """render_description에 준 facts에 없는 정보(여기서는 특정 개선 기준
    이름과 구조 사실 문자열)가 실제로 프롬프트에 그대로 실리는지, 그리고
    facts에 없는 내용을 프롬프트가 따로 지어내지 않는지 확인한다. 이
    저장소의 규칙(3단 comparison_scope, patterns.find_patterns의 구조
    사실, 개선/악화 기준, 포트)이 전부 facts를 거쳐야 프롬프트에 나타난다는
    것을 고정한다."""
    facts = {
        "topology_id": "folded_cascode_pmos_in_cs",
        "block_path": "BUF_P",
        "ports": ["vinp", "vinn", "vout", "vdd", "vss"],
        "addresses": ["buf0_loop_gain"],
        "structural_facts": ["M2.s == M1.d at mid, M2.g on ncas"],
        "comparison_scope": {"knobs_swept": ["Xt.W"], "simulation_count": 5},
    }
    with patch(
        "analogcoder.agents.curator.run_agent",
        new=AsyncMock(return_value={"description": "..."}),
    ) as mock_run:
        await render_description(facts, backend=object())

    prompt = mock_run.call_args.kwargs["user_prompt"]
    assert "folded_cascode_pmos_in_cs" in prompt
    assert "buf0_loop_gain" in prompt
    assert "M2.s == M1.d at mid, M2.g on ncas" in prompt
    assert "Xt.W" in prompt
    # a name that was never in facts must not appear, i.e. the prompt is
    # built from facts, not from some separately hardcoded example
    assert "vbg1_residual" not in prompt


@pytest.mark.asyncio
async def test_render_description_returns_the_agent_result_and_source_on_success():
    facts = {"topology_id": "t1", "block_path": "B"}
    with patch(
        "analogcoder.agents.curator.run_agent",
        new=AsyncMock(return_value={"description": "A folded cascode variant."}),
    ):
        text, source = await render_description(facts, backend=object())

    assert text == "A folded cascode variant."
    assert source == "agent"


@pytest.mark.asyncio
async def test_an_agent_failure_falls_back_to_a_template():
    """AgentExecutionError(백엔드 오류든 스키마 검증 실패든)를 잡아 결정론적
    템플릿으로 폴백해야 한다 - 큐레이션 산출물이 LLM 가용성에 걸리면 안
    된다는 규칙. `except`를 지우는 변형은 여기서 미처리 예외로 걸린다."""
    facts = {
        "topology_id": "t1",
        "block_path": "B",
        "ports": ["a", "b"],
        "addresses": ["gain"],
    }
    with patch(
        "analogcoder.agents.curator.run_agent",
        new=AsyncMock(side_effect=AgentExecutionError("rate limited")),
    ):
        text, source = await render_description(facts, backend=object())

    assert source == "template"
    assert isinstance(text, str) and text
    # the fallback must still be grounded in the facts it was given
    assert "t1" in text
    assert "gain" in text


@pytest.mark.asyncio
async def test_a_caller_side_type_error_is_still_not_swallowed():
    """The fallback is broad enough to cover every way a BACKEND can fail
    (see DESCRIPTION_FALLBACK_ERRORS) and deliberately no broader. TypeError
    is the shape of a CALLER handing render_description a wrong `facts`
    structure - a bug in this pipeline, not "the LLM did not work" - and
    hiding it behind a silent template fallback would make this repo's own
    defect read as an LLM outage.

    Mutation this catches: widening the except clause to a bare `Exception`
    (observed: no TypeError is raised, the call returns
    `('No measured facts were available for this candidate.', 'template')`,
    and `pytest.raises(TypeError)` fails with DID NOT RAISE)."""
    assert TypeError not in DESCRIPTION_FALLBACK_ERRORS
    with patch(
        "analogcoder.agents.curator.run_agent",
        new=AsyncMock(side_effect=TypeError("boom")),
    ):
        with pytest.raises(TypeError):
            await render_description({}, backend=object())


@pytest.mark.parametrize(
    "exc",
    [
        KeyError("choices"),
        IndexError("list index out of range"),
        ValueError("Expecting value: line 1 column 1 (char 0)"),
        OSError("connection reset by peer"),
    ],
    ids=["KeyError", "IndexError", "ValueError", "OSError"],
)
@pytest.mark.asyncio
async def test_a_backend_failure_that_is_not_agent_execution_error_still_falls_back(exc):
    """I4. `render_description` used to catch AgentExecutionError alone, so
    any other backend failure escaped and cli_curate's guard turned a run
    whose FOUR stages all passed into INCONCLUSIVE - the LLM deciding the
    verdict, which this design forbids outright.

    These four are reachable, not hypothetical:
      - openai_compatible.py did `response.json()["choices"][0]["message"]`
        unguarded -> ValueError (json decode) / KeyError / IndexError;
      - `os.environ[api_key_env]` with the token variable unset -> KeyError;
      - a low-level socket failure httpx does not wrap -> OSError.
    (Both backends now normalise their own errors to AgentExecutionError as
    well - this fallback is the second line of defence, since a THIRD
    backend can be added without that discipline.)

    Mutation this catches: narrowing DESCRIPTION_FALLBACK_ERRORS back to
    `(AgentExecutionError,)` (observed: each parametrised case fails with the
    raw exception propagating out of render_description)."""
    facts = {"topology_id": "t1", "block_path": "B", "addresses": ["gain"]}
    with patch("analogcoder.agents.curator.run_agent", new=AsyncMock(side_effect=exc)):
        text, source = await render_description(facts, backend=object())

    assert source == "template"
    assert "t1" in text and "gain" in text


@pytest.mark.asyncio
async def test_the_fallback_records_why_it_fell_back(caplog):
    """A silent fallback makes "the LLM answered" and "the LLM died and we
    templated over it" indistinguishable in a run log - this repo's recurring
    failure shape. `description_source` in the artifacts says WHICH path ran;
    this log line says WHY.

    Mutation this catches: deleting the logger.warning call from the except
    block (observed: caplog.text is empty and both assertions fail)."""
    import logging

    with caplog.at_level(logging.WARNING, logger="analogcoder.agents.curator"):
        with patch(
            "analogcoder.agents.curator.run_agent",
            new=AsyncMock(side_effect=KeyError("choices")),
        ):
            await render_description({"topology_id": "t1"}, backend=object())

    assert "KeyError" in caplog.text
    assert "template" in caplog.text
