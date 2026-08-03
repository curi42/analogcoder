"""`two_stage_opamp/spec_pvt.yaml` 이 자기 45코너 격자에서 어디가 어떻게
깨지는지 코너별로 적는다. 고치지 않는다 - 무엇을 고쳐야 하는지 알기 위한
측정이다.

이 측정이 다른 답을 낼 수 있는 조건: 코너별 통과/실패가 실제로 갈려야 한다.
전부 실패하거나 전부 통과하면 그 사실 자체가 결과이고, 그때는 "어느 축이
문제인가" 를 물을 수 없다는 것을 결론에 적어야 한다.
"""

import json
import math
import sys

from analogcoder.spec import load_spec
from analogcoder.pvt import run_full_pvt_sweep, all_corners
from analogcoder.judge_tools import evaluate_criteria
from analogcoder.simulators.ngspice import NgspiceBackend

spec_path = sys.argv[1] if len(sys.argv) > 1 else "benchmarks/two_stage_opamp/spec_pvt.yaml"
spec = load_spec(spec_path)
texts = {tb.name: open(tb.netlist_path).read() for tb in spec.testbenches}
sweep = run_full_pvt_sweep(texts, spec, NgspiceBackend())

corners = all_corners(spec.pvt_corners)
crits = spec.all_criteria
rows = []
for i, c in enumerate(corners):
    meas = sweep["per_corner"][i]["measurements"]
    verdict = evaluate_criteria(meas, crits)
    rows.append({
        "corner": c.corner_id,
        "process": c.process, "voltage": c.voltage, "temperature": c.temperature,
        "overall_pass": verdict["overall_pass"],
        "criteria": {
            e["name"]: {
                "actual": (None if isinstance(e["actual"], float) and math.isnan(e["actual"])
                           else e["actual"]),
                "nan": isinstance(e["actual"], float) and math.isnan(e["actual"]),
                "pass": e["pass"],
            } for e in verdict["criteria"]
        },
        # 바이스테이블 가설의 관측 채널. 이 스펙의 측정 이름에 없으면 None 이고,
        # 그 경우 "재지 않았다" 이지 "괜찮다" 가 아니다.
        "degn": meas.get("degn_v"),
        "measured_names": sorted(meas.keys()),
    })

out = {
    "spec": spec_path,
    "n_corners": len(corners),
    "n_criteria": len(crits),
    "overall_pass": sweep["overall_pass"],
    "passing_corners": [r["corner"] for r in rows if r["overall_pass"]],
    "rows": rows,
}
print(json.dumps(out, ensure_ascii=False, indent=1))
