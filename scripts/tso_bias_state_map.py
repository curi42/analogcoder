"""45코너 각각에서 `degn` 을 재고, `ugbw` 의 이봉 분포와 맞춰 본다.

가설: `spec_pvt.yaml` 의 코너 실패는 스펙이 빡세서가 아니라 **문서화된 바이어스
바이스테이블**이 코너마다 다른 해로 떨어지기 때문이다(`CLAUDE.md`: 상태 A
`degn` 0.0119 V, 상태 B 0.0626 V, `ugbw` 2.08e6 -> 2.70e7).

이 측정이 다른 답을 낼 수 있는 조건: `degn` 이 코너마다 갈려야 한다. 45개가
전부 한 값이면 가설은 반증되고, 그때 `ugbw` 의 이봉성은 다른 원인이다.

덱과 컨트롤 블록은 실제 스윕이 쓰는 것을 그대로 쓰고(`deck_for_corner`),
컨트롤 블록에만 `degn` 측정 한 줄을 더한다 - 회로는 건드리지 않는다.
"""

import json
import math
import pathlib

from analogcoder.spec import load_spec
from analogcoder.pvt import all_corners, deck_for_corner
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.simulators.parallel import map_points
from analogcoder.pvt import _simulate_rendered

spec = load_spec("benchmarks/two_stage_opamp/spec_pvt.yaml")
tb = [t for t in spec.testbenches if t.name == "ac_loop_gain"][0]
text = pathlib.Path(tb.netlist_path).read_text()
corners = all_corners(spec.pvt_corners)
bench_dir = str(pathlib.Path(spec.canonical.netlist_path).parent)

# 컨트롤 블록에 op 와 degn 측정을 더한다. 기존 줄은 순서를 지켜 그대로 둔다.
block = tb.control_block.replace(
    ".endc",
    "op\nprint v(xdut.degn) v(xdut.nbias)\n.endc",
)

backend = NgspiceBackend()
points = []
for i, c in enumerate(corners):
    render = deck_for_corner(tb, text, c, bench_dir)
    points.append((i, (render.text, block)))

res = map_points(lambda p: _simulate_rendered(backend, p[0], p[1]), points, None)

rows = []
for i, c in enumerate(corners):
    r = res[i]
    m = r.measurements or {}
    raw = r.raw_log or ""
    degn = None
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("v(xdut.degn)") and "=" in s:
            try:
                degn = float(s.split("=")[1].split()[0])
            except (ValueError, IndexError):
                degn = None
            break
    rows.append({
        "corner": c.corner_id,
        "process": c.process, "voltage": c.voltage, "temperature": c.temperature,
        "degn": degn,
        "ugbw_hz": m.get("ugbw_hz"),
        "gain_db": m.get("gain_db"),
        "phase_margin_deg": m.get("phase_margin_deg"),
    })

vals = [r["degn"] for r in rows if r["degn"] is not None]
print(json.dumps({
    "n_corners": len(corners),
    "degn_measured": len(vals),
    "degn_min": min(vals) if vals else None,
    "degn_max": max(vals) if vals else None,
    "degn_distinct_rounded": sorted({round(v, 4) for v in vals}),
    "rows": rows,
}, ensure_ascii=False, indent=1))
