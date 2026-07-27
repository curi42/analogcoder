import inspect
import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.backends.claude_sdk import ClaudeSDKBackend
from analogcoder.agents.backends.openai_compatible import OpenAICompatibleBackend
from analogcoder.cli import (
    AGENT_NAMES,
    _build_agent_backend,
    _build_agent_backends,
    _run,
    build_arg_parser,
)

# 최적화 단계가 시뮬레이션 결과를 어떻게 읽는지를 그대로 상대로 삼는다.
# cli.py가 만드는 status를 optimizer가 실제로 거절하는지까지 확인하지 않으면,
# "합쳤다"는 사실만 남고 그것이 무엇을 막는지는 아무도 지키지 않는다.
from analogcoder.optimizer import _run_simulation

SPEC_YAML = (
    "circuit_name: test\n"
    "testbenches:\n"
    "  - name: ac_loop_gain\n"
    "    netlist: netlist.cir\n"
    "    analyses: [\"ac\"]\n"
    "    control_block: |\n"
    "      .control\n"
    "      .endc\n"
    "    criteria: []\n"
)

TWO_TESTBENCH_SPEC_YAML = (
    "circuit_name: test\n"
    "testbenches:\n"
    "  - name: ac_loop_gain\n"
    "    netlist: netlist.cir\n"
    '    analyses: ["ac"]\n'
    '    control_block: ".control\\n.endc\\n"\n'
    "    criteria: []\n"
    "  - name: psr_plus\n"
    "    netlist: netlist_psr_plus.cir\n"
    '    analyses: ["ac"]\n'
    '    control_block: ".control\\n.endc\\n"\n'
    "    criteria: []\n"
)

OPTIMIZE_SPEC_YAML = (
    "circuit_name: test\n"
    "optimize:\n"
    "  objective: iq_ua\n"
    "  area_budget: 1.1\n"
    "  guard_band: 0.2\n"
    "pvt_corners:\n"
    "  process: [tt]\n"
    "  voltage: [1.8]\n"
    "  temperature: [27]\n"
    "testbenches:\n"
    "  - name: ac_loop_gain\n"
    "    netlist: netlist.cir\n"
    '    analyses: ["ac"]\n'
    '    control_block: ".control\\n.endc\\n"\n'
    "    criteria: []\n"
)

OPTIMIZE_NO_CORNERS_SPEC_YAML = (
    "circuit_name: test\n"
    "optimize:\n"
    "  objective: iq_ua\n"
    "  area_budget: 1.1\n"
    "  guard_band: 0.2\n"
    "testbenches:\n"
    "  - name: ac_loop_gain\n"
    "    netlist: netlist.cir\n"
    '    analyses: ["ac"]\n'
    '    control_block: ".control\\n.endc\\n"\n'
    "    criteria: []\n"
)


# 메인 루프가 고쳐 놓은 덱. 원본과 **내용이 달라야** 한다 - 최적화가 어느
# 쪽을 받는지는 키 집합으로는 구별되지 않기 때문이다(둘 다 테스트벤치 이름이
# 키다). 이 상수가 없으면 원본을 넘기는 배선 실수가 전 테스트를 통과한다.
TUNED_TEXT = "* tuned by the main loop\nM1 d g 0 0 nch W=44 L=1\n.end\n"


def _orchestration(result, captured: dict | None = None):
    """run_orchestration 대역. **v0을 push하고 그 위에 튜닝된 v1을 push한다.**

    진짜 run_orchestration은 첫 줄에서 state.push_netlist_version을 부르고
    (orchestrator.py:61) 그 뒤로 튜닝이 적용될 때마다 새 버전을 민다. 그것을
    흉내내지 않는 mock은 프로덕션에 존재하지 않는 모양(버전이 없거나, 현재
    덱이 원본과 똑같은 RunState)을 남긴다. 후자가 특히 위험하다: 최적화에
    현재 덱 대신 원본을 넘기는 배선 실수가 그 상태에서는 아무 테스트도 깨지
    않고, 실제 실행에서는 튜닝 결과를 통째로 버린 덱이 확정되고 보고된다."""

    async def fake(initial_netlist_texts, spec, state, agents):
        state.push_netlist_version(initial_netlist_texts)
        state.push_netlist_version({name: TUNED_TEXT for name in initial_netlist_texts})
        if captured is not None:
            captured["spec"] = spec
            captured["state"] = state
            captured["agents"] = agents
        return result

    return fake


def _one_history_event(run_dir: str, step: str) -> dict:
    """history.jsonl에서 그 step 이벤트 하나를 꺼낸다. 없거나 둘 이상이면 실패."""
    with open(os.path.join(run_dir, "history.jsonl")) as f:
        events = [json.loads(line) for line in f if line.strip()]
    matching = [e for e in events if e["step"] == step]
    assert len(matching) == 1, f"expected exactly one {step!r} event, got {len(matching)}"
    return matching[0]


def _pass_result(run_dir: str) -> dict:
    return {
        "status": "PASS",
        "final_netlist_paths": {},
        "run_dir": run_dir,
        "iterations_used": 1,
        "final_criteria": [],
    }


def test_arg_parser_requires_spec_only():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml"])
    assert args.spec == "s.yaml"
    assert args.simulator == "ngspice"
    assert args.agent_backend == "claude"
    assert not hasattr(args, "netlist")


def test_build_agent_backend_returns_claude_backend_by_default():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml"])
    backend = _build_agent_backend(args)
    assert isinstance(backend, ClaudeSDKBackend)


def test_build_agent_backend_returns_openai_compatible_backend_when_configured():
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "--spec", "s.yaml",
            "--agent-backend", "openai-compatible",
            "--llm-base-url", "http://local",
            "--llm-model", "glm-5.2",
        ]
    )
    backend = _build_agent_backend(args)
    assert isinstance(backend, OpenAICompatibleBackend)
    assert backend.base_url == "http://local"
    assert backend.model == "glm-5.2"
    assert backend.api_key_env == "LOCAL_LLM_API_KEY"


def test_build_agent_backend_raises_when_openai_compatible_missing_config():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml", "--agent-backend", "openai-compatible"])
    with pytest.raises(ValueError):
        _build_agent_backend(args)


@pytest.mark.asyncio
async def test_run_wires_orchestration_and_returns_its_result(tmp_path):
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(SPEC_YAML)

    fake_result = {
        "status": "PASS",
        "final_netlist_paths": {"ac_loop_gain": str(tmp_path / "runs" / "r1" / "netlist_v0_ac_loop_gain.cir")},
        "run_dir": str(tmp_path / "runs" / "r1"),
        "iterations_used": 1,
        "final_criteria": [],
    }

    parser = build_arg_parser()
    args = parser.parse_args(
        ["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r1")]
    )

    with patch("analogcoder.cli.run_orchestration", new=_orchestration(fake_result)):
        result = await _run(args)

    # `result == fake_result`로는 아무것도 못 지킨다 - _run은 같은 dict를
    # 제자리에서 고치므로 어떤 키를 더해도 그 비교는 참이다. 오케스트레이션이
    # 낸 값이 그대로 통과했는지를 키별로 본다.
    assert result["status"] == "PASS"
    assert result["run_dir"] == str(tmp_path / "runs" / "r1")
    assert result["iterations_used"] == 1
    assert result["final_criteria"] == []


@pytest.mark.asyncio
async def test_run_passes_one_netlist_text_per_testbench_to_run_orchestration(tmp_path):
    (tmp_path / "netlist.cir").write_text("* ac netlist\n.end\n")
    (tmp_path / "netlist_psr_plus.cir").write_text("* psr netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        "circuit_name: test\n"
        "testbenches:\n"
        "  - name: ac_loop_gain\n"
        "    netlist: netlist.cir\n"
        "    analyses: [\"ac\"]\n"
        "    control_block: \".control\\n.endc\\n\"\n"
        "    criteria: []\n"
        "  - name: psr_plus\n"
        "    netlist: netlist_psr_plus.cir\n"
        "    analyses: [\"ac\"]\n"
        "    control_block: \".control\\n.endc\\n\"\n"
        "    criteria: []\n"
    )

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r2")])

    captured = {}

    async def fake_run_orchestration(initial_netlist_texts, spec, state, agents):
        state.push_netlist_version(initial_netlist_texts)
        captured["texts"] = initial_netlist_texts
        return {
            "status": "PASS",
            "final_netlist_paths": {},
            "run_dir": str(tmp_path / "runs" / "r2"),
            "iterations_used": 1,
            "final_criteria": [],
        }

    with patch("analogcoder.cli.run_orchestration", new=fake_run_orchestration):
        await _run(args)

    assert captured["texts"] == {"ac_loop_gain": "* ac netlist\n.end\n", "psr_plus": "* psr netlist\n.end\n"}


@pytest.mark.asyncio
async def test_run_skips_pvt_sweep_when_spec_has_no_pvt_corners(tmp_path):
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(SPEC_YAML)

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r3")])

    fake_result = {
        "status": "PASS", "final_netlist_paths": {}, "run_dir": str(tmp_path / "runs" / "r3"),
        "iterations_used": 1, "final_criteria": [],
    }

    with (
        patch("analogcoder.cli.run_orchestration", new=_orchestration(fake_result)),
        patch("analogcoder.cli.run_full_pvt_sweep") as mock_sweep,
    ):
        result = await _run(args)

    mock_sweep.assert_not_called()
    assert result["status"] == "PASS"
    assert "pvt_sweep" not in result


@pytest.mark.asyncio
async def test_run_overrides_pass_to_fail_when_final_pvt_sweep_fails(tmp_path):
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")
    spec_path = tmp_path / "spec_pvt.yaml"
    spec_path.write_text(
        "circuit_name: test\n"
        "pvt_corners:\n"
        "  process: [tt]\n"
        "  voltage: [1.8]\n"
        "  temperature: [27]\n"
        "testbenches:\n"
        "  - name: ac_loop_gain\n"
        "    netlist: netlist.cir\n"
        '    analyses: ["ac"]\n'
        '    control_block: ".control\\n.endc\\n"\n'
        "    criteria:\n"
        "      - name: gain\n"
        "        measurement: gain_db\n"
        '        operator: ">="\n'
        "        threshold: 10.0\n"
    )

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r4")])

    fake_result = {
        "status": "PASS", "final_netlist_paths": {"ac_loop_gain": str(tmp_path / "netlist.cir")},
        "run_dir": str(tmp_path / "runs" / "r4"), "iterations_used": 1, "final_criteria": [],
    }
    fake_final_sweep = {
        "overall_pass": False, "criteria": [], "summary": "one or more criteria failed",
        "worst_case_corners": {"gain": {"process": "tt", "voltage": 1.8, "temperature": 27, "value": 5.0}},
    }

    # 이 스펙에는 optimize 블록이 없으므로 최적화는 SKIPPED로 지나가고
    # (pvt_sweep=None) 최종 스윕이 실제로 돈다 - 재사용 경로가 아니다.
    # run_full_pvt_sweep은 mock이라 넷리스트 인자를 보지 않는다.
    with (
        patch("analogcoder.cli.run_orchestration", new=_orchestration(fake_result)),
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=fake_final_sweep) as mock_sweep,
    ):
        result = await _run(args)

    assert mock_sweep.call_count == 2  # baseline sweep + final sweep
    assert result["status"] == "FAIL"
    assert "pvt_sweep" in result
    assert result["pvt_sweep"]["worst_case_corners"]["gain"]["process"] == "tt"


@pytest.mark.asyncio
async def test_run_keeps_pass_when_final_pvt_sweep_also_passes(tmp_path):
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")
    spec_path = tmp_path / "spec_pvt.yaml"
    spec_path.write_text(
        "circuit_name: test\n"
        "pvt_corners:\n"
        "  process: [tt]\n"
        "  voltage: [1.8]\n"
        "  temperature: [27]\n"
        "testbenches:\n"
        "  - name: ac_loop_gain\n"
        "    netlist: netlist.cir\n"
        '    analyses: ["ac"]\n'
        '    control_block: ".control\\n.endc\\n"\n'
        "    criteria:\n"
        "      - name: gain\n"
        "        measurement: gain_db\n"
        '        operator: ">="\n'
        "        threshold: 10.0\n"
    )

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r5")])

    fake_result = {
        "status": "PASS", "final_netlist_paths": {"ac_loop_gain": str(tmp_path / "netlist.cir")},
        "run_dir": str(tmp_path / "runs" / "r5"), "iterations_used": 1, "final_criteria": [],
    }
    fake_passing_sweep = {
        "overall_pass": True, "criteria": [], "summary": "all criteria passed",
        "worst_case_corners": {},
    }

    with (
        patch("analogcoder.cli.run_orchestration", new=_orchestration(fake_result)),
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=fake_passing_sweep) as mock_sweep,
    ):
        result = await _run(args)

    assert mock_sweep.call_count == 2
    assert result["status"] == "PASS"
    assert result["pvt_sweep"]["overall_pass"] is True


@pytest.mark.asyncio
async def test_run_hands_orchestration_include_resolved_netlist_texts(tmp_path):
    # The orchestration loop simulates the netlist copies RunState stages into
    # the run dir, not the originals in the benchmark dir. A bare relative
    # `.include "pdk_corner.inc"` cannot resolve from there, so _run must
    # absolutize includes against the netlist's own directory before handing
    # the texts off - otherwise every simulation of a real PDK-based benchmark
    # fails with "could not find include file" and the whole loop runs on
    # empty measurements.
    (tmp_path / "pdk_corner.inc").write_text("* pdk\n")
    (tmp_path / "netlist.cir").write_text('* netlist\n.include "pdk_corner.inc"\n.end\n')
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(SPEC_YAML)

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r6")])

    fake_result = {
        "status": "PASS", "final_netlist_paths": {"ac_loop_gain": str(tmp_path / "netlist.cir")},
        "run_dir": str(tmp_path / "runs" / "r6"), "iterations_used": 1, "final_criteria": [],
    }

    with patch(
        "analogcoder.cli.run_orchestration", new=AsyncMock(side_effect=_orchestration(fake_result))
    ) as mock_orch:
        await _run(args)

    passed_texts = mock_orch.await_args.args[0]
    assert f'.include "{tmp_path / "pdk_corner.inc"}"' in passed_texts["ac_loop_gain"]


def test_claude_backend_defaults_to_sonnet():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml"])

    backends = _build_agent_backends(args)

    assert set(backends) == {"simulator", "judge", "tuner", "verifier", "optimizer"}
    assert all(b.model == "sonnet" for b in backends.values())


def test_claude_model_flag_sets_every_agent_model():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml", "--claude-model", "haiku"])

    backends = _build_agent_backends(args)

    assert all(b.model == "haiku" for b in backends.values())


def test_agent_model_flag_overrides_a_single_agent():
    # Lets a run drop one agent to a weaker model to see whether the pipeline
    # still holds - the tool-calling agents (simulator, judge) are the ones a
    # lower-capability model has historically struggled with.
    parser = build_arg_parser()
    args = parser.parse_args(
        ["--spec", "s.yaml", "--claude-model", "sonnet", "--agent-model", "simulator=haiku"]
    )

    backends = _build_agent_backends(args)

    assert backends["simulator"].model == "haiku"
    assert backends["tuner"].model == "sonnet"


def test_agent_model_flag_rejects_an_unknown_agent_name():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml", "--agent-model", "nosuchagent=haiku"])

    with pytest.raises(ValueError, match="nosuchagent"):
        _build_agent_backends(args)


def test_optimizer_is_an_agent_whose_model_can_be_overridden():
    # 최적화 제안도 LLM 호출이므로 다른 에이전트와 같은 자리에 있어야 한다 -
    # 아니면 그 한 에이전트만 모델을 내려 실험할 방법이 없다.
    assert "optimizer" in AGENT_NAMES

    parser = build_arg_parser()
    args = parser.parse_args(
        ["--spec", "s.yaml", "--claude-model", "sonnet", "--agent-model", "optimizer=haiku"]
    )

    backends = _build_agent_backends(args)

    assert backends["optimizer"].model == "haiku"
    assert backends["tuner"].model == "sonnet"


# --- 지뢰 1: 테스트벤치를 가로지르는 status ------------------------------------


async def _capture_simulate_fn(tmp_path, spec_yaml: str, run_dir: str):
    """_run을 한 번 돌려 orchestrator에 넘어가는 simulate 콜러블을 꺼낸다.

    최적화 단계도 **같은 콜러블**을 받으므로(cli.py가 simulate_fn 하나를 두
    소비자에게 준다) 여기서 꺼낸 것이 곧 optimizer가 보는 것이다."""
    (tmp_path / "netlist.cir").write_text("* ac netlist\n.end\n")
    (tmp_path / "netlist_psr_plus.cir").write_text("* psr netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(spec_yaml)

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", run_dir])

    captured: dict = {}
    with patch(
        "analogcoder.cli.run_orchestration",
        new=_orchestration(_pass_result(run_dir), captured),
    ):
        await _run(args)

    return captured["agents"].simulate, captured["spec"]


@pytest.mark.asyncio
async def test_simulate_fn_merges_a_non_success_status_across_testbenches(tmp_path):
    # 한 테스트벤치가 수렴하지 못했는데 합쳐진 결과가 성공으로 보이면,
    # 최적화는 수렴 실패한 해의 측정값으로 마진을 태우는 결정을 내린다
    # (실제로 iq_ua=1.0이 235->1 개선으로 수락된 적이 있다). optimizer는
    # status 키가 **없으면** 성공으로 읽으므로, 이 신호가 없는 것은 틀린 것보다
    # 나쁘다 - 어떤 mock에도 보이지 않는다.
    run_dir = str(tmp_path / "runs" / "s1")
    # control_block은 프로덕션의 SIMULATION_SCHEMA가 required로 두는 키다.
    # 이 대역은 cli.agent_simulate를 통째로 갈아끼워 스키마 검증을 지나치므로
    # 없어도 오늘은 통과하지만, 프로덕션이 내놓지 않는 모양을 흉내내는 대역은
    # 그 키를 읽는 소비자가 생기는 순간 조용히 폴백 경로만 재게 된다.
    per_testbench = [
        {"measurements": {"gain_db": 40.0}, "status": "success", "warnings": [],
         "control_block": ".control\n.endc\n"},
        {"measurements": {"iq_ua": 1.0}, "status": "convergence_failure", "warnings": [],
         "control_block": ".control\n.endc\n"},
    ]

    async def fake_agent_simulate(*args, **kwargs):
        return per_testbench.pop(0)

    with patch("analogcoder.cli.agent_simulate", new=fake_agent_simulate):
        simulate_fn, spec = await _capture_simulate_fn(
            tmp_path, TWO_TESTBENCH_SPEC_YAML, run_dir
        )
        merged = await simulate_fn({}, spec)

    assert merged["status"] == "convergence_failure"
    # 전부 성공했을 때만 성공이라는 규칙이 소비자 쪽에서 실제로 걸리는가.
    result, reason = await _run_simulation(
        lambda texts, spec_arg: _as_coroutine(merged), {}, spec
    )
    assert result is None
    assert "convergence_failure" in reason


@pytest.mark.asyncio
async def test_simulate_fn_reports_success_only_when_every_testbench_succeeded(tmp_path):
    # 반대 방향. status를 무조건 실패로 박아도 위 테스트는 통과하므로,
    # 성공 경로가 성공으로 남는지 같이 고정한다.
    run_dir = str(tmp_path / "runs" / "s2")
    per_testbench = [
        {"measurements": {"gain_db": 40.0}, "status": "success", "warnings": [],
         "control_block": ".control\n.endc\n"},
        {"measurements": {"iq_ua": 200.0}, "status": "success", "warnings": [],
         "control_block": ".control\n.endc\n"},
    ]

    async def fake_agent_simulate(*args, **kwargs):
        return per_testbench.pop(0)

    with patch("analogcoder.cli.agent_simulate", new=fake_agent_simulate):
        simulate_fn, spec = await _capture_simulate_fn(
            tmp_path, TWO_TESTBENCH_SPEC_YAML, run_dir
        )
        merged = await simulate_fn({}, spec)

    assert merged["status"] == "success"
    assert merged["measurements"] == {"gain_db": 40.0, "iq_ua": 200.0}
    result, reason = await _run_simulation(
        lambda texts, spec_arg: _as_coroutine(merged), {}, spec
    )
    assert result is merged
    assert reason is None


async def _as_coroutine(value):
    return value


# --- 최적화 배선 --------------------------------------------------------------


def _optimization_result(**overrides) -> dict:
    base = {
        "status": "UNCHANGED",
        "objective_before": 200.0,
        "objective_after": 200.0,
        "area_before": 1.0,
        "area_after": 1.0,
        "steps_accepted": 0,
        "steps_rejected": 0,
        "corner_confirmed": False,
        "corner_failure": None,
        "pvt_sweep": None,
        "final_netlist_paths": {},
    }
    return {**base, **overrides}


async def _run_with_optimization(
    tmp_path, spec_yaml, run_dir, fake_optimization, sweep_result, extra_args=()
):
    """_run을 돌리되 최적화와 스윕을 둘 다 대역으로 바꾼다.
    (result, mock_sweep, captured) 를 돌려준다."""
    (tmp_path / "netlist.cir").write_text("* ac netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(spec_yaml)

    parser = build_arg_parser()
    args = parser.parse_args(
        ["--spec", str(spec_path), "--run-dir", run_dir, *extra_args]
    )

    captured: dict = {}

    with (
        patch(
            "analogcoder.cli.run_orchestration",
            new=_orchestration(_pass_result(run_dir), captured),
        ),
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=sweep_result) as mock_sweep,
        patch("analogcoder.cli.run_optimization", new=fake_optimization(captured, mock_sweep)),
    ):
        result = await _run(args)

    return result, mock_sweep, captured


@pytest.mark.asyncio
async def test_optimization_runs_after_pass_and_before_the_final_pvt_sweep(tmp_path):
    # 순서가 계약이다. 최종 스윕이 최적화된 넷리스트를 확정하는 역할을 하려면
    # 최적화가 그 앞에 와야 한다 - 뒤에 두면 아무도 확인하지 않은 넷리스트로
    # 실행이 끝난다.
    passing = {"overall_pass": True, "criteria": [], "summary": "ok", "worst_case_corners": {}}

    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            # 여기까지 온 스윕 호출은 baseline 하나뿐이어야 한다.
            captured["sweeps_before_optimization"] = mock_sweep.call_count
            captured["netlist_texts"] = netlist_texts
            captured["optimizer_simulate"] = agents.simulate
            return _optimization_result()

        return run

    result, mock_sweep, captured = await _run_with_optimization(
        tmp_path, OPTIMIZE_SPEC_YAML, str(tmp_path / "runs" / "o1"), fake_optimization, passing
    )

    assert captured["sweeps_before_optimization"] == 1  # baseline만 돌았다
    assert mock_sweep.call_count == 2  # 최적화가 코너를 확인하지 못했으니 최종 스윕이 돈다
    # 최적화는 실행의 **현재** 넷리스트 위에서 돈다 - 파일에서 읽은 원본이
    # 아니다. 키 집합은 둘이 같으므로 **내용**을 본다. 원본을 넘기면
    # run_optimization이 인자와 state의 불일치를 보고 원본을 새 버전으로
    # 밀어 넣어(optimizer.py:534) 메인 루프의 수리를 되돌린다.
    assert captured["netlist_texts"] == {"ac_loop_gain": TUNED_TEXT}
    # 메인 루프가 쓰던 것과 **같은** simulate여야 한다. 최적화 전용 래퍼를 따로
    # 두면 status 병합 같은 규칙이 한쪽에만 붙는다.
    assert captured["optimizer_simulate"] is captured["agents"].simulate
    assert result["optimization"]["status"] == "UNCHANGED"
    assert result["status"] == "PASS"


@pytest.mark.asyncio
async def test_optimization_does_not_run_when_the_orchestration_did_not_pass(tmp_path):
    (tmp_path / "netlist.cir").write_text("* ac netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(OPTIMIZE_NO_CORNERS_SPEC_YAML)

    run_dir = str(tmp_path / "runs" / "o2")
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", run_dir])

    fail_result = {**_pass_result(run_dir), "status": "FAIL", "failure_reason": "max iterations"}

    with (
        patch("analogcoder.cli.run_orchestration", new=_orchestration(fail_result)),
        patch("analogcoder.cli.run_optimization", new=AsyncMock()) as mock_opt,
    ):
        result = await _run(args)

    # 통과하지 못한 설계의 마진을 더 깎을 이유가 없다.
    mock_opt.assert_not_called()
    assert "optimization" not in result


@pytest.mark.asyncio
async def test_verify_corners_handed_to_the_optimizer_is_synchronous(tmp_path):
    # run_optimization은 이것을 await 없이 직접 부른다. async로 감싸면
    # 코루틴 객체가 "쓸 수 없는 결과"로 접혀 최적화 단계 전체가 조용히
    # UNCHANGED가 된다 - 크래시도 로그도 없이.
    passing = {"overall_pass": True, "criteria": [], "summary": "ok", "worst_case_corners": {}}

    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            captured["verify_corners"] = agents.verify_corners
            # run_optimization이 하는 그대로 - await 없이 직접 부른다.
            captured["returned"] = agents.verify_corners({"ac_loop_gain": "* ac netlist\n.end\n"})
            return _optimization_result()

        return run

    _, mock_sweep, captured = await _run_with_optimization(
        tmp_path, OPTIMIZE_SPEC_YAML, str(tmp_path / "runs" / "o3"), fake_optimization, passing
    )

    verify_corners = captured["verify_corners"]
    assert verify_corners is not None
    assert not inspect.iscoroutinefunction(verify_corners)
    # 코루틴이면 run_optimization의 _run_sweep이 "overall_pass가 없는 결과"로
    # 접어 코너를 확인하지 못한 채 조용히 지나간다.
    assert not inspect.isawaitable(captured["returned"])
    assert captured["returned"] is passing


@pytest.mark.asyncio
async def test_verify_corners_is_none_when_the_spec_declares_no_corners(tmp_path):
    # 코너를 잴 수단이 없으면 None을 준다 - 그때 run_optimization은
    # 확인하지 않았다고 보고한다. 빈 스윕을 지어내지 않는다.
    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            captured["verify_corners"] = agents.verify_corners
            return _optimization_result()

        return run

    result, mock_sweep, captured = await _run_with_optimization(
        tmp_path,
        OPTIMIZE_NO_CORNERS_SPEC_YAML,
        str(tmp_path / "runs" / "o4"),
        fake_optimization,
        None,
    )

    assert captured["verify_corners"] is None
    mock_sweep.assert_not_called()
    assert "pvt_sweep" not in result


@pytest.mark.asyncio
async def test_a_confirmed_optimization_sweep_is_reused_as_the_final_sweep(tmp_path):
    # 최적화가 착지시킨 지점은 정의상 스윕을 통과한 버전이다. 같은 덱에
    # 같은 값(bandgap 기준 286초)을 두 번 치를 이유가 없다.
    confirmed = {
        "overall_pass": True, "criteria": [], "summary": "all corners passed",
        "worst_case_corners": {"iq": {"process": "ff", "voltage": 1.98, "temperature": 125}},
    }

    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            return _optimization_result(
                status="OPTIMIZED", objective_after=180.0, steps_accepted=2,
                corner_confirmed=True, pvt_sweep=confirmed,
            )

        return run

    result, mock_sweep, _ = await _run_with_optimization(
        tmp_path, OPTIMIZE_SPEC_YAML, str(tmp_path / "runs" / "o5"), fake_optimization, confirmed
    )

    assert mock_sweep.call_count == 1  # baseline 스윕 하나뿐이다
    assert result["pvt_sweep"] is confirmed
    assert result["status"] == "PASS"
    # 두 결과가 같은 키 이름을 쓴다. 어느 쪽도 다른 쪽을 덮지 않는다.
    assert result["optimization"]["pvt_sweep"] is confirmed
    assert result["optimization"]["corner_confirmed"] is True
    # 재사용해도 이력에는 남는다 - history.jsonl에서 pvt_final_sweep을 찾는
    # 사람이 하필 스윕을 가장 많이 돌린 실행에서 빈손이 되면 안 된다.
    event = _one_history_event(str(tmp_path / "runs" / "o5"), "pvt_final_sweep")
    assert event["reused_from_optimization"] is True
    assert event["summary"] == "all corners passed"


@pytest.mark.asyncio
async def test_an_unconfirmed_optimization_keeps_its_corner_failure_and_still_sweeps(tmp_path):
    # 스윕이 **돌지 못한** 사유는 결과에 남아야 한다. "코너가 깨져서
    # 되돌아왔다"와 "스윕을 못 돌려서 되돌아왔다"는 다른 사실이고, 후자는
    # 고칠 대상이 회로가 아니다. 그리고 확인이 없었으므로 최종 스윕은 돈다.
    passing = {"overall_pass": True, "criteria": [], "summary": "ok", "worst_case_corners": {}}

    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            return _optimization_result(
                corner_failure="corner sweep raised RuntimeError: ngspice died"
            )

        return run

    result, mock_sweep, _ = await _run_with_optimization(
        tmp_path, OPTIMIZE_SPEC_YAML, str(tmp_path / "runs" / "o6"), fake_optimization, passing
    )

    assert mock_sweep.call_count == 2  # baseline + 최종
    assert result["pvt_sweep"] is passing
    assert (
        result["optimization"]["corner_failure"]
        == "corner sweep raised RuntimeError: ngspice died"
    )
    assert result["optimization"]["pvt_sweep"] is None
    # 실제로 돈 스윕은 재사용이 아니라고 말한다.
    event = _one_history_event(str(tmp_path / "runs" / "o6"), "pvt_final_sweep")
    assert event["reused_from_optimization"] is False


@pytest.mark.asyncio
async def test_a_failing_optimization_sweep_still_turns_the_run_into_a_fail(tmp_path):
    # 브리프의 산문은 "재사용 경로에서는 status가 FAIL로 뒤집힐 수 없다"고
    # 말하지만, 진입 스윕이 실패한 경우 run_optimization은 **통과하지 않은**
    # 스윕을 실어 UNCHANGED를 낸다. 그 넷리스트는 최적화를 돌리지 않았을 때
    # 최종 스윕이 보게 될 바로 그 덱이므로, 판정은 그때와 같아야 한다.
    failing = {
        "overall_pass": False, "criteria": [], "summary": "one or more criteria failed",
        "worst_case_corners": {},
    }

    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            return _optimization_result(pvt_sweep=failing)

        return run

    result, mock_sweep, _ = await _run_with_optimization(
        tmp_path, OPTIMIZE_SPEC_YAML, str(tmp_path / "runs" / "o7"), fake_optimization, failing
    )

    assert mock_sweep.call_count == 1  # 같은 덱을 두 번 재지 않는다
    assert result["status"] == "FAIL"
    # 사유는 **실제로 일어난 일**을 말해야 한다. 이 경로에서 최종 스윕은 돌지
    # 않았으므로 "final PVT sweep failed"라고 적으면 history.jsonl과 대조하는
    # 사람이 있지도 않은 스윕을 찾게 된다.
    assert result["failure_reason"] == (
        "PVT sweep from the optimization phase failed: one or more criteria failed"
    )


@pytest.mark.asyncio
async def test_the_reported_netlist_paths_follow_the_version_optimization_landed_on(tmp_path):
    # 최적화는 넷리스트 버전을 밀고 되돌린다. 실행이 내놓는 경로가 그것을
    # 따라가지 않으면 report가 최적화 **이전** 덱을 가리킨다 - 실제로 실린
    # 회로와 보고된 회로가 다른 상태다.
    passing = {"overall_pass": True, "criteria": [], "summary": "ok", "worst_case_corners": {}}

    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            state.push_netlist_version({"ac_loop_gain": "* optimized\n.end\n"})
            captured["landed"] = state.current_netlist_paths()
            return _optimization_result(status="OPTIMIZED", steps_accepted=1, pvt_sweep=passing)

        return run

    result, _, captured = await _run_with_optimization(
        tmp_path, OPTIMIZE_SPEC_YAML, str(tmp_path / "runs" / "o9"), fake_optimization, passing
    )

    assert result["final_netlist_paths"] == captured["landed"]
    with open(result["final_netlist_paths"]["ac_loop_gain"]) as f:
        assert f.read() == "* optimized\n.end\n"


@pytest.mark.asyncio
async def test_the_optimizer_proposal_runs_on_the_optimizer_agent_backend(tmp_path):
    # propose가 다른 에이전트의 백엔드에 배선되면 --agent-model optimizer=...가
    # 아무 데도 닿지 않는다. 모델을 갈라 두고 실제로 넘어간 백엔드를 본다.
    passing = {"overall_pass": True, "criteria": [], "summary": "ok", "worst_case_corners": {}}

    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            captured["propose"] = agents.propose
            return _optimization_result()

        return run

    _, _, captured = await _run_with_optimization(
        tmp_path,
        OPTIMIZE_SPEC_YAML,
        str(tmp_path / "runs" / "o8"),
        fake_optimization,
        passing,
        extra_args=["--claude-model", "sonnet", "--agent-model", "optimizer=haiku"],
    )

    with patch("analogcoder.cli.propose_candidates", new=AsyncMock(return_value={})) as mock_propose:
        await captured["propose"]("structure", [{"name": "gain"}], "iq_ua", "netlist")

    args = mock_propose.await_args.args
    assert args[:4] == ("structure", [{"name": "gain"}], "iq_ua", "netlist")
    assert args[4].model == "haiku"


# --- 최종 리뷰 Finding 2: 결과가 최적화 **전** 회로를 설명하고 있었다 --------


@pytest.mark.asyncio
async def test_the_reported_criteria_come_from_the_version_optimization_landed_on(tmp_path):
    # final_netlist_paths는 착지 버전으로 갱신되는데 final_criteria는 메인
    # 루프의 judge 결과(최적화 **전** 덱)로 남아 있었다. 실측 bandgap 실행에서
    # 212.25uA를 재는 넷리스트 경로 옆에 212.99uA가 적혔다.
    passing = {"overall_pass": True, "criteria": [], "summary": "ok", "worst_case_corners": {}}
    landed = [{"name": "iq", "target": "<=300.0", "actual": 212.25, "pass": True,
               "margin": -87.75}]

    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            return _optimization_result(status="OPTIMIZED", final_criteria=landed)

        return run

    result, _, _ = await _run_with_optimization(
        tmp_path, OPTIMIZE_SPEC_YAML, str(tmp_path / "runs" / "o7"), fake_optimization, passing
    )

    assert result["final_criteria"] == landed


@pytest.mark.asyncio
async def test_an_optimization_without_criteria_leaves_the_main_loop_judgement_alone(tmp_path):
    # 최적화가 기준을 재지 못한 경로(기준선 시뮬레이션 실패, 이 단계 자체가
    # 터진 경우)가 있다. 그때 없는 값으로 덮으면 리포트가 통째로 빈다.
    passing = {"overall_pass": True, "criteria": [], "summary": "ok", "worst_case_corners": {}}

    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            return _optimization_result(failure="AgentExecutionError: rate limited")

        return run

    result, _, _ = await _run_with_optimization(
        tmp_path, OPTIMIZE_SPEC_YAML, str(tmp_path / "runs" / "o8"), fake_optimization, passing
    )

    assert result["final_criteria"] == []   # 메인 루프가 남긴 것 그대로
    assert result["optimization"]["failure"] == "AgentExecutionError: rate limited"
