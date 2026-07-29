import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass

from analogcoder.corner_selection import NOMINAL, CornerSet, label, next_probe, promote
from analogcoder.judge_tools import evaluate_criteria
from analogcoder.pvt import (
    CornerPoint,
    deck_for_corner,
    render_corner_netlist,
    worst_case_measurements,
)
from analogcoder.simulators.base import RawSimResult
from analogcoder.simulators.cache import attach_log_event
from analogcoder.simulators.parallel import map_points


@dataclass
class CornerState:
    """선택 집합을 담는 **가변** 상자.

    CornerSet 자체는 frozen이고 promote/grown_with가 새 인스턴스를 만든다.
    simulate_fn은 반복 안에서 승격을 일으키므로 그 결과를 되돌려 놓을 곳이
    필요하고, cli.py의 재진입 루프는 같은 상자를 계속 들고 있어야 한다."""

    corner_set: CornerSet
    # **최적화 탐색이 도는 동안 회전을 멈춘다.**
    #
    # 최적화기는 이 상자를 메인 루프와 **일부러** 공유한다(선택 집합이 갈라지면
    # 탐색이 메인 루프가 배운 코너를 못 본 채 여유분을 요구한다). 그런데 회전
    # 탐침까지 함께 도니, `_search` 안의 매 시뮬레이션이 탐침을 하나 돌리고
    # 실패하면 코너를 **승격**시킨다. 그러면 `records[version]["objective"]`와
    # `best_objective`가 **서로 다른 코너 집합에서 잰 값**끼리 비교된다:
    # 승격은 최악값 목적을 단조롭게 악화시키므로 그 뒤의 모든 단계가
    # "objective가 현재 최선보다 낮지 않다"는 사유로 거부되고, 그 사유는
    # 원인이 승격인데도 knob 이름을 지목한다. `allowances`도 승격 이전
    # 집합에서 계산된 값이다.
    #
    # 실행이 위험해지지는 않는다(확인과 이분 탐색은 전체 스윕을 쓰고, 이
    # 단계에는 FAIL 결말이 없다). 다만 최적화 단계의 수확이 틀린 사유와 함께
    # 조용히 0이 될 수 있다. 그래서 **선택 집합은 계속 공유하되 회전만**
    # 얼린다 - 탐색이 처음부터 끝까지 같은 기준점 위에서 돈다.
    probe_frozen: bool = False


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
    tb=None,
    nominal_corner=None,
) -> RawSimResult:
    """한 점에서의 직접 시뮬레이션. NOMINAL은 **덱 그대로** - 렌더링을 거치면
    그것은 더 이상 임계값이 정해진 그 덱이 아니다. 코너는
    run_full_pvt_sweep과 같은 "임시 디렉터리에 렌더링해서 돌린다" 패턴.

    두 갈래가 서로 다른 출처를 읽는다는 점에 주의할 것: NOMINAL은 nominal_path의
    **파일**을, 코너는 netlist_text **인자**를 읽는다. 둘이 같은 덱이어야 한다는
    불변식은 호출부에서 _check_deck_matches_state가 지킨다."""
    composed = tb is not None and tb.fragments is not None
    if point is NOMINAL and not composed:
        return sim_backend.run(nominal_path, {"control_block": control_block})

    # 조합형 테스트벤치에는 "렌더링을 거치지 않은 덱"이 디스크에 없다 -
    # `nominal_path`가 가리키는 것은 **tunable 조각**이고 그것만으로는 회로가
    # 아니다. 그래서 NOMINAL도 조합을 거치며, 슬롯은 스펙이 선언한 nominal
    # 코너가 채운다.
    rendered = deck_for_corner(
        tb, netlist_text, point, benchmark_dir, nominal=nominal_corner
    ).text if composed else render_corner_netlist(
        netlist_text, point.process, point.voltage, point.temperature, benchmark_dir
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        netlist_path = os.path.join(tmpdir, "corner.cir")
        with open(netlist_path, "w") as f:
            f.write(rendered)
        return sim_backend.run(netlist_path, {"control_block": control_block})


def _log_corner_render(tb, netlist_text, cs, benchmark_dir, log_event, nominal_corner=None) -> None:
    """이 테스트벤치의 덱을 렌더링하면 세 재작성 중 무엇이 실제로 적용되는지를
    적는다. NOMINAL은 렌더링을 거치지 않으므로, 선택 집합이 NOMINAL뿐이면
    적을 것이 없다 - 그때는 조용히 넘어가는 것이 맞다(재작성을 아무도 요청하지
    않았다).

    대표 코너 하나로 재는 이유: 세 상태는 덱에 그 줄이 있느냐만 보므로 코너
    좌표에 의존하지 않는다. 그래서 존재하는 코너 중 첫 번째를 쓴다 - 가짜
    좌표를 지어내지 않는다(`_corner_fields`가 (deck)에 대해 지키는 것과 같은
    규칙)."""
    representative = next((point for point in cs.corners if point is not NOMINAL), None)
    if representative is None or log_event is None:
        return
    render = deck_for_corner(
        tb, netlist_text, representative, benchmark_dir, nominal=nominal_corner
    )
    log_event(
        "corner_render",
        {"testbench": tb.name, "mode": render.mode, "states": render.states},
    )


def build_corner_simulate(
    agent_simulate, sim_backend, state, corner_state: CornerState, log_event, max_workers=None
) -> Callable:
    """기존 simulate_fn 계약을 지키면서, 판정을 선택 코너들의 최악값으로 바꾼다.

    에이전트는 nominal에서만 돌고 그 기여분은 정확히 두 가지다 - 수렴하는
    control block을 찾는 것과 status를 보고하는 것. 측정값은 전부 직접 경로에서
    나온다. 그래야 모든 코너의 키 집합이 같다: 에이전트가 내놓는 키와
    sim_backend가 내놓는 키가 다를 수 있다는 것은 이미 기록된 사실이고, 최악값을
    두 경로에 걸쳐 뽑으면 그 차이가 판정에 들어간다. nominal을 직접 경로로 한 번
    더 도는 비용은 그 차이를 판정에서 빼기 위한 값이다.

    반환 dict는 기존 키(status/measurements/by_testbench)에 corner_worst와
    probe를 **더한다** - 추가 키이므로 기존 소비자는 영향받지 않는다.

    **선택 코너와 탐침은 한 테스트벤치 안에서 함께 병렬로 돈다.** 그 점들은
    서로 독립이고 각자 자기 임시 디렉터리를 판다. 테스트벤치 **바깥** 루프는
    순차로 남는다 - 그 안에 LLM 호출(`agent_simulate`)이 있고, 코너의 control
    block은 그 호출이 수렴시킨 것을 써야 하기 때문이다. 결과는 점으로 색인해
    모으고 아래에서 `cs.corners` 순서대로 다시 읽으므로 완료 순서는 status에도
    측정값에도 닿지 않는다."""
    attach_log_event(sim_backend, log_event)

    async def simulate_fn(netlist_texts, spec):
        benchmark_dir = os.path.dirname(spec.canonical.netlist_path)
        # 조합형 테스트벤치의 nominal은 **스펙이 선언한 코너**다 - 조합 모델에
        # 렌더링을 거치지 않은 덱은 존재하지 않는다.
        # 단일 파일 경로에서는 아예 묻지 않는다 - 거기서 nominal은 덱 그 자체이고
        # 코너가 아니므로, 없는 것을 기본값으로 채우는 자리를 만들지 않는다.
        composed_here = any(tb.fragments is not None for tb in spec.testbenches)
        nominal_corner = spec.nominal_corner() if composed_here else None
        cs = corner_state.corner_set
        # 회전은 여기서 한 번만 진행하고, 아래 finally에서 **무조건** 커밋한다.
        # 선택 코너의 시뮬레이션이 터지면 이 함수는 예외를 그대로 올려보내는데
        # (그것은 판정 경로라 삼킬 수 없다), optimizer._run_simulation이 그
        # 예외를 삼키고 계속 돈다 - 그때 회전이 커밋되지 않으면 상자는 조용히
        # 같은 탐침 코너에 영원히 머문다.
        # 상자의 얼림 상태는 **호출 시점에** 읽는다 - corner_set과 같다.
        # 최적화 단계가 도는 동안에는 탐침도 승격도 없다(CornerState.probe_frozen).
        probing = _probe_enabled(spec) and not corner_state.probe_frozen
        probe_point, cs = next_probe(cs) if probing else (None, cs)

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
            # **결정론적 게이트는 LLM 호출보다 먼저 돈다** - 이 저장소가 면적
            # 게이트와 refdes 게이트에 대해 문서로 못박아 둔 순서다. 검사를
            # 에이전트 루프 안에 두면 tb2의 불일치는 tb1의 LLM 호출을 이미 쓴
            # 뒤에야 발견된다. 여기서 한 바퀴 먼저 돌면 어느 테스트벤치가
            # 어긋났든 호출은 하나도 쓰이지 않는다.
            #
            # try **안**이어야 한다: 밖으로 빼면 위에서 이미 진행된 회전이
            # finally에 도달하지 못해 커밋되지 않고, optimizer._run_simulation이
            # 이 예외를 삼키는 경로에서 상자가 같은 탐침 코너에 영원히 머문다.
            for tb in spec.testbenches:
                _check_deck_matches_state(tb.name, netlist_texts[tb.name], paths[tb.name])

            for tb in spec.testbenches:
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

                # **렌더링이 무엇을 했는지는 테스트벤치마다 한 번, 무조건
                # 적는다.** 전체 스윕(pvt.run_full_pvt_sweep)과 같은 이유이자
                # 같은 사건 이름이다: 요청한 재작성이 0건 매치인 것과 정상
                # 적용된 것이 history.jsonl에서 똑같이 보이면, 축이 죽은 채로
                # 도는 실행을 아무도 알아채지 못한다(startup 테스트벤치의
                # 전압 축이 정확히 그랬다). 상태는 코너가 아니라 덱의 성질이라
                # 대표 코너 하나로 충분하고, 그 렌더링 비용은 정규식 몇 개다.
                _log_corner_render(
                    tb, netlist_texts[tb.name], cs, benchmark_dir, log_event, nominal_corner
                )

                def _point_task(point, _tb=tb, _cb=control_block):
                    return _run_point(
                        sim_backend,
                        netlist_texts[_tb.name],
                        point,
                        _cb,
                        benchmark_dir,
                        paths[_tb.name],
                        _tb,
                        nominal_corner,
                    )

                # 탐침도 같은 배치에 넣는다 - 선택 코너와 마찬가지로 독립이고,
                # 따로 돌리면 테스트벤치마다 병렬 구간이 끝난 뒤 직렬 꼬리가
                # 하나씩 붙는다. 다만 **예외 처리는 갈라진다**: 선택 코너의
                # 예외는 판정 경로라 그대로 올려보내고, 탐침의 예외는 삼켜
                # 기록만 남긴다. 그래서 탐침 태스크만 자기 안에서 잡는다.
                probing_here = probe_point is not None and probe_error is None

                def _probe_task(point, _run=_point_task):
                    try:
                        return _run(point)
                    except Exception as exc:  # noqa: BLE001 - 탐침은 무엇으로도 실행을 멈추지 못한다
                        return exc

                # payload는 `(태스크 함수, 점)` - 선택 코너와 탐침이 서로 다른
                # 예외 정책을 갖기 때문에 함수를 payload에 실어 보낸다.
                items = [(("corner", i), (_point_task, point)) for i, point in enumerate(cs.corners)]
                if probing_here:
                    items.append((("probe", 0), (_probe_task, probe_point)))

                raws = map_points(lambda payload: payload[0](payload[1]), items, max_workers)

                # 완료 순서가 아니라 **선언 순서**로 읽는다. status는 "처음 만난
                # 비성공"이므로, 완료 순서로 읽으면 같은 스윕이 실행마다 다른
                # 코너를 사유로 적는다.
                for i, point in enumerate(cs.corners):
                    raw = raws[("corner", i)]
                    per_point[point].update(raw.measurements)
                    # 어느 점에서 난 실패인지를 status 문자열에 싣는다.
                    # optimizer._run_simulation은 이 값을 그대로 사유에 적으므로,
                    # 싣지 않으면 "convergence_failure"만 남고 45개 중 어느
                    # 코너였는지가 사라진다. status를 열거값과 **같은지** 보는
                    # 소비자는 없다 - 전부 `!= "success"`로만 읽는다.
                    if status == "success" and raw.status != "success":
                        status = f"{raw.status} at {label(point)} in testbench {tb.name}"

                if probing_here:
                    # 탐침은 판정에 참여하지 않으므로, 실패해도 이 반복을 멈출
                    # 근거가 없다. 그 반복의 탐침 **결과**를 없던 것으로 하고
                    # (판정도 승격도 없다) 기록만 남긴다. 남은 테스트벤치의
                    # 탐침도 건너뛴다 - 일부 테스트벤치의 측정값만으로 판정하면
                    # 나머지가 "측정값 없음"으로 실패해 크래시를 근거로 코너를
                    # 승격시킨다. 회전은 그대로 진행된 채 커밋되므로, 터진
                    # 코너는 다음 한 바퀴 뒤에 다시 온다.
                    raw = raws[("probe", 0)]
                    if isinstance(raw, BaseException):
                        probe_error = f"{type(raw).__name__}: {raw}"
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
