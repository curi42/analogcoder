import json
import math
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
        corners=(NOMINAL, CornerPoint(process="ff", voltage=1.98, temperature=27.0), CornerPoint(process="ss", voltage=1.62, temperature=27.0)),
        probe_order=(CornerPoint(process="tt", voltage=1.8, temperature=27.0),),
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


# ------------------------------------------------- 비유한 값의 전송 형식


def _strict_json_loads(text):
    """bare `NaN`/`Infinity` 를 거부하는 파서. node 의 `JSON.parse` 와 같은
    엄격도이며, `json.loads` 의 기본값은 그것들을 **받아 준다**."""

    def reject(token):
        raise ValueError(f"RFC 8259 가 아닌 토큰: {token}")

    return json.loads(text, parse_constant=reject)


def _nan_judge():
    """`judge_tools.evaluate_criteria` 가 측정이 없는 기준에 싣는 모양.
    예외 경로가 아니라 정상 경로다."""
    return {
        "overall_pass": False,
        "criteria": [
            {"name": "ugbw", "pass": False, "actual": math.nan, "margin": math.nan},
            {"name": "gain", "pass": True, "actual": 71.09, "margin": 11.09},
        ],
    }


def test_a_checkpoint_carrying_an_unmeasured_criterion_is_valid_json(tmp_path):
    """`result.json` 과 `history.jsonl` 이 이미 값을 치른 결함이 체크포인트에
    남아 있었다. bare `NaN` 은 node 가 파일 **전체**를 거부하고, jq 1.7.1 은
    거부하지 않고 `null` 로 바꿔 준다."""
    spec_path, spec = make_spec(tmp_path)
    run_dir, cp = make_checkpoint(
        tmp_path, spec_path, spec, progress=make_progress(judge_result=_nan_judge())
    )

    raw = open(write_checkpoint(run_dir, cp)).read()

    _strict_json_loads(raw)  # 던지면 실패


def test_an_unmeasured_criterion_resumes_as_a_float_not_a_marker_string(tmp_path):
    """표지는 **전송 형식이지 값이 아니다.** 체크포인트는 `result.json` 과 달리
    다시 **읽혀서 도는 런에 들어간다** - `judge_result` 는 재개한 루프가
    그대로 쓰는 값이고, `attempt_log.deltas_between` 은 judge 값을 **뺀다**.
    표지 문자열이 그대로 돌아오면 그 뺄셈이 `TypeError` 다."""
    spec_path, spec = make_spec(tmp_path)
    run_dir, cp = make_checkpoint(
        tmp_path, spec_path, spec, progress=make_progress(judge_result=_nan_judge())
    )

    write_checkpoint(run_dir, cp)
    back = from_payload(read_payload(run_dir))

    actual = back.progress.judge_result["criteria"][0]["actual"]
    assert isinstance(actual, float) and math.isnan(actual)
    assert back.progress.judge_result["criteria"][1]["actual"] == 71.09


def test_the_wire_format_keeps_unmeasured_distinct_from_no_value(tmp_path):
    """이 저장소가 여러 번 값을 치른 구별이다. `null` 은 "그 필드에 값이 없다",
    `NaN` 은 "쟀는데 값이 안 나왔다" - 다른 사실이고, jq 는 둘을 같은 토큰으로
    만들었다."""
    spec_path, spec = make_spec(tmp_path)
    judge = _nan_judge()
    judge["criteria"][1]["actual"] = None  # 값이 아예 없는 필드
    run_dir, cp = make_checkpoint(
        tmp_path, spec_path, spec, progress=make_progress(judge_result=judge)
    )

    payload = json.loads(open(write_checkpoint(run_dir, cp)).read())

    criteria = payload["progress"]["judge_result"]["criteria"]
    assert criteria[0]["actual"] == "NaN"
    assert criteria[1]["actual"] is None

    back = from_payload(read_payload(run_dir))
    restored = back.progress.judge_result["criteria"]
    assert math.isnan(restored[0]["actual"])
    assert restored[1]["actual"] is None


def test_the_orchestration_result_takes_the_same_wire_format(tmp_path):
    """최적화 경계의 체크포인트는 `final_criteria` 를 실어 나르는데, 그것이
    바로 `evaluate_criteria` 의 출력이다 - `judge_result` 와 같은 자리에서
    같은 `NaN` 이 나온다."""
    spec_path, spec = make_spec(tmp_path)
    result = {"status": "FAIL", "iterations_used": 4, "final_criteria": _nan_judge()["criteria"]}
    run_dir, cp = make_checkpoint(
        tmp_path, spec_path, spec, boundary=BOUNDARY_OPTIMIZATION, progress=None,
        orchestration_result=result,
    )

    raw = open(write_checkpoint(run_dir, cp)).read()
    _strict_json_loads(raw)

    back = from_payload(read_payload(run_dir))
    assert math.isnan(back.orchestration_result["final_criteria"][0]["actual"])


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


# ---------------------------------------------------------- 시뮬레이터 정체성


def test_resuming_with_a_different_simulator_is_refused(tmp_path):
    """`cache.simulation_key` 는 `identity()` 를 **네 번째 결정 요인**으로 이미
    쓴다 - 근거는 "시뮬레이터가 다르면 다른 값이 나올 수 있으므로 키에서
    빠지면 캐시가 다른 엔진의 측정값을 이 엔진의 값으로 돌려준다" 이다.
    바로 위 층에는 그 결정 요인이 없었는데, 재개는 **진입 코너 스윕을 통째로
    재사용**하고(`cli._reused_baseline_sweep`) 그 값이 `corner_allowances` 와
    `seed_from_sweep` 으로 흘러간다. 즉 두 엔진의 측정이 한 결과에 섞인다 -
    스펙 해시로 재개를 거부하는 것, `push_netlist_version` 을 원자적으로 만든
    것과 정확히 같은 논리다."""
    spec_path, spec = make_spec(tmp_path)
    run_dir, cp = make_checkpoint(tmp_path, spec_path, spec, simulator_identity="ngspice|/usr/bin/ngspice|43")
    write_checkpoint(run_dir, cp)

    with pytest.raises(CheckpointRejected) as exc:
        load_checkpoint(run_dir, spec_path, spec, simulator_identity="hspice|/tools/hspice|X-2024")

    assert "시뮬레이터" in str(exc.value)
    assert "ngspice|/usr/bin/ngspice|43" in str(exc.value)
    assert "hspice|/tools/hspice|X-2024" in str(exc.value)


def test_resuming_with_the_same_simulator_is_allowed(tmp_path):
    spec_path, spec = make_spec(tmp_path)
    ident = "ngspice|/usr/bin/ngspice|43"
    run_dir, cp = make_checkpoint(tmp_path, spec_path, spec, simulator_identity=ident)
    write_checkpoint(run_dir, cp)

    assert load_checkpoint(run_dir, spec_path, spec, simulator_identity=ident) == cp


def test_the_simulator_identity_round_trips_and_defaults_to_unrecorded(tmp_path):
    from analogcoder.checkpoint import SIMULATOR_UNRECORDED, simulator_identity_state

    spec_path, spec = make_spec(tmp_path)
    run_dir, cp = make_checkpoint(tmp_path, spec_path, spec)
    write_checkpoint(run_dir, cp)
    payload = read_payload(run_dir)

    assert cp.simulator_identity is None
    # 키는 **항상** 있다. `null` 과 "필드가 통째로 없다" 가 같아지면
    # "기록 안 함" 과 "검사가 사라졌다" 를 구별할 수 없다.
    assert "simulator_identity" in payload
    assert payload["simulator_identity"] is None
    assert from_payload(payload) == cp
    assert simulator_identity_state(payload, "ngspice|x") == SIMULATOR_UNRECORDED


def test_the_four_simulator_identity_states_are_distinguishable(tmp_path):
    """**이 게이트가 아무것도 안 할 때가 로그에서 보여야 한다.**

    오늘 `cli.py` 는 정체성을 넘기지 않으므로(다른 에이전트 소유, 배선은
    후속 작업) 프로덕션 상태는 `unrecorded` 다. 그 사실이 이름을 가진 값으로
    나오지 않으면 "엔진이 같아서 통과" 와 "검사가 인자 없이 불려서 통과" 가
    구별 불가가 된다 - 이 저장소가 아홉 번 값을 치른 모양이다."""
    from analogcoder.checkpoint import (
        SIMULATOR_MATCH,
        SIMULATOR_MISMATCH,
        SIMULATOR_UNRECORDED,
        SIMULATOR_UNSUPPLIED,
        simulator_identity_state,
    )

    assert simulator_identity_state({"simulator_identity": "a"}, "a") == SIMULATOR_MATCH
    assert simulator_identity_state({"simulator_identity": "a"}, "b") == SIMULATOR_MISMATCH
    assert simulator_identity_state({"simulator_identity": None}, "a") == SIMULATOR_UNRECORDED
    assert simulator_identity_state({"simulator_identity": "a"}, None) == SIMULATOR_UNSUPPLIED
    assert simulator_identity_state({}, None) == SIMULATOR_UNRECORDED


def test_an_unrecorded_or_unsupplied_identity_does_not_reject(tmp_path):
    """거부는 **불일치일 때만**이다. 기록이 없는데 거부하면 배선 전에 쓰인
    체크포인트가 전부 재개 불가가 되고, 그것은 이 기능이 막으려는 것(버려진
    실행 시간)을 스스로 만든다."""
    spec_path, spec = make_spec(tmp_path)
    run_dir, cp = make_checkpoint(tmp_path, spec_path, spec)
    write_checkpoint(run_dir, cp)

    assert load_checkpoint(run_dir, spec_path, spec, simulator_identity="anything") == cp
    assert load_checkpoint(run_dir, spec_path, spec) == cp
