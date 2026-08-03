"""45코너 격자에서 코너 축소를 켰을 때의 **반복당 비용**을 잰다.

CLAUDE.md 가 남긴 선행 조건: 45코너에서는 `max_corners` 상한이 없으면 축소가
대체하려던 전체 스윕보다 **더 비쌀 수** 있다(반복당 ~125 시뮬 투영). 그 투영이
맞는지 실측한다.

비용 모형은 저장소가 이미 쓰는 것을 그대로 쓴다:
  직접 시뮬 = 테스트벤치 수 x (씨앗 코너 수 + 덱 1점 + 탐침 1점)
  전체 스윕 = 테스트벤치 수 x 코너 수
LLM 은 부르지 않는다. 새 시뮬레이션은 진입 스윕 한 번뿐이다.

이 측정이 다른 답을 낼 수 있는 조건: 씨앗 크기가 min(기준 수, 코너 수) 로
묶이므로, 22 기준 x 45 코너에서 씨앗이 22 에 가까우면 축소는 살 게 없고
9 에 가까우면 크게 산다. 두 결과가 다 가능하다.
"""
import json, pathlib, sys
from analogcoder.spec import load_spec
from analogcoder.pvt import run_full_pvt_sweep, all_corners
from analogcoder.corner_selection import seed_from_sweep
from analogcoder.simulators.ngspice import NgspiceBackend

out = {}
for name in sys.argv[1:]:
    spec = load_spec(name)
    texts = {tb.name: open(tb.netlist_path).read() for tb in spec.testbenches}
    sweep = run_full_pvt_sweep(texts, spec, NgspiceBackend())
    cs, record = seed_from_sweep(sweep, spec)
    n_tb = len(spec.testbenches)
    n_corner = len(all_corners(spec.pvt_corners))
    seed = len(cs.corners)
    # 덱 1점(명목은 코너가 아니라 파일 그대로) + 탐침 1점.
    pts_per_tb = seed + 1 + 1
    out[name] = {
        "testbenches": n_tb, "criteria": len(spec.all_criteria), "corners": n_corner,
        "seed_corners": seed,
        "points_per_tb": pts_per_tb,
        "sims_per_iteration": n_tb * pts_per_tb,
        "sims_full_sweep": n_tb * n_corner,
        "ratio_iter_vs_sweep": round(n_tb * pts_per_tb / (n_tb * n_corner), 3),
        "entry_sweep_overall_pass": sweep["overall_pass"],
        "seed_record_keys": sorted(record.keys()),
        "dropped": record.get("dropped"),
        "seed_labels": sorted(getattr(c, "corner_id", str(c)) for c in cs.corners),
    }
print(json.dumps(out, ensure_ascii=False, indent=1))
