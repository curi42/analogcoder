"""면적 단계의 여유분 하한 측정.

Task 4: 사전 등록 `docs/superpowers/specs/2026-08-02-area-phase-margin-floor-design.md`
(커밋 `0c6d7f3`, 이 스크립트를 확장하기 전에 고정됨)의 규칙(F1/F2/F3), 값 격자,
쌍(P1/P2), 판정 규칙 1~4, timeout, P2 오염 처리를 **그대로** 구현한다. 규칙을
다시 쓰지 않는다 - 사전 등록 문서를 읽고 이 코드가 그것을 따르게 한다.

이 파일은 원래 한 조합(bandgap, 하한 없음)만 돌리던 스크립트
(`docs/superpowers/specs/2026-08-02-area-phase-guard-measurement-design.md`,
커밋 `295d309`)를 조합 루프로 확장한 것이다. 그 스크립트가 겪은 두 실패는
그대로 옮긴다:

- **`resolve_includes`를 덱 원문에 반드시 먼저 먹인다.** 안 하면 상대
  `.include`가 미해결로 남고, 텍스트를 다른 디렉터리에 쓰는 순간 ngspice가
  모델을 못 찾는다. 증상은 조용하다 - 측정값이 비고, 모든 후보가 "기준
  불통과"로 거절되고, 단계는 UNCHANGED로 깨끗하게 끝난다.
- **계측기 검증은 재기 전에, 그리고 각 쌍에 대해 한다.** 기준선 시뮬레이션이
  기준이 요구하는 측정값을 실제로 내놓는지 먼저 확인하고, 아니면 재지 않고
  멈춘다. VOID(조건이 발생하지 않음)와 REFUSED(계측기가 고장)는 다른 사실이다.

세 번째 도구화 결정(이 스크립트가 새로 지는 것): **조합 하나는 별도 프로세스로
돈다.** 사전 등록의 10분 timeout을 프로세스 경계 없이 이 저장소의 코드로
강제하려면 asyncio 안에서 진짜 선점이 필요한데, `agents.propose=None`인 이
단계의 호출 사슬은 실제로 매달리는 await(진짜 I/O 대기)가 하나도 없다 -
`subprocess.run`이 동기로 블로킹하므로 `asyncio.wait_for`의 타이머가 끼어들
지점이 없다. 그래서 조합 하나를 `--run-one`으로 별도 프로세스에 맡기고, 그
프로세스를 실행하는 쪽(오케스트레이터 - 사람이거나 셸 루프)이 진짜 OS
타임아웃(`timeout(1)` 또는 이 파일을 부르는 호출부의 timeout)으로 시간을
강제한다. 조합끼리 프로세스가 갈리므로 `CachingSimulator`도 조합마다
새로 만들어진다 - 사전 등록이 이미 적어 둔 사실("조합 간 적중률을 미리 알 수
없다")과 어긋나지 않는다: 공유 캐시는 비용을 줄이는 최적화였지 판정의 일부가
아니었다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from analogcoder.area import total_area
from analogcoder.json_io import json_safe
from analogcoder.judge_tools import relative_slack
from analogcoder.netlist import resolve_includes
from analogcoder.optimizer import MarginFloor, OptimizerAgents, run_area_optimization
from analogcoder.pvt import run_full_pvt_sweep
from analogcoder.simulators.cache import CachingSimulator
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.state import RunState

REPO = Path(__file__).resolve().parent.parent

# --- 사전 등록이 고정한 쌍과 격자. 넓히지 않는다. -----------------------------

PAIRS: dict[str, dict] = {
    "P1": {
        "no_corner": REPO / "benchmarks/bandgap/spec.yaml",
        "corner": REPO / "benchmarks/bandgap/spec_pvt.yaml",
        "grid": 45,
        "criteria": 22,
        "degn_check": False,
    },
    "P2": {
        "no_corner": REPO / "benchmarks/two_stage_opamp/spec.yaml",
        "corner": REPO / "benchmarks/two_stage_opamp/spec_pvt.yaml",
        "grid": 45,
        "criteria": 7,
        "degn_check": True,
    },
}

GRID: dict[str, list[float]] = {
    "f1": [0.02, 0.05, 0.10, 0.20],
    "f2": [0.25, 0.50, 0.75],
}

# 사전 등록: "조합 하나가 10분을 넘기면 그 조합을 timeout으로 기록하고
# 다음으로 간다."
TIMEOUT_S = 600

# 사전 등록: "런 시작 시점의 값에서 2배 이상 벗어난 스텝이 하나라도 있으면
# P2 전체를 contaminated로 기록하고 판정에서 제외한다." 두 문서화된 상태
# (0.0119V / 0.0626V)는 5.3배 떨어져 있으므로 2배는 전이를 놓치지 않으면서
# 정상적인 사이징 변동을 전이로 오인하지도 않는다 - 대칭으로 적용한다
# (위로 2배든 아래로 1/2배든 같은 사실이다).
DEGN_RATIO_HIGH = 2.0
DEGN_RATIO_LOW = 0.5

# `xdut.degn` - Xn2(자기 바이어스 beta-multiplier)와 Rdeg의 접점. 이 이름은
# `benchmarks/two_stage_opamp/netlist.cir`가 DUT를 `Xdut ... OPAMP2STAGE`로
# 인스턴스화하는 것에서 나온 사실이지 추측이 아니다(node "degn"은
# `OPAMP2STAGE`의 body 안에서 정의되므로 top에서는 인스턴스 이름으로 스코프된
# `xdut.degn`이다) - `.control op\nprint all\n.endc`로 실측 확인했다.
DEGN_NODE = "xdut.degn"
DEGN_PROBE_CONTROL = f".control\nop\nlet degn_v = v({DEGN_NODE})\nprint degn_v\n.endc\n"


def _criteria_key(spec) -> dict:
    return {c.name: (c.measurement, c.operator, c.threshold) for c in spec.all_criteria}


def _texts(spec) -> dict[str, str]:
    """덱 텍스트를 **재배치 가능한** 형태로 읽는다.

    `resolve_includes`를 빼면 상대 `.include`가 미해결로 남고, 텍스트를 다른
    디렉터리에 쓰는 순간 ngspice가 모델을 못 찾는다. 증상은 조용하다 - 측정값이
    비고, `_run_simulation`이 그것을 삼키고, 모든 후보가 "기준 불통과"로 거절되고,
    단계는 `UNCHANGED`로 깨끗하게 끝난다. `scripts/dc_solution_uniqueness.py`의
    다섯 번째 실패가 정확히 이것이고, 원래 이 스크립트(단일 조합 버전)도
    첫 시도에서 그대로 밟았다."""
    return {
        tb.name: resolve_includes(
            Path(tb.netlist_path).read_text(encoding="utf-8"),
            str(Path(tb.netlist_path).parent),
        )
        for tb in spec.testbenches
    }


def _make_simulate(sim_dir: Path, backend: CachingSimulator):
    """면적 탐색이 매 후보마다 부르는 콜러블. `SimulatorBackend.run`은
    **경로와 dict**를 받는다(텍스트가 아니다) - 원래 스크립트가 이 자리에서
    한 번 실수했다(`tests/unit/test_optimizer_area_phase_ngspice.py`의 배선이
    정답이고, 이 함수는 그것과 같은 모양이어야 한다)."""

    async def simulate(netlist_texts: dict[str, str], spec_arg) -> dict:
        measurements: dict = {}
        for tb in spec_arg.testbenches:
            path = sim_dir / f"{tb.name}.cir"
            path.write_text(netlist_texts[tb.name], encoding="utf-8")
            measurements.update(
                backend.run(str(path), {"control_block": tb.control_block}).measurements
            )
        return {"measurements": measurements}

    return simulate


async def verify_instrument(pair_name: str, spec_nc, backend: CachingSimulator, sim_dir: Path) -> dict:
    """계측기 검증 - **재기 전에** 한다. 첫 시도가 이 검증 없이 돌아 VOID를
    냈고, 그 VOID는 "조건이 발생하지 않았다"가 아니라 "계측기가 죽었다"였다.
    기준선 시뮬레이션이 기준이 요구하는 측정값을 실제로 내놓는지 확인하고,
    아니면 REFUSED를 돌려준다 - 재지 않는다.

    **async다** - `_run_combination_async` 안(이미 도는 이벤트 루프 위)에서
    부르므로 여기서 `asyncio.run`을 새로 쓰면 "cannot be called from a running
    event loop"로 터진다. 최상위 진입점(`verify_pairs`)만 `asyncio.run`으로
    감싼다."""
    simulate = _make_simulate(sim_dir, backend)
    texts = _texts(spec_nc)
    baseline = await simulate(texts, spec_nc)
    wanted = {c.measurement for c in spec_nc.all_criteria}
    got = {k for k, v in baseline["measurements"].items() if v is not None}
    missing = sorted(wanted - got)
    ok = not missing
    return {
        "pair": pair_name,
        "wanted": len(wanted),
        "missing": missing,
        "ok": ok,
    }


def probe_degn(text: str, backend: CachingSimulator, sim_dir: Path, tag: str) -> tuple[float | None, str | None]:
    """이 텍스트의 DC 동작점에서 `xdut.degn`의 전압. 실패하면 (None, 사유)."""
    path = sim_dir / f"degn_probe_{tag}.cir"
    path.write_text(text, encoding="utf-8")
    raw = backend.run(str(path), {"control_block": DEGN_PROBE_CONTROL})
    value = raw.measurements.get("degn_v")
    if value is None:
        return None, f"status={raw.status} failure_kind={getattr(raw, 'failure_kind', None)}"
    return value, None


def check_degn_contamination(
    state: RunState, canonical_name: str, backend: CachingSimulator, sim_dir: Path
) -> dict:
    """P2의 각 수락 스텝에서 degn을 재고, 시작값에서 2배 이상 벗어난 스텝이
    있으면 contaminated=True.

    `state.netlist_versions[canonical_name]`는 baseline(v0) + **수락된** 버전만
    담는다 - 거절된 후보는 `_optimize`가 매 시도마다 push했다가 거절되면
    `state.rollback()`으로 pop하므로, 이 목록에 남는 것은 정확히 "각 수락
    스텝"이다(코너 인식이 아니므로 이분 탐색으로 되돌려지는 경로도 없다).
    별도로 탐색 내부를 훅킹하지 않고 이 사실 하나로 정확히 사전 등록이 말하는
    집합을 얻는다."""
    versions = state.netlist_versions[canonical_name]
    start_text = Path(versions[0]).read_text(encoding="utf-8")
    start_v, start_err = probe_degn(start_text, backend, sim_dir, "v0")

    steps = []
    contaminated = False
    for i, path in enumerate(versions[1:], start=1):
        text = Path(path).read_text(encoding="utf-8")
        v, err = probe_degn(text, backend, sim_dir, f"v{i}")
        ratio = (v / start_v) if (v is not None and start_v not in (None, 0.0)) else None
        flipped = ratio is not None and (ratio >= DEGN_RATIO_HIGH or ratio <= DEGN_RATIO_LOW)
        if flipped:
            contaminated = True
        steps.append(
            {"version": i, "degn_v": v, "ratio_to_start": ratio, "flipped": flipped, "probe_error": err}
        )

    return {
        "start_degn_v": start_v,
        "start_probe_error": start_err,
        "accepted_step_count": len(steps),
        "accepted_step_probes": steps,
        "contaminated": contaminated,
    }


async def _run_combination_async(
    rule: str, value: float, pair_name: str, out_dir: Path
) -> dict:
    """(F, value, Pair) 하나. 코너 없는 스펙에서 하한 F(value)를 켠 면적
    단계를 돌리고, 착지한 덱을 짝의 45코너 그리드로 전체 스윕한다.

    **안전** = 스윕의 `overall_pass`. **유용** = 면적 감소율 > 0(문턱 없음).
    """
    pair = PAIRS[pair_name]
    spec_nc = load_spec(str(pair["no_corner"]))
    spec_c = load_spec(str(pair["corner"]))

    if _criteria_key(spec_nc) != _criteria_key(spec_c):
        return {
            "outcome": "refused",
            "reason": "두 스펙의 기준이 동일하지 않다 - 측정이 코너를 격리하지 못한다",
        }

    sim_dir = out_dir / "sim"
    sim_dir.mkdir(parents=True, exist_ok=True)

    backend = CachingSimulator(NgspiceBackend(timeout=180))
    inst = await verify_instrument(pair_name, spec_nc, backend, sim_dir)
    if not inst["ok"]:
        return {
            "outcome": "refused",
            "reason": "기준선 시뮬레이션이 기준이 요구하는 측정값을 내지 못한다 - 계측기 고장",
            "instrument": inst,
        }

    canonical_name = spec_nc.canonical.name
    state = RunState(run_dir=str(out_dir), testbench_names=[tb.name for tb in spec_nc.testbenches])
    start_texts = _texts(spec_nc)
    state.push_netlist_version(start_texts)
    area_before = total_area(start_texts[canonical_name]).area

    simulate = _make_simulate(sim_dir, backend)
    agents = OptimizerAgents(propose=None, simulate=simulate)
    margin_floor = MarginFloor(rule=rule, value=value)

    t0 = time.time()
    result = await run_area_optimization(
        start_texts, spec_nc, state, agents, margin_floor=margin_floor
    )
    t_area = time.time() - t0

    landed = state.current_netlist_texts()
    area_after = total_area(landed[canonical_name]).area
    accepted = result.get("steps_accepted", 0)
    reduction = (1.0 - area_after / area_before) if area_before else None

    degn = None
    if pair["degn_check"]:
        degn = check_degn_contamination(state, canonical_name, backend, sim_dir)

    t1 = time.time()
    sweep = run_full_pvt_sweep(landed, spec_c, backend)
    t_sweep = time.time() - t1

    failed = [c for c in sweep["criteria"] if not c["pass"]]
    by_name = {c.name: c for c in spec_c.all_criteria}
    slacks = {}
    for c in sweep["criteria"]:
        crit = by_name.get(c["name"])
        if crit is not None:
            slacks[c["name"]] = relative_slack(crit, c.get("actual"))

    safe = bool(sweep.get("overall_pass"))
    useful = reduction is not None and reduction > 0.0

    return {
        "outcome": "completed",
        "rule": rule,
        "value": value,
        "pair": pair_name,
        "no_corner_spec": str(pair["no_corner"].relative_to(REPO)),
        "corner_spec": str(pair["corner"].relative_to(REPO)),
        "area_phase": {
            "status": result["status"],
            "steps_accepted": accepted,
            "steps_rejected": result.get("steps_rejected"),
            "area_before": area_before,
            "area_after": area_after,
            "reduction_ratio": reduction,
            "unguarded_criteria": result.get("unguarded_criteria"),
            "tightest_slack": result.get("tightest_slack"),
            "seconds": t_area,
        },
        "sweep": {
            "corners": len(spec_c.pvt_corners.corners),
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
            "seconds": t_sweep,
        },
        "degn": degn,
        "safe": safe,
        "useful": useful,
    }


def run_one(pair_name: str, rule: str, value: float, out_dir: Path) -> int:
    """조합 하나를 돌리고 `out_dir/result.json`에 쓴다. 예외를 삼키고
    `outcome="error"`로 적는다 - 이 프로세스가 죽으면 오케스트레이터가 timeout과
    구별할 수 없게 되므로, 어떤 실패든 파일 하나는 남기는 편이 낫다(진짜
    timeout은 이 프로세스 자체가 OS에 의해 죽는 경우라 여기서 처리할 수 없고,
    호출부의 몫이다)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    combo_id = f"{pair_name}_{rule}_{value}"
    t0 = time.time()
    try:
        record = asyncio.run(_run_combination_async(rule, value, pair_name, out_dir))
    except Exception as exc:  # noqa: BLE001 - 이 프로세스가 무엇을 내든 파일로 남긴다
        record = {"outcome": "error", "reason": f"{type(exc).__name__}: {exc}"}
    record.setdefault("rule", rule)
    record.setdefault("value", value)
    record.setdefault("pair", pair_name)
    record["combo_id"] = combo_id
    record["wall_seconds"] = time.time() - t0

    result_path = out_dir / "result.json"
    result_path.write_text(
        json.dumps(json_safe(record), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[{combo_id}] outcome={record['outcome']} ({record['wall_seconds']:.1f}s) -> {result_path}")
    return 0


def verify_pairs() -> int:
    """Step 1: 두 쌍의 계측기를 검증한다. 조합 하나를 돌리기 전에, 각 쌍에
    대해 한 번씩 - 기준선 시뮬레이션이 기준이 요구하는 측정값을 실제로
    내놓는지, 그리고 코너 없는/코너 스펙의 기준이 동일한지."""
    ok = True
    for pair_name, pair in PAIRS.items():
        spec_nc = load_spec(str(pair["no_corner"]))
        spec_c = load_spec(str(pair["corner"]))
        if _criteria_key(spec_nc) != _criteria_key(spec_c):
            print(f"[{pair_name}] REFUSED: 두 스펙의 기준이 동일하지 않다")
            ok = False
            continue
        sim_dir = REPO / "runs" / "area_margin_floor_measurement" / f"_verify_{pair_name}" / "sim"
        sim_dir.mkdir(parents=True, exist_ok=True)
        backend = CachingSimulator(NgspiceBackend(timeout=180))
        inst = asyncio.run(verify_instrument(pair_name, spec_nc, backend, sim_dir))
        status = "OK" if inst["ok"] else "REFUSED"
        print(f"[{pair_name}] {status}: 요구 {inst['wanted']}개, 없는 것 {inst['missing']}")
        ok = ok and inst["ok"]
    return 0 if ok else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_mutually_exclusive_group(required=True)
    sub.add_argument("--verify", action="store_true", help="Step 1: 두 쌍의 계측기 검증")
    sub.add_argument(
        "--run-one",
        nargs=4,
        metavar=("PAIR", "RULE", "VALUE", "OUT_DIR"),
        help="조합 하나를 돌리고 OUT_DIR/result.json에 쓴다",
    )
    args = parser.parse_args()

    if args.verify:
        return verify_pairs()

    pair_name, rule, value_s, out_dir_s = args.run_one
    if pair_name not in PAIRS:
        print(f"알 수 없는 쌍: {pair_name!r} (있는 것: {sorted(PAIRS)})")
        return 2
    if rule not in GRID:
        print(f"알 수 없는 규칙: {rule!r} (있는 것: {sorted(GRID)})")
        return 2
    value = float(value_s)
    if value not in GRID[rule]:
        print(f"격자에 없는 값: {rule}={value} (격자: {GRID[rule]}) - 사후에 값을 추가하지 않는다")
        return 2
    return run_one(pair_name, rule, value, Path(out_dir_s))


if __name__ == "__main__":
    sys.exit(main())
