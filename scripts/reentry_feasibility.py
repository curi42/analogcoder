#!/usr/bin/env python3
"""필요조건 3(재진입)이 **발화할 수 있는가**를 실행 전에 잰다. LLM 없음.

재진입은 둘이 동시에 성립해야 발화한다.

1. 판정(전체) 스윕이 실패하고,
2. 실패하는 기준의 **최악 코너가 중간 집합 밖**에 있을 것.

2번이 왜 어려운가: 씨앗은 기준별 argmax 의 합집합이므로, 어떤 기준의 argmax 가
집합 안에 있으면 중간 루프가 그 코너를 이미 시뮬레이션했고 거기서 먼저 실패했을
것이다. 그래서 재진입은 **argmax 가 진입 스윕 이후로 집합 밖으로 옮겨간** 기준을
필요로 한다. CLAUDE.md 는 밴드갭 9코너 격자에서 이것이 구조적으로 성립하지
않는다고 적는다 - 집합 밖 코너가 전부 `tt` 인데 그것은 아무의 최악도 아니다.

**이 스크립트는 그 주장을 여러 덱 상태에서 확인한다.** 중간 집합은 **진입** 덱의
스윕에서 뽑히고, 판정은 튜너가 **움직인** 덱에서 난다. 그래서 (진입 덱, 이동 덱)
쌍을 훑는다:

    씨앗 S = seed(sweep(D0))          # argmax 와 coverage 각각
    판정   = sweep(D1)
    발화   = ∃ 기준 c: c 가 D1 에서 실패 ∧ argmax_c(D1) ∉ S

coverage 씨앗은 argmax 씨앗보다 작으므로 **집합 밖이 더 넓고, 구조적으로 재진입이
더 잘 발화한다.** 두 팔의 재진입 동작이 같을 것이라고 기대할 근거가 없다는 뜻이고,
그것 자체가 이 측정이 답해야 할 것이다.

사용:

    .venv/bin/python scripts/reentry_feasibility.py benchmarks/bandgap/spec_pvt.yaml
    .venv/bin/python scripts/reentry_feasibility.py benchmarks/bandgap/spec_corner_reduction.yaml

45코너 격자(`spec_pvt.yaml`)가 발화 가능성이 가장 높다 - 씨앗이
`min(기준 수, 코너 수)` 로 막혀 9 근처인데 코너가 45개라 집합 밖이 36개다.
9코너 격자에서는 집합 밖이 3개뿐이다.

산출물: `reentry_feasibility.json` (cwd, 환경변수 `REENTRY_FEASIBILITY_OUT` 로 변경).
"""

import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
sys.path.insert(0, _HERE)

from analogcoder.corner_selection import coverage_seed, label
from analogcoder.netlist import apply_changes, resolve_includes
from analogcoder.pvt import run_full_pvt_sweep
from analogcoder.simulators.cache import CachingSimulator
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import CoverageConfig, load_spec

WORSE_IS_SMALLER = {">=", ">"}


# 교란 모양은 `scripts/perturbations.py` 가 소유한다 - `coverage_feasibility.py`
# 와 같은 목록을 써야 "필요조건 1을 재진입이 발화한 바로 그 덱 상태에서
# 쟀는가" 를 나중에 확인할 수 있다.
from perturbations import PERTURBATIONS


def _worse(op):
    return min if op in WORSE_IS_SMALLER else max


def _missing(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


def _violates(value, op, threshold):
    if _missing(value):
        return True  # 측정값이 안 나온 것은 가장 강한 위반 증거다
    return not {
        ">=": value >= threshold, ">": value > threshold,
        "<=": value <= threshold, "<": value < threshold,
    }[op]


def sweep_deck(spec, spec_dir, perturb, backend):
    texts = {}
    for tb in spec.testbenches:
        text = resolve_includes(open(tb.netlist_path).read(), spec_dir)
        if perturb:
            text = apply_changes(text, perturb)
        texts[tb.name] = text
    return run_full_pvt_sweep(texts, spec, backend)


def _coord_key(process, voltage, temperature):
    """코너의 **좌표**로 만든 조인 키.

    `per_corner` 의 코너 dict 는 `{process, voltage, temperature}` 만 들고
    `corner_id` 를 들지 않는다(실측). 반면 `coverage_seed` 는 `CornerPoint` 를
    돌려준다. 두 축을 `corner_id` 로 이으려던 첫 판은 키가 전부 `None` 이 되어
    **씨앗이 조용히 빈 리스트가 되고**, 그 빈 씨앗이 "재진입이 발화한다"는 답을
    만들어 냈다 - 이 저장소가 세는 무효 측정과 같은 모양이라 좌표로 잇는다."""
    return f"{process}/{voltage}/{temperature}"


def per_criterion(sweep, criteria):
    """{기준 이름: 코너별 값 리스트} 와 코너 라벨 순서."""
    per = sweep["per_corner"]
    labels = [
        _coord_key(e["corner"].get("process"), e["corner"].get("voltage"),
                   e["corner"].get("temperature"))
        for e in per
    ]
    table = {c.name: [e["measurements"].get(c.measurement) for e in per] for c in criteria}
    return table, labels


def argmax_label(values, labels, op):
    """이 기준의 최악 코너 라벨. 측정값이 없는 코너가 있으면 그 **첫** 코너 -
    `pvt.worst_case_measurements` 와 같은 규약이며, argmax 가 아니라 "여기서
    측정이 안 나왔다"는 다른 사실이다."""
    missing = [i for i, v in enumerate(values) if _missing(v)]
    if missing:
        return labels[missing[0]], True
    return labels[values.index(_worse(op)(values))], False


def seed_labels(sweep, criteria, labels, coverage=None):
    """중간 집합에 들어가는 코너 라벨 집합(NOMINAL 제외)."""
    if coverage is None:
        chosen = []
        for c in criteria:
            values = [
                e["measurements"].get(c.measurement) for e in sweep["per_corner"]
            ]
            lbl, _ = argmax_label(values, labels, c.operator)
            if lbl not in chosen:
                chosen.append(lbl)
        return chosen
    points, _record = coverage_seed(sweep, list(criteria), coverage)
    chosen = [_coord_key(p.process, p.voltage, p.temperature) for p in points]
    # **조인이 실패하면 조용히 작은 씨앗이 되고, 작은 씨앗은 "재진입이 발화한다"를
    # 자동으로 만들어 낸다.** 그래서 여기서 시끄럽게 실패한다.
    stray = [c for c in chosen if c not in labels]
    if stray:
        raise RuntimeError(
            f"coverage seed produced corner(s) not in the swept grid: {stray} "
            f"(grid: {labels})"
        )
    return chosen


def analyse(spec_path, coverage, shapes):
    spec = load_spec(spec_path)
    spec_dir = os.path.dirname(os.path.abspath(spec_path))
    criteria = list(spec.all_criteria)
    backend = CachingSimulator(NgspiceBackend())

    print(f"\n{'='*90}\n{os.path.basename(spec_path)}: "
          f"{len(spec.testbenches)} testbenches x {len(criteria)} criteria")

    sweeps, tables, labels = {}, {}, None
    for name in shapes:
        sw = sweep_deck(spec, spec_dir, PERTURBATIONS[name], backend)
        tbl, lbls = per_criterion(sw, criteria)
        labels = lbls
        sweeps[name], tables[name] = sw, tbl
        fails = [c.name for c in criteria
                 if _violates(tbl[c.name][
                     labels.index(argmax_label(tbl[c.name], labels, c.operator)[0])],
                     c.operator, c.threshold)]
        print(f"  {name:<16} overall_pass={str(sw['overall_pass']):<5} "
              f"failing={len(fails):>2}/{len(criteria)}")

    n_corners = len(labels)
    print(f"  corners in grid: {n_corners}")

    rows = []
    for mode, cov in (("argmax", None), ("coverage", coverage)):
        for d0 in shapes:
            seed = seed_labels(sweeps[d0], criteria, labels, cov)
            outside = [l for l in labels if l not in seed]
            for d1 in shapes:
                tbl = tables[d1]
                fired, detail = [], []
                for c in criteria:
                    values = tbl[c.name]
                    lbl, was_missing = argmax_label(values, labels, c.operator)
                    worst = values[labels.index(lbl)]
                    if not _violates(worst, c.operator, c.threshold):
                        continue
                    if lbl in seed:
                        continue
                    # **여기까지는 느슨한 조건이다.** 최악이 씨앗 밖이어도, 그
                    # 기준이 씨앗 **안** 어느 코너에서 이미 실패하면 중간 루프가
                    # 거기서 먼저 실패한다 - 루프는 PASS 로 빠져나가지 못하고
                    # 판정 스윕에 도달하지 않으므로 재진입은 발화하지 않는다.
                    #
                    # 재진입이 실제로 발화하려면 셋이 다 필요하다:
                    #   (1) 중간 루프가 축소 집합에서 PASS 로 빠져나가고,
                    #   (2) 판정 스윕이 그 기준을 실패시키고,
                    #   (3) 그 최악 코너가 집합 밖일 것.
                    # (1)이 곧 이 설계가 "낙관적 PASS" 라고 부르는 바로 그것이다.
                    inside = [
                        values[labels.index(s)] for s in seed if s in labels
                    ]
                    if any(_violates(v, c.operator, c.threshold) for v in inside):
                        continue
                    fired.append(c.name)
                    detail.append({
                        "criterion": c.name, "worst_corner": lbl,
                        "value": None if was_missing else worst,
                        "no_measurement": was_missing,
                        "passes_everywhere_in_seed": True,
                    })
                rows.append({
                    "mode": mode, "entry_deck": d0, "verdict_deck": d1,
                    "seed_size": len(seed), "outside": len(outside),
                    "verdict_fails": not sweeps[d1]["overall_pass"],
                    "reentry_fires": bool(fired) and not sweeps[d1]["overall_pass"],
                    "criteria": detail,
                })

    print(f"\n  {'mode':<9} | {'entry deck':<16} | {'verdict deck':<16} | "
          f"{'seed':>4} | {'out':>4} | {'fail':>5} | reentry")
    print(f"  {'-'*9}-+-{'-'*16}-+-{'-'*16}-+-{'-'*4}-+-{'-'*4}-+-{'-'*5}-+--------")
    for r in rows:
        mark = "**FIRES**" if r["reentry_fires"] else ""
        names = ",".join(d["criterion"] for d in r["criteria"][:2])
        print(f"  {r['mode']:<9} | {r['entry_deck']:<16} | {r['verdict_deck']:<16} | "
              f"{r['seed_size']:>4} | {r['outside']:>4} | "
              f"{str(r['verdict_fails']):>5} | {mark} {names}")

    fires = [r for r in rows if r["reentry_fires"]]
    print(f"\n  재진입이 발화하는 (모드, 진입덱, 판정덱) 조합: "
          f"**{len(fires)} / {len(rows)}**")
    for m in ("argmax", "coverage"):
        n = sum(1 for r in fires if r["mode"] == m)
        print(f"    {m}: {n}")
    return {
        "spec": spec_path, "corners": n_corners, "criteria": len(criteria),
        "shapes": list(shapes), "rows": rows,
    }


if __name__ == "__main__":
    spec_path = sys.argv[1]
    shapes = sys.argv[2:] or list(PERTURBATIONS)
    unknown = [s for s in shapes if s not in PERTURBATIONS]
    if unknown:
        raise SystemExit(f"unknown perturbation shape(s): {unknown}")
    # ε 은 이 저장소가 밴드갭에서 유도한 값이다. 다른 덱에서는 다시 유도해야
    # 한다 - 그래서 코드 상수가 아니라 스펙 선언이고, 여기서는 실측된 값을
    # 명시적으로 재사용한다.
    out = analyse(spec_path, CoverageConfig(epsilon=0.03, tau=1.0), shapes)
    dest = os.environ.get("REENTRY_FEASIBILITY_OUT", "reentry_feasibility.json")
    with open(dest, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nwrote {dest}")
