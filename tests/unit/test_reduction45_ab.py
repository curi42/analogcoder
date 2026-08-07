"""`scripts/reduction45_ab.py`/`scripts/reduction45_aggregate.py`의 순수 함수
시험. ngspice도 LLM도 돌리지 않는다 - 가짜 `result.json`/`history.jsonl`과
아주 짧은 더미 서브프로세스(`sleep`)만 쓴다."""

import json
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from reduction45_ab import (  # noqa: E402
    ENABLED_TRUE_RE,
    _terminate_then_kill,
    run_with_cap,
    write_off_copy,
)
from reduction45_aggregate import (  # noqa: E402
    build_row,
    check_accuracy,
    check_cost,
    check_measurability,
    check_precondition,
    corner_seed_dropped,
    judge,
    landed_deck_sha256,
    mid_pass_sweep_fail_events,
    probe_promoted_count,
    reentry_count,
    sim_counts,
    total_outer_iterations,
)


# ---------------------------------------------------------------------------
# Step 1: 팔 전환 = 정확히 1회 계수 치환
# ---------------------------------------------------------------------------

def _write_slot(tmp_path, body: str) -> str:
    path = tmp_path / "slot.yaml"
    path.write_text(body)
    return str(path)


def test_enabled_true_substituted_exactly_once(tmp_path):
    src = _write_slot(tmp_path, "corner_reduction:\n  enabled: true\n  retry_budget: 2\n")
    dst = str(tmp_path / "off.yaml")

    write_off_copy(src, dst)

    text = open(dst).read()
    assert "enabled: false" in text
    assert "enabled: true" not in text
    # 다른 줄은 손대지 않는다.
    assert "retry_budget: 2" in text


def test_zero_matches_dies():
    with pytest.raises(SystemExit):
        _write_and_call("corner_reduction:\n  enabled: false\n")


def test_two_matches_dies():
    with pytest.raises(SystemExit):
        _write_and_call("a:\n  enabled: true\nb:\n  enabled: true\n")


def _write_and_call(body: str):
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "slot.yaml")
        with open(src, "w") as f:
            f.write(body)
        write_off_copy(src, os.path.join(d, "off.yaml"))


def test_enabled_true_regex_ignores_other_booleans():
    """`probe: true`처럼 다른 필드의 `true`는 건드리지 않는다는 것을
    정규식만으로 확인한다(치환은 `enabled:` 줄에만 걸린다)."""
    text = "corner_reduction:\n  enabled: true\n  probe: true\n"
    _, n = ENABLED_TRUE_RE.subn("enabled: false", text)
    assert n == 1


# ---------------------------------------------------------------------------
# Step 2: 감시견 (최소 검증 - 아주 짧은 상한만 쓴다)
# ---------------------------------------------------------------------------

def test_watchdog_kills_a_process_that_outlives_the_cap():
    start = time.monotonic()
    outcome = run_with_cap(["sleep", "5"], cap_s=0.2)
    elapsed_wall = time.monotonic() - start

    assert outcome["killed_by_cap"] is True
    # 죽이는 데 5초짜리 sleep을 다 기다리지 않는다 - 유예 5초를 넉넉히 두고도
    # 훨씨 짧게 끝나야 한다.
    assert elapsed_wall < 4.0


def test_watchdog_lets_a_fast_process_finish_normally():
    outcome = run_with_cap(["true"], cap_s=5.0)

    assert outcome["killed_by_cap"] is False
    assert outcome["exit"] == 0


def test_terminate_then_kill_survives_a_process_that_already_exited():
    """M1: `poll()` 직후의 좁은 경합 창에서 자식이 방금 끝났으면
    `os.getpgid(proc.pid)`가 `ProcessLookupError`를 낼 수 있다 - `killpg`
    호출들과 같은 방식으로 감싸지 않으면 감시견 자신이 죽어 남은 런이
    통째로 실행되지 않는다. 이미 끝나고 `wait()`까지 돼 완전히 회수된
    프로세스에 `_terminate_then_kill`을 직접 불러 예외 없이 조용히
    반환하는지 확인한다."""
    proc = subprocess.Popen(["true"], start_new_session=True)
    proc.wait()  # 완전히 회수 - pid가 이제 유효하지 않다.

    _terminate_then_kill(proc)  # 여기서 예외가 나면 이 시험이 실패한다.


# ---------------------------------------------------------------------------
# Step 3: mid_pass_sweep_fail - "실행이 FAIL로 끝났다"와 뭉개지 않는다
# ---------------------------------------------------------------------------

def _ev(step, **fields):
    return {"step": step, **fields}


def test_mid_pass_sweep_fail_fires_on_pass_then_sweep_fail():
    events = [
        _ev("orchestration_attempt", attempt=0, status="PASS"),
        _ev("pvt_final_sweep", overall_pass=False, summary="x failed"),
    ]
    hits = mid_pass_sweep_fail_events(events)
    assert len(hits) == 1


def test_mid_pass_sweep_fail_does_not_fire_when_mid_loop_failed():
    """중간 루프가 애초에 FAIL이면(예: max iterations reached) 최종 스윕이
    실패해도 이 사건이 아니다 - 낙관을 뒤집는 사건이 아니라 이미 실패였던
    것이 스윕에서도 실패한 것뿐이다."""
    events = [
        _ev("orchestration_attempt", attempt=0, status="max iterations reached"),
        _ev("pvt_final_sweep", overall_pass=False, summary="x failed"),
    ]
    assert mid_pass_sweep_fail_events(events) == []


def test_mid_pass_sweep_fail_can_differ_from_run_ending_in_fail():
    """재진입 시나리오: attempt 0은 중간 PASS인데 스윕 실패(사건 발생), 코너
    집합이 자라서 재진입, attempt 1은 중간 PASS + 스윕 PASS - **실행 전체는
    PASS로 끝나지만 사건은 일어난 것으로 남아야 한다.** 이것이 브리프가
    요구하는, `result["status"]`만으로는 못 뽑는 값이라는 증거다."""
    events = [
        _ev("orchestration_attempt", attempt=0, status="PASS"),
        _ev("pvt_final_sweep", overall_pass=False, summary="vbg0_droop failed"),
        _ev("corner_set_grown", attempt=1, added=["ss/1.62/-40"]),
        _ev("orchestration_attempt", attempt=1, status="PASS"),
        _ev("pvt_final_sweep", overall_pass=True, summary="all pass"),
    ]
    hits = mid_pass_sweep_fail_events(events)
    # 사건은 1건 일어났다 - "실행이 FAIL로 끝났다"라면 이 값이 0이어야 하는데
    # 아니다. 이 실행은 사실 PASS로 끝난다.
    assert len(hits) == 1


def test_mid_pass_sweep_fail_can_differ_from_run_ending_in_pass_too():
    """반대 방향도 확인한다: 중간 루프가 FAIL로 끝나 스윕 자체가 안 도는(혹은
    실패한 채) 실행도 있을 수 있는데, 그 경우 `result.status == "FAIL"`이지만
    이 사건(중간 PASS -> 스윕 뒤집힘)은 0건이다 - 두 값이 서로 독립임을
    보인다."""
    events = [_ev("orchestration_attempt", attempt=0, status="max iterations reached")]
    assert mid_pass_sweep_fail_events(events) == []


# ---------------------------------------------------------------------------
# sim_counts - 면적 단계는 루프 비용에서 뺀다
# ---------------------------------------------------------------------------

def test_sim_counts_splits_area_phase_from_loop():
    events = (
        # 진입 스윕: 3 sim -> loop
        [_ev("sim_cache", hit=False) for _ in range(3)]
        + [_ev("pvt_baseline_sweep", overall_pass=False)]
        # 면적 단계 진입 스윕: 4 sim -> area
        + [_ev("sim_cache", hit=False) for _ in range(4)]
        + [_ev("optimize_area_entry_sweep", overall_pass=True)]
        # 면적 단계 확인 스윕: 2 sim(캐시 적중 1건 포함, 적중은 안 센다) -> area
        + [_ev("sim_cache", hit=True), _ev("sim_cache", hit=False), _ev("sim_cache", hit=False)]
        + [_ev("optimize_area_confirm_sweep", overall_pass=True)]
        # 최종 스윕: 5 sim -> loop
        + [_ev("sim_cache", hit=False) for _ in range(5)]
        + [_ev("pvt_final_sweep", overall_pass=True)]
    )
    counts = sim_counts(events)
    assert counts["loop_sims"] == 3 + 5
    assert counts["area_phase_sims"] == 4 + 2
    assert counts["cache_hits"] == 1
    assert counts["cache_misses"] == 3 + 4 + 2 + 5
    assert counts["unclosed_misses"] == 0


def test_sim_counts_unclosed_batch_falls_into_loop():
    events = [_ev("pvt_baseline_sweep", overall_pass=True)] + [
        _ev("sim_cache", hit=False) for _ in range(2)
    ]
    counts = sim_counts(events)
    assert counts["unclosed_misses"] == 2
    assert counts["loop_sims"] == 2
    assert counts["area_phase_sims"] == 0


def test_reentry_count_counts_corner_set_grown():
    events = [
        _ev("corner_set_grown", attempt=1),
        _ev("orchestration_attempt", attempt=1, status="PASS"),
        _ev("corner_set_grown", attempt=2),
    ]
    assert reentry_count(events) == 2


# ---------------------------------------------------------------------------
# 판정 규칙 (v2): 선행 조건 P1+P2, 정확성(단일 절)+비용(반복 수), killed_by_cap
#
# v1 -> v2 로 바뀐 것과 그래서 바뀐 시험:
#   - `loop_sims` -> `total_outer_iterations`: 비용 축이 바뀌었으므로 판정에
#     쓰이는 모든 행 픽스처가 `loop_sims=500/550/2000` 대신 실측과 같은
#     자릿수인 `total_outer_iterations=4/5/7`을 쓰도록 고쳤다(§비용 축).
#   - `test_accept_requires_both_accuracy_and_cost_not_just_one`의 Case B
#     주석("정확성이 OFF와 같다")은 v1의 둘째 절을 가리키던 말이라 지웠다 -
#     v2는 그 절이 아예 없으므로 Case B가 기각되는 이유는 여전히
#     "on_fail_count(1) != 0" 하나뿐이다(로직은 안 바뀌었다, 주석만 정리).
# ---------------------------------------------------------------------------

def _ok_row(arm, index, *, mid_fail, total_outer_iterations):
    return {
        "arm": arm, "index": index, "row_status": "ok", "drop_reason": None,
        "mid_pass_sweep_fail": mid_fail,
        "total_outer_iterations": total_outer_iterations,
    }


def _dropped_row(arm, index, reason):
    return {
        "arm": arm, "index": index, "row_status": "dropped", "drop_reason": reason,
        "mid_pass_sweep_fail": None, "total_outer_iterations": None,
    }


def test_precondition_void_when_off_never_shows_the_event():
    off_rows = [_ok_row("off", i, mid_fail=False, total_outer_iterations=4) for i in (1, 2, 3)]
    on_rows = [_ok_row("on", i, mid_fail=False, total_outer_iterations=5) for i in (1, 2, 3)]

    result = judge(off_rows, on_rows)

    assert result["verdict"] == "void"
    assert result["precondition"]["holds"] is False


def test_precondition_holds_with_one_off_hit():
    check = check_precondition([
        _ok_row("off", 1, mid_fail=True, total_outer_iterations=4),
        _ok_row("off", 2, mid_fail=False, total_outer_iterations=4),
        _ok_row("off", 3, mid_fail=False, total_outer_iterations=4),
    ])
    assert check["holds"] is True
    assert check["off_hit_count"] == 1


def test_accept_requires_both_accuracy_and_cost_not_just_one():
    off_rows = [
        _ok_row("off", 1, mid_fail=True, total_outer_iterations=4),
        _ok_row("off", 2, mid_fail=True, total_outer_iterations=4),
        _ok_row("off", 3, mid_fail=False, total_outer_iterations=4),
    ]
    # Case A: 정확성은 만족하지만(사건 0건) 비용이 1.5배를 넘는다(7/4=1.75) ->
    # 기각.
    on_rows_cost_fails = [
        _ok_row("on", 1, mid_fail=False, total_outer_iterations=7),
        _ok_row("on", 2, mid_fail=False, total_outer_iterations=7),
        _ok_row("on", 3, mid_fail=False, total_outer_iterations=7),
    ]
    result_a = judge(off_rows, on_rows_cost_fails)
    assert result_a["accuracy"]["holds"] is True
    assert result_a["cost"]["holds"] is False
    assert result_a["verdict"] == "rejected"

    # Case B: 비용은 만족하지만(5/4=1.25, 1.5배 이내) 정확성이 불성립이다
    # (사건 1건 - v2는 "ON 사건 0건" 단일 절이므로 이것만으로 기각된다) ->
    # 기각.
    on_rows_accuracy_fails = [
        _ok_row("on", 1, mid_fail=True, total_outer_iterations=5),
        _ok_row("on", 2, mid_fail=False, total_outer_iterations=5),
        _ok_row("on", 3, mid_fail=False, total_outer_iterations=5),
    ]
    result_b = judge(off_rows, on_rows_accuracy_fails)
    assert result_b["cost"]["holds"] is True
    assert result_b["accuracy"]["holds"] is False
    assert result_b["verdict"] == "rejected"

    # Case C: 둘 다 만족 -> 채택.
    on_rows_both = [
        _ok_row("on", 1, mid_fail=False, total_outer_iterations=5),
        _ok_row("on", 2, mid_fail=False, total_outer_iterations=5),
        _ok_row("on", 3, mid_fail=False, total_outer_iterations=5),
    ]
    result_c = judge(off_rows, on_rows_both)
    assert result_c["verdict"] == "accepted"


def test_killed_by_cap_run_blocks_acceptance():
    off_rows = [
        _ok_row("off", 1, mid_fail=True, total_outer_iterations=4),
        _ok_row("off", 2, mid_fail=False, total_outer_iterations=4),
        _ok_row("off", 3, mid_fail=False, total_outer_iterations=4),
    ]
    on_rows = [
        _dropped_row("on", 1, "killed_by_cap"),
        _ok_row("on", 2, mid_fail=False, total_outer_iterations=5),
        _ok_row("on", 3, mid_fail=False, total_outer_iterations=5),
    ]
    result = judge(off_rows, on_rows)
    assert result["accuracy"]["holds"] is False
    assert result["verdict"] != "accepted"


def test_dropped_off_rows_do_not_inflate_off_failure_count():
    """OFF 쪽 탈락 행은 실패로 세지 않는다(ON에 유리하게 만들지 않는 방향) -
    떨어진 표본을 실패로 세면 OFF의 실패 건수가 부풀어 ON이 이기기 쉬워진다.
    v2에서 `off_fail_count`는 더 이상 채택 여부를 결정하지 않지만(둘째 절
    삭제) 여전히 기록으로 계산되므로 이 시험은 그대로 유효하다."""
    off_rows = [
        _dropped_row("off", 1, "no_result_json"),
        _ok_row("off", 2, mid_fail=True, total_outer_iterations=4),
        _ok_row("off", 3, mid_fail=False, total_outer_iterations=4),
    ]
    check = check_accuracy(off_rows, [
        _ok_row("on", 1, mid_fail=False, total_outer_iterations=5),
        _ok_row("on", 2, mid_fail=False, total_outer_iterations=5),
        _ok_row("on", 3, mid_fail=False, total_outer_iterations=5),
    ])
    assert check["off_fail_count"] == 1  # 탈락 행은 안 셌다


# ---------------------------------------------------------------------------
# v2 신설: P2(측정이 가능했는가) - 어느 팔이든 관측 런이 0건이면 void
# ---------------------------------------------------------------------------

def test_p2_void_when_on_arm_never_observed():
    """v1이 실제로 겪은 경로: OFF는 사건을 1건 관측했다(P1 성립)지만 ON은
    3런 모두 상한에 걸려 죽어 한 번도 관측되지 않았다. v1 규칙으로는 이것이
    accuracy에서 "on_fail=3(탈락은 사건으로 셈)"으로 흘러 `rejected`가
    나왔다(실제 v1 결과 문서의 판정). v2는 P2가 먼저 이것을 잡아 `void`를
    낸다."""
    off_rows = [
        _ok_row("off", 1, mid_fail=True, total_outer_iterations=4),
        _dropped_row("off", 2, "killed_by_cap"),
        _dropped_row("off", 3, "killed_by_cap"),
    ]
    on_rows = [
        _dropped_row("on", 1, "killed_by_cap"),
        _dropped_row("on", 2, "killed_by_cap"),
        _dropped_row("on", 3, "killed_by_cap"),
    ]

    result = judge(off_rows, on_rows)

    assert result["verdict"] == "void"
    assert result["measurability"]["holds"] is False
    assert result["measurability"]["on_runs_observed"] == 0
    assert result["measurability"]["off_runs_observed"] == 1
    # P1은(OFF가 1건 관측됐고 그 안에 사건이 있으므로) 성립한다 - P2 단독으로
    # void를 유발했다는 것을 보인다.
    assert result["precondition"]["holds"] is True


def test_p2_void_when_off_arm_never_observed():
    off_rows = [_dropped_row("off", i, "killed_by_cap") for i in (1, 2, 3)]
    on_rows = [_ok_row("on", i, mid_fail=False, total_outer_iterations=4) for i in (1, 2, 3)]

    result = judge(off_rows, on_rows)

    assert result["verdict"] == "void"
    assert result["measurability"]["holds"] is False
    assert result["measurability"]["off_runs_observed"] == 0


def test_check_measurability_holds_when_both_arms_have_at_least_one_observed_run():
    off_rows = [
        _ok_row("off", 1, mid_fail=True, total_outer_iterations=4),
        _dropped_row("off", 2, "killed_by_cap"),
        _dropped_row("off", 3, "killed_by_cap"),
    ]
    on_rows = [
        _dropped_row("on", 1, "killed_by_cap"),
        _dropped_row("on", 2, "killed_by_cap"),
        _ok_row("on", 3, mid_fail=False, total_outer_iterations=5),
    ]
    check = check_measurability(off_rows, on_rows)
    assert check["holds"] is True
    assert check["off_runs_observed"] == 1
    assert check["on_runs_observed"] == 1


# ---------------------------------------------------------------------------
# v2: 정확성은 단일 절 - 둘째 절("OFF 보다 적다")이 있었다면 거절했을 입력이
# v2에서는 통과함을 직접 보인다(변이 감지: 이 시험은 v1 수식으로 되돌리면
# 깨진다).
# ---------------------------------------------------------------------------

def test_accuracy_holds_on_zero_on_fail_regardless_of_off_fail_count():
    """`check_accuracy`는 `off_rows`/`on_rows`를 직접 받으므로 선행 조건 P1을
    거치지 않고도 `off_fail_count == 0`인 입력을 만들 수 있다(실제
    `judge()` 경로에서는 P1이 이를 막지만, 함수 자체의 계약을 시험한다).
    v1 수식이었다면 `holds = on_fail==0 and on_fail<off_fail`이므로
    `0 < 0`이 거짓이라 여기서 기각됐을 것이다 - v2는 그 절이 없으므로
    채택된다."""
    off_rows = [_ok_row("off", i, mid_fail=False, total_outer_iterations=4) for i in (1, 2, 3)]
    on_rows = [_ok_row("on", i, mid_fail=False, total_outer_iterations=4) for i in (1, 2, 3)]

    check = check_accuracy(off_rows, on_rows)

    assert check["off_fail_count"] == 0
    assert check["on_fail_count"] == 0
    assert check["holds"] is True


# ---------------------------------------------------------------------------
# v2: 비용은 `total_outer_iterations` 로 계산 - `loop_sims`가 아니다(변이
# 감지: `loop_sims`를 읽도록 되돌리면 이 시험의 비율이 반대로 나온다).
# ---------------------------------------------------------------------------

def test_cost_uses_total_outer_iterations_not_loop_sims():
    """OFF는 `loop_sims`가 크고(1000) `total_outer_iterations`는 작다(4).
    ON은 반대로 `loop_sims`가 작고(100) `total_outer_iterations`는 크다(10,
    OFF의 2.5배 - 1.5배 문턱을 넘는다). `check_cost`가 여전히 `loop_sims`를
    읽는다면 비율이 100/1000=0.1로 나와 채택 쪽으로 판정될 것이다 - 실제로는
    반복 수 축을 읽으므로 기각돼야 한다."""
    off_rows = [
        {
            "arm": "off", "index": i, "row_status": "ok",
            "total_outer_iterations": 4, "loop_sims": 1000,
        }
        for i in (1, 2, 3)
    ]
    on_rows = [
        {
            "arm": "on", "index": i, "row_status": "ok",
            "total_outer_iterations": 10, "loop_sims": 100,
        }
        for i in (1, 2, 3)
    ]

    check = check_cost(off_rows, on_rows)

    assert check["off_median"] == 4
    assert check["on_median"] == 10
    assert check["ratio"] == pytest.approx(2.5)
    assert check["holds"] is False


# ---------------------------------------------------------------------------
# total_outer_iterations - orchestration_attempt 이벤트들의 iterations_used
# 합. 이벤트가 없으면 None(0으로 지어내지 않는다).
# ---------------------------------------------------------------------------

def test_total_outer_iterations_sums_across_reentries():
    events = [
        _ev("orchestration_attempt", attempt=0, status="PASS", iterations_used=4),
        _ev("pvt_final_sweep", overall_pass=False),
        _ev("corner_set_grown", attempt=1),
        _ev("orchestration_attempt", attempt=1, status="PASS", iterations_used=2),
        _ev("pvt_final_sweep", overall_pass=True),
    ]
    assert total_outer_iterations(events) == 6


def test_total_outer_iterations_none_when_no_attempt_event():
    """상한에 걸려 죽은 실행처럼 `orchestration_attempt` 이벤트가 하나도
    없으면 `None` - "반복을 0회 했다"와 "이벤트가 없다"는 다른 사실이므로
    0으로 지어내지 않는다(실측: 상한에 걸려 죽은 `off_1`은 이 이벤트가
    0건이다)."""
    assert total_outer_iterations([]) is None
    assert total_outer_iterations([_ev("sim_cache", hit=True)]) is None


def test_total_outer_iterations_treats_missing_field_as_zero_for_that_attempt():
    events = [_ev("orchestration_attempt", attempt=0, status="PASS")]
    assert total_outer_iterations(events) == 0


# ---------------------------------------------------------------------------
# build_row - 탈락 경로가 라벨을 달고 남는다
# ---------------------------------------------------------------------------

def test_build_row_labels_missing_result_json(tmp_path):
    run_dir = tmp_path / "off_1"
    run_dir.mkdir()
    invocation = {
        "arm": "off", "index": 1, "spec": "x.yaml", "exit": 1,
        "killed_by_cap": False, "elapsed_s": 12.3, "run_dir": str(run_dir),
    }
    row = build_row(invocation)
    assert row["row_status"] == "dropped"
    assert row["drop_reason"] == "no_result_json"
    assert row["mid_pass_sweep_fail"] is None  # 지어내지 않는다
    assert row["total_outer_iterations"] is None  # 탈락 행은 반복 수도 None


def test_build_row_labels_killed_by_cap_even_if_result_json_exists(tmp_path):
    run_dir = tmp_path / "on_1"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "FAIL"}))
    invocation = {
        "arm": "on", "index": 1, "spec": "x.yaml", "exit": None,
        "killed_by_cap": True, "elapsed_s": 2400.0, "run_dir": str(run_dir),
    }
    row = build_row(invocation)
    assert row["row_status"] == "dropped"
    assert row["drop_reason"] == "killed_by_cap"


def test_build_row_labels_an_entry_simulation_gate_fail_as_dropped(tmp_path):
    """**새 값싼 실패 모양.** 진입 시뮬레이션 게이트(2026-08-07)는 환경이
    깨진 실행을 몇 초 만에 끝내면서 `result.json`과 `history.jsonl`을 둘 다
    남긴다 - 상한에도 안 걸렸고 산출물도 있으므로 옛 정의로는 `row_status="ok"`,
    `iterations_used=1`인 **빠르고 깨끗한 런**으로 읽힌다. 이 변경 전에는
    똑같은 환경 실패가 3반복 75분을 태워 눈에 띄었다.

    이 저장소가 이미 두 번 기록한 "관측의 정의가 불완전하다" 결함이고
    (v1 결함 2, v2 결함 5), 판별자인 `failure_reason`은 이번에 생겼는데
    아무도 읽지 않았다. 세지 않는 것이 옳다: 이 실행은 축소가 있든 없든
    아무것도 재지 못했으므로 두 팔 어느 쪽에 대해서도 증거가 아니다."""
    run_dir = tmp_path / "off_1"
    run_dir.mkdir()
    result = {
        "status": "FAIL",
        "iterations_used": 1,
        "failure_reason": (
            "the entry simulation produced no measurements in any testbench: "
            "dc_tc: ngspice fatal error, exit(1): could not find include file "
            "../../third_party/skywater-pdk-libs-sky130_fd_pr/models/parameters/"
            "lod.spice referenced from benchmarks/bandgap/pdk_corner.inc"
        ),
    }
    (run_dir / "result.json").write_text(json.dumps(result))
    with open(run_dir / "history.jsonl", "w") as f:
        f.write(json.dumps({"step": "entry_simulation_empty", "outer_iter": 1, "attempt": 0}) + "\n")

    invocation = {
        "arm": "off", "index": 1, "spec": "x.yaml", "exit": 0,
        "killed_by_cap": False, "elapsed_s": 8.4, "run_dir": str(run_dir),
    }
    row = build_row(invocation)

    assert row["row_status"] == "dropped"
    assert row["drop_reason"] == "entry_simulation_empty"
    # 라벨이 붙어도 읽힌 사실은 남는다 - `dropped`는 "값이 없다"가 아니라
    # "판정에 못 쓴다"이고, 두 사실을 뭉개면 왜 떨어졌는지 알 수 없다.
    assert row["result_status"] == "FAIL"
    assert "lod.spice" in row["result_reason"]
    # 지어내지 않는다: 이 실행은 사건이 일어났는지 확인할 수 없다.
    assert row["mid_pass_sweep_fail"] is None
    assert row["total_outer_iterations"] is None


def test_build_row_keeps_an_ordinary_fail_observed(tmp_path):
    """진입 게이트 라벨이 **모든** FAIL을 떨어뜨리면 안 된다 - 그러면 v1의
    `off_3`(중간 PASS 뒤 최종 스윕 실패)처럼 선행 조건이 걸려 있는 관측까지
    사라져 판정이 거짓 `void`가 된다. 판별자는 `failure_reason` 문자열
    하나뿐이고, 그 문자열이 `orchestrator`가 쓰는 것과 같아야 한다."""
    run_dir = tmp_path / "off_2"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({
        "status": "FAIL", "iterations_used": 5,
        "failure_reason": "final PVT sweep failed: vbg0_droop 22.9376 > 15",
    }))
    with open(run_dir / "history.jsonl", "w") as f:
        f.write(json.dumps({"step": "orchestration_attempt", "attempt": 0,
                            "status": "PASS", "iterations_used": 5}) + "\n")
        f.write(json.dumps({"step": "pvt_final_sweep", "overall_pass": False, "summary": "x"}) + "\n")

    row = build_row({
        "arm": "off", "index": 2, "spec": "x.yaml", "exit": 1,
        "killed_by_cap": False, "elapsed_s": 3600.0, "run_dir": str(run_dir),
    })

    assert row["row_status"] == "ok"
    assert row["drop_reason"] is None
    assert row["mid_pass_sweep_fail"] is True


def test_the_entry_gate_reason_the_aggregator_matches_is_the_one_orchestrator_writes(tmp_path):
    """**두 번째 사본을 만들지 않는다.** 집계기가 자기 문자열을 손으로 적으면
    `orchestrator.py`가 사유 문장을 한 글자 고치는 순간 이 라벨이 조용히
    영영 안 붙는다 - 그리고 그 침묵은 "진입 게이트 실패가 없었다"와 구별되지
    않는다. 그래서 집계기는 `orchestrator`가 쓰는 접두사를 **import**한다."""
    from analogcoder.orchestrator import ENTRY_SIMULATION_EMPTY_REASON
    from reduction45_aggregate import ENTRY_SIMULATION_EMPTY_REASON as aggregated

    assert aggregated is ENTRY_SIMULATION_EMPTY_REASON


def test_build_row_reads_a_real_result_and_history(tmp_path):
    run_dir = tmp_path / "off_1"
    run_dir.mkdir()
    result = {
        "status": "PASS", "failure_reason": None, "iterations_used": 3,
        "area_optimization": {
            "corner_confirmed": True,
            "final_netlist_paths": {"dc_tc": "runs/x/netlist_v2.cir"},
        },
    }
    (run_dir / "result.json").write_text(json.dumps(result))
    events = [
        {"step": "sim_cache", "hit": False},
        {"step": "pvt_baseline_sweep", "overall_pass": False},
        {"step": "orchestration_attempt", "attempt": 0, "status": "PASS", "iterations_used": 3},
        {"step": "sim_cache", "hit": False},
        {"step": "pvt_final_sweep", "overall_pass": False, "summary": "x"},
        {"step": "corner_set_grown", "attempt": 1},
        {"step": "orchestration_attempt", "attempt": 1, "status": "PASS", "iterations_used": 2},
        {"step": "sim_cache", "hit": False},
        {"step": "pvt_final_sweep", "overall_pass": True, "summary": "ok"},
    ]
    with open(run_dir / "history.jsonl", "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    invocation = {
        "arm": "off", "index": 1, "spec": "x.yaml", "exit": 0,
        "killed_by_cap": False, "elapsed_s": 555.0, "run_dir": str(run_dir),
    }
    row = build_row(invocation)

    assert row["row_status"] == "ok"
    assert row["result_status"] == "PASS"
    assert row["mid_pass_sweep_fail"] is True
    assert row["mid_pass_sweep_fail_attempts"] == 1
    assert row["reentry_count"] == 1
    assert row["loop_sims"] == 3
    # 재진입 포함 총 바깥 반복 수: attempt 0의 3 + attempt 1(재진입)의 2 = 5.
    assert row["total_outer_iterations"] == 5
    assert row["corner_confirmed"] is True
    assert row["landed_netlist_paths"] == {"dc_tc": "runs/x/netlist_v2.cir"}


# ---------------------------------------------------------------------------
# M2: 부수 기록 (corner_seed.dropped / 탐침 승격 수 / 착지 덱 SHA-256) -
# 판정에 안 흘러들어간다.
# ---------------------------------------------------------------------------

def test_corner_seed_dropped_none_when_no_seed_was_drawn():
    assert corner_seed_dropped({"corner_reduction": {"seed": None}}) is None
    assert corner_seed_dropped({"corner_reduction": {}}) is None
    assert corner_seed_dropped(None) is None


def test_corner_seed_dropped_reads_the_seed_record():
    result = {"corner_reduction": {"seed": {"mode": "argmax", "dropped": []}}}
    assert corner_seed_dropped(result) == []

    result_coverage = {
        "corner_reduction": {"seed": {"mode": "coverage", "dropped": ["ss/1.62/-40"]}}
    }
    assert corner_seed_dropped(result_coverage) == ["ss/1.62/-40"]


def test_probe_promoted_count_none_when_probe_never_ran():
    """탐침 이벤트가 아예 없으면(탐침이 안 돎) None - "0건 승격"과 다른 사실."""
    events = [_ev("orchestration_attempt", attempt=0, status="PASS")]
    assert probe_promoted_count(events) is None


def test_probe_promoted_count_zero_when_probe_ran_but_never_promoted():
    events = [
        _ev("corner_probe", corner="tt/1.8/27", failed=False, promoted=False),
        _ev("corner_probe", corner="ss/1.62/-40", failed=False, promoted=False),
    ]
    assert probe_promoted_count(events) == 0


def test_probe_promoted_count_counts_only_promoted_true():
    events = [
        _ev("corner_probe", corner="tt/1.8/27", failed=False, promoted=False),
        _ev("corner_probe", corner="ss/1.62/-40", failed=True, promoted=True),
        _ev("corner_probe", corner="ff/1.98/125", error="timeout", failed=False, promoted=False),
        _ev("corner_probe", corner="sf/1.62/125", failed=True, promoted=True),
    ]
    assert probe_promoted_count(events) == 2


def test_landed_deck_sha256_none_when_no_paths():
    assert landed_deck_sha256(None) is None
    assert landed_deck_sha256({}) is None


def test_landed_deck_sha256_none_when_a_file_is_unreadable(tmp_path):
    assert landed_deck_sha256({"dc_tc": str(tmp_path / "does-not-exist.cir")}) is None


def test_landed_deck_sha256_hashes_content_deterministically(tmp_path):
    import hashlib

    p1 = tmp_path / "a.cir"
    p2 = tmp_path / "b.cir"
    p1.write_text("* deck A\n")
    p2.write_text("* deck B\n")
    paths = {"tb_b": str(p2), "tb_a": str(p1)}

    got = landed_deck_sha256(paths)

    expected = hashlib.sha256()
    for name in sorted(paths):  # 이름순 - 딕셔너리 순서에 안 기댄다
        expected.update(name.encode("utf-8") + b"\n" + open(paths[name], "rb").read() + b"\n")
    assert got == expected.hexdigest()

    # 파일 내용이 바뀌면 해시도 바뀐다.
    p1.write_text("* deck A, changed\n")
    assert landed_deck_sha256(paths) != got


def test_build_row_fills_the_three_secondary_fields(tmp_path):
    run_dir = tmp_path / "on_1"
    run_dir.mkdir()
    netlist = run_dir / "netlist_v1.cir"
    netlist.write_text("* deck\n")
    result = {
        "status": "PASS",
        "failure_reason": None,
        "final_netlist_paths": {"dc_tc": str(netlist)},
        "corner_reduction": {"seed": {"mode": "argmax", "dropped": []}},
        "area_optimization": {"corner_confirmed": True, "final_netlist_paths": {"dc_tc": str(netlist)}},
    }
    (run_dir / "result.json").write_text(json.dumps(result))
    events = [
        {"step": "corner_probe", "corner": "ss/1.62/-40", "failed": True, "promoted": True},
        {"step": "orchestration_attempt", "attempt": 0, "status": "PASS"},
        {"step": "pvt_final_sweep", "overall_pass": True, "summary": "ok"},
    ]
    with open(run_dir / "history.jsonl", "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    invocation = {
        "arm": "on", "index": 1, "spec": "x.yaml", "exit": 0,
        "killed_by_cap": False, "elapsed_s": 100.0, "run_dir": str(run_dir),
    }
    row = build_row(invocation)

    assert row["corner_seed_dropped"] == []
    assert row["probe_promoted_count"] == 1
    assert row["landed_deck_sha256"] == landed_deck_sha256({"dc_tc": str(netlist)})


def test_secondary_fields_do_not_change_judge_output():
    """M2 요구: 세 부수 필드를 바꿔도 `judge`의 출력이 바뀌지 않는다.

    두 판정 표본을 만든다 - 판정에 쓰이는 필드(`row_status`,
    `mid_pass_sweep_fail`, `total_outer_iterations`)는 완전히 같고, 세 부수
    필드만 다르게 채운다. `judge`의 출력이 바이트 단위로 같아야 한다."""
    def _rows(secondary):
        off = [
            {
                "arm": "off", "index": i, "row_status": "ok",
                "mid_pass_sweep_fail": (i == 1), "total_outer_iterations": 4, **secondary,
            }
            for i in (1, 2, 3)
        ]
        on = [
            {
                "arm": "on", "index": i, "row_status": "ok",
                "mid_pass_sweep_fail": False, "total_outer_iterations": 5, **secondary,
            }
            for i in (1, 2, 3)
        ]
        return off, on

    off_a, on_a = _rows({
        "corner_seed_dropped": None, "probe_promoted_count": None, "landed_deck_sha256": None,
    })
    off_b, on_b = _rows({
        "corner_seed_dropped": ["ss/1.62/-40"], "probe_promoted_count": 3,
        "landed_deck_sha256": "deadbeef" * 8,
    })

    result_a = judge(off_a, on_a)
    result_b = judge(off_b, on_b)

    assert result_a == result_b
    assert result_a["verdict"] == "accepted"
