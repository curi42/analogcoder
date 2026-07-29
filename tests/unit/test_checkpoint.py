import json
import os
from types import SimpleNamespace

import pytest

from analogcoder.attempt_log import Attempt
from analogcoder.checkpoint import (
    BOUNDARY_ATTEMPT,
    BOUNDARY_OPTIMIZATION,
    BOUNDARY_OUTER_ITERATION,
    CHECKPOINT_FILENAME,
    CHECKPOINT_SCHEMA_VERSION,
    Checkpoint,
    CheckpointRejected,
    LoopProgress,
    build_checkpoint,
    from_payload,
    load_checkpoint,
    read_payload,
    restore_state,
    to_payload,
    write_checkpoint,
)
from analogcoder.corner_selection import NOMINAL, CornerSet
from analogcoder.pvt import CornerPoint
from analogcoder.state import RunState


def make_spec(tmp_path, *, netlist_text="* deck\n.end\n", names=("ac_loop_gain",)):
    tmp_path.mkdir(parents=True, exist_ok=True)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text("circuit_name: fake\n")
    testbenches = []
    for name in names:
        netlist_path = tmp_path / f"{name}.cir"
        netlist_path.write_text(netlist_text)
        testbenches.append(SimpleNamespace(name=name, netlist_path=str(netlist_path)))
    return str(spec_path), SimpleNamespace(testbenches=testbenches, canonical=testbenches[0])


def make_progress(**overrides):
    defaults = dict(
        outer_iter=3,
        entry_netlist_paths={"ac_loop_gain": "/runs/r1/netlist_v0_ac_loop_gain.cir"},
        tried_topologies={("AMP", "miller_basic"), ("AMP2", "miller_nulling_resistor")},
        consecutive_rollbacks=2,
        tuning_history=[
            Attempt(
                outer_iter=1,
                retry=1,
                refdes="Rf",
                param="value",
                old_value="10k",
                new_value="11k",
                outcome="kept",
                deltas=(("gain", 1.5), ("pm", -0.25)),
                regressed=("pm",),
            ),
            Attempt(
                outer_iter=2,
                retry=1,
                refdes="Rf",
                param="value",
                old_value="11k",
                new_value="12k",
                outcome="rejected",
                reason="area",
                detail="Rf grows 3.0x",
            ),
        ],
        topology_swaps=[{"outer_iter": 2, "block_path": "AMP", "topology_id": "miller_basic", "outcome": "kept"}],
        judge_result={"overall_pass": False, "criteria": [{"name": "gain", "actual": 18.0, "pass": False}]},
    )
    defaults.update(overrides)
    return LoopProgress(**defaults)


def make_run_dir(tmp_path, names=("ac_loop_gain",), versions=2):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    netlist_versions = {}
    for name in names:
        paths = []
        for v in range(versions):
            path = run_dir / f"netlist_v{v}_{name}.cir"
            path.write_text(f"* v{v}\n.end\n")
            paths.append(str(path))
        netlist_versions[name] = paths
    return str(run_dir), netlist_versions


def make_checkpoint(tmp_path, spec_path, spec, **overrides):
    run_dir, netlist_versions = make_run_dir(tmp_path, [tb.name for tb in spec.testbenches])
    progress = overrides.pop("progress", make_progress(
        entry_netlist_paths={tb.name: netlist_versions[tb.name][0] for tb in spec.testbenches}
    ))
    defaults = dict(
        boundary=BOUNDARY_OUTER_ITERATION,
        spec_path=spec_path,
        spec=spec,
        netlist_versions=netlist_versions,
        history_lines=42,
        attempt=0,
        all_topology_swaps=[],
        corner_set=None,
        progress=progress,
        orchestration_result=None,
    )
    defaults.update(overrides)
    return run_dir, build_checkpoint(**defaults)


# ---------------------------------------------------------------- 직렬화 왕복


def test_a_checkpoint_round_trips_through_json(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    _, cp = make_checkpoint(tmp_path, spec_path, spec)

    back = from_payload(json.loads(json.dumps(to_payload(cp))))

    assert back == cp


def test_tried_topologies_round_trips_as_a_set_of_pairs(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    _, cp = make_checkpoint(tmp_path, spec_path, spec)

    payload = json.loads(json.dumps(to_payload(cp)))

    # JSON에는 리스트의 리스트로 - set도 tuple도 JSON에 없다.
    assert sorted(payload["progress"]["tried_topologies"]) == [
        ["AMP", "miller_basic"],
        ["AMP2", "miller_nulling_resistor"],
    ]
    assert from_payload(payload).progress.tried_topologies == {
        ("AMP", "miller_basic"),
        ("AMP2", "miller_nulling_resistor"),
    }


def test_the_tuning_history_round_trips_including_deltas_and_reasons(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    _, cp = make_checkpoint(tmp_path, spec_path, spec)

    back = from_payload(json.loads(json.dumps(to_payload(cp))))

    assert back.progress.tuning_history == cp.progress.tuning_history
    assert back.progress.tuning_history[0].deltas == (("gain", 1.5), ("pm", -0.25))
    assert back.progress.tuning_history[0].regressed == ("pm",)
    assert back.progress.tuning_history[1].reason == "area"


def test_a_corner_set_round_trips_with_nominal_first(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    corner_set = CornerSet(
        corners=(NOMINAL, CornerPoint("ff", 1.98, 27.0), CornerPoint("ss", 1.62, 27.0)),
        probe_order=(CornerPoint("tt", 1.8, 27.0),),
        probe_index=1,
    )
    _, cp = make_checkpoint(tmp_path, spec_path, spec, corner_set=corner_set)

    back = from_payload(json.loads(json.dumps(to_payload(cp))))

    assert back.corner_set == corner_set
    assert back.corner_set.corners[0] is NOMINAL


def test_a_corner_set_payload_that_breaks_an_invariant_is_refused_on_load(tmp_path):
    """CornerSet.__post_init__을 통과시켜 되살린다 - 역직렬화가 불변식을 우회하는
    뒷문이 되면 next_probe가 이미 선택된 코너를 또 고른다."""
    spec_path, spec = make_spec(tmp_path)
    _, cp = make_checkpoint(tmp_path, spec_path, spec)
    payload = to_payload(cp)
    payload["corner_set"] = {
        "corners": [None, {"process": "ff", "voltage": 1.98, "temperature": 27.0}],
        "probe_order": [{"process": "ff", "voltage": 1.98, "temperature": 27.0}],
        "probe_index": 0,
    }

    with pytest.raises(ValueError):
        from_payload(payload)


def test_an_optimization_boundary_carries_the_orchestration_result(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    result = {"status": "PASS", "iterations_used": 4, "final_criteria": [], "topology_swaps": []}
    _, cp = make_checkpoint(
        tmp_path, spec_path, spec, boundary=BOUNDARY_OPTIMIZATION, progress=None, orchestration_result=result
    )

    back = from_payload(json.loads(json.dumps(to_payload(cp))))

    assert back.boundary == BOUNDARY_OPTIMIZATION
    assert back.progress is None
    assert back.orchestration_result == result


# ---------------------------------------------------------------- 원자적 쓰기


def test_write_then_read_returns_the_payload(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    run_dir, cp = make_checkpoint(tmp_path, spec_path, spec)

    path = write_checkpoint(run_dir, cp)

    assert path == os.path.join(run_dir, CHECKPOINT_FILENAME)
    assert read_payload(run_dir) == to_payload(cp)


def test_read_payload_of_a_run_dir_with_no_checkpoint_is_none(tmp_path):
    run_dir, _ = make_run_dir(tmp_path)

    assert read_payload(run_dir) is None


def test_a_write_that_dies_midway_leaves_the_previous_checkpoint_intact(tmp_path, monkeypatch):
    """판정 규칙이 정확히 "임의 지점에서 강제 종료"다. 체크포인트를 직접
    덮어쓰면 찢어진 JSON이 남고, 그러면 재개가 크래시하거나 - 더 나쁘게 -
    부분적으로 읽힌다."""
    spec_path, spec = make_spec(tmp_path)
    run_dir, first = make_checkpoint(tmp_path, spec_path, spec)
    write_checkpoint(run_dir, first)

    second = from_payload(to_payload(first))
    second.history_lines = 999

    import analogcoder.checkpoint as checkpoint_module

    def dump_then_die(obj, fp, **kwargs):
        fp.write('{"schema_version": 1, "boundar')
        raise OSError("disk full")

    monkeypatch.setattr(checkpoint_module.json, "dump", dump_then_die)
    with pytest.raises(OSError):
        write_checkpoint(run_dir, second)
    monkeypatch.undo()

    assert read_payload(run_dir)["history_lines"] == 42


def test_a_replace_that_never_happens_leaves_the_previous_checkpoint_intact(tmp_path, monkeypatch):
    spec_path, spec = make_spec(tmp_path)
    run_dir, first = make_checkpoint(tmp_path, spec_path, spec)
    write_checkpoint(run_dir, first)

    second = from_payload(to_payload(first))
    second.history_lines = 999

    import analogcoder.checkpoint as checkpoint_module

    monkeypatch.setattr(
        checkpoint_module.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    )
    with pytest.raises(OSError):
        write_checkpoint(run_dir, second)
    monkeypatch.undo()

    assert read_payload(run_dir)["history_lines"] == 42


def test_the_checkpoint_file_is_overwritten_not_accumulated(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    run_dir, cp = make_checkpoint(tmp_path, spec_path, spec)

    write_checkpoint(run_dir, cp)
    cp.history_lines = 77
    write_checkpoint(run_dir, cp)

    assert read_payload(run_dir)["history_lines"] == 77
    assert [n for n in os.listdir(run_dir) if n.startswith("checkpoint")] == [CHECKPOINT_FILENAME]


# ---------------------------------------------------------------- 재개 거부


def test_no_checkpoint_is_refused_with_a_reason(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    run_dir, _ = make_run_dir(tmp_path)

    with pytest.raises(CheckpointRejected) as exc:
        load_checkpoint(run_dir, spec_path, spec)

    assert "checkpoint.json" in str(exc.value)


def test_a_different_schema_version_is_refused(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    run_dir, cp = make_checkpoint(tmp_path, spec_path, spec)
    write_checkpoint(run_dir, cp)
    payload = read_payload(run_dir)
    payload["schema_version"] = CHECKPOINT_SCHEMA_VERSION + 1
    with open(os.path.join(run_dir, CHECKPOINT_FILENAME), "w") as f:
        json.dump(payload, f)

    with pytest.raises(CheckpointRejected) as exc:
        load_checkpoint(run_dir, spec_path, spec)

    assert "schema version" in str(exc.value)


def test_a_changed_spec_file_is_refused(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    run_dir, cp = make_checkpoint(tmp_path, spec_path, spec)
    write_checkpoint(run_dir, cp)

    with open(spec_path, "a") as f:
        f.write("# 기준을 하나 조였다\n")

    with pytest.raises(CheckpointRejected) as exc:
        load_checkpoint(run_dir, spec_path, spec)

    assert "spec" in str(exc.value)


def test_a_changed_netlist_file_is_refused(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    run_dir, cp = make_checkpoint(tmp_path, spec_path, spec)
    write_checkpoint(run_dir, cp)

    with open(spec.testbenches[0].netlist_path, "a") as f:
        f.write("* 다른 회로\n")

    with pytest.raises(CheckpointRejected) as exc:
        load_checkpoint(run_dir, spec_path, spec)

    assert "netlist" in str(exc.value)


def test_different_testbench_names_are_refused(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    run_dir, cp = make_checkpoint(tmp_path, spec_path, spec)
    write_checkpoint(run_dir, cp)

    other_spec_path, other_spec = make_spec(tmp_path / "other", names=("ac_loop_gain", "psr_plus"))
    # spec 파일 내용은 같게 둔다 - 여기서 걸려야 하는 것은 테스트벤치 이름이다.
    with pytest.raises(CheckpointRejected) as exc:
        load_checkpoint(run_dir, other_spec_path, other_spec)

    assert "testbench" in str(exc.value)


def test_a_missing_netlist_version_file_is_refused(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    run_dir, cp = make_checkpoint(tmp_path, spec_path, spec)
    write_checkpoint(run_dir, cp)

    os.remove(cp.netlist_versions["ac_loop_gain"][-1])

    with pytest.raises(CheckpointRejected) as exc:
        load_checkpoint(run_dir, spec_path, spec)

    assert "netlist_v1_ac_loop_gain.cir" in str(exc.value)


def test_a_missing_entry_netlist_file_is_refused(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    run_dir, cp = make_checkpoint(tmp_path, spec_path, spec)
    write_checkpoint(run_dir, cp)

    os.remove(cp.progress.entry_netlist_paths["ac_loop_gain"])

    with pytest.raises(CheckpointRejected) as exc:
        load_checkpoint(run_dir, spec_path, spec)

    assert "netlist_v0_ac_loop_gain.cir" in str(exc.value)


def test_a_valid_checkpoint_loads(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    run_dir, cp = make_checkpoint(tmp_path, spec_path, spec)
    write_checkpoint(run_dir, cp)

    assert load_checkpoint(run_dir, spec_path, spec) == cp


# ---------------------------------------------------------------- 상태 복원


def test_restore_state_puts_the_version_stack_back(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    run_dir, cp = make_checkpoint(tmp_path, spec_path, spec)
    state = RunState(run_dir=run_dir, testbench_names=["ac_loop_gain"])

    restore_state(state, cp)

    assert state.netlist_versions == cp.netlist_versions
    assert state.current_netlist_texts() == {"ac_loop_gain": "* v1\n.end\n"}


def test_restore_state_does_not_alias_the_checkpoint_lists(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    run_dir, cp = make_checkpoint(tmp_path, spec_path, spec)
    state = RunState(run_dir=run_dir, testbench_names=["ac_loop_gain"])

    restore_state(state, cp)
    state.netlist_versions["ac_loop_gain"].append("/somewhere/else.cir")

    assert len(cp.netlist_versions["ac_loop_gain"]) == 2


def test_the_attempt_boundary_needs_neither_progress_nor_result(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    run_dir, cp = make_checkpoint(
        tmp_path, spec_path, spec, boundary=BOUNDARY_ATTEMPT, progress=None, attempt=1
    )
    write_checkpoint(run_dir, cp)

    back = load_checkpoint(run_dir, spec_path, spec)

    assert (back.boundary, back.attempt, back.progress) == (BOUNDARY_ATTEMPT, 1, None)


def test_an_unknown_boundary_is_refused(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    run_dir, cp = make_checkpoint(tmp_path, spec_path, spec)
    write_checkpoint(run_dir, cp)
    payload = read_payload(run_dir)
    payload["boundary"] = "mid_iteration"
    with open(os.path.join(run_dir, CHECKPOINT_FILENAME), "w") as f:
        json.dump(payload, f)

    with pytest.raises(CheckpointRejected) as exc:
        load_checkpoint(run_dir, spec_path, spec)

    assert "boundary" in str(exc.value)


def test_checkpoint_is_a_plain_dataclass_so_equality_is_by_value(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    _, cp = make_checkpoint(tmp_path, spec_path, spec)

    assert isinstance(cp, Checkpoint)
    assert cp == from_payload(to_payload(cp))
