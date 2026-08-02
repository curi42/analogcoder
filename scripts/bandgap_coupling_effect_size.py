#!/usr/bin/env python3
"""T18b 보조 측정 두 가지를 재현 가능한 산출물로 만든다.

`scripts/pairwise_coupling.py`가 내는 JSON은 `I_rel`(상호작용 대비를 **단일축
효과로 정규화한** 양)만 담는다. 그 값으로 답할 수 없는 두 질문이 있고, 둘 다
T18b의 결론에 직접 들어간다:

1. **무효과 확인.** `dc_tc`에서 면적 순위 상위 12개 중 열 개의 `I_rel`이 정확히
   0으로 나왔다. `_interaction`은 단일축 효과가 0이면 `I`도 0이라 `(0, 0.0)`을
   내므로, 그 0은 "결합이 없다"가 아니라 **"이 계기가 그 노브를 못 봤다"**일 수
   있다. 그 둘은 다른 사실이므로 노브를 하나씩 움직여 측정값이 **바이트 동일한지**
   직접 본다. 이것을 확인하지 않고 0을 직교성으로 읽는 것이 D1의 `0.000`과 같은
   부류의 실수다("다른 답이 나올 수 있는 조건이 측정한 런에 있었는가").

2. **절대 효과 크기.** `I_rel`은 크기가 아니다 - 두 축이 모두 거의 0인 곳에서
   물리적으로 하찮은 절대 효과가 큰 `I_rel`을 낸다. 확정된 쌍이 실제로 얼마나
   먼 곳까지 가는지는 격자 전체 폭과 단일축 폭을 나란히 봐야 안다.

노브 순위·격자·덱 텍스트 생성은 전부 `pairwise_coupling`에서 **가져다 쓴다**.
복제하면 이 스크립트가 재는 격자와 본 측정의 격자가 갈라진다.

사용:

    .venv/bin/python scripts/bandgap_coupling_effect_size.py
"""

import importlib.util
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(REPO, "src"))

_spec = importlib.util.spec_from_file_location(
    "pairwise_coupling", os.path.join(_HERE, "pairwise_coupling.py")
)
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)

from analogcoder.netlist import apply_changes, resolve_includes  # noqa: E402
from analogcoder.simulators.cache import CachingSimulator  # noqa: E402
from analogcoder.simulators.ngspice import NgspiceBackend  # noqa: E402
from analogcoder.spec import load_spec  # noqa: E402

OUT_JSON = os.path.join(
    REPO, "docs", "superpowers", "specs", "2026-08-02-bandgap-coupling-effect-size.json"
)

# 무효과 확인에 쓸 단축 변경. 상위 12개의 `Xcc` 노브 하나씩 + 대조군으로
# 효과가 있다고 아는 저항 노브 하나. 값은 격자가 실제로 밟는 범위 안에서 고른다.
INERTNESS_PROBES = [
    ("BUF_N.Xcc", "W", "25"),
    ("BUF_N.Xcc", "W", "100"),
    ("BUF_N.Xcc", "L", "25"),
    ("BUF_P.Xcc", "W", "20"),
    ("ERRAMP.Xcc", "W", "20"),
    ("TRIMAMP.Xcc", "W", "20"),
    ("BGR_CORE.Xcc", "W", "10"),
    # 대조군: 효과가 있어야 한다. 없으면 이 측정 자체가 무효다.
    ("BANDGAP.XRl2", "w", "0.5"),
]

# 절대 크기를 볼 쌍. 2단계에서 확정된 15쌍 중 (a) 한 소자 안의 L x W와
# (b) 블록을 가로지르는 쌍을 하나씩 고른다.
EFFECT_PAIRS = [
    ("TRIMAMP.Xcc.L", "TRIMAMP.Xcc.W"),
    ("ERRAMP.Xcc.L", "BGR_CORE.Xcc.L"),
    ("BUF_P.Xcc.L", "BUF_P.Xcc.W"),
]


def _run(backend, text, control_block):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "deck.cir")
        with open(path, "w") as f:
            f.write(text)
        return backend.run(path, {"control_block": control_block})


def main() -> int:
    spec = load_spec(pc.BANDGAP_PROFILE.spec_path)
    canonical = spec.canonical
    loops = next(t for t in spec.testbenches if t.name == "amp_loops")
    backend = CachingSimulator(NgspiceBackend())

    # --- 1. 무효과 확인: canonical 테스트벤치에서 노브 하나씩 움직인다 ---
    dc_text = resolve_includes(
        open(canonical.netlist_path).read(), os.path.dirname(canonical.netlist_path)
    )
    baseline = _run(backend, dc_text, canonical.control_block)
    inertness = []
    for refdes, param, value in INERTNESS_PROBES:
        moved = apply_changes(
            dc_text, [{"refdes": refdes, "param": param, "new_value": value}]
        )
        r = _run(backend, moved, canonical.control_block)
        changed = {
            k: [baseline.measurements.get(k), r.measurements.get(k)]
            for k in sorted(set(baseline.measurements) | set(r.measurements))
            if baseline.measurements.get(k) != r.measurements.get(k)
        }
        inertness.append({
            "refdes": refdes, "param": param, "new_value": value,
            "status": r.status,
            "identical_to_baseline": not changed,
            "changed_measurements": changed,
        })
        print(f"{refdes}.{param}={value}: identical={not changed}", flush=True)

    # --- 2. 절대 효과 크기: amp_loops의 7x7 격자를 그대로 다시 읽는다 ---
    profile = pc.BANDGAP_LOOPS_PROFILE
    loops_text = resolve_includes(
        open(loops.netlist_path).read(), os.path.dirname(loops.netlist_path)
    )
    control_block = pc.build_control_block(loops.control_block, profile)
    # 순위는 canonical 덱 원문에서 - `run_area_optimization`이 읽는 것과 같다.
    knobs, _skipped, ranking = pc.build_knobs_from_area_ranking(
        open(canonical.netlist_path).read(), profile
    )
    idx = {k.label: i for i, k in enumerate(knobs)}

    effects = {}
    for label_a, label_b in EFFECT_PAIRS:
        i, j = idx[label_a], idx[label_b]
        texts, level_cache = pc._collect_texts_for_pairs(
            knobs, [(i, j)], pc.SCALES_STAGE2, loops_text, profile
        )
        results = pc._run_texts(texts, control_block, backend, None, profile)
        grid = pc._pair_grid(knobs, i, j, level_cache, loops_text, results, profile)
        ba, bb = grid.base_idx_a, grid.base_idx_b
        per_measurement = {}
        for name in profile.measurements:
            allv = [v[name] for v in grid.values.values() if v[name] is not None]
            axis_a = [
                grid.values[(ia, bb)][name] for ia in range(len(grid.levels_a))
                if grid.values[(ia, bb)][name] is not None
            ]
            axis_b = [
                grid.values[(ba, ib)][name] for ib in range(len(grid.levels_b))
                if grid.values[(ba, ib)][name] is not None
            ]
            if not allv or not axis_a or not axis_b:
                per_measurement[name] = {"void": "격자에 유효한 값이 없다"}
                continue
            per_measurement[name] = {
                "baseline": grid.values[(ba, bb)][name],
                "grid_span": max(allv) - min(allv),
                "axis_a_span": max(axis_a) - min(axis_a),
                "axis_b_span": max(axis_b) - min(axis_b),
                "n_valid_points": len(allv),
            }
        effects[f"{label_a}|{label_b}"] = {
            "levels_a": grid.levels_a,
            "levels_b": grid.levels_b,
            "n_grid_points": len(grid.values),
            # 값이 없는 점 수. sky130 소자 bin 상한(100um)을 넘는 x3.0 레벨이
            # `could not find a valid modelname`으로 중단되기 때문이다 -
            # 계기의 침묵이 아니라 기록된 사실이다.
            "n_missing_points": sum(
                1 for v in grid.values.values() if v[profile.measurements[0]] is None
            ),
            "measurements": per_measurement,
        }
        print(f"{label_a} x {label_b}: done", flush=True)

    out = {
        "purpose": "T18b 보조 측정. pairwise_coupling의 I_rel이 답할 수 없는 두 가지: "
                   "(1) I_rel=0이 직교성인가 무효과인가, (2) 확정된 결합의 절대 크기.",
        "spec": os.path.relpath(pc.BANDGAP_PROFILE.spec_path, REPO),
        "inertness": {
            "testbench": canonical.name,
            "baseline_measurements": dict(baseline.measurements),
            "note": "identical_to_baseline=true 는 그 노브가 이 테스트벤치의 어느 "
                    "측정값도 바꾸지 않는다는 뜻이다. 그런 노브의 I_rel=0 은 "
                    "'결합 없음'이 아니라 '계기가 못 봄'이다.",
            "probes": inertness,
        },
        "effect_size": {
            "testbench": loops.name,
            "rank_testbench": canonical.name,
            "scales": list(pc.SCALES_STAGE2),
            "note": "I_rel 은 단일축 효과로 정규화한 양이라 크기가 아니다. "
                    "grid_span 을 axis_a_span/axis_b_span 과 나란히 읽어야 "
                    "'두 축을 함께 움직여야만 닿는 곳'의 크기를 알 수 있다.",
            "pairs": effects,
        },
        "area_gain_ranking": ranking,
        "bistability_checked": False,
        "bistability_note": "이 실행에서도 바이스테이블 확인은 돌지 않았다 - "
                            "밴드갭 프로파일에는 프로브도 완화책도 없다. "
                            "근거는 scripts/dc_solution_uniqueness.py 의 별도 측정이다.",
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w") as f:
        json.dump(out, f, indent=2, sort_keys=True)
    print(f"결과를 {OUT_JSON}에 썼다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
