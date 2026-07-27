"""코너 축소와 재진입을 **진짜 회로**에 대고 종단으로 잰다.

이 하위 프로젝트의 태스크 1~7은 전부 대역(fake) 위에서만 검증됐다 - 유일한
예외가 `test_corner_sim_ngspice.py`(3초, 코너 두 점)다. 여기서 처음 붙는 것은
사슬 전체다: 진입 스윕 → 씨앗 → corner-aware simulate의 최악값 → 탐침 회전 →
설계가 움직인 뒤의 판정 스윕 → `grown_with`.

LLM 에이전트만 대역이다. corner_sim에서 에이전트의 기여는 control block 선택과
status 보고뿐이고 측정값은 전부 직접 경로에서 나오므로(build_corner_simulate의
docstring), 대역 에이전트가 측정하는 것을 바꾸지 않는다.

`ngspice`를 PATH에서 가정한다 - 이 저장소의 관례다(스킵 게이트 없음).

**실행 시간 129초(실측).** 대부분은 9코너 x 5테스트벤치 스윕 두 번(진입 ~57초,
움직인 덱의 판정 ~57초)이고 나머지 시뮬레이션은 dc_tc 하나로 줄여 놓았다.
"""
import os

import pytest

from analogcoder.cli import _argmax_drift
from analogcoder.corner_selection import NOMINAL, CornerSet, grown_with, label, seed_from_sweep
from analogcoder.corner_sim import CornerState, build_corner_simulate
from analogcoder.netlist import apply_changes, resolve_includes
from analogcoder.pvt import all_corners, run_full_pvt_sweep
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.state import RunState

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPEC_PATH = os.path.join(REPO, "benchmarks", "bandgap", "spec_corner_reduction.yaml")

# 메인 루프의 튜너가 낼 법한 크기의 변경을 대신한다. 두 증폭기의 꼬리 전류원을
# 8 -> 4로 좁히는 것으로, 실측상 buf1 루프를 임계값 아래로 밀어 **판정 스윕을
# 실패시킨다** - 재진입 경로에 필요한 입력이 바로 그것이다. 값은 이 파일이
# 단언하는 수치를 실제로 낸 것이고, 바꾸면 그 수치도 다시 재야 한다.
MOVED_DESIGN = [
    {"refdes": "TRIMAMP.Xt", "param": "W", "new_value": "4"},
    {"refdes": "BUF_P.Xt", "param": "W", "new_value": "4"},
]


def _spec():
    return load_spec(SPEC_PATH)


def _texts(spec) -> dict[str, str]:
    return {
        tb.name: resolve_includes(open(tb.netlist_path).read(), os.path.dirname(tb.netlist_path))
        for tb in spec.testbenches
    }


@pytest.fixture(scope="module")
def entry_sweep():
    """진입 스윕은 9코너 x 5테스트벤치라 ~57초다. 모듈에서 한 번만 돈다."""
    spec = _spec()
    return run_full_pvt_sweep(_texts(spec), spec, NgspiceBackend())


@pytest.fixture(scope="module")
def verdict_sweep():
    """움직인 덱의 판정 스윕. 역시 ~57초라 한 번만 돈다."""
    spec = _spec()
    moved = {name: apply_changes(text, MOVED_DESIGN) for name, text in _texts(spec).items()}
    return run_full_pvt_sweep(moved, spec, NgspiceBackend())


def _seeded(entry_sweep):
    spec = _spec()
    return spec, seed_from_sweep(entry_sweep, spec)


def test_the_mid_loop_sees_corners_and_the_set_is_smaller_than_the_full_sweep(entry_sweep):
    """축소가 실제로 일어났음을 씨앗 크기로 못박는다.

    **어떤 변형을 잡는가.** `seed_from_sweep`이 기준별 argmax의 합집합 대신
    격자 전체를 돌려주는 변형(예: `worst_case_corners` 대신
    `per_corner`를 순회하도록 바꾸는 것)을 잡는다. 그렇게 되면 씨앗이 9코너
    전부가 되어 아래 `<`가 등호가 되고, 이 하위 프로젝트는 아무것도 줄이지
    않은 것이 된다. 밖에 남는 코너가 있다는 단언은 탐침이 돌 수 있다는
    전제까지 함께 지킨다.
    """
    spec, cs = _seeded(entry_sweep)
    grid = all_corners(spec.pvt_corners)
    assert len(grid) == 9

    # NOMINAL은 코너가 아니라 덱 그대로이므로 항상 [0]이고, 격자와 비교할 때는
    # 1을 더해 센다.
    assert cs.corners[0] is NOMINAL
    assert 1 <= len(cs.corners) < 1 + len(grid)

    # 이 실행에서 실제로 나온 값들. 씨앗 6코너는 전부 ff/ss이고 tt 3개가 밖에
    # 남는다 - tt는 전형 코너라 어떤 기준의 최악도 아니다.
    assert len(cs.corners) == 7
    assert {label(c) for c in cs.corners[1:]} == {
        "ff/1.62/27.0", "ff/1.8/27.0", "ff/1.98/27.0",
        "ss/1.62/27.0", "ss/1.8/27.0", "ss/1.98/27.0",
    }
    assert {label(c) for c in cs.probe_order} == {
        "tt/1.62/27.0", "tt/1.8/27.0", "tt/1.98/27.0",
    }


@pytest.mark.asyncio
async def test_the_judge_sees_a_worse_value_than_nominal_alone(entry_sweep, tmp_path):
    """중간 루프가 코너를 본다는 것의 **관찰 가능한** 결과.

    corner-aware simulate가 배선되지 않으면(또는 최악값 대신 nominal 값을
    그대로 싣는 변형이면) judge는 덱 그대로의 값을 본다. 여기서는 같은 덱을
    두 CornerSet - 씨앗 전체와 NOMINAL 하나 - 으로 돌려 두 측정값을 나란히
    놓는다.

    **어떤 변형을 잡는가.** `build_corner_simulate`이 `worst_case_measurements`에
    `cs.corners` 대신 `[NOMINAL]`만 넘기거나, `_run_point`이 코너에서도
    `render_corner_netlist`를 건너뛰고 덱 그대로를 돌리는 변형. 둘 다 아래
    두 값을 같게 만든다.

    비용을 위해 테스트벤치는 canonical(dc_tc) 하나로 줄인다 - 씨앗은 위에서
    전체 스펙의 스윕으로 뽑은 것 그대로다.
    """
    spec, cs = _seeded(entry_sweep)
    spec.testbenches = spec.testbenches[:1]
    tb = spec.canonical
    text = _texts(spec)[tb.name]

    state = RunState(run_dir=str(tmp_path / "run"), testbench_names=[tb.name])
    state.push_netlist_version({tb.name: text})

    async def agent(netlist_path, control_block):
        return {"status": "success", "measurements": {}, "warnings": [],
                "control_block": control_block}

    async def measure(corner_set):
        simulate = build_corner_simulate(
            agent, NgspiceBackend(), state, CornerState(corner_set), lambda step, data: None
        )
        return await simulate(state.current_netlist_texts(), spec)

    # 탐침은 판정에 참여하지 않으므로 여기서는 끈다(probe_order를 비운다) -
    # 이 테스트가 재는 것은 선택 집합의 최악값 하나다.
    worst = await measure(CornerSet(corners=cs.corners, probe_order=()))
    nominal = await measure(CornerSet(corners=(NOMINAL,), probe_order=()))

    # 두 갈래 모두 실제로 측정을 냈는지 먼저 확인한다 - 값이 통째로 빠지는 것이
    # 이 경로에서 가장 흔한 실패 모양이다.
    assert worst["status"] == "success"
    assert nominal["status"] == "success"

    # quiescent_current는 "<=" 기준이므로 최악은 **최대**다. 실측: 덱 그대로
    # 219.9uA, 9코너 씨앗의 최악 231.2uA(ff/1.98).
    assert worst["measurements"]["iq_ua"] > nominal["measurements"]["iq_ua"]
    assert worst["corner_worst"]["quiescent_current"]["process"] == "ff"
    assert worst["corner_worst"]["quiescent_current"]["voltage"] == 1.98

    # 판정에 실제로 쓰이는 것은 씨앗 최악값이지 nominal이 아니다.
    assert nominal["corner_worst"]["quiescent_current"]["process"] == "(deck)"

    # **양면 창(window)은 measurements 하나에 담기지 않는다 - 이 실행이 실제로
    # 드러낸 사실이다.** vbgout_v에는 기준이 둘 붙어 있다(vbgout_min ">=",
    # vbgout_max "<="). worst_case_measurements는 기준별로
    # measurements[criterion.measurement]에 쓰므로 **뒤에 오는 기준이 이긴다** -
    # 여기서는 vbgout_max의 최댓값(ss/1.62의 1.24512)이 남고, vbgout_min이
    # 봐야 할 최솟값(ff/1.98의 1.233753)은 사라진다. 그래서 이 값은 덱 그대로의
    # 1.238874보다 **크다**. 즉 중간 루프의 판정자는 양면 창의 한쪽만 본다.
    #
    # 이것은 코너 축소가 아니라 **판정자 계약**의 문제다(judge는 측정값 이름으로
    # 키가 잡힌 dict 하나를 받는다). run_full_pvt_sweep은 같은 함정을 기준
    # 하나씩 따로 평가해서 피한다. 시스템 수준의 보증은 그대로다 - 중간 루프의
    # 낙관은 최종 판정 스윕이 잡고, 잡히면 재진입한다. 대가는 반복 하나다.
    # 조용히 두지 않기 위해 여기서 못박는다.
    assert worst["measurements"]["vbgout_v"] > nominal["measurements"]["vbgout_v"]
    assert worst["measurements"]["vbgout_v"] == worst["corner_worst"]["vbgout_max"]["value"]
    # 기준별 최악값 자체는 corner_worst에 **양쪽 다** 제대로 들어 있다.
    assert worst["corner_worst"]["vbgout_min"]["value"] < nominal["measurements"]["vbgout_v"]
    assert worst["corner_worst"]["vbgout_min"]["process"] == "ff"
    assert worst["corner_worst"]["vbgout_max"]["process"] == "ss"


@pytest.mark.asyncio
async def test_a_probe_run_records_its_corner_in_history(entry_sweep, tmp_path):
    """탐침이 조용히 꺼져 있으면 이 단언이 잡는다.

    **어떤 변형을 잡는가.** `build_corner_simulate`이 `next_probe`를 부르지
    않거나(탐침 자체가 사라짐), `_probe_enabled`가 스펙을 읽지 않고 항상
    False를 돌려주거나, 탐침 결과를 `log_event`로 남기지 않는 변형. 셋 다
    `corner_probe` 이벤트를 사라지게 한다.

    탐침이 **판정에 불참한다**는 것도 함께 못박는다: 탐침 코너의 측정값이
    corner_worst에 실리면 축소 집합이 항상 낙관적이라는 논증이 무너진다.
    """
    spec, cs = _seeded(entry_sweep)
    spec.testbenches = spec.testbenches[:1]
    tb = spec.canonical
    text = _texts(spec)[tb.name]

    state = RunState(run_dir=str(tmp_path / "run"), testbench_names=[tb.name])
    state.push_netlist_version({tb.name: text})

    async def agent(netlist_path, control_block):
        return {"status": "success", "measurements": {}, "warnings": [],
                "control_block": control_block}

    events: list = []
    corner_state = CornerState(cs)
    simulate = build_corner_simulate(
        agent, NgspiceBackend(), state, corner_state,
        lambda step, data: events.append((step, data)),
    )

    result = await simulate(state.current_netlist_texts(), spec)

    # 탐침은 severity 오름차순 첫 번째 - 이 실행에서는 tt/1.62/27이다.
    assert result["probe"]["corner"] == "tt/1.62/27.0"
    assert events == [("corner_probe", result["probe"])]

    # tt는 이 덱에서 모든 기준을 통과하므로 승격은 없다. 회전만 진행된다.
    assert result["probe"]["failed"] is False
    assert result["probe"]["promoted"] is False
    assert corner_state.corner_set.corners == cs.corners
    assert corner_state.corner_set.probe_index == 1

    # 탐침 코너는 판정에 불참한다: corner_worst의 어느 항목도 tt를 가리키지 않는다.
    assert "tt" not in {raw["process"] for raw in result["corner_worst"].values()}


def test_the_re_entry_path_does_not_fire_on_this_deck_and_the_reason_is_structural(
    entry_sweep, verdict_sweep
):
    """**재진입은 이 벤치마크에서 발화하지 않는다 - 그리고 그것이 산출물이다.**

    움직인 덱의 판정 스윕은 실제로 실패한다(buf1 루프). 그런데 `grown_with`는
    아무 코너도 더하지 못한다. 이유는 우연이 아니라 구조다:

      씨앗은 **모든** 기준의 argmax 합집합이다. 판정 스윕에서 실패한 기준의
      최악 코너는 정의상 그 기준의 argmax이므로, argmax가 씨앗 **밖으로**
      옮겨가지 않는 한 언제나 이미 집합 안에 있다. 이 격자에서 밖에 남는
      코너는 tt 3개뿐이고 tt는 전형 코너라 어떤 기준의 최악도 아니다.

    그래서 cli.py는 이 경우를 **경로 불일치**로 진단하고 재시도하지 않는다.
    그 진단이 공허하지 않은 것은, 두 경로가 같은 코너를 정말 다르게 잴 수 있는
    통로가 둘 있기 때문이다: 중간 루프는 에이전트가 수렴시킨 control block을
    쓰고 판정 스윕은 스펙 원문을 쓴다는 것, 그리고 중간 루프의 판정자는 LLM인데
    스윕은 결정론적 `evaluate_criteria`라는 것.

    **어떤 변형을 잡는가.** `grown_with`의 "이미 집합 안에 있으면 건너뛴다"
    검사를 없애는 변형. 그러면 이미 있는 코너가 다시 더해져 `added`가 비지
    않게 되고, cli.py는 진단 대신 무의미한 재진입을 돌며 - `CornerSet`의
    중복 검사에 걸려 ValueError로 끝난다.
    """
    spec, cs = _seeded(entry_sweep)

    assert verdict_sweep["overall_pass"] is False
    failing = [e["name"] for e in verdict_sweep["criteria"] if not e.get("pass")]
    assert failing == ["buf1_loop_gain", "buf1_phase_margin"]

    # 실패한 두 기준의 최악 코너는 이미 씨앗 안에 있다.
    in_set = {label(c) for c in cs.corners}
    for name in failing:
        raw = verdict_sweep["worst_case_corners"][name]
        assert f"{raw['process']}/{raw['voltage']}/{raw['temperature']}" in in_set

    grown, added = grown_with(cs, verdict_sweep, failing)
    assert added == []
    assert grown is cs


def test_the_argmax_moves_when_the_design_moves(entry_sweep, verdict_sweep):
    """설계가 움직일 때 최악 코너가 얼마나 움직이는가 - 아무도 재지 않았던 수치.

    거의 안 움직이면 코너 지속성이 좋아 어떤 적응형 기법도 고정 집합을 이기지
    못하고, 많이 움직이면 적응이 필요하다. 다음 축소 기법을 고르는 근거가 이
    숫자다.

    **실측: 22개 기준 중 5개가 움직였다.** 다섯 개 전부 씨앗 **안**에서
    안으로 움직였다 - 그래서 위 테스트의 재진입이 발화하지 않는다. 그리고
    다섯 중 셋은 process를 유지한 채 **voltage 축만** 옮겨갔다(ff/1.98 ->
    ff/1.62 등). 이 회로의 argmax 이동은 대체로 축 하나 안에서 일어난다.

    **어떤 변형을 잡는가.** `_argmax_drift`가 코너를 좌표 일부로만 비교하는
    변형 - 예를 들어 `_corner_label`이 voltage를 빼고 이름을 만들면. 위 실측대로
    이동의 3/5가 process 안에서 일어나므로 moved_count가 5에서 2로 줄고, 이
    단언이 잡는다. (한쪽 스윕에만 있는 기준을 moved로 세는 변형은 **여기서
    잡히지 않는다** - 두 스윕이 같은 스펙이라 22개가 정확히 겹쳐 짝 없는
    기준이 아예 없다. 그쪽은 tests/unit/test_cli.py가 대역으로 덮는다.)
    """
    spec, cs = _seeded(entry_sweep)
    drift = _argmax_drift(entry_sweep, verdict_sweep)

    assert drift["total"] == 22
    assert drift["moved_count"] == 5
    moved = {c["name"]: (c["entry"], c["final"]) for c in drift["criteria"] if c["moved"]}
    assert moved == {
        "vbg1_min": ("ff/1.98/27.0", "ff/1.62/27.0"),
        "vbg1_max": ("ss/1.62/27.0", "ss/1.98/27.0"),
        "buf1_loop_gain": ("ff/1.62/27.0", "ff/1.98/27.0"),
        "buf1_phase_margin": ("ss/1.8/27.0", "ff/1.98/27.0"),
        "buf0_loop_gain": ("ss/1.62/27.0", "ff/1.62/27.0"),
    }

    # 움직인 곳이 전부 씨앗 안이라는 사실이 재진입 미발화의 직접 원인이다.
    in_set = {label(c) for c in cs.corners}
    assert {dest for _, dest in moved.values()} <= in_set
