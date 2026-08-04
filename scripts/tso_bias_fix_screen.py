"""바이어스 수정 후보를 **0 교차 개수**로 선별한다.

`tso_bias_fixedpoints.py` 와 같은 방법이다: `nbias` 에 전압원을 붙여 쓸면서
회로가 공급해야 하는 전류의 0 교차를 센다. 0 교차 = 자기일관 DC 해이므로
솔버가 어디로 떨어지든 무관하다. 목표는 **45 코너 전부에서 정확히 1 개**다.

측정 전에 적어 둔다(결과를 보고 고치지 않는다):

- **대조군 `baseline` 이 45/45 에서 3 을 내지 않으면 하니스가 깨진 것**이고
  다른 행을 읽지 않는다. `2026-08-03-tso-third-bias-state.md` 가 3 을 쟀다.
- **클램프가 3 → 2 를 만들지 못하면 래치 가설이 틀린 것**이다. 가설은
  "해3 은 `nbias` 고전압 래치이고, `degn` 이 높을 때 `nbias` 를 아래로 당기면
  깨진다" 이다. `degn` 은 세 해에서 0.012 / 0.063 / 0.85 V 로 갈리므로
  게이트를 `degn` 에 물린 NMOS 는 정상 동작에서 꺼져 있다.
- **해1·해2 는 클램프로 사라지지 않아야 정상이다.** 둘은 래치가 아니라
  약반전에서 베타 배율기 방정식이 퇴화해 생기는 해이므로, 그것을 없애려면
  강반전으로 옮겨야 한다 - `Rdeg` 를 낮추는 행들이 그 축이다.
- 남는 0 교차의 **전압**도 기록한다. 개수만 보면 어느 해가 사라졌는지 모른다.

`no_data`(측정 실패)와 "교차 0 개" 는 갈라 센다.
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

# 접두사는 `X` 여야 한다. sky130 소자 모델은 `.model` 이 아니라 **서브회로**이고
# 이 덱의 모든 소자가 `X` 로 쓰여 있다. `M` 으로 쓰면 ngspice 가 MOSFET 모델을
# 찾지 못해 그 코너 전체가 `no_data` 로 죽는다 - 실제로 45/45 를 그렇게 잃었다.
CLAMP = "Xkill nbias degn vss vss sky130_fd_pr__nfet_01v8 L=0.5 W={w}"


def _sub1(text, pattern, repl, what):
    """치환은 반드시 1회. `re.sub` 는 조용하므로 세고 확인한다(저장소 규칙)."""
    text, n = re.subn(pattern, repl, text, flags=re.MULTILINE)
    if n != 1:
        raise SystemExit(f"{what}: 치환 {n} 회 - 1 회여야 한다")
    return text


def add_clamp(text, w):
    return _sub1(text, r"^(Rstart vdd nbias 3Meg)$",
                 lambda m: m.group(1) + "\n" + CLAMP.format(w=w), "clamp")


def set_rdeg(text, r):
    return _sub1(text, r"^Rdeg degn vss 20k$", f"Rdeg degn vss {r}", "rdeg")


def diode_bias(text):
    """베타 배율기를 **저항 + 다이오드**로 바꾼다. 해가 하나인 것이 구조적으로
    보장된다: 저항 전류는 `V(nbias)` 에 감소, 다이오드 전류는 증가하므로
    교차가 정확히 하나다. 대가는 전원 의존성이다."""
    old = ("Xp4 nbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2\n"
           "Xn1 pbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=2\n"
           "Xn2 nbias nbias degn vss sky130_fd_pr__nfet_01v8 L=0.5 W=8\n"
           "Rdeg degn vss 20k\n"
           "Rstart vdd nbias 3Meg")
    new = ("Xn1 pbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=2\n"
           "Xn2 nbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=8\n"
           "Rbias vdd nbias 1Meg")
    if text.count(old) != 1:
        raise SystemExit("diode_bias: 원본 블록을 1 회 찾지 못했다")
    return text.replace(old, new)


def pmos_len(text, l):
    """PMOS 미러를 길게 만든다. 래치가 유지되는 것은 `pbias` 가 바닥일 때
    `Xp4` 가 240 µA 급을 공급할 수 있기 때문이므로, 미러의 구동력을 줄이는 것이
    클램프를 키우는 것보다 직접적인 축이다."""
    text = _sub1(text, r"^Xp3 pbias pbias vdd vdd (\S+) L=0\.5 W=2$",
                 lambda m: f"Xp3 pbias pbias vdd vdd {m.group(1)} L={l} W=2", "xp3")
    return _sub1(text, r"^Xp4 nbias pbias vdd vdd (\S+) L=0\.5 W=2$",
                 lambda m: f"Xp4 nbias pbias vdd vdd {m.group(1)} L={l} W=2", "xp4")


def nmos_pair(text, w1, w2, l, rdeg):
    """베타 배율기의 NMOS 쌍과 축퇴 저항을 함께 바꾼다. 4:1 비율은 유지한다 -
    그것이 배율기의 `K` 이고 바꾸면 다른 회로가 된다."""
    text = _sub1(text, r"^Xn1 pbias nbias vss vss (\S+) L=0\.5 W=2$",
                 lambda m: f"Xn1 pbias nbias vss vss {m.group(1)} L={l} W={w1}", "xn1")
    text = _sub1(text, r"^Xn2 nbias nbias degn vss (\S+) L=0\.5 W=8$",
                 lambda m: f"Xn2 nbias nbias degn vss {m.group(1)} L={l} W={w2}", "xn2")
    return set_rdeg(text, rdeg)


def set_rbias(text, r):
    return _sub1(text, r"^Rbias vdd nbias 1Meg$", f"Rbias vdd nbias {r}", "rbias")


CANDIDATES = {
    "baseline":        lambda t: t,
    "clamp_w1":        lambda t: add_clamp(t, 1),
    "clamp_w4":        lambda t: add_clamp(t, 4),
    "clamp_rdeg_12k":  lambda t: set_rdeg(add_clamp(t, 4), "12k"),
    "clamp_rdeg_6k":   lambda t: set_rdeg(add_clamp(t, 4), "6k"),
    "clamp_rdeg_3k":   lambda t: set_rdeg(add_clamp(t, 4), "3k"),
    # 클램프가 기여하는지 가르기 위한 대조 - `rdeg` 단독
    "rdeg_3k":         lambda t: set_rdeg(t, "3k"),
    "rdeg_2k":         lambda t: set_rdeg(t, "2k"),
    "rdeg_1k":         lambda t: set_rdeg(t, "1k"),
    # 미러 구동력을 줄이는 축
    "pmos_l2":         lambda t: pmos_len(t, 2),
    "pmos_l8":         lambda t: pmos_len(t, 8),
    "pmos_l8_clamp":   lambda t: pmos_len(add_clamp(t, 4), 8),
    "diode_bias":      diode_bias,
    "diode_bias_500k": lambda t: set_rbias(diode_bias(t), "500k"),
    "diode_bias_2meg": lambda t: set_rbias(diode_bias(t), "2Meg"),
    # 전류를 지금 수준에 두면서 강반전으로 옮기는 축.
    #   I = 1/(2 beta R^2),  Vov = 1/(beta R)
    # 이므로 R 을 키우고 beta 를 R^-2 로 줄이면 I 는 그대로 두고 Vov 만 올릴 수
    # 있다. beta 를 줄이는 것은 W/L 을 줄이는 것 - 좁고 길게.
    "si_lowI":  lambda t: nmos_pair(t, "0.5", "2", 5, "200k"),   # I ~ 0.6uA 목표
    "si_midI":  lambda t: nmos_pair(t, "1", "4", 2, "42k"),      # I ~ 3uA 목표
    "si_lowI2": lambda t: nmos_pair(t, "0.5", "2", 8, "300k"),
    "si_midI2": lambda t: nmos_pair(t, "1", "4", 4, "60k"),
    # NMOS 재치수가 필요한가 - `Rdeg` 단독 대조. 클램프에서 배운 대로, 두 변경을
    # 함께 넣고 효과를 한쪽에 귀속시키지 않는다.
    "rdeg_100k": lambda t: set_rdeg(t, "100k"),
    "rdeg_200k": lambda t: set_rdeg(t, "200k"),
    "rdeg_400k": lambda t: set_rdeg(t, "400k"),
}


def _probe(text):
    """`dc` 스윕은 최상위 소자만 대상으로 삼으므로 `nbias` 를 진단용 포트로만
    빼내고 프로브를 최상위에 단다. 소자는 건드리지 않는다."""
    text = _sub1(text, r"^(\.subckt\s+OPAMP2STAGE\s+vinp\s+vinn\s+vout\s+vdd\s+vss)\s*$",
                 lambda m: m.group(1) + " nbias", "probe.subckt")
    text = _sub1(text, r"^(Xdut\s+vinp\s+vinn\s+vout\s+vdd\s+vss)(\s+OPAMP2STAGE)\s*$",
                 lambda m: m.group(1) + " nbias" + m.group(2), "probe.instance")
    return _sub1(text, r"^(Cload\s+vout\s+0\s+\S+)\s*$",
                 lambda m: m.group(1) + "\nVnb nbias vss DC 0", "probe.source")


BLOCK = ".control\ndc Vnb 0 2 0.004\nprint i(Vnb)\n.endc"
ROW = re.compile(r"^\s*\d+\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s*$")
corners = all_corners(spec.pvt_corners)


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


def crossings(pts):
    zs = []
    for i in range(len(pts) - 1):
        v0, i0 = pts[i]
        v1, i1 = pts[i + 1]
        if i0 == 0.0:
            zs.append(v0)
        elif (i0 < 0) != (i1 < 0):
            zs.append(v0 + (v1 - v0) * (0 - i0) / (i1 - i0))
    return zs


wanted = sys.argv[1:] or list(CANDIDATES)
report = {}
for name in wanted:
    text = _probe(CANDIDATES[name](base))
    points = [((c.corner_id,), (deck_for_corner(tb, text, c, bench).text, BLOCK))
              for c in corners]
    res = map_points(lambda p: _simulate_rendered(NgspiceBackend(), p[0], p[1]), points, None)
    rows, no_data = [], []
    for c in corners:
        pts = curve(res[(c.corner_id,)].raw_log)
        if not pts:
            no_data.append(c.corner_id)
            continue
        zs = crossings(pts)
        rows.append({"corner": c.corner_id, "n": len(zs),
                     "at": [round(z, 4) for z in zs]})
    hist = {}
    for r in rows:
        hist[r["n"]] = hist.get(r["n"], 0) + 1
    report[name] = {"no_data": no_data, "n_crossings_histogram": hist, "rows": rows}
    print(f"{name:16s} no_data={len(no_data):2d}  crossings histogram={hist}", flush=True)

pathlib.Path("runs/tso_bias_fix_screen.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=1))
print("\n-> runs/tso_bias_fix_screen.json")
