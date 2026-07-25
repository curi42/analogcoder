import json
import os

import pytest

from analogcoder.state import RunState


def test_push_netlist_version_writes_one_file_per_testbench(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain", "psr_plus"])

    v0_paths = state.push_netlist_version({"ac_loop_gain": "* ac v0\n.end\n", "psr_plus": "* psr v0\n.end\n"})
    v1_paths = state.push_netlist_version({"ac_loop_gain": "* ac v1\n.end\n", "psr_plus": "* psr v1\n.end\n"})

    assert os.path.basename(v0_paths["ac_loop_gain"]) == "netlist_v0_ac_loop_gain.cir"
    assert os.path.basename(v0_paths["psr_plus"]) == "netlist_v0_psr_plus.cir"
    assert os.path.basename(v1_paths["ac_loop_gain"]) == "netlist_v1_ac_loop_gain.cir"
    assert state.current_netlist_paths() == v1_paths
    with open(v1_paths["psr_plus"]) as f:
        assert f.read() == "* psr v1\n.end\n"


def test_current_netlist_texts_reads_back_latest_version(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain", "psr_plus"])
    state.push_netlist_version({"ac_loop_gain": "* ac v0\n.end\n", "psr_plus": "* psr v0\n.end\n"})
    state.push_netlist_version({"ac_loop_gain": "* ac v1\n.end\n", "psr_plus": "* psr v1\n.end\n"})

    texts = state.current_netlist_texts()

    assert texts == {"ac_loop_gain": "* ac v1\n.end\n", "psr_plus": "* psr v1\n.end\n"}


def test_rollback_restores_every_testbench_together(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain", "psr_plus"])
    v0_paths = state.push_netlist_version({"ac_loop_gain": "* ac v0\n.end\n", "psr_plus": "* psr v0\n.end\n"})
    state.push_netlist_version({"ac_loop_gain": "* ac v1\n.end\n", "psr_plus": "* psr v1\n.end\n"})

    restored_paths = state.rollback()

    assert restored_paths == v0_paths
    assert state.current_netlist_paths() == v0_paths


def test_rollback_raises_when_no_previous_version(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])
    state.push_netlist_version({"ac_loop_gain": "* v0\n.end\n"})

    with pytest.raises(ValueError):
        state.rollback()


def test_log_event_appends_jsonl(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])
    state.log_event("judge", {"overall_pass": False})
    state.log_event("judge", {"overall_pass": True})

    with open(state.history_path) as f:
        lines = [json.loads(line) for line in f]

    assert lines[0] == {"step": "judge", "overall_pass": False}
    assert lines[1] == {"step": "judge", "overall_pass": True}
