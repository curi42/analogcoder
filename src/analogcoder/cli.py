import argparse
import asyncio
import os
import sys
import uuid

from analogcoder.agents.backend import AgentBackend
from analogcoder.agents.backends.claude_sdk import DEFAULT_CLAUDE_MODEL, ClaudeSDKBackend
from analogcoder.agents.backends.openai_compatible import OpenAICompatibleBackend
from analogcoder.agents.judge import judge_measurements
from analogcoder.agents.optimizer import propose_candidates
from analogcoder.agents.simulator_agent import simulate as agent_simulate
from analogcoder.agents.tuner import propose_topology_swap, propose_tuning
from analogcoder.agents.verifier import verify_post, verify_pre
from analogcoder.corner_selection import grown_with, label, seed_from_sweep
from analogcoder.corner_sim import CornerState, build_corner_simulate
from analogcoder.netlist import resolve_includes
from analogcoder.optimizer import OptimizerAgents, run_optimization
from analogcoder.orchestrator import OrchestratorAgents, run_orchestration
from analogcoder.pvt import run_full_pvt_sweep
from analogcoder.report import write_report_md, write_result_json
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
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
    return parser


AGENT_NAMES = ("simulator", "judge", "tuner", "verifier", "optimizer")

_NO_DRIFT = {"criteria": [], "moved_count": 0, "total": 0}


def _corner_label(raw: dict | None) -> str | None:
    """worst_case_corners 항목 하나의 사람이 읽는 이름.

    corner_selection.label과 **같은 문자열**을 내야 한다 - 두 이름이 갈리면
    final_set과 argmax_drift를 나란히 놓고 읽을 수 없다. 렌더링을 거치지 않은
    덱은 pvt._corner_fields가 좌표 없이 적으므로, 그 부재로 판별한다(이름
    매칭이 아니다). corner_selection._as_point는 같은 모양을 **거부**하지만
    여기서는 적기만 한다 - argmax 계측은 순수한 기록이고, 기록이 실행을
    멈추게 할 수는 없다."""
    if raw is None:
        return None
    if raw.get("voltage") is None and raw.get("temperature") is None:
        return "(deck)"
    return f"{raw['process']}/{raw['voltage']}/{raw['temperature']}"


def _argmax_drift(entry_sweep: dict, verdict_sweep: dict) -> dict:
    """기준별 최악 코너가 진입 스윕과 판정 스윕 사이에서 움직였는가.

    **설계가 움직일 때 최악 코너가 얼마나 움직이는가**는 아직 아무도 재지
    않았다. 거의 안 움직이면 코너 지속성이 최적에 가깝고 어떤 적응형 기법도
    이기지 못한다; 많이 움직이면 지속성이 나쁘고 적응이 필요하다. 다음 축소
    기법을 고르는 근거가 이 숫자이며, 실행이 이미 만드는 데이터라 공짜다.

    **판정에는 아무 영향을 주지 않는다 - 순수한 기록이다.**

    한쪽 스윕에만 있는 기준은 moved=False다. 짝이 없으면 "움직였다"고 말할
    근거가 없고, 없는 것을 이동으로 세면 이 숫자의 유일한 용도가 망가진다."""
    entry_wc = entry_sweep.get("worst_case_corners", {})
    final_wc = verdict_sweep.get("worst_case_corners", {})
    names = list(entry_wc) + [name for name in final_wc if name not in entry_wc]

    criteria = []
    for name in names:
        entry_label = _corner_label(entry_wc.get(name))
        final_label = _corner_label(final_wc.get(name))
        criteria.append({
            "name": name,
            "entry": entry_label,
            "final": final_label,
            "moved": (
                entry_label is not None
                and final_label is not None
                and entry_label != final_label
            ),
        })

    return {
        "criteria": criteria,
        "moved_count": sum(1 for c in criteria if c["moved"]),
        "total": len(criteria),
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
    state = RunState(run_dir=run_dir, testbench_names=[tb.name for tb in spec.testbenches])
    sim_backend = NgspiceBackend()
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
        return await judge_measurements(measurements, spec_arg.all_criteria, agent_backends["judge"])

    async def tune_fn(structure_view, judge_result, history, rejection_feedback, netlist_text_arg):
        return await propose_tuning(
            structure_view, judge_result, history, rejection_feedback, netlist_text_arg, agent_backends["tuner"]
        )

    async def verify_pre_fn(structure_view, judge_result, proposal, netlist_text_arg):
        return await verify_pre(structure_view, judge_result, proposal, netlist_text_arg, agent_backends["verifier"])

    async def verify_post_fn(prev_judge_result, new_judge_result, applied_changes):
        return await verify_post(prev_judge_result, new_judge_result, applied_changes, agent_backends["verifier"])

    async def propose_topology_fn(structure_view, judge_result, available_topologies, rejection_feedback):
        return await propose_topology_swap(
            structure_view, judge_result, available_topologies, rejection_feedback, agent_backends["tuner"]
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
        return run_full_pvt_sweep(netlist_texts, spec, sim_backend)

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

    baseline_sweep = None
    if corner_capable:
        baseline_sweep = run_full_pvt_sweep(initial_netlist_texts, spec, sim_backend)
        state.log_event("pvt_baseline_sweep", baseline_sweep)

    corner_state = None
    # 축소가 꺼졌으면 오늘의 simulate_fn 그대로 - nominal 한 점이다.
    simulate_for_run = simulate_fn
    if reduction_active:
        corner_state = CornerState(seed_from_sweep(baseline_sweep, spec))
        state.log_event(
            "corner_set_seeded",
            {
                "corners": [label(c) for c in corner_state.corner_set.corners],
                "by_criterion": {
                    name: _corner_label(raw)
                    for name, raw in baseline_sweep.get("worst_case_corners", {}).items()
                },
                "outside": len(corner_state.corner_set.probe_order),
            },
        )
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
    retry_budget = reduction.retry_budget if reduction_active else 0
    attempt = 0
    grown_labels: list[list[str]] = []
    path_disagreement: dict | None = None
    final_sweep: dict | None = None

    while True:
        result = await run_orchestration(
            # 재진입은 **수렴된 덱에서 시작한다** - 롤백하지 않는다. 되돌리면
            # 방금 루프가 해낸 튜닝을 통째로 버리고 같은 실패를 다시 찾게 된다.
            # (최적화 배선이 같은 이유로 같은 값을 넘긴다 - 아래 주석 참조.)
            initial_netlist_texts if attempt == 0 else state.current_netlist_texts(),
            spec,
            state,
            agents,
        )

        # 최적화는 PASS 뒤에만 의미가 있고(통과하지 못한 설계의 마진을 더 깎을
        # 이유가 없다), 최종 PVT 스윕 **앞에** 와야 한다 - 그 스윕이 최적화된
        # 넷리스트를 확정하는 역할을 그대로 하기 때문이다. 뒤에 두면 아무도
        # 확인하지 않은 넷리스트로 실행이 끝난다.
        if result["status"] == "PASS":
            optimization = await run_optimization(
                # **실행의 현재 덱**이다 - 파일에서 읽은 원본이 아니다. 메인 루프가
                # 고쳐 놓은 것을 최적화의 출발점으로 삼지 않으면, run_optimization이
                # 인자와 state가 어긋난 것을 보고 원본을 새 버전으로 밀어 넣어
                # (optimizer.py:534) 튜닝 결과를 통째로 되돌린다 - 그리고 그 덱이
                # 확정되고 보고된다.
                state.current_netlist_texts(),
                spec,
                state,
                OptimizerAgents(
                    propose=propose_candidates_fn,
                    simulate=simulate_for_run,
                    # 코너를 잴 수단이 없으면 None을 준다. run_optimization은 그때
                    # 확인이 없었다고 보고한다 - 빈 스윕을 지어내지 않는다.
                    verify_corners=verify_corners_fn if corner_capable else None,
                ),
            )
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
            final_sweep = run_full_pvt_sweep(state.current_netlist_texts(), spec, sim_backend)
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
        result["failure_reason"] = f"{sweep_label} failed: {final_sweep['summary']}"

        if not reduction_active or attempt >= retry_budget:
            break

        failing_names = [
            entry["name"] for entry in final_sweep.get("criteria", []) if not entry.get("pass")
        ]
        grown, added = grown_with(corner_state.corner_set, final_sweep, failing_names)
        if not added:
            # **경로 불일치.** 실패한 코너가 전부 이미 중간 루프의 집합 안에
            # 있다면, 두 실행 경로가 같은 덱의 같은 코너를 두고 서로 다른 말을
            # 하고 있는 것이다. 재시도해 봐야 같은 정보로 같은 결과를 낼 뿐이니
            # 무한 루프가 될 자리를 진단으로 바꾼다 - 어느 기준이 어느 코너에서
            # 어긋났는지를 적는다.
            disagreeing = [
                _corner_label(final_sweep.get("worst_case_corners", {}).get(name))
                for name in failing_names
            ]
            path_disagreement = {
                "criteria": failing_names,
                "corners": [c for c in disagreeing if c is not None],
            }
            state.log_event("corner_path_disagreement", path_disagreement)
            pairs = ", ".join(
                f"{name} at {corner}"
                for name, corner in zip(failing_names, disagreeing)
                if corner is not None
            )
            result["failure_reason"] += (
                f" - path disagreement: every failing corner was already in the "
                f"mid-loop corner set ({pairs or 'no corner reported'}), so the "
                f"mid-loop and the verdict sweep judged the same deck at the same "
                f"corner differently; retrying would re-run identical information"
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
            },
        )

    # argmax 이동량은 **판정에 아무 영향을 주지 않는다** - 순수한 기록이다.
    # 진입 스윕과 판정 스윕이 둘 다 있을 때만 잴 수 있으므로, 코너를 못 재는
    # 스펙에서는 빈 기록이 된다.
    drift = dict(_NO_DRIFT)
    if baseline_sweep is not None and final_sweep is not None:
        drift = _argmax_drift(baseline_sweep, final_sweep)
        state.log_event("corner_argmax_drift", drift)

    result["corner_reduction"] = {
        "active": reduction_active,
        "reason": reduction_reason,
        "final_set": [label(c) for c in corner_state.corner_set.corners] if corner_state else [],
        "attempts": attempt,
        "grown": grown_labels,
        "path_disagreement": path_disagreement,
        "argmax_drift": drift,
    }

    return result


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    result = asyncio.run(_run(args))

    run_dir = result["run_dir"]
    write_result_json(run_dir, result)
    write_report_md(run_dir, result)

    print(f"Status: {result['status']}")
    sys.exit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
