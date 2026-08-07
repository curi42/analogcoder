"""45코너 코너 축소 A/B 집계기 - v2 재측정
(`docs/superpowers/specs/2026-08-03-reduction45-benefit-v2-design.md`)가 정한
규칙을 그대로 코드로 옮긴다.

**규칙을 새로 정하지 않는다.** 값·격자·판정 규칙은 그 문서가 정했고, 여기는
`result.json`/`history.jsonl`에서 그 규칙이 필요로 하는 값을 뽑아 규칙을
그대로 적용하기만 한다. 각 함수의 독스트링에 옮긴 원문을 그대로 적는다.

v1(`2026-08-03-reduction45-benefit-design.md` 개정 1)에서 v2로 바뀐 것은
판정 규칙 셋이다: (1) 선행 조건에 P2("측정이 가능했는가")가 신설됐다 - 두
팔 모두 관측 런이 1건 이상이어야 하고, 아니면 `void`다. (2) 정확성 규칙의
둘째 절("OFF 보다 적다")을 삭제했다 - 선행 조건 P1이 이미
`off_fail_count >= 1`을 보장하므로 그 절은 발화할 수 없었다(v1 결함 3).
(3) 비용 축을 `loop_sims`에서 **총 바깥 반복 수**(`history.jsonl`의
`orchestration_attempt` 이벤트들의 `iterations_used` 합, 재진입분 포함)로
바꿨다 - `loop_sims`는 면적 단계가 최종 스윕 직전 같은 격자를 돌아 정의된
"최종 스윕" 항이 캐시로 인해 항상 0이 될 수밖에 없었다(v1 결함 4).
`loop_sims`/`area_phase_sims`는 부수 기록으로 계속 남긴다.

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
import hashlib
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from analogcoder.history import read_events  # noqa: E402
from analogcoder.json_io import restore_non_finite  # noqa: E402
from analogcoder.orchestrator import ENTRY_SIMULATION_EMPTY_REASON  # noqa: E402

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


def total_outer_iterations(events: list[dict]) -> int | None:
    """v2 사전 등록의 비용 축(§비용 축을 바꾼다): **"코너 확인된 판정에
    이르기까지 소비한 바깥 반복 수"(재진입분을 모두 합산한다)**.

    유도: `history.jsonl`의 `orchestration_attempt` 이벤트들의
    `iterations_used`를 전부 더한다 - 재진입이 있으면 이 이벤트가 여러 건
    난다(매 attempt마다 하나씩). `loop_sims`(시뮬 수)에서 이 축으로 바꾼
    이유는 v1 결함 4 - 반복 하나가 약 10분인데 그중 시뮬은 수십 초라 LLM
    지연이 지배적이고, 게다가 면적 단계가 최종 스윕 직전 같은 격자를 돌아
    `loop_sims`가 정의한 "최종 스윕" 항이 캐시로 인해 구조적으로 0이 될 수밖에
    없었다.

    이 이벤트가 **하나도 없으면** `None`을 돌려준다 - "반복을 0회 했다"와
    "이벤트 자체가 없다"는 다른 사실이고, 상한에 걸려 죽은 실행은 정확히 이
    경우다(실측: 상한에 걸려 죽은 `off_1`은 `orchestration_attempt` 이벤트가
    0건이고, 완주한 `off_3`/`on_3`은 1건에 `iterations_used=4`다 - 0으로
    지어내지 않는다). 개별 attempt 이벤트에 `iterations_used`가 없으면(정상
    경로에서는 항상 있지만) 그 attempt는 0으로 취급하고 나머지를 합산한다 -
    이벤트 자체는 있었으니 `None`으로 뭉개지 않는다."""
    attempts = [e for e in events if e.get("step") == "orchestration_attempt"]
    if not attempts:
        return None
    total = 0
    for attempt in attempts:
        value = attempt.get("iterations_used")
        if value is not None:
            total += value
    return total


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
# 부수 기록(판정에 안 쓴다) - 사전 등록: "부수적으로 기록하되 판정에 쓰지
# 않는다 ... corner_seed 의 dropped, 탐침이 승격시킨 코너 수, 그리고 각
# 실행이 착지한 덱의 SHA-256." **이 셋은 `judge`/`check_precondition`/
# `check_accuracy`/`check_cost` 어디에도 안 흘러들어간다** -
# `test_secondary_fields_do_not_change_judge_output`이 그것을 못박는다.
# ---------------------------------------------------------------------------

def corner_seed_dropped(result: dict | None) -> list | None:
    """`corner_seed`의 `dropped`(부수 기록). `cli.py`는 `seed_record`를 그대로
    `result["corner_reduction"]["seed"]`에 옮긴다(cli.py의 "corner_seed는
    seed_record를 그대로 옮긴다" 주석). `corner_selection.seed_from_sweep`이
    항상 `dropped` 키를 채운다(argmax 모드는 `[]`, coverage 모드는 실제
    탈락 목록).

    씨앗을 아예 안 뽑았으면(축소가 꺼졌거나 재개된 실행이 이번 회차에 다시
    안 뽑았으면) `seed`가 `None`이다 - 그때 이 함수도 `None`을 돌려준다.
    `[]`(뽑았고 하나도 안 버렸다)와 `None`(안 뽑았다)은 다른 사실이다."""
    seed = (result or {}).get("corner_reduction", {}).get("seed")
    if seed is None:
        return None
    return seed.get("dropped")


def probe_promoted_count(events: list[dict]) -> int | None:
    """탐침이 승격시킨 코너 수(부수 기록). `corner_sim.py`는 탐침을 쓸 때마다
    `corner_probe` 이벤트를 남기고 `promoted` 필드에 그 탐침이 코너 집합을
    실제로 키웠는지(`True`/`False`)를 싣는다(corner_sim.py 351~373줄) - 그래서
    `promoted is True`인 이벤트 수가 승격된 코너 수다.

    `corner_probe` 이벤트가 **하나도 없으면** `None`을 돌려준다(탐침 자체가
    이 실행에서 한 번도 안 돎 - 축소가 꺼졌거나 탐침이 꺼졌거나 진입 스윕
    전에 실행이 끝남). 이벤트는 있었지만 전부 `promoted=False`이면 `0`을
    돌려준다 - "아무것도 승격 안 됐다"(0)와 "탐침 자체가 없었다"(`None`)는
    다른 사실이다(브리프의 요구)."""
    probes = [event for event in events if event.get("step") == "corner_probe"]
    if not probes:
        return None
    return sum(1 for probe in probes if probe.get("promoted") is True)


def landed_deck_sha256(paths: dict | None) -> str | None:
    """각 실행이 착지한 덱의 SHA-256(부수 기록). `result["final_netlist_paths"]`
    (테스트벤치 이름 -> 파일 경로)는 `orchestrator.py`가 자기 반환값에 항상
    싣고(`"final_netlist_paths": state.current_netlist_paths()`) `cli.py`의
    이른 실패 경로도 항상 싣는 키다(I-3 키 존재 계약) - 그래서 `result.json`이
    있는 실행이면 언제나 값이 있다.

    "덱"은 테스트벤치 여러 개로 이뤄지므로, 테스트벤치 이름으로 정렬해
    `"<이름>\\n<그 파일 내용>\\n"`을 이어붙인 뒤 그 전체를 SHA-256으로
    해싱한다 - 경로 문자열이 아니라 **내용**을 해싱한다(이 저장소의 시뮬
    캐시가 경로가 아니라 덱 텍스트를 키에 넣는 것과 같은 이유: 경로는 실행마다
    임의로 바뀔 수 있지만 내용이 실제로 재현 가능한 사실이다).

    `paths`가 `None`이거나 어느 파일이든 지금 읽을 수 없으면(런 디렉터리가
    이미 정리됐거나 하는 경우) `None`을 돌려준다 - 못 잰 것을 빈 해시나
    가짜 해시로 지어내지 않는다."""
    if not paths:
        return None
    digest = hashlib.sha256()
    try:
        for name in sorted(paths):
            with open(paths[name], "rb") as f:
                content = f.read()
            digest.update(name.encode("utf-8") + b"\n" + content + b"\n")
    except OSError:
        return None
    return digest.hexdigest()


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
      - `"dropped"`, `drop_reason="entry_simulation_empty"`: 진입 시뮬레이션
        게이트(2026-08-07)가 끝낸 실행이다. **이 실패 모양은 값이 싸다** -
        상한에 안 걸리고, 두 산출물을 다 남기고, 몇 초 만에 끝난다. 그래서
        위 세 라벨 어느 것에도 안 걸리고 `iterations_used=1`인 "빠르고 깨끗한
        런"으로 읽힌다(이 게이트가 생기기 전에는 같은 환경 실패가 3반복
        75분을 태워 눈에 띄었다). 세지 않는 것이 옳다: 이 실행은 아무것도
        재지 못했으므로 축소를 켰든 껐든 어느 팔에 대해서도 증거가 아니다.
        판별자는 `failure_reason`뿐이고, 그 접두사는 `orchestrator`에서
        **import**한다 - 사본을 만들면 문장이 바뀌는 순간 조용히 안 맞게
        되고, 그 침묵은 "진입 게이트 실패가 없었다"와 구별되지 않는다.
        이 저장소가 이미 두 번 기록한 "관측의 정의가 불완전하다" 결함
        (v1 결함 2, v2 결함 5)이 **새로운 값싼 실패 모양**을 만난 것이다.

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
        "total_outer_iterations": None,
        "loop_sims": None,
        "area_phase_sims": None,
        "objective_phase_sims": None,
        "cache_hits": None,
        "cache_misses": None,
        "reentry_count": None,
        "iterations_used": None,
        "corner_confirmed": None,
        "landed_netlist_paths": None,
        # 부수 기록(판정에 안 쓴다) - 위 "부수 기록" 절 참조.
        "corner_seed_dropped": None,
        "probe_promoted_count": None,
        "landed_deck_sha256": None,
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

    # 진입 시뮬레이션 게이트가 끝낸 실행은 여기서 라벨을 단다 - `result.json`은
    # 읽고 나서(그래야 왜 떨어졌는지가 행에 남는다), 판정 값을 채우기 전에.
    # **접두사로 본다**: `cli.py`가 뒤에 최종 스윕 사유를 덧붙일 수 있어
    # 완전 일치로는 못 잡는다.
    if (row["result_reason"] or "").startswith(ENTRY_SIMULATION_EMPTY_REASON):
        row["row_status"] = "dropped"
        row["drop_reason"] = "entry_simulation_empty"
        return row
    area = area_optimization_summary(result)
    row["corner_confirmed"] = area["corner_confirmed"]
    row["landed_netlist_paths"] = area["landed_netlist_paths"]
    row["corner_seed_dropped"] = corner_seed_dropped(result)
    row["landed_deck_sha256"] = landed_deck_sha256(result.get("final_netlist_paths"))

    history_path = os.path.join(run_dir, "history.jsonl")
    if not os.path.exists(history_path):
        row["row_status"] = "dropped"
        row["drop_reason"] = "no_history_jsonl"
        return row

    events = read_events(history_path)
    hits = mid_pass_sweep_fail_events(events)
    row["mid_pass_sweep_fail"] = len(hits) > 0
    row["mid_pass_sweep_fail_attempts"] = len(hits)
    row["total_outer_iterations"] = total_outer_iterations(events)
    counts = sim_counts(events)
    row["loop_sims"] = counts["loop_sims"]
    row["area_phase_sims"] = counts["area_phase_sims"]
    row["objective_phase_sims"] = counts["objective_phase_sims"]
    row["cache_hits"] = counts["cache_hits"]
    row["cache_misses"] = counts["cache_misses"]
    row["reentry_count"] = reentry_count(events)
    row["probe_promoted_count"] = probe_promoted_count(events)
    return row


# ---------------------------------------------------------------------------
# 판정 - 사전 등록의 규칙을 그대로.
# ---------------------------------------------------------------------------

def check_precondition(off_rows: list[dict]) -> dict:
    """v2 사전 등록의 선행 조건 **P1(사건이 발생했는가)**: **"축소를 끈 팔의
    관측 런 중 중간 루프가 PASS 로 나온 뒤 최종 스윕이 실패한 실행이 1 건
    이상."** v1 에서 실증됐다(`off_3`).

    탈락한(`row_status != "ok"`) OFF 행은 사건을 확인할 수 없으므로 "일어난
    실행"으로 세지 않는다 - 값을 모르는 것을 있었다고 세면 없는 증거로 P1을
    통과시키는 것이 되어 위험한 방향이다."""
    observed = [r for r in off_rows if r["row_status"] == "ok"]
    hit_count = sum(1 for r in observed if r["mid_pass_sweep_fail"])
    return {
        "holds": hit_count >= 1,
        "off_runs_observed": len(observed),
        "off_runs_total": len(off_rows),
        "off_hit_count": hit_count,
    }


def check_measurability(off_rows: list[dict], on_rows: list[dict]) -> dict:
    """v2 사전 등록이 신설한 선행 조건 **P2(측정이 가능했는가)**: **"두 팔
    모두 관측 런이 1 건 이상. '관측' := 상한에 걸리지 않았고 `result.json`
    과 `history.jsonl` 을 남겼다. 어느 팔이든 0 건이면 `void` 다."**

    "관측"은 `build_row`가 이미 매기는 `row_status == "ok"` 그대로다(상한에
    걸려 죽었거나 산출물이 없으면 `"dropped"`로 라벨이 붙어 여기서 관측으로
    세지 않는다). v1은 이 조항이 없어, 처치(ON) 팔을 한 번도 못 본 상태가
    `void`가 아니라 `rejected`라는 라벨로 나갔다(v1 결함 2,
    `2026-08-03-reduction45-benefit-results.md`) - P2는 그 경로를 `void`로
    바로잡는다."""
    off_observed = sum(1 for r in off_rows if r["row_status"] == "ok")
    on_observed = sum(1 for r in on_rows if r["row_status"] == "ok")
    return {
        "holds": off_observed >= 1 and on_observed >= 1,
        "off_runs_observed": off_observed,
        "on_runs_observed": on_observed,
    }


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def check_accuracy(off_rows: list[dict], on_rows: list[dict]) -> dict:
    """v2 사전 등록: **"정확성: ON 의 관측 런에서 '중간 PASS 인데 최종 스윕
    실패'가 0 건."**

    v1의 둘째 절("OFF 보다 적다")은 **삭제됐다** - 선행 조건 P1이 이미
    `off_fail_count >= 1`을 보장하므로, `on_fail_count == 0`이 참이면 그 절
    (`on_fail < off_fail`, 즉 `0 < off_fail`)은 `off_fail >= 1`인 한 항상
    참이었다. 즉 어떤 입력에서도 판정을 바꾸지 못하는 절이었다(v1 결함 3).
    `off_fail_count`는 더 이상 채택 여부(`holds`)를 결정하지 않지만, 기록으로
    남긴다.

    ON 쪽 탈락 행(`killed_by_cap` 포함)은 사건이 없었다고 확인할 수 없으므로
    **사건이 일어난 것으로 센다** - 사전 등록이 이미 명시한 규칙이다:
    "timeout 은 채택 조건을 만족시키지 못한다." OFF 쪽 탈락 행은 반대
    방향으로 불리하게 둔다: 세지 않는다(즉 OFF 의 실패 건수를 부풀리지
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
        "holds": on_fail == 0,
        "on_fail_count": on_fail,
        "off_fail_count": off_fail,
        "on_dropped": [r for r in on_rows if r["row_status"] != "ok"],
    }


def check_cost(off_rows: list[dict], on_rows: list[dict]) -> dict:
    """v2 사전 등록: **"비용: ON 의 총 바깥 반복 수 중앙값이 OFF 의 1.5 배
    이하."**

    v1은 이 비를 시뮬 수(`loop_sims`)로 계산했으나 결함 4로 무효화됐다 - 면적
    단계가 최종 스윕 직전 같은 격자를 전량 재시뮬레이션하는데(실측:
    `off_3`에서 면적 진입 스윕 0 적중/225 불발, 그 직후 최종 스윕이 225
    적중/0 불발) 그 최종 스윕은 `loop_sims`의 정의상 캐시로 전량 적중되어
    항상 0을 더한다 - 정의된 항이 어떤 입력에서도 0이 아닌 값을 낼 수 없었다.
    v2는 이 축을 `total_outer_iterations`(총 바깥 반복 수, 재진입분 포함)로
    바꾼다 - 반복 수는 LLM 호출 수에 비례하고(실제 비용의 대부분), 환경
    독립적이며(벽시계와 달리 기계 부하에 흔들리지 않는다), 축소가 정말
    비싸지는 경로(코너 목표를 쫓느라 반복이 더 필요해짐)를 그대로 잡는다.
    문턱 1.5배는 그대로다(재진입 한 번이 대략 +25~50%라는 근거는 바뀌지
    않았다).

    탈락한 행이나 `total_outer_iterations`를 모르는 행(이벤트 자체가 없는
    경우)은 중앙값 표본에서 뺀다(0 이나 무한대로 지어내지 않는다). 어느
    쪽이든 관측된 표본이 하나도 없으면 비를 낼 수 없으므로 기각된다(채택에
    불리한 기본값)."""
    on_iters = [
        r["total_outer_iterations"] for r in on_rows
        if r["row_status"] == "ok" and r["total_outer_iterations"] is not None
    ]
    off_iters = [
        r["total_outer_iterations"] for r in off_rows
        if r["row_status"] == "ok" and r["total_outer_iterations"] is not None
    ]
    on_median = _median(on_iters)
    off_median = _median(off_iters)
    if on_median is None or off_median is None or off_median == 0:
        return {
            "holds": False, "on_median": on_median, "off_median": off_median,
            "ratio": None, "on_n": len(on_iters), "off_n": len(off_iters),
        }
    ratio = on_median / off_median
    return {
        "holds": ratio <= COST_RATIO_LIMIT,
        "on_median": on_median, "off_median": off_median, "ratio": ratio,
        "on_n": len(on_iters), "off_n": len(off_iters),
    }


def judge(off_rows: list[dict], on_rows: list[dict]) -> dict:
    """v2 사전 등록의 판정 규칙 전체를 그대로 적용한다:

    **"선행 조건을 둘 다 먼저 본다. 하나라도 불성립이면 `void` 이고 채택
    규칙을 적용하지 않는다."** (P1: 사건이 발생했는가. P2: 측정이
    가능했는가 - v2 신설.)
    **"둘 다 성립하면: 채택 := ON 이 아래 둘을 모두 만족한다. ... 기각 := 위를
    만족하지 못한다."** **"동률·부분 성립은 기각이다. 규칙이 둘이 되지 않게
    한다."**

    반환: `{"verdict": "void" | "accepted" | "rejected", "measurability":
    ..., "precondition": ..., "accuracy": ..., "cost": ...}`. `measurability`
    (P2)와 `precondition`(P1)은 항상 계산해 둘 다 남긴다 - 어느 쪽이 `void`를
    유발했는지 결과 문서가 구분해 적어야 하기 때문이다. `accuracy`/`cost`는
    P1·P2 가 둘 다 성립하지 않으면 계산하지 않는다(`void`는 채택 규칙을
    적용하기 전의 산출물이다).
    """
    measurability = check_measurability(off_rows, on_rows)
    precondition = check_precondition(off_rows)
    if not measurability["holds"] or not precondition["holds"]:
        return {
            "verdict": "void",
            "measurability": measurability,
            "precondition": precondition,
            "accuracy": None,
            "cost": None,
        }

    accuracy = check_accuracy(off_rows, on_rows)
    cost = check_cost(off_rows, on_rows)
    accepted = accuracy["holds"] and cost["holds"]
    return {
        "verdict": "accepted" if accepted else "rejected",
        "measurability": measurability,
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
