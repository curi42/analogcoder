import json
import os

import pytest

from analogcoder.state import RunState


def test_push_netlist_version_writes_versioned_files(tmp_path):
    state = RunState(run_dir=str(tmp_path))

    v0_path = state.push_netlist_version("* v0\n.end\n")
    v1_path = state.push_netlist_version("* v1\n.end\n")

    assert os.path.basename(v0_path) == "netlist_v0.cir"
    assert os.path.basename(v1_path) == "netlist_v1.cir"
    assert state.current_netlist_path() == v1_path
    with open(v1_path) as f:
        assert f.read() == "* v1\n.end\n"


def test_rollback_returns_to_previous_version(tmp_path):
    state = RunState(run_dir=str(tmp_path))
    v0_path = state.push_netlist_version("* v0\n.end\n")
    state.push_netlist_version("* v1\n.end\n")

    restored_path = state.rollback()

    assert restored_path == v0_path
    assert state.current_netlist_path() == v0_path


def test_rollback_raises_when_no_previous_version(tmp_path):
    state = RunState(run_dir=str(tmp_path))
    state.push_netlist_version("* v0\n.end\n")

    with pytest.raises(ValueError):
        state.rollback()


def test_log_event_appends_jsonl(tmp_path):
    state = RunState(run_dir=str(tmp_path))
    state.log_event("judge", {"overall_pass": False})
    state.log_event("judge", {"overall_pass": True})

    with open(state.history_path) as f:
        lines = [json.loads(line) for line in f]

    assert lines[0] == {"step": "judge", "overall_pass": False}
    assert lines[1] == {"step": "judge", "overall_pass": True}
