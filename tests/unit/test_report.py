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
    # 단계 수와 면적은 렌더된 문장 그대로 못박는다. `"4" in content`는
    # netlist_v4_...cir이, `"7" in content`는 -87.75가 이미 만족시켜서
    # 두 줄을 통째로 지워도 통과했다.
    assert "4 accepted, 7 rejected" in content
    assert "4e-12" in content and "3.1e-12" in content        # 면적 before/after
    # 최적화가 돌았다는 사실만 적고 확인 여부를 빼면, 코너를 못 버틴 설계와
    # 확인된 설계가 같은 리포트를 낸다. `"True" in content`는 헤딩
    # `**Corner confirmed:**` 자체가 만족시키므로 값까지 붙여 못박는다.
    assert "**Corner confirmed:** True" in content


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


CORNER_REDUCTION_RESULT = {
    **SAMPLE_RESULT,
    "corner_reduction": {
        "active": True,
        "reason": None,
        "final_set": ["(deck)", "ss/1.62/27.0", "ff/1.98/27.0"],
        "attempts": 2,
        "area_baselines": 3,
        "grown": [["ff/1.98/27.0"]],
        "path_disagreement": None,
        "unattributed_failures": None,
        "reentry_skipped": None,
        "argmax_drift": {"criteria": [], "moved_count": 0, "total": 0},
    },
}


def test_write_report_md_reports_the_corner_reduction_phase(tmp_path):
    """`area_baselines > 1`은 사람이 result.json을 열지 않고 알아야 하는 사실이다.

    재진입할 때마다 면적 게이트의 기준선이 다시 잡히므로, 한 소자가 원래 덱에
    대해 허용받는 성장은 tier^(area_baselines)가 된다. 그 사실은 PASS로 끝난
    실행에서 리포트에 **한 줄도** 나타나지 않았다 - 이 저장소에서 면적 게이트가
    조용히 안 걸린 것이 네 번이고 네 번 다 로그에 안 보였다.

    **어떤 변형을 잡는가.** `_corner_reduction_lines`를 통째로 지우거나
    `write_report_md`에서 그 호출을 빼는 변형.
    """
    path = write_report_md(str(tmp_path), CORNER_REDUCTION_RESULT)
    with open(path) as f:
        content = f.read()
    assert "## Corner reduction" in content
    assert "**Active:** True" in content
    assert "3 corners" in content          # final_set 크기
    assert "**Re-entry attempts:** 2" in content
    assert "**Area-gate baselines:** 3" in content
    assert "re-anchored" in content        # 1보다 크다는 사실의 의미


def test_write_report_md_says_nothing_about_corner_reduction_when_the_key_is_absent(tmp_path):
    # corner_reduction 키가 없는 결과(예: 씨앗 실패 이전의 옛 실행, 다른 호출부)
    # 에 빈 섹션을 그리면 "돌았는데 아무것도 못 했다"로 읽힌다.
    path = write_report_md(str(tmp_path), SAMPLE_RESULT)
    with open(path) as f:
        content = f.read()
    assert "Corner reduction" not in content


def test_write_report_md_reports_an_inactive_corner_reduction_with_its_reason(tmp_path):
    result = {
        **SAMPLE_RESULT,
        "corner_reduction": {
            **CORNER_REDUCTION_RESULT["corner_reduction"],
            "active": False,
            "reason": "the spec declares no pvt_corners",
            "final_set": [],
            "attempts": 0,
            "area_baselines": 1,
        },
    }
    path = write_report_md(str(tmp_path), result)
    with open(path) as f:
        content = f.read()
    assert "**Active:** False" in content
    assert "no pvt_corners" in content


def test_write_report_md_surfaces_a_path_disagreement_and_a_skipped_re_entry(tmp_path):
    # 둘 다 "왜 재진입이 더 안 일어났는가"를 설명하는 사실이고, attempts 숫자만
    # 보고는 구별되지 않는다.
    result = {
        **SAMPLE_FAIL_RESULT,
        "corner_reduction": {
            **CORNER_REDUCTION_RESULT["corner_reduction"],
            "path_disagreement": {"criteria": ["gain"], "corners": ["fs/1.98/125.0"]},
            "reentry_skipped": {
                "attempt": 0,
                "orchestration_status": "FAIL",
                "orchestration_failure_reason": "max iterations reached",
            },
        },
    }
    path = write_report_md(str(tmp_path), result)
    with open(path) as f:
        content = f.read()
    assert "Path disagreement" in content
    assert "gain" in content
    assert "fs/1.98/125.0" in content
    assert "Re-entry skipped" in content
    assert "max iterations reached" in content
