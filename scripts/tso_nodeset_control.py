"""대조: 내 `.nodeset` 줄이 **실제로 무언가를 한다**는 것을 먼저 보인다.

앞선 결과(무리 C 14개 중 0개 이동)는 `.nodeset` 이 무시됐을 때와 구별되지
않는다. CLAUDE.md 가 기록한 알려진 뒤집힘 지점 `X6.W = 6.0` 에서 같은
`.nodeset` 줄이 답을 바꾸면 기제가 작동하는 것이고, 안 바꾸면 앞 결과는
무효다.
"""
import json, pathlib, re
from analogcoder.spec import load_spec
from analogcoder.pvt import all_corners, deck_for_corner, _simulate_rendered
from analogcoder.simulators.ngspice import NgspiceBackend

spec = load_spec("benchmarks/two_stage_opamp/spec_pvt.yaml")
tb = [t for t in spec.testbenches if t.name == "ac_loop_gain"][0]
base = pathlib.Path(tb.netlist_path).read_text()
bench = str(pathlib.Path(spec.canonical.netlist_path).parent)
block = tb.control_block.replace(".endc", "op\nprint v(xdut.degn)\n.endc")
backend = NgspiceBackend()
nominal = [c for c in all_corners(spec.pvt_corners) if c.corner_id == "tt/1.8/27.0"][0]
NODESET = "\n.nodeset v(xdut.degn)=0.012 v(xdut.nbias)=0.65\n.end"

def widths(text, w):
    out, n = re.subn(r"^(X6\s+vout\s+outA\s+vss\s+vss\s+\S+\s+L=0\.5\s+W=)\S+\s*$",
                     lambda m: m.group(1) + str(w), text, flags=re.MULTILINE)
    if n != 1:
        raise SystemExit(f"X6.W 치환 {n}회 - 1회여야 한다")
    return out

def run(text):
    r = _simulate_rendered(backend, deck_for_corner(tb, text, nominal, bench).text, block)
    degn = None
    for line in (r.raw_log or "").splitlines():
        s = line.strip()
        if s.startswith("v(xdut.degn)") and "=" in s:
            try: degn = float(s.split("=")[1].split()[0])
            except (ValueError, IndexError): pass
            break
    m = r.measurements or {}
    return {"degn": degn, "gain_db": m.get("gain_db"), "ugbw_hz": m.get("ugbw_hz")}

out = {}
for w in ("5.999999", "6.0", "6.000001", "8"):
    t = widths(base, w)
    out[w] = {"no_nodeset": run(t), "nodeset": run(t.replace("\n.end", NODESET, 1))}
print(json.dumps(out, ensure_ascii=False, indent=1))
