#!/usr/bin/env python3
"""로드맵 태스크 6(노브 순위의 그래프 사전확률)의 **착수 조건**을 모은다.

착수 조건은 이것이다: **실패 criterion 이 2개 이상이고 서로 다른 블록을
가리키는 실제 실행 3건.** 지금 정답표 다섯 케이스는 전부 실패 criterion 이
정확히 1개라, 세 거리 정의가 각각 선언한 집계 규칙(min-rank / min-distance /
min+sum)이 **한 번도 실행된 적이 없다** - 이 저장소 자신의 기준으로
UNINFORMATIVE 다. 그 상태에서 집계 규칙을 코드에 넣으면 D1 의 "측정이 부정이
아니라 무효였다" 가 그대로 재발한다.

**블록은 이름으로 추측하지 않는다.** `trim_phase_margin` 이 `TRIMAMP` 을
가리킨다고 접두사로 읽는 것은 이 저장소가 금지하는 종류의 추측이다(전원 레일을
`vdd` 라는 이름으로 알아보는 것과 같은 자리). 대신 실제 파이프라인이 쓰는
결정론적 경로를 그대로 탄다:

    criterion.measurement --control_block.measurement_nets--> 넷 집합
    넷 집합 --structure_view.select_focus--> 블록 경로 집합

이것은 수리 루프가 튜너에게 무엇을 보여줄지 정할 때 쓰는 바로 그 함수다.
그래서 여기서 나온 "서로 다른 블록" 은 그래프 사전확률이 판정될 자리와 같은
정의를 쓴다.

사용:

    .venv/bin/python scripts/multi_block_failures.py benchmarks/bandgap/spec_corner_reduction.yaml

산출물: `multi_block_failures.json` (cwd, 환경변수 `MULTI_BLOCK_OUT` 로 변경).
"""

import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
sys.path.insert(0, _HERE)

from analogcoder.control_block import measurement_nets
from analogcoder.netlist import apply_changes, resolve_includes
from analogcoder.pvt import run_full_pvt_sweep
from analogcoder.signal_path import build_signal_paths
from analogcoder.simulators.cache import CachingSimulator
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.structure import derive_structure
from analogcoder.structure_view import select_focus
from perturbations import PERTURBATIONS


def _missing(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


def _violates(value, op, threshold):
    if _missing(value):
        return True
    return not {
        ">=": value >= threshold, ">": value > threshold,
        "<=": value <= threshold, "<": value < threshold,
    }[op]


def failing_criteria(sweep, criteria):
    """판정 스윕이 실패시킨 criterion 이름. `evaluate_criteria` 와 같은 정의를
    쓰기 위해 스윕이 이미 낸 `criteria` 항목을 그대로 읽는다 - 여기서 다시
    비교식을 세우면 두 정의가 갈라진다."""
    by_name = {e["name"]: e for e in sweep.get("criteria", [])}
    return [c.name for c in criteria if not by_name.get(c.name, {}).get("pass", True)]


def blocks_for(names, spec, texts):
    """실패 criterion 들이 가리키는 블록 경로. 파이프라인과 **같은 경로**다.

    criterion 하나가 여러 블록을 가리킬 수 있고(초점은 집합이다), 어떤
    criterion 은 아무 블록도 가리키지 않을 수 있다 - 측정 넷이 최상위에만
    걸려 있으면 그렇다. 둘 다 사실이므로 그대로 돌려준다."""
    out = {}
    for tb in spec.testbenches:
        nets_by_meas = measurement_nets(tb.control_block)
        text = texts[tb.name]
        structure = derive_structure(text, spec.circuit_name)
        paths = build_signal_paths(structure)
        for c in tb.criteria:
            if c.name not in names:
                continue
            nets = nets_by_meas.get(c.measurement, set())
            focus = select_focus(structure, paths, set(nets), set(), text)
            out.setdefault(c.name, set()).update(focus)
    return {k: sorted(v) for k, v in out.items()}


def analyse(spec_path, shapes):
    spec = load_spec(spec_path)
    spec_dir = os.path.dirname(os.path.abspath(spec_path))
    criteria = list(spec.all_criteria)
    backend = CachingSimulator(NgspiceBackend())

    print(f"\n{os.path.basename(spec_path)}: {len(criteria)} criteria\n")
    print(f"  {'shape':<16} {'실패':>4}  {'블록':>4}  블록 목록")
    print(f"  {'-'*16}-{'-'*5}-{'-'*5}--------------------------------")

    rows = []
    for name in shapes:
        perturb = PERTURBATIONS[name]
        texts = {}
        for tb in spec.testbenches:
            t = resolve_includes(open(tb.netlist_path).read(), spec_dir)
            if perturb:
                t = apply_changes(t, perturb)
            texts[tb.name] = t
        sweep = run_full_pvt_sweep(texts, spec, backend)
        names = failing_criteria(sweep, criteria)
        per_crit = blocks_for(set(names), spec, texts) if names else {}
        all_blocks = sorted({b for v in per_crit.values() for b in v})
        qualifies = len(names) >= 2 and len(all_blocks) >= 2
        rows.append({
            "shape": name, "failing": names, "blocks_per_criterion": per_crit,
            "blocks": all_blocks, "qualifies": qualifies,
        })
        mark = " **착수조건 충족**" if qualifies else ""
        print(f"  {name:<16} {len(names):>4}  {len(all_blocks):>4}  "
              f"{', '.join(all_blocks) or '-'}{mark}")

    ok = [r for r in rows if r["qualifies"]]
    print(f"\n  착수 조건(실패 2개 이상 × 블록 2개 이상)을 만족하는 덱 상태: "
          f"**{len(ok)}건** (필요: 3건)")
    for r in ok:
        print(f"    {r['shape']}: {', '.join(r['failing'])}")
        for c, b in sorted(r["blocks_per_criterion"].items()):
            print(f"      {c:<24} -> {', '.join(b) or '(블록 없음)'}")
    return {"spec": spec_path, "rows": rows, "qualifying": len(ok)}


if __name__ == "__main__":
    spec_path = sys.argv[1]
    shapes = sys.argv[2:] or list(PERTURBATIONS)
    unknown = [s for s in shapes if s not in PERTURBATIONS]
    if unknown:
        raise SystemExit(f"unknown perturbation shape(s): {unknown}")
    out = analyse(spec_path, shapes)
    dest = os.environ.get("MULTI_BLOCK_OUT", "multi_block_failures.json")
    with open(dest, "w") as f:
        json.dump(out, f, indent=2, default=list)
    print(f"\nwrote {dest}")
