#!/usr/bin/env python3
"""단계 1(부분모듈 최대피복) 타당성 측정 - 구현 전에 '발화할 수 있는가'를 먼저 잰다.

세 가지를 잰다. 전부 **진입 스윕 데이터 하나**로 되고 추가 시뮬레이션이 없다.

A. epsilon 을 스캔했을 때 씨앗이 실제로 줄어드는가.
   argmax 피복 관계에서 집합들은 서로소이므로(각 기준의 argmax 는 하나뿐)
   피복 함수가 가법적이고 탐욕이 정확히 최적이다 - 부분모듈성이 내용을 갖지
   않는다. epsilon-근접 피복은 집합을 겹치게 만들어 그 자리를 살리려는 것이고,
   **겹침이 실제로 생기지 않으면 단계 1은 시작 전에 죽는다.**

B. 씨앗이 줄면 벽시계가 줄어드는가. 중간 루프는 코너를 병렬로 돌리되
   테스트벤치는 병렬 바깥이므로, 비용은 코너 수가 아니라
   `테스트벤치 수 x ceil(테스트벤치당 점 / 워커)` 로 움직인다. 점 수가 이미
   워커 수 안에 들어가면 코너를 줄여도 벽시계는 그대로다.

C. argmax 씨앗이 잡던 위반을 epsilon 씨앗이 놓치는가. 한 건이라도 놓치면
   사전 등록 규칙상 불채택이다.

**C 는 진입 덱에서 재면 구조적으로 0 만 낸다.** 이 저장소의 코너 선언 스펙은
전 코너에서 통과하도록 만들어져 있어 놓칠 위반이 애초에 없다 - D1 의 반복제안률
0.000 과 같은 자리다. 중간 루프는 튜너가 **움직인 덱**에서 도므로 재는 자리도
거기이고, 그래서 이 스크립트는 교란 폭을 인자로 받는다.

사용:

    .venv/bin/python scripts/coverage_feasibility.py benchmarks/bandgap/spec_pvt.yaml
    .venv/bin/python scripts/coverage_feasibility.py benchmarks/bandgap/spec_pvt.yaml 4 3 2

두 번째 형태는 두 증폭기의 테일 폭(`TRIMAMP.Xt.W` / `BUF_P.Xt.W`)을 8 에서 그
값들로 줄인 덱 각각에 대해 잰다. 설계 문서:
`docs/superpowers/specs/2026-07-29-theory-combination-evaluation-design.md`
"""

import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
sys.path.insert(0, _HERE)

from analogcoder.pvt import all_corners, run_full_pvt_sweep
from analogcoder.netlist import apply_changes, resolve_includes
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.simulators.cache import CachingSimulator
from analogcoder.spec import load_spec

WORSE_IS_SMALLER = {">=", ">"}


def _worse(op):
    """이 연산자에서 '더 나쁜' 방향. >= 면 작을수록 나쁘다."""
    return min if op in WORSE_IS_SMALLER else max


def _violates(value, op, threshold):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return True  # 측정값이 안 나온 것은 가장 강한 위반 증거다
    if op == ">=":
        return not (value >= threshold)
    if op == ">":
        return not (value > threshold)
    if op == "<=":
        return not (value <= threshold)
    if op == "<":
        return not (value < threshold)
    raise ValueError(f"unknown operator {op!r}")


def build_table(sweep, criteria):
    """{기준 이름: [코너별 값]} 과 코너 라벨."""
    per = sweep["per_corner"]
    labels = [json.dumps(entry["corner"], sort_keys=True) for entry in per]
    table = {}
    for crit in criteria:
        values = []
        for entry in per:
            values.append(entry["measurements"].get(crit.measurement))
        table[crit.name] = values
    return table, labels


def covering_sets(table, criteria, eps):
    """코너 index -> 그 코너가 epsilon 이내로 덮는 기준 이름 집합.

    '덮는다' = 그 코너에서의 값이 이 기준의 최악값으로부터 상대 epsilon 이내.
    측정값이 없는(None/NaN) 코너는 그 기준의 최악이고, 값이 있는 어떤 코너도
    그것을 덮지 못한다 - 회로가 거기서 동작하지 않는다는 사실은 근사되지
    않는다.
    """
    n_corners = len(next(iter(table.values())))
    sets = {i: set() for i in range(n_corners)}
    for crit in criteria:
        values = table[crit.name]
        missing = [i for i, v in enumerate(values)
                   if v is None or (isinstance(v, float) and math.isnan(v))]
        if missing:
            for i in missing:
                sets[i].add(crit.name)
            continue
        worst = _worse(crit.operator)(values)
        scale = abs(worst) if worst else 1.0
        for i, v in enumerate(values):
            if abs(v - worst) <= eps * scale:
                sets[i].add(crit.name)
    return sets


def greedy(sets, universe, tau=1.0):
    """탐욕 최대피복. tau 비율을 덮는 즉시 멈춘다. 고른 코너 index 목록."""
    target = math.ceil(tau * len(universe))
    covered, chosen = set(), []
    remaining = dict(sets)
    while len(covered) < target:
        best, gain = None, 0
        for i, s in remaining.items():
            g = len(s - covered)
            if g > gain or (g == gain and best is not None and g > 0 and i < best):
                best, gain = i, g
        if best is None or gain == 0:
            break
        chosen.append(best)
        covered |= remaining.pop(best)
    return chosen, covered


def argmax_seed(table, criteria):
    """오늘의 씨앗: 기준별 argmax 코너의 합집합."""
    chosen = []
    for crit in criteria:
        values = table[crit.name]
        missing = [i for i, v in enumerate(values)
                   if v is None or (isinstance(v, float) and math.isnan(v))]
        idx = missing[0] if missing else values.index(_worse(crit.operator)(values))
        if idx not in chosen:
            chosen.append(idx)
    return chosen


def caught_violations(seed, table, criteria):
    """이 씨앗이 실제로 잡아내는 위반 기준의 집합."""
    caught = set()
    for crit in criteria:
        values = table[crit.name]
        for i in seed:
            if _violates(values[i], crit.operator, crit.threshold):
                caught.add(crit.name)
                break
    return caught


def waves(points_per_tb, n_tb, workers):
    """테스트벤치-바깥/코너-안쪽 구조에서의 병렬 웨이브 수."""
    return n_tb * math.ceil(points_per_tb / workers)


def analyse(spec_path, workers, perturb=None):
    """perturb: [{"refdes":..,"param":..,"new_value":..}] - 덱을 움직인 뒤 잰다.

    **진입 덱에서 재면 C(놓친 위반)는 구조적으로 0만 낼 수 있다.** 두 벤치마크
    다 전 코너에서 통과하도록 만들어졌으므로 놓칠 위반이 애초에 없다. 중간
    루프는 튜너가 움직인 덱에서 도는 것이 정상이므로, 재는 자리도 거기다.
    """
    spec = load_spec(spec_path)
    texts = {}
    for tb in spec.testbenches:
        base = os.path.dirname(os.path.abspath(tb.netlist_path))
        text = resolve_includes(open(tb.netlist_path).read(), base)
        if perturb:
            text = apply_changes(text, perturb)
        texts[tb.name] = text

    backend = CachingSimulator(NgspiceBackend())
    sweep = run_full_pvt_sweep(texts, spec, backend)

    criteria = list(spec.all_criteria)
    table, labels = build_table(sweep, criteria)
    n_corners = len(labels)
    n_tb = len(spec.testbenches)

    base_seed = argmax_seed(table, criteria)
    base_caught = caught_violations(base_seed, table, criteria)

    tag = "" if not perturb else "  [perturbed: " + ", ".join(
        f"{c['refdes']}.{c['param']}={c['new_value']}" for c in perturb) + "]"
    print(f"\n{'='*78}\n{os.path.basename(spec_path)}: "
          f"{n_corners} corners x {len(criteria)} criteria x {n_tb} testbenches{tag}")
    print(f"  sweep overall_pass       : {sweep['overall_pass']}")
    print(f"  argmax seed (today)      : {len(base_seed)} corners "
          f"-> {len(base_seed)+2} points/tb (+NOMINAL +probe), "
          f"{waves(len(base_seed)+2, n_tb, workers)} waves")
    print(f"  violations caught by it  : {len(base_caught)} of {len(criteria)} criteria")
    print(f"  full grid every iteration: {n_corners} points/tb, "
          f"{waves(n_corners, n_tb, workers)} waves")

    print(f"\n  {'eps':>8} | {'|seed|':>6} | {'cover':>6} | {'pts/tb':>6} | "
          f"{'waves':>5} | {'missed violations':>18}")
    print(f"  {'-'*8}-+-{'-'*6}-+-{'-'*6}-+-{'-'*6}-+-{'-'*5}-+-{'-'*18}")
    rows = []
    for eps in [0.0, 1e-5, 1e-4, 1e-3, 1e-2, 3e-2, 1e-1]:
        sets = covering_sets(table, criteria, eps)
        chosen, covered = greedy(sets, set(table), tau=1.0)
        caught = caught_violations(chosen, table, criteria)
        missed = base_caught - caught
        pts = len(chosen) + 2
        rows.append({
            "eps": eps, "seed": len(chosen), "covered": len(covered),
            "points_per_tb": pts, "waves": waves(pts, n_tb, workers),
            "missed": sorted(missed),
        })
        print(f"  {eps:>8.0e} | {len(chosen):>6} | {len(covered):>6} | {pts:>6} | "
              f"{waves(pts, n_tb, workers):>5} | {len(missed):>3} "
              f"{sorted(missed)[:2] if missed else ''}")
    return {
        "spec": spec_path, "perturb": perturb, "overall_pass": sweep["overall_pass"],
        "corners": n_corners, "criteria": len(criteria),
        "testbenches": n_tb, "workers": workers,
        "argmax_seed": len(base_seed),
        "argmax_waves": waves(len(base_seed) + 2, n_tb, workers),
        "full_grid_waves": waves(n_corners, n_tb, workers),
        "violations_caught_by_argmax": len(base_caught),
        "eps_scan": rows,
    }


# 교란 모양은 `scripts/perturbations.py` 가 소유한다. 예전에는 이 파일이
# "두 증폭기의 테일 폭" 하나만 인자로 받았고, **한 종류로만 쟀다는 것이
# `2026-07-29-theory-combination-results.md` §7-8 의 명시된 한계**였다.
# 이제 이름으로 받고, `reentry_feasibility.py` 와 같은 목록을 쓴다.
from perturbations import PERTURBATIONS


if __name__ == "__main__":
    workers = (os.cpu_count() or 2) - 1
    spec_path = sys.argv[1]
    shapes = sys.argv[2:] or list(PERTURBATIONS)
    unknown = [x for x in shapes if x not in PERTURBATIONS]
    if unknown:
        raise SystemExit(
            f"unknown perturbation shape(s): {unknown}. "
            f"known: {sorted(PERTURBATIONS)}"
        )
    out = []
    for name in shapes:
        row = analyse(spec_path, workers, PERTURBATIONS[name] or None)
        row["shape"] = name
        out.append(row)
    # 산출물은 **cwd** 에 쓴다. `os.path.dirname(__file__)` 로 쓰면 저장소의
    # `scripts/` 안에 결과 파일이 쌓인다.
    dest = os.environ.get("COVERAGE_FEASIBILITY_OUT", "coverage_feasibility.json")
    with open(dest, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nwrote {dest}")
