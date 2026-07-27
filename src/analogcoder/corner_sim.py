import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass

from analogcoder.corner_selection import NOMINAL, CornerSet, label, next_probe, promote
from analogcoder.judge_tools import evaluate_criteria
from analogcoder.pvt import CornerPoint, render_corner_netlist, worst_case_measurements
from analogcoder.simulators.base import RawSimResult


@dataclass
class CornerState:
    """선택 집합을 담는 **가변** 상자.

    CornerSet 자체는 frozen이고 promote/grown_with가 새 인스턴스를 만든다.
    simulate_fn은 반복 안에서 승격을 일으키므로 그 결과를 되돌려 놓을 곳이
    필요하고, cli.py의 재진입 루프는 같은 상자를 계속 들고 있어야 한다."""

    corner_set: CornerSet


def _probe_enabled(spec) -> bool:
    """스펙이 탐침을 껐는지. 블록이 없으면 CornerReduction의 기본값(True)을
    따른다 - 이 함수가 도는 시점에는 축소가 이미 켜져 있다."""
    reduction = getattr(spec, "corner_reduction", None)
    return True if reduction is None else reduction.probe


def _run_point(
    sim_backend,
    netlist_text: str,
    point: CornerPoint | None,
    control_block: str,
    benchmark_dir: str,
    nominal_path: str,
) -> RawSimResult:
    """한 점에서의 직접 시뮬레이션. NOMINAL은 **덱 그대로** - 렌더링을 거치면
    그것은 더 이상 임계값이 정해진 그 덱이 아니다. 코너는
    run_full_pvt_sweep과 같은 "임시 디렉터리에 렌더링해서 돌린다" 패턴."""
    if point is NOMINAL:
        return sim_backend.run(nominal_path, {"control_block": control_block})

    rendered = render_corner_netlist(
        netlist_text, point.process, point.voltage, point.temperature, benchmark_dir
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        netlist_path = os.path.join(tmpdir, "corner.cir")
        with open(netlist_path, "w") as f:
            f.write(rendered)
        return sim_backend.run(netlist_path, {"control_block": control_block})


def build_corner_simulate(
    agent_simulate, sim_backend, state, corner_state: CornerState, log_event
) -> Callable:
    """기존 simulate_fn 계약을 지키면서, 판정을 선택 코너들의 최악값으로 바꾼다.

    에이전트는 nominal에서만 돌고 그 기여분은 정확히 두 가지다 - 수렴하는
    control block을 찾는 것과 status를 보고하는 것. 측정값은 전부 직접 경로에서
    나온다. 그래야 모든 코너의 키 집합이 같다: 에이전트가 내놓는 키와
    sim_backend가 내놓는 키가 다를 수 있다는 것은 이미 기록된 사실이고, 최악값을
    두 경로에 걸쳐 뽑으면 그 차이가 판정에 들어간다. nominal을 직접 경로로 한 번
    더 도는 비용은 그 차이를 판정에서 빼기 위한 값이다.

    반환 dict는 기존 키(status/measurements/by_testbench)에 corner_worst와
    probe를 **더한다** - 추가 키이므로 기존 소비자는 영향받지 않는다."""

    async def simulate_fn(netlist_texts, spec):
        benchmark_dir = os.path.dirname(spec.canonical.netlist_path)
        cs = corner_state.corner_set
        probe_point, cs = next_probe(cs) if _probe_enabled(spec) else (None, cs)

        status = "success"
        by_testbench: dict = {}
        # 한 코너의 측정값 전체가 테스트벤치 반복에 걸쳐 흩어진다(바깥이
        # 테스트벤치, 안이 코너). run_full_pvt_sweep과 같은 이유로 점별로
        # 누적한다.
        per_point: dict = {point: {} for point in cs.corners}
        probe_measurements: dict = {}
        probe_error: str | None = None

        paths = state.current_netlist_paths()
        for tb in spec.testbenches:
            agent_result = await agent_simulate(paths[tb.name], tb.control_block)
            by_testbench[tb.name] = agent_result
            # 기본값 "success"에 도달하는 경로는 오늘 없다 - SIMULATION_SCHEMA가
            # status를 required로 둔다. 그래도 없는 키를 실패로 읽으면 스키마가
            # 느슨해지는 날 시뮬레이터 에이전트 전체가 조용히 실패로 접힌다.
            if status == "success" and agent_result.get("status", "success") != "success":
                status = agent_result["status"]
            # 에이전트가 수렴시킨 control block을 코너가 그대로 쓴다. 스펙
            # 원문으로 돌아가면 수렴 재시도의 이득을 코너가 못 받는다.
            # 폴백은 에이전트가 아무것도 주지 않았을 때만이다.
            control_block = agent_result.get("control_block") or tb.control_block

            for point in cs.corners:
                raw = _run_point(
                    sim_backend,
                    netlist_texts[tb.name],
                    point,
                    control_block,
                    benchmark_dir,
                    paths[tb.name],
                )
                per_point[point].update(raw.measurements)
                if status == "success" and raw.status != "success":
                    status = raw.status

            if probe_point is not None and probe_error is None:
                # 탐침은 판정에 참여하지 않으므로, 실패해도 이 반복을 멈출
                # 근거가 없다. 그 반복의 탐침을 없던 것으로 하고 기록만 남긴다.
                try:
                    raw = _run_point(
                        sim_backend,
                        netlist_texts[tb.name],
                        probe_point,
                        control_block,
                        benchmark_dir,
                        paths[tb.name],
                    )
                except Exception as exc:  # noqa: BLE001 - 탐침은 무엇으로도 실행을 멈추지 못한다
                    probe_error = f"{type(exc).__name__}: {exc}"
                else:
                    probe_measurements.update(raw.measurements)

        # 판정에 들어가는 것은 **선택 집합뿐**이다. 탐침을 섞으면 축소 집합이
        # 항상 낙관적이라는 논증이 무너진다 - 그 논증이 이 방식을 안전하게
        # 만드는 전부다.
        measurements, corner_worst = worst_case_measurements(
            list(cs.corners), [per_point[p] for p in cs.corners], spec.all_criteria
        )

        probe_record = None
        if probe_point is not None:
            if probe_error is not None:
                probe_record = {"corner": label(probe_point), "error": probe_error}
            else:
                failed = not evaluate_criteria(probe_measurements, spec.all_criteria)["overall_pass"]
                if failed:
                    cs = promote(cs, probe_point)
                probe_record = {
                    "corner": label(probe_point),
                    "failed": failed,
                    "promoted": failed,
                }
            log_event("corner_probe", probe_record)

        corner_state.corner_set = cs
        return {
            "status": status,
            "measurements": measurements,
            "by_testbench": by_testbench,
            "corner_worst": corner_worst,
            "probe": probe_record,
        }

    return simulate_fn
