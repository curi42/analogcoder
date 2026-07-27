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

    agents = OrchestratorAgents(
        simulate=simulate_fn,
        judge=judge_fn,
        tune=tune_fn,
        verify_pre=verify_pre_fn,
        verify_post=verify_post_fn,
        propose_topology=propose_topology_fn,
    )

    # "코너를 잴 수 있는가"를 한 번만 적는다. 세 군데에 같은 조건을 따로 쓰면
    # 한 곳만 바뀌었을 때 baseline은 재는데 최적화에는 None이 가는 식으로
    # 조용히 어긋난다. optimizer.py도 같은 이름(corner_capable)을 쓴다.
    corner_capable = spec.pvt_corners is not None

    if corner_capable:
        baseline_sweep = run_full_pvt_sweep(initial_netlist_texts, spec, sim_backend)
        state.log_event("pvt_baseline_sweep", baseline_sweep)

    result = await run_orchestration(initial_netlist_texts, spec, state, agents)

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
                simulate=simulate_fn,
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

    if corner_capable:
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
        # 재사용 경로에서 이 판정이 FAIL로 뒤집히는 일은 사실상 없다(착지
        # 지점은 통과한 버전이다). 그래도 조건을 걸어 두는 것은, 최적화가
        # **진입** 스윕에서 이미 실패한 코너를 그대로 실어 보낼 수 있고 그때는
        # 최적화를 돌리지 않았을 때와 똑같이 FAIL이어야 하기 때문이다. 그
        # 경우 최종 스윕은 돌지 않았으므로 사유도 그렇게 적는다 - 돌지 않은
        # 스윕이 실패했다고 적으면 history.jsonl과 대조하는 사람이 헤맨다.
        if not final_sweep["overall_pass"]:
            result["status"] = "FAIL"
            result["failure_reason"] = f"{sweep_label} failed: {final_sweep['summary']}"

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
