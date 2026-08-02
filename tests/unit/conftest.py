"""tests/unit 전역 conftest.

면적 최소화 단계(`analogcoder.optimizer.run_area_optimization`)는 스펙 선언 없이
`cli._run`의 PASS 분기마다 **무조건** 돈다(`c786554`) - 선언이 없어도 도는 것이
이 단계의 존재 이유다. 그런데 `cli._run`을 통째로 도는 단위 테스트 대부분은 그
단계 자체가 아니라 다른 배선(코너 축소, 재개, 스윕 순서·병합 등)을 재는
것이라, 진짜 함수가 돌면 매번 실제 노브 랭킹 계산과 (노브가 있으면) 실제
시뮬레이션 에이전트 호출을 치른다 - 무관한 단언이 깨지는 것은 덤이고 테스트
하나가 수십 초씩 걸린다.

`_disable_area_optimization`이 그 실행을 무력화하는 **유일한** 자리다(전용
헬퍼 하나 - 20곳에 흩어 패치하지 않는다). 배선 자체(면적 단계가 PASS에서
`run_optimization`보다 **먼저** 도는가, 그 `final_criteria`가
`run_optimization`이 아무것도 못 쟀을 때 최상위로 오르는가)를 재는 테스트는 이
대역을 쓰지 않고 `analogcoder.cli.run_area_optimization`을 자기 것으로 다시
덮는다 - `tests/unit/test_cli.py`의
`test_the_area_phase_runs_on_pass_before_run_optimization_and_its_final_criteria_lands_in_the_result`
참고. `unittest.mock.patch`는 중첩되므로 그렇게 덮어도 이 fixture가 만든
대역을 정상적으로 가리고, 테스트가 끝나면 다시 이 fixture의 대역으로,
그리고 이 fixture가 끝나면 진짜 함수로 복원된다.

`analogcoder.cli._run`을 부르지 않는 테스트(예: `analogcoder.optimizer`를
직접 재는 `test_optimizer_area_phase.py`)에는 이 patch가 아무 효과가
없다 - 대상이 `analogcoder.cli` 모듈이 자기 이름공간에 들여온 참조이고,
그 경로에서만 읽힌다."""

from unittest.mock import AsyncMock, patch

import pytest


def area_optimization_noop_result() -> dict:
    """`run_area_optimization`이 아무것도 바꾸지 못했을 때의 모양을 흉내낸다.

    `final_criteria`가 None이므로 `cli._run`의 상향 대입
    (`if result["area_optimization"].get("final_criteria"): ...`)이 조용히
    아무 일도 하지 않는다 - 이 대역을 쓰는 기존 테스트들의 단언(진입/판정
    스윕 횟수, `result["final_criteria"]` 등)을 이 대역이 건드리지 않는
    이유다. `final_netlist_paths`가 빈 dict인 것도 같은 이유 - 이 대역은
    넷리스트 버전을 밀지 않는다."""
    return {
        "status": "UNCHANGED",
        "objective_before": None,
        "objective_after": None,
        "area_before": 0.0,
        "area_after": 0.0,
        "steps_accepted": 0,
        "steps_rejected": 0,
        "rejected_by_reason": {},
        "corner_confirmed": False,
        "pvt_sweep": None,
        "corner_failure": None,
        "final_criteria": None,
        "final_netlist_paths": {},
    }


@pytest.fixture(autouse=True)
def _disable_area_optimization():
    """`analogcoder.cli.run_area_optimization`을 무해한 대역으로 바꾼다.

    선언 없이 무조건 도는 것이 이 단계의 계약이므로, `cli._run`을 도는
    테스트라면 그 테스트의 주제가 무엇이든 실제 함수가 실행된다 - 이
    fixture가 없으면 20개 넘는 테스트가 매번 실제 시뮬레이션을 치른다."""
    with patch(
        "analogcoder.cli.run_area_optimization",
        new=AsyncMock(return_value=area_optimization_noop_result()),
    ):
        yield
