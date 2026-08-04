"""선별을 통과한 바이어스 수정 후보가 **증폭기에** 무엇을 하는지 잰다.

0 교차 1 개는 필요조건이지 충분조건이 아니다. 해가 하나여도 그 해에서
증폭기가 쓸모없으면 벤치마크로서 죽는다. 그래서 45 코너 × 7 기준을 실제로
돌리고, 기준별 통과 수와 명목 동작점을 나란히 적는다.

읽는 법을 미리 적어 둔다:

- **`spec_pvt.yaml` 의 45/45 통과는 목표가 아니다.** 이 덱은 위상여유가 실패하도록
  출하됐고(`CLAUDE.md`) `spec_pvt` 가 그 임계값(60도)을 물려받는다. 기준선도
  0/45 다. 보는 것은 **`dc_gain` 이 무너진 코너가 사라졌는가** — 기준선의
  14 코너(무리 C, 이득 3-26 dB)가 이 수정으로 없어져야 한다.
- **`degn` 은 이 스펙의 측정 이름에 없다.** 그러므로 여기서 `None` 인 것은
  "괜찮다" 가 아니라 "재지 않았다" 다. 동작점은 별도 열로 싣는다.
- NaN 은 `null` 과 갈라 센다.
"""
import json, math, pathlib, re, sys

from analogcoder.spec import load_spec
from analogcoder.pvt import run_full_pvt_sweep, all_corners
from analogcoder.judge_tools import evaluate_criteria
from analogcoder.simulators.ngspice import NgspiceBackend

sys.path.insert(0, str(pathlib.Path(__file__).parent))

SPEC = "benchmarks/two_stage_opamp/spec_pvt.yaml"

BETA_BLOCK = ("Xp4 nbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2\n"
              "Xn1 pbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=2\n"
              "Xn2 nbias nbias degn vss sky130_fd_pr__nfet_01v8 L=0.5 W=8\n"
              "Rdeg degn vss 20k\n"
              "Rstart vdd nbias 3Meg")
DIODE_BLOCK = ("Xn1 pbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=2\n"
               "Xn2 nbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=8\n"
               "Rbias vdd nbias 1Meg")


def _sub1(text, pattern, repl, what):
    text, n = re.subn(pattern, repl, text, flags=re.MULTILINE)
    if n != 1:
        raise SystemExit(f"{what}: 치환 {n} 회 - 1 회여야 한다")
    return text


def rdeg(value):
    def f(text):
        return _sub1(text, r"^Rdeg degn vss 20k$", f"Rdeg degn vss {value}", "rdeg")
    return f


def diode(text):
    if text.count(BETA_BLOCK) != 1:
        raise SystemExit("diode: 원본 블록을 1 회 찾지 못했다")
    return text.replace(BETA_BLOCK, DIODE_BLOCK)


CANDIDATES = {
    "baseline":   lambda t: t,
    "rdeg_100k":  rdeg("100k"),
    "rdeg_200k":  rdeg("200k"),
    "rdeg_400k":  rdeg("400k"),
    "diode_bias": diode,
}

spec = load_spec(SPEC)
corners = all_corners(spec.pvt_corners)
crits = spec.all_criteria
report = {}

for name in (sys.argv[1:] or list(CANDIDATES)):
    patch = CANDIDATES[name]
    texts = {tb.name: patch(pathlib.Path(tb.netlist_path).read_text())
             for tb in spec.testbenches}
    sweep = run_full_pvt_sweep(texts, spec, NgspiceBackend())

    per_crit = {}
    rows = []
    for i, c in enumerate(corners):
        meas = sweep["per_corner"][i]["measurements"]
        v = evaluate_criteria(meas, crits)
        entry = {"corner": c.corner_id, "overall_pass": v["overall_pass"], "criteria": {}}
        for e in v["criteria"]:
            nan = isinstance(e["actual"], float) and math.isnan(e["actual"])
            entry["criteria"][e["name"]] = {
                "actual": None if nan else e["actual"], "nan": nan, "pass": e["pass"]}
            s = per_crit.setdefault(e["name"], {"pass": 0, "fail": 0, "nan": 0})
            s["nan" if nan else ("pass" if e["pass"] else "fail")] += 1
        rows.append(entry)

    nominal = next(r for r in rows if r["corner"] == "tt/1.8/27.0")
    report[name] = {"per_criterion": per_crit, "rows": rows,
                    "nominal": nominal["criteria"],
                    "overall_pass_count": sum(r["overall_pass"] for r in rows)}

    gains = [r["criteria"]["dc_gain"]["actual"] for r in rows]
    collapsed = sum(1 for g in gains if g is not None and g < 60)
    print(f"=== {name}")
    print(f"    dc_gain<60 인 코너: {collapsed}/45   전체통과: "
          f"{report[name]['overall_pass_count']}/45")
    print("    기준별 pass/fail/nan: " +
          "  ".join(f"{k}={v['pass']}/{v['fail']}/{v['nan']}" for k, v in per_crit.items()))
    print("    명목(tt/1.8/27): " +
          "  ".join(f"{k}={d['actual']}" for k, d in nominal["criteria"].items()), flush=True)

pathlib.Path("runs/tso_bias_fix_perf.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=1))
print("\n-> runs/tso_bias_fix_perf.json")
