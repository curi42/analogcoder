from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.cli import _run, build_arg_parser


def test_arg_parser_requires_netlist_and_spec():
    parser = build_arg_parser()
    args = parser.parse_args(["--netlist", "n.cir", "--spec", "s.yaml"])
    assert args.netlist == "n.cir"
    assert args.spec == "s.yaml"
    assert args.simulator == "ngspice"


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
