"""corner-aware simulate를 **진짜 회로**에 대고 한 번 돌린다.

이 하위 프로젝트의 앞선 태스크는 전부 대역(fake) 위에서만 검증됐다 -
`_FakeBackend`는 넷리스트 텍스트를 기록만 하고 ngspice를 부르지 않는다. 여기서
실제로 붙는 것은 세 가지다: RunState가 실행 디렉터리에 스테이징한 덱을 NOMINAL이
**파일 그대로** 시뮬레이션한다는 것, 코너가 그 덱을 렌더링해 진짜 PDK 코너
include를 읽는다는 것, 그리고 두 갈래의 측정값 키 집합이 실제로 같아서
worst_case_measurements가 기준별 최악 코너를 고를 수 있다는 것.

LLM 에이전트만 대역이다 - 그쪽 기여는 control block 선택과 status 보고뿐이고,
측정값은 전부 직접 경로에서 나온다(build_corner_simulate의 docstring).

비용을 위해 canonical 테스트벤치 하나와 코너 두 점으로 줄였다. 45 코너 전체는
tests/unit/test_pvt_sweep_ngspice.py 쪽이 이미 치른다.
"""
import os

import pytest

from analogcoder.corner_selection import NOMINAL, CornerSet
from analogcoder.corner_sim import CornerState, build_corner_simulate
from analogcoder.netlist import resolve_includes
from analogcoder.pvt import CornerPoint
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.state import RunState

SPEC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "benchmarks", "bandgap", "spec_pvt.yaml",
)

FS = CornerPoint(process="fs", voltage=1.98, temperature=125.0)
SS = CornerPoint(process="ss", voltage=1.62, temperature=-40.0)


@pytest.mark.asyncio
async def test_the_corner_aware_simulate_measures_a_real_deck_at_real_corners(tmp_path):
    spec = load_spec(SPEC_PATH)
    # canonical 하나만 남긴다. all_criteria는 testbenches 위의 property이므로
    # 기준도 함께 줄어든다 - 다른 테스트벤치의 기준이 "측정값 없음"으로 남지 않는다.
    spec.testbenches = spec.testbenches[:1]
    tb = spec.canonical

    with open(tb.netlist_path) as f:
        text = resolve_includes(f.read(), os.path.dirname(tb.netlist_path))

    state = RunState(run_dir=str(tmp_path / "run"), testbench_names=[tb.name])
    state.push_netlist_version({tb.name: text})

    async def agent(netlist_path, control_block):
        return {"status": "success", "measurements": {}, "warnings": [],
                "control_block": control_block}

    corner_state = CornerState(CornerSet(corners=(NOMINAL, FS, SS), probe_order=()))
    events: list = []
    simulate = build_corner_simulate(
        agent, NgspiceBackend(), state, corner_state,
        lambda step, data: events.append((step, data)),
    )

    result = await simulate(state.current_netlist_texts(), spec)

    assert result["status"] == "success"
    # 벤치마크가 실제로 내는 값들. 대충 큰 범위로 두되 **없으면** 실패한다 -
    # 값이 빠지는 것이 이 경로에서 가장 흔한 실패 모양이기 때문이다.
    assert 1.15 < result["measurements"]["vbg1_v"] < 1.25
    assert 0.45 < result["measurements"]["vbg0_v"] < 0.55

    # 세 점이 서로 다른 회로 상태이므로 최악 코너가 한 점에 몰릴 수 없다.
    # 렌더링을 건너뛰고 세 점 모두 덱 그대로를 돌리는 변형(이 하위 프로젝트
    # 전체를 무의미하게 만드는 변형)은 여기서 모든 기준이 (deck)을 가리키게
    # 만든다 - 그때 이 단언이 걸린다.
    picked = {
        (raw["process"], raw["voltage"], raw["temperature"])
        for raw in result["corner_worst"].values()
    }
    assert ("fs", 1.98, 125.0) in picked
    assert ("ss", 1.62, -40.0) in picked

    # 집합 밖이 비었으므로 탐침은 없다.
    assert result["probe"] is None
    # 사건 이름을 지목해서 본다. 예전에는 `events == []`이었는데, 그것은 "탐침
    # 사건이 없다"가 아니라 "이 경로가 아무것도 기록하지 않는다"를 고정하는
    # 단언이라, 렌더링 상태 기록처럼 **기록이 늘어나는** 방향의 변경까지 함께
    # 막았다. 이 저장소가 반복해서 값을 치른 것은 기록이 없는 쪽이다.
    assert [name for name, _ in events if name == "corner_probe"] == []
    assert [name for name, _ in events] == ["corner_render"]
