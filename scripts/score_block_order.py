#!/usr/bin/env python3
"""태스크 6 1층 — 거리 기반 **블록 렌더 순서**를 실측 정답표로 채점한다. 시뮬레이션·LLM 없음.

**판정 규칙은 이 스크립트가 정하지 않는다.**
`docs/superpowers/specs/2026-07-29-theory-adoption-roadmap.md` 의 「태스크 6 사전
등록」 절이 정답표가 도착하기 **전에** 커밋됐고(`edec7f6`), 이 스크립트는 그 절을
코드로 옮긴 것이다. 결과를 본 뒤 규칙을 고치는 것은 이 저장소가 D1 에서 명시적으로
철회한 절차다.

사전 등록에서 그대로 가져오는 것:

- 개입은 **순서만 바꾼다. 절대 걸러내지 않는다.** 초점은 후보 생성기이고 필터가
  아니다(CLAUDE.md). 그래서 도달 못 한 블록도 **렌더 목록에서 빠지지 않고**
  마지막 동률 그룹에 들어간다 - 순위를 안 매기고 빼버리면 그 블록을 지운
  개입이 되고, 그건 판정 대상이 아닌 다른 개입이다.
- 지표는 **가장 앞선 알려진 충분 블록의 순위**(낮을수록 좋다), 동률을 깨지 않는
  **최악 순위**(`score_knob_distance.rank_bands`).
- 기준선은 오늘의 `structure_view.select_focus` 이고 집합이므로 동률 그룹 하나다:
  초점 안 = 최악 순위 `|focus|`, 초점 밖 = 최악 순위 `총 블록 수`.
- **1차 구성은 `logdeg` x `min_distance` 하나.** 나머지 8칸은 탐색적이고 보고만
  한다. n=3 에 9칸을 걸면 갈림길의 정원이 된다.
- **최소 효과 크기:** 1차 구성이 3건 **전부에서** 기준선보다 나쁘지 않고,
  **최소 2건에서 1 순위 이상** 좋아야 통과. 1건만 좋아지는 것은 n=3 에서 잡음과
  구별되지 않는다.
- **1층 통과는 채택이 아니다.** "2층(LLM 쌍 프로브)을 돌릴 값이 있는가" 만 답한다.

정답표를 **하한**으로 읽는 방향도 사전 등록에 있다: 충분 블록은 단일 노브 세 값으로
쟀으므로, 실제로는 충분하지만 여기서 못 잡은 블록이 순위 앞에 있으면 지표는 순위를
실제보다 **나쁘게** 평가한다 - 채택에 불리한 방향이라 하한을 쓸 수 있다. 그래서
`block_state` 의 `capped`(= 모른다)는 충분 블록으로 **세지 않는다**.

## 이 스크립트가 사전 등록을 넘어 결정해야 했던 것 하나 - 공개한다

사전 등록은 거리정의 x 집계규칙만 구성 축으로 명명했고, **어느 덱의 그래프에서
거리를 재는가**는 적지 않았다. 두 답이 있다:

- `per_testbench`: criterion 마다 그 criterion 의 테스트벤치 덱에서 잰다.
  `scripts/aggregation_discriminates.py` 가 이렇게 했고, 사전 등록의 타당성 확인이
  인용한 최악 순위(BUF_N 2 / TRIMAMP 2 / BANDGAP 3 / BUF_P 4 / BGR_CORE 6 /
  ERRAMP 6)가 이 계산에서 나왔다.
- `canonical`: 정경 덱 하나에서 잰다. **출하 경로가 이것이다** -
  `orchestrator.py` 는 `derive_structure(netlist_texts[canonical_name])` 로
  구조를 만들고 그 구조를 렌더한다.

**1차 판정은 `per_testbench`** 로 한다. 사전 등록이 인용한 숫자가 그 계산이므로,
지금 `canonical` 로 바꾸면 정답표를 보기 전이라 해도 사전 등록이 committed 한
구성을 바꾸는 것이 된다. `canonical` 은 같이 계산해서 **탐색적으로 보고**한다 -
둘이 갈리면 그 갈림이 결과이고, 실제 개입을 구현할 때는 `canonical` 이어야 한다는
것도 같이 기록된다.

사용:

    .venv/bin/python scripts/score_block_order.py
"""

import json
import os
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
sys.path.insert(0, _HERE)

from analogcoder.control_block import measurement_nets  # noqa: E402
from analogcoder.signal_path import build_signal_paths  # noqa: E402
from analogcoder.spec import load_spec  # noqa: E402
from analogcoder.structure import derive_structure  # noqa: E402
from analogcoder.structure_view import select_focus  # noqa: E402

# **채점기를 새로 쓰지 않는다** - 거리 정의와 순위 밴드는 태스크 6 이 이미 만들었고,
# 집계 규칙은 `aggregation_discriminates` 가 이미 만들었다. 손으로 다시 만들면
# `compose.py` 가 `netlist.py` 의 파싱 규칙을 베껴 두 방향으로 갈라진 것과 같은
# 모양이 된다(T13 이 같은 이유로 세 스크립트의 판정 술어 중복을 없앴다).
from aggregation_discriminates import DEFINITIONS, _aggregate, _block_of  # noqa: E402
from aggregation_discriminates import _per_criterion_scores as _per_crit_by_testbench  # noqa: E402
from score_knob_distance import INF, BipartiteGraph, SignalFlowGraph, rank_bands  # noqa: E402

REPO = os.path.dirname(_HERE)
SPEC = os.path.join(REPO, "benchmarks", "bandgap", "spec_corner_reduction.yaml")
ANSWER_KEY = os.path.join(
    REPO, "docs", "superpowers", "specs", "2026-07-30-task6-sufficient-blocks.json"
)
OUT = os.path.join(REPO, "docs", "superpowers", "specs", "2026-07-30-task6-block-order-score.json")

RULES = ("min_distance", "min_rank", "min_sum")
PRIMARY = ("logdeg", "min_distance")
DECK_MODES = ("per_testbench", "canonical")


# ------------------------------------------------------- 거리: 정경 덱 한 장에서
def _per_criterion_scores_canonical(spec, criterion_name, definition, structure, paths, nets_by_meas):
    """`per_testbench` 의 정경-덱 대응물. 출하 경로(`orchestrator.py`)와 같은 그래프다.

    측정 넷은 `orchestrator` 와 똑같이 **테스트벤치를 가로질러 합집합**으로 병합한
    표에서 읽는다 - dict.update 로 덮어쓰면 PSR 테스트벤치들처럼 이름을 재사용하는
    경우에 앞선 것이 보던 넷이 조용히 사라진다.
    """
    crit = next(c for tb in spec.testbenches for c in tb.criteria if c.name == criterion_name)
    names = sorted(nets_by_meas.get(crit.measurement, set()))

    if definition == "signal_flow":
        graph = SignalFlowGraph(structure, paths)
        resolved = [n for n in names if graph.uf.find((None, n)) in graph.rev]
        dist = graph.distances(resolved)
        return {refdes: dist.get(refdes, INF) for refdes in graph.devices}, len(resolved)

    graph = BipartiteGraph(structure, paths)
    sources = [graph.net_node(None, n) for n in names]
    resolved = [s for s in sources if s in graph.adj]
    dist = graph.distances(resolved, definition)
    return {refdes: dist.get(("DEV", refdes), INF) for refdes in graph.devices}, len(resolved)


# ------------------------------------------------------- 블록 순위와 지표
def _block_scores(device_scores: dict[str, float]) -> dict[str, float]:
    """블록 점수 = 그 안 소자 점수의 최솟값. 최상위 소자(스코프 없음)는 블록이 아니다."""
    best: dict[str, float] = {}
    for refdes, v in device_scores.items():
        block = _block_of(refdes)
        if not block:
            continue
        if block not in best or v < best[block]:
            best[block] = v
    return best


def _worst_ranks(block_scores: dict[str, float], all_blocks: set[str]) -> dict[str, int]:
    """블록 -> 동률을 깨지 않는 최악 순위.

    **도달 못 한 블록(INF)과 점수가 아예 없는 블록도 순위를 받는다** - 마지막 동률
    그룹, 최악 순위 = 총 블록 수. 개입이 순서만 바꾸고 걸러내지 않으므로 그 블록도
    렌더되며, 다만 맨 뒤에 렌더된다. 빼버리면 판정 대상이 다른 개입이 된다.
    """
    finite = {b: v for b, v in block_scores.items() if v != INF}
    bands = rank_bands({(b,): v for b, v in finite.items()})
    out = {b: band[1] for (b,), band in bands.items()}
    tail = sorted(all_blocks - set(out))
    if tail:
        worst = len(all_blocks)
        for b in tail:
            out[b] = worst
    return out


def _metric(worst_ranks: dict[str, int], sufficient: list[str]) -> int | None:
    """가장 앞선 **알려진** 충분 블록의 최악 순위. 낮을수록 좋다."""
    ranks = [worst_ranks[b] for b in sufficient if b in worst_ranks]
    return min(ranks) if ranks else None


def _baseline(spec, structure, paths, canonical_text, failing, nets_by_meas, all_blocks, sufficient):
    """오늘의 `select_focus`. 집합이므로 동률 그룹 하나다.

    `touched_refdes` 는 **빈 집합**이다: 거리 순서에는 "이미 건드린 블록" 개념이
    없으므로 양쪽이 같은 정보를 봐야 비교가 성립하고, 첫 제안 지점이 그 상태다.
    """
    failing_nets: set[str] = set()
    meas_by_crit = {c.name: c.measurement for tb in spec.testbenches for c in tb.criteria}
    for name in failing:
        failing_nets |= nets_by_meas.get(meas_by_crit.get(name), set())
    focus = select_focus(structure, paths, failing_nets, set(), canonical_text)
    focus_blocks = {b for b in focus if b in all_blocks}
    ranks = {b: (len(focus_blocks) if b in focus_blocks else len(all_blocks)) for b in all_blocks}
    return {
        "focus": sorted(focus),
        "focus_blocks": sorted(focus_blocks),
        "failing_nets": sorted(failing_nets),
        "worst_ranks": ranks,
        "metric": _metric(ranks, sufficient),
    }


def _null_pass_rate(cases: list[tuple[list[str], int]], blocks: list[str]) -> dict:
    """**무작위 블록 순서가 이 규칙을 통과하는 비율.** 사전 등록 규칙의 검정력이다.

    왜 이것을 같이 내는가: 사전 등록의 두 번째 규칙("이 지표가 다른 답을 낼 수 있는
    조건이 있었는가")은 지표에만 적용되는 것이 아니라 **판정 자체에도** 적용된다.
    통과율이 높으면 "통과" 는 개입에 대한 증거가 아니라 기준선이 약하다는 사실의
    재진술이다 - D1 의 `0.000` 과 같은 부류를 판정 층에서 잡는 장치다.

    귀무 모형은 **동률 없는 균등 무작위 순열**이다: 블록 수가 6 이라 720개를 전수
    열거하므로 표본 오차가 없다. 개입이 낸 순서가 세 케이스에서 동일했으므로
    순열 하나를 세 케이스에 함께 적용한다 - 케이스마다 독립 순열을 뽑으면 개입보다
    자유도가 큰 모형이 되어 통과율을 과소평가한다.
    """
    import itertools

    n_pass = 0
    total = 0
    for perm in itertools.permutations(blocks):
        pos = {b: i + 1 for i, b in enumerate(perm)}
        deltas = [base - min(pos[b] for b in suf) for suf, base in cases]
        total += 1
        if all(d >= 0 for d in deltas) and sum(1 for d in deltas if d >= 1) >= 2:
            n_pass += 1
    return {
        "model": "동률 없는 균등 무작위 순열, 720개 전수 열거, 순열 하나를 세 케이스에 공통 적용",
        "n_permutations": total,
        "n_passing": n_pass,
        "pass_rate": n_pass / total,
    }


def _order_string(worst_ranks: dict[str, int]) -> str:
    groups: dict[int, list[str]] = defaultdict(list)
    for b, r in worst_ranks.items():
        groups[r].append(b)
    parts = []
    for r in sorted(groups):
        g = sorted(groups[r])
        parts.append("{" + "|".join(g) + "}" if len(g) > 1 else g[0])
    return " > ".join(parts)


# ------------------------------------------------------- 본체
def main() -> int:
    if not os.path.exists(ANSWER_KEY):
        raise SystemExit(
            f"정답표가 없다: {os.path.relpath(ANSWER_KEY, REPO)}\n"
            "먼저 `scripts/sufficient_blocks.py` 를 돌려라 - 이 스크립트는 실측 정답표만 채점한다."
        )

    key = json.load(open(ANSWER_KEY))
    spec = load_spec(SPEC)
    canonical_text = open(spec.canonical.netlist_path).read()
    structure = derive_structure(canonical_text, spec.circuit_name)
    paths = build_signal_paths(structure)
    # 최상위 스코프는 블록이 아니다 - `structure.blocks` 는 그것을 `None` 으로 담고,
    # `select_focus` 도 반환값에 담지 않는다(렌더러가 무조건 포함하므로 언제나 초점).
    all_blocks = {b for b in structure.blocks if b}

    nets_by_meas: dict[str, set[str]] = {}
    for tb in spec.testbenches:
        for name, nets in measurement_nets(tb.control_block).items():
            nets_by_meas.setdefault(name, set()).update(nets)

    out = {
        "spec": os.path.relpath(SPEC, REPO),
        "answer_key": os.path.relpath(ANSWER_KEY, REPO),
        "preregistration": (
            "docs/superpowers/specs/2026-07-29-theory-adoption-roadmap.md 「태스크 6 사전 등록」 "
            "(정답표 도착 전 커밋, edec7f6)"
        ),
        "primary_configuration": {"distance": PRIMARY[0], "aggregation": PRIMARY[1],
                                  "deck": "per_testbench"},
        "all_blocks": sorted(all_blocks),
        "cases": [],
    }

    print(f"정답표: {out['answer_key']}")
    print(f"블록 {len(all_blocks)}개: {', '.join(sorted(all_blocks))}")
    print("**1차 구성은 logdeg x min_distance x per_testbench 하나다.** 나머지는 탐색적.\n")

    for case in key["cases"]:
        shape = case["shape"]
        if not case.get("reproduced"):
            print(f"=== {shape}: 재현 실패 - 채점 불가")
            out["cases"].append({"shape": shape, "scored": False, "reason": "not_reproduced"})
            continue

        failing = case["reproduced_failing"]
        sufficient = case["sufficient_blocks"]
        states = case.get("block_state", {})
        unknown = sorted(b for b, s in states.items() if s.get("state") == "capped")

        base = _baseline(spec, structure, paths, canonical_text, failing,
                         nets_by_meas, all_blocks, sufficient)

        print(f"=== {shape} — 실패 {len(failing)}개: {', '.join(failing)}")
        print(f"    충분 블록(알려진): {', '.join(sufficient) if sufficient else '(없음)'}")
        print(f"    모르는 블록(capped): {', '.join(unknown) if unknown else '(없음)'}")
        print(f"    기준선 select_focus: {{{', '.join(base['focus_blocks'])}}} "
              f"-> 지표 {base['metric']}")

        if not sufficient:
            # 충분 블록이 하나도 없으면 어떤 순서도 지표를 낼 수 없다. 사전 등록의
            # 두 번째 규칙("이 지표가 다른 답을 낼 수 있는 조건이 있었는가")이
            # 여기서 걸린다 - D1 의 `0.000` 과 같은 모양이므로 무효로 적는다.
            print("    => 충분 블록이 없다. 이 케이스는 **무효**이고 판정에 넣지 않는다.\n")
            out["cases"].append({
                "shape": shape, "scored": False, "reason": "no_known_sufficient_block",
                "failing": failing, "unknown_blocks": unknown, "baseline": base,
            })
            continue

        cells = {}
        for deck_mode in DECK_MODES:
            for definition in DEFINITIONS:
                per_crit = {}
                unresolved = []
                for name in failing:
                    if deck_mode == "per_testbench":
                        scores, n_src = _per_crit_by_testbench(spec, name, definition)
                    else:
                        scores, n_src = _per_criterion_scores_canonical(
                            spec, name, definition, structure, paths, nets_by_meas
                        )
                    per_crit[name] = scores
                    if n_src == 0:
                        unresolved.append(name)
                for rule in RULES:
                    ranks = _worst_ranks(_block_scores(_aggregate(per_crit, rule)), all_blocks)
                    cells[f"{deck_mode}/{definition}/{rule}"] = {
                        "worst_ranks": ranks,
                        "order": _order_string(ranks),
                        "metric": _metric(ranks, sufficient),
                        "unresolved_criteria": unresolved,
                    }

        primary_key = f"per_testbench/{PRIMARY[0]}/{PRIMARY[1]}"
        prim = cells[primary_key]
        delta = None if prim["metric"] is None or base["metric"] is None \
            else base["metric"] - prim["metric"]
        print(f"    1차 구성 {primary_key}: {prim['order']}")
        print(f"      -> 지표 {prim['metric']} (기준선 {base['metric']}, "
              f"개선 {'+' if delta and delta > 0 else ''}{delta} 순위)")
        for name, cell in sorted(cells.items()):
            if name == primary_key:
                continue
            print(f"      [탐색] {name:<38} 지표 {cell['metric']}")
        print()

        out["cases"].append({
            "shape": shape, "scored": True, "failing": failing,
            "sufficient_blocks": sufficient, "unknown_blocks": unknown,
            "baseline": base, "cells": cells,
            "primary": {"key": primary_key, "metric": prim["metric"],
                        "baseline_metric": base["metric"], "improvement": delta},
        })

    # ------------------------------------------------- 사전 등록한 판정
    scored = [c for c in out["cases"] if c.get("scored")]
    deltas = [c["primary"]["improvement"] for c in scored]
    n_cases = len(scored)
    no_worse = all(d is not None and d >= 0 for d in deltas)
    n_better = sum(1 for d in deltas if d is not None and d >= 1)
    passed = n_cases == 3 and no_worse and n_better >= 2

    out["verdict"] = {
        "rule": ("1차 구성의 순위가 3건 전부에서 기준선보다 나쁘지 않고, 최소 2건에서 "
                 "최소 1 순위 이상 좋아야 통과"),
        "n_scored_cases": n_cases,
        "improvements": deltas,
        "no_case_worse": no_worse,
        "n_cases_better_by_1_or_more": n_better,
        "passed": passed,
        "meaning": ("1층은 채택 결정이 아니다. 통과는 '2층(LLM 쌍 프로브)을 돌릴 값이 "
                    "있다'는 뜻이고, 불통과는 '거리 기반 블록 순서에 2층을 걸 근거가 "
                    "없다'는 뜻이다."),
    }
    if scored:
        out["verdict"]["null_pass_rate"] = _null_pass_rate(
            [(c["sufficient_blocks"], c["primary"]["baseline_metric"]) for c in scored],
            sorted(all_blocks),
        )
    if n_cases != 3:
        out["verdict"]["void_reason"] = (
            f"채점 가능한 케이스가 3건이 아니라 {n_cases}건이다. 사전 등록의 최소 효과 "
            "크기가 3건을 전제하므로 이 판정은 규칙을 만족시킬 수 없다 - 무효다."
        )

    print("=" * 72)
    print(f"사전 등록 규칙: {out['verdict']['rule']}")
    print(f"채점된 케이스 {n_cases}건, 개선 {deltas}")
    print(f"  3건 전부 나쁘지 않다: {no_worse} / 1 순위 이상 좋아진 케이스: {n_better}")
    print(f"  => 1층 {'통과' if passed else '불통과'}")
    null = out["verdict"].get("null_pass_rate")
    if null:
        print(f"\n  검정력: 무작위 순서 {null['n_permutations']}개 중 {null['n_passing']}개가 "
              f"같은 규칙을 통과한다 (**{null['pass_rate']:.1%}**).")
        print("  => 통과율이 높으면 '통과' 는 개입의 증거가 아니라 기준선이 약하다는")
        print("     사실의 재진술이다. 이 숫자를 판정과 함께 읽어야 한다.")
    if n_cases != 3:
        print(f"  !! {out['verdict']['void_reason']}")
    print(f"  {out['verdict']['meaning']}")

    with open(OUT, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, default=repr)
    print(f"\nwrote {os.path.relpath(OUT, REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
