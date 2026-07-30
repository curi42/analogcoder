#!/usr/bin/env python3
"""탐침 회전이 **실제로 도달하는 범위**를 잰다. 시뮬레이션 없음, 셈만.

`corner_sim` 은 이터레이션마다 집합 밖 코너를 **하나** 돌린다
(`next_probe` 는 `probe_order` 를 심각도 오름차순으로 순환한다). 승격이
일어나면 회전 인덱스가 0 으로 돌아간다.

**왜 재는가.** `runs/cc20_coverage` 에서 탐침은 안전망으로 실제 작동했다 -
ε-피복이 버린 코너를 골라 실패를 찾아냈다. 그런데 **회전 주기를 아무도 재본
적이 없다.** 집합 밖이 예산보다 크면 탐침은 밖의 일부만 보고 실행이 끝나고,
그 사실은 로그 어디에도 안 남는다. 이 저장소가 열두 번 지불한 모양이다:
"게이트가 아무것도 안 할 때 로그가 어떻게 보이는가."

**상한을 잰다.** 실제 도달 수는 이보다 작을 수 있다 - 승격이 인덱스를 0 으로
되돌리므로 이미 본 코너를 다시 본다. 상한조차 작으면 그 아래는 볼 것도 없다.

사용:

    .venv/bin/python scripts/probe_rotation_coverage.py

산출물은 표준 출력뿐이다 - 이 계산은 스펙 파일과 두 상수(MAX_OUTER_ITERATIONS,
retry_budget)만 읽으므로 재현에 아티팩트가 필요 없다.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))

from analogcoder.orchestrator import MAX_OUTER_ITERATIONS
from analogcoder.pvt import all_corners
from analogcoder.spec import load_spec

REPO = os.path.dirname(_HERE)


def budget_coverage(grid: int, seed: int, retry_budget: int) -> dict:
    """탐침 회전이 예산 안에서 도달하는 범위의 상한. 셈의 규약은 이 함수 하나가
    갖는다 - 다른 스크립트(예: T18a의 `argmax_reduction_45.py`)는 이 함수를
    불러 써야지 같은 산수를 새로 베끼면 안 된다. 베끼면 두 스크립트가 갈라질
    수 있고, 갈라진 쪽이 `DEFAULT_RETRY_BUDGET`/`(retry_budget + 1)` 같은
    디테일 하나를 놓치는 것이 정확히 T12가 리뷰에서 잡힌 모양이다(예산을 0으로
    잡아 피복률이 3배 낮게 나온 것).

    실제 도달 수는 이 상한보다 작을 수 있다 - 승격이 회전 인덱스를 0으로
    되돌리므로 이미 본 코너를 다시 볼 수 있다."""
    outside = grid - seed
    # 한 실행이 쓸 수 있는 이터레이션의 상한. 재진입 시도마다 바깥 루프가
    # 다시 돌므로 (retry_budget + 1) 회분이다.
    iters = MAX_OUTER_ITERATIONS * (retry_budget + 1)
    reached = min(outside, iters)
    pct = 100.0 * reached / outside if outside else 100.0
    return {"outside": outside, "iters": iters, "reached": reached, "pct": pct}

# 실측된 씨앗 크기. 스윕을 다시 돌지 않기 위해 이 저장소가 이미 문서화한 값을
# 쓴다 - 전부 CLAUDE.md 와 결과 문서에 실측으로 적혀 있다.
#
#   (스펙 경로, 격자 크기, argmax 씨앗, coverage 씨앗 or None)
MEASURED = [
    ("benchmarks/bandgap/spec_corner_reduction.yaml", 9, 6, None),
    ("benchmarks/bandgap/spec_corner_coverage.yaml", 9, 6, 2),
    ("benchmarks/bandgap/spec_pvt.yaml", 45, 9, None),
    ("benchmarks/two_stage_opamp/spec_pvt.yaml", 45, 5, None),
]


def main() -> None:
    print(f"MAX_OUTER_ITERATIONS = {MAX_OUTER_ITERATIONS}  "
          f"(이터레이션마다 탐침 1개, 재진입 시도당 이 예산이 새로 돈다)\n")
    print(f"  {'spec':<52} {'축소':>4} {'격자':>4} {'씨앗':>4} {'밖':>4} "
          f"{'한바퀴':>6} {'예산내':>6} {'피복률':>7}")
    print(f"  {'-'*52} {'-'*4} {'-'*4} {'-'*4} {'-'*4} {'-'*6} {'-'*6} {'-'*7}")

    for rel, grid, argmax_seed, coverage_seed in MEASURED:
        spec = load_spec(os.path.join(REPO, rel))
        declared = len(all_corners(spec.pvt_corners)) if spec.pvt_corners else 0
        if declared != grid:
            raise SystemExit(
                f"{rel}: 격자가 {grid} 이 아니라 {declared} 이다 - "
                f"이 스크립트의 실측 표가 낡았다"
            )
        red = spec.corner_reduction
        # **선언 여부를 표에 싣는다.** 이 블록이 없으면 탐침은 오늘 **아예 돌지
        # 않는다** - 그런 스펙의 숫자는 실측이 아니라 "켰다면 이랬을 것"이라는
        # 투영이고, 둘을 같은 칸에 적으면 이 저장소가 반복해서 지불한 오류다.
        declared_reduction = red is not None and red.enabled
        # **선언하지 않은 스펙의 예산은 0 이 아니라 기본값이다.** 이 줄들이
        # 답하는 질문은 "축소를 **켜면** 어떻게 되는가" 이고, 켜면
        # `retry_budget` 은 선언 없이도 기본 2 다(`spec.py`). 0 을 쓰면
        # 예산이 10 이 되어 피복률이 3 배 낮게 나온다 - T12 의 유일한 산출물이
        # 자기 공식에 대해 틀린 값이 된다(리뷰가 잡았다).
        DEFAULT_RETRY_BUDGET = 2
        budget = red.retry_budget if red else DEFAULT_RETRY_BUDGET
        seeds = [("argmax", argmax_seed)]
        if coverage_seed is not None:
            seeds = [("coverage", coverage_seed)]
        for mode, seed in seeds:
            cov = budget_coverage(grid, seed, budget)
            # basename 만 쓰면 두 벤치마크의 spec_pvt.yaml 이 같은 줄로 보인다.
            name = f"{rel.split('/')[1]}/{os.path.basename(rel)} [{mode}]"
            mark = "O" if declared_reduction else "-"
            print(f"  {name:<52} {mark:>4} {grid:>4} {seed:>4} {cov['outside']:>4} "
                  f"{cov['outside']:>6} {cov['reached']:>6} {cov['pct']:>6.0f}%")

    print("""
  읽는 법:
  - "축소" = 이 스펙이 corner_reduction 을 **선언하는가**. `-` 인 줄의 숫자는
    실측이 아니라 **투영**이다 - 그 스펙에서는 탐침이 오늘 아예 돌지 않는다.
  - "한바퀴" = 밖의 모든 코너를 한 번씩 보는 데 필요한 이터레이션 수.
  - "예산내 도달" = MAX_OUTER_ITERATIONS x (retry_budget + 1) 안에서 볼 수 있는
    코너 수의 **상한**. 승격이 회전 인덱스를 0으로 되돌리므로 실제는 더 작다.
  - 피복률 100% 는 "예산 안에 한 바퀴가 돈다"는 뜻이지 "다 봤다"가 아니다 -
    실행이 그 전에 PASS 로 끝나면 거기서 멈춘다.""")


if __name__ == "__main__":
    main()
