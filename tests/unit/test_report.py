import json
import os

from analogcoder.report import write_report_md, write_result_json

SAMPLE_RESULT = {
    "status": "PASS",
    "final_netlist_paths": {
        "ac_loop_gain": "runs/abc123/netlist_v1_ac_loop_gain.cir",
        "psr_plus": "runs/abc123/netlist_v1_psr_plus.cir",
    },
    "run_dir": "runs/abc123",
    "iterations_used": 2,
    "final_criteria": [{"name": "gain", "target": ">=19.5", "actual": 20.0, "pass": True, "margin": 0.5}],
}

SAMPLE_FAIL_RESULT = {
    "status": "FAIL",
    "final_netlist_paths": {"ac_loop_gain": "runs/abc123/netlist_v3_ac_loop_gain.cir"},
    "run_dir": "runs/abc123",
    "iterations_used": 10,
    "final_criteria": [{"name": "gain", "target": ">=19.5", "actual": 15.0, "pass": False, "margin": -4.5}],
    "failure_reason": "max iterations reached",
}


def test_write_result_json(tmp_path):
    path = write_result_json(str(tmp_path), SAMPLE_RESULT)
    assert os.path.basename(path) == "result.json"
    with open(path) as f:
        assert json.load(f) == SAMPLE_RESULT


def test_write_report_md_includes_status_criteria_and_every_testbench_netlist(tmp_path):
    path = write_report_md(str(tmp_path), SAMPLE_RESULT)
    with open(path) as f:
        content = f.read()
    assert "PASS" in content
    assert "gain" in content
    assert "[PASS] gain" in content
    assert "ac_loop_gain" in content
    assert "netlist_v1_ac_loop_gain.cir" in content
    assert "psr_plus" in content
    assert "netlist_v1_psr_plus.cir" in content


def test_write_report_md_includes_failure_reason(tmp_path):
    path = write_report_md(str(tmp_path), SAMPLE_FAIL_RESULT)
    with open(path) as f:
        content = f.read()
    assert "max iterations reached" in content
    assert "[FAIL] gain" in content


# --- 최종 리뷰 Finding 2: 리포트가 최적화를 한 줄도 말하지 않았다 ------------
# write_report_md는 final_criteria와 final_netlist_paths만 그린다. 최적화가
# 돌아 넷리스트가 바뀌어도 리포트에는 그 사실이 없고, 실측 bandgap 실행에서는
# 212.25uA를 재는 넷리스트 경로 옆에 212.99uA가 적혔다.

SAMPLE_OPTIMIZED_RESULT = {
    "status": "PASS",
    "final_netlist_paths": {"ac_loop_gain": "runs/abc123/netlist_v4_ac_loop_gain.cir"},
    "run_dir": "runs/abc123",
    "iterations_used": 2,
    "final_criteria": [
        {"name": "iq", "target": "<=300.0", "actual": 212.25, "pass": True, "margin": -87.75}
    ],
    "optimization": {
        "status": "OPTIMIZED",
        "objective_before": 212.9881,
        "objective_after": 212.2517,
        "area_before": 4.0e-12,
        "area_after": 3.1e-12,
        "steps_accepted": 4,
        "steps_rejected": 7,
        "corner_confirmed": True,
        "corner_failure": None,
        "failure": None,
        "guard_infeasible": [],
        "area_coverage": {"counted": 40, "skipped": 0, "budget_enforced": True, "reason": None},
        "pvt_sweep": None,
        "final_netlist_paths": {},
    },
}


def test_write_report_md_reports_the_optimization_phase(tmp_path):
    path = write_report_md(str(tmp_path), SAMPLE_OPTIMIZED_RESULT)
    with open(path) as f:
        content = f.read()

    assert "Optimization" in content
    assert "OPTIMIZED" in content
    assert "212.9881" in content and "212.2517" in content   # before/after
    assert "4" in content and "7" in content                 # 수락/거절 단계 수
    assert "corner" in content.lower()
    # 최적화가 돌았다는 사실만 적고 확인 여부를 빼면, 코너를 못 버틴 설계와
    # 확인된 설계가 같은 리포트를 낸다.
    assert "True" in content or "confirmed" in content.lower()


def test_write_report_md_says_nothing_about_optimization_when_it_did_not_run(tmp_path):
    # 최적화가 없었던 실행에 빈 섹션을 그리면, 리포트를 훑는 사람이 "돌았는데
    # 아무것도 못 했다"로 읽는다.
    path = write_report_md(str(tmp_path), SAMPLE_RESULT)
    with open(path) as f:
        content = f.read()

    assert "Optimization" not in content


def test_write_report_md_reports_an_optimization_that_could_not_run(tmp_path):
    # 이 단계에는 FAIL 결말이 없으므로 실행은 여전히 PASS로 끝난다 - 그래서
    # 리포트가 말하지 않으면 최적화가 통째로 죽은 것을 아무도 모른다.
    result = {
        **SAMPLE_OPTIMIZED_RESULT,
        "optimization": {
            **SAMPLE_OPTIMIZED_RESULT["optimization"],
            "status": "UNCHANGED",
            "steps_accepted": 0,
            "failure": "AgentExecutionError: backend returned output that does not match the schema",
        },
    }

    path = write_report_md(str(tmp_path), result)
    with open(path) as f:
        content = f.read()

    assert "does not match the schema" in content
