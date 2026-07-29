#!/usr/bin/env python3
"""두 노브의 2차원 격자를 훑어 **결합으로만 도달 가능한 영역**이 있는지 잰다.

**왜 재는가 — 선언하면 안 되기 때문이다.** `spec_topology_required.yaml` 이
"이 임계값은 단일 노브로 도달 불가" 라고 **선언**만 했다가, 강한 모델이
`Cc`+`M6.W` 조합으로 2 이터레이션에 풀어 버린 전례가 이 저장소에 있다. 판별력
있는 벤치마크의 값어치는 전적으로 그 성질이 **측정된 것**이냐에 달려 있다.

로드맵 단계 3(신뢰영역 DFO)의 부정 결과가 "겨냥한 약점 셋 중 둘이 원리적으로
발화하지 못했다 - 노브 순위에 노브가 하나뿐이라 복합 이동이 존재할 수 없다"
였고, 단계 4(제약 BO)의 선행 조건이 그 때문에 열려 있다. 결합이 실재하는
구성이 있어야 그 둘을 판정할 수 있다.

무엇을 내는가: 각 (노브A, 노브B) 점의 세 측정값과, **단일 노브 축**(A만 움직인
행, B만 움직인 열)이 도달한 최댓값. 어떤 점이 두 축의 최댓값을 **동시에** 넘으면
그 점은 결합으로만 도달 가능하다.

nominal(tt/1.8/27, 렌더링 없는 덱) 에서만 돈다 - 임계값 후보를 좁히는 것이
목적이고, 코너 확인은 후보가 정해진 뒤 별도로 한다.

사용:

    .venv/bin/python scripts/knob_coupling_scan.py
"""

import itertools
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))

from analogcoder.netlist import apply_changes, resolve_includes
from analogcoder.simulators.cache import CachingSimulator
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec

REPO = os.path.dirname(_HERE)
SPEC = os.path.join(REPO, "benchmarks", "two_stage_opamp", "spec_pvt.yaml")

# 이 저장소가 **이미 실측한** 결합 쌍이다 - CLAUDE.md 가 "실제 실행이 Cc+M6.W
# 조합으로 2 이터레이션에 풀었고 그것은 Cc 단독 스윕 밖의 조합이었다" 고 적는다.
# 이 덱에서 그 둘은 OPAMP2STAGE.Xcc(밀러 보상 MiM 캡, 정사각이라 w=l)와
# OPAMP2STAGE.X6(출력단 NMOS 폭)이다.
KNOB_A = ("OPAMP2STAGE.Xcc", "w")   # 출하값 12.05
KNOB_B = ("OPAMP2STAGE.X6", "W")    # 출하값 8

A_VALUES = [6.0, 9.0, 12.05, 16.0, 20.0, 26.0, 34.0]
B_VALUES = [4.0, 6.0, 8.0, 11.0, 15.0, 20.0, 27.0]

MEASUREMENTS = ("gain_db", "ugbw_hz", "phase_margin_deg")


def _point(tb, base_text, a, b, backend):
    """Xcc 는 정사각 캡이라 w 와 l 을 **함께** 움직인다 - 한쪽만 바꾸면 면적이
    아니라 종횡비를 바꾸는 것이고, 이 저장소가 보상 캡을 다룰 때 쓰는 축은
    면적이다."""
    changes = [
        {"refdes": KNOB_A[0], "param": "w", "new_value": str(a)},
        {"refdes": KNOB_A[0], "param": "l", "new_value": str(a)},
        {"refdes": KNOB_B[0], "param": KNOB_B[1], "new_value": str(b)},
    ]
    text = apply_changes(base_text, changes)
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "deck.cir")
        with open(path, "w") as f:
            f.write(text)
        result = backend.run(path, {"control_block": tb.control_block})
    return result.measurements or {}


def main() -> None:
    spec = load_spec(SPEC)
    tb = spec.testbenches[0]
    assert tb.name == "ac_loop_gain", tb.name
    base = resolve_includes(
        open(tb.netlist_path).read(), os.path.dirname(tb.netlist_path)
    )
    backend = CachingSimulator(NgspiceBackend())

    grid = {}
    for a, b in itertools.product(A_VALUES, B_VALUES):
        grid[(a, b)] = _point(tb, base, a, b, backend)

    ship = (12.05, 8.0)
    print(f"출하 덱 {KNOB_A[0]}.w=l={ship[0]}  {KNOB_B[0]}.W={ship[1]}")
    print(f"  {grid[ship]}\n")

    for name in MEASUREMENTS:
        print(f"=== {name} ===")
        header = "   a\\b  " + "".join(f"{b:>10.4g}" for b in B_VALUES)
        print(header)
        for a in A_VALUES:
            row = f"  {a:>5.4g} "
            for b in B_VALUES:
                v = grid[(a, b)].get(name)
                row += f"{v:>10.4g}" if isinstance(v, (int, float)) else f"{'-':>10}"
            print(row)
        # 단일 축 최댓값: 출하값 행/열만 움직인 것.
        row_max = max(
            (grid[(ship[0], b)].get(name) for b in B_VALUES
             if isinstance(grid[(ship[0], b)].get(name), (int, float))),
            default=None,
        )
        col_max = max(
            (grid[(a, ship[1])].get(name) for a in A_VALUES
             if isinstance(grid[(a, ship[1])].get(name), (int, float))),
            default=None,
        )
        single = None
        if row_max is not None and col_max is not None:
            single = max(row_max, col_max)
        print(f"  단일 노브 최대: B축(Xcc 고정) {row_max:.6g} / "
              f"A축(X6 고정) {col_max:.6g} -> {single:.6g}")
        beats = [
            (a, b, grid[(a, b)][name])
            for a, b in grid
            if isinstance(grid[(a, b)].get(name), (int, float))
            and grid[(a, b)][name] > single
        ]
        beats.sort(key=lambda t: -t[2])
        print(f"  두 축 최댓값을 **넘는** 격자점: {len(beats)}개"
              + (f"  최고 {beats[0][2]:.6g} at a={beats[0][0]}, b={beats[0][1]}"
                 if beats else "  (결합 이득 없음)"))
        print()

    dest = os.environ.get("KNOB_COUPLING_OUT", "knob_coupling_scan.json")
    with open(dest, "w") as f:
        json.dump({f"{a}|{b}": m for (a, b), m in grid.items()}, f, indent=2)
    print(f"wrote {dest}")


if __name__ == "__main__":
    main()
