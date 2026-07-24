from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.backends.claude_sdk import ClaudeSDKBackend
from analogcoder.agents.backends.openai_compatible import OpenAICompatibleBackend
from analogcoder.cli import _build_agent_backend, _run, build_arg_parser


def test_arg_parser_requires_netlist_and_spec():
    parser = build_arg_parser()
    args = parser.parse_args(["--netlist", "n.cir", "--spec", "s.yaml"])
    assert args.netlist == "n.cir"
    assert args.spec == "s.yaml"
    assert args.simulator == "ngspice"
    assert args.agent_backend == "claude"


def test_build_agent_backend_returns_claude_backend_by_default():
    parser = build_arg_parser()
    args = parser.parse_args(["--netlist", "n.cir", "--spec", "s.yaml"])
    backend = _build_agent_backend(args)
    assert isinstance(backend, ClaudeSDKBackend)


def test_build_agent_backend_returns_openai_compatible_backend_when_configured():
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "--netlist", "n.cir",
            "--spec", "s.yaml",
            "--agent-backend", "openai-compatible",
            "--llm-base-url", "http://local",
            "--llm-model", "glm-5.2",
        ]
    )
    backend = _build_agent_backend(args)
    assert isinstance(backend, OpenAICompatibleBackend)
    assert backend.base_url == "http://local"
    assert backend.model == "glm-5.2"
    assert backend.api_key_env == "LOCAL_LLM_API_KEY"


def test_build_agent_backend_raises_when_openai_compatible_missing_config():
    parser = build_arg_parser()
    args = parser.parse_args(
        ["--netlist", "n.cir", "--spec", "s.yaml", "--agent-backend", "openai-compatible"]
    )
    with pytest.raises(ValueError):
        _build_agent_backend(args)


@pytest.mark.asyncio
async def test_run_wires_orchestration_and_returns_its_result(tmp_path):
    netlist_path = tmp_path / "netlist.cir"
    netlist_path.write_text("* netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        "circuit_name: test\nanalyses: [\"ac\"]\ncontrol_block: |\n  .control\n  .endc\ncriteria: []\n"
    )

    fake_result = {
        "status": "PASS",
        "final_netlist_path": str(tmp_path / "runs" / "r1" / "netlist_v0.cir"),
        "iterations_used": 1,
        "final_criteria": [],
    }

    parser = build_arg_parser()
    args = parser.parse_args(
        ["--netlist", str(netlist_path), "--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r1")]
    )

    with patch("analogcoder.cli.run_orchestration", new=AsyncMock(return_value=fake_result)):
        result = await _run(args)

    assert result == fake_result
