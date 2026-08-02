#!/usr/bin/env python3
"""탐색기 A/B 하니스 - 두 탐색 전략을 **LLM 없이** 같은 조건에서 돌린다.

로드맵 단계 3(신뢰영역 DFO)과 단계 4(제약 베이지안 최적화)는 둘 다 "현행보다
나은가"로 판정된다. 그 판정이 성립하려면 두 가지가 필요하다: 현행을 떼어낼 수
있는 이음매(optimizer.py의 SearchOracle / 전략 / accept_step)와, 두 탐색기를
**같은 것 위에서** 돌릴 수 있는 러너. 이 파일이 두 번째다.

**왜 LLM을 빼는가.** SPICE는 결정론적이다. 실행에서 LLM을 전부 빼면 실행
자체가 결정론적이 되고, 두 전략의 차이를 표본 몇 개로 - 원리적으로는 하나로 -
판정할 수 있다. 남겨 두면 같은 입력에서 다른 노브 순위가 나오고(이 저장소는
한 넷리스트에서 93/26/1개의 역할을 받은 전례가 있다), 그 분산이 탐색기 차이보다
커진다. D1이 정확히 그 자리에서 결론을 못 냈다.

빼는 방법은 두 개다:
  - **노브 순위**: `OptimizerAgents.knob_ranking`으로 고정 주입한다. 그러면
    `agents.propose`는 한 번도 불리지 않는다.
  - **시뮬레이션**: cli.py의 simulate_fn은 시뮬레이터 **에이전트**를 거치지만
    여기서는 `NgspiceBackend`를 직접 부른다. 그래서 이 하니스가 쓰는 제어
    블록은 스펙에 적힌 원문이고, 실제 실행에서 에이전트가 수렴시킨 것이
    아니다 - 두 전략에 **똑같이** 적용되므로 비교는 통제되지만, 절대값이
    실제 실행과 다를 수 있다는 뜻이다. 이 하니스의 산출물은 절대값이 아니라
    **두 전략의 차이**다.

**시뮬레이션 캐시(`CachingSimulator`)를 일부러 붙이지 않는다.** cli.py는
붙이고, 그것이 실행에는 옳다 - 같은 점을 두 번 재지 않는다. 그러나 이 하니스가
재는 것 중 하나가 **비용**이고, 캐시가 붙으면 "이 전략이 몇 번 쟀는가"가
"앞 전략이 이미 재 둔 점을 몇 개 겹쳤는가"로 바뀐다. 두 전략이 같은 후보를
많이 공유할수록 뒤에 도는 쪽이 공짜로 유리해지는데, 그것은 전략의 성질이
아니라 실행 순서의 성질이다. 캐시를 붙이려면 hit/miss를 별도 칸으로 내고
동률 판정에서 miss만 세도록 고쳐야 한다 - 그 전에는 붙이지 말 것.

벽시계는 참고값이다. `run_full_pvt_sweep`이 코너를 스레드로 병렬화하므로
절대값이 워커 수에 달려 있다 - 같은 실행 안의 두 쪽끼리는 비교되지만, 다른
기계·다른 `ANALOGCODER_SIM_WORKERS`의 숫자와는 비교되지 않는다. 판정에 쓰는
비용 지표는 시뮬레이션 횟수다.

**1차 지표는 코너 확인을 통과한 목적값이다.** nominal에서 더 내려가는 것은
현행 좌표 하강이 이미 잘하고, 그게 문제였다 - 실측으로 nominal 10단계를
수락하고 확인 스윕에서 6개 기준이 깨져 4단계만 살아남았다. 표에서 nominal
값도 같이 보여주는 것은 그 격차 자체가 읽을 거리이기 때문이다.

**같은 전략을 양쪽에 넣으면 두 기록이 완전히 같아야 한다.** 그것이 이 하니스의
자기 검사이고, `--assert-identical`(같은 전략이면 기본으로 켜진다)이 그것을
검사한다. 다르면 통제되지 않은 무언가가 남아 있다는 뜻이고, 그 상태에서 잰
A/B는 아무것도 증명하지 못한다.

사용 예:

    # 자기 비교 - 하니스 자신의 정확성 검사
    .venv/bin/python scripts/search_ab.py \\
        --spec benchmarks/bandgap/spec.yaml \\
        --knob TRIMAMP.Xt:W:decrease \\
        --strategy coordinate_descent --strategy coordinate_descent

    # 단계 3이 붙은 뒤의 실제 A/B (코너 확인이 도는 스펙이어야 한다)
    .venv/bin/python scripts/search_ab.py \\
        --spec benchmarks/bandgap/spec_pvt.yaml \\
        --knob TRIMAMP.Xt:W:decrease \\
        --strategy coordinate_descent --strategy mads --max-steps 20
"""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import analogcoder.optimizer as optimizer_module  # noqa: E402
from analogcoder.netlist import resolve_includes  # noqa: E402
from analogcoder.optimizer import (  # noqa: E402
    SEARCH_STRATEGIES,
    OptimizerAgents,
    run_area_optimization,
    run_optimization,
)
from analogcoder.pvt import all_corners, run_full_pvt_sweep  # noqa: E402
from analogcoder.simulators.ngspice import NgspiceBackend  # noqa: E402
from analogcoder.spec import load_spec  # noqa: E402
from analogcoder.state import RunState  # noqa: E402


def parse_knob(text: str) -> dict:
    """`REFDES:PARAM:DIRECTION` -> 후보 하나.

    값을 실을 자리가 없는 것이 의도다. OPTIMIZER_SCHEMA가 에이전트에게 값을
    금하는 것과 같은 이유로, 고정 순위에도 값을 넣지 않는다 - 얼마나 움직일지는
    **전략이** 정하고, 그것이 바로 비교 대상이다."""
    parts = text.split(":")
    if len(parts) != 3 or not all(parts):
        raise argparse.ArgumentTypeError(
            f"knob must be REFDES:PARAM:DIRECTION (got {text!r})"
        )
    refdes, param, direction = parts
    if direction not in ("increase", "decrease"):
        raise argparse.ArgumentTypeError(
            f"direction must be 'increase' or 'decrease' (got {direction!r})"
        )
    return {
        "refdes": refdes,
        "param": param,
        "direction": direction,
        # 스키마의 required와 같은 모양을 유지한다 - 이 dict는 이력에 그대로
        # 실리고, 나중에 에이전트가 낸 순위와 나란히 읽히게 된다.
        "reasoning": "fixed ranking supplied to scripts/search_ab.py",
    }


def parse_corner_regime(text: str):
    """`argmax` 또는 `coverage:<epsilon>:<tau>`.

    **읽을 수 없으면 거부한다.** 조용히 argmax 로 떨어지면 두 쪽이 같은
    체제로 돌면서 기록에는 다른 이름이 실리고, 격자의 셀 하나가 통째로
    거짓이 된다."""
    from analogcoder.spec import CoverageConfig

    if text == "argmax":
        return None
    parts = text.split(":")
    if len(parts) != 3 or parts[0] != "coverage":
        raise ValueError(
            f"unreadable corner regime {text!r}: use 'argmax' or 'coverage:<eps>:<tau>'"
        )
    try:
        epsilon, tau = float(parts[1]), float(parts[2])
    except ValueError:
        raise ValueError(
            f"unreadable corner regime {text!r}: epsilon and tau must be numbers"
        ) from None
    return CoverageConfig(epsilon=epsilon, tau=tau)


def load_deck(spec):
    """스펙이 선언한 모든 테스트벤치의 넷리스트 원문.

    cli.py와 같은 지점에서 include를 절대경로화한다. RunState가 덱을 실행
    디렉터리로 옮기고 NgspiceBackend가 다시 임시 디렉터리로 옮기므로,
    `.include "pdk_corner.inc"`는 텍스트가 움직이는 순간 해소되지 않는다."""
    texts = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            texts[tb.name] = resolve_includes(f.read(), os.path.dirname(tb.netlist_path))
    return texts


class Meter:
    """이 실행이 실제로 쓴 시뮬레이션. 두 전략을 같은 예산에서 비교하려면
    "몇 번 쟀는가"가 기록에 있어야 한다 - 동률일 때 채택 규칙이 읽는 것이
    이 숫자다.

    두 층을 따로 센다. `nominal_calls`는 탐색이 후보 하나를 재려고 부른
    횟수이고, `spice_runs`는 그 아래에서 실제로 돈 ngspice 프로세스 수다 -
    테스트벤치가 5개면 한 번의 호출이 5회다. 코너 스윕은 corners x testbenches
    이므로 한 번이 수백 회가 된다. 한 숫자로 뭉치면 "탐색이 몇 번 물어봤나"와
    "그 값이 얼마였나"가 구별되지 않는다."""

    def __init__(self) -> None:
        self.nominal_calls = 0
        self.sweep_calls = 0
        self.spice_runs = 0

    def as_dict(self) -> dict:
        return {
            "nominal_calls": self.nominal_calls,
            "sweep_calls": self.sweep_calls,
            "spice_runs": self.spice_runs,
        }


def build_agents(spec, strategy_name: str, ranking: list[dict], meter: Meter, timeout: int):
    """이 실행의 OptimizerAgents. propose는 **부르면 터지는** 자리 표시자다.

    None을 넣지 않는 이유는 가시성이다: None이면 AttributeError가 나면서
    "왜 None인가"를 아무도 설명하지 못한다. 여기서는 예외 문구 자체가
    "LLM이 없어야 하는데 불렸다"고 말한다 - 이 저장소가 아홉 번 당한 조용한
    무력화를 이 자리에서 반복하지 않는 방법이다."""
    backend = NgspiceBackend(timeout=timeout)

    async def forbidden_propose(*args, **kwargs):
        raise AssertionError(
            "the A/B harness must contain no LLM call, but the optimizer asked the "
            "ranking agent for candidates - the injected knob_ranking did not take"
        )

    async def simulate(netlist_texts, spec_arg):
        merged = {}
        meter.nominal_calls += 1
        with tempfile.TemporaryDirectory() as tmpdir:
            for tb in spec_arg.testbenches:
                path = os.path.join(tmpdir, f"{tb.name}.cir")
                with open(path, "w") as f:
                    f.write(netlist_texts[tb.name])
                meter.spice_runs += 1
                merged.update(
                    backend.run(path, {"control_block": tb.control_block}).measurements
                )
        return {"measurements": merged}

    def verify_corners(netlist_texts):
        # **동기**여야 한다. run_optimization은 await 없이 직접 부르므로 async로
        # 감싸면 코루틴 객체가 "쓸 수 없는 결과"로 접혀 최적화가 크래시도 로그도
        # 없이 통째로 UNCHANGED가 된다.
        meter.sweep_calls += 1
        # 스윕 하나는 corners x testbenches 회다. 그 곱이 이 단계의 비용 전부라
        # 호출 수만 세면 "45코너 5테스트벤치 한 번"과 "9코너 한 번"이 같은
        # 숫자가 된다.
        meter.spice_runs += len(all_corners(spec.pvt_corners)) * len(spec.testbenches)
        return run_full_pvt_sweep(netlist_texts, spec, backend)

    return OptimizerAgents(
        propose=forbidden_propose,
        simulate=simulate,
        verify_corners=verify_corners if spec.pvt_corners is not None else None,
        knob_ranking=list(ranking),
        search_strategy=SEARCH_STRATEGIES[strategy_name],
    )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _count_compound_accepted(steps: list[dict]) -> int:
    """조합 스텝(노브 2개 이상을 한 후보로 묶은 시도)이 실제로 **수락**된 수.

    `changes`가 실린 스텝(`_steps_from_history` 참고)만 조합 시도이고, 그중
    `accepted`가 참인 것만 센다 - 단일 노브 수락(`changes` 없음)과 거절된
    조합(`accepted=False`)을 섞지 않는다. 사전 등록의 선행 조건("조합 스텝이
    한 번도 수락되지 않으면 그 슬롯은 void")이 이 값을 읽으므로, 거절된
    조합을 세어 넣으면 실제로 발화하지 않은 조건을 발화한 것처럼 판정하게
    된다."""
    return sum(1 for s in steps if s["accepted"] and s.get("changes"))


def _corner_capability(spec, agents) -> dict:
    """`corner_capable`이 참인지 결정하는 두 사실을 따로 잰다.

    `optimizer.py:1757`의 정의(`spec.pvt_corners is not None and
    agents.verify_corners is not None`)를 **재유도하지 않고 그대로 읽는다** -
    이 저장소의 규칙이다(규칙을 소유한 곳에서 가져온다). 이 하니스의
    `build_agents`는 두 조건을 하나로 묶어 놓았지만
    (`verify_corners=... if spec.pvt_corners is not None else None`), 그
    묶임을 지키는 것은 코드가 아니라 이 파일을 읽는 사람의 규율이다 - 그래서
    두 사실을 따로 재 두면, 나중에 이 하니스가 바뀌어 둘이 갈라지더라도
    "무엇이 없어서 명목 전용이 됐는가"를 여전히 가를 수 있다."""
    pvt_corners_declared = spec.pvt_corners is not None
    verify_corners_wired = agents.verify_corners is not None
    return {
        "pvt_corners_declared": pvt_corners_declared,
        "verify_corners_wired": verify_corners_wired,
        "corner_capable": pvt_corners_declared and verify_corners_wired,
    }


def _eligibility_verdict(
    corner_capability: dict,
    control_side,
    control_steps_accepted,
    control_corner_confirmed,
) -> dict:
    """적격성 게이트의 순수 판정 - 시뮬레이션·파일 IO에서 분리해 두어 이
    판정만 ngspice 없이 단위 시험할 수 있게 한다.

    네 결과(`nominal_only`/`not_applicable`/`void`/`eligible`) 모두 같은 키
    모양을 쓴다. "게이트가 통과했다"와 "게이트가 없다"와 "이 실행은 애초에
    잴 자격이 없다"가 `history`/`comparison.json`만으로 구별되려면 세 가지가
    같은 모양으로, 항상 남아야 한다.

    **`nominal_only`가 가장 먼저 판정된다.** F1 리뷰: `corner_capable`이
    거짓이면 `steps_accepted`는 확인 스윕 생존 수가 아니라 `_search`가 낸
    원시 명목 수락 수다(`optimizer.py` 1911-1915 vs 1937) -
    `steps_survived > 0`만으로는 이 둘을 구별할 수 없으므로, 대조군을
    돌리기도 전에 이 전제부터 거부한다. "대조군이 스텝을 0개 수락했다"
    (`void`, 조건이 발생하지 않았다)와는 다른 사실이다 - 이 실행은 애초에
    코너 인식 수락으로 돌지 않는다."""
    verdict = {
        "checked": False,
        "control_strategy": None,
        "control_side": control_side,
        "control_steps_accepted": control_steps_accepted,
        "pvt_corners_declared": corner_capability["pvt_corners_declared"],
        "verify_corners_wired": corner_capability["verify_corners_wired"],
        "corner_confirmed": control_corner_confirmed,
        "verdict": None,
        "reason": None,
    }
    if not corner_capability["corner_capable"]:
        if not corner_capability["pvt_corners_declared"]:
            missing = "spec.pvt_corners is None"
        else:
            missing = "agents.verify_corners is None even though spec.pvt_corners is declared"
        verdict["verdict"] = "nominal_only"
        verdict["reason"] = (
            f"{missing} - acceptance would be judged at a single nominal point, not "
            "a corner-confirmed sweep; revision 1 of the design doc forbids "
            "measuring search-strategy differences under that rule"
        )
        return verdict
    if control_side is None:
        verdict["verdict"] = "not_applicable"
        verdict["reason"] = (
            "neither --strategy is literally 'coordinate_descent'; there is no "
            "designated control to establish a baseline against, so the eligibility "
            "gate does not apply to this pair"
        )
        return verdict
    verdict["checked"] = True
    verdict["control_strategy"] = "coordinate_descent"
    if control_steps_accepted == 0:
        verdict["verdict"] = "void"
        verdict["reason"] = (
            "control (coordinate_descent) accepted 0 steps on this slot; "
            "there is no baseline to compare against"
        )
    else:
        verdict["verdict"] = "eligible"
    return verdict


def _steps_from_history(state, step_event: str) -> list[dict]:
    """이력의 `step_event`(단계에 따라 `optimize_step` 또는 `optimize_area_step`)를
    비교 가능한 모양으로 줄인다.

    기록에 이것을 넣는 이유는 자기 검사 때문이다. 같은 전략을 양쪽에 넣었을 때
    최종 목적값만 같은 것으로는 부족하다 - 서로 다른 경로로 같은 값에 도달할 수
    있고, 그러면 통제되지 않은 무언가가 남아 있는데도 검사가 통과한다.

    **`step_event`는 호출자가 준다, 여기서 추측하지 않는다.** `SearchRun.log_event`가
    `f"{phase.label}_{suffix}"`로 스스로 라벨을 붙이므로(목적 단계는 `optimize_`,
    면적 단계는 `optimize_area_`), 상수 `"optimize_step"` 하나로 두 단계를 같이
    읽으면 면적 단계를 돌린 기록에서 스텝이 하나도 안 잡히고 "조합이 한 번도
    수락되지 않았다"와 "이 필터가 이름을 놓쳤다"가 똑같이 빈 목록으로 보인다."""
    steps = []
    with open(state.history_path) as f:
        for line in f:
            event = json.loads(line)
            if event["step"] != step_event:
                continue
            steps.append(
                {
                    "refdes": event.get("refdes"),
                    "param": event.get("param"),
                    "direction": event.get("direction"),
                    "before": event.get("before"),
                    "after": event.get("after"),
                    "objective": event.get("objective"),
                    "accepted": event.get("accepted"),
                    "gate": event.get("gate"),
                    "reason": event.get("reason"),
                    # 조합 스텝(노브 2개 이상을 한 후보로 묶은 시도)에만 `changes`가
                    # 실린다(optimizer.py:1085-1091, `len(steps) > 1`). 없으면
                    # 단일 노브 스텝이라는 뜻이라 `None`을 그대로 둔다 - `[]`로
                    # 접으면 "조합을 시도했는데 변경이 0개"와 "애초에 단일
                    # 노브였다"가 구별되지 않는다.
                    "changes": event.get("changes"),
                }
            )
    return steps


def run_side(
    side: str,
    strategy_name: str,
    args,
    ranking: list[dict],
    out_dir: str,
    corner_regime=None,
    phase: str = "objective",
) -> dict:
    """한쪽을 돌리고 기록을 만든다.

    `phase="area"`는 `run_area_optimization`을 부른다 - `optimize:` 선언이
    필요 없고 노브 순위를 `rank_by_area_gain`으로 스스로 계산하므로, 여기서
    넘기는 `ranking`(고정 순위)은 목적 단계에서만 쓰이고 면적 단계에서는
    `run_area_optimization`이 만드는 새 `OptimizerAgents`로 통째로 대체된다
    (`optimizer.py:2109-2118`) - `agents.propose`/`simulate`/`verify_corners`/
    `search_strategy`만 그대로 이어받는다. **그래서 `phase="area"`에는
    `ranking`을 비워서 줘야 한다** - 아래 가드가 그것을 강제한다.

    `record`와 `meta`를 가르는 것이 중요하다. record는 **결정론적이어야 하는**
    모든 것이고, meta는 그렇지 않은 것(벽시계, 경로)이다. 자기 검사는 record만
    비교한다 - meta를 섞으면 검사가 언제나 실패해 아무 말도 하지 않게 된다."""
    if phase == "area" and ranking:
        # F2 리뷰: `main()`의 인자 검증(`--phase area`와 `--knob`을 함께 주면
        # 거부)은 이 스크립트를 CLI로 부를 때만 지난다 - 이 모듈을 직접
        # import해서 `run_side`를 부르면 그 검증을 건너뛴다. 조용히 받으면
        # `record["knob_ranking"]`이 `run_area_optimization`이 실제로는 아예
        # 읽지 않는 값을 그대로 실어("결과는 자기가 반환한 것을 기술해야
        # 한다"는 규칙 위반) 실제로 쓰인 순위(`history.jsonl`의
        # `optimize_area_ranking` 이벤트)와 달라 보이게 된다. `corner_regime`
        # 가드와 같은 이유로 여기서도 다시 거부한다.
        raise ValueError(
            f"run_side(phase='area', ranking={ranking!r})에 노브 순위를 주면 "
            "안 된다: run_area_optimization은 rank_by_area_gain으로 순위를 "
            "스스로 계산해 OptimizerAgents를 통째로 새로 만들고 "
            "(optimizer.py:2109-2118) 여기서 받은 ranking은 그 안에서 아예 "
            "읽히지 않는다."
        )
    if corner_regime is not None:
        # **거부를 `main()`에만 두지 않는다.** `main()`의 인자 검증은 이
        # 스크립트를 CLI로 부를 때만 지난다 - 이 모듈을 직접 import해서
        # `run_side`를 부르면(예: 다른 스크립트, 노트북, 미래의 하니스) 그
        # 검증을 완전히 건너뛴다. `run_side`는 이 가드를 지난 뒤로는
        # `corner_regime`을 **어디에도** 쓰지 않는다(M9, T19 리뷰 - record의
        # `corner_regime` 라벨도 상수 `"argmax"`로 고정돼 있다. 아래를 보라) -
        # 조용히 통과시키면 두 쪽이 실제로는 같은(argmax) 코너로 돌면서
        # 기록에는 서로 다른 체제 이름이 실려 격자의 셀 하나가 통째로 거짓이
        # 된다 - 이 저장소가 세는 조용히 무력한 게이트 #11이 정확히 이 모양이었다.
        raise ValueError(
            f"run_side()가 argmax가 아닌 코너 체제({corner_regime!r})를 받았다: "
            "이 하니스는 run_optimization을 직접 부르고 corner_reduction의 "
            "중간-루프 코너 축소(corner_sim.build_corner_simulate)를 거치지 "
            "않으므로 coverage 체제를 실제로 적용할 수 없다 - corner_regime은 "
            "이 지점을 지나면 어디에도 쓰이지 않고 회로가 도는 방식은 전혀 "
            "바뀌지 않는다. run_side가 cli.py처럼 "
            "corner_selection.seed_from_sweep / "
            "corner_sim.build_corner_simulate 를 거치도록 고쳐야 이 값을 "
            "받을 수 있다."
        )
    spec = load_spec(args.spec)
    # **coverage로 spec.corner_reduction을 고쳐 쓰지 않는다.** 위 가드와
    # `main()`의 인자 검증이 함께 coverage 체제를 시작 전에 거부하므로
    # `corner_regime`은 이 지점에서 항상 None이다 - 그 스펙 변형
    # (dataclasses.replace)이 여기 남아 있으면 `run_optimization`이 애초에
    # 읽지도 않는 필드를 조용히 고쳐 놓고, record의 `corner_regime` 문자열만
    # 다른 이름을 다는 것을 다시 열어 두는 셈이 된다.
    texts = load_deck(spec)
    run_dir = os.path.join(out_dir, f"{side}_{strategy_name}")
    os.makedirs(run_dir, exist_ok=True)
    state = RunState(run_dir=run_dir, testbench_names=list(texts))
    state.push_netlist_version(texts)

    meter = Meter()
    agents = build_agents(spec, strategy_name, ranking, meter, args.sim_timeout)

    # 예산은 모듈 전역이고, 두 쪽에 **같은** 값이 가야 한다. 여기서 한 번만
    # 대입하고 그 값을 기록에 싣는다 - 예산이 다르면 비교 자체가 성립하지
    # 않는데, 그 사실이 기록에 없으면 나중에 아무도 확인할 수 없다.
    previous_budget = optimizer_module.MAX_OPTIMIZE_STEPS
    optimizer_module.MAX_OPTIMIZE_STEPS = args.max_steps
    started = time.monotonic()
    try:
        if phase == "area":
            result = asyncio.run(run_area_optimization(texts, spec, state, agents))
        else:
            result = asyncio.run(run_optimization(texts, spec, state, agents))
    finally:
        optimizer_module.MAX_OPTIMIZE_STEPS = previous_budget
    elapsed = time.monotonic() - started

    # `SearchRun.log_event`가 `f"{phase.label}_{suffix}"`로 스스로 라벨을
    # 붙인다(optimizer.py:1023) - 목적 단계는 `optimize_step`, 면적 단계는
    # `optimize_area_step`. 상수 하나로 두 단계를 같이 읽으면 면적 단계
    # 기록에서 스텝이 통째로 안 잡힌다.
    step_event = "optimize_area_step" if phase == "area" else "optimize_step"
    steps = _steps_from_history(state, step_event)
    accepted_nominal = sum(1 for s in steps if s["accepted"])
    nominal_objectives = [s["objective"] for s in steps if s["accepted"]]
    # 사전 등록의 선행 조건: 조합 스텝(노브 2개 이상)이 이 런에서 실제로 한 번
    # 이라도 **수락**됐는가. 0이어도 키를 쓴다 - "조합이 한 번도 안 됐다"와
    # "이 계측이 사라졌다"가 history.jsonl 만으로는 구별되지 않으면 안 된다.
    compound_steps_accepted = _count_compound_accepted(steps)
    final_texts = state.current_netlist_texts()

    record = {
        "side": side,
        "strategy": strategy_name,
        "phase": phase,
        # M9(T19): 이 지점에서 `corner_regime`은 **항상 None**이다 - 위 가드가
        # 그 외의 모든 값을 함수 진입 시점에 거부하고 함수를 빠져나가므로,
        # `else` 분기는 도달 불가능한 코드였다. 죽은 분기가 "이 하니스가
        # coverage 체제를 기록할 수 있다"고 광고하는 것 자체가 #11과 같은
        # 종류의 거짓이라, 상수로 정리한다 - 반영하려면 위 가드부터 고쳐야
        # 한다(가드가 지나갈 때만 `corner_regime`이 None 아닌 값일 수 있다).
        "corner_regime": "argmax",
        "spec": os.path.relpath(args.spec, os.getcwd()),
        "knob_ranking": ranking,
        "step_budget": args.max_steps,
        "status": result["status"],
        "objective_name": spec.optimize.objective if spec.optimize else None,
        "objective_before": result["objective_before"],
        # nominal 탐색이 도달한 값 - 마지막으로 **수락된** 단계의 목적값이다.
        # 확인 스윕이 되돌리기 전의 값이므로 1차 지표가 아니다.
        "objective_nominal": nominal_objectives[-1] if nominal_objectives else None,
        # **1차 지표.** 코너 확인을 통과한 덱의 목적값. 확인이 없었으면
        # (코너를 잴 수 없는 스펙이면) None이다 - 확인하지 않은 값을 확인된
        # 것처럼 이 칸에 적지 않는다.
        "objective_confirmed": (
            result["objective_after"] if result["corner_confirmed"] else None
        ),
        "corner_confirmed": result["corner_confirmed"],
        "corner_failure": result["corner_failure"],
        "failure": result["failure"],
        "guard_infeasible": result["guard_infeasible"],
        "area_before": result["area_before"],
        "area_after": result["area_after"],
        # nominal에서 수락된 단계 수와, 그중 코너 확인에서 **살아남은** 수.
        # 둘의 격차가 현행 탐색의 약점 그 자체다(실측 10 -> 4).
        "steps_accepted_nominal": accepted_nominal,
        "steps_survived": result["steps_accepted"],
        "steps_rejected": result["steps_rejected"],
        "compound_steps_accepted": compound_steps_accepted,
        "simulations": meter.as_dict(),
        "steps": steps,
        "final_deck_sha256": {name: _digest(text) for name, text in sorted(final_texts.items())},
    }
    meta = {
        "wall_clock_s": round(elapsed, 3),
        "run_dir": run_dir,
        "history": state.history_path,
    }
    payload = {"record": record, "meta": meta}
    with open(os.path.join(out_dir, f"{side}_{strategy_name}.json"), "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    return payload


def _fmt(value, digits=4):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}g}"
    return str(value)


def print_table(a: dict, b: dict) -> None:
    rows = [
        ("strategy", lambda p: p["record"]["strategy"]),
        ("status", lambda p: p["record"]["status"]),
        ("objective before", lambda p: _fmt(p["record"]["objective_before"])),
        ("objective @ nominal", lambda p: _fmt(p["record"]["objective_nominal"])),
        ("objective @ corners *", lambda p: _fmt(p["record"]["objective_confirmed"])),
        ("corner confirmed", lambda p: str(p["record"]["corner_confirmed"])),
        ("steps accepted (nominal)", lambda p: str(p["record"]["steps_accepted_nominal"])),
        ("steps survived confirm", lambda p: str(p["record"]["steps_survived"])),
        ("steps rejected", lambda p: str(p["record"]["steps_rejected"])),
        ("nominal sim calls", lambda p: str(p["record"]["simulations"]["nominal_calls"])),
        ("corner sweeps", lambda p: str(p["record"]["simulations"]["sweep_calls"])),
        # 실제 비용. 위 두 줄은 "탐색이 몇 번 물어봤나"이고 이 줄은 그 아래에서
        # 돈 ngspice 프로세스 수다 - 45코너 5테스트벤치 스윕 하나가 225회다.
        ("ngspice runs (total)", lambda p: str(p["record"]["simulations"]["spice_runs"])),
        ("wall clock (s)", lambda p: _fmt(p["meta"]["wall_clock_s"], 6)),
    ]
    width = max(len(name) for name, _ in rows)
    print()
    print(f"{'':{width}}   {'A':>22}   {'B':>22}")
    print("-" * (width + 52))
    for name, get in rows:
        print(f"{name:{width}}   {get(a):>22}   {get(b):>22}")
    print()
    print("* 1차 지표. nominal 값이 아니라 코너 확인을 통과한 값으로 판정한다 -")
    print("  nominal에서 더 내려가는 것은 현행이 이미 잘하고, 그것이 문제였다.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--spec", required=True, help="benchmarks/.../spec.yaml")
    parser.add_argument(
        "--knob",
        action="append",
        type=parse_knob,
        default=[],
        metavar="REFDES:PARAM:DIRECTION",
        help="고정 노브 순위. 순서가 곧 순위다. 여러 번 줄 수 있다. "
             "--phase objective 에서는 필수(LLM을 부르지 않으려면 순위를 "
             "고정해야 한다), --phase area 에서는 줄 수 없다 "
             "(run_area_optimization 이 rank_by_area_gain 으로 순위를 스스로 "
             "계산해 여기서 준 값을 아예 읽지 않기 때문 - 조용히 버리는 대신 "
             "거부한다).",
    )
    parser.add_argument(
        "--strategy",
        action="append",
        default=[],
        help=f"전략 이름 두 개. 고를 수 있는 것: {', '.join(sorted(SEARCH_STRATEGIES))}",
    )
    parser.add_argument(
        "--corner-regime",
        action="append",
        type=parse_corner_regime,
        default=[],
        metavar="argmax|coverage:EPS:TAU",
        # **coverage:EPS:TAU는 파싱만 된다 - 오늘은 받아들여지지 않는다.**
        # 예전 문구는 metavar가 coverage 형식을 광고하면서 help는 아무 말도
        # 안 해서, "받는 것처럼" 보이고 실제로는 main()이 거부하는 격차가
        # 있었다. run_side가 run_optimization을 직접 부르고
        # corner_reduction의 중간-루프 축소를 거치지 않는 한(cli.py처럼
        # seed_from_sweep/corner_sim.build_corner_simulate를 타지 않는 한)
        # coverage는 시작 전에 거부된다 - 그 사실을 문구 자체에 적는다.
        help="쪽마다의 코너 체제. 전략과 마찬가지로 정확히 두 번 준다. "
             "생략하면 양쪽 다 argmax. coverage:EPS:TAU 형식은 파싱은 되지만 "
             "이 하니스가 아직 corner_reduction의 중간-루프 축소를 거치지 "
             "않으므로 argmax가 아니면 시작 전에 거부된다(reject) - "
             "run_side가 cli.py처럼 seed_from_sweep을 거치게 된 뒤에 열린다.",
    )
    parser.add_argument(
        "--phase", choices=("objective", "area"), default="objective",
        help="area 는 run_area_optimization(결정론적 순위, optimize: 불필요). "
             "탐색 전략 A/B 의 기본 선택이다 - LLM 분산이 0이므로 실행 하나가 "
             "판정에 쓰일 수 있다.",
    )
    parser.add_argument("--max-steps", type=int, default=optimizer_module.MAX_OPTIMIZE_STEPS,
                        help="시뮬레이션 예산(탐색 단계 수). 양쪽에 같은 값이 간다.")
    parser.add_argument("--sim-timeout", type=int, default=300)
    parser.add_argument("--out-dir", default=os.path.join("runs", "search_ab"))
    parser.add_argument("--name", default=None, help="기록 디렉터리 이름")
    parser.add_argument("--force", action="store_true",
                        help="기록 디렉터리가 이미 있으면 지우고 다시 쓴다")
    parser.add_argument(
        "--assert-identical",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="두 기록이 같아야 한다고 주장한다. 같은 전략이면 기본으로 켜진다.",
    )
    args = parser.parse_args(argv)

    if len(args.strategy) != 2:
        parser.error("--strategy를 정확히 두 번 주어야 한다 (같은 이름을 두 번 줘도 된다)")
    for name in args.strategy:
        if name not in SEARCH_STRATEGIES:
            parser.error(f"unknown strategy {name!r}; known: {sorted(SEARCH_STRATEGIES)}")
    if args.phase == "objective" and not args.knob:
        parser.error(
            "--knob을 최소 하나 주어야 한다. 순위를 고정하지 않으면 LLM을 부르게 되고, "
            "그러면 이 하니스가 존재하는 이유가 사라진다"
        )
    if args.phase == "area" and args.knob:
        # F2 리뷰(Important, 승격): `run_area_optimization`은
        # `rank_by_area_gain`으로 순위를 스스로 계산해 `OptimizerAgents`를
        # 통째로 새로 만들고(`optimizer.py:2109-2118`), 여기서 준 `--knob`은
        # 그 안에서 **아무 데도 읽히지 않는다** - 실제로 쓰인 순위는
        # `history.jsonl`의 `optimize_area_ranking` 이벤트에만 있다. 조용히
        # 버리면 `record["knob_ranking"]`이 무시된 값을 그대로 실어 "결과는
        # 자기가 반환한 것을 기술해야 한다"는 규칙(이 저장소가 다섯 번 친
        # 규칙)을 어긴다. `corner_regime`의 coverage를 시작 전에 거부하는
        # 것과 같은 모양·같은 이유로 여기서도 경계에서 거부한다.
        parser.error(
            "--phase area와 --knob을 함께 줄 수 없다: run_area_optimization은 "
            "rank_by_area_gain으로 순위를 스스로 계산하므로 --knob으로 준 "
            "고정 순위는 아무 데도 읽히지 않는다 - 조용히 버리는 대신 시작 "
            "전에 거부한다. 실제로 쓰인 순위는 history.jsonl의 "
            "optimize_area_ranking 이벤트에서 확인해라."
        )
    if not args.corner_regime:
        args.corner_regime = [None, None]
    if len(args.corner_regime) != 2:
        parser.error("--corner-regime을 정확히 두 번 주어야 한다 (또는 아예 주지 않는다)")
    # **coverage 체제를 여기서 거부한다 - 조용히 받아 놓고 아무것도 안 하지
    # 않는다.** `spec.corner_reduction.coverage`는 `corner_selection.
    # seed_from_sweep` 한 곳에서만 읽히고, 그 함수는 `cli.py`에서만 불린다.
    # 그런데 이 하니스는 `run_optimization`을 직접 부르고(`run_side`,
    # 오케스트레이터의 코너-축소 중간 루프에 들어가지 않는다) `optimizer.py`는
    # `corner_reduction`을 한 번도 참조하지 않는다 - 그래서 coverage 체제는
    # spec 필드를 바꿔 놓을 뿐 이 호출 경로 어디에서도 읽히지 않는다. 조용히
    # 통과시키면 두 쪽이 실제로는 같은(argmax) 코너로 돌면서 기록에는 서로
    # 다른 체제 이름이 실려, 격자의 셀 하나가 통째로 거짓이 된다 - 시작 전에
    # 거부하는 것이 이 저장소의 기존 관례다(5764abe: "튜닝 루프는 조합형
    # 스펙을 조각으로 돌지 않고 시작 전에 거부한다").
    for regime in args.corner_regime:
        if regime is not None:
            parser.error(
                "이 하니스는 run_optimization을 직접 부르고 corner_reduction의 "
                "중간-루프 코너 축소를 거치지 않으므로, corner_reduction.coverage는 "
                "이 경로에서 아무것도 읽지 않는다 - argmax만 낼 수 있다. "
                "coverage는 corner_selection.seed_from_sweep에서만 읽히고 그것은 "
                "cli.py에서만 불린다; 이 하니스에서 coverage를 실제로 살리려면 "
                "이 파일이 그 배선을 통과하도록 바뀌어야 한다."
            )

    name = args.name or f"{args.strategy[0]}__vs__{args.strategy[1]}"
    out_dir = os.path.join(args.out_dir, name)
    # **이미 있는 디렉터리에 겹쳐 쓰지 않는다.** RunState의 history.jsonl은
    # append이고 이 하니스는 그 파일에서 단계를 읽는다 - 같은 --name으로 두 번
    # 돌리면 두 번째 실행의 기록에 첫 실행의 단계가 섞인다. 실제로 당했다:
    # 자기 검사가 한쪽에 2단계, 다른 쪽에 1단계를 보고 "다르다"고 했는데 원인은
    # 전략이 아니라 남아 있던 파일이었다. 그 반대 방향(양쪽에 똑같이 섞여
    # 우연히 같아 보이는 것)이 훨씬 나쁘다 - 검사가 통과했는데 아무것도 재지
    # 않은 것이 된다.
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        if not args.force:
            parser.error(
                f"{out_dir} already has contents; the run dirs would be appended to, "
                f"mixing an earlier run's steps into this record. Use --force to wipe it "
                f"or --name to pick another."
            )
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    # 코너 경로의 적격성(사전 등록 개정 1, design.md "코너 경로의 적격성"절).
    #
    # F1 리뷰(Important): 먼저 이 게이트가 딛고 선 전제(`corner_capable` -
    # optimizer.py:1757의 `spec.pvt_corners is not None and
    # agents.verify_corners is not None`) 그 자체를 대조군을 돌리기 **전에**
    # 확인한다. 거짓이면 결과의 `steps_accepted`가 확인 스윕 생존 수가 아니라
    # `_search`의 원시 명목 수락 수라(1911-1915 vs 1937), `steps_survived > 0`
    # 만으로는 이 실행이 코너 확인을 거쳤는지 알 수 없다 - 그래서 시뮬레이션을
    # 전혀 쓰지 않고(스펙 재파싱 + `OptimizerAgents` 구성뿐, ngspice 미실행)
    # 이 전제부터 거부한다. `nominal_only`는 "대조군이 0개 수락했다"(`void`,
    # 조건이 발생하지 않았다)와는 **다른 사실** - 이 실행은 애초에 코너 인식
    # 수락으로 돌지 않는다.
    spec_for_gate = load_spec(args.spec)
    gate_agents = build_agents(
        spec_for_gate, args.strategy[0], args.knob, Meter(), args.sim_timeout
    )
    corner_capability = _corner_capability(spec_for_gate, gate_agents)

    # 대조군은 두 --strategy 중 문자 그대로 "coordinate_descent"인 쪽이다
    # (위치는 무관 - a든 b든). 그 대조군을 **먼저** 돌리고, 0 수락이면 나머지
    # 쪽은 아예 돌리지 않는다(대조군이 스텝을 하나도 수락하지 못하면 비교할
    # 기준선이 없다 - 기준선이 자기 가드를 못 지키는 경우가 그렇고, 여유분
    # 하한 측정이 P2 에서 정확히 이렇게 무효가 됐다. void 는 실패가 아니라
    # "조건이 발생하지 않았다"이다). 어느 쪽도 "coordinate_descent"가
    # 아니면(둘 다 compound_fallback_*인 비교 등) 이 게이트가 세울 기준선
    # 자체가 없으므로 적용하지 않는다 - **적용하지 않았다는 사실도
    # 기록한다**, 조용히 건너뛰면 "게이트가 통과했다"와 "게이트가 아예 없다"가
    # history 만으로는 구별되지 않는다.
    control_idx = next(
        (i for i, s in enumerate(args.strategy) if s == "coordinate_descent"), None
    )
    control_side = None if control_idx is None else ("a" if control_idx == 0 else "b")

    def _run(idx: int) -> dict:
        side = "a" if idx == 0 else "b"
        return run_side(
            side, args.strategy[idx], args, args.knob, out_dir,
            args.corner_regime[idx], phase=args.phase,
        )

    if not corner_capability["corner_capable"]:
        eligibility = _eligibility_verdict(corner_capability, control_side, None, None)
        comparison = {
            "spec": os.path.relpath(args.spec, os.getcwd()),
            "phase": args.phase,
            "step_budget": args.max_steps,
            "knob_ranking": args.knob,
            "strategies": args.strategy,
            "eligibility_check": eligibility,
            "verdict": eligibility["verdict"],
            "reason": eligibility["reason"],
            "a": None,
            "b": None,
        }
        with open(os.path.join(out_dir, "comparison.json"), "w") as f:
            json.dump(comparison, f, indent=2, sort_keys=True)
        print(
            f"\nNOMINAL_ONLY: {eligibility['reason']}. Neither side was run. "
            f"wrote {os.path.join(out_dir, 'comparison.json')}"
        )
        return 0

    payloads = {0: None, 1: None}
    if control_idx is None:
        eligibility = _eligibility_verdict(corner_capability, None, None, None)
        payloads[0] = _run(0)
        payloads[1] = _run(1)
    else:
        control_payload = _run(control_idx)
        payloads[control_idx] = control_payload
        # record["steps_survived"]/["corner_confirmed"]는 run_side가 이름만
        # 바꿔 실은 optimizer.run_optimization/run_area_optimization의 원시
        # 결과 result["steps_accepted"]/["corner_confirmed"]와 같은 값이다 -
        # corner_capable이 참으로 확인된 뒤이므로 여기서는 steps_survived가
        # 확인 스윕에서 살아남은 수라고 읽어도 된다.
        control_steps = control_payload["record"]["steps_survived"]
        control_confirmed = control_payload["record"]["corner_confirmed"]
        eligibility = _eligibility_verdict(
            corner_capability, control_side, control_steps, control_confirmed
        )
        if eligibility["verdict"] == "eligible":
            other_idx = 1 - control_idx
            payloads[other_idx] = _run(other_idx)

    a, b = payloads[0], payloads[1]

    if eligibility["verdict"] == "void":
        # partners>0(또는 짝이 되는 쪽)을 돌리지 않는다 - 비교할 기준선이 없다.
        comparison = {
            "spec": (a or b)["record"]["spec"],
            "phase": args.phase,
            "step_budget": args.max_steps,
            "knob_ranking": args.knob,
            "strategies": args.strategy,
            "eligibility_check": eligibility,
            "verdict": "void",
            "void_reason": eligibility["reason"],
            "a": a,
            "b": b,
        }
        with open(os.path.join(out_dir, "comparison.json"), "w") as f:
            json.dump(comparison, f, indent=2, sort_keys=True)
        print(
            f"\nVOID: control (coordinate_descent, side {eligibility['control_side']}) "
            "accepted 0 steps on this slot - there is no baseline to compare against. "
            f"The other side was not run. wrote {os.path.join(out_dir, 'comparison.json')}"
        )
        return 0

    print_table(a, b)

    identical = _records_match(a["record"], b["record"])
    should_assert = args.assert_identical
    if should_assert is None:
        should_assert = (args.strategy[0] == args.strategy[1]
                         and args.corner_regime[0] == args.corner_regime[1])

    comparison = {
        "spec": a["record"]["spec"],
        "phase": args.phase,
        "step_budget": args.max_steps,
        "knob_ranking": args.knob,
        "strategies": args.strategy,
        "eligibility_check": eligibility,
        "records_identical": identical,
        "identity_asserted": should_assert,
        "a": a,
        "b": b,
    }
    with open(os.path.join(out_dir, "comparison.json"), "w") as f:
        json.dump(comparison, f, indent=2, sort_keys=True)

    if should_assert:
        # 같은 전략을 양쪽에 넣었는데 기록이 다르면 통제되지 않은 무언가가
        # 남아 있다는 뜻이다. 그 상태에서 잰 A/B는 아무것도 증명하지 못하므로,
        # 경고가 아니라 실패로 끝난다.
        if identical:
            print("SELF-COMPARISON: identical records (side a == side b)")
        else:
            print("SELF-COMPARISON FAILED: the two records differ", file=sys.stderr)
            for line in _diff_lines(a["record"], b["record"]):
                print(f"  {line}", file=sys.stderr)
            return 1
    print(f"\nwrote {os.path.join(out_dir, 'comparison.json')}")
    return 0


def _comparable(record: dict) -> dict:
    """비교에서 빼는 것은 `side` 하나다 - 그것만이 정의상 다르다."""
    return {k: v for k, v in record.items() if k != "side"}


def _records_match(a: dict, b: dict) -> bool:
    return _comparable(a) == _comparable(b)


def _diff_lines(a: dict, b: dict) -> list[str]:
    left, right = _comparable(a), _comparable(b)
    lines = []
    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            lines.append(f"{key}: {left.get(key)!r} != {right.get(key)!r}")
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
