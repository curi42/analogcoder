"""가설: 무리 C(바이어스 폭주)의 원인은 `Rstart` 가 시동 소자가 아니라 **상시
전류원**이라는 것이다. 3 MΩ 은 1.98 V 에서 약 0.66 uA 를 계속 `nbias` 로 밀어
넣는데, 상태 A 의 루프 전류가 약 0.6 uA 다 - 같은 자릿수다.

이 가설이 맞다면 `Rstart` 를 키울수록 무리 C 가 줄어야 한다. 그리고 너무 키우면
**시동이 안 걸려** degn 이 0 근처로 죽는 코너가 생겨야 한다. 두 방향이 다 보여야
"Rstart 가 원인" 이 성립하고, 한쪽만 보이면 다른 설명이 필요하다.

**이 측정이 다른 답을 낼 수 있는 조건:** 무리 분포가 `Rstart` 에 따라 변해야
한다. 네 값에서 분포가 같으면 가설은 반증되고 원인은 `Rstart` 가 아니다.

회로에서 바꾸는 것은 `Rstart` 의 값 **하나뿐**이다. 그 외 텍스트는 그대로다.
"""

import json
import pathlib
import re

from analogcoder.spec import load_spec
from analogcoder.pvt import all_corners, deck_for_corner, _simulate_rendered
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.simulators.parallel import map_points

VALUES = ["3Meg", "10Meg", "30Meg", "100Meg", "300Meg"]
# 무리 경계는 실측에서 왔다: A 는 0.0084-0.0132, B 는 0.0457-0.0686,
# C 는 0.5360-0.9091. 간극이 x3.46 과 x7.81 이므로 0.03 과 0.1 에서 자른다.
# "죽음"(시동 실패)은 상태 A 의 하한보다 한 자릿수 아래로 둔다.
DEAD, A_B, B_C = 0.001, 0.03, 0.1

spec = load_spec("benchmarks/two_stage_opamp/spec_pvt.yaml")
tb = [t for t in spec.testbenches if t.name == "ac_loop_gain"][0]
base_text = pathlib.Path(tb.netlist_path).read_text()
corners = all_corners(spec.pvt_corners)
bench_dir = str(pathlib.Path(spec.canonical.netlist_path).parent)
block = tb.control_block.replace(".endc", "op\nprint v(xdut.degn)\n.endc")
backend = NgspiceBackend()

# `re.sub` 는 조용하다 - 이 저장소의 규칙대로 바뀐 횟수를 센다.
PAT = re.compile(r"^(Rstart\s+vdd\s+nbias\s+)(\S+)\s*$", re.MULTILINE | re.IGNORECASE)

points, meta = [], []
for value in VALUES:
    text, n = PAT.subn(lambda m: m.group(1) + value, base_text)
    if n != 1:
        raise SystemExit(f"Rstart 치환이 {n}회다 - 1회여야 한다. 덱이 바뀌었다.")
    for i, c in enumerate(corners):
        key = (value, i)
        points.append((key, (deck_for_corner(tb, text, c, bench_dir).text, block)))
        meta.append((key, value, c))

res = map_points(lambda p: _simulate_rendered(backend, p[0], p[1]), points, None)

def bucket(d):
    if d is None:
        return "unmeasured"
    if d < DEAD:
        return "dead"
    if d < A_B:
        return "A"
    if d < B_C:
        return "B"
    return "C"

rows = []
for key, value, c in meta:
    r = res[key]
    m = r.measurements or {}
    degn = None
    for line in (r.raw_log or "").splitlines():
        s = line.strip()
        if s.startswith("v(xdut.degn)") and "=" in s:
            try:
                degn = float(s.split("=")[1].split()[0])
            except (ValueError, IndexError):
                degn = None
            break
    rows.append({
        "rstart": value, "corner": c.corner_id,
        "voltage": c.voltage, "temperature": c.temperature, "process": c.process,
        "degn": degn, "bucket": bucket(degn),
        "gain_db": m.get("gain_db"), "ugbw_hz": m.get("ugbw_hz"),
        "phase_margin_deg": m.get("phase_margin_deg"),
        "status": r.status,
    })

summary = {}
for value in VALUES:
    sub = [r for r in rows if r["rstart"] == value]
    counts = {}
    for r in sub:
        counts[r["bucket"]] = counts.get(r["bucket"], 0) + 1
    gains = [r["gain_db"] for r in sub if r["gain_db"] is not None]
    summary[value] = {
        "buckets": counts,
        "gain_lt_60": sum(1 for g in gains if g < 60),
        "gain_min": min(gains) if gains else None,
        "gain_measured": len(gains),
    }

print(json.dumps({"values": VALUES, "n_corners": len(corners),
                  "thresholds": {"dead": DEAD, "A_B": A_B, "B_C": B_C},
                  "summary": summary, "rows": rows},
                 ensure_ascii=False, indent=1))
