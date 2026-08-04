"""수용 조건: **소자 크기를 바꿔도 해가 하나로 남는가.**

이 결함의 실질적 피해는 코너가 아니라 **튜너**다. 튜너는 소자 크기를 바꾸고,
`CLAUDE.md` 가 기록한 대로 `X6.W` 가 5.999999 -> 6.0 -> 6.000001 로 갈 때 해가
뒤집힌다(`ugbw` 2.07e6 -> 2.70e7 -> 2.07e6). 모델 구간도 비결정성도 아니다.

그러므로 45 코너 전부에서 0 교차가 1 개인 것만으로는 부족하다. 탐색이 지나가는
**크기 축에서도** 1 개여야 한다. 여기서는 명목 코너에 고정하고 `X6.W` 를 쓴다 -
알려진 뒤집힘 점을 포함해서.

읽는 법: 기준선이 `X6.W=6.0` 에서 `ugbw` 가 13 배 뛰는 것을 재현하지 못하면
이 하니스가 그 현상을 볼 수 없는 것이고, 후보의 "안 뒤집힌다" 는 근거가 못 된다.
**대조가 먼저다.**
"""
import json, pathlib, re, sys

from analogcoder.spec import load_spec
from analogcoder.pvt import all_corners, deck_for_corner, _simulate_rendered
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.simulators.parallel import map_points

spec = load_spec("benchmarks/two_stage_opamp/spec_pvt.yaml")
tb = [t for t in spec.testbenches if t.name == "ac_loop_gain"][0]
base = pathlib.Path(tb.netlist_path).read_text()
bench = str(pathlib.Path(spec.canonical.netlist_path).parent)

BETA_BLOCK = ("Xp4 nbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2\n"
              "Xn1 pbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=2\n"
              "Xn2 nbias nbias degn vss sky130_fd_pr__nfet_01v8 L=0.5 W=8\n"
              "Rdeg degn vss 20k\n"
              "Rstart vdd nbias 3Meg")
DIODE_BLOCK = ("Xn1 pbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=2\n"
               "Xn2 nbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=8\n"
               "Rbias vdd nbias 1Meg")

WIDTHS = ["4", "5.999999", "6.0", "6.000001", "8", "12", "20", "30"]
NOMINAL = "tt/1.8/27.0"


def _sub1(text, pattern, repl, what):
    text, n = re.subn(pattern, repl, text, flags=re.MULTILINE)
    if n != 1:
        raise SystemExit(f"{what}: 치환 {n} 회 - 1 회여야 한다")
    return text


def diode(text):
    if text.count(BETA_BLOCK) != 1:
        raise SystemExit("diode: 원본 블록을 1 회 찾지 못했다")
    return text.replace(BETA_BLOCK, DIODE_BLOCK)


def set_x6w(text, w):
    return _sub1(text, r"^(X6   vout outA vss vss \S+ L=0\.5 )W=8$",
                 lambda m: m.group(1) + f"W={w}", "x6w")


def probe(text):
    text = _sub1(text, r"^(\.subckt\s+OPAMP2STAGE\s+vinp\s+vinn\s+vout\s+vdd\s+vss)\s*$",
                 lambda m: m.group(1) + " nbias", "probe.subckt")
    text = _sub1(text, r"^(Xdut\s+vinp\s+vinn\s+vout\s+vdd\s+vss)(\s+OPAMP2STAGE)\s*$",
                 lambda m: m.group(1) + " nbias" + m.group(2), "probe.instance")
    return _sub1(text, r"^(Cload\s+vout\s+0\s+\S+)\s*$",
                 lambda m: m.group(1) + "\nVnb nbias vss DC 0", "probe.source")


DC_BLOCK = ".control\ndc Vnb 0 2 0.004\nprint i(Vnb)\n.endc"
AC_BLOCK = tb.control_block
ROW = re.compile(r"^\s*\d+\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s*$")
corner = [c for c in all_corners(spec.pvt_corners) if c.corner_id == NOMINAL][0]


def crossings(log):
    pts = []
    for line in (log or "").splitlines():
        m = ROW.match(line)
        if m:
            try:
                pts.append((float(m.group(1)), float(m.group(2))))
            except ValueError:
                pass
    if not pts:
        return None
    zs = []
    for i in range(len(pts) - 1):
        v0, i0 = pts[i]
        v1, i1 = pts[i + 1]
        if i0 == 0.0:
            zs.append(v0)
        elif (i0 < 0) != (i1 < 0):
            zs.append(v0 + (v1 - v0) * (0 - i0) / (i1 - i0))
    return zs


variants = {"baseline": lambda t: t, "diode_bias": diode}
report = {}
for name, patch in variants.items():
    # 해의 개수 - 프로브를 단 덱
    dc_pts = [((w,), (deck_for_corner(tb, probe(set_x6w(patch(base), w)), corner, bench).text,
                      DC_BLOCK)) for w in WIDTHS]
    dc = map_points(lambda p: _simulate_rendered(NgspiceBackend(), p[0], p[1]), dc_pts, None)
    # 솔버가 실제로 착지한 곳 - 프로브 없는 원래 덱
    ac_pts = [((w,), (deck_for_corner(tb, set_x6w(patch(base), w), corner, bench).text,
                      AC_BLOCK)) for w in WIDTHS]
    ac = map_points(lambda p: _simulate_rendered(NgspiceBackend(), p[0], p[1]), ac_pts, None)

    rows = []
    for w in WIDTHS:
        zs = crossings(dc[(w,)].raw_log)
        m = ac[(w,)].measurements
        rows.append({"X6.W": w,
                     "n_solutions": None if zs is None else len(zs),
                     "at": None if zs is None else [round(z, 4) for z in zs],
                     "gain_db": m.get("gain_db"), "ugbw_hz": m.get("ugbw_hz"),
                     "phase_margin_deg": m.get("phase_margin_deg")})
    report[name] = rows
    print(f"=== {name}")
    for r in rows:
        print("    X6.W=%-10s 해=%-5s gain=%-9s ugbw=%-12s pm=%s"
              % (r["X6.W"], r["n_solutions"], r["gain_db"], r["ugbw_hz"],
                 r["phase_margin_deg"]), flush=True)

pathlib.Path("runs/tso_bias_fix_sizesweep.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=1))
print("\n-> runs/tso_bias_fix_sizesweep.json")
