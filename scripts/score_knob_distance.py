#!/usr/bin/env python3
"""단계 0 · 태스크 6 채점기 — 그래프 거리 노브 사전확률을 정답표에 대고 잰다.

이 스크립트는 **장치**이지 결과가 아니다. `tests/fixtures/task6_ground_truth.json`
과 커밋된 벤치마크 덱만 읽고, LLM 도 ngspice 도 쓰지 않는다. 클린 체크아웃에서
같은 숫자가 나와야 하며, 나오지 않으면 그것이 결함이다.

세 거리 정의를 열거값으로 받는다. 셋 다 2026-07-29 의 조사가 실제로 돌린
정의이고, 여기 옮긴 이유는 그 조사가 보고한 숫자가 그 세션과 함께 사라지지
않게 하기 위해서다.

    hop          무방향 넷–소자 이분 그래프의 홉 수. 채택 후보.
    logdeg       같은 그래프, 노드 가중치 ln(deg). 자유 파라미터 0개.
    signal_flow  단자 역할이 방향을 주는 그래프에서의 역방향 BFS
                 (mode="source_bidir"). 기각된 정의 — 남긴 이유는 §기각 근거.

공통 규약 (셋 다):
  * 그래프는 **실패 criterion 이 측정되는 그 테스트벤치 덱**에서 만든다.
    정본 덱이 아니다 — trim_pm 은 netlist_loops.cir, buf0_droop 은
    netlist_settling.cir 이다.
  * 노브 인덱스(분모)는 정본 덱의 `structure.tunable` 이다. bandgap 의 여섯 덱은
    모두 같은 167 개를 내므로 이 선택은 이 정답표에서 무해하다.
  * `bulk` 단자는 엣지를 만들지 않는다. `structure.py` 가 이미 같은 이유로 같은
    분류를 한다 ("drive 로 묶으면 모든 블록이 vss 를 구동하게 된다").
  * 순위는 **동률을 깨지 않는다**. worst rank = 자기와 동률인 노브를 전부 자기
    앞에 세운 값. 같은 소자의 모든 param 은 정의상 동거리이므로 top-1 은 외부
    정보 없이 원리적으로 도달 불가하다.

사용:
    .venv/bin/python scripts/score_knob_distance.py            # 세 정의 전부
    .venv/bin/python scripts/score_knob_distance.py -d hop
    .venv/bin/python scripts/score_knob_distance.py --json
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import sys
from collections import defaultdict, deque

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from analogcoder.control_block import measurement_nets  # noqa: E402
from analogcoder.signal_path import build_signal_paths  # noqa: E402
from analogcoder.spec import load_spec  # noqa: E402
from analogcoder.structure import derive_structure  # noqa: E402
from analogcoder.structure_view import select_focus  # noqa: E402

GROUND_TRUTH = os.path.join(REPO, "tests", "fixtures", "task6_ground_truth.json")

DEFINITIONS = ("hop", "logdeg", "signal_flow")

INF = float("inf")


# --------------------------------------------------------------------- union-find
class UnionFind:
    """(scope, local_net) 를 인스턴스 포트 사상으로 병합해 계층을 평탄화한다."""

    def __init__(self) -> None:
        self._parent: dict[tuple, tuple] = {}

    def find(self, x):
        self._parent.setdefault(x, x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


def _merge_nets(paths):
    """포트 사상으로 넷을 병합하고, 병합할 수 없었던 인스턴스를 함께 돌려준다.

    `paths.net_blocks` 는 쓰지 않는다 — 그것은 최상위 넷만 담아서 bandgap 에서
    키가 11 개인데 실제 병합된 넷 클래스는 55~56 개다. 여섯 블록을 잇는 바이어스
    레일이 통째로 빠진다.
    """
    uf = UnionFind()
    instance_refdes: set[str] = set()
    disconnected: list[str] = []
    for edge in paths.instances:
        instance_refdes.add(edge.instance_refdes)
        scope = edge.instance_refdes.rpartition(".")[0] or None
        if edge.mismatch is not None or not edge.port_nets:
            # 포트 수 불일치. 그 아래는 그래프에서 분리된다 — 조용히 "가장 먼
            # 노브" 가 되지 않도록 호출자에게 알린다.
            disconnected.append(edge.instance_refdes)
            continue
        for port, outer_net in edge.port_nets.items():
            uf.union((scope, outer_net), (edge.definition, port))
    return uf, instance_refdes, disconnected


# ------------------------------------------------------- hop / logdeg 그래프
class BipartiteGraph:
    """무방향 넷–소자 이분 그래프. `hop` 과 `logdeg` 가 공유한다."""

    def __init__(self, structure, paths):
        uf, instance_refdes, self.disconnected = _merge_nets(paths)
        self.uf = uf
        self.adj: dict[tuple, set[tuple]] = defaultdict(set)
        self.devices: set[str] = set()

        for scope, block in structure.blocks.items():
            for fact in block.components:
                if fact.refdes in instance_refdes:
                    # 해소된 서브회로 인스턴스는 노드가 아니라 **엣지**다
                    # (위 union). `ctype == "X"` 를 인스턴스로 읽으면 sky130
                    # 프리미티브가 전부 X 라서 bandgap 이 91 소자가 아니라
                    # 4 소자가 된다.
                    continue
                dev = ("DEV", fact.refdes)
                self.devices.add(fact.refdes)
                for net in self._terminal_nets(fact):
                    rep = ("NET", uf.find((scope, net)))
                    self.adj[dev].add(rep)
                    self.adj[rep].add(dev)

    @staticmethod
    def _terminal_nets(fact) -> list[str]:
        if fact.terminals:
            return [net for t, net in zip(fact.terminals, fact.nodes) if t.role != "bulk"]
        if fact.ctype in ("V", "I"):
            # SPICE 는 독립 소스의 앞 두 위치 토큰이 단자임을 보장한다.
            # 자르지 않으면 값 토큰 `DC` 가 가짜 넷이 되어 루프브레이크 소스가
            # 한 점에 묶인다. `signal_path._supply_nets` 가 이미 같은 절단을 쓴다.
            return list(fact.nodes[:2])
        return list(fact.nodes)

    def net_node(self, scope, name) -> tuple:
        return ("NET", self.uf.find((scope, name)))

    def distances(self, sources: list[tuple], weight: str) -> dict[tuple, float]:
        if weight == "hop":
            def w(u):
                return 1.0 if u[0] == "DEV" else 0.0
        elif weight == "logdeg":
            def w(u):
                return math.log(max(len(self.adj[u]), 1))
        else:  # pragma: no cover - argparse restricts this
            raise ValueError(weight)

        dist: dict[tuple, float] = {}
        seen: set[tuple] = set()
        counter = 0
        pq = []
        for s in sources:
            if s in self.adj:
                pq.append((0.0, counter, s))
                counter += 1
        heapq.heapify(pq)
        while pq:
            d, _, u = heapq.heappop(pq)
            if u in seen:
                continue
            seen.add(u)
            dist[u] = d
            for v in sorted(self.adj[u], key=repr):
                if v not in seen:
                    heapq.heappush(pq, (d + w(u), counter, v))
                    counter += 1
        return dist


# ---------------------------------------------------------- signal_flow 그래프
class SignalFlowGraph:
    """단자 역할이 방향을 주는 그래프. 실패 넷에서 **거슬러** 올라간다.

    sense = 넷->소자, drive = 소자->넷, bulk = 엣지 없음. sense 단자가 하나도
    없는 소자(R/C/L/D 와 이 저장소 단자표상의 BJT)는 입출력을 구별할 근거가
    없으므로 drive 단자를 양방향으로 둔다. mode="source_bidir" 은 거기에 더해
    MOS 의 `s` 단자를 양방향으로 둔다 — 소스는 소스팔로워에서는 출력이고
    공통게이트/캐스코드/축퇴에서는 입력이라 방향이 하나로 정해지지 않는다.
    단자 **이름** `s` 는 SPICE 위치 순서(d g s b)가 보장하는 파싱된 사실이므로
    넷 이름 추측과 다르다.
    """

    def __init__(self, structure, paths):
        uf, _instances, self.disconnected = _merge_nets(paths)
        self.uf = uf
        self.rev: dict[object, set[object]] = defaultdict(set)
        self.devices: set[str] = set()

        for scope, block in structure.blocks.items():
            for fact in block.components:
                if not fact.terminals:
                    # 단자표가 없는 것: 최상위 V/I 와 서브회로 인스턴스.
                    # 인스턴스는 넷 병합으로 이미 녹았다. V/I 는 nodes 에 값
                    # 토큰이 섞이는 것이 알려진 사실이라 노드를 만들지 않는다.
                    continue
                dev = fact.refdes
                self.devices.add(dev)
                has_sense = any(t.role == "sense" for t in fact.terminals)
                for terminal, local in zip(fact.terminals, fact.nodes):
                    n = uf.find((scope, local))
                    if terminal.role == "bulk":
                        continue
                    if terminal.role == "sense":
                        self.rev[dev].add(n)          # fwd: net -> dev
                    elif terminal.role == "drive":
                        self.rev[n].add(dev)          # fwd: dev -> net
                        if not has_sense or terminal.name == "s":
                            self.rev[dev].add(n)

    def distances(self, source_nets: list[str]) -> dict[object, float]:
        dist: dict[object, float] = {}
        q: deque = deque()
        for name in source_nets:
            n = self.uf.find((None, name))
            if n not in dist:
                dist[n] = 0.0
                q.append(n)
        while q:
            u = q.popleft()
            for v in self.rev.get(u, ()):
                if v not in dist:
                    dist[v] = dist[u] + 1.0
                    q.append(v)
        return dist


# ------------------------------------------------------------------- 순위
def rank_bands(scores: dict[tuple, float]) -> dict[tuple, tuple[int, int, int]]:
    """(refdes, param) -> (best_rank, worst_rank, tie_group_size), 1-기반.

    동률은 깨지 않는다. 이름으로 2차 정렬하면 그 순간 정렬 순서가 산출물로
    둔갑한다.
    """
    counts: dict[float, int] = defaultdict(int)
    for v in scores.values():
        counts[v] += 1
    running = 0
    best: dict[float, int] = {}
    worst: dict[float, int] = {}
    for d in sorted(counts):
        best[d] = running + 1
        running += counts[d]
        worst[d] = running
    return {k: (best[v], worst[v], counts[v]) for k, v in scores.items()}


def repair_set_worst_rank(bands, repair_sets) -> tuple[int | None, list | None]:
    """정답표의 결정 지표. 낮을수록 좋다.

    수리 집합 하나가 '완비되는' 순위는 그 집합에서 가장 늦게 들어오는 노브의
    순위다. 여러 집합이 있으면 그중 가장 빨리 완비되는 것을 쓴다.
    """
    best_rank: int | None = None
    best_set = None
    for rset in repair_sets:
        ranks = []
        for refdes, param in rset:
            band = bands.get((refdes, param))
            if band is None:
                ranks = None
                break
            ranks.append(band[1])
        if not ranks:
            continue
        completed = max(ranks)
        if best_rank is None or completed < best_rank:
            best_rank, best_set = completed, rset
    return best_rank, best_set


# ------------------------------------------------------------------- 채점
def score_case(case: dict, definition: str) -> dict:
    spec = load_spec(os.path.join(REPO, case["spec_path"]))
    canonical = open(os.path.join(REPO, case["canonical_netlist"])).read()
    knobs = [(t.refdes, t.param) for t in derive_structure(canonical, spec.circuit_name).tunable]

    crit = case["failing_criteria"][0]
    testbench = next(t for t in spec.testbenches if t.name == crit["testbench"])
    deck = open(testbench.netlist_path).read()
    structure = derive_structure(deck, spec.circuit_name)
    paths = build_signal_paths(structure)

    # 소스 넷: 실패 measurement 를 관측하는 control-block 의 넷들. 넷이 아닌
    # 이름(전압원 이름 등)은 **버리고 기록한다** - 소자의 nodes[:2] 로 치환하지
    # 않는다. 실측으로 그 치환은 순위를 무작위보다 나쁘게 만들었다.
    names = sorted(measurement_nets(testbench.control_block).get(crit["measurement"], set()))

    if definition == "signal_flow":
        graph = SignalFlowGraph(structure, paths)
        resolved = [n for n in names if graph.uf.find((None, n)) in graph.rev]
        dist = graph.distances(resolved)
        raw = {refdes: dist.get(refdes, INF) for refdes in graph.devices}
        devices, disconnected = graph.devices, graph.disconnected
    else:
        graph = BipartiteGraph(structure, paths)
        sources = [graph.net_node(None, n) for n in names]
        resolved = [n for n, s in zip(names, sources) if s in graph.adj]
        dist = graph.distances([s for s in sources if s in graph.adj], definition)
        raw = {refdes: dist.get(("DEV", refdes), INF) for refdes in graph.devices}
        devices, disconnected = graph.devices, graph.disconnected

    scores = {k: raw.get(k[0], INF) for k in knobs}
    bands = rank_bands(scores)

    focus = select_focus(structure, paths, set(names), set(), deck)
    focus_knobs = [k for k in knobs if (k[0].rpartition(".")[0] or None) in set(focus)]

    sc = case["scoring"]
    flat = [tuple(x) for x in sc.get("correct_set_refdes_param", [])]
    forbidden = [tuple(x) for x in sc.get("must_not_be_top_ranked", [])]
    repair_sets = [[tuple(k) for k in s] for s in sc.get("repair_sets", [])]

    rs_rank, rs_set = repair_set_worst_rank(bands, repair_sets)
    if sc.get("requires_both"):
        flat_rank = max((bands[k][1] for k in flat if k in bands), default=None)
    else:
        flat_rank = min((bands[k][1] for k in flat if k in bands), default=None)

    levels = sorted({v for v in scores.values()})
    buckets: dict[float, int] = defaultdict(int)
    for v in scores.values():
        buckets[v] += 1
    max_bucket = max(buckets.values()) if buckets else 0

    return {
        "case_id": case["case_id"],
        "definition": definition,
        "n_knobs": len(knobs),
        "n_devices": len(devices),
        "n_devices_reached": sum(1 for r in devices if raw.get(r, INF) != INF),
        "source_names": names,
        "source_names_resolved": resolved,
        "source_names_unresolved": [n for n in names if n not in resolved],
        "disconnected_instances": disconnected,
        # --- 결정 지표 ---
        "repair_set_worst_rank": rs_rank,
        "repair_set_used": [list(k) for k in rs_set] if rs_set else None,
        # --- 평면 리스트 채점(원래 규약) ---
        "flat_correct_rank": flat_rank,
        "correct_knobs": {
            f"{r}.{p}": {"dist": None if scores[(r, p)] == INF else round(scores[(r, p)], 4),
                         "rank_best": bands[(r, p)][0], "rank_worst": bands[(r, p)][1],
                         "tie_group": bands[(r, p)][2]}
            for r, p in flat if (r, p) in bands
        },
        "forbidden_knobs": {
            f"{r}.{p}": {"dist": None if scores[(r, p)] == INF else round(scores[(r, p)], 4),
                         "rank_best": bands[(r, p)][0], "rank_worst": bands[(r, p)][1]}
            for r, p in forbidden if (r, p) in bands
        },
        # --- 이겨야 하는 기준선 ---
        "focus_blocks": sorted(focus),
        "focus_knob_count": len(focus_knobs),
        # --- 퇴화 진단: 지표가 아무것도 못 했을 때의 모습 ---
        "n_levels": len(levels),
        "head_bucket_size": buckets[levels[0]] if levels else 0,
        "max_bucket_size": max_bucket,
        "max_bucket_share": round(max_bucket / len(knobs), 4) if knobs else 0.0,
        "degenerate": len(levels) <= 1 or (max_bucket / len(knobs) if knobs else 0) >= 0.5,
    }


def score_negative_control(case: dict, definition: str) -> dict:
    """`scoreable: False` 인 케이스 — 노브 순위는 무효고 **블록** 판정만 유효하다.

    정답은 파라미터 튜닝이 아니라 토폴로지 스왑(BUF_P)이므로, 여기서 물을 수 있는
    것은 하나뿐이다: 거리가 스왑 대상 블록을 최근접으로 짚는가.
    """
    spec = load_spec(os.path.join(REPO, case["spec_path"]))
    crit = case["failing_criteria"][0]
    testbench = next(t for t in spec.testbenches if t.name == crit["testbench"])
    deck = open(testbench.netlist_path).read()
    structure = derive_structure(deck, spec.circuit_name)
    paths = build_signal_paths(structure)
    knobs = [(t.refdes, t.param) for t in structure.tunable]
    names = sorted(measurement_nets(testbench.control_block).get(crit["measurement"], set()))

    if definition == "signal_flow":
        graph = SignalFlowGraph(structure, paths)
        dist = graph.distances([n for n in names if graph.uf.find((None, n)) in graph.rev])
        raw = {r: dist.get(r, INF) for r in graph.devices}
    else:
        graph = BipartiteGraph(structure, paths)
        sources = [graph.net_node(None, n) for n in names]
        dist = graph.distances([s for s in sources if s in graph.adj], definition)
        raw = {r: dist.get(("DEV", r), INF) for r in graph.devices}

    by_block: dict[str, float] = {}
    for refdes, _param in knobs:
        block = refdes.rpartition(".")[0] or "(top)"
        d = raw.get(refdes, INF)
        by_block[block] = min(by_block.get(block, INF), d)
    ordered = sorted(by_block.items(), key=lambda kv: kv[1])
    nearest = [b for b, v in ordered if v == ordered[0][1]]
    return {
        "case_id": case["case_id"],
        "definition": definition,
        "source_names": names,
        "block_distances": [(b, None if v == INF else round(v, 4)) for b, v in ordered],
        "nearest_blocks": nearest,
        "expected_block": case["correct_answer"]["block"],
        "block_verdict_correct": nearest == [case["correct_answer"]["block"]],
    }


def score_optimizer_case(case: dict, definition: str, *, rescue: bool) -> dict:
    """로드맵 176 행이 태스크 6 의 A/B 지점으로 못박은 자리 — 최적화 단계의 노브 순위.

    여기에는 '실패한 criterion 의 넷' 이 없다. 목적함수 `iq_ua` 가 있을 뿐이고,
    그것을 만드는 control-block 은 `i(Vdd)` 를 읽으므로 `measurement_nets` 가
    돌려주는 것은 넷이 아니라 **전압원 이름** 이다. 그래서:

      rescue=False  소스 0 개 -> 어떤 노브도 순위가 없다.
      rescue=True   `Vdd` 를 그 소자의 nodes[:2] 로 치환한다. 채택 정의가
                    **거부하는** 치환이며, 여기서만 부정 결과를 보이기 위해 켠다.

    무작위 기대치는 분모/2 다 (167 -> ~84).
    """
    spec = load_spec(os.path.join(REPO, case["spec_path"]))
    testbench = next(
        t for t in spec.testbenches
        if case["objective"]["name"] in measurement_nets(t.control_block)
    )
    deck = open(testbench.netlist_path).read()
    structure = derive_structure(deck, spec.circuit_name)
    paths = build_signal_paths(structure)
    knobs = [(t.refdes, t.param) for t in structure.tunable]
    names = sorted(measurement_nets(testbench.control_block)[case["objective"]["name"]])

    top_level = {f.refdes: f for f in structure.blocks[None].components}
    if definition == "signal_flow":
        graph = SignalFlowGraph(structure, paths)
        source_nets, unresolved = [], []
        for n in names:
            if graph.uf.find((None, n)) in graph.rev:
                source_nets.append(n)
            elif rescue and n in top_level:
                source_nets.extend(top_level[n].nodes[:2])
                unresolved.append(f"{n}->nodes[:2]")
            else:
                unresolved.append(n)
        dist = graph.distances(source_nets)
        raw = {r: dist.get(r, INF) for r in graph.devices}
        n_devices = len(graph.devices)
    else:
        graph = BipartiteGraph(structure, paths)
        source_nodes, unresolved = [], []
        for n in names:
            node = graph.net_node(None, n)
            if node in graph.adj:
                source_nodes.append(node)
            elif rescue and n in top_level:
                for inner in top_level[n].nodes[:2]:
                    inner_node = graph.net_node(None, inner)
                    if inner_node in graph.adj:
                        source_nodes.append(inner_node)
                unresolved.append(f"{n}->nodes[:2]")
            else:
                unresolved.append(n)
        dist = graph.distances(source_nodes, definition)
        raw = {r: dist.get(("DEV", r), INF) for r in graph.devices}
        n_devices = len(graph.devices)

    scores = {k: raw.get(k[0], INF) for k in knobs}
    bands = rank_bands(scores)
    ref = (case["verified_reference_knob"]["refdes"], case["verified_reference_knob"]["param"])
    # 도달 불가는 '가장 나쁜 순위' 가 아니라 **순위 없음** 이다. 전부 도달
    # 불가일 때 모두가 동률이 되어 1..167 이라는 숫자가 나오는데, 그것을
    # 순위로 읽으면 "재 봤더니 나빴다" 가 "잴 수 없었다" 를 가린다.
    band = bands.get(ref) if scores.get(ref, INF) != INF else None
    reached = sum(1 for v in raw.values() if v != INF)
    return {
        "case_id": case["case_id"],
        "definition": definition,
        "rescue": rescue,
        "objective": case["objective"]["name"],
        "source_names": names,
        "source_names_unresolved": unresolved,
        "n_devices_reached": f"{reached}/{n_devices}",
        "reference_knob": f"{ref[0]}.{ref[1]}",
        "reference_rank_best": band[0] if band else None,
        "reference_rank_worst": band[1] if band else None,
        "n_knobs": len(knobs),
        "random_expectation": round(len(knobs) / 2, 1),
        "ranking_exists": reached > 0,
    }


def load_ground_truth(path: str = GROUND_TRUTH) -> dict:
    with open(path) as f:
        return json.load(f)


def score_all(definitions=DEFINITIONS, path: str = GROUND_TRUTH) -> list[dict]:
    gt = load_ground_truth(path)
    out = []
    for case in gt["cases"]:
        if not case.get("scoreable") or "failing_criteria" not in case:
            continue
        if not case["scoring"].get("repair_sets"):
            # 최적화 단계 케이스: 실패 criterion 이 없어 이 지표의 소스가 없다.
            continue
        for definition in definitions:
            out.append(score_case(case, definition))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-d", "--definition", choices=(*DEFINITIONS, "all"), default="all")
    ap.add_argument("--json", action="store_true", help="원시 레코드를 JSON 으로")
    ap.add_argument("--ground-truth", default=GROUND_TRUTH)
    args = ap.parse_args()

    defs = DEFINITIONS if args.definition == "all" else (args.definition,)
    rows = score_all(defs, args.ground_truth)

    if args.json:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
        return 0

    by_case: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        by_case[r["case_id"]][r["definition"]] = r

    print("결정 지표: 수리 집합이 완비되는 최악 순위 (낮을수록 좋다).")
    print("기준선: select_focus 후보 수 — 이미 있는 결정론적 초점.\n")
    head = f"{'case':32s} {'분모':>5s} {'focus':>6s}"
    for d in defs:
        head += f" {d:>12s}"
    print(head)
    print("-" * len(head))
    for case_id, per_def in by_case.items():
        any_row = next(iter(per_def.values()))
        line = f"{case_id:32s} {any_row['n_knobs']:5d} {any_row['focus_knob_count']:6d}"
        for d in defs:
            r = per_def.get(d)
            line += f" {('-' if r is None or r['repair_set_worst_rank'] is None else r['repair_set_worst_rank']):>12}"
        print(line)

    print("\n세부:")
    for case_id, per_def in by_case.items():
        print(f"\n## {case_id}")
        any_row = next(iter(per_def.values()))
        print(f"   소스 넷 {any_row['source_names']} "
              f"(해소 {any_row['source_names_resolved']}, "
              f"미해소 {any_row['source_names_unresolved']})")
        print(f"   focus {any_row['focus_blocks']} -> {any_row['focus_knob_count']}/{any_row['n_knobs']} 노브")
        for d in defs:
            r = per_def.get(d)
            if r is None:
                continue
            print(f"   [{d}] 수리집합 최악순위={r['repair_set_worst_rank']} "
                  f"(집합 {r['repair_set_used']}) 평면채점={r['flat_correct_rank']} "
                  f"도달 {r['n_devices_reached']}/{r['n_devices']}")
            for name, v in r["correct_knobs"].items():
                print(f"        정답  {name:24s} d={v['dist']} rank {v['rank_best']}..{v['rank_worst']} (동률 {v['tie_group']})")
            for name, v in r["forbidden_knobs"].items():
                print(f"        금지  {name:24s} d={v['dist']} rank {v['rank_best']}..{v['rank_worst']}")
            print(f"        레벨수={r['n_levels']} 머리버킷={r['head_bucket_size']} "
                  f"최대버킷={r['max_bucket_size']}({r['max_bucket_share']}) "
                  f"퇴화={r['degenerate']} 분리인스턴스={r['disconnected_instances']}")

    gt = load_ground_truth(args.ground_truth)

    print("\n\n== 음성 대조군 (노브 순위 무효, 블록 판정만) ==")
    for case in gt["cases"]:
        if case.get("scoreable") or "correct_answer" not in case:
            continue
        for d in defs:
            r = score_negative_control(case, d)
            print(f"  {r['case_id']} [{d}] 최근접블록={r['nearest_blocks']} "
                  f"기대={r['expected_block']} 옳음={r['block_verdict_correct']}")
            print(f"     블록거리 {r['block_distances']}")

    print("\n\n== 로드맵 176 행의 A/B 지점 (최적화 단계 노브 순위) ==")
    print("   무작위 기대치보다 나쁘면 로드맵의 조건문에 따라 LLM 호출을 없앨 수 없다.")
    for case in gt["cases"]:
        if case.get("phase") != "optimization":
            continue
        for rescue in (False, True):
            for d in defs:
                r = score_optimizer_case(case, d, rescue=rescue)
                label = "Vdd->nodes[:2] 구제" if rescue else "구제 없음"
                rank = ("순위 없음" if not r["ranking_exists"]
                        else f"{r['reference_rank_best']}..{r['reference_rank_worst']}/{r['n_knobs']}")
                print(f"  [{label:18s}] {d:12s} 도달 {r['n_devices_reached']:>7s} "
                      f"{r['reference_knob']} rank={rank} "
                      f"(무작위 기대 ~{r['random_expectation']}) 미해소={r['source_names_unresolved']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
