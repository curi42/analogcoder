"""45코너 코너 축소 A/B 집계기 - `docs/superpowers/specs/2026-08-03-
reduction45-benefit-design.md`(개정 1)가 정한 규칙을 그대로 코드로 옮긴다.

**규칙을 새로 정하지 않는다.** 값·격자·판정 규칙은 그 문서가 정했고, 여기는
`result.json`/`history.jsonl`에서 그 규칙이 필요로 하는 값을 뽑아 규칙을
그대로 적용하기만 한다. 각 함수의 독스트링에 옮긴 원문을 그대로 적는다.

읽는 것: `scripts/reduction45_ab.py`가 쓴 `runs/reduction45/invocations.jsonl`
한 줄마다 하나의 `run_dir`, 그리고 그 안의 `result.json` / `history.jsonl`.

**탈락 경로는 소리 없이 빠지지 않는다.** `result.json`이 없거나(`no_result_json`)
상한에 걸려 죽었으면(`killed_by_cap`) 그 행은 `row_status="dropped"`로 라벨이
붙어 그대로 출력에 남고, 판정에서는 **채택에 불리한 쪽**으로 작용한다(아래
`judge` 독스트링). **행 자체의 탈락 상태 키(`row_status`)와 `result.json`
안의 판정 키(`result_status`)는 이름이 다르다** - 다른 집계기가 딕셔너리
병합 중 두 `status`가 같은 이름이라 서로를 덮어써 모든 행이 조용히 탈락하고
판정이 거짓 `void`로 나온 적이 있어서다.
"""

import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from analogcoder.history import read_events  # noqa: E402
from analogcoder.json_io import restore_non_finite  # noqa: E402

RUN_ROOT = "runs/reduction45"
INVOCATIONS_PATH = os.path.join(RUN_ROOT, "invocations.jsonl")

# 사전 등록: "1.5 배라는 값의 근거 ... 이 값은 잡음에서 온 것이 아니라
# 의미에서 왔다."
COST_RATIO_LIMIT = 1.5


# ---------------------------------------------------------------------------
# history.jsonl에서 값 뽑기 - 순수 함수, 실행하지 않는다.
# ---------------------------------------------------------------------------

def mid_pass_sweep_fail_events(events: list[dict]) -> list[dict]:
    """`mid_pass_sweep_fail`의 정의는 사전 등록의 문장 그대로다: **"중간 루프가
    낙관적으로 PASS 를 내고 최종 스윕이 그것을 뒤집는 사건"** - 브리프의
    표현으로는 **"중간 루프가 PASS 로 나온 뒤 최종 스윕이 실패했는가"**.

    "실행 전체가 FAIL로 끝났다"와 뭉개지 않는다: 재진입이 있으면 실행은 결국
    PASS로 끝날 수 있고, 그래도 이 사건은 일어난 것이다 - 그래서 이 함수는
    `result["status"]`를 전혀 보지 않고 `history.jsonl`의 사건 열만 본다.

    `cli.py`는 한 attempt(재진입 포함) 안에서 순서대로
    `orchestration_attempt`(중간 루프의 판정, `status` 필드) 다음에 - 이
    attempt가 corner-capable이면 - `pvt_final_sweep`(최종 스윕의 판정,
    `overall_pass` 필드)를 로그로 남긴다(cli.py 약 855~1003줄). 이 실행은
    단일 스레드로 순차 진행하므로 두 사건은 절대 다른 attempt의 것과 섞이지
    않는다 - 그래서 "가장 최근에 본 `orchestration_attempt.status`"를 들고
    있다가 다음 `pvt_final_sweep`을 만났을 때 그 상태가 `PASS`이고
    `overall_pass`가 `False`이면 사건이 일어난 것으로 기록한다.

    반환: 사건이 일어난 `pvt_final_sweep` 이벤트마다 하나씩,
    `{"mid_status": "PASS", "overall_pass": False, "summary": ...}`.
    """
    hits = []
    pending_status = None
    for event in events:
        step = event.get("step")
        if step == "orchestration_attempt":
            pending_status = event.get("status")
        elif step == "pvt_final_sweep":
            if pending_status == "PASS" and event.get("overall_pass") is False:
                hits.append({
                    "mid_status": pending_status,
                    "overall_pass": event.get("overall_pass"),
                    "summary": event.get("summary"),
                })
            # 이 attempt의 최종 스윕은 소비했다 - 다음 attempt가 새
            # orchestration_attempt를 반드시 먼저 남기므로 여기서 리셋해도
            # 안전하지만, 리셋하지 않아도 다음 orchestration_attempt가 곧바로
            # 덮으므로 결과는 같다. 명시적으로 리셋해 그 사실에 기대지 않는다.
            pending_status = None
    return hits


def sim_counts(events: list[dict]) -> dict:
    """사전 등록(개정 1): **"총 시뮬레이션은 진입 스윕 + 중간 루프 직접 시뮬 +
    재진입분 + 최종 스윕(들)을 센다. 캐시 적중은 시뮬로 세지 않는다(그것이
    캐시의 정의다) - 그러나 적중·불발 수를 함께 기록한다."** 그리고: **"면적
    단계와 전류 단계의 시뮬레이션은 이 합계에서 제외하고 따로 기록한다."**

    `simulators/cache.py`는 실제 SPICE 호출마다(적중이든 미적중이든) `sim_cache`
    이벤트를 무조건 남기지만, 그 이벤트 자체에는 어느 단계(중간 루프 vs
    면적/전류 최적화 단계)가 부른 것인지 적혀 있지 않다. 대신 각 단계는 자기
    스윕/스텝을 요약하는 이벤트를 **그 스윕에 쓴 `sim_cache` 이벤트들 뒤에**
    남긴다(예: `pvt_baseline_sweep`, `pvt_final_sweep`, `simulation`,
    `corner_probe`는 중간 루프 쪽, `optimize_area_*`/`optimize_*`는 면적/전류
    단계 쪽 - `optimizer.py`의 `PhaseConfig.label`이 `"optimize_area"`와
    `"optimize"`다). 그래서 연속된 `sim_cache` 배치를 그 배치 **바로 다음에
    나오는 비-`sim_cache` 이벤트**로 분류한다: 다음 이벤트 이름이
    `optimize_area_`로 시작하면 면적 단계, `optimize_`로 시작하면(그리고
    `optimize_area_`는 아니면) 전류(objective) 단계, 그 외는 전부 루프
    쪽이다. 배치가 파일 끝까지 닫히지 않으면(그 실행이 정확히 시뮬 도중
    죽었다는 뜻) `unclosed_misses`로 따로 싣고 **루프 쪽으로 센다** - 어느
    쪽인지 모를 때 더 비싼 쪽(루프 비용)에 넣는 것이 채택에 불리한 방향이다.

    반환: `{"loop_sims", "area_phase_sims", "objective_phase_sims",
    "cache_hits", "cache_misses", "unclosed_misses"}`. `loop_sims`가 채택
    규칙의 비(§비용 회계)에 쓰는 값이고, `area_phase_sims`는 부수 기록이다.
    """
    loop_sims = 0
    area_phase_sims = 0
    objective_phase_sims = 0
    cache_hits = 0
    cache_misses = 0

    pending_misses = 0
    for event in events:
        step = event.get("step")
        if step == "sim_cache":
            if event.get("hit"):
                cache_hits += 1
            else:
                cache_misses += 1
                pending_misses += 1
            continue
        # 비-sim_cache 이벤트를 만났다 - 지금까지 쌓인 배치를 이 이벤트로 닫는다.
        if step is not None and step.startswith("optimize_area_"):
            area_phase_sims += pending_misses
        elif step is not None and step.startswith("optimize_"):
            objective_phase_sims += pending_misses
        else:
            loop_sims += pending_misses
        pending_misses = 0

    unclosed_misses = pending_misses
    # 파일 끝까지 닫히지 않은 배치 - 어느 쪽인지 모른다. 루프 쪽(더 비싼 쪽,
    # ON이 불리해지는 쪽)에 더한다.
    loop_sims += unclosed_misses

    return {
        "loop_sims": loop_sims,
        "area_phase_sims": area_phase_sims,
        "objective_phase_sims": objective_phase_sims,
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "unclosed_misses": unclosed_misses,
    }


def reentry_count(events: list[dict]) -> int:
    """재진입 횟수(부수 기록). `cli.py`는 코너 집합을 실제로 키워 재진입할
    때(일반 성장, 탐침 승격 재진입 둘 다) 매번 `corner_set_grown` 이벤트를
    남기고 `attempt`를 하나 늘린다(cli.py 1130~1224줄 부근) - 그래서
    `corner_set_grown` 이벤트 수가 곧 이 실행이 재진입한 횟수다."""
    return sum(1 for event in events if event.get("step") == "corner_set_grown")


def area_optimization_summary(result: dict | None) -> dict:
    """사전 등록(개정 1)의 확인 사항: **"이 슬롯은 `pvt_corners`를 선언하므로
    `corner_capable`이 참이고, 그러면 면적 단계는 실측 여유분으로 수락하고
    확인 스윕과 이분 탐색으로 코너 확인된 버전에 착지한다 ... 이 문장은
    확인 사항이므로 결과 문서가 각 실행에서 이를 확인해 적어야 한다
    (`area_optimization`의 `corner_confirmed`와 착지 버전)."**

    `corner_confirmed`는 `optimizer._result`가 이미 재는 값
    (`bool(pvt_sweep and pvt_sweep.get("overall_pass"))`)을 그대로 옮긴다.
    "착지 버전"은 이 단계가 돌려준 `final_netlist_paths`다 - 그 단계가 옮긴
    덱의 파일 경로이므로(경로에 버전 번호가 실린다), 새로 만들지 않고 있는
    값을 그대로 노출한다."""
    area = (result or {}).get("area_optimization") or {}
    return {
        "corner_confirmed": area.get("corner_confirmed"),
        "landed_netlist_paths": area.get("final_netlist_paths"),
    }


# ---------------------------------------------------------------------------
# 실행 하나를 행으로: 탈락 경로에 라벨을 붙인다.
# ---------------------------------------------------------------------------

def build_row(invocation: dict, *, run_root: str = RUN_ROOT) -> dict:
    """`invocations.jsonl` 한 줄 + 그 `run_dir`의 산출물로 집계기 행 하나를
    만든다.

    **탈락 경로는 라벨을 붙여 그대로 남긴다** (`row_status`), **판정 값은
    측정된 것만 채우고 탈락한 자리는 `None`으로 남긴다**(추측해서 `False`를
    채우지 않는다 - CLAUDE.md: "측정된 사실이 선언된 사실을 이긴다"). 채택
    쪽으로의 편향은 여기서 만들지 않고 `judge`가 명시적으로 만든다 - 이
    함수는 사실만 옮긴다.

    `row_status`의 값:
      - `"ok"`: `result.json`이 있고 상한에 죽지 않았다.
      - `"dropped"`, `drop_reason="killed_by_cap"`: 감시견이 죽였다. 사전
        등록: "timeout은 채택 조건의 정확성 절을 만족시키지 못한다."
      - `"dropped"`, `drop_reason="no_result_json"`: 죽임당하지 않았는데도
        `result.json`이 없다(다른 크래시).
      - `"dropped"`, `drop_reason="no_history_jsonl"`: `result.json`은 있는데
        `history.jsonl`이 없다 - `mid_pass_sweep_fail`/`loop_sims`를 뽑을 수
        없다.

    **`row_status`(행의 탈락 상태)와 `result_status`(`result.json`의
    `"status"`, PASS/FAIL)는 이름이 다른 키다.** 하나로 합치면 두 사실 중
    하나가 조용히 없어진다 - 그것이 이 저장소가 최근 겪은 사고다.
    """
    run_dir = invocation["run_dir"]
    row = {
        "arm": invocation["arm"],
        "index": invocation["index"],
        "run_dir": run_dir,
        "invocation": dict(invocation),
        "wall_clock_s": invocation.get("elapsed_s"),
        "row_status": "ok",
        "drop_reason": None,
        "result_status": None,
        "result_reason": None,
        "mid_pass_sweep_fail": None,
        "mid_pass_sweep_fail_attempts": None,
        "loop_sims": None,
        "area_phase_sims": None,
        "objective_phase_sims": None,
        "cache_hits": None,
        "cache_misses": None,
        "reentry_count": None,
        "iterations_used": None,
        "corner_confirmed": None,
        "landed_netlist_paths": None,
    }

    if invocation.get("killed_by_cap"):
        row["row_status"] = "dropped"
        row["drop_reason"] = "killed_by_cap"
        return row

    result_path = os.path.join(run_dir, "result.json")
    if not os.path.exists(result_path):
        row["row_status"] = "dropped"
        row["drop_reason"] = "no_result_json"
        return row

    with open(result_path) as f:
        result = restore_non_finite(json.load(f))
    row["result_status"] = result.get("status")
    row["result_reason"] = result.get("failure_reason")
    row["iterations_used"] = result.get("iterations_used")
    area = area_optimization_summary(result)
    row["corner_confirmed"] = area["corner_confirmed"]
    row["landed_netlist_paths"] = area["landed_netlist_paths"]

    history_path = os.path.join(run_dir, "history.jsonl")
    if not os.path.exists(history_path):
        row["row_status"] = "dropped"
        row["drop_reason"] = "no_history_jsonl"
        return row

    events = read_events(history_path)
    hits = mid_pass_sweep_fail_events(events)
    row["mid_pass_sweep_fail"] = len(hits) > 0
    row["mid_pass_sweep_fail_attempts"] = len(hits)
    counts = sim_counts(events)
    row["loop_sims"] = counts["loop_sims"]
    row["area_phase_sims"] = counts["area_phase_sims"]
    row["objective_phase_sims"] = counts["objective_phase_sims"]
    row["cache_hits"] = counts["cache_hits"]
    row["cache_misses"] = counts["cache_misses"]
    row["reentry_count"] = reentry_count(events)
    return row


# ---------------------------------------------------------------------------
# 판정 - 사전 등록의 규칙을 그대로.
# ---------------------------------------------------------------------------

def check_precondition(off_rows: list[dict]) -> dict:
    """사전 등록: **"선행 조건(P): 축소를 끈 팔의 k 회 실행 중, 중간 루프가
    PASS 로 나온 뒤 최종 스윕이 실패한 실행이 1 건 이상 있어야 한다. P 가
    성립하지 않으면 이 측정은 void 다."**

    탈락한(`row_status != "ok"`) OFF 행은 사건을 확인할 수 없으므로 "일어난
    실행"으로 세지 않는다 - 값을 모르는 것을 있었다고 세면 없는 증거로 P를
    통과시키는 것이 되어 위험한 방향이다."""
    observed = [r for r in off_rows if r["row_status"] == "ok"]
    hit_count = sum(1 for r in observed if r["mid_pass_sweep_fail"])
    return {
        "holds": hit_count >= 1,
        "off_runs_observed": len(observed),
        "off_runs_total": len(off_rows),
        "off_hit_count": hit_count,
    }


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def check_accuracy(off_rows: list[dict], on_rows: list[dict]) -> dict:
    """사전 등록: **"1. 정확성: ON 의 3 회 중 '중간 PASS 인데 최종 스윕 실패'가
    0 건 이고, OFF 보다 적다."**

    ON 쪽 탈락 행(`killed_by_cap` 포함)은 사건이 없었다고 확인할 수 없으므로
    **사건이 일어난 것으로 센다** - 사전 등록이 이미 명시한 규칙이다:
    "timeout 은 채택 조건의 정확성 절을 만족시키지 못한다." OFF 쪽 탈락 행은
    반대 방향으로 불리하게 둔다: 세지 않는다(즉 OFF 의 실패 건수를 부풀리지
    않는다) - 그래야 ON 이 이겨야 하는 기준선이 더 낮아지지 않는다(더
    관대해지지 않는다).
    """
    on_fail = sum(
        1 for r in on_rows
        if r["row_status"] != "ok" or r["mid_pass_sweep_fail"]
    )
    off_fail = sum(
        1 for r in off_rows
        if r["row_status"] == "ok" and r["mid_pass_sweep_fail"]
    )
    return {
        "holds": on_fail == 0 and on_fail < off_fail,
        "on_fail_count": on_fail,
        "off_fail_count": off_fail,
        "on_dropped": [r for r in on_rows if r["row_status"] != "ok"],
    }


def check_cost(off_rows: list[dict], on_rows: list[dict]) -> dict:
    """사전 등록: **"2. 비용: ON 의 총 시뮬레이션 수 중앙값이 OFF 의 1.5 배
    이하."** (§비용 회계, 개정 1: 이 비는 루프 비용(`loop_sims`)으로 계산하고
    면적/전류 단계 시뮬은 뺀다.)

    탈락한 행은 시뮬 수를 모르므로 중앙값 표본에서 뺀다(0 이나 무한대로
    지어내지 않는다). 어느 쪽이든 관측된 표본이 하나도 없으면 비를 낼 수
    없으므로 기각된다(채택에 불리한 기본값)."""
    on_sims = [r["loop_sims"] for r in on_rows if r["row_status"] == "ok"]
    off_sims = [r["loop_sims"] for r in off_rows if r["row_status"] == "ok"]
    on_median = _median(on_sims)
    off_median = _median(off_sims)
    if on_median is None or off_median is None or off_median == 0:
        return {
            "holds": False, "on_median": on_median, "off_median": off_median,
            "ratio": None, "on_n": len(on_sims), "off_n": len(off_sims),
        }
    ratio = on_median / off_median
    return {
        "holds": ratio <= COST_RATIO_LIMIT,
        "on_median": on_median, "off_median": off_median, "ratio": ratio,
        "on_n": len(on_sims), "off_n": len(off_sims),
    }


def judge(off_rows: list[dict], on_rows: list[dict]) -> dict:
    """사전 등록의 판정 규칙 전체를 그대로 적용한다:

    **"P 를 먼저 보고, 성립할 때만 아래 채택 규칙을 적용한다."**
    **"채택 := ON 이 아래 둘을 모두 만족한다. ... 기각 := 위를 만족하지
    못한다."** **"동률·부분 성립은 기각이다. 규칙이 둘이 되지 않게 한다."**

    반환: `{"verdict": "void" | "accepted" | "rejected", "precondition":
    ..., "accuracy": ..., "cost": ...}`. `accuracy`/`cost`는 `precondition`이
    성립하지 않으면 계산하지 않는다(사전 등록: "P 가 성립하지 않으면 이
    측정은 void 다" - void 는 규칙 두 절을 적용하기 전의 산출물이다).
    """
    precondition = check_precondition(off_rows)
    if not precondition["holds"]:
        return {"verdict": "void", "precondition": precondition, "accuracy": None, "cost": None}

    accuracy = check_accuracy(off_rows, on_rows)
    cost = check_cost(off_rows, on_rows)
    accepted = accuracy["holds"] and cost["holds"]
    return {
        "verdict": "accepted" if accepted else "rejected",
        "precondition": precondition,
        "accuracy": accuracy,
        "cost": cost,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_invocations(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--invocations", default=INVOCATIONS_PATH)
    parser.add_argument("--out", default=None, help="집계 결과를 JSON으로도 쓸 경로")
    args = parser.parse_args(argv)

    invocations = load_invocations(args.invocations)
    rows = [build_row(inv) for inv in invocations]
    off_rows = [r for r in rows if r["arm"] == "off"]
    on_rows = [r for r in rows if r["arm"] == "on"]

    verdict = judge(off_rows, on_rows)
    output = {"rows": rows, "judgement": verdict}

    print(json.dumps(output, indent=2, sort_keys=True))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(output, f, indent=2, sort_keys=True)

    dropped = [r for r in rows if r["row_status"] != "ok"]
    if dropped:
        print(
            f"경고: {len(dropped)}개 행이 탈락했다 - "
            + ", ".join(f"{r['arm']}_{r['index']}:{r['drop_reason']}" for r in dropped),
            file=sys.stderr,
        )
    print(f"판정: {verdict['verdict']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
