from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.backends.claude_sdk import ClaudeSDKBackend
from analogcoder.agents.backends.openai_compatible import OpenAICompatibleBackend
from analogcoder.cli import _build_agent_backend, _build_agent_backends, _run, build_arg_parser

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


@pytest.mark.asyncio
async def test_run_skips_pvt_sweep_when_spec_has_no_pvt_corners(tmp_path):
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(SPEC_YAML)

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r3")])

    fake_result = {
        "status": "PASS", "final_netlist_paths": {}, "run_dir": str(tmp_path / "runs" / "r3"),
        "iterations_used": 1, "final_criteria": [],
    }

    with (
        patch("analogcoder.cli.run_orchestration", new=AsyncMock(return_value=fake_result)),
        patch("analogcoder.cli.run_full_pvt_sweep") as mock_sweep,
    ):
        result = await _run(args)

    mock_sweep.assert_not_called()
    assert result["status"] == "PASS"
    assert "pvt_sweep" not in result


@pytest.mark.asyncio
async def test_run_overrides_pass_to_fail_when_final_pvt_sweep_fails(tmp_path):
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")
    spec_path = tmp_path / "spec_pvt.yaml"
    spec_path.write_text(
        "circuit_name: test\n"
        "pvt_corners:\n"
        "  process: [tt]\n"
        "  voltage: [1.8]\n"
        "  temperature: [27]\n"
        "testbenches:\n"
        "  - name: ac_loop_gain\n"
        "    netlist: netlist.cir\n"
        '    analyses: ["ac"]\n'
        '    control_block: ".control\\n.endc\\n"\n'
        "    criteria:\n"
        "      - name: gain\n"
        "        measurement: gain_db\n"
        '        operator: ">="\n'
        "        threshold: 10.0\n"
    )

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r4")])

    fake_result = {
        "status": "PASS", "final_netlist_paths": {"ac_loop_gain": str(tmp_path / "netlist.cir")},
        "run_dir": str(tmp_path / "runs" / "r4"), "iterations_used": 1, "final_criteria": [],
    }
    fake_final_sweep = {
        "overall_pass": False, "criteria": [], "summary": "one or more criteria failed",
        "worst_case_corners": {"gain": {"process": "tt", "voltage": 1.8, "temperature": 27, "value": 5.0}},
    }

    # run_orchestration is mocked out entirely, so it never calls
    # state.push_netlist_version - RunState.current_netlist_texts() then
    # naturally returns {} (no versions tracked), which is fine here since
    # run_full_pvt_sweep is also mocked and ignores its netlist_texts arg.
    with (
        patch("analogcoder.cli.run_orchestration", new=AsyncMock(return_value=fake_result)),
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=fake_final_sweep) as mock_sweep,
    ):
        result = await _run(args)

    assert mock_sweep.call_count == 2  # baseline sweep + final sweep
    assert result["status"] == "FAIL"
    assert "pvt_sweep" in result
    assert result["pvt_sweep"]["worst_case_corners"]["gain"]["process"] == "tt"


@pytest.mark.asyncio
async def test_run_keeps_pass_when_final_pvt_sweep_also_passes(tmp_path):
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")
    spec_path = tmp_path / "spec_pvt.yaml"
    spec_path.write_text(
        "circuit_name: test\n"
        "pvt_corners:\n"
        "  process: [tt]\n"
        "  voltage: [1.8]\n"
        "  temperature: [27]\n"
        "testbenches:\n"
        "  - name: ac_loop_gain\n"
        "    netlist: netlist.cir\n"
        '    analyses: ["ac"]\n'
        '    control_block: ".control\\n.endc\\n"\n'
        "    criteria:\n"
        "      - name: gain\n"
        "        measurement: gain_db\n"
        '        operator: ">="\n'
        "        threshold: 10.0\n"
    )

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r5")])

    fake_result = {
        "status": "PASS", "final_netlist_paths": {"ac_loop_gain": str(tmp_path / "netlist.cir")},
        "run_dir": str(tmp_path / "runs" / "r5"), "iterations_used": 1, "final_criteria": [],
    }
    fake_passing_sweep = {
        "overall_pass": True, "criteria": [], "summary": "all criteria passed",
        "worst_case_corners": {},
    }

    with (
        patch("analogcoder.cli.run_orchestration", new=AsyncMock(return_value=fake_result)),
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=fake_passing_sweep) as mock_sweep,
    ):
        result = await _run(args)

    assert mock_sweep.call_count == 2
    assert result["status"] == "PASS"
    assert result["pvt_sweep"]["overall_pass"] is True


@pytest.mark.asyncio
async def test_run_hands_orchestration_include_resolved_netlist_texts(tmp_path):
    # The orchestration loop simulates the netlist copies RunState stages into
    # the run dir, not the originals in the benchmark dir. A bare relative
    # `.include "pdk_corner.inc"` cannot resolve from there, so _run must
    # absolutize includes against the netlist's own directory before handing
    # the texts off - otherwise every simulation of a real PDK-based benchmark
    # fails with "could not find include file" and the whole loop runs on
    # empty measurements.
    (tmp_path / "pdk_corner.inc").write_text("* pdk\n")
    (tmp_path / "netlist.cir").write_text('* netlist\n.include "pdk_corner.inc"\n.end\n')
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(SPEC_YAML)

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r6")])

    fake_result = {
        "status": "PASS", "final_netlist_paths": {"ac_loop_gain": str(tmp_path / "netlist.cir")},
        "run_dir": str(tmp_path / "runs" / "r6"), "iterations_used": 1, "final_criteria": [],
    }

    with patch("analogcoder.cli.run_orchestration", new=AsyncMock(return_value=fake_result)) as mock_orch:
        await _run(args)

    passed_texts = mock_orch.await_args.args[0]
    assert f'.include "{tmp_path / "pdk_corner.inc"}"' in passed_texts["ac_loop_gain"]


def test_claude_backend_defaults_to_sonnet():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml"])

    backends = _build_agent_backends(args)

    assert set(backends) == {"simulator", "judge", "tuner", "verifier"}
    assert all(b.model == "sonnet" for b in backends.values())


def test_claude_model_flag_sets_every_agent_model():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml", "--claude-model", "haiku"])

    backends = _build_agent_backends(args)

    assert all(b.model == "haiku" for b in backends.values())


def test_agent_model_flag_overrides_a_single_agent():
    # Lets a run drop one agent to a weaker model to see whether the pipeline
    # still holds - the tool-calling agents (simulator, judge) are the ones a
    # lower-capability model has historically struggled with.
    parser = build_arg_parser()
    args = parser.parse_args(
        ["--spec", "s.yaml", "--claude-model", "sonnet", "--agent-model", "simulator=haiku"]
    )

    backends = _build_agent_backends(args)

    assert backends["simulator"].model == "haiku"
    assert backends["tuner"].model == "sonnet"


def test_agent_model_flag_rejects_an_unknown_agent_name():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml", "--agent-model", "nosuchagent=haiku"])

    with pytest.raises(ValueError, match="nosuchagent"):
        _build_agent_backends(args)
