from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.backends.claude_sdk import ClaudeSDKBackend
from analogcoder.agents.backends.openai_compatible import OpenAICompatibleBackend
from analogcoder.cli import _build_agent_backend, _run, build_arg_parser

SPEC_YAML = (
    "circuit_name: test\n"
    "testbenches:\n"
    "  - name: ac_loop_gain\n"
    "    netlist: netlist.cir\n"
    "    analyses: [\"ac\"]\n"
    "    control_block: |\n"
    "      .control\n"
    "      .endc\n"
    "    criteria: []\n"
)


def test_arg_parser_requires_spec_only():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml"])
    assert args.spec == "s.yaml"
    assert args.simulator == "ngspice"
    assert args.agent_backend == "claude"
    assert not hasattr(args, "netlist")


def test_build_agent_backend_returns_claude_backend_by_default():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml"])
    backend = _build_agent_backend(args)
    assert isinstance(backend, ClaudeSDKBackend)


def test_build_agent_backend_returns_openai_compatible_backend_when_configured():
    parser = build_arg_parser()
    args = parser.parse_args(
        [
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
    args = parser.parse_args(["--spec", "s.yaml", "--agent-backend", "openai-compatible"])
    with pytest.raises(ValueError):
        _build_agent_backend(args)


@pytest.mark.asyncio
async def test_run_wires_orchestration_and_returns_its_result(tmp_path):
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(SPEC_YAML)

    fake_result = {
        "status": "PASS",
        "final_netlist_paths": {"ac_loop_gain": str(tmp_path / "runs" / "r1" / "netlist_v0_ac_loop_gain.cir")},
        "run_dir": str(tmp_path / "runs" / "r1"),
        "iterations_used": 1,
        "final_criteria": [],
    }

    parser = build_arg_parser()
    args = parser.parse_args(
        ["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r1")]
    )

    with patch("analogcoder.cli.run_orchestration", new=AsyncMock(return_value=fake_result)):
        result = await _run(args)

    assert result == fake_result


@pytest.mark.asyncio
async def test_run_passes_one_netlist_text_per_testbench_to_run_orchestration(tmp_path):
    (tmp_path / "netlist.cir").write_text("* ac netlist\n.end\n")
    (tmp_path / "netlist_psr_plus.cir").write_text("* psr netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        "circuit_name: test\n"
        "testbenches:\n"
        "  - name: ac_loop_gain\n"
        "    netlist: netlist.cir\n"
        "    analyses: [\"ac\"]\n"
        "    control_block: \".control\\n.endc\\n\"\n"
        "    criteria: []\n"
        "  - name: psr_plus\n"
        "    netlist: netlist_psr_plus.cir\n"
        "    analyses: [\"ac\"]\n"
        "    control_block: \".control\\n.endc\\n\"\n"
        "    criteria: []\n"
    )

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r2")])

    captured = {}

    async def fake_run_orchestration(initial_netlist_texts, spec, state, agents):
        captured["texts"] = initial_netlist_texts
        return {
            "status": "PASS",
            "final_netlist_paths": {},
            "run_dir": str(tmp_path / "runs" / "r2"),
            "iterations_used": 1,
            "final_criteria": [],
        }

    with patch("analogcoder.cli.run_orchestration", new=fake_run_orchestration):
        await _run(args)

    assert captured["texts"] == {"ac_loop_gain": "* ac netlist\n.end\n", "psr_plus": "* psr netlist\n.end\n"}
