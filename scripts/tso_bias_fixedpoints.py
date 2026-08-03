"""바이어스 루프의 DC 해를 **직접 센다**. 솔버 유도(.nodeset)에 기대지 않는다.

방법: `nbias` 에 전압원 `Vnb` 를 붙이고 그 전압을 쓸면서 원이 공급해야 하는
전류 `i(Vnb)` 를 본다. 그 전류가 0 이면 그 전압에서 회로가 스스로 성립한다 -
**0 교차의 개수가 곧 자기일관 해의 개수다.** 전압원은 노드를 강제할 뿐이므로
솔버가 어느 해로 떨어지든 무관하다.

이 측정이 다른 답을 낼 수 있는 조건: 코너마다 0 교차 수가 달라야 한다. 전부
같으면 "해의 개수" 로는 무리 C 를 설명할 수 없고 다른 설명이 필요하다.

자기점검이 하나 필요하다: `i(Vnb)` 를 실제로 읽었는가. 데이터가 0 행이면
그것은 "교차 0개" 가 아니라 **측정 실패**다 - 둘을 뭉개지 않는다.
"""
import json, pathlib, re
from analogcoder.spec import load_spec
from analogcoder.pvt import all_corners, deck_for_corner, _simulate_rendered
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.simulators.parallel import map_points

spec = load_spec("benchmarks/two_stage_opamp/spec_pvt.yaml")
tb = [t for t in spec.testbenches if t.name == "ac_loop_gain"][0]
base = pathlib.Path(tb.netlist_path).read_text()
bench = str(pathlib.Path(spec.canonical.netlist_path).parent)

# `dc` 스윕은 **최상위** 소자만 대상으로 삼는다(서브회로 안의 이름은
# "no such device" 로 죽는다 - 실제로 겪었다). 그래서 진단용으로만 `nbias` 를
# 포트로 빼내고 프로브 전압원을 최상위에 단다. 소자는 하나도 건드리지 않고
# 노드도 그대로다 - 포트가 하나 늘고 전압원이 하나 붙을 뿐이다.
def _probe(text):
    text, a = re.subn(r"^(\.subckt\s+OPAMP2STAGE\s+vinp\s+vinn\s+vout\s+vdd\s+vss)\s*$",
                      lambda m: m.group(1) + " nbias", text, flags=re.MULTILINE)
    text, b = re.subn(r"^(Xdut\s+vinp\s+vinn\s+vout\s+vdd\s+vss)(\s+OPAMP2STAGE)\s*$",
                      lambda m: m.group(1) + " nbias" + m.group(2), text, flags=re.MULTILINE)
    text, c = re.subn(r"^(Cload\s+vout\s+0\s+\S+)\s*$",
                      lambda m: m.group(1) + "\nVnb nbias vss DC 0", text, flags=re.MULTILINE)
    if (a, b, c) != (1, 1, 1):
        raise SystemExit(f"프로브 삽입 치환 횟수 {(a, b, c)} - 전부 1회여야 한다")
    return text

text = _probe(base)

BLOCK = """.control
dc Vnb 0 2 0.004
print i(Vnb)
.endc"""

corners = all_corners(spec.pvt_corners)
points = [((c.corner_id,), (deck_for_corner(tb, text, c, bench).text, BLOCK)) for c in corners]
res = map_points(lambda p: _simulate_rendered(NgspiceBackend(), p[0], p[1]), points, None)

ROW = re.compile(r"^\s*\d+\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s*$")

def curve(log):
    pts = []
    for line in (log or "").splitlines():
        m = ROW.match(line)
        if m:
            try:
                pts.append((float(m.group(1)), float(m.group(2))))
            except ValueError:
                pass
    return pts

out = []
for c in corners:
    pts = curve(res[(c.corner_id,)].raw_log)
    if not pts:
        out.append({"corner": c.corner_id, "status": "no_data", "crossings": None})
        continue
    zeros = []
    for i in range(len(pts) - 1):
        v0, i0 = pts[i]; v1, i1 = pts[i + 1]
        if i0 == 0.0:
            zeros.append(v0)
        elif (i0 < 0) != (i1 < 0):
            zeros.append(v0 + (v1 - v0) * (0 - i0) / (i1 - i0))
    out.append({"corner": c.corner_id, "process": c.process, "voltage": c.voltage,
                "temperature": c.temperature, "status": "ok", "n_points": len(pts),
                "crossings": [round(z, 5) for z in zeros], "n_crossings": len(zeros)})

print(json.dumps({"rows": out,
                  "no_data": [r["corner"] for r in out if r["status"] == "no_data"]},
                 ensure_ascii=False, indent=1))
