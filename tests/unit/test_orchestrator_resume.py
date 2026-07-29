"""중단된 실행을 경계에서 이어 돌린다 - LLM도 ngspice도 부르지 않는다.

가짜 에이전트는 **호출 순서가 아니라 입력의 함수**여야 한다. 크래시한
이터레이션은 재개 후 통째로 다시 도는데, 순서 기반 가짜는 그때 다른 응답을
내놓아 "같은 최종 덱"을 재는 것이 아니라 가짜의 상태를 재는 것이 된다.
"""

import json
import re
from types import SimpleNamespace

import pytest

from analogcoder.checkpoint import (
    BOUNDARY_OUTER_ITERATION,
    Checkpoint,
    from_payload,
    restore_state,
    to_payload,
)
from analogcoder.history import line_count, read_events
from analogcoder.orchestrator import OrchestratorAgents, run_orchestration
from analogcoder.state import RunState

TB = "ac_loop_gain"
SPEC = SimpleNamespace(
    circuit_name="fake",
    testbenches=[SimpleNamespace(name=TB, criteria=[], control_block=".control\n.endc\n")],
        fragments=None)
SPEC.canonical = SPEC.testbenches[0]

PASS_AT = 14
# 이 값을 제안하면 verify_pre 가 거부한다 - 재시도 경로를 결정론적으로 만든다.
FORBIDDEN = "13"
# 이 값이 적용되면 verify_post 가 롤백한다.
BAD = "12"


class Boom(RuntimeError):
    """AgentExecutionError 도 ValueError 도 아니어야 한다 - 둘 중 하나면
    run_orchestration 이 깨끗한 FAIL 로 접어 버려서 '중간에 죽었다'가 되지
    않는다."""


def deck(value: int) -> str:
    return f"* netlist\nRf vminus vout {value}\n.end\n"


def deck_value(text: str) -> int:
    return int(re.search(r"^Rf vminus vout (\S+)", text, re.M).group(1))


def make_agents(crash: tuple[str, int] | None = None) -> OrchestratorAgents:
    """crash = (에이전트 이름, 몇 번째 호출)에서 Boom 을 던진다."""
    calls: dict[str, int] = {}

    def bump(name: str) -> None:
        calls[name] = calls.get(name, 0) + 1
        if crash is not None and crash[0] == name and calls[name] == crash[1]:
            raise Boom(f"{name} call #{calls[name]}")

    async def simulate(netlist_texts, spec):
        bump("simulate")
        return {
            "measurements": {"gain_db": float(deck_value(netlist_texts[TB]))},
            "status": "success",
            "warnings": [],
        }

    async def judge(measurements, spec):
        bump("judge")
        value = measurements["gain_db"]
        ok = value >= PASS_AT
        return {
            "overall_pass": ok,
            "criteria": [
                {
                    "name": "gain",
                    "target": f">={PASS_AT}",
                    "actual": value,
                    "pass": ok,
                    "margin": value - PASS_AT,
                }
            ],
        }

    async def tune(structure_view, judge_result, attempts_view, rejection_feedback, netlist_view):
        bump("tune")
        current = int(judge_result["criteria"][0]["actual"])
        if rejection_feedback is not None:
            new = current + 5
        else:
            # 과거 시도 기록을 실제로 읽는다 - 이 한 줄이 tuning_history 의
            # 왕복까지 판정에 묶어 준다. 기록이 재개에서 사라지면 다른 값이 나온다.
            new = current + 1 + attempts_view.count("rolled_back")
        return {
            "proposed_changes": [
                {
                    "refdes": "Rf",
                    "param": "value",
                    "old_value": str(current),
                    "new_value": str(new),
                }
            ]
        }

    async def verify_pre(structure_view, judge_result, proposal, netlist_view):
        bump("verify_pre")
        forbidden = any(c["new_value"] == FORBIDDEN for c in proposal["proposed_changes"])
        if forbidden:
            return {"approved": False, "concerns": ["forbidden"], "feedback": f"{FORBIDDEN} 은 안 된다"}
        return {"approved": True, "concerns": [], "feedback": "ok"}

    async def verify_post(prev_judge, new_judge, applied_changes):
        bump("verify_post")
        bad = any(c.get("new_value") == BAD for c in applied_changes)
        return {
            "improved": not bad,
            "regressed_criteria": ["gain"] if bad else [],
            "recommendation": "rollback" if bad else "keep",
            "feedback": "worse" if bad else "better",
        }

    async def propose_topology(structure_view, judge_result, candidates, library, feedback):
        bump("propose_topology")
        raise AssertionError("이 시나리오는 토폴로지 스왑에 도달하지 않는다")

    return OrchestratorAgents(
        simulate=simulate,
        judge=judge,
        tune=tune,
        verify_pre=verify_pre,
        verify_post=verify_post,
        propose_topology=propose_topology,
    )


def make_saver(state: RunState, box: dict):
    """cli 가 하는 일의 축소판 - LoopProgress 를 **JSON 왕복시켜** 담는다.
    메모리로 넘겨받으면 직렬화가 깨져도 테스트가 통과한다."""

    def save(progress):
        checkpoint = Checkpoint(
            boundary=BOUNDARY_OUTER_ITERATION,
            spec_path="spec.yaml",
            spec_sha256="deadbeef",
            netlist_sha256={TB: "cafe"},
            testbench_names=[TB],
            netlist_versions={n: list(p) for n, p in state.netlist_versions.items()},
            history_lines=line_count(state.history_path),
            progress=progress,
        )
        box["checkpoint"] = from_payload(json.loads(json.dumps(to_payload(checkpoint))))

    return save


async def run_uninterrupted(run_dir):
    state = RunState(run_dir=str(run_dir), testbench_names=[TB])
    box: dict = {}
    result = await run_orchestration(
        {TB: deck(10)}, SPEC, state, make_agents(), save_checkpoint=make_saver(state, box)
    )
    return result, state


async def run_crashed_then_resumed(run_dir, crash):
    state = RunState(run_dir=str(run_dir), testbench_names=[TB])
    box: dict = {}
    with pytest.raises(Boom):
        await run_orchestration(
            {TB: deck(10)},
            SPEC,
            state,
            make_agents(crash=crash),
            save_checkpoint=make_saver(state, box),
        )
    checkpoint = box["checkpoint"]

    # 크래시한 이터레이션의 부분 이벤트가 로그에 남아 있다. 자르지 않고,
    # 버려진 범위를 선언하는 resume 이벤트를 덧붙인다.
    discarded_from = checkpoint.history_lines
    discarded_to = line_count(state.history_path)

    resumed_state = RunState(run_dir=str(run_dir), testbench_names=[TB])
    restore_state(resumed_state, checkpoint)
    resumed_state.log_event(
        "resume",
        {
            "boundary": checkpoint.boundary,
            "outer_iter": checkpoint.progress.outer_iter,
            "discarded_lines": [discarded_from, discarded_to],
        },
    )
    entry_texts = {
        name: open(path).read() for name, path in checkpoint.progress.entry_netlist_paths.items()
    }
    box2: dict = {}
    result = await run_orchestration(
        entry_texts,
        SPEC,
        resumed_state,
        make_agents(),
        resume=checkpoint.progress,
        save_checkpoint=make_saver(resumed_state, box2),
    )
    return result, resumed_state, (discarded_from, discarded_to)


# ---------------------------------------------------------------- 기준 실행


@pytest.mark.asyncio
async def test_the_uninterrupted_run_exercises_a_rollback_and_a_retry(tmp_path):
    """재개 테스트가 무엇을 덮는지 못 박는다: 이 시나리오는 롤백 하나와
    verify_pre 거부 뒤 재시도 하나를 실제로 지난다."""
    result, state = await run_uninterrupted(tmp_path)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 3
    assert state.current_netlist_texts()[TB] == deck(16)

    steps = [e["step"] for e in read_events(state.history_path)]
    assert steps.count("verify_post") == 3
    rollbacks = [
        e for e in read_events(state.history_path)
        if e["step"] == "verify_post" and e["recommendation"] == "rollback"
    ]
    assert len(rollbacks) == 1
    rejections = [
        e for e in read_events(state.history_path)
        if e["step"] == "verify_pre" and not e["approved"]
    ]
    assert len(rejections) == 1


# ---------------------------------------------------------------- 사전 등록 판정 규칙 1


@pytest.mark.parametrize(
    "crash,label",
    [
        (("judge", 1), "첫 이터레이션 한복판"),
        (("simulate", 5), "롤백 직후 이터레이션의 첫 시뮬레이션"),
        (("tune", 4), "튜닝 재시도 중간 (verify_pre 거부 뒤 두 번째 tune)"),
        (("verify_post", 2), "롤백 판정 직후"),
        (("verify_pre", 1), "첫 제안 검증 직전"),
    ],
)
@pytest.mark.asyncio
async def test_a_resumed_run_lands_on_the_same_deck_and_verdict(tmp_path, crash, label):
    reference, ref_state = await run_uninterrupted(tmp_path / "a")
    resumed, res_state, _ = await run_crashed_then_resumed(tmp_path / "b", crash)

    assert resumed["status"] == reference["status"], label
    assert resumed["iterations_used"] == reference["iterations_used"], label
    assert resumed["final_criteria"] == reference["final_criteria"], label
    # **바이트 단위로** 같은 덱. "거의 같다"는 통과가 아니다.
    assert res_state.current_netlist_texts() == ref_state.current_netlist_texts(), label


@pytest.mark.parametrize("crash", [("judge", 1), ("simulate", 5), ("tune", 4)])
@pytest.mark.asyncio
async def test_a_resumed_run_has_the_same_version_stack_depth(tmp_path, crash):
    """재개 경로에서 push_netlist_version 을 다시 하면 v0 가 중복 push 되어
    버전 번호가 어긋난다 - 덱 내용이 같아도 final_netlist_paths 가 달라진다."""
    _, ref_state = await run_uninterrupted(tmp_path / "a")
    _, res_state, _ = await run_crashed_then_resumed(tmp_path / "b", crash)

    assert len(res_state.netlist_versions[TB]) == len(ref_state.netlist_versions[TB])
    assert [p.rsplit("/", 1)[-1] for p in res_state.netlist_versions[TB]] == [
        p.rsplit("/", 1)[-1] for p in ref_state.netlist_versions[TB]
    ]


# ---------------------------------------------------------------- 버려진 이벤트


@pytest.mark.asyncio
async def test_the_abandoned_iteration_events_are_dropped_not_deleted(tmp_path):
    """튜닝 재시도 중간에 죽으면 그 이터레이션의 `tuning_proposal` 이 이미
    로그에 있다. 재개하면 같은 제안이 또 쓰인다 - 로그를 그대로 세는
    measure_repeat_rate.py 가 하나를 둘로 센다. 그것이 D1 을 무효로 만든 것과
    같은 부류의 결함이다."""
    _, state, (start, end) = await run_crashed_then_resumed(tmp_path, ("tune", 4))

    everything = read_events(state.history_path, drop_discarded=False)
    kept = read_events(state.history_path)

    assert end > start  # 버려진 줄이 실제로 있었다
    # 원본은 디스크에 그대로 남는다 - 증거를 파괴하지 않는다.
    assert len(everything) - len(kept) == end - start
    abandoned = everything[start:end]
    assert [e["step"] for e in abandoned] == [
        "simulation",
        "judge",
        "focus",
        "attempt_log",
        "tuning_proposal",
        "area_check",
        "refdes_check",
        "param_check",
        "stimulus_check",
        "verify_pre",
        "attempt_log",
    ]
    # 재개하면 같은 이터레이션이 다시 돌아 **글자 그대로 같은** 이벤트를 또
    # 쓴다. 그래서 "버려진 이벤트가 kept 에 없다"는 성립하지 않는다 - 성립해야
    # 하는 것은 **한 번만 세어진다**는 것이고, 그것이 아래 테스트다.


@pytest.mark.asyncio
async def test_the_kept_log_counts_each_proposal_once(tmp_path):
    """재개된 로그를 헬퍼로 읽으면 제안 수가 중단 없이 돈 실행과 같다."""
    _, ref_state = await run_uninterrupted(tmp_path / "a")
    _, res_state, _ = await run_crashed_then_resumed(tmp_path / "b", ("tune", 4))

    def proposals(state):
        return [
            (e["outer_iter"], e["retry"], e["proposed_changes"][0]["new_value"])
            for e in read_events(state.history_path)
            if e["step"] == "tuning_proposal"
        ]

    assert proposals(res_state) == proposals(ref_state)
    raw = [
        e for e in read_events(res_state.history_path, drop_discarded=False)
        if e["step"] == "tuning_proposal"
    ]
    # 자르지 않은 로그에는 정말로 하나가 더 있다 - 이 테스트가 재는 것이 그 차이다.
    assert len(raw) == len(proposals(ref_state)) + 1


# ---------------------------------------------------------------- 반송 상태


@pytest.mark.asyncio
async def test_the_checkpoint_carries_the_tuning_history_forward(tmp_path):
    _, state = await run_uninterrupted(tmp_path / "a")
    state_b = RunState(run_dir=str(tmp_path / "b"), testbench_names=[TB])
    box: dict = {}
    with pytest.raises(Boom):
        await run_orchestration(
            {TB: deck(10)},
            SPEC,
            state_b,
            make_agents(crash=("simulate", 5)),
            save_checkpoint=make_saver(state_b, box),
        )

    progress = box["checkpoint"].progress

    assert progress.outer_iter == 3
    assert progress.consecutive_rollbacks == 1
    assert [(a.outer_iter, a.outcome) for a in progress.tuning_history] == [
        (1, "kept"),
        (2, "rolled_back"),
    ]
    # 측정된 델타까지 함께 넘어간다 - 튜너 프롬프트가 읽는 것이 이 숫자다.
    assert progress.tuning_history[1].deltas == (("gain", 1.0),)
    assert progress.judge_result["criteria"][0]["actual"] == 12.0


@pytest.mark.asyncio
async def test_a_resume_without_a_saver_still_runs(tmp_path):
    """save_checkpoint 는 선택 인자다 - 기존 호출부(테스트 포함)가 그대로 돈다."""
    state = RunState(run_dir=str(tmp_path), testbench_names=[TB])

    result = await run_orchestration({TB: deck(10)}, SPEC, state, make_agents())

    assert result["status"] == "PASS"
