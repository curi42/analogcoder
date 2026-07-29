"""단계 2의 **사전 등록 판정 규칙**을 실제로 돌린다.

규칙(로드맵 `2026-07-29-theory-adoption-roadmap.md` 단계 2, 인용):
  채택: 22기준 중 **18개 이상**에서 PB가 지목한 지배 축이 전수 스윕의
        argmax 축과 일치할 것.
  불채택: 그 미만. 이때 PB는 진단 도구로만 남기고 축 삭제에는 쓰지 않는다.
  주의: 공정 축은 **범주형**이라 PB의 연속 인자 가정에 맞지 않는다. 전압·온도
        축에만 적용하고 그 사실을 결과에 적는다.

이 스크립트는 시뮬레이션을 하지 않는다. 45코너 전수 결과 하나만 읽는다.

**PB를 흉내내는 방법과 그 한계를 먼저 적는다.** 진짜 PB는 각 인자를 두
수준(-1/+1)으로 두고 직교 설계의 행만 돈다. 여기서는 전압·온도가 각각 3수준
(1.62/1.8/1.98, -40/27/125)이므로 **극단 두 수준만** 취해 2^2 완전요인의 부분
집합을 만든다 - 인자가 2개일 때 PB(N=4)는 2^2 완전요인과 같다. 즉 이것은
"PB가 이 격자에서 볼 수 있는 최선"이고, 실제 PB보다 **관대한** 대리물이다.
그래도 불일치가 나면 진짜 PB에서도 난다.

공정 축은 규칙이 명시한 대로 뺀다. 대신 어느 process 수준에서 도는지가
결과를 바꾸는지 확인하기 위해 **모든 process 수준에서 따로** 돌린 뒤,
가장 흔한 지목을 PB의 답으로 쓴다(그리고 수준마다 답이 갈리는지도 센다).
"""

import json
import pathlib
from collections import Counter

SWEEP = pathlib.Path("runs/pb_screening/pvt45_sweep.json")

sw = json.loads(SWEEP.read_text())
per_corner = sw["per_corner"]
criteria = sw["criteria"]

# criterion 이름 -> measurement 이름. criteria 항목이 measurement 를 직접
# 들고 있지 않으면 스펙에서 읽는다.
from analogcoder.spec import load_spec

spec = load_spec("benchmarks/bandgap/spec_pvt.yaml")
meas_of = {c.name: c.measurement for c in spec.all_criteria}

# (process, voltage, temperature) -> measurements
grid = {}
for entry in per_corner:
    c = entry["corner"]
    grid[(c["process"], c["voltage"], c["temperature"])] = entry["measurements"]

processes = sorted({p for p, _, _ in grid})
voltages = sorted({v for _, v, _ in grid})
temps = sorted({t for _, _, t in grid})
V_LO, V_HI = voltages[0], voltages[-1]
T_LO, T_HI = temps[0], temps[-1]


def exhaustive_axis(name):
    """전수 스윕이 말하는 지배 축. 45점 전부에서 각 축의 **주효과 범위**
    (그 축의 수준별 평균들의 max-min)를 재고 큰 쪽을 고른다. 공정 축도
    같이 재서, 규칙이 PB에서 빼라고 한 축이 실제로는 지배하는 경우를
    따로 셀 수 있게 한다."""
    points = [(k, m.get(name)) for k, m in grid.items() if m.get(name) is not None]
    if len(points) < len(grid) * 0.5:
        return None, {}, len(points)
    ranges = {}
    for axis, index in (("process", 0), ("voltage", 1), ("temperature", 2)):
        by_level = {}
        for key, value in points:
            by_level.setdefault(key[index], []).append(value)
        means = [sum(v) / len(v) for v in by_level.values()]
        ranges[axis] = max(means) - min(means)
    winner = max(ranges, key=ranges.get)
    return winner, ranges, len(points)


def pb_axis(name):
    """PB(2수준)가 지목하는 축. process 수준마다 따로 돌리고 최빈값을 쓴다."""
    picks = []
    for p in processes:
        corners = {
            (v, t): grid.get((p, v, t), {}).get(name)
            for v in (V_LO, V_HI)
            for t in (T_LO, T_HI)
        }
        if any(x is None for x in corners.values()):
            continue
        # 2수준 주효과 = (+1 평균) - (-1 평균)
        eff_v = abs(
            (corners[(V_HI, T_LO)] + corners[(V_HI, T_HI)]) / 2
            - (corners[(V_LO, T_LO)] + corners[(V_LO, T_HI)]) / 2
        )
        eff_t = abs(
            (corners[(V_LO, T_HI)] + corners[(V_HI, T_HI)]) / 2
            - (corners[(V_LO, T_LO)] + corners[(V_HI, T_LO)]) / 2
        )
        picks.append("voltage" if eff_v >= eff_t else "temperature")
    if not picks:
        return None, 0, 0
    counts = Counter(picks)
    top, n = counts.most_common(1)[0]
    return top, n, len(picks)


agree = disagree = unusable = 0
proc_dominant = 0
split = 0
rows = []
for c in criteria:
    name = c["name"]
    m = meas_of.get(name)
    ex_axis, ranges, npts = exhaustive_axis(m)
    pb, votes, total = pb_axis(m)
    if ex_axis is None or pb is None:
        unusable += 1
        rows.append((name, m, "—", "—", "측정 부족", npts))
        continue
    if total and votes < total:
        split += 1
    if ex_axis == "process":
        proc_dominant += 1
        # 규칙이 PB에서 빼라고 한 축이 지배한다. PB 는 이것을 **구조적으로
        # 지목할 수 없으므로** 일치할 수 없다.
        disagree += 1
        rows.append((name, m, pb, ex_axis, f"공정 지배({votes}/{total})", npts))
        continue
    if pb == ex_axis:
        agree += 1
        rows.append((name, m, pb, ex_axis, f"일치({votes}/{total})", npts))
    else:
        disagree += 1
        rows.append((name, m, pb, ex_axis, f"불일치({votes}/{total})", npts))

w = max(len(r[0]) for r in rows)
print(f"{'criterion':{w}}  {'PB 지목':12} {'전수 argmax':12} 비고")
print("-" * (w + 42))
for name, m, pb, ex, note, npts in rows:
    print(f"{name:{w}}  {str(pb):12} {str(ex):12} {note}")

n = len(criteria)
print()
print(f"기준 {n}개 · 일치 {agree} · 불일치 {disagree} · 판정 불가 {unusable}")
print(f"그중 전수 argmax 가 **공정 축**인 것: {proc_dominant} (PB 가 구조적으로 지목 불가)")
print(f"process 수준에 따라 PB 답이 갈린 기준: {split}")
print()
print(f"사전 등록 기준: 18/{n} 이상 일치 -> 채택")
print(f"결과: {agree}/{n}  ->  " + ("채택" if agree >= 18 else "불채택"))
