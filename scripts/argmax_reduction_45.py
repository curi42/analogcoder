#!/usr/bin/env python3
"""T18a - 순수 argmax 코너 축소를 **45코너 격자**에서 실측한다.

사전 등록: `docs/superpowers/specs/2026-07-29-theory-adoption-roadmap.md`의
「T18 사전 등록」절(이미 커밋됨, 이 스크립트가 그 규칙을 바꾸지 않는다).
브리프: T18은 T18a(이 스크립트, 싸다)와 T18b(전체 실행 A/B, 비싸고 조건부)로
쪼개진다 - 1차 지표 셋이 **전부 진입 스윕 하나**에서 나오고 LLM이 개입하지
않기 때문이다.

**LLM 없음, 튜닝 루프 없음.** 하는 일은 셋뿐이다:

1. `benchmarks/bandgap/spec_corner_reduction_45.yaml`의 진입 스윕을 돈다
   (`pvt.run_full_pvt_sweep`, 병렬+캐시 - 45코너 x 5테스트벤치 = 225 시뮬,
   `spec_pvt.yaml`에서 실측된 52.6 s를 투영으로 쓴다).
2. `corner_selection.seed_from_sweep`을 **그대로** 부른다 - 씨앗을 손으로
   재현하지 않는다(출하 경로를 타야 한다).
3. 1차 지표(테스트벤치당 점 수 / 씨앗 크기 / 집합 밖 코너 수 / 탐침이 예산
   안에 한 바퀴를 도는가)를 투영과 나란히 낸다. 탐침 피복 계산은
   `scripts/probe_rotation_coverage.py`의 `budget_coverage`를 그대로
   불러 쓴다 - 그 셈의 규약을 여기서 새로 베끼지 않는다(T13이 같은 이유로
   스크립트 셋의 중복을 없앴다).

**판정 규칙(사전 등록에서 그대로 가져옴):** 투영과 실측이 어긋나면 그 어긋남이
결과다. 씨앗이 투영(9)보다 크게 나오면 "45코너에서도 축소가 별로 안 산다"가
결과이고, 그것은 부정 결과이지 오류가 아니다.

**측정 대상이 없는 코너를 "일치"로 읽지 않는다.** `worst_case_corners`에
항목이 없는 기준(어떤 코너에서도 측정이 안 나온 경우)은 씨앗 계산에서 통째로
빠진다(`corner_selection.seed_from_sweep`의 규약) - 이 스크립트는 그 결측을
0이나 통과로 되읽지 않고, `missing_criteria`로 따로 센다.

사용:

    .venv/bin/python scripts/argmax_reduction_45.py

산출물: `docs/superpowers/specs/2026-07-30-argmax-reduction-45.json`
(환경변수 `ARGMAX_REDUCTION_45_OUT`로 변경 가능).
"""

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
sys.path.insert(0, _HERE)

from analogcoder.corner_selection import label, seed_from_sweep
from analogcoder.netlist import resolve_includes
from analogcoder.orchestrator import MAX_OUTER_ITERATIONS
from analogcoder.pvt import all_corners, run_full_pvt_sweep
from analogcoder.simulators.cache import CachingSimulator
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec

# `budget_coverage`의 규약을 그대로 쓴다 - 새로 베끼지 않는다(브리프 제약 C).
from probe_rotation_coverage import budget_coverage

REPO = os.path.dirname(_HERE)
SPEC_PATH = os.path.join(REPO, "benchmarks", "bandgap", "spec_corner_reduction_45.yaml")
OUT_PATH = os.environ.get(
    "ARGMAX_REDUCTION_45_OUT",
    os.path.join(
        REPO, "docs", "superpowers", "specs", "2026-07-30-argmax-reduction-45.json"
    ),
)

# T18 사전 등록이 적어 둔 투영값. 이 스크립트는 이 값을 바꾸지 않는다 - 어긋남을
# 적을 뿐이다(사전 등록: "규칙이 불편한 답을 내면 그 답을 적어라").
PROJECTED = {
    "points_per_tb": 11,
    "seed_size": 9,
    "outside": 36,
    "probe_coverage_pct": 83,
}


def _load_netlist_texts(spec) -> dict[str, str]:
    """cli.py의 진입 텍스트 조립과 바이트 동일한 경로(`resolve_includes`,
    테스트벤치 자신의 디렉터리 기준). 손으로 다르게 조립하면 진입 스윕이
    출하 경로와 다른 덱을 시뮬레이션하는 것이 된다."""
    texts = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            texts[tb.name] = resolve_includes(f.read(), os.path.dirname(tb.netlist_path))
    return texts


def main() -> None:
    spec = load_spec(SPEC_PATH)
    grid = all_corners(spec.pvt_corners)
    criteria = list(spec.all_criteria)
    n_tb = len(spec.testbenches)

    print(f"spec: {SPEC_PATH}")
    print(f"grid: {len(grid)} corners x {n_tb} testbenches = "
          f"{len(grid) * n_tb} simulations, {len(criteria)} criteria")
    print("running entry sweep (foreground, real ngspice)...")

    netlist_texts = _load_netlist_texts(spec)
    backend = CachingSimulator(NgspiceBackend())

    events: list[dict] = []
    t0 = time.monotonic()
    sweep = run_full_pvt_sweep(
        netlist_texts, spec, backend, log_event=lambda kind, data: events.append(
            {"kind": kind, **data}
        )
    )
    wall_s = time.monotonic() - t0
    cache_stats = backend.stats()

    # ---- 출하 경로 그대로: seed_from_sweep을 부른다. 손으로 재현하지 않는다. ----
    corner_set, seed_record = seed_from_sweep(sweep, spec)

    seed_labels = [label(p) for p in corner_set.corners if p is not None]
    outside_labels = [label(p) for p in corner_set.probe_order]

    # "측정 대상이 없는 코너"를 일치/통과로 읽지 않는다: seed_from_sweep이
    # 이미 빠뜨린 기준(어떤 코너에도 측정이 없는 경우)을 별도로 센다 -
    # worst_case_corners에 항목 자체가 없는 이름이 그것이다.
    present = set(sweep.get("worst_case_corners", {}).keys())
    missing_criteria = [c.name for c in criteria if c.name not in present]

    cov = budget_coverage(
        len(grid), seed_record["seed_size"], spec.corner_reduction.retry_budget
    )

    # ---- 재진입 발화 가능성(진입 덱 자체에서는 관찰 대상이 아니다) ----
    # T18a는 진입 스윕 하나만 돈다 - 재진입은 튜너가 움직인 덱에서만 의미를
    # 갖는 개념이라(사전 등록 "재진입이 발화하는지" 절), 여기서는 세지 않고
    # T18b로 넘긴다는 사실만 기록한다.

    result = {
        "task": "T18a",
        "spec": os.path.relpath(SPEC_PATH, REPO),
        "grid_size": len(grid),
        "testbenches": n_tb,
        "criteria": len(criteria),
        "total_simulations": len(grid) * n_tb,
        "wall_clock_s": wall_s,
        "cache_stats": cache_stats,
        "seed": {
            "mode": seed_record["mode"],
            "seed_size": seed_record["seed_size"],
            "seed_corners": seed_labels,
            "record": seed_record,
        },
        "points_per_tb": seed_record["points_per_tb"],
        "outside": {
            "count": len(outside_labels),
            # 집합 밖 코너 = probe_order 그 자체(둘 다 corner_selection이
            # severity 오름차순으로 만든 같은 목록) - 별도 필드로 중복해
            # 싣지 않는다.
            "probe_order": outside_labels,
        },
        "missing_criteria": missing_criteria,
        "overall_pass": sweep.get("overall_pass"),
        "failing_criteria": [e["name"] for e in sweep.get("criteria", []) if not e["pass"]],
        "probe_budget_coverage": {
            "retry_budget": spec.corner_reduction.retry_budget,
            "max_outer_iterations": MAX_OUTER_ITERATIONS,
            **cov,
            "reaches_full_rotation": cov["reached"] >= cov["outside"],
        },
        "projected": PROJECTED,
        "deviations": {},
        "events_sample": {
            "corner_render_count": sum(1 for e in events if e["kind"] == "corner_render"),
            "sim_cache_count": sum(1 for e in events if e["kind"] == "sim_cache"),
        },
    }

    # ---- 투영 대조: 어긋나면 어긋남 자체가 결과다 ----
    deviations = {}
    if seed_record["seed_size"] != PROJECTED["seed_size"]:
        deviations["seed_size"] = {
            "projected": PROJECTED["seed_size"], "measured": seed_record["seed_size"],
        }
    if seed_record["points_per_tb"] != PROJECTED["points_per_tb"]:
        deviations["points_per_tb"] = {
            "projected": PROJECTED["points_per_tb"], "measured": seed_record["points_per_tb"],
        }
    if len(outside_labels) != PROJECTED["outside"]:
        deviations["outside"] = {
            "projected": PROJECTED["outside"], "measured": len(outside_labels),
        }
    measured_pct = round(cov["pct"])
    if measured_pct != PROJECTED["probe_coverage_pct"]:
        deviations["probe_coverage_pct"] = {
            "projected": PROJECTED["probe_coverage_pct"], "measured": measured_pct,
        }
    result["deviations"] = deviations

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\nwall clock: {wall_s:.1f}s  ({result['total_simulations']} sims, "
          f"cache hits={cache_stats['hits']} misses={cache_stats['misses']})")
    print(f"overall_pass (entry deck, 45 corners): {result['overall_pass']}")
    if result["failing_criteria"]:
        print(f"  failing: {result['failing_criteria']}")
    if missing_criteria:
        print(f"  missing measurements (no corner produced a value): {missing_criteria}")
    print()
    print(f"{'metric':<24} {'projected':>10} {'measured':>10}  match")
    print(f"{'-'*24} {'-'*10} {'-'*10}  -----")
    print(f"{'points_per_tb':<24} {PROJECTED['points_per_tb']:>10} "
          f"{seed_record['points_per_tb']:>10}  "
          f"{'points_per_tb' not in deviations}")
    print(f"{'seed_size':<24} {PROJECTED['seed_size']:>10} "
          f"{seed_record['seed_size']:>10}  {'seed_size' not in deviations}")
    print(f"{'outside':<24} {PROJECTED['outside']:>10} "
          f"{len(outside_labels):>10}  {'outside' not in deviations}")
    print(f"{'probe_coverage_pct':<24} {PROJECTED['probe_coverage_pct']:>10} "
          f"{measured_pct:>10}  {'probe_coverage_pct' not in deviations}")
    print(f"\nseed corners: {seed_labels}")
    print(f"outside (probe order, severity asc): {outside_labels}")
    print(f"\nresult written to {OUT_PATH}")

    if deviations:
        print(f"\nDEVIATIONS FROM PROJECTION (this is a result, not an error): "
              f"{json.dumps(deviations, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
