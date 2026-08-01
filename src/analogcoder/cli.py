import argparse
import asyncio
import os
import sys
import uuid

from analogcoder.agents.backend import AgentBackend
from analogcoder.agents.backends.claude_sdk import DEFAULT_CLAUDE_MODEL, ClaudeSDKBackend
from analogcoder.agents.backends.openai_compatible import OpenAICompatibleBackend
from analogcoder.agents.optimizer import propose_candidates
from analogcoder.agents.simulator_agent import simulate as agent_simulate
from analogcoder.agents.tuner import propose_topology_swap, propose_tuning
from analogcoder.agents.verifier import verify_post, verify_pre
from analogcoder.checkpoint import (
    BOUNDARY_ATTEMPT,
    BOUNDARY_OPTIMIZATION,
    BOUNDARY_OUTER_ITERATION,
    CheckpointRejected,
    build_checkpoint,
    checkpoint_path,
    load_checkpoint,
    restore_state,
    write_checkpoint,
)
from analogcoder.corner_selection import grown_with, label, raw_label, seed_from_sweep
from analogcoder.corner_sim import CornerState, build_corner_simulate
from analogcoder.history import count_events, discarded_ranges, line_count, read_events
from analogcoder.judge_tools import evaluate_criteria
from analogcoder.netlist import resolve_includes
from analogcoder.optimizer import OptimizerAgents, run_optimization
from analogcoder.orchestrator import OrchestratorAgents, _attempt_summary, run_orchestration
from analogcoder.pvt import run_full_pvt_sweep
from analogcoder.report import write_report_md, write_result_json
from analogcoder.simulators.cache import CachingSimulator
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec, refuse_composed_testbenches
from analogcoder.state import RunState


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="analogcoder")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--simulator", choices=["ngspice"], default="ngspice")
    parser.add_argument("--agent-backend", choices=["claude", "openai-compatible"], default="claude")
    parser.add_argument("--llm-base-url", default=None)
    parser.add_argument("--llm-model", default=None)
    parser.add_argument("--claude-model", default=DEFAULT_CLAUDE_MODEL)
    parser.add_argument(
        "--agent-model",
        action="append",
        default=[],
        metavar="AGENT=MODEL",
        help=f"override one agent's model; AGENT is one of {', '.join(AGENT_NAMES)}",
    )
    parser.add_argument("--run-dir", default=None)
    # **재개는 기본 동작이 아니다.** 플래그 없이 기존 run-dir을 다시 가리키면
    # 오늘 그대로 처음부터 돈다 - 조용히 이어가면 사용자가 새 실행을 기대한
    # 자리에서 절반짜리 실행이 완성되고, 그것이 온전한 실행처럼 측정에 들어간다.
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume the run in --run-dir from its checkpoint (rejects if anything drifted)",
    )
    return parser


# judge는 여기 없다. 판정은 LLM이 아니라 `judge_tools.evaluate_criteria`이므로
# 얹을 모델이 없고, `--agent-model judge=...`는 이제 에러다 - 조용히 받아주면
# 사용자가 아무것도 바꾸지 못한 채 바꿨다고 믿는다.
AGENT_NAMES = ("simulator", "tuner", "verifier", "optimizer")

def _no_drift() -> dict:
    """빈 argmax 기록. **매번 새로 만든다** - 모듈 수준 상수를 얕게 복사해
    돌려주면 result에 실리는 "criteria" 리스트가 그 상수와 **같은 객체**가 되어,
    거기에 append하는 소비자 하나가 프로세스 전역을 오염시킨다.

    키는 `_argmax_drift`가 내는 것과 **같아야** 한다. 코너를 못 재는 스펙의
    결과에서만 칸 하나가 사라지면, 그 칸을 읽는 소비자가 하필 "아무것도 안 잰
    실행"에서 KeyError로 죽는다."""
    return {
        "criteria": [],
        "moved_count": 0,
        "total": 0,
        "compared_count": 0,
        "unmeasured_count": 0,
        "measurability_changed_count": 0,
        "unpaired_count": 0,
    }


def _argmax_drift(entry_sweep: dict, verdict_sweep: dict) -> dict:
    """기준별 최악 코너가 진입 스윕과 판정 스윕 사이에서 움직였는가.

    **설계가 움직일 때 최악 코너가 얼마나 움직이는가**는 아직 아무도 재지
    않았다. 거의 안 움직이면 코너 지속성이 최적에 가깝고 어떤 적응형 기법도
    이기지 못한다; 많이 움직이면 지속성이 나쁘고 적응이 필요하다. 다음 축소
    기법을 고르는 근거가 이 숫자이며, 실행이 이미 만드는 데이터라 공짜다.

    **판정에는 아무 영향을 주지 않는다 - 순수한 기록이다.**

    한쪽 스윕에만 있는 기준은 moved=False다. 짝이 없으면 "움직였다"고 말할
    근거가 없고, 없는 것을 이동으로 세면 이 숫자의 유일한 용도가 망가진다.

    **`value is None`인 항목은 argmax가 아니다.** `worst_case_measurements`는
    어느 코너에서 측정이 빠지면 그 중 **첫 번째** 코너를 값 없이 적는다
    (`pvt.py`의 `missing_corners[0]`). 그 "첫 번째"를 정하는 것은 회로가 아니라
    `all_corners`의 순서, 즉 스펙의 **코너 선언 순서**다. 그것을 이동으로 세면
    이 지표는 코너 지속성이 아니라 리스트 순서를 재게 된다 - 실측
    (`runs/pvt_sonnet_1`): 일곱 기준의 **측정값이 양쪽 스윕에서 전부 동일**한데
    `moved_count`가 2로 나왔고, 두 건 다 양쪽 값이 None이었다. 스펙의 process
    목록 순서만 바꾸면(측정값 불변) 판정이 뒤집힌다.

    그래서 네 상태를 **다른 칸에** 센다. 뭉뚱그리면 서로 다른 사실이 한 숫자가
    된다:

    - `compared` - 양쪽 다 값이 있다. **`moved`를 말할 수 있는 유일한 상태**이고
      `moved_count`의 분모는 `total`이 아니라 `compared_count`다.
    - `unmeasured` - 양쪽 다 값이 없다. 코너 이름은 남기지만(그 코너가 "처음으로
      값이 안 나온 코너"라는 것도 사실이다) 이동으로 세지 않는다.
    - `measurability_changed` - 한쪽만 값이 있다. 두 이름이 애초에 같은 종류가
      아니다(하나는 argmax, 다른 하나는 빠진 코너). 이것을 이동으로 세면 지표가
      코너 지속성이 아니라 **수렴 여부**를 재게 된다.
    - `unpaired` - 한쪽 스윕에만 있는 기준.

    D1을 무효로 만든 질문("이 지표가 다른 답을 낼 조건이 내가 잰 런에 있었는가")이
    여기서는 지표 **정의** 안에서 깨져 있었다. 판정에 필요한 정보는 이미 같은
    dict 안에 있었고 읽지 않았을 뿐이다."""
    entry_wc = entry_sweep.get("worst_case_corners", {})
    final_wc = verdict_sweep.get("worst_case_corners", {})
    names = list(entry_wc) + [name for name in final_wc if name not in entry_wc]

    criteria = []
    for name in names:
        entry_raw = entry_wc.get(name)
        final_raw = final_wc.get(name)
        entry_label = raw_label(entry_raw)
        final_label = raw_label(final_raw)

        if entry_raw is None or final_raw is None:
            status = "unpaired"
        else:
            measured = (entry_raw.get("value") is not None, final_raw.get("value") is not None)
            status = {
                (True, True): "compared",
                (False, False): "unmeasured",
            }.get(measured, "measurability_changed")

        criteria.append({
            "name": name,
            "entry": entry_label,
            "final": final_label,
            "status": status,
            "moved": (
                status == "compared"
                and entry_label is not None
                and final_label is not None
                and entry_label != final_label
            ),
        })

    def _count(status: str) -> int:
        return sum(1 for c in criteria if c["status"] == status)

    return {
        "criteria": criteria,
        "moved_count": sum(1 for c in criteria if c["moved"]),
        "total": len(criteria),
        "compared_count": _count("compared"),
        "unmeasured_count": _count("unmeasured"),
        "measurability_changed_count": _count("measurability_changed"),
        "unpaired_count": _count("unpaired"),
    }


def _reduction_off_reason(corner_capable: bool, reduction) -> str:
    """축소가 꺼진 **이유**. 조용히 아무것도 안 하는 것이 이 저장소가 반복해서
    당한 실패 모양이므로, 꺼졌다는 사실만으로는 부족하고 왜 꺼졌는지가 결과와
    이력 양쪽에 남아야 한다. 세 경우는 서로 다른 사실이고 고치는 방법도 다르다."""
    if not corner_capable:
        return (
            "the spec declares no pvt_corners, so there are no corners to reduce - "
            "the mid-loop simulates the deck as written, exactly as it does today"
        )
    if reduction is None:
        return (
            "the spec declares no corner_reduction block, so the mid-loop keeps "
            "today's behaviour (the deck as written, one point)"
        )
    return "corner_reduction.enabled is false in the spec"


def _sweep_error(phase: str, exc: BaseException) -> dict:
    """**돌지 못한** 스윕의 기록. '스윕이 실패했다'와 '스윕이 돌지 못했다'는
    다른 사실이고, 산출물에서 구별되어야 한다 - 전자는 코너별 값이 있고
    (report.md가 PVT 섹션을 그린다) 후자는 실을 값 자체가 없다.

    `phase`는 `entry`(진입 스윕) 또는 `verdict`(판정 스윕)다. 둘은 잃는 것이
    다르다: 진입 스윕이 못 돌면 실행이 시작조차 못 하고, 판정 스윕이 못 돌면
    이미 끝난 튜닝 루프(two_stage_opamp 기준 103분)의 결과가 확정되지 못한다."""
    return {"phase": phase, "type": type(exc).__name__, "error": f"{type(exc).__name__}: {exc}"}


def _early_fail_result(
    *,
    run_dir: str,
    netlist_paths: dict,
    resumed_from: dict | None,
    topology_swaps: list,
    failure_reason: str,
    reduction_reason: str,
    sweep_error: dict | None = None,
    attempts: int = 0,
    grown: list[list[str]] | None = None,
    promotion_reentries: list[dict] | None = None,
) -> dict:
    """루프를 시작하지 못하고 끝난 실행의 결과. **모양은 여기 한 곳에서만
    정의한다.**

    이 저장소는 결과 모양이 두 곳에서 따로 만들어졌을 때 한쪽만 키를 빠뜨리는
    사고를 이미 겪었다(코너 시드 실패의 이른 반환에 `topology_swaps`가 없었다 -
    "스왑 0건"과 "이 실행은 스왑 기록을 아예 안 쓴다"가 같은 부재가 됐다).
    이른 반환이 둘로 늘어나므로 생성기를 하나로 묶는다.

    `pvt_sweep`은 **스윕을 시도했을 때만** 실린다(그때 값은 `None`이다). 키의
    부재는 "이 스펙에는 판정 스윕이라는 것이 없다"이고, `None`은 "돌려 했는데
    값이 없다"이며, dict는 "돌았다"다 - 셋은 다른 사실이다.

    **`attempts`/`grown`/`promotion_reentries`는 인자다. 하드코딩된 0/[]가
    아니다.** 이 셋도 `topology_swaps`와 정확히 같은 이유로 누적값이어야
    한다: **재개된** 실행이 이 갈래로 끝날 수 있고(체크포인트를 읽은 뒤 진입
    스윕 재실행이 실패하는 경우), 그때 체크포인트는 이전 attempt 들의
    `grown_labels`와 `promotion_reentries`를 이미 싣고 있다. 0/[]로 내보내면
    "재진입이 0건이었다"와 "재진입 기록을 잃었다"가 같은 값이 되고, 그것이
    이 함수가 존재하는 이유인 그 사고와 같은 모양이다. 도달 불가 논증으로는
    닫히지 않는다 - 최종 리뷰가 도달 경로를 찾았다."""
    result = {
        "status": "FAIL",
        "final_netlist_paths": netlist_paths,
        "run_dir": run_dir,
        "iterations_used": 0,
        "final_criteria": [],
        "failure_reason": failure_reason,
        # 어느 갈래로 끝나든 결과는 자기가 재개된 것인지 말해야 한다.
        "resumed_from": resumed_from,
        # 같은 이유로 topology_swaps도 실린다(I-3, 키 존재 계약). 하드코딩된
        # []가 아니라 누적값인 이유는, 이 갈래에 체크포인트가 실어 온 스왑이
        # 있을 수 있고 그때 []는 없어진 기록을 없었던 것처럼 만들기 때문이다.
        "topology_swaps": list(topology_swaps),
        # 같은 계약이 `attempt_summary`에도 걸린다. 이 갈래는 루프를 시작조차
        # 못 했으므로 제안이 하나도 없고, 그 사실은 **0으로 채운 dict**로
        # 말해야 한다 - 키가 통째로 없으면 "제안이 0건"과 "이 실행은 집계를
        # 아예 안 쓴다"가 같은 부재가 되고, 그것이 D1 측정을 무효로 만든 모양이다.
        "attempt_summary": _attempt_summary([]),
        # "확인했고 멀쩡했다"와 "그 기록이 사라졌다"가 같은 부재면 안 된다.
        "pvt_sweep_error": sweep_error,
        "corner_reduction": {
            "active": False,
            "reason": reduction_reason,
            "final_set": [],
            "attempts": attempts,
            "area_baselines": 0,
            "grown": [list(g) for g in (grown or [])],
            # 같은 계약이 M10(T19)에도 걸린다 - 이 갈래는 재진입 루프 자체를
            # 시작하지 못했으므로 승격 재진입도 0건이고, 그 사실은 빈 리스트로
            # 말해야 한다(키 부재가 아니라).
            "promotion_reentries": [dict(r) for r in (promotion_reentries or [])],
            "path_disagreement": None,
            "unattributed_failures": None,
            "reentry_skipped": None,
            "argmax_drift": _no_drift(),
        },
    }
    if sweep_error is not None:
        result["pvt_sweep"] = None
    return result


def _reused_baseline_sweep(history_path) -> dict | None:
    """재개한 실행이 다시 쓰는 **진입** PVT 스윕. 이력에 없으면 None.

    이 스윕의 입력은 스펙이 가리키는 원본 덱이고, 재개는 스펙/넷리스트 해시가
    같을 때만 허용된다 - 그래서 다시 돌면 같은 값이 나온다. 그런데 bandgap의
    45 코너 스윕은 286초이고, 최적화의 여유분은 이 스윕에서 읽는다. 아무것도
    배우지 않는 재실행에 그 시간을 다시 치를 이유가 없다.

    버려진 이터레이션의 이벤트는 read_events가 이미 떨어뜨린다. 진입 스윕은
    어떤 체크포인트보다 먼저 쓰이므로 버려진 범위에 들어갈 수 없다.
    """
    if not os.path.exists(history_path):
        return None
    for event in reversed(read_events(history_path)):
        if event.get("step") == "pvt_baseline_sweep":
            return {k: v for k, v in event.items() if k != "step"}
    return None


def _entry_texts(resume_progress, initial_netlist_texts, attempt, state) -> dict[str, str]:
    """이 attempt의 run_orchestration에 넘길 덱.

    재개가 아니면 오늘 그대로: attempt 0은 원본, 재진입은 **수렴된 덱**이다.

    경계 1에서 재개하면 그 attempt가 실제로 **받았던** 덱을 파일에서 되읽는다.
    면적 게이트의 기준선은 이 인자에서 잡히므로(`index_baseline_components`),
    원본을 다시 읽으면 재진입한 attempt의 성장 한도가 조용히 달라진다. 그
    경로들은 체크포인트가 적어 두고, load_checkpoint가 존재 여부까지 검사한다.
    """
    if resume_progress is not None:
        texts = {}
        for name, path in resume_progress.entry_netlist_paths.items():
            with open(path) as f:
                texts[name] = f.read()
        return texts
    return initial_netlist_texts if attempt == 0 else state.current_netlist_texts()


def _build_agent_backend(args, model: str | None = None) -> AgentBackend:
    if args.agent_backend == "claude":
        return ClaudeSDKBackend(model=model or getattr(args, "claude_model", DEFAULT_CLAUDE_MODEL))
    if not args.llm_base_url or not args.llm_model:
        raise ValueError("--llm-base-url and --llm-model are required when --agent-backend=openai-compatible")
    return OpenAICompatibleBackend(base_url=args.llm_base_url, api_key_env="LOCAL_LLM_API_KEY", model=args.llm_model)


def _build_agent_backends(args) -> dict[str, AgentBackend]:
    """One backend instance per agent, so a single agent can be dropped to a
    weaker model independently. Agent modules stay untouched - cli.py already
    injects a backend per agent, so the model choice lives entirely here."""
    overrides = {}
    for raw in getattr(args, "agent_model", []) or []:
        name, _, model = raw.partition("=")
        if name not in AGENT_NAMES:
            raise ValueError(f"unknown agent '{name}' in --agent-model; expected one of {list(AGENT_NAMES)}")
        overrides[name] = model

    return {name: _build_agent_backend(args, overrides.get(name)) for name in AGENT_NAMES}


async def _run(args) -> dict:
    spec = load_spec(args.spec)

    # **조합형 테스트벤치는 아직 튜닝 루프에 배선되지 않았다 - 그래서 여기서
    # 시작 전에 거부한다.**
    #
    # 조용히 도는 쪽이 훨씬 나쁘다: 튜닝 루프의 `simulate_fn`은 시뮬레이터
    # 에이전트에게 `state.current_netlist_paths()`를 넘기는데, 조합형
    # 테스트벤치에서 그 파일은 **tunable 조각 하나**이고 회로가 아니다. 신호
    # 선언부도 코너도 없는 덱이므로 자극 없는 회로가 돌고, 그 결과가 판정에
    # 들어간다. 게다가 분석 3이 실측으로 보인 것처럼 조각 뷰에서는
    # `check_stimulus_untouched`가 자극 변경을 **approved=True로 통과**시키고
    # (게이트가 열린 채 실패한다), `signal_path`는 `AMP drives vdd`라는 거짓
    # 구조 주장을 되살리며, `.option scale`이 다른 조각에 실려 있으면 면적
    # 게이트의 판정이 뒤집힌다(같은 제안이 approved=True <-> False).
    #
    # 전체 코너 스윕(`run_full_pvt_sweep`)과 코너 축소 경로는 배선되어 있다 -
    # 그쪽은 덱을 텍스트로 만들어 임시 파일에 쓰므로 조합이 그 자리에 정확히
    # 들어간다. 남은 것은 에이전트에게 **경로**를 넘기는 이 경로 하나다.
    refuse_composed_testbenches(
        spec,
        consumer="the tuning loop",
        detail="the simulator agent is handed that path directly.",
    )

    # Includes are absolutized here, at the one point netlist text enters the
    # system, because everything downstream relocates that text away from the
    # directory it was read from (RunState stages it into the run dir, then
    # NgspiceBackend stages that into a temp dir) and a bare relative
    # .include stops resolving the moment it moves.
    initial_netlist_texts = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            initial_netlist_texts[tb.name] = resolve_includes(f.read(), os.path.dirname(tb.netlist_path))

    run_dir = args.run_dir or os.path.join("runs", uuid.uuid4().hex[:8])

    # **거부는 아무것도 시작하기 전에.** load_checkpoint는 어긋난 것이 하나라도
    # 있으면 CheckpointRejected를 던지고, main()이 그것을 사유와 함께 찍고
    # 끝낸다 - 크래시가 아니라 무엇이 왜 어긋났는지 말하는 오류다.
    checkpoint = load_checkpoint(run_dir, args.spec, spec) if getattr(args, "resume", False) else None

    state = RunState(run_dir=run_dir, testbench_names=[tb.name for tb in spec.testbenches])

    resumed_from = None
    if checkpoint is not None:
        restore_state(state, checkpoint)
        # 크래시한 이터레이션의 **부분** 이벤트가 로그에 남아 있다. 자르지
        # 않는다 - 증거를 파괴하는 것은 답이 아니다. 대신 버려진 줄 범위를
        # 선언하고, analogcoder.history.read_events가 읽을 때 떨어뜨린다.
        # 이것이 없으면 measure_repeat_rate.py와 paired_tuner_probe.py가
        # 버려진 시도의 제안을 실제 제안으로 센다 - D1을 무효로 만든 것과 같은
        # 부류의 결함이다.
        discarded = [checkpoint.history_lines, line_count(state.history_path)]
        discarded_events = count_events(state.history_path, discarded[0], discarded[1])
        state.log_event(
            "resume",
            {
                "boundary": checkpoint.boundary,
                "attempt": checkpoint.attempt,
                "outer_iter": checkpoint.progress.outer_iter if checkpoint.progress else None,
                "checkpoint_path": checkpoint_path(run_dir),
                "discarded_lines": discarded,
                "discarded_events": discarded_events,
            },
        )
        resumed_from = {
            "boundary": checkpoint.boundary,
            "attempt": checkpoint.attempt,
            "outer_iter": checkpoint.progress.outer_iter if checkpoint.progress else None,
            "checkpoint_path": checkpoint_path(run_dir),
            "discarded_lines": discarded,
            "discarded_events": discarded_events,
            # 이 run-dir이 지금까지 재개된 횟수(이번 것 포함). 두 번 재개된
            # 실행이 한 번짜리와 같아 보이면 안 된다.
            "resume_count": len(discarded_ranges(read_events(state.history_path, drop_discarded=False))),
        }

    # 내용 주소 캐시로 감싼다. 실행 하나가 같은 (덱, control block, 코너,
    # 시뮬레이터)를 여러 번 재는 자리가 실제로 있다 - 최적화의 이분 탐색은 이미
    # 스윕한 버전을 되짚고, 롤백 직후의 다음 외부 이터레이션은 되돌아간 덱을
    # 다시 잰다. 적중/미적중은 state.log_event로 history.jsonl에 무조건 남는다
    # (한 번도 안 맞는 캐시와 아예 안 붙은 캐시가 같아 보이면 안 된다).
    sim_backend = CachingSimulator(NgspiceBackend(), log_event=state.log_event)
    agent_backends = _build_agent_backends(args)

    async def simulate_fn(netlist_texts, spec_arg):
        merged_measurements = {}
        by_testbench = {}
        # 테스트벤치별 status를 하나로 합친다. **전부 성공했을 때만** 성공이고,
        # 처음 만난 비성공이 합쳐진 status가 된다.
        #
        # 최상위 status가 아예 없으면 optimizer._run_simulation이 "없는 키는
        # 실패가 아니다"라는 자기 규칙에 따라 성공으로 읽는다. 그러면 수렴하지
        # 못한 테스트벤치의 측정값으로 마진을 태우는 결정이 내려진다 - 실제로
        # 수렴 실패가 낸 iq_ua=1.0이 235->1 개선으로 수락된 적이 있다. 신호가
        # 틀린 것이 아니라 **없는** 쪽이라 어떤 mock에도 보이지 않는 결함이다.
        status = "success"
        paths = state.current_netlist_paths()
        for tb in spec_arg.testbenches:
            result = await agent_simulate(paths[tb.name], tb.control_block, sim_backend, agent_backends["simulator"])
            merged_measurements.update(result["measurements"])
            by_testbench[tb.name] = result
            # 기본값 "success"에 도달하는 경로는 오늘 없다 - SIMULATION_SCHEMA가
            # status를 required로 두고 run_agent가 검증한다. 그래도 기본값을 두는
            # 것은, 없는 키를 실패로 읽으면 스키마가 느슨해지는 날 시뮬레이터
            # 에이전트 전체가 조용히 실패로 접히기 때문이다.
            tb_status = result.get("status", "success")
            if status == "success" and tb_status != "success":
                status = tb_status
        return {"status": status, "measurements": merged_measurements, "by_testbench": by_testbench}

    async def judge_fn(measurements, spec_arg):
        """판정은 LLM이 아니다 - `evaluate_criteria`를 그대로 부른다.

        기록된 실행의 judge 이벤트 58건(기준 인스턴스 742건)을 재생해
        대조한 결과, 기준별 `pass`도 `overall_pass`도 **차이가 0건**이었다.
        LLM이 더한 것은 판정이 아니라 해악뿐이다: 시뮬레이션이 실패해
        measurements가 비었을 때 **25건에서 `actual=0, margin=0`을 지어냈다**.
        `evaluate_criteria`는 그 자리에 `NaN`을 쓴다 - "재지 못했다"는
        "재보니 0이었다"와 다른 사실이다.

        **`async`는 유지한다.** `orchestrator.py`가 `await agents.judge(...)`로
        부르므로, 동기 함수로 바꾸면 코루틴 계약이 깨진다.

        `evaluate_criteria`는 손대지 않는다 - `pvt.py`/`optimizer.py`/
        `curation.py`가 이미 같은 함수를 직접 부르고 그 출력이 산출물에
        실린다. 결과로 `target` 문자열에서 단위가 빠지는데, 이는 의도된
        것이다: 중간 루프와 최종 스윕이 이제 같은 문자열을 낸다."""
        return evaluate_criteria(measurements, spec_arg.all_criteria)

    async def tune_fn(structure_view, judge_result, attempts_view, rejection_feedback, netlist_text_arg):
        return await propose_tuning(
            structure_view, judge_result, attempts_view, rejection_feedback, netlist_text_arg, agent_backends["tuner"]
        )

    async def verify_pre_fn(structure_view, judge_result, proposal, netlist_text_arg):
        return await verify_pre(structure_view, judge_result, proposal, netlist_text_arg, agent_backends["verifier"])

    async def verify_post_fn(prev_judge_result, new_judge_result, applied_changes):
        return await verify_post(prev_judge_result, new_judge_result, applied_changes, agent_backends["verifier"])

    async def propose_topology_fn(structure_view, judge_result, candidates, library, rejection_feedback):
        return await propose_topology_swap(
            structure_view, judge_result, candidates, library, rejection_feedback, agent_backends["tuner"]
        )

    async def propose_candidates_fn(structure_view, margins, objective, netlist_view):
        return await propose_candidates(
            structure_view, margins, objective, netlist_view, agent_backends["optimizer"]
        )

    def verify_corners_fn(netlist_texts):
        # **동기** 함수여야 한다. run_optimization은 이것을 await 없이 직접
        # 부르므로, async로 감싸면 돌아오는 코루틴 객체가 "쓸 수 없는 결과"로
        # 접혀 최적화가 크래시도 로그도 없이 통째로 UNCHANGED가 된다.
        # run_full_pvt_sweep 자체가 동기이고 LLM이 끼지 않으므로 감쌀 이유도 없다.
        #
        # log_event=state.log_event를 넘긴다 - 이것은 최적화 **확인** 스윕이라
        # 한 실행 안에서 여러 번 돈다(밴드갭 실측 6회 x 5테스트벤치 = 30줄).
        # 그래도 적는다: "코너마다 45줄은 안 된다"던 원래 논거는 *같은 덱을
        # 반복해서* 렌더링하는 것에 대한 것이었지, 여기는 그렇지 않다 - 탐색이
        # 스텝마다 넷리스트를 바꾸므로 매 확인 스윕이 실제로 다른 덱을 렌더링하고,
        # PWL 공급이나 include 경로가 그 스텝에서 깨졌는지는 이전 스텝의 렌더
        # 상태로는 알 수 없다. 30줄은 같은 사실 30번이 아니라 30개의 다른 사실이다.
        return run_full_pvt_sweep(netlist_texts, spec, sim_backend, log_event=state.log_event)

    async def agent_simulate_fn(netlist_path, control_block):
        """corner_sim이 부르는 모양은 `(netlist_path, control_block)` 두 인자다.
        시뮬레이터 백엔드와 에이전트 백엔드는 여기서 닫아 준다 - agent_simulate를
        그대로 넘기면 그 두 인자가 빠져 판정 경로 한가운데서 TypeError가 난다."""
        return await agent_simulate(
            netlist_path, control_block, sim_backend, agent_backends["simulator"]
        )

    # "코너를 잴 수 있는가"를 한 번만 적는다. 세 군데에 같은 조건을 따로 쓰면
    # 한 곳만 바뀌었을 때 baseline은 재는데 최적화에는 None이 가는 식으로
    # 조용히 어긋난다. optimizer.py도 같은 이름(corner_capable)을 쓴다.
    corner_capable = spec.pvt_corners is not None
    reduction = spec.corner_reduction
    # 블록이 없으면 축소는 켜지지 않는다 - CornerReduction의 기본값(enabled=True)은
    # **블록이 선언됐을 때** 무엇이 기본인지를 말하는 것이지, 선언하지 않은
    # 스펙의 동작을 바꾸라는 뜻이 아니다. 기존 스펙의 동작은 그대로 둔다.
    reduction_active = corner_capable and reduction is not None and reduction.enabled
    reduction_reason = (
        None if reduction_active else _reduction_off_reason(corner_capable, reduction)
    )

    # 시도를 가로질러 누적하는 유일한 result 키(I-2). 아래 **두** 이른 반환
    # (진입 스윕 실패, 코너 시드 실패)이 이 값을 실어야 하므로 그 앞에서
    # 만든다 - 상세한 근거는 아래 재진입 루프 직전의 주석에 있다.
    all_topology_swaps: list[dict] = (
        [dict(s) for s in checkpoint.all_topology_swaps] if checkpoint is not None else []
    )

    baseline_sweep = None
    if corner_capable:
        # 재개할 때는 이력에 남은 진입 스윕을 다시 쓴다. 이 스윕의 입력은
        # **원본 덱**이고 스펙 해시가 이미 검증됐으므로 다시 돌면 같은 값이
        # 나온다 - bandgap 45 코너 기준 286초를 아무것도 배우지 않고 다시
        # 치르는 것이다. 재개가 아니면 오늘 그대로 돈다.
        reused = _reused_baseline_sweep(state.history_path) if checkpoint is not None else None
        if reused is not None:
            baseline_sweep = reused
            state.log_event(
                "pvt_baseline_sweep_reused",
                {"corners": len(reused.get("per_corner", [])), "overall_pass": reused.get("overall_pass")},
            )
        else:
            try:
                baseline_sweep = run_full_pvt_sweep(
                    initial_netlist_texts, spec, sim_backend, log_event=state.log_event
                )
            except Exception as exc:   # noqa: BLE001 - 근거는 아래 주석
                # **가드가 없으면 여기서 터진 예외가 main()의
                # write_result_json/write_report_md 두 줄을 그대로 건너뛴다.**
                # run_optimization이 이미 같은 이유로 감싸여 있는데(그 자리의
                # 독스트링이 근거를 적는다) 스윕 두 호출부는 빠져 있었다.
                #
                # **왜 Exception인가.** 좁게 잡는 것으로는 부족하다:
                # `OSError`(워커의 ENOSPC/EAGAIN)와 `ValueError`(pvt.py의
                # CornerRenderError)만이 아니라, 스펙의 `operator: "=~"` 오타
                # 하나가 `judge_tools._OPERATORS[c.operator]`에서 `KeyError`를
                # 낸다 - 환경 장애 없이, 결정론적으로. 예외 종류를 열거하는
                # 것은 "다음에 어떤 종류가 이 자리에 도달하는가"를 추측하는
                # 것이고, 이 저장소는 그런 추측을 사실로 쓰지 않는다.
                #
                # 조용해지지 않는다: 예외의 **종류와 메시지가 그대로**
                # failure_reason과 history 이벤트에 실리고 종료 코드는 1이다.
                # 삼키는 것이 아니라 **산출물을 남기고 시끄럽게 실패**하는 것이다.
                error = _sweep_error("entry", exc)
                state.log_event("pvt_sweep_failed", error)
                return _early_fail_result(
                    run_dir=run_dir,
                    netlist_paths=state.current_netlist_paths(),
                    resumed_from=resumed_from,
                    topology_swaps=all_topology_swaps,
                    attempts=checkpoint.attempt if checkpoint is not None else 0,
                    grown=checkpoint.grown_labels if checkpoint is not None else None,
                    promotion_reentries=(
                        checkpoint.promotion_reentries if checkpoint is not None else None
                    ),
                    failure_reason=f"the entry PVT sweep could not run: {error['error']}",
                    reduction_reason=(
                        f"the entry PVT sweep could not run, so no corner set could be "
                        f"seeded: {error['error']}"
                    ),
                    sweep_error=error,
                )
            state.log_event("pvt_baseline_sweep", baseline_sweep)

    corner_state = None
    # 어떤 방식으로 씨앗을 뽑았는지(`corner_selection.seed_from_sweep`의 record).
    # 축소가 꺼진 분기에서는 이번 실행이 실제로 씨앗을 뽑지 않았으므로 None으로
    # 남는다 - result.json의 "seed"가 그 부재를 그대로 보여야 한다("seed": null과
    # "이번에 안 뽑았다"가 같은 사실). 재개된 실행이 코너 집합을 **다시 뽑지
    # 않는** 분기(checkpoint.corner_set이 있는 경우)는 씨앗을 새로 뽑지 않지만,
    # 그렇다고 None이 되는 것은 **아니다** - 체크포인트가 담아 온
    # `checkpoint.corner_seed`를 그대로 물려받는다(T2). 그러지 않으면 재개된
    # 실행의 seed가 영원히 null이 되어, 중단 없이 돈 실행과 재개된 실행이 같은
    # 사실("argmax를 골랐다")에 대해 다른 것("기록이 사라졌다")을 말하게 된다.
    seed_record: dict | None = None
    # 축소가 꺼졌으면 오늘의 simulate_fn 그대로 - nominal 한 점이다.
    simulate_for_run = simulate_fn
    if reduction_active and checkpoint is not None and checkpoint.corner_set is not None:
        # 씨앗을 **다시 뽑지 않는다.** 코너 집합은 attempt마다 자라므로
        # 진입 스윕에서 다시 씨앗을 뽑으면 재진입이 배운 코너가 통째로
        # 사라지고, 재개한 실행이 중단 없이 돈 실행보다 덜 보는 집합으로
        # 판정하게 된다. 뽑는 대신 체크포인트가 기록해 둔 것을 그대로 옮긴다 -
        # 다시 뽑을 스윕이 없고(진입 스윕은 재사용될 뿐이다), 뽑는다 해도 이
        # 지점에서는 재진입이 자라게 한 코너들이 이미 반영된 뒤라 원래 실행이
        # 뽑았던 값과 달라질 수 있다.
        # `last_judged_corners`도 같은 이유로 체크포인트에서 그대로 옮긴다(C1) -
        # 다시 뽑지 않으면 재개된 실행은 corner_sim이 처음 한 번 돌 때까지
        # judged=None이고, 재개 경계가 BOUNDARY_OPTIMIZATION이면 그 한 번조차
        # run_orchestration이 통째로 건너뛰어져 영영 안 찍힌다.
        corner_state = CornerState(
            checkpoint.corner_set, last_judged_corners=checkpoint.last_judged_corners
        )
        seed_record = checkpoint.corner_seed
        state.log_event(
            "corner_set_restored",
            {
                "corners": [label(c) for c in corner_state.corner_set.corners],
                "outside": len(corner_state.corner_set.probe_order),
                "probe_index": corner_state.corner_set.probe_index,
            },
        )
        simulate_for_run = build_corner_simulate(
            agent_simulate_fn, sim_backend, state, corner_state, state.log_event
        )
    elif reduction_active:
        try:
            seed_cs, seed_record = seed_from_sweep(baseline_sweep, spec)
            state.log_event("corner_seed", seed_record)
            corner_state = CornerState(seed_cs)
        except ValueError as exc:
            # 씨앗을 못 뽑으면 축소를 시작할 수 없다. 넷리스트 적용 경로의
            # ValueError와 같은 취급 - 크래시가 아니라 **깨끗한 FAIL**로 끝낸다.
            # 그래야 result.json과 report.md가 쓰이고 사유가 남는다.
            # (오늘 도달하지 않는다: _as_point가 거부하는 (deck) 항목을
            # run_full_pvt_sweep은 만들지 않는다. corner_sim의 corner_worst는
            # 만든다 - 그것이 이 자리로 흘러드는 날의 벽이다.)
            reason = f"{type(exc).__name__}: {exc}"
            state.log_event("corner_set_seed_failed", {"reason": reason})
            # 결과 모양은 _early_fail_result 한 곳에서만 만든다(그 독스트링이
            # 근거를 적는다 - 이 자리의 topology_swaps 누락이 그 사고였다).
            # sweep_error는 None이다: 스윕은 멀쩡히 돌았고 못 한 것은 씨앗
            # 뽑기이므로, 여기에 스윕 실패를 적으면 없는 사실을 적는 것이 된다.
            return _early_fail_result(
                run_dir=run_dir,
                netlist_paths=state.current_netlist_paths(),
                resumed_from=resumed_from,
                topology_swaps=all_topology_swaps,
                attempts=checkpoint.attempt if checkpoint is not None else 0,
                grown=checkpoint.grown_labels if checkpoint is not None else None,
                promotion_reentries=(
                    checkpoint.promotion_reentries if checkpoint is not None else None
                ),
                failure_reason=f"could not seed the mid-loop corner set: {reason}",
                reduction_reason=f"seeding the corner set from the entry sweep failed: {reason}",
            )
        seeded_event = {
            "corners": [label(c) for c in corner_state.corner_set.corners],
            "outside": len(corner_state.corner_set.probe_order),
        }
        # **argmax 모드에서만 by_criterion을 적는다.** argmax의 선택 집합은
        # `worst_case_corners`의 상(image) 그 자체이므로 이 매핑은 참인 진술이다.
        # coverage 모드의 선택 집합은 탐욕 피복이 고르므로 이 매핑이 가리키는
        # 코너가 선택 집합 **밖**에 있을 수 있다 - 측정된 사례(review finding #3)
        # 에서 corners=['(deck)', 'ss/1.62/125.0']인데 by_criterion이
        # {'gain': 'fs/...'}를 적어, 집합에 없는 fs가 gain이 여기 있는 이유인
        # 것처럼 읽혔다. `OPAMP2STAGE drives vdd,vss`와 같은 모양의 거짓 구조
        # 주장이라, 적을 수 없을 때는 키를 아예 비운다 - 부재가 정직한 신호다.
        if reduction.coverage is None:
            seeded_event["by_criterion"] = {
                name: raw_label(raw)
                for name, raw in baseline_sweep.get("worst_case_corners", {}).items()
            }
        state.log_event("corner_set_seeded", seeded_event)
        # **같은 콜러블이 오케스트레이터와 최적화기 양쪽에 간다.** 회전 탐침과
        # 탐침 승격이 사는 상자(CornerState)는 하나여야 한다 - 배선을 한 곳만
        # 바꾸면 회전이 갈라지고, 최적화 탐색은 메인 루프가 배운 코너를 보지
        # 못한 채 nominal 기준의 여유분을 요구하게 된다.
        simulate_for_run = build_corner_simulate(
            agent_simulate_fn, sim_backend, state, corner_state, state.log_event
        )
    else:
        state.log_event("corner_reduction_inactive", {"reason": reduction_reason})

    agents = OrchestratorAgents(
        simulate=simulate_for_run,
        judge=judge_fn,
        tune=tune_fn,
        verify_pre=verify_pre_fn,
        verify_post=verify_post_fn,
        propose_topology=propose_topology_fn,
    )

    # 판정 스윕이 실패하면 그 실패가 지목한 코너를 중간 루프의 집합에 영구히
    # 더하고 **수렴된 덱 그대로** 루프를 다시 돈다. 재진입마다
    # MAX_OUTER_ITERATIONS 예산은 새로 받는다(run_orchestration이 호출마다
    # 0에서 세므로 자동이다) - 예산이 소진된 채로 재진입하면 아무 일도 일어나지
    # 않기 때문이다. 그 대가로 최악의 경우 비용은
    # `(R+1) x MAX_OUTER_ITERATIONS x 반복당 비용`이 된다(R = retry_budget).
    #
    # 최적화와 판정 스윕은 이 루프 **안에** 남는다. 최적화기는 이미 "진입
    # 스윕이 실패한 설계는 최적화하지 않는다"는 규칙이 있어 실패한 시도에서는
    # 그 스윕 하나만 돌고 즉시 돌아오므로, 실패한 시도의 추가 비용은 어차피
    # 필요했던 판정 스윕 하나뿐이다. 루프 밖에 두면 판정 스윕이 하나 더 든다.
    #
    # **재진입마다 면적 게이트의 기준선이 다시 잡힌다 - 알고 하는 것이다.**
    # orchestrator.py:69-74는 `index_baseline_components`를 자기가 **받은**
    # 넷리스트에서 호출마다 한 번 계산한다. 여기서 넘기는 것이 수렴된 덱이므로,
    # 재진입한 루프의 성장 한도는 실행이 시작한 덱이 아니라 **직전 시도가 끝난
    # 덱**을 기준으로 잰다. 결과적으로 retry_budget=R이면 한 소자가 원래 덱에
    # 대해 허용받는 성장은 `tier^(R+1)`이다 - 기본값(R=2, 1.5x 티어)에서
    # 1.5^3 = 3.375배. 이 하위 프로젝트의 전제가 "run_orchestration을 고치지
    # 않는다"이므로 기준선을 고정하는 것은 별도 작업이고, 여기서는 **보이게**
    # 한다: 이 저장소에서 게이트가 조용히 안 걸린 것이 네 번이고 네 번 다 실행
    # 로그에 안 보였다. 재진입 시점마다 corner_set_grown에 남기고, 실행이 쓴
    # 기준선 개수를 result["corner_reduction"]["area_baselines"]에 싣는다.
    retry_budget = reduction.retry_budget if reduction_active else 0
    attempt = checkpoint.attempt if checkpoint is not None else 0
    grown_labels: list[list[str]] = (
        [list(g) for g in checkpoint.grown_labels] if checkpoint is not None else []
    )
    # M10(T19): 탐침 승격 재진입의 attempt별 기록. `grown_labels`와 나란히
    # 쌓이지만 다른 사실이다 - 승격 재진입 attempt는 `grown_labels`에 빈
    # 리스트를 밀어 `len(grown) == attempts` 불변식을 지키는데, 그 사실만으로는
    # "성장 없이 그냥 안 재진입함"과 "성장은 없지만 새 코너를 판정하러
    # 재진입함"을 구별할 수 없다. checkpoint.py의 grown_labels/corner_seed와
    # 같은 규칙으로 재개 시 복원한다.
    promotion_reentries: list[dict] = (
        [dict(p) for p in checkpoint.promotion_reentries] if checkpoint is not None else []
    )
    path_disagreement: dict | None = None
    unattributed_failures: dict | None = None
    reentry_skipped: dict | None = None
    final_sweep: dict | None = None
    # **돌지 못한** 판정 스윕의 기록. 루프 밖에서 미리 만드는 것은 결과에
    # **무조건** 실리기 때문이다 - "스윕이 멀쩡히 돌았다"와 "그 기록이
    # 사라졌다"가 같은 부재면 안 된다(optimize_guard_infeasible이 치른 값).
    sweep_error: dict | None = None
    # **스왑 레코드만은 시도를 가로질러 누적한다.** 아래 루프는 시도마다
    # run_orchestration을 새로 부르고 그 결과로 result를 통째로 덮는데,
    # 재진입은 **수렴된 덱에서 시작하므로**(아래 state.current_netlist_texts())
    # 앞선 시도가 유지한 스왑은 지금 돌려주는 덱에 구조로 그대로 살아 있다.
    # 누적하지 않으면 result.json이 "스왑 없음"이라고 말하면서
    # final_netlist_paths는 본문이 통째로 교체된 덱을 가리킨다 - I-3이 금지한
    # 바로 그 어긋남이다. 실측: attempt 0이
    # [AMP <- miller_nulling_resistor, kept]를 내고 attempt 1이 []를 냈는데
    # 덱의 AMP 본문에는 여전히 Rz가 있었다.
    #
    # 이것이 iterations_used/final_criteria와 다른 이유: 그 둘은 **그 시도의
    # 덱**을 옳게 설명하고(그리고 시도별이라는 사실이
    # corner_reduction.attempts로 공개된다), topology_swaps는 **이어서 넘겨지는
    # 덱의 구조**를 설명한다. 누적 덱에 시도별 기록을 붙이는 것이 어긋남이다.
    #
    # **정의는 위로 올라갔다** - 코너 시드 실패의 이른 반환이 이 값을 실어야
    # 하기 때문이다(위 그 자리의 주석 참조).

    def _save(boundary: str, *, progress=None, orchestration_result=None) -> None:
        """경계 하나에서 체크포인트를 원자적으로 갈아 끼운다.

        cli 쪽 상태(attempt, 누적 스왑, 코너 집합, 성장 이력, 코너 씨앗 기록)와
        run_orchestration이 넘겨 준 루프 상태를 한 파일에 함께 담는다 - 재개는
        둘 다 있어야 성립하고, 두 파일로 나누면 그 둘이 어긋날 자리가 생긴다.
        `corner_seed`는 `seed_record`를 그대로 옮긴다 - 이번 실행이 새로 뽑았든
        (elif 분기) 앞선 실행의 체크포인트에서 물려받았든(if 분기, 위 T2 주석)
        같은 변수이므로 다음 경계까지 그대로 이어진다.
        `last_judged_corners`도 `corner_set`과 같은 곳(`corner_state`)에서 매번
        다시 읽는다 - C1: 담지 않으면 재개된 실행은 판정자가 마지막으로 실제
        본 코너 집합을 잃고, 특히 BOUNDARY_OPTIMIZATION에서 재개하면
        run_orchestration을 통째로 건너뛰어 다시 찍을 기회도 없다.
        """
        write_checkpoint(
            run_dir,
            build_checkpoint(
                boundary=boundary,
                spec_path=args.spec,
                spec=spec,
                netlist_versions=state.netlist_versions,
                history_lines=line_count(state.history_path),
                attempt=attempt,
                all_topology_swaps=all_topology_swaps,
                corner_set=corner_state.corner_set if corner_state is not None else None,
                grown_labels=grown_labels,
                promotion_reentries=promotion_reentries,
                corner_seed=seed_record,
                last_judged_corners=(
                    corner_state.last_judged_corners if corner_state is not None else None
                ),
                progress=progress,
                orchestration_result=orchestration_result,
            ),
        )

    # 경계 1(outer iteration)과 경계 3(최적화 진입)에서 재개하면 그 시도의
    # run_orchestration 호출만 달라진다. 경계 2(attempt 시작)는 아무것도 담을
    # 것이 없다 - 그 지점의 상태가 곧 아래 루프 머리의 상태다.
    resume_progress = (
        checkpoint.progress if checkpoint is not None and checkpoint.boundary == BOUNDARY_OUTER_ITERATION else None
    )
    resume_result = (
        checkpoint.orchestration_result
        if checkpoint is not None and checkpoint.boundary == BOUNDARY_OPTIMIZATION
        else None
    )

    while True:
        if resume_result is not None:
            # 경계 3에서 재개했다. 메인 루프는 이미 끝났고 그 결과(누적 스왑까지
            # 합쳐진 것)가 체크포인트에 있다 - 다시 돌리면 이미 통과한 루프를
            # 통째로 다시 치른다. **최적화 단계는 여기서부터 처음부터 돈다**:
            # 자체 버전 스택과 이분 탐색을 갖고 있어 중간 재개가 그 불변식을
            # 다시 논증하게 만들기 때문이다.
            result = resume_result
            orch_status = result["status"]
            resume_result = None
            fresh_orchestration = False
        else:
            fresh_orchestration = True
            result = await run_orchestration(
                # 재진입은 **수렴된 덱에서 시작한다** - 롤백하지 않는다. 되돌리면
                # 방금 루프가 해낸 튜닝을 통째로 버리고 같은 실패를 다시 찾게 된다.
                # (최적화 배선이 같은 이유로 같은 값을 넘긴다 - 아래 주석 참조.)
                _entry_texts(resume_progress, initial_netlist_texts, attempt, state),
                spec,
                state,
                agents,
                resume=resume_progress,
                save_checkpoint=lambda progress: _save(
                    BOUNDARY_OUTER_ITERATION, progress=progress
                ),
            )
            # 재개 상태는 **한 번만** 쓴다. 남겨 두면 다음 attempt가 지난
            # 시도의 이터레이션 번호에서 시작한다.
            resume_progress = None

        # **이 시도의 오케스트레이션 결과는 여기서만 온전하다.** 아래에서
        # result["status"]는 FAIL로 덮이고 failure_reason에는 스윕 사유가
        # 덧붙는다. _final_result는 history.jsonl에 아무것도 쓰지 않으므로,
        # 여기서 남기지 않으면 attempt 0의 "agent execution error: rate limited"는
        # **어디에도** 흔적이 남지 않는다 - 재진입이 붙으면서 버려지는 결과가
        # 하나에서 최대 세 개로 늘었다.
        #
        # 경계 3에서 재개했으면 이 이벤트도 누적도 이미 끝나 있다. 다시 하면
        # 이력에 같은 시도가 두 번 적히고 스왑이 두 번 누적된다 - 버려진
        # 이벤트의 이중 계수와 같은 부류의 결함이다.
        if fresh_orchestration:
            orch_status = result["status"]
            state.log_event(
                "orchestration_attempt",
                {
                    "attempt": attempt,
                    "status": orch_status,
                    "iterations_used": result.get("iterations_used"),
                    "failure_reason": result.get("failure_reason"),
                },
            )
            # `attempt`를 함께 박는다. `outer_iter`는 시도마다 1부터 다시 세고
            # `tried_topologies`도 시도마다 리셋되므로, 한 블록이 다른 시도에서
            # 정당하게 다시 스왑될 수 있다 - 그러면 누적 목록에 같은 블록·같은
            # outer_iter의 레코드가 둘 생기고 시도 번호 없이는 구별되지 않는다.
            all_topology_swaps += [
                {"attempt": attempt, **swap} for swap in result.get("topology_swaps", [])
            ]
            result["topology_swaps"] = list(all_topology_swaps)

            # **경계 3.** 메인 루프가 끝났고 최적화 단계는 아직 시작하지 않았다.
            # 여기서 죽으면 재개는 메인 루프를 통째로 건너뛰고 최적화부터 다시
            # 돈다 - two_stage_opamp 기준 최대 103분을 아끼는 자리다.
            _save(BOUNDARY_OPTIMIZATION, orchestration_result=result)

        # 최적화는 PASS 뒤에만 의미가 있고(통과하지 못한 설계의 마진을 더 깎을
        # 이유가 없다), 최종 PVT 스윕 **앞에** 와야 한다 - 그 스윕이 최적화된
        # 넷리스트를 확정하는 역할을 그대로 하기 때문이다. 뒤에 두면 아무도
        # 확인하지 않은 넷리스트로 실행이 끝난다.
        if result["status"] == "PASS":
            # **탐색이 도는 동안 탐침 회전을 얼린다.** 상자는 계속 공유한다 -
            # 선택 집합이 갈라지면 탐색이 메인 루프가 배운 코너를 못 본 채
            # 여유분을 요구한다. 얼리는 것은 회전뿐이고, 이유는
            # CornerState.probe_frozen의 주석에 있다: 탐색 도중의 승격은
            # 서로 다른 코너 집합에서 잰 목적값을 비교하게 만들고, 그 뒤의 모든
            # 단계를 원인이 아닌 knob을 지목하는 사유로 거부시킨다.
            #
            # finally로 되돌리는 것은 재진입 때문이다 - 다음 시도의 메인 루프는
            # 다시 탐침을 돌려야 한다.
            if corner_state is not None:
                corner_state.probe_frozen = True
            try:
                optimization = await run_optimization(
                    # **실행의 현재 덱**이다 - 파일에서 읽은 원본이 아니다. 메인
                    # 루프가 고쳐 놓은 것을 최적화의 출발점으로 삼지 않으면,
                    # run_optimization이 인자와 state가 어긋난 것을 보고 원본을 새
                    # 버전으로 밀어 넣어 (optimizer.py:534) 튜닝 결과를 통째로
                    # 되돌린다 - 그리고 그 덱이 확정되고 보고된다.
                    state.current_netlist_texts(),
                    spec,
                    state,
                    OptimizerAgents(
                        propose=propose_candidates_fn,
                        simulate=simulate_for_run,
                        # 코너를 잴 수단이 없으면 None을 준다. run_optimization은
                        # 그때 확인이 없었다고 보고한다 - 빈 스윕을 지어내지 않는다.
                        verify_corners=verify_corners_fn if corner_capable else None,
                    ),
                )
            finally:
                if corner_state is not None:
                    corner_state.probe_frozen = False
            result["optimization"] = optimization
            # 최적화는 넷리스트 버전을 밀고 되돌린다. 실행이 내놓는 경로는 그것이
            # 착지한 버전이어야 한다.
            result["final_netlist_paths"] = state.current_netlist_paths()
            # ...그리고 기준 판정도 같은 버전의 것이어야 한다. result["final_criteria"]는
            # run_orchestration의 judge 결과라 최적화 **전** 덱을 설명한다. 경로만
            # 갱신하고 이것을 두면 리포트가 서로 다른 두 회로를 나란히 적는다 -
            # 실측 bandgap 실행에서 212.25uA를 재는 넷리스트 옆에 212.99uA가 적혔다.
            #
            # 최적화가 기준을 재지 못한 경로(기준선 시뮬레이션 실패, 이 단계 자체가
            # 터짐)에서는 None이 오고, 그때는 메인 루프의 판정을 그대로 둔다 -
            # 없는 값으로 덮으면 리포트가 통째로 빈다.
            if optimization.get("final_criteria"):
                result["final_criteria"] = optimization["final_criteria"]

        if not corner_capable:
            break

        # 최적화가 코너를 확인했으면 그 스윕이 곧 이 넷리스트의 최종 스윕이다.
        # 착지 지점은 정의상 스윕을 통과한 버전이므로(_bisect_last_passing은
        # 통과가 확인된 인덱스에만 착지한다) 다시 도는 것은 같은 덱에 같은
        # 값을 두 번 치르는 것이다 - bandgap 45 코너 기준 286초짜리다.
        #
        # 최적화의 pvt_sweep은 result["optimization"] 안에 그대로 남는다.
        # 두 결과가 같은 키 이름을 쓰므로 어느 쪽도 다른 쪽을 덮지 않도록
        # 최상위에만 대입한다.
        confirmed = (result.get("optimization") or {}).get("pvt_sweep")
        if confirmed is not None:
            final_sweep = confirmed
            sweep_label = "PVT sweep from the optimization phase"
            # 재사용해도 이력에는 같은 이름으로 남긴다. history.jsonl에서
            # pvt_final_sweep을 찾는 사람이, 하필 스윕을 가장 많이 돌린
            # 실행에서 빈손으로 돌아가면 안 된다. reused가 그것이 새로 돈
            # 스윕이 아님을 말한다.
            state.log_event("pvt_final_sweep", {"reused_from_optimization": True, **final_sweep})
        else:
            try:
                final_sweep = run_full_pvt_sweep(
                    state.current_netlist_texts(), spec, sim_backend, log_event=state.log_event
                )
            except Exception as exc:   # noqa: BLE001 - 근거는 진입 스윕 쪽 주석
                # **가장 비싼 자리다.** 여기까지 오면 튜닝 루프가 이미 끝났고
                # (two_stage_opamp 기준 103분) 최적화까지 돌았는데, 가드가
                # 없으면 run-dir에 history.jsonl과 netlist_v*.cir만 남는다.
                #
                # 상태는 **FAIL**이다. 판정을 내리는 것이 이 스윕이므로, 돌지
                # 못한 스윕을 통과로 읽는 것은 아무도 확인하지 않은 넷리스트를
                # PASS로 출하하는 것이다. 그리고 재진입하지 않는다 - 성장시킬
                # 실패 코너가 없다(스윕이 아무 값도 내지 않았다). 이것은
                # corner_path_disagreement와 같은 판단이다: 같은 정보로 다시
                # 도는 것은 진단이 아니라 낭비다.
                sweep_error = _sweep_error("verdict", exc)
                state.log_event("pvt_sweep_failed", sweep_error)
                result["status"] = "FAIL"
                # '스윕이 실패했다'와 '스윕이 돌지 못했다'는 다른 사실이다.
                # None을 싣는 것은 "돌려 했는데 실을 값이 없다"이고, 키의
                # 부재("이 스펙에는 판정 스윕이 없다")와도 구별된다.
                result["pvt_sweep"] = None
                sweep_reason = f"final PVT sweep could not run: {sweep_error['error']}"
                prior = result.get("failure_reason")
                result["failure_reason"] = f"{prior}; {sweep_reason}" if prior else sweep_reason
                break
            sweep_label = "final PVT sweep"
            state.log_event("pvt_final_sweep", {"reused_from_optimization": False, **final_sweep})
        result["pvt_sweep"] = final_sweep

        if final_sweep["overall_pass"]:
            break

        # 재사용 경로에서 이 판정이 FAIL로 뒤집히는 일은 사실상 없다(착지
        # 지점은 통과한 버전이다). 그래도 조건을 걸어 두는 것은, 최적화가
        # **진입** 스윕에서 이미 실패한 코너를 그대로 실어 보낼 수 있고 그때는
        # 최적화를 돌리지 않았을 때와 똑같이 FAIL이어야 하기 때문이다. 그
        # 경우 최종 스윕은 돌지 않았으므로 사유도 그렇게 적는다 - 돌지 않은
        # 스윕이 실패했다고 적으면 history.jsonl과 대조하는 사람이 헤맨다.
        result["status"] = "FAIL"
        # **덧붙인다 - 덮지 않는다.** 여기 이미 들어 있는 것은 run_orchestration이
        # 보고한 사유이고(예: "max iterations reached"), 그것이 곧 이 시도가
        # 어떻게 끝났는지다. 덮어쓰면 리포트를 읽는 사람에게 남는 것은 스윕이
        # 실패했다는 사실뿐이고, 그 앞에서 루프가 왜 멈췄는지는 사라진다.
        sweep_reason = f"{sweep_label} failed: {final_sweep['summary']}"
        prior = result.get("failure_reason")
        result["failure_reason"] = f"{prior}; {sweep_reason}" if prior else sweep_reason

        if not reduction_active or attempt >= retry_budget:
            break

        # **재진입은 수렴된 덱을 전제로 한다.** 설계 문서가 말하는 "수렴된 덱"은
        # PASS로 끝난 덱이다. 10 반복을 소진하고 멈춘 덱은 그것이 아니고,
        # "tuning proposal repeatedly rejected"는 이 저장소가 **일부러** 토폴로지
        # 교체까지 건너뛰며 하드 FAIL로 만든 결말이며, "agent execution error"는
        # 거의 확실히 재발한다. 셋 다 예산이 새로 채워진 완전한 튜닝 루프를 최대
        # retry_budget번 더 돌게 된다 - 코너를 하나 더 봤다고 달라지는 실패가
        # 아닌데도. 조용히 break하지 않고 사유를 남기는 것은, 재진입이 왜 안
        # 일어났는지가 attempts==0과 구별되지 않으면 이 하위 프로젝트의 "전부
        # 보인다"는 주장이 깨지기 때문이다.
        if orch_status != "PASS":
            skipped = {
                "attempt": attempt,
                "orchestration_status": orch_status,
                "orchestration_failure_reason": prior,
            }
            state.log_event("corner_reentry_skipped", skipped)
            reentry_skipped = skipped
            result["failure_reason"] += (
                f" - the corner set was not grown and the loop was not re-entered: "
                f"the tuning loop itself returned {orch_status} "
                f"({prior or 'no reason reported'}), so this run never converged; "
                f"re-entry carries a converged deck forward and there is none"
            )
            break

        failing_names = [
            entry["name"] for entry in final_sweep.get("criteria", []) if not entry.get("pass")
        ]
        worst = final_sweep.get("worst_case_corners", {})
        try:
            grown, added = grown_with(corner_state.corner_set, final_sweep, failing_names)
        except ValueError as exc:
            # 넷리스트 적용 경로의 ValueError와 같은 취급 - 크래시가 아니라
            # 깨끗한 FAIL이다. 판정 스윕이 이미 실패했으므로 status는 그대로
            # 두고 사유만 덧붙인다.
            reason = f"{type(exc).__name__}: {exc}"
            state.log_event("corner_set_growth_failed", {"reason": reason})
            result["failure_reason"] += f" - the corner set could not be grown: {reason}"
            break

        if not added:
            # 여기서 두 사실이 갈린다. 실패한 기준에 **최악 코너가 붙어 있는가**로
            # 나뉘며, 둘을 한 문장으로 뭉개면 데이터가 뒷받침하지 않는 구조적
            # 주장을 하게 된다.
            attributed = [(name, raw_label(worst[name])) for name in failing_names if name in worst]
            if attributed:
                # **"지금 집합 안"과 "마지막으로 판정한 시점의 집합 안"은 다른
                # 사실이다.** 탐침 승격은 그 코너를 *다음* 이터레이션을 위해
                # corner_set에 넣지만, 그 이터레이션의 판정자는 승격 **이전**
                # 집합으로 판정한다(corner_sim.build_corner_simulate). 그래서
                # 마지막 판정 이후에 승격된 코너는 지금 집합에는 있어도 아직
                # 한 번도 판정된 적이 없다 - 거기서 실패가 나는 것은 두 경로가
                # 의견이 갈린 것이 아니라, 중간 루프가 그 코너를 판정 대상에
                # 넣은 적이 없다는 것뿐이다.
                #
                # `last_judged_corners`가 None이면(corner_sim이 이 실행에서 아직
                # 한 번도 안 돌았다는 뜻 - 축소가 꺼진 스펙, 또는 옛 체크포인트를
                # 읽은 재개처럼 스냅샷 자체가 없는 경우) **재시도하지 않는 것은
                # 그대로 안전하지만, "마지막으로 판정한 시점에 이미 집합 안에
                # 있었다"고 주장할 근거는 없다.** 그 코너가 판정된 적이 있는지
                # 자체를 모르기 때문이다 - "판정됐다"와 "안 됐다" 둘 다 데이터가
                # 없는데 전자로 단정하면 `OPAMP2STAGE drives vdd,vss`와 같은
                # 모양의 근거 없는 구조적 주장이 된다. 아래에서 `judged is None`과
                # `judged is not None`을 갈라 문구를 다르게 쓰는 이유가 이것이다 -
                # 재시도하지 않는다는 **결론**은 같아도(둘 다 안전한 방향), 그
                # 결론을 뒷받침하는 근거는 다르다.
                judged = corner_state.last_judged_corners
                stale = [
                    (name, corner) for name, corner in attributed
                    if judged is not None and corner not in judged
                ]
                if stale:
                    # (b) **탐침 승격 재진입.** corner_set은 이미 그 코너를
                    # 담고 있으므로 grown_with가 더할 것은 없지만, 그 코너는
                    # 아직 판정된 적이 없는 새 정보다 - retry_budget 안에서
                    # 재진입한다. 다음 라운드에는 그 코너가 실제로 판정
                    # 대상이므로 (a) 경로 불일치이거나 수렴, 둘 중 하나다.
                    promotion_reentry = {
                        "criteria": [name for name, _ in stale],
                        "corners": [corner for _, corner in stale],
                    }
                    state.log_event("corner_probe_promotion_reentry", promotion_reentry)
                    pairs = ", ".join(f"{name} at {corner}" for name, corner in stale)
                    result["failure_reason"] += (
                        f" - {pairs} entered the mid-loop corner set via probe "
                        f"promotion after the judge last saw it; this is new "
                        f"information, not a repeated disagreement, so the run "
                        f"re-enters with the corner now judged"
                    )
                    attempt += 1
                    # M10(T19): `grown_labels`와 나란히, 같은 길이로 쌓는다 -
                    # `len(grown) == attempts` 불변식을 지켜야 `grown[i]`가
                    # attempt `i+1`의 것이라고 읽을 수 있다. 이 attempt는 코너를
                    # 하나도 더하지 않았으므로 빈 리스트를 민다 - "무엇을
                    # 더했는가"와 "왜 재진입했는가"는 다른 질문이고, 후자는
                    # promotion_reentries가 답한다.
                    grown_labels.append([])
                    # `promotion_reentry`(바로 위, history 이벤트로 이미 나간
                    # 것)와 같은 `criteria`/`corners`를 **다시 만들지 않는다** -
                    # M4(T19 리뷰): 독립된 리스트 컴프리헨션 두 벌은 한쪽만
                    # 고치면 history.jsonl과 result.json이 조용히 갈라지는
                    # 자리다. `attempt`만 덧붙인다.
                    promotion_reentries.append({"attempt": attempt, **promotion_reentry})
                    state.log_event(
                        "corner_set_grown",
                        {
                            "attempt": attempt,
                            "added": [],
                            "failing_criteria": failing_names,
                            "size": len(corner_state.corner_set.corners),
                            "area_baseline_reanchored": True,
                            "area_baselines_so_far": attempt + 1,
                            "reason": "probe_promotion",
                        },
                    )
                    _save(BOUNDARY_ATTEMPT)
                    continue
                # (a) **경로 불일치.** 실패한 코너가 전부 마지막으로 판정한
                # 시점에도 이미 집합 안에 있었다면, 두 실행 경로가 같은 덱의
                # 같은 코너를 두고 서로 다른 말을 하고 있는 것이다. 재시도해
                # 봐야 같은 정보로 같은 결과를 낼 뿐이니 무한 루프가 될 자리를
                # 진단으로 바꾼다.
                path_disagreement = {
                    "criteria": [name for name, _ in attributed],
                    "corners": [corner for _, corner in attributed],
                }
                # `judged_snapshot_available`은 이 이벤트에만 실린다 -
                # `result["corner_reduction"]["path_disagreement"]`은
                # `path_disagreement` 변수를 그대로 쓰므로 그 모양은 건드리지
                # 않는다(기존 소비자·테스트가 `{"criteria", "corners"}` 두 키만
                # 기대한다). 대신 history.jsonl 쪽에서 "진짜 불일치를 봤다"와
                # "스냅샷이 없어 안전한 방향으로 접었다"를 구별할 수 있게 한다.
                state.log_event(
                    "corner_path_disagreement",
                    {**path_disagreement, "judged_snapshot_available": judged is not None},
                )
                pairs = ", ".join(f"{name} at {corner}" for name, corner in attributed)
                if judged is not None:
                    result["failure_reason"] += (
                        f" - path disagreement: every failing corner was already in the "
                        f"mid-loop corner set at the time it was last judged ({pairs}), "
                        f"so the mid-loop and the verdict sweep judged the same deck at "
                        f"the same corner differently; retrying would re-run identical "
                        f"information"
                    )
                else:
                    # **주장을 낮춘다.** 스냅샷이 없으므로 "마지막으로 판정한
                    # 시점에 이미 집합 안에 있었다"를 확인할 수 없다 - 아는 것은
                    # "지금 집합 안에 있다"뿐이다. 재시도하지 않는 결론은 여전히
                    # 안전한 방향이므로 바꾸지 않는다(모르는 상태에서 재시도해도
                    # 근거가 늘지 않는다) - 하지만 그 결론을 "두 경로가 실제로
                    # 다른 말을 했다"는 확인된 사실인 것처럼 적지 않는다.
                    result["failure_reason"] += (
                        f" - path disagreement (unconfirmed): every failing corner is "
                        f"already in the mid-loop corner set ({pairs}), but this run has "
                        f"no last-judged snapshot to compare against, so whether the "
                        f"mid-loop actually judged that corner before is unknown rather "
                        f"than confirmed; folding this into a path disagreement (no "
                        f"retry) is the safe fallback, not a claim that the two paths "
                        f"were observed to disagree"
                    )
            else:
                # 실패한 기준 어느 것에도 최악 코너가 붙지 않았다.
                # worst_case_measurements는 어떤 코너에서도 측정값이 나오지 않은
                # 기준을 worst_case_corners에서 **통째로 뺀다** - "회로가 어디서도
                # 동작하지 않는다"는 경우다. 그것을 경로 불일치라고 적으면 두 실행
                # 경로에 대해 데이터가 뒷받침하지 않는 주장을 하는 것이 된다
                # (구조 쪽의 `OPAMP2STAGE drives vdd,vss`와 같은 모양의 오류).
                # 재진입하지 않는 것은 양쪽 다 옳다 - 더할 코너가 없다는 사실은
                # 같기 때문이다. 달라지는 것은 진단뿐이다.
                unattributed_failures = {"criteria": failing_names}
                state.log_event("corner_unattributed_failure", unattributed_failures)
                result["failure_reason"] += (
                    f" - no corner could be attributed to the failing criteria "
                    f"({', '.join(failing_names) or 'none reported'}): the verdict "
                    f"sweep produced no measurement for them at any corner, so there "
                    f"is nothing to add to the mid-loop corner set"
                )
            break

        corner_state.corner_set = grown
        added_labels = [label(point) for point in added]
        grown_labels.append(added_labels)
        attempt += 1
        state.log_event(
            "corner_set_grown",
            {
                "attempt": attempt,
                "added": added_labels,
                "failing_criteria": failing_names,
                "size": len(grown.corners),
                # 이 재진입은 면적 게이트의 기준선을 다시 잡는다(루프 머리 주석).
                # 재진입 순간에 이력에 남겨 두어야, 나중에 소자가 왜 원래 덱의
                # 티어를 넘어 커져 있는지를 실행 로그에서 되짚을 수 있다.
                "area_baseline_reanchored": True,
                "area_baselines_so_far": attempt + 1,
            },
        )
        # **경계 2.** 다음 attempt는 아직 아무것도 하지 않았고, 그 시작 상태가
        # 지금 이 지점이다. 여기서 재개하면 run_orchestration을 평범하게(재개
        # 없이) 부르므로 중단 없이 돈 실행과 같은 버전을 민다.
        _save(BOUNDARY_ATTEMPT)

    # argmax 이동량은 **판정에 아무 영향을 주지 않는다** - 순수한 기록이다.
    # 진입 스윕과 판정 스윕이 둘 다 있을 때만 잴 수 있으므로, 코너를 못 재는
    # 스펙에서는 빈 기록이 된다.
    drift = _no_drift()
    if baseline_sweep is not None and final_sweep is not None:
        drift = _argmax_drift(baseline_sweep, final_sweep)
        state.log_event("corner_argmax_drift", drift)

    # **재개하지 않은 실행에도 항상 실린다(null).** "재개 안 함"과 "필드가
    # 사라짐"이 같은 모양이면 안 된다 - topology_swaps가 항상 실리는 것과 같은
    # 이유이고, 부분 런이 온전한 런처럼 측정 데이터에 들어간 것이 D1 측정을
    # 무효로 만든 원인의 절반이었다. 재개 여부가 결과에서 안 보이면 이 기능은
    # 측정 장치를 고치는 게 아니라 망가뜨린다.
    result["resumed_from"] = resumed_from

    # **스윕이 멀쩡히 돈 실행에도 null로 실린다.** 실패할 때만 적으면 "돌았고
    # 멀쩡했다"와 "이 실행은 그 기록을 아예 안 쓴다"가 같은 부재가 된다 -
    # 이 저장소가 게이트에 대해 아홉 번 치른 값이고, optimize_guard_infeasible이
    # 무조건 로깅되는 것과 같은 규칙이다.
    result["pvt_sweep_error"] = sweep_error

    result["corner_reduction"] = {
        "active": reduction_active,
        "reason": reduction_reason,
        "final_set": [label(c) for c in corner_state.corner_set.corners] if corner_state else [],
        "attempts": attempt,
        # 면적 게이트가 이 실행에서 쓴 기준선의 **개수**. 1보다 크면 성장 한도가
        # 실행 시작 덱이 아니라 중간 덱들에 대해 다시 잡혔다는 뜻이다(루프 머리
        # 주석의 tier^(R+1)). 조용히 그러는 것이 이 저장소의 반복된 실패 모양이라
        # 결과에 싣는다.
        "area_baselines": attempt + 1,
        "grown": grown_labels,
        # M10(T19): 탐침 승격 재진입의 attempt별 기록. **무조건** 실린다(승격
        # 재진입이 없었으면 빈 리스트) - "없었다"와 "이 필드가 사라졌다"가 같은
        # 부재이면 안 된다는 이 저장소의 규칙(topology_swaps/resumed_from과
        # 같다). `grown`이 같은 attempt에서 빈 리스트를 신는 것만으로는 "성장
        # 없이 안 재진입"과 "성장 없이 승격 판정을 위해 재진입"을 구별할 수
        # 없어서 별도로 싣는다.
        "promotion_reentries": promotion_reentries,
        "path_disagreement": path_disagreement,
        "unattributed_failures": unattributed_failures,
        # 재진입을 건너뛴 **이유**. path_disagreement/unattributed_failures와
        # 같은 부류의 사실이라 같은 자리에 싣는다 - attempts==0만 보고 "성장할
        # 코너가 없었다"로 읽으면 틀린다.
        "reentry_skipped": reentry_skipped,
        "argmax_drift": drift,
        # **어떤 방식으로 씨앗을 뽑았는지, 결과에서도 보여야 한다.** 지금까지
        # `corner_seed`는 history.jsonl에만 남았고 result.json/report.md 두
        # 산출물만 보는 사람은 argmax와 ε-coverage 중 무엇이 돌았는지 알 수
        # 없었다 - "결과는 자기가 돌려주는 덱을 설명해야 한다"는 이 저장소의
        # 다섯 번째 반복. 재개된 실행이 이번 회차에 씨앗을 다시 뽑지 않았거나
        # 축소 자체가 꺼졌으면 None이다(위 seed_record 주석 참조) - 뽑지 않은
        # 것을 지어내지 않는다.
        "seed": seed_record,
    }

    return result


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        result = asyncio.run(_run(args))
    except CheckpointRejected as exc:
        # 거부는 크래시가 아니라 **무엇이 왜 어긋났는지 말하는 오류**다.
        # result.json도 report.md도 쓰지 않는다 - 실행이 시작조차 하지 않았고,
        # 시작하지 않은 실행의 산출물을 남기면 그것이 측정에 들어간다.
        print(f"--resume 거부: {exc}", file=sys.stderr)
        sys.exit(2)

    run_dir = result["run_dir"]
    write_result_json(run_dir, result)
    write_report_md(run_dir, result)

    print(f"Status: {result['status']}")
    sys.exit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
