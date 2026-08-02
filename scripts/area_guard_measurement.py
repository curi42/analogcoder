"""면적 단계의 무방비 가드 측정.

사전 등록: `docs/superpowers/specs/2026-08-02-area-phase-guard-measurement-design.md`
(커밋 `295d309`, 이 스크립트를 돌리기 전에 고정됨).

답하려는 질문 하나: **면적 단계가 코너 없이 수락한 스텝이, 코너를 선언한 같은
회로에서 실제로 기준을 깨뜨리는가.**

주 판정은 런 A 가 착지시킨 덱을 스펙 B 의 45 코너 그리드로 전체 스윕한 결과다.
`spec.yaml` 과 `spec_pvt.yaml` 은 기준 22 개가 **완전히 동일**하다(이름·측정
값·연산자·임계값 전부, 이 스크립트가 시작할 때 다시 확인하고 다르면 중단한다).
차이는 `pvt_corners` 선언과 `optimize:` 블록뿐이므로, 스윕 결과의 차이는 코너
때문이지 판정 기준 때문이 아니다.

문턱을 새로 만들지 않는다 - 실패의 정의는 `evaluate_criteria` 의 `overall_pass`
이고 그것은 스펙이 이미 선언한 임계값이다.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from analogcoder.area import total_area
from analogcoder.json_io import json_safe
from analogcoder.optimizer import OptimizerAgents, run_area_optimization
from analogcoder.pvt import run_full_pvt_sweep
from analogcoder.simulators.cache import CachingSimulator
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.state import RunState

REPO = Path(__file__).resolve().parent.parent
SPEC_A = REPO / "benchmarks/bandgap/spec.yaml"
SPEC_B = REPO / "benchmarks/bandgap/spec_pvt.yaml"


def _criteria_key(spec) -> dict:
    return {c.name: (c.measurement, c.operator, c.threshold) for c in spec.all_criteria}


def _relative_slack(criterion, actual: float | None) -> float | None:
    """임계값 대비 남은 상대 여유. 부호는 통과 방향이 양수다.

    스케일은 `max(|threshold|, |actual|)` 이다 - 임계값이 0 인 기준에서
    0 으로 나누지 않기 위해서이고, 이 저장소가 개선량 정의에서 이미 쓰는
    스케일과 같다. **판정에는 쓰지 않는다**(사전 등록의 "부수적으로 기록하되
    판정에 쓰지 않는 것"). 2 단계가 읽을 값이다.
    """
    if actual is None:
        return None
    scale = max(abs(criterion.threshold), abs(actual))
    if scale == 0.0:
        return 0.0
    if criterion.operator in (">=", ">"):
        return (actual - criterion.threshold) / scale
    return (criterion.threshold - actual) / scale


def _texts(spec) -> dict[str, str]:
    return {tb.name: Path(tb.netlist_path).read_text(encoding="utf-8") for tb in spec.testbenches}


def main() -> int:
    spec_a = load_spec(str(SPEC_A))
    spec_b = load_spec(str(SPEC_B))

    # 교란 요인을 재확인한다. 두 스펙의 기준이 갈리면 스윕 실패가 코너 때문인지
    # 임계값 때문인지 말할 수 없으므로, 그때는 재지 않고 멈춘다.
    if _criteria_key(spec_a) != _criteria_key(spec_b):
        print("REFUSED: 두 스펙의 기준이 동일하지 않다 - 측정이 코너를 격리하지 못한다")
        return 2

    out_dir = REPO / "runs" / "area_guard_measurement"
    out_dir.mkdir(parents=True, exist_ok=True)

    state = RunState(
        run_dir=str(out_dir), testbench_names=[tb.name for tb in spec_a.testbenches]
    )
    start_texts = _texts(spec_a)
    state.push_netlist_version(start_texts)

    backend = CachingSimulator(NgspiceBackend())

    async def simulate(netlist_texts, spec_arg):
        measurements: dict = {}
        warnings: list = []
        for tb in spec_arg.testbenches:
            raw = backend.run(netlist_texts[tb.name], tb.control_block)
            measurements.update(raw.measurements)
            warnings.extend(raw.warnings)
        return {"measurements": measurements, "status": "success", "warnings": warnings}

    # --- 런 A: 코너 없는 스펙에서 면적 단계 ---------------------------------
    t0 = time.time()
    area_before = total_area(start_texts[spec_a.canonical.name]).area
    result = await_run(
        run_area_optimization(
            start_texts, spec_a, state, OptimizerAgents(propose=None, simulate=simulate)
        )
    )
    landed = state.current_netlist_texts()
    area_after = total_area(landed[spec_a.canonical.name]).area
    t_a = time.time() - t0

    accepted = result.get("steps_accepted", 0)
    print(f"\n[A] status={result['status']}  수락 {accepted} / 거절 {result.get('steps_rejected')}")
    print(f"[A] 면적 {area_before:.6g} -> {area_after:.6g}  ({(1 - area_after / area_before) * 100:.2f}%)")
    print(f"[A] {t_a:.1f}s")

    record: dict = {
        "preregistration": "docs/superpowers/specs/2026-08-02-area-phase-guard-measurement-design.md",
        "run_a": {
            "spec": str(SPEC_A.relative_to(REPO)),
            "status": result["status"],
            "steps_accepted": accepted,
            "steps_rejected": result.get("steps_rejected"),
            "area_before": area_before,
            "area_after": area_after,
            "unguarded_criteria": result.get("unguarded_criteria"),
            "seconds": t_a,
        },
    }

    # 사전 등록의 규칙 3: 수락된 스텝이 0 이면 판정은 무효다. 실패도 성공도
    # 아니고, 조건이 발생하지 않은 측정이다.
    if accepted == 0:
        record["verdict"] = "void"
        record["reason"] = (
            "런 A 가 스텝을 하나도 수락하지 않았다 - 깨뜨릴 수락 스텝이 없으므로 "
            "이 표본은 질문에 답할 수 없다(사전 등록 규칙 3)"
        )
        _write(out_dir, record)
        print("\n판정: VOID - 수락된 스텝이 0 이다")
        return 0

    # --- 주 판정: A 가 착지한 덱을 B 의 45 코너 그리드로 전체 스윕 ----------
    print(f"\n[B] 45 코너 x 5 테스트벤치 스윕 시작 (오래 걸린다)...")
    t1 = time.time()
    sweep = run_full_pvt_sweep(landed, spec_b, backend)
    t_b = time.time() - t1

    failed = [c for c in sweep["criteria"] if not c["pass"]]
    slacks = {}
    by_name = {c.name: c for c in spec_b.all_criteria}
    for c in sweep["criteria"]:
        crit = by_name.get(c["name"])
        if crit is not None:
            slacks[c["name"]] = _relative_slack(crit, c.get("actual"))

    record["run_b_sweep"] = {
        "spec": str(SPEC_B.relative_to(REPO)),
        "corners": len(spec_b.pvt_corners.corners),
        "overall_pass": sweep["overall_pass"],
        "failed_criteria": [
            {
                "name": c["name"],
                "actual": c.get("actual"),
                "target": c.get("target"),
                "worst_corner": sweep.get("worst_case_corners", {}).get(c["name"]),
            }
            for c in failed
        ],
        "relative_slack": slacks,
        "seconds": t_b,
    }

    # 사전 등록의 규칙 1/2. 문턱은 없다 - overall_pass 가 곧 판정이다.
    if sweep["overall_pass"]:
        record["verdict"] = "keep"
        record["reason"] = (
            "A 가 착지시킨 덱이 45 코너 전부에서 22 개 기준을 통과했다 - 트리거가 "
            "발화하지 않았다. 이것은 '이 결정이 안전하다'가 아니라 '이 회로에서 "
            "이번에 안 깨졌다'의 증거다"
        )
        print(f"\n판정: KEEP - 45 코너 전부 통과 ({t_b:.1f}s)")
    else:
        record["verdict"] = "revert"
        record["reason"] = (
            f"A 가 착지시킨 덱이 코너 스윕에서 {len(failed)} 개 기준을 실패했다 - "
            "트리거가 발화했다. 면적 단계에 여유분 하한을 도입하고, 그 값은 다시 "
            "사전 등록해서 정한다"
        )
        print(f"\n판정: REVERT - {len(failed)} 개 기준 실패 ({t_b:.1f}s)")
        for c in failed:
            print(f"  {c['name']}: {c.get('actual')} vs {c.get('target')}")

    _write(out_dir, record)
    return 0


def _write(out_dir: Path, record: dict) -> None:
    path = out_dir / "measurement.json"
    path.write_text(json.dumps(json_safe(record), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"기록: {path}")


def await_run(coro):
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    sys.exit(main())
