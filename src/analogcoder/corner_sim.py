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
    """스펙이 탐침을 껐는지. TargetSpec은 corner_reduction 필드를 항상 들고
    있고(선언이 없으면 None), None이면 CornerReduction의 기본값(True)을 따른다.
    getattr 기본값을 두면 "필드가 없다"가 조용히 "탐침 켜짐"이 되는데, 그것은
    이 저장소가 반복해서 당한 조용한 기본값 사고와 같은 모양이다."""
    reduction = spec.corner_reduction
    return True if reduction is None else reduction.probe


def _check_deck_matches_state(tb_name: str, netlist_text: str, nominal_path: str) -> None:
    """nominal은 파일을, 코너는 인자를 읽는다 - 그래서 둘이 같아야 한다.

    이 모듈의 안전 논증은 "모든 점이 같은 경로에서 나오므로 키 집합이 구조적으로
    같다"이다. 그런데 netlist_texts가 state보다 한 버전 뒤처지면 nominal은 새 덱을,
    코너는 옛 덱을 돌면서도 **키 집합은 여전히 같다** - 아무도 눈치채지 못한 채
    서로 다른 두 회로의 최악값이 하나의 판정이 된다. 오늘 프로덕션 호출부
    네 곳은 전부 push 후에 시뮬레이션하므로 성립하지만, 그 결합은 보이지 않는다.

    ValueError인 이유는 run_orchestration이 이미 ValueError를 잡아 깨끗한
    FAIL로 접기 때문이다 - 넷리스트 적용 경로의 ValueError와 같은 취급."""
    with open(nominal_path) as f:
        on_disk = f.read()
    if on_disk != netlist_text:
        raise ValueError(
            f"testbench {tb_name!r}: the netlist_texts argument does not match the "
            f"current RunState version at {nominal_path}. nominal simulates that file "
            f"while every corner renders the argument, so a mismatch would silently "
            f"judge two different circuits together. Push the texts into RunState "
            f"before simulating."
        )


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
    run_full_pvt_sweep과 같은 "임시 디렉터리에 렌더링해서 돌린다" 패턴.

    두 갈래가 서로 다른 출처를 읽는다는 점에 주의할 것: NOMINAL은 nominal_path의
    **파일**을, 코너는 netlist_text **인자**를 읽는다. 둘이 같은 덱이어야 한다는
    불변식은 호출부에서 _check_deck_matches_state가 지킨다."""
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
        # 회전은 여기서 한 번만 진행하고, 아래 finally에서 **무조건** 커밋한다.
        # 선택 코너의 시뮬레이션이 터지면 이 함수는 예외를 그대로 올려보내는데
        # (그것은 판정 경로라 삼킬 수 없다), optimizer._run_simulation이 그
        # 예외를 삼키고 계속 돈다 - 그때 회전이 커밋되지 않으면 상자는 조용히
        # 같은 탐침 코너에 영원히 머문다.
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
        try:
            for tb in spec.testbenches:
                _check_deck_matches_state(tb.name, netlist_texts[tb.name], paths[tb.name])
                agent_result = await agent_simulate(paths[tb.name], tb.control_block)
                by_testbench[tb.name] = agent_result
                # 기본값 "success"에 도달하는 경로는 오늘 없다 - SIMULATION_SCHEMA가
                # status를 required로 둔다. 그래도 없는 키를 실패로 읽으면 스키마가
                # 느슨해지는 날 시뮬레이터 에이전트 전체가 조용히 실패로 접힌다.
                # cli.py의 기존 simulate_fn과 **글자 그대로 같은** 값을 싣는다 -
                # 에이전트 쪽 status는 오늘의 동작이고, 여기서 바꿀 이유가 없다.
                # 코너 쪽만 아래에서 좌표를 덧붙이므로, 장식이 붙어 있다는 것
                # 자체가 "이건 코너가 낸 실패다"라는 표시가 된다.
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
                    # 어느 점에서 난 실패인지를 status 문자열에 싣는다.
                    # optimizer._run_simulation은 이 값을 그대로 사유에 적으므로,
                    # 싣지 않으면 "convergence_failure"만 남고 45개 중 어느
                    # 코너였는지가 사라진다. status를 열거값과 **같은지** 보는
                    # 소비자는 없다 - 전부 `!= "success"`로만 읽는다.
                    if status == "success" and raw.status != "success":
                        status = f"{raw.status} at {label(point)} in testbench {tb.name}"

                if probe_point is not None and probe_error is None:
                    # 탐침은 판정에 참여하지 않으므로, 실패해도 이 반복을 멈출
                    # 근거가 없다. 그 반복의 탐침 **결과**를 없던 것으로 하고
                    # (판정도 승격도 없다) 기록만 남긴다. 남은 테스트벤치의
                    # 탐침도 건너뛴다 - 일부 테스트벤치의 측정값만으로 판정하면
                    # 나머지가 "측정값 없음"으로 실패해 크래시를 근거로 코너를
                    # 승격시킨다. 회전은 그대로 진행된 채 커밋되므로, 터진
                    # 코너는 다음 한 바퀴 뒤에 다시 온다.
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

            # 탐침의 판정·승격·기록은 테스트벤치 루프가 **끝난 뒤** 정확히 한 번이다.
            # 루프 안으로 들어가면 테스트벤치 하나 분량의 측정값으로 판정하게 되어
            # 나머지 기준이 "측정값 없음"으로 실패하고, 그 근거로 코너가 승격된다.
            probe_record = None
            if probe_point is not None:
                if probe_error is not None:
                    # 정상 경로와 **같은 모양**이어야 한다. failed/promoted를
                    # 빼면 record["failed"]를 읽는 소비자는 KeyError가 나고,
                    # record.get("failed")를 읽는 소비자는 터진 탐침을 통과한
                    # 탐침으로 읽는다. 터진 탐침은 아무것도 판정하지 않았으므로
                    # 둘 다 False이고, 그것을 구분하는 것은 error 키다.
                    probe_record = {
                        "corner": label(probe_point),
                        "failed": False,
                        "promoted": False,
                        "error": probe_error,
                    }
                else:
                    failed = not evaluate_criteria(
                        probe_measurements, spec.all_criteria
                    )["overall_pass"]
                    if failed:
                        cs = promote(cs, probe_point)
                    probe_record = {
                        "corner": label(probe_point),
                        "failed": failed,
                        "promoted": failed,
                    }
                log_event("corner_probe", probe_record)

            return {
                "status": status,
                "measurements": measurements,
                "by_testbench": by_testbench,
                "corner_worst": corner_worst,
                "probe": probe_record,
            }
        finally:
            # 상자의 계약은 무조건이다 - 이 함수가 어떻게 끝나든 회전(과 일어난
            # 승격)은 커밋된다.
            corner_state.corner_set = cs

    return simulate_fn
