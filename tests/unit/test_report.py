import json
import os

from analogcoder.report import write_report_md, write_result_json

SAMPLE_RESULT = {
    "status": "PASS",
    "final_netlist_paths": {
        "ac_loop_gain": "runs/abc123/netlist_v1_ac_loop_gain.cir",
        "psr_plus": "runs/abc123/netlist_v1_psr_plus.cir",
    },
    "run_dir": "runs/abc123",
    "iterations_used": 2,
    "final_criteria": [{"name": "gain", "target": ">=19.5", "actual": 20.0, "pass": True, "margin": 0.5}],
}

SAMPLE_FAIL_RESULT = {
    "status": "FAIL",
    "final_netlist_paths": {"ac_loop_gain": "runs/abc123/netlist_v3_ac_loop_gain.cir"},
    "run_dir": "runs/abc123",
    "iterations_used": 10,
    "final_criteria": [{"name": "gain", "target": ">=19.5", "actual": 15.0, "pass": False, "margin": -4.5}],
    "failure_reason": "max iterations reached",
}


def test_write_result_json(tmp_path):
    path = write_result_json(str(tmp_path), SAMPLE_RESULT)
    assert os.path.basename(path) == "result.json"
    with open(path) as f:
        assert json.load(f) == SAMPLE_RESULT


def test_write_report_md_includes_status_criteria_and_every_testbench_netlist(tmp_path):
    path = write_report_md(str(tmp_path), SAMPLE_RESULT)
    with open(path) as f:
        content = f.read()
    assert "PASS" in content
    assert "gain" in content
    assert "[PASS] gain" in content
    assert "ac_loop_gain" in content
    assert "netlist_v1_ac_loop_gain.cir" in content
    assert "psr_plus" in content
    assert "netlist_v1_psr_plus.cir" in content


def test_write_report_md_includes_failure_reason(tmp_path):
    path = write_report_md(str(tmp_path), SAMPLE_FAIL_RESULT)
    with open(path) as f:
        content = f.read()
    assert "max iterations reached" in content
    assert "[FAIL] gain" in content
