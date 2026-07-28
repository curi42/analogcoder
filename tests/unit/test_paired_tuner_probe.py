"""scripts/paired_tuner_probe.py - 재생 복원과 R_exact/R_knob 채점.

이 스크립트가 LLM 호출을 쓰기 전에 하는 유일한 주장은 "재생이 런과 같다"이고,
그 주장이 틀리면 실험 전체가 틀린 상태에서 돈다. 형제 측정 스크립트가 테스트
없이 출하돼 실제 결함(토폴로지 스왑 오귀속)을 통과시킨 전례가 있다.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "paired_tuner_probe.py"
_spec = importlib.util.spec_from_file_location("paired_tuner_probe", _SCRIPT)
probe = importlib.util.module_from_spec(_spec)
# dataclass 는 cls.__module__ 로 sys.modules 를 되찾으므로, exec 전에 등록하지
# 않으면 모듈 안의 @dataclass 가 AttributeError 로 터진다.
sys.modules["paired_tuner_probe"] = probe
_spec.loader.exec_module(probe)


NETLIST = """* two device deck
.subckt AMP in out
M1 out in 0 0 nfet W=8 L=0.5
M2 out out 0 0 nfet W=4 L=0.5
.ends
Xa in out AMP
V1 in 0 DC 1
.end
"""


def _judge(pm_pass: bool, pm_value: float) -> dict:
    return {
        "overall_pass": pm_pass,
        "summary": "s",
        "criteria": [
            {
                "name": "phase_margin",
                "actual": pm_value,
                "target": 60.0,
                "margin": pm_value - 60.0,
                "pass": pm_pass,
            }
        ],
    }


def _change(refdes: str, param: str, old: str, new: str) -> dict:
    return {
        "refdes": refdes,
        "param": param,
        "old_value": old,
        "new_value": new,
        "reasoning": "r",
    }


def _events(*items) -> list[dict]:
    return list(items)


def _attempt_log(outer: int, retry: int, total: int, by_outcome: dict) -> dict:
    rendered = min(total, 30)
    return {
        "step": "attempt_log",
        "outer_iter": outer,
        "retry": retry,
        "total": total,
        "by_outcome": by_outcome,
        "rendered": rendered,
        "dropped": total - rendered,
    }


def _proposal(outer: int, retry: int, changes: list[dict]) -> dict:
    return {
        "step": "tuning_proposal",
        "outer_iter": outer,
        "retry": retry,
        "proposed_changes": changes,
        "confidence": 0.5,
        "overall_reasoning": "r",
    }


def _gate(step: str, outer: int, retry: int, approved: bool, feedback=None) -> dict:
    return {
        "step": step,
        "outer_iter": outer,
        "retry": retry,
        "approved": approved,
        "feedback": feedback,
    }


def _gates_ok(outer: int, retry: int) -> list[dict]:
    return [
        _gate("area_check", outer, retry, True),
        _gate("refdes_check", outer, retry, True),
        _gate("param_check", outer, retry, True),
        _gate("stimulus_check", outer, retry, True),
    ]


# --------------------------------------------------------------- 값 정규화


def test_a_numeric_value_compares_by_number_not_by_spelling():
    assert probe.normalize_value("14") == probe.normalize_value("14.0")
    assert probe.normalize_value(" 14 ") == probe.normalize_value("14")


def test_a_value_only_one_side_can_parse_is_never_equal():
    # "14" 와 "14u" 는 한쪽만 float 이 된다. 태그가 없으면 문자열 비교로
    # 떨어지지만, 태그가 있어야 그 이유가 기록에 남는다.
    assert probe.normalize_value("14") != probe.normalize_value("14u")
    assert probe.normalize_value("14u") == probe.normalize_value("14u")


# --------------------------------------------------------------- 채점


def test_the_same_value_returning_scores_as_r_exact():
    failed_knobs = {("AMP.M1", "W")}
    failed_triples = {probe.triple_of("AMP.M1", "W", "14")}
    result = probe.score_changes(
        [_change("AMP.M1", "W", "8", "14")], failed_knobs, failed_triples
    )
    assert result["any_exact"] is True
    assert result["any_knob"] is False
    assert result["exact_changes"] == [{"refdes": "AMP.M1", "param": "W", "new_value": "14"}]


def test_the_same_knob_with_a_different_value_scores_as_r_knob_only():
    failed_knobs = {("AMP.M1", "W")}
    failed_triples = {probe.triple_of("AMP.M1", "W", "14")}
    result = probe.score_changes(
        [_change("AMP.M1", "W", "8", "16")], failed_knobs, failed_triples
    )
    assert result["any_exact"] is False
    assert result["any_knob"] is True


def test_a_value_that_differs_only_in_spelling_is_still_r_exact():
    failed_triples = {probe.triple_of("AMP.M1", "W", "14")}
    result = probe.score_changes(
        [_change("AMP.M1", "W", "8", "14.0")], {("AMP.M1", "W")}, failed_triples
    )
    assert result["any_exact"] is True


def test_an_untouched_knob_scores_as_neither():
    result = probe.score_changes(
        [_change("AMP.M2", "W", "4", "6")],
        {("AMP.M1", "W")},
        {probe.triple_of("AMP.M1", "W", "14")},
    )
    assert result["any_exact"] is False
    assert result["any_knob"] is False


# --------------------------------------------------------------- 재생


def test_a_verify_pre_rejection_becomes_a_rejected_attempt_the_next_timepoint_sees():
    events = _events(
        {"step": "judge", "outer_iter": 1, **_judge(False, 30.0)},
        _attempt_log(1, 1, 0, {}),
        _proposal(1, 1, [_change("AMP.M1", "W", "8", "14")]),
        *_gates_ok(1, 1),
        _gate("verify_pre", 1, 1, False, "no"),
        _attempt_log(1, 2, 1, {"rejected": 1}),
        _proposal(1, 2, [_change("AMP.M1", "W", "8", "16")]),
    )
    timepoints, final = probe.replay(events, {"tb": NETLIST}, run_label="r")

    assert probe.verify_replay(timepoints) == []
    assert len(timepoints) == 2
    assert timepoints[0].history == []
    assert [a.outcome for a in timepoints[1].history] == ["rejected"]
    assert timepoints[1].history[0].reason == "verify_pre"
    assert timepoints[1].failed_knobs == {("AMP.M1", "W")}
    assert timepoints[1].failed_triples == {probe.triple_of("AMP.M1", "W", "14")}
    # 아무것도 적용되지 않았으므로 덱은 그대로다.
    assert final == {"tb": NETLIST}


def test_a_gate_rejection_records_every_change_with_the_gates_reason_code():
    events = _events(
        {"step": "judge", "outer_iter": 1, **_judge(False, 30.0)},
        _attempt_log(1, 1, 0, {}),
        _proposal(1, 1, [_change("AMP.M1", "W", "8", "99"), _change("AMP.M2", "W", "4", "40")]),
        _gate("area_check", 1, 1, False, "too big"),
        _attempt_log(1, 2, 2, {"rejected": 2}),
        _proposal(1, 2, [_change("AMP.M1", "W", "8", "10")]),
    )
    timepoints, _ = probe.replay(events, {"tb": NETLIST}, run_label="r")
    assert probe.verify_replay(timepoints) == []
    assert {a.reason for a in timepoints[1].history} == {"area"}
    assert timepoints[1].failed_knobs == {("AMP.M1", "W"), ("AMP.M2", "W")}
    # 게이트 피드백이 다음 시점의 rejection_feedback 으로 이어진다.
    assert timepoints[1].rejection_feedback == "too big"


def test_a_rollback_makes_the_knob_failed_and_leaves_the_deck_where_it_was():
    events = _events(
        {"step": "judge", "outer_iter": 1, **_judge(False, 30.0)},
        _attempt_log(1, 1, 0, {}),
        _proposal(1, 1, [_change("AMP.M1", "W", "8", "20")]),
        *_gates_ok(1, 1),
        _gate("verify_pre", 1, 1, True, "ok"),
        {"step": "judge", "outer_iter": 1, "post_tuning": True, **_judge(False, 25.0)},
        {"step": "verify_post", "outer_iter": 1, "recommendation": "rollback", "feedback": "worse"},
        {"step": "judge", "outer_iter": 2, **_judge(False, 30.0)},
        _attempt_log(2, 1, 1, {"rolled_back": 1}),
        _proposal(2, 1, [_change("AMP.M1", "W", "8", "20")]),
    )
    timepoints, final = probe.replay(events, {"tb": NETLIST}, run_label="r")
    assert probe.verify_replay(timepoints) == []
    assert [a.outcome for a in timepoints[1].history] == ["rolled_back"]
    assert timepoints[1].failed_triples == {probe.triple_of("AMP.M1", "W", "20")}
    assert final == {"tb": NETLIST}
    # 새 outer iteration 은 rejection_feedback 을 지운다.
    assert timepoints[1].rejection_feedback is None


def test_a_kept_change_is_applied_to_the_deck_the_next_timepoint_is_shown():
    events = _events(
        {"step": "judge", "outer_iter": 1, **_judge(False, 30.0)},
        _attempt_log(1, 1, 0, {}),
        _proposal(1, 1, [_change("AMP.M1", "W", "8", "20")]),
        *_gates_ok(1, 1),
        _gate("verify_pre", 1, 1, True, "ok"),
        {"step": "judge", "outer_iter": 1, "post_tuning": True, **_judge(False, 40.0)},
        {"step": "verify_post", "outer_iter": 1, "recommendation": "keep", "feedback": "better"},
        {"step": "judge", "outer_iter": 2, **_judge(False, 40.0)},
        _attempt_log(2, 1, 1, {"kept": 1}),
        _proposal(2, 1, [_change("AMP.M1", "W", "20", "24")]),
    )
    timepoints, final = probe.replay(events, {"tb": NETLIST}, run_label="r")
    assert probe.verify_replay(timepoints) == []
    assert "W=20" in timepoints[1].netlist_texts["tb"]
    assert "W=20" in final["tb"]
    # kept 는 실패가 아니므로 failed 집합에 들어가지 않는다.
    assert timepoints[1].failed_knobs == set()
    # 델타는 judge 숫자에서 나온다.
    assert timepoints[1].history[0].deltas == (("phase_margin", 10.0),)


def test_a_kept_attempt_alone_does_not_qualify_a_timepoint():
    """히스토리가 있어도 전부 kept 면 R_exact 의 분자가 구조적으로 0 이다 -
    첫 측정을 무효로 만든 바로 그 모양이므로 시점에서 뺀다."""
    events = _events(
        {"step": "judge", "outer_iter": 1, **_judge(False, 30.0)},
        _attempt_log(1, 1, 0, {}),
        _proposal(1, 1, [_change("AMP.M1", "W", "8", "20")]),
        *_gates_ok(1, 1),
        _gate("verify_pre", 1, 1, True, "ok"),
        {"step": "judge", "outer_iter": 1, "post_tuning": True, **_judge(False, 40.0)},
        {"step": "verify_post", "outer_iter": 1, "recommendation": "keep", "feedback": "better"},
        {"step": "judge", "outer_iter": 2, **_judge(False, 40.0)},
        _attempt_log(2, 1, 1, {"kept": 1}),
        _proposal(2, 1, [_change("AMP.M1", "W", "20", "24")]),
    )
    timepoints, _ = probe.replay(events, {"tb": NETLIST}, run_label="r")
    assert probe.select_timepoints(timepoints) == []


def test_a_replay_that_disagrees_with_the_runs_attempt_log_is_reported_not_ignored():
    events = _events(
        {"step": "judge", "outer_iter": 1, **_judge(False, 30.0)},
        _attempt_log(1, 1, 0, {}),
        _proposal(1, 1, [_change("AMP.M1", "W", "8", "14")]),
        *_gates_ok(1, 1),
        _gate("verify_pre", 1, 1, False, "no"),
        # 런은 2건을 봤다고 기록했는데 재생은 1건이다.
        _attempt_log(1, 2, 2, {"rejected": 2}),
        _proposal(1, 2, [_change("AMP.M1", "W", "8", "16")]),
    )
    timepoints, _ = probe.replay(events, {"tb": NETLIST}, run_label="r")
    problems = probe.verify_replay(timepoints)
    assert any("total" in p for p in problems)
    assert any("by_outcome" in p for p in problems)


def test_a_topology_event_stops_the_replay_rather_than_being_skipped():
    events = _events(
        {"step": "judge", "outer_iter": 1, **_judge(False, 30.0)},
        {"step": "topology_swap", "outer_iter": 1, "block_path": "AMP", "topology_id": "x"},
    )
    with pytest.raises(ValueError):
        probe.replay(events, {"tb": NETLIST}, run_label="r")


# --------------------------------------------------------------- 통계


def test_mcnemar_uses_the_exact_binomial_and_ignores_concordant_pairs():
    # 불일치가 없으면 p=1.
    assert probe.mcnemar_exact(0, 0) == 1.0
    # 10 대 0 은 2 * 2^-10.
    assert probe.mcnemar_exact(10, 0) == pytest.approx(2 * (0.5**10))
    # 대칭이다 - 방향은 비율이 말하고 p 는 크기만 말한다.
    assert probe.mcnemar_exact(3, 9) == probe.mcnemar_exact(9, 3)
    assert probe.mcnemar_exact(1, 1) == 1.0


def test_the_verdict_follows_the_preregistered_rule():
    points = [
        probe.Timepoint(
            run="r",
            outer_iter=1,
            retry=2,
            judge_result=_judge(False, 30.0),
            rejection_feedback=None,
            netlist_texts={"tb": NETLIST},
            history=[],
            logged_attempt_log=None,
            actual_changes=[],
        )
    ]

    def rec(arm, repeat, exact):
        return {
            "run": "r",
            "outer_iter": 1,
            "retry": 2,
            "arm": arm,
            "repeat": repeat,
            "result": {"any_exact": exact, "any_knob": False, "proposed_changes": []},
        }

    # A 가 10회 전부 R_exact, B 가 0회 -> 효과 있음.
    records = []
    for i in range(10):
        records.append(rec("A", i, True))
        records.append(rec("B", i, False))
    summary = probe.analyse(points, records, repeats=10)
    assert summary["r_exact"]["table"] == {"both": 0, "a_only": 10, "b_only": 0, "neither": 0}
    assert summary["r_exact"]["mcnemar_exact_p"] < 0.05
    assert summary["verdict"] == "D1 효과 있음"

    # 완전 동률 -> 효과 없음.
    tie = []
    for i in range(10):
        tie.append(rec("A", i, True))
        tie.append(rec("B", i, True))
    assert probe.analyse(points, tie, repeats=10)["verdict"] == "효과 없음"


def test_a_dropped_call_removes_its_pair_rather_than_counting_as_zero():
    points = [
        probe.Timepoint(
            run="r",
            outer_iter=1,
            retry=2,
            judge_result=_judge(False, 30.0),
            rejection_feedback=None,
            netlist_texts={"tb": NETLIST},
            history=[],
            logged_attempt_log=None,
            actual_changes=[],
        )
    ]
    records = [
        {"run": "r", "outer_iter": 1, "retry": 2, "arm": "A", "repeat": 0,
         "result": {"any_exact": True, "any_knob": False, "proposed_changes": []}},
        {"run": "r", "outer_iter": 1, "retry": 2, "arm": "B", "repeat": 0, "result": None,
         "error": "AgentExecutionError: boom"},
        {"run": "r", "outer_iter": 1, "retry": 2, "arm": "A", "repeat": 1,
         "result": {"any_exact": True, "any_knob": False, "proposed_changes": []}},
        {"run": "r", "outer_iter": 1, "retry": 2, "arm": "B", "repeat": 1,
         "result": {"any_exact": False, "any_knob": True, "proposed_changes": []}},
    ]
    summary = probe.analyse(points, records, repeats=2)
    assert summary["pairs"] == 1
    assert summary["r_exact"]["a_rate"] == 1.0
    assert summary["r_exact"]["b_rate"] == 0.0


# --------------------------------------------------------------- 종단 확인


def test_the_final_deck_check_catches_a_reconstruction_that_drifted(tmp_path):
    (tmp_path / "netlist_v9_tb.cir").write_text("* different\n")
    (tmp_path / "result.json").write_text(
        json.dumps({"final_netlist_paths": {"tb": str(tmp_path / "netlist_v9_tb.cir")}})
    )
    assert probe.check_final_deck(tmp_path, {"tb": NETLIST}) != []
    assert probe.check_final_deck(tmp_path, {"tb": "* different\n"}) == []
