#!/usr/bin/env python3
"""세 집계 규칙이 **서로 다른 답을 낼 수 있는지** 착수 전에 잰다. 시뮬레이션·LLM 없음.

**왜 이것을 먼저 재는가.** 태스크 6(노브 순위의 그래프 사전확률)의 착수 조건은
"실패 criterion 이 2개 이상이고 서로 다른 블록을 가리키는 케이스 3건" 이었고
`scripts/multi_block_failures.py` 가 그것을 채웠다. 그런데 조건이 채워졌다는 것은
**잴 수 있다**는 뜻일 뿐이다. 이 저장소가 세 번 지불한 실수가 정확히 그 자리다:

- D1 의 반복 제안률이 `0.000` 을 낸 것은 기준선 런에 실패 이벤트가 **0건**이라
  그 지표가 다른 값을 낼 수 있는 조건이 아예 없었기 때문이다.
- 첫 ε 타당성 측정의 "0 of 22" 도 같은 모양이다.
- 단계 1 의 부분모듈 최대피복은 피복 집합들이 서로소여서 탐욕이 정확히 최적이라
  사전 등록 규칙("같은 피복률에서 시뮬레이션 감소")을 만족할 경우가 **없었다** -
  그것은 착수 전에 잡혔고, 그것이 이 스크립트와 같은 종류의 확인이었다.

그래서 사전 등록을 쓰기 전에 묻는다: **min-rank / min-distance / min+sum 이 이
3건에서 실제로 다른 블록 순위를 내는가.** 아니면 "집계 규칙을 고르는 실험" 은
답이 이미 정해진 실험이고, 그 사실은 설계 전에 알아야 한다.

**왜 교란 덱을 만들지 않는가.** 교란은 `W`/`l` 값만 바꾸고 연결을 바꾸지 않으므로
거리 그래프가 진입 덱과 **동일하다**. 그래서 필요한 것은 실패 criterion 목록뿐이고,
그것은 `multi_block_failures.py` 가 이미 실측해 두었다.

사용:

    .venv/bin/python scripts/aggregation_discriminates.py
"""

import json
import os
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
sys.path.insert(0, _HERE)

from analogcoder.control_block import measurement_nets
from analogcoder.signal_path import build_signal_paths
from analogcoder.spec import load_spec
from analogcoder.structure import derive_structure

# **채점기를 새로 쓰지 않는다.** 거리 정의 세 개와 순위 밴드 계산은 태스크 6 이
# 이미 만들어 커밋했다. 손으로 다시 만들면 `compose.py` 가 `netlist.py` 의 파싱
# 규칙을 베껴 두 방향으로 갈라진 것과 같은 모양이 된다(T13 이 같은 이유로 세
# 스크립트의 판정 술어 중복을 없앴다).
from score_knob_distance import (  # noqa: E402
    INF,
    BipartiteGraph,
    SignalFlowGraph,
    rank_bands,
)

REPO = os.path.dirname(_HERE)
SPEC = os.path.join(REPO, "benchmarks", "bandgap", "spec_corner_reduction.yaml")
CASES = os.path.join(
    REPO, "docs", "superpowers", "specs", "2026-07-30-multi-block-failure-cases.json"
)

DEFINITIONS = ("hop", "logdeg", "signal_flow")


def _per_criterion_scores(spec, criterion_name, definition):
    """한 criterion 의 측정 넷에서 모든 (refdes,param) 까지의 거리.

    `score_knob_distance.score_case` 와 같은 규약을 지킨다 - **넷이 아닌 이름은
    버린다.** 소자의 `nodes[:2]` 로 치환하는 구제는 실측으로 순위를 무작위보다
    나쁘게 만들었고(로드맵 부정 2), 그것은 넷이 아닌 것을 넷으로 바꾸는 추측이라
    이 저장소가 금지한 부류다.
    """
    crit = next(c for tb in spec.testbenches for c in tb.criteria if c.name == criterion_name)
    testbench = next(tb for tb in spec.testbenches if crit in tb.criteria)
    deck = open(testbench.netlist_path).read()
    structure = derive_structure(deck, spec.circuit_name)
    paths = build_signal_paths(structure)
    names = sorted(measurement_nets(testbench.control_block).get(crit.measurement, set()))

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


def _block_of(refdes: str) -> str:
    """`SUBCKT.refdes` 의 스코프. 최상위 소자는 스코프가 없다."""
    return refdes.rsplit(".", 1)[0] if "." in refdes else ""


def _aggregate(per_crit: dict[str, dict[str, float]], rule: str) -> dict[str, float]:
    """세 집계 규칙. 낮을수록 좋다(거리이므로).

    - `min_distance`: 어느 실패 criterion 에서든 가장 가까운 거리.
    - `min_rank`: criterion 별로 순위를 매기고 그중 가장 좋은 순위. 거리의
      **척도**를 지우므로 criterion 사이의 단위 차이에 둔감하다.
    - `min_sum`: min 을 1차 키로 쓰고 합을 2차 키로 쓴다. 여러 criterion 에서
      동시에 가까운 소자를 하나에서만 가까운 소자보다 앞에 세운다.
    """
    devices = set()
    for scores in per_crit.values():
        devices |= set(scores)

    if rule == "min_distance":
        return {d: min(s.get(d, INF) for s in per_crit.values()) for d in devices}

    if rule == "min_rank":
        ranks: dict[str, list[int]] = defaultdict(list)
        for scores in per_crit.values():
            bands = rank_bands({(d,): v for d, v in scores.items()})
            for (d,), band in bands.items():
                ranks[d].append(band[1])   # 동률을 깨지 않는 최악 순위
        return {d: float(min(ranks[d])) if ranks[d] else INF for d in devices}

    if rule == "min_sum":
        out = {}
        for d in devices:
            vals = [s.get(d, INF) for s in per_crit.values()]
            finite = [v for v in vals if v != INF]
            # min 이 1차, 합이 2차. 합을 아주 작은 가중치로 더해 1차 키를 넘지
            # 않게 한다 - 정렬 키 두 개를 스칼라 하나로 접는 표준 수법이고,
            # 여기서는 동률 구조를 보려는 것이므로 접어도 정보가 안 준다.
            base = min(vals) if vals else INF
            if base == INF:
                out[d] = INF
            else:
                out[d] = base + 1e-6 * (sum(finite) if finite else 0.0)
        return out

    raise ValueError(rule)


def _block_order(scores: dict[str, float]) -> list[str]:
    """블록 순위. **블록의 점수는 그 안 소자의 최솟값이다.**

    정답표가 블록 단위로만 신뢰할 수 있다고 적혀 있으므로(로드맵: "교란한 자리가
    곧 고쳐야 할 자리는 아니다" - 같은 블록 안 다른 소자로 고친 실측이 있다)
    비교도 블록 단위로 한다. 도달 못 한 블록(전부 INF)은 목록에서 **뺀다** -
    순위가 없는 것을 꼴찌라고 적으면 없는 사실을 지어내는 것이다.
    """
    best: dict[str, float] = {}
    for refdes, v in scores.items():
        block = _block_of(refdes)
        if not block:
            continue
        if block not in best or v < best[block]:
            best[block] = v
    reachable = {b: v for b, v in best.items() if v != INF}
    # 동률은 깨지 않는다 - 이름으로 2차 정렬하면 정렬 순서가 산출물로 둔갑한다.
    # 대신 동률 그룹을 괄호로 묶어 문자열로 낸다.
    groups: dict[float, list[str]] = defaultdict(list)
    for b, v in reachable.items():
        groups[v].append(b)
    out = []
    for v in sorted(groups):
        out.append("{" + "|".join(sorted(groups[v])) + "}" if len(groups[v]) > 1 else groups[v][0])
    return out


def main() -> int:
    spec = load_spec(SPEC)
    cases = json.load(open(CASES))
    qualifying = [r for r in cases["rows"] if r.get("qualifies")]
    if len(qualifying) != 3:
        raise SystemExit(
            f"자격 케이스가 3건이 아니라 {len(qualifying)}건이다 - "
            f"{os.path.relpath(CASES, REPO)} 가 바뀌었다면 이 스크립트의 전제를 다시 읽어라"
        )

    print(f"스펙: {os.path.relpath(SPEC, REPO)}")
    print(f"케이스: {', '.join(r['shape'] for r in qualifying)}\n")
    print("**교란 덱을 만들지 않는다** - 교란은 W/l 값만 바꾸므로 거리 그래프가")
    print("진입 덱과 동일하다. 실패 criterion 목록만 실측에서 가져온다.\n")

    differing = 0
    total = 0
    for row in qualifying:
        shape = row["shape"]
        failing = row["failing"]
        print(f"=== {shape} — 실패 {len(failing)}개: {', '.join(failing)}")
        for definition in DEFINITIONS:
            per_crit = {}
            unresolved = []
            for name in failing:
                scores, n_src = _per_criterion_scores(spec, name, definition)
                per_crit[name] = scores
                if n_src == 0:
                    unresolved.append(name)
            orders = {rule: _block_order(_aggregate(per_crit, rule))
                      for rule in ("min_distance", "min_rank", "min_sum")}
            distinct = {tuple(v) for v in orders.values()}
            total += 1
            same = len(distinct) == 1
            if not same:
                differing += 1
            mark = "규칙이 갈린다" if not same else "세 규칙이 같은 순위"
            print(f"  [{definition}] {mark}"
                  + (f"  (소스 넷 없는 criterion: {', '.join(unresolved)})" if unresolved else ""))
            for rule, order in orders.items():
                print(f"      {rule:<13} {' > '.join(order) if order else '(도달한 블록 없음)'}")
        print()

    print(f"판정: (케이스 x 거리정의) {total}칸 중 **{differing}칸**에서 세 집계 규칙이")
    print("갈린다.")
    if differing == 0:
        print("""
  => 집계 규칙을 고르는 실험은 **답이 이미 정해져 있다.** 세 규칙이 이 3건에서
     같은 블록 순위를 내므로, 어느 것을 고르든 측정 결과가 같다. 착수 조건이
     이름으로만 채워진 것이고, 그 사실은 사전 등록을 쓰기 전에 알아야 한다 -
     D1 의 `0.000` 과 같은 부류다.""")
    else:
        print(f"""
  => 집계 규칙은 **판별 가능하다**({differing}/{total}). 사전 등록이 값을 갖는다.
     다만 이것은 "규칙들이 서로 다른 답을 낼 수 있다"는 것뿐이고, "어느 규칙이
     옳은가" 는 정답표가 답한다 - 그리고 이 3건의 정답표는 **교란 지점**이므로
     블록 단위로만, 그리고 약한 증거로만 읽어야 한다.""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
