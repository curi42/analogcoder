"""단계 2의 사전 등록 검증에 필요한 45코너 per_corner 데이터를 만든다.
LLM 0회. `run_full_pvt_sweep`을 직접 부른다."""
import json, pathlib, time
from analogcoder.spec import load_spec
from analogcoder.pvt import run_full_pvt_sweep
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.json_io import dump as json_dump

spec = load_spec("benchmarks/bandgap/spec_pvt.yaml")
texts = {tb.name: pathlib.Path(tb.netlist_path).read_text() for tb in spec.testbenches}
print("testbenches:", list(texts), "criteria:", len(spec.all_criteria))
t0 = time.time()
sweep = run_full_pvt_sweep(texts, spec, NgspiceBackend())
print(f"elapsed {time.time()-t0:.1f}s  per_corner={len(sweep['per_corner'])}  criteria={len(sweep['criteria'])}")
out = pathlib.Path("runs/pb_screening/pvt45_sweep.json")
out.parent.mkdir(parents=True, exist_ok=True)
with out.open("w") as f:
    json_dump(sweep, f, indent=1)
print("wrote", out)
