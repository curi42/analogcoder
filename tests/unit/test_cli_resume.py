"""`--resume` 배선 - 경계 세 곳, 거부 경로, 그리고 "재개는 기본 동작이 아니다".

LLM도 ngspice도 부르지 않는다. `run_orchestration` 자체의 재개는
`test_orchestrator_resume.py`가 판정하고, 여기서 재는 것은 cli가 그 상태를
디스크에 원자적으로 남기고 되돌려 놓는가다.
"""

import json
import os
from unittest.mock import patch

import pytest

from analogcoder.checkpoint import (
    BOUNDARY_ATTEMPT,
    BOUNDARY_OPTIMIZATION,
    BOUNDARY_OUTER_ITERATION,
    CHECKPOINT_FILENAME,
    CheckpointRejected,
    LoopProgress,
    read_payload,
)
from analogcoder.cli import _run, build_arg_parser
from analogcoder.history import read_events

# 코너 축소 스펙과 스윕 모양 헬퍼는 test_cli.py에 이미 있다 - 같은 모양을 여기서
# 다시 만들면 두 파일이 "run_full_pvt_sweep이 내놓는 모양"에 대해 갈라진 정의를
# 갖게 된다.
from tests.unit.test_cli import (
    CORNER_REDUCTION_SPEC_YAML,
    _history_events,
    _judged_snapshot_build_corner_simulate,
    _orchestration_sequence,
    _pass_result,
    _sweep,
    _sweep_sequence,
    _wc,
)

SPEC_YAML = (
    "circuit_name: test\n"
    "testbenches:\n"
    "  - name: ac_loop_gain\n"
    "    netlist: netlist.cir\n"
    '    analyses: ["ac"]\n'
    '    control_block: ".control\\n.endc\\n"\n'
    "    criteria: []\n"
)

NETLIST = "* netlist\nRf vminus vout 10k\n.end\n"
TUNED = "* netlist\nRf vminus vout 11k\n.end\n"


class Boom(RuntimeError):
    pass


def make_args(tmp_path, *extra, spec_yaml=SPEC_YAML, netlist=NETLIST):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "netlist.cir").write_text(netlist)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(spec_yaml)
    run_dir = str(tmp_path / "runs" / "r1")
    parser = build_arg_parser()
    return parser.parse_args(["--spec", str(spec_path), "--run-dir", run_dir, *extra]), run_dir


def pass_result(run_dir):
    return {
        "status": "PASS",
        "final_netlist_paths": {},
        "run_dir": run_dir,
        "iterations_used": 2,
        "final_criteria": [],
        "topology_swaps": [],
    }


def orchestration(result, *, checkpoint_at=None, crash=False, captured=None):
    """run_orchestration 대역. 진짜와 같은 자리에서 버전을 밀고, 시키면 루프
    머리에서 save_checkpoint 를 부른 뒤 죽는다."""

    async def fake(initial_netlist_texts, spec, state, agents, resume=None, save_checkpoint=None):
        if captured is not None:
            captured.setdefault("calls", []).append(
                {"resume": resume, "initial": dict(initial_netlist_texts)}
            )
        if resume is None:
            state.push_netlist_version(initial_netlist_texts)
        if checkpoint_at is not None and save_checkpoint is not None:
            save_checkpoint(
                LoopProgress(
                    outer_iter=checkpoint_at,
                    entry_netlist_paths=state.current_netlist_paths(),
                    consecutive_rollbacks=1,
                )
            )
        if crash:
            state.log_event("tuning_proposal", {"outer_iter": checkpoint_at, "retry": 1})
            state.log_event("area_check", {"outer_iter": checkpoint_at, "retry": 1})
            raise Boom("죽었다")
        if resume is None:
            state.push_netlist_version({name: TUNED for name in initial_netlist_texts})
        return dict(result)

    return fake


# ---------------------------------------------------------------- 기본 동작


@pytest.mark.asyncio
async def test_a_normal_run_carries_resumed_from_null(tmp_path):
    """"재개 안 함"과 "필드가 사라짐"이 같은 모양이면 안 된다."""
    args, run_dir = make_args(tmp_path)

    with patch("analogcoder.cli.run_orchestration", new=orchestration(pass_result(run_dir))):
        result = await _run(args)

    assert "resumed_from" in result
    assert result["resumed_from"] is None


@pytest.mark.asyncio
async def test_a_normal_run_writes_a_checkpoint_at_the_optimization_boundary(tmp_path):
    args, run_dir = make_args(tmp_path)

    with patch("analogcoder.cli.run_orchestration", new=orchestration(pass_result(run_dir))):
        await _run(args)

    payload = read_payload(run_dir)
    assert payload["boundary"] == BOUNDARY_OPTIMIZATION
    assert payload["orchestration_result"]["status"] == "PASS"


@pytest.mark.asyncio
async def test_an_existing_run_dir_without_the_flag_starts_fresh(tmp_path):
    """**재개는 기본 동작이 아니다.** 플래그 없이 같은 run-dir 을 다시 가리키면
    오늘 그대로 처음부터 돈다 - 조용히 이어가면 절반짜리 실행이 온전한 실행처럼
    측정에 들어간다."""
    args, run_dir = make_args(tmp_path)
    with patch("analogcoder.cli.run_orchestration", new=orchestration(pass_result(run_dir))):
        await _run(args)

    captured: dict = {}
    with patch(
        "analogcoder.cli.run_orchestration",
        new=orchestration(pass_result(run_dir), captured=captured),
    ):
        result = await _run(args)

    assert captured["calls"][0]["resume"] is None
    assert captured["calls"][0]["initial"] == {"ac_loop_gain": NETLIST}
    assert result["resumed_from"] is None


# ---------------------------------------------------------------- 거부 경로


@pytest.mark.asyncio
async def test_resume_with_no_checkpoint_is_refused(tmp_path):
    args, _ = make_args(tmp_path, "--resume")

    with pytest.raises(CheckpointRejected) as exc:
        await _run(args)

    assert CHECKPOINT_FILENAME in str(exc.value)


@pytest.mark.asyncio
async def test_resume_after_the_spec_changed_is_refused(tmp_path):
    args, run_dir = make_args(tmp_path)
    with patch("analogcoder.cli.run_orchestration", new=orchestration(pass_result(run_dir))):
        await _run(args)

    with open(args.spec, "a") as f:
        f.write("# 기준을 하나 조였다\n")
    resume_args, _ = make_args(tmp_path, "--resume", spec_yaml=open(args.spec).read())

    with pytest.raises(CheckpointRejected) as exc:
        await _run(resume_args)

    assert "spec" in str(exc.value)


@pytest.mark.asyncio
async def test_resume_after_the_netlist_changed_is_refused(tmp_path):
    args, run_dir = make_args(tmp_path)
    with patch("analogcoder.cli.run_orchestration", new=orchestration(pass_result(run_dir))):
        await _run(args)

    (tmp_path / "netlist.cir").write_text("* 다른 회로\nRf vminus vout 99k\n.end\n")
    parser = build_arg_parser()
    resume_args = parser.parse_args(
        ["--spec", str(tmp_path / "spec.yaml"), "--run-dir", run_dir, "--resume"]
    )

    with pytest.raises(CheckpointRejected) as exc:
        await _run(resume_args)

    assert "netlist" in str(exc.value)


@pytest.mark.asyncio
async def test_resume_with_a_missing_version_file_is_refused(tmp_path):
    args, run_dir = make_args(tmp_path)
    with patch("analogcoder.cli.run_orchestration", new=orchestration(pass_result(run_dir))):
        await _run(args)

    os.remove(os.path.join(run_dir, "netlist_v1_ac_loop_gain.cir"))
    resume_args, _ = make_args(tmp_path, "--resume")

    with pytest.raises(CheckpointRejected) as exc:
        await _run(resume_args)

    assert "netlist_v1_ac_loop_gain.cir" in str(exc.value)


@pytest.mark.asyncio
async def test_resume_with_a_different_schema_version_is_refused(tmp_path):
    args, run_dir = make_args(tmp_path)
    with patch("analogcoder.cli.run_orchestration", new=orchestration(pass_result(run_dir))):
        await _run(args)

    path = os.path.join(run_dir, CHECKPOINT_FILENAME)
    payload = json.loads(open(path).read())
    payload["schema_version"] = 999
    with open(path, "w") as f:
        json.dump(payload, f)
    resume_args, _ = make_args(tmp_path, "--resume")

    with pytest.raises(CheckpointRejected) as exc:
        await _run(resume_args)

    assert "schema version" in str(exc.value)


# ---------------------------------------------------------------- 경계 1


@pytest.mark.asyncio
async def test_resuming_at_the_outer_iteration_boundary_hands_the_progress_back(tmp_path):
    args, run_dir = make_args(tmp_path)
    with patch(
        "analogcoder.cli.run_orchestration",
        new=orchestration(pass_result(run_dir), checkpoint_at=3, crash=True),
    ):
        with pytest.raises(Boom):
            await _run(args)

    assert read_payload(run_dir)["boundary"] == BOUNDARY_OUTER_ITERATION

    captured: dict = {}
    resume_args, _ = make_args(tmp_path, "--resume")
    with patch(
        "analogcoder.cli.run_orchestration",
        new=orchestration(pass_result(run_dir), captured=captured),
    ):
        result = await _run(resume_args)

    call = captured["calls"][0]
    assert call["resume"].outer_iter == 3
    assert call["resume"].consecutive_rollbacks == 1
    # 원본이 아니라 그 attempt 가 실제로 **받았던** 덱을 되읽는다 - 면적 게이트의
    # 기준선이 이 인자에서 잡힌다.
    assert call["initial"] == {"ac_loop_gain": NETLIST}
    assert result["resumed_from"]["boundary"] == BOUNDARY_OUTER_ITERATION
    assert result["resumed_from"]["outer_iter"] == 3
    assert result["status"] == "PASS"


def _corner_args(tmp_path, spec_yaml: str, run_dir: str, *extra):
    (tmp_path / "netlist.cir").write_text("* ac netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(spec_yaml)
    parser = build_arg_parser()
    return parser.parse_args(["--spec", str(spec_path), "--run-dir", run_dir, *extra])


@pytest.mark.asyncio
async def test_resuming_at_the_outer_iteration_boundary_restores_the_corner_seed_record(tmp_path):
    """T2: 체크포인트가 코너 씨앗 기록을 잃는다.

    코너 축소가 켜진 실행이 outer-iteration 경계에서 죽었다가 재개하면, 재개된
    실행의 `result["corner_reduction"]["seed"]`는 (a) null이 아니고 (b) 원본
    실행이 실제로 뽑은 씨앗과 같아야 한다. 재개는 **다시 씨앗을 뽑지 않는다** -
    진입 스윕을 재사용하므로 다시 뽑을 스윕이 없고, 다시 뽑으면 (오늘의
    `_reused_baseline_sweep` 경로가 없다면) 원래 실행과 다른 값이 나올 수 있다.
    """
    run_dir = str(tmp_path / "runs" / "seedresume1")
    entry = _sweep({"gain": _wc("fs", 41.0), "pm": _wc("fs", 55.0)})

    with (
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=entry) as mock_sweep,
        patch(
            "analogcoder.cli.run_orchestration",
            new=orchestration(pass_result(run_dir), checkpoint_at=3, crash=True),
        ),
    ):
        with pytest.raises(Boom):
            await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    # 첫 실행은 진입 스윕 한 번만 돌고(판정 스윕 전에 죽는다) 체크포인트에
    # corner_seed를 남긴다.
    assert mock_sweep.call_count == 1
    original_seed = read_payload(run_dir)["corner_seed"]
    assert original_seed is not None
    assert original_seed["mode"] == "argmax"

    with (
        # 재개한 실행에서도 run_full_pvt_sweep은 **한 번** 불린다 - 그것은
        # 판정(verdict) 스윕이다. 진입 스윕은 이력에서 재사용되어 다시 불리지
        # 않는다(재사용이 깨지면 "씨앗을 다시 뽑을 스윕이 있는" 상태가 되어
        # 아래 seed 비교가 우연히 통과할 수 있으므로, 호출 횟수로 그 경로가
        # 살아 있는지도 함께 확인한다).
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=entry) as mock_sweep_resume,
        patch(
            "analogcoder.cli.run_orchestration",
            new=orchestration(pass_result(run_dir)),
        ),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir, "--resume"))

    assert mock_sweep_resume.call_count == 1
    assert result["corner_reduction"]["seed"] is not None
    assert result["corner_reduction"]["seed"] == original_seed


@pytest.mark.asyncio
async def test_the_resume_event_declares_the_abandoned_range_and_read_events_drops_it(tmp_path):
    args, run_dir = make_args(tmp_path)
    with patch(
        "analogcoder.cli.run_orchestration",
        new=orchestration(pass_result(run_dir), checkpoint_at=3, crash=True),
    ):
        with pytest.raises(Boom):
            await _run(args)

    resume_args, _ = make_args(tmp_path, "--resume")
    with patch("analogcoder.cli.run_orchestration", new=orchestration(pass_result(run_dir))):
        result = await _run(resume_args)

    history = os.path.join(run_dir, "history.jsonl")
    everything = read_events(history, drop_discarded=False)
    kept = read_events(history)

    # 크래시한 이터레이션이 남긴 두 이벤트는 디스크에 그대로 있고(증거를
    # 파괴하지 않는다), 읽을 때만 떨어진다.
    assert [e["step"] for e in everything].count("tuning_proposal") == 1
    assert [e["step"] for e in kept].count("tuning_proposal") == 0
    assert result["resumed_from"]["discarded_events"] == 2
    start, end = result["resumed_from"]["discarded_lines"]
    assert end - start == 2
    resume_events = [e for e in kept if e["step"] == "resume"]
    assert len(resume_events) == 1
    assert resume_events[0]["discarded_lines"] == [start, end]
    assert result["resumed_from"]["resume_count"] == 1


@pytest.mark.asyncio
async def test_a_run_resumed_twice_says_so(tmp_path):
    args, run_dir = make_args(tmp_path)
    for _ in range(2):
        with patch(
            "analogcoder.cli.run_orchestration",
            new=orchestration(pass_result(run_dir), checkpoint_at=3, crash=True),
        ):
            with pytest.raises(Boom):
                await _run(args if _ == 0 else make_args(tmp_path, "--resume")[0])

    resume_args, _ = make_args(tmp_path, "--resume")
    with patch("analogcoder.cli.run_orchestration", new=orchestration(pass_result(run_dir))):
        result = await _run(resume_args)

    assert result["resumed_from"]["resume_count"] == 2


# ---------------------------------------------------------------- 경계 3


@pytest.mark.asyncio
async def test_resuming_at_the_optimization_boundary_skips_the_main_loop(tmp_path):
    """**최적화 단계는 처음부터 다시 돈다** - 자체 버전 스택과 이분 탐색을 갖고
    있어 중간 재개가 그 불변식을 다시 논증하게 만든다. 건너뛰는 것은 메인
    루프뿐이고, 그것이 이 경계가 존재하는 이유다(two_stage_opamp 기준 103분)."""
    args, run_dir = make_args(tmp_path)

    async def optimization_boom(*a, **k):
        raise Boom("최적화 단계에서 죽었다")

    with patch("analogcoder.cli.run_orchestration", new=orchestration(pass_result(run_dir))):
        with patch("analogcoder.cli.run_optimization", new=optimization_boom):
            with pytest.raises(Boom):
                await _run(args)

    assert read_payload(run_dir)["boundary"] == BOUNDARY_OPTIMIZATION

    captured: dict = {}
    resume_args, _ = make_args(tmp_path, "--resume")
    with patch(
        "analogcoder.cli.run_orchestration",
        new=orchestration(pass_result(run_dir), captured=captured),
    ):
        result = await _run(resume_args)

    # 메인 루프는 다시 돌지 않는다.
    assert captured.get("calls") is None
    assert result["status"] == "PASS"
    assert result["iterations_used"] == 2
    assert result["resumed_from"]["boundary"] == BOUNDARY_OPTIMIZATION
    # 이 시도의 orchestration_attempt 이벤트도 스왑 누적도 두 번 일어나지 않는다.
    events = read_events(os.path.join(run_dir, "history.jsonl"))
    assert [e["step"] for e in events].count("orchestration_attempt") == 1


@pytest.mark.asyncio
async def test_resuming_at_the_optimization_boundary_preserves_the_last_judged_snapshot(tmp_path):
    """C1 (전체-브랜치 리뷰가 종단으로 재현): `last_judged_corners`가 체크포인트에
    안 실리면, BOUNDARY_OPTIMIZATION에서 재개한 실행은 run_orchestration을
    통째로 건너뛰므로(그것이 이 경계가 존재하는 이유 - 위 test가 확인한다)
    그 스냅샷을 다시 찍을 기회가 아예 없다. `judged`가 영원히 None이 되면, 탐침이
    승격시켰지만 아직 한 번도 판정된 적 없는 코너에서 판정 스윕이 실패해도
    진짜 경로 불일치와 구별할 수 없다.

    리뷰어가 실제로 `_save(BOUNDARY_OPTIMIZATION)` 직후 죽이고 `--resume`으로
    재현한 것과 같은 모양: 탐침이 ss/1.98/125.0을 승격시킨 직후(그러나 그
    이터레이션의 판정자는 아직 승격 이전 집합만 봤다) 최적화 단계에서 죽는다.

    **반증 확인**: `cli.py`/`checkpoint.py`가 `last_judged_corners`를 체크포인트에
    싣지 않던 상태로 되돌리면(스키마 2), 재개된 실행에서 `judged`가 None이 되어
    아래 `corner_probe_promotion_reentry` 단언이 깨지고 대신
    `corner_path_disagreement`가 나서 `assert ... == []`가 실패한다."""
    run_dir = str(tmp_path / "runs" / "c1_resume")
    entry = _sweep({"gain": _wc("fs", 41.0), "pm": _wc("fs", 55.0)})   # 씨앗 = {NOMINAL, fs}
    promoted = _wc("ss", 12.0, voltage=1.98, temperature=125.0)

    async def optimization_boom(*a, **k):
        raise Boom("최적화 단계에서 죽었다")

    with (
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=entry),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], [])),
        patch("analogcoder.cli.build_corner_simulate",
              new=_judged_snapshot_build_corner_simulate(promoted_raw=promoted)),
        patch("analogcoder.cli.run_optimization", new=optimization_boom),
    ):
        with pytest.raises(Boom):
            await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    # 체크포인트는 승격 **이전** 스냅샷을 담아야 한다 - 진입 씨앗(NOMINAL, fs)만.
    payload = read_payload(run_dir)
    assert payload["boundary"] == BOUNDARY_OPTIMIZATION
    assert set(payload["last_judged_corners"]) == {"(deck)", "fs/1.98/125.0"}
    # 그런데 corner_set 자체는 탐침이 승격시킨 ss를 이미 담고 있다 - 스냅샷과
    # 집합이 갈라진 바로 그 상태를 체크포인트가 그대로 보존해야 한다.
    processes = {c["process"] for c in payload["corner_set"]["corners"] if c is not None}
    assert "ss" in processes

    # 재개: 판정 스윕이 정확히 그 승격된(그러나 아직 판정된 적 없는) 코너에서
    # 실패한다.
    verdict_fail = _sweep({"pm": promoted}, failing=["pm"])
    verdict_pass = _sweep({"pm": _wc("ss", 55.0, voltage=1.98, temperature=125.0)})

    async def optimization_noop(*a, **k):
        return {}

    resume_orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([verdict_fail, verdict_pass], [])),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], resume_orch_calls)),
        patch("analogcoder.cli.run_optimization", new=optimization_noop),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir, "--resume"))

    # 재진입이 실제로 일어났다 - 경로 불일치로 접혔다면 run_orchestration이
    # 다시 불리지 않았을 것이다.
    assert len(resume_orch_calls) == 1
    assert _history_events(run_dir, "corner_path_disagreement") == []
    assert len(_history_events(run_dir, "corner_probe_promotion_reentry")) == 1
    assert result["status"] == "PASS"
    # M10(T19): 이 재진입은 재개된 실행 **안에서** 처음 일어났다(재개 이전에는
    # attempt==0이었다) - 그래도 결과는 그것을 승격 재진입으로 실어야 한다.
    corner_reduction = result["corner_reduction"]
    assert len(corner_reduction["grown"]) == corner_reduction["attempts"] == 1
    assert corner_reduction["grown"] == [[]]
    assert corner_reduction["promotion_reentries"] == [
        {"attempt": 1, "criteria": ["pm"], "corners": ["ss/1.98/125.0"]}
    ]


@pytest.mark.asyncio
async def test_the_optimization_boundary_checkpoint_carries_the_accumulated_swaps(tmp_path):
    args, run_dir = make_args(tmp_path)
    result_with_swap = {
        **pass_result(run_dir),
        "topology_swaps": [{"outer_iter": 2, "block_path": "AMP", "topology_id": "miller_basic"}],
    }

    with patch("analogcoder.cli.run_orchestration", new=orchestration(result_with_swap)):
        await _run(args)

    payload = read_payload(run_dir)
    assert payload["all_topology_swaps"] == [
        {"attempt": 0, "outer_iter": 2, "block_path": "AMP", "topology_id": "miller_basic"}
    ]


# ---------------------------------------------------------------- 원자성


@pytest.mark.asyncio
async def test_the_checkpoint_file_is_never_left_half_written(tmp_path):
    """루프가 여러 번 체크포인트를 갈아 끼워도 파일은 언제나 온전한 JSON이다.
    임시 파일도 남지 않는다."""
    args, run_dir = make_args(tmp_path)
    with patch(
        "analogcoder.cli.run_orchestration",
        new=orchestration(pass_result(run_dir), checkpoint_at=2),
    ):
        await _run(args)

    names = sorted(n for n in os.listdir(run_dir) if n.startswith("checkpoint"))
    assert names == [CHECKPOINT_FILENAME]
    json.loads(open(os.path.join(run_dir, CHECKPOINT_FILENAME)).read())


# ---------------------------------------------------------------- 경계 2


@pytest.mark.asyncio
async def test_an_attempt_boundary_checkpoint_restores_the_attempt_counter(tmp_path):
    """경계 2에서 재개하면 run_orchestration 은 **재개 인자 없이** 불린다 -
    그 지점의 상태가 곧 다음 attempt 의 시작 상태이므로 루프 상태가 없다."""
    args, run_dir = make_args(tmp_path)
    with patch("analogcoder.cli.run_orchestration", new=orchestration(pass_result(run_dir))):
        await _run(args)

    path = os.path.join(run_dir, CHECKPOINT_FILENAME)
    payload = json.loads(open(path).read())
    payload["boundary"] = BOUNDARY_ATTEMPT
    payload["attempt"] = 2
    payload["grown_labels"] = [[], ["ff/1.98/27"]]
    # M10(T19): attempt 1은 승격 재진입이었다(코너를 하나도 더하지 않아
    # grown_labels[0]이 []다) - promotion_reentries가 그 attempt·기준·코너를
    # 담아 재개된 실행으로도 살아 있어야 한다.
    payload["promotion_reentries"] = [
        {"attempt": 1, "criteria": ["gain"], "corners": ["ss/1.62/27"]}
    ]
    payload["progress"] = None
    payload["orchestration_result"] = None
    with open(path, "w") as f:
        json.dump(payload, f)

    captured: dict = {}
    resume_args, _ = make_args(tmp_path, "--resume")
    with patch(
        "analogcoder.cli.run_orchestration",
        new=orchestration(pass_result(run_dir), captured=captured),
    ):
        result = await _run(resume_args)

    assert captured["calls"][0]["resume"] is None
    # attempt > 0 이므로 원본이 아니라 **수렴된 덱**에서 다시 시작한다.
    assert captured["calls"][0]["initial"] == {"ac_loop_gain": TUNED}
    assert result["corner_reduction"]["attempts"] == 2
    assert result["corner_reduction"]["grown"] == [[], ["ff/1.98/27"]]
    # **반증 확인 대상**: cli.py가 체크포인트에서 promotion_reentries를 복원하지
    # 않으면(초기화만 하고 checkpoint.promotion_reentries를 안 읽으면) 이
    # 단언은 빈 리스트를 보고 실패한다.
    assert result["corner_reduction"]["promotion_reentries"] == [
        {"attempt": 1, "criteria": ["gain"], "corners": ["ss/1.62/27"]}
    ]
    assert result["resumed_from"]["boundary"] == BOUNDARY_ATTEMPT


# ---------------------------------------------------------------- 사전 등록 판정 규칙 3


@pytest.mark.asyncio
async def test_a_run_without_the_flag_writes_no_resume_only_event(tmp_path):
    """`--resume` 없이 도는 기존 경로의 **동작**이 바뀌면 안 된다. 재개 전용
    이벤트 셋 중 하나라도 평범한 실행의 history.jsonl 에 나타나면 불채택이다."""
    args, run_dir = make_args(tmp_path)

    with patch(
        "analogcoder.cli.run_orchestration",
        new=orchestration(pass_result(run_dir), checkpoint_at=2),
    ):
        await _run(args)

    steps = {e["step"] for e in read_events(os.path.join(run_dir, "history.jsonl"))}

    assert steps & {"resume", "corner_set_restored", "pvt_baseline_sweep_reused"} == set()


@pytest.mark.asyncio
async def test_a_run_without_the_flag_adds_exactly_one_result_key(tmp_path):
    """재개 기능이 결과에 더하는 것은 `resumed_from` 하나뿐이고, 그것은 null 이다.

    (`pvt_sweep_error`는 재개가 더한 키가 **아니다** - 감사 §2.6의 스윕 가드가
    더한 것이고, `resumed_from`과 같은 규칙으로 스윕이 멀쩡히 돈 실행에도 null로
    실린다. 집합을 정확히 유지하는 것이 이 단언의 요점이므로 함께 적는다.)"""
    args, run_dir = make_args(tmp_path)

    with patch("analogcoder.cli.run_orchestration", new=orchestration(pass_result(run_dir))):
        result = await _run(args)

    assert set(result) == {
        "status",
        "final_netlist_paths",
        "run_dir",
        "iterations_used",
        "final_criteria",
        "topology_swaps",
        "optimization",
        "corner_reduction",
        "resumed_from",
        "pvt_sweep_error",
    }
