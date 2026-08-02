import json
import math
import os
import subprocess

import pytest

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
        # **`len(grown) == attempts` 는 T19 이후 실행이 지키는 불변식이므로
        # 정본 픽스처도 그것을 지켜야 한다.** 예전 이 픽스처는 `attempts: 2`
        # 인데 `grown` 이 한 항목이고 `promotion_reentries` 키가 아예 없었다 -
        # 즉 **지금 실행이 낼 수 없는 모양**이었다. 아래 세 테스트가 이것을
        # `**CORNER_REDUCTION_RESULT[...]` 로 상속하므로, 다음 사람이 이것을
        # 정본 result 모양으로 읽을 확률이 오히려 올라간 상태였다.
        "grown": [["ff/1.98/27.0"], []],
        "promotion_reentries": [
            {"attempt": 2, "criteria": ["gain_db"], "corners": ["ss/1.62/27.0"]}
        ],
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


def test_write_report_md_puts_a_blank_line_after_the_growth_bullet_list(tmp_path):
    """M3(T19 리뷰): `write_report_md`는 `"\\n".join(lines)`로 쓴다. 새 성장
    불릿 목록(`- attempt N: ...`) 바로 다음 줄이 다른 굵은 줄(`**Area-gate
    baselines:**` 등)이면, 커먼마크의 lazy continuation 규칙 때문에 그 줄이
    마지막 불릿의 문단으로 흡수돼 더 이상 독립된 줄로 렌더링되지 않는다 -
    `**Area-gate baselines:**`는 `_corner_reduction_lines` 독스트링이 "이
    섹션이 존재하는 유일한 이유"라고 적은 줄이라 특히 중요하다.

    `CORNER_REDUCTION_RESULT`는 `seed` 키가 없어(스킵됨) 성장 목록 바로 뒤에
    `**Area-gate baselines:**`가 오는, 결함이 실제로 일어나는 정확한 인접
    상황이다. 문자열 부분 일치(`in content`)만으로는 이 결함을 못 잡는다 -
    흡수돼도 텍스트 자체는 여전히 어딘가에 나타나기 때문에, 줄 단위로 빈 줄이
    있는지 확인해야 한다.

    **반증 확인 대상**: 성장 블록 끝의 `lines.append("")`를 지우면 이 단언이
    실패한다."""
    path = write_report_md(str(tmp_path), CORNER_REDUCTION_RESULT)
    with open(path) as f:
        content_lines = f.read().splitlines()
    idx = next(i for i, line in enumerate(content_lines) if line.startswith("**Area-gate baselines:**"))
    assert content_lines[idx - 1] == ""


def test_write_report_md_draws_a_growth_line_for_a_pure_growth_attempt(tmp_path):
    """M10(T19): `grown`이 아예 그려지지 않던 결함. attempts > 0이면 무엇이
    더해졌는지 리포트에 한 줄 나와야 한다."""
    result = {
        **SAMPLE_RESULT,
        "corner_reduction": {
            **CORNER_REDUCTION_RESULT["corner_reduction"],
            "attempts": 1,
            "grown": [["ff/1.98/27.0"]],
            "promotion_reentries": [],
        },
    }
    path = write_report_md(str(tmp_path), result)
    with open(path) as f:
        content = f.read()
    assert "**Corner set growth:**" in content
    assert "attempt 1: added ff/1.98/27.0" in content


def test_write_report_md_marks_a_promotion_reentry_attempt_distinctly_from_a_growth_attempt(
    tmp_path,
):
    """M10(T19) 핵심: 승격 재진입 attempt는 `grown`에 빈 리스트를 신는다 -
    그것을 "아무것도 안 했다"로 그리면 승격 재진입이 리포트에서 완전히
    사라진다. `promotion_reentries`로 그 attempt를 짚어 성장 attempt와
    구별되게 그린다.

    **반증 확인 대상**: `promotion_reentries`를 무시하고 `grown[i-1]`만 보는
    변형으로 되돌리면, attempt 2가 "no corner added"류의 일반 문구가 되고
    "promoted by the probe"/구체적 기준·코너가 사라져 이 단언들이 실패한다.
    """
    result = {
        **SAMPLE_RESULT,
        "corner_reduction": {
            **CORNER_REDUCTION_RESULT["corner_reduction"],
            "attempts": 2,
            "grown": [["ff/1.98/27.0"], []],
            "promotion_reentries": [
                {"attempt": 2, "criteria": ["trim_phase_margin"], "corners": ["ss/1.98/27.0"]}
            ],
        },
    }
    path = write_report_md(str(tmp_path), result)
    with open(path) as f:
        content = f.read()
    assert "attempt 1: added ff/1.98/27.0" in content
    assert "attempt 2: no corner added" in content
    assert "trim_phase_margin at ss/1.98/27.0" in content
    assert "promoted by the probe" in content


def test_write_report_md_draws_no_promotion_line_when_promotion_reentries_is_empty(tmp_path):
    """`promotion_reentries`가 비어 있으면 그 사실에 대한 줄을 그리지 않는다 -
    이 섹션의 기존 규칙(path_disagreement/reentry_skipped와 같다)과 같다.

    attempt 2가 코너를 하나도 더하지 않았는데(`grown[1] == []`)
    `promotion_reentries`도 비어 있는 경우로 짠다 - `grown`의 빈 리스트만 보고
    "승격 재진입이었을 것"이라고 추측하는 변형이라면 여기서 "promoted by the
    probe"를 잘못 그린다."""
    result = {
        **SAMPLE_RESULT,
        "corner_reduction": {
            **CORNER_REDUCTION_RESULT["corner_reduction"],
            "attempts": 2,
            "grown": [["ff/1.98/27.0"], []],
            "promotion_reentries": [],
        },
    }
    path = write_report_md(str(tmp_path), result)
    with open(path) as f:
        content = f.read()
    assert "promoted by the probe" not in content


def test_write_report_md_says_nothing_about_corner_reduction_when_the_key_is_absent(tmp_path):
    # corner_reduction 키가 없는 결과(예: 씨앗 실패 이전의 옛 실행, 다른 호출부)
    # 에 빈 섹션을 그리면 "돌았는데 아무것도 못 했다"로 읽힌다.
    path = write_report_md(str(tmp_path), SAMPLE_RESULT)
    with open(path) as f:
        content = f.read()
    assert "Corner reduction" not in content


def test_write_report_md_names_the_argmax_seeding_mode(tmp_path):
    """IMPORTANT 6: result.json/report.md만 보는 사람이 지금까지 argmax와
    ε-coverage 중 무엇이 돌았는지 알 방법이 없었다 - `corner_seed`는
    history.jsonl에만 남았다."""
    result = {
        **SAMPLE_RESULT,
        "corner_reduction": {
            **CORNER_REDUCTION_RESULT["corner_reduction"],
            "seed": {"mode": "argmax", "epsilon": None, "tau": None, "dropped": []},
        },
    }
    path = write_report_md(str(tmp_path), result)
    with open(path) as f:
        content = f.read()
    assert "**Corner seed:** argmax" in content


def test_write_report_md_names_epsilon_tau_and_dropped_count_in_coverage_mode(tmp_path):
    result = {
        **SAMPLE_RESULT,
        "corner_reduction": {
            **CORNER_REDUCTION_RESULT["corner_reduction"],
            "seed": {
                "mode": "coverage", "epsilon": 0.05, "tau": 1.0,
                "dropped": ["fs/1.98/125.0", "ss/1.62/27.0"],
            },
        },
    }
    path = write_report_md(str(tmp_path), result)
    with open(path) as f:
        content = f.read()
    assert "**Corner seed:** coverage" in content
    assert "epsilon=0.05" in content
    assert "tau=1.0" in content
    assert "dropped 2 argmax corner(s)" in content


def test_an_old_result_json_without_promotion_reentries_says_so_per_attempt(tmp_path):
    """옛 `result.json`(승격 재진입 필드가 생기기 전)을 재렌더할 때의 경로.

    **이 테스트가 왜 따로 필요한가.** `"no growth record available"` 분기는
    `attempts` 가 `grown` 항목보다 많고 그 attempt 의 승격 기록도 없을 때만
    돈다. T19 이후의 실행은 `len(grown) == attempts` 를 지키고 승격 attempt 에
    `promotion_reentries` 항목을 넣으므로 **그 상태를 낼 수 없다.** 그래서 이
    분기의 유일한 실행 경로는 옛 result.json 이고, 그것을 정본 픽스처로
    흉내내면 픽스처가 "지금 실행이 낼 수 없는 모양"이 된다 - 최종 리뷰가 잡은
    M5 가 정확히 그 상태였다.

    분기를 지우거나 문구를 바꾸면 이 테스트가 실패한다. 그리고 이 자리에서
    숫자를 **지어내지 않는다**는 것이 요점이다: 기록이 없으면 없다고 적는다.
    """
    result = {
        **SAMPLE_RESULT,
        "corner_reduction": {
            "active": True,
            "reason": None,
            "final_set": ["(deck)", "ss/1.62/27.0"],
            "attempts": 2,
            "area_baselines": 2,
            # 옛 실행의 모양: attempt 2개인데 성장 기록은 하나뿐이고
            # `promotion_reentries` 키가 아예 없다.
            "grown": [["ff/1.98/27.0"]],
            "path_disagreement": None,
            "unattributed_failures": None,
            "reentry_skipped": None,
            "argmax_drift": {"criteria": [], "moved_count": 0, "total": 0},
        },
    }
    path = write_report_md(str(tmp_path), result)
    with open(path) as f:
        content = f.read()

    assert "- attempt 1: added ff/1.98/27.0" in content
    assert "- attempt 2: no growth record available" in content


def test_write_report_md_says_nothing_about_the_seed_when_the_key_is_absent(tmp_path):
    # 재개 회차에 다시 안 뽑았거나 축소 자체가 꺼진 경우 - 뽑지 않은 것을 뽑은
    # 것처럼 적지 않는다(최적화/토폴로지/재개 섹션과 같은 규칙).
    result = {
        **SAMPLE_RESULT,
        "corner_reduction": {**CORNER_REDUCTION_RESULT["corner_reduction"], "seed": None},
    }
    path = write_report_md(str(tmp_path), result)
    with open(path) as f:
        content = f.read()
    assert "Corner seed" not in content


def test_write_report_md_names_points_per_tb_in_argmax_mode(tmp_path):
    """T4: `points_per_tb`가 이 기법의 알고리즘 지표다(웨이브 수는 워커 수에
    딸린 배포 사실이라 지표가 아니다) - 리포트만 보는 사람에게는 지금까지
    안 보였다."""
    result = {
        **SAMPLE_RESULT,
        "corner_reduction": {
            **CORNER_REDUCTION_RESULT["corner_reduction"],
            "seed": {
                "mode": "argmax", "epsilon": None, "tau": None, "dropped": [],
                "points_per_tb": 8,
            },
        },
    }
    path = write_report_md(str(tmp_path), result)
    with open(path) as f:
        content = f.read()
    assert "points_per_tb=8" in content


def test_write_report_md_names_points_per_tb_in_coverage_mode(tmp_path):
    result = {
        **SAMPLE_RESULT,
        "corner_reduction": {
            **CORNER_REDUCTION_RESULT["corner_reduction"],
            "seed": {
                "mode": "coverage", "epsilon": 0.05, "tau": 1.0,
                "dropped": ["fs/1.98/125.0", "ss/1.62/27.0"],
                "points_per_tb": 5, "reached_target": True,
            },
        },
    }
    path = write_report_md(str(tmp_path), result)
    with open(path) as f:
        content = f.read()
    assert "points_per_tb=5" in content


def test_write_report_md_flags_a_coverage_seed_that_missed_its_target(tmp_path):
    """목표 피복률에 못 미친 씨앗이 성공처럼 읽히면 안 된다 - `reached_target`이
    False일 때 눈에 띄어야 한다."""
    result = {
        **SAMPLE_RESULT,
        "corner_reduction": {
            **CORNER_REDUCTION_RESULT["corner_reduction"],
            "seed": {
                "mode": "coverage", "epsilon": 0.05, "tau": 1.0,
                "dropped": [], "points_per_tb": 3, "reached_target": False,
            },
        },
    }
    path = write_report_md(str(tmp_path), result)
    with open(path) as f:
        content = f.read()
    assert "reached_target=False" in content
    assert "did NOT reach" in content or "DID NOT" in content.upper()


def test_write_report_md_does_not_flag_a_coverage_seed_that_reached_its_target(tmp_path):
    result = {
        **SAMPLE_RESULT,
        "corner_reduction": {
            **CORNER_REDUCTION_RESULT["corner_reduction"],
            "seed": {
                "mode": "coverage", "epsilon": 0.05, "tau": 1.0,
                "dropped": [], "points_per_tb": 3, "reached_target": True,
            },
        },
    }
    path = write_report_md(str(tmp_path), result)
    with open(path) as f:
        content = f.read()
    assert "reached_target=True" in content
    assert "did NOT reach" not in content


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


# --- 최종 리뷰 I-3: 토폴로지 스왑이 result.json에도 report.md에도 없었다 ------
# 실측 실행에서 BUF_P의 16소자 본문이 극성도 사이징도 다른 본문으로 통째로
# 교체됐는데 두 산출물 어디에도 그 사실이 없다. 최적화 단계에서 이미 같은 값을
# 치른 모양이다("결과는 자기가 돌려주는 덱을 설명해야 한다").

SAMPLE_SWAPPED_RESULT = {
    **SAMPLE_RESULT,
    "topology_swaps": [
        {
            "attempt": 0,
            "outer_iter": 4,
            "block_path": "BUF_P",
            "topology_id": "folded_cascode_pmos_in_cs",
            "unconstrained_refdes": 14,
            "stale_baseline_refdes": 2,
            "outcome": "kept",
        },
        # 코너 축소 재진입이 붙으면 outer_iter가 시도마다 1부터 다시 세므로,
        # 시도 번호 없이는 이 줄과 위 줄이 같은 iteration으로 보인다.
        {
            "attempt": 1,
            "outer_iter": 4,
            "block_path": "TRIMAMP",
            "topology_id": "folded_cascode_nmos_in_cs",
            "unconstrained_refdes": 3,
            "stale_baseline_refdes": 0,
            "outcome": "rolled_back",
        },
    ],
}


def test_write_report_md_reports_every_topology_swap(tmp_path):
    """어떤 변형을 잡는가: `_topology_lines`를 통째로 지우거나
    `write_report_md`에서 그 호출을 빼는 변형, 그리고 유지/롤백 결말이나
    면적 개수를 빼는 변형. 블록 경로가 없으면 bandgap처럼 앰프가 넷인 덱에서
    어느 블록이 바뀌었는지 리포트만 보고는 알 수 없다."""
    path = write_report_md(str(tmp_path), SAMPLE_SWAPPED_RESULT)
    with open(path) as f:
        content = f.read()

    assert "## Topology swaps" in content
    assert "BUF_P" in content and "folded_cascode_pmos_in_cs" in content
    assert "TRIMAMP" in content and "folded_cascode_nmos_in_cs" in content
    assert "attempt 0, iteration 4" in content
    assert "attempt 1, iteration 4" in content
    # 유지된 스왑과 되돌린 스왑은 다른 사실이다 - 둘을 구별하지 않으면 리포트가
    # 실제로 출하된 덱을 잘못 설명한다.
    assert "(kept)" in content
    assert "(rolled_back)" in content
    # 면적 게이트가 이 실행의 나머지 구간에서 무엇을 더 이상 묶지 못하는가.
    assert "14 refdes unconstrained, 2 with a stale baseline" in content
    assert "3 refdes unconstrained, 0 with a stale baseline" in content


def test_write_report_md_says_nothing_about_topology_when_no_swap_happened(tmp_path):
    # 스왑이 없었던 실행에 빈 섹션을 그리면 "스왑을 시도했는데 아무것도 못
    # 했다"로 읽힌다 - 최적화/코너 축소 섹션과 같은 규칙.
    path = write_report_md(str(tmp_path), {**SAMPLE_RESULT, "topology_swaps": []})
    with open(path) as f:
        content = f.read()
    assert "Topology" not in content


# ---------------------------------------------------------------- 재개


def test_a_run_that_was_not_resumed_draws_no_resume_section(tmp_path):
    """`resumed_from`은 결과에 **항상** 실리지만(null 포함), 리포트는 재개한
    실행에만 섹션을 그린다 - 최적화/코너 축소 섹션과 같은 규칙이다."""
    result = {**SAMPLE_RESULT, "resumed_from": None}
    path = write_report_md(str(tmp_path), result)
    with open(path) as f:
        content = f.read()
    assert "## Resume" not in content


def test_a_resumed_run_says_where_it_resumed_and_what_was_abandoned(tmp_path):
    result = {
        **SAMPLE_RESULT,
        "resumed_from": {
            "boundary": "outer_iteration",
            "attempt": 0,
            "outer_iter": 3,
            "checkpoint_path": "runs/abc123/checkpoint.json",
            "discarded_lines": [42, 53],
            "discarded_events": 11,
            "resume_count": 1,
        },
    }
    path = write_report_md(str(tmp_path), result)
    with open(path) as f:
        content = f.read()
    assert "## Resume" in content
    assert "outer_iteration" in content
    assert "iteration 3" in content
    assert "Abandoned history lines:** 11" in content
    assert "lines 42-52" in content
    assert "Resumes so far (this run dir):** 1" in content


def test_a_resume_that_abandoned_nothing_says_so(tmp_path):
    result = {
        **SAMPLE_RESULT,
        "resumed_from": {
            "boundary": "attempt",
            "attempt": 1,
            "outer_iter": None,
            "checkpoint_path": "runs/abc123/checkpoint.json",
            "discarded_lines": [40, 40],
            "discarded_events": 0,
            "resume_count": 2,
        },
    }
    path = write_report_md(str(tmp_path), result)
    with open(path) as f:
        content = f.read()
    assert "Abandoned history lines:** 0" in content


# --- 감사 2.1: 리포트가 **판정을 내리는** PVT 스윕을 한 줄도 그리지 않았다 -----
#
# 이 픽스처의 숫자는 지어낸 것이 아니라 `runs/pvt_sonnet_1/result.json`에서
# 그대로 옮긴 것이다. 그 실행을 옛 코드로 렌더링하면 `**Status:** FAIL` 아래에
# 7개 기준이 **전부 `[PASS]`**로 적히고(중간 루프 judge의 값), `[FAIL]`은 0줄,
# `corner` 문자열 0회, 최악 코너 좌표 0회다. 같은 파일의 `pvt_sweep`은 7개
# 전부 FAIL이고 dc_gain은 71.09 -> 3.14 dB로 붕괴했다. 리포트만 읽는 사람은
# "기준은 다 통과했는데 왜 FAIL이지"가 된다.
#
# "결과는 자기가 낸 덱을 설명해야 한다"의 **네 번째** 재발이고, 유일하게 실제
# 산출물에서 관측된 재발이다.

PVT_SONNET_1_SWEEP = {
    "overall_pass": False,
    "criteria": [
        {"name": "dc_gain", "target": ">=60.0", "actual": 3.13783, "pass": False, "margin": -56.86217},
        {"name": "unity_gain_bandwidth", "target": ">=1500000.0", "actual": math.nan, "pass": False, "margin": math.nan},
        {"name": "phase_margin", "target": ">=60.0", "actual": math.nan, "pass": False, "margin": math.nan},
        {"name": "psr_plus", "target": "<=-10.0", "actual": 26.2352, "pass": False, "margin": 36.2352},
        {"name": "psr_minus", "target": "<=0.0", "actual": 9.53353, "pass": False, "margin": 9.53353},
        {"name": "settling_time_hi", "target": "<=2.8e-06", "actual": math.nan, "pass": False, "margin": math.nan},
        {"name": "settling_time_lo", "target": "<=2.8e-06", "actual": math.nan, "pass": False, "margin": math.nan},
    ],
    "summary": "one or more criteria failed",
    "worst_case_corners": {
        "dc_gain": {"process": "fs", "voltage": 1.98, "temperature": 125.0, "value": 3.13783},
        "unity_gain_bandwidth": {"process": "ss", "voltage": 1.62, "temperature": 125.0, "value": None},
        "phase_margin": {"process": "ss", "voltage": 1.62, "temperature": 125.0, "value": None},
        "psr_plus": {"process": "sf", "voltage": 1.62, "temperature": -40.0, "value": 26.2352},
        "psr_minus": {"process": "sf", "voltage": 1.8, "temperature": -40.0, "value": 9.53353},
        "settling_time_hi": {"process": "tt", "voltage": 1.98, "temperature": 125.0, "value": None},
        "settling_time_lo": {"process": "tt", "voltage": 1.98, "temperature": 125.0, "value": None},
    },
}

PVT_SONNET_1_RESULT = {
    "status": "FAIL",
    "final_netlist_paths": {"ac_loop_gain": "runs/pvt_sonnet_1/netlist_v2_ac_loop_gain.cir"},
    "run_dir": "runs/pvt_sonnet_1",
    "iterations_used": 10,
    # 중간 루프 judge가 **명목 한 점**에서 낸 값. 전부 통과다.
    "final_criteria": [
        {"name": "dc_gain", "target": ">=60.0", "actual": 71.0861, "pass": True, "margin": 11.0861},
        {"name": "unity_gain_bandwidth", "target": ">=1500000.0", "actual": 2010000.0, "pass": True, "margin": 510000.0},
        {"name": "phase_margin", "target": ">=60.0", "actual": 65.2, "pass": True, "margin": 5.2},
        {"name": "psr_plus", "target": "<=-10.0", "actual": -15.12, "pass": True, "margin": -5.12},
        {"name": "psr_minus", "target": "<=0.0", "actual": -3.36, "pass": True, "margin": -3.36},
        {"name": "settling_time_hi", "target": "<=2.8e-06", "actual": 1.9e-06, "pass": True, "margin": -9e-07},
        {"name": "settling_time_lo", "target": "<=2.8e-06", "actual": 1.8e-06, "pass": True, "margin": -1e-06},
    ],
    "failure_reason": "final PVT sweep failed: one or more criteria failed",
    "pvt_sweep": PVT_SONNET_1_SWEEP,
}


def test_the_report_draws_the_pvt_sweep_that_decides_the_verdict(tmp_path):
    """`runs/pvt_sonnet_1`을 렌더링하면 FAIL 7줄과 최악 코너 좌표가 보여야 한다.

    **어떤 변형을 잡는가**: `_pvt_lines`를 통째로 지우거나 `write_report_md`에서
    그 호출을 빼는 변형, 그리고 판정 스윕의 기준을 그리면서 **어느 코너에서**
    깨졌는지를 빼는 변형. 코너 좌표가 없으면 "45개 중 어디가 문제인가"를
    result.json을 열어야만 알 수 있고, 그것이 이 섹션이 존재하는 이유다.
    """
    path = write_report_md(str(tmp_path), PVT_SONNET_1_RESULT)
    with open(path) as f:
        content = f.read()

    assert "## PVT sweep" in content
    # 옛 코드의 실측: FAIL 0줄. 판정을 내린 스윕은 7개 전부 FAIL이다.
    assert content.count("[FAIL]") >= 7
    for name in (
        "dc_gain",
        "unity_gain_bandwidth",
        "phase_margin",
        "psr_plus",
        "psr_minus",
        "settling_time_hi",
        "settling_time_lo",
    ):
        assert f"[FAIL] {name}" in content
    # 붕괴한 값 자체.
    assert "3.13783" in content
    # 옛 코드의 실측: `corner` 0회, `fs/1.98/125.0` 0회.
    assert "corner" in content
    assert "fs/1.98/125.0" in content
    assert "sf/1.62/-40.0" in content
    assert "sf/1.8/-40.0" in content


def test_a_worst_corner_with_no_value_is_not_reported_as_an_argmax(tmp_path):
    """`value: None`은 "이 코너가 최악이었다"가 아니라 "여기서 처음으로 측정이
    안 나왔다"이다(`pvt.worst_case_measurements`의 `missing_corners[0]`).

    둘을 같은 문장으로 적으면 리포트가 데이터에 없는 구조적 주장을 한다 -
    이 저장소가 `OPAMP2STAGE drives vdd,vss`에서 이미 치른 값이다.
    """
    path = write_report_md(str(tmp_path), PVT_SONNET_1_RESULT)
    with open(path) as f:
        content = f.read()

    lines = [line for line in content.splitlines() if "unity_gain_bandwidth" in line and "[FAIL]" in line]
    assert lines, content
    line = lines[0]
    assert "ss/1.62/125.0" in line
    assert "no measurement" in line
    assert "worst at" not in line


def test_the_report_says_nothing_about_a_pvt_sweep_that_did_not_run(tmp_path):
    """키가 없으면 빈 목록 - 최적화/코너 축소/토폴로지 섹션과 같은 규칙이다.
    돌지 않은 단계에 빈 섹션을 그리면 "돌았는데 아무것도 못 했다"로 읽힌다."""
    path = write_report_md(str(tmp_path), SAMPLE_RESULT)
    with open(path) as f:
        content = f.read()
    assert "PVT sweep" not in content


def test_a_passing_pvt_sweep_is_drawn_too(tmp_path):
    """PASS로 끝난 실행에서도 그린다. "스윕이 통과했다"와 "스윕이 안 돌았다"가
    같은 침묵이면 안 된다 - 이 저장소가 게이트에 대해 아홉 번 치른 값이다."""
    sweep = {
        "overall_pass": True,
        "criteria": [{"name": "gain", "target": ">=19.5", "actual": 20.0, "pass": True, "margin": 0.5}],
        "summary": "all criteria passed",
        "worst_case_corners": {"gain": {"process": "ss", "voltage": 1.62, "temperature": 125.0, "value": 20.0}},
    }
    path = write_report_md(str(tmp_path), {**SAMPLE_RESULT, "pvt_sweep": sweep})
    with open(path) as f:
        content = f.read()
    assert "## PVT sweep" in content
    assert "[PASS] gain" in content
    assert "ss/1.62/125.0" in content


# --- 감사 2.1의 더 깊은 절반: "Final criteria"가 어느 조건의 측정인가 --------
#
# 후보가 셋이다. 라벨이 없으면 판정 스윕과 나란히 놓인 두 표가 서로 다른 회로를
# 설명하고 있다는 사실이 보이지 않는다.


def test_final_criteria_says_it_is_the_deck_the_tuning_loop_returned(tmp_path):
    """LLM judge가 제거된 뒤 "누가 판정했는가" 축은 세 후보 모두
    `evaluate_criteria` 하나로 접힌다. 그러므로 이 줄이 구별해야 하는 것은
    **어느 덱을** 판정했는가이고, 그것을 단언한다 - `evaluate_criteria`만
    확인하면 세 후보 중 어느 것에서도 통과하는 단언이 된다."""
    path = write_report_md(str(tmp_path), SAMPLE_RESULT)
    with open(path) as f:
        content = f.read()
    assert "## Final criteria" in content
    assert "the deck the tuning loop returned" in content
    assert "evaluate_criteria" in content
    # 코너를 렌더링하지 않은 덱 한 점.
    assert "no corner rendering" in content
    # 그리고 최적화가 착지한 버전이 **아니다**.
    assert "optimization phase landed" not in content


def test_final_criteria_says_when_it_is_the_reduced_corner_sets_worst_case(tmp_path):
    """코너 축소가 켜져 있으면 중간 루프 judge가 본 값은 명목 한 점이 아니라
    **선택 집합의 최악값**이다(`corner_sim.build_corner_simulate`). 그 사실이
    라벨에 없으면 판정 스윕과의 차이가 "코너를 안 봤다"로 잘못 읽힌다."""
    path = write_report_md(str(tmp_path), CORNER_REDUCTION_RESULT)
    with open(path) as f:
        content = f.read()
    # **Final criteria 블록 안에서** 확인한다. `"3 corners"`는 아래 Corner
    # reduction 섹션이 이미 만족시키므로, 라벨을 통째로 지워도 통과하는
    # 단언이 된다.
    head = content.split("## Corner reduction")[0]
    assert "reduced corner set" in head
    assert "3 corners" in head


def test_final_criteria_says_when_it_came_from_the_optimization_phases_landing(tmp_path):
    """`cli.py`는 최적화가 기준을 재고 왔으면 `final_criteria`를 **덮는다**.
    그때 이 표는 LLM judge의 것이 아니라 `evaluate_criteria`가 낸,
    bisection이 착지한 버전의 판정이다."""
    result = {
        **SAMPLE_OPTIMIZED_RESULT,
        "optimization": {
            **SAMPLE_OPTIMIZED_RESULT["optimization"],
            "final_criteria": SAMPLE_OPTIMIZED_RESULT["final_criteria"],
        },
    }
    path = write_report_md(str(tmp_path), result)
    with open(path) as f:
        content = f.read()
    assert "optimization phase landed" in content
    assert "evaluate_criteria" in content


def test_final_criteria_points_at_the_pvt_sweep_when_one_decided_the_verdict(tmp_path):
    """이 결함의 핵심 증상 - 사람이 "기준은 다 통과했는데 왜 FAIL이지"가 되는
    것 - 을 막는 한 줄. 스윕이 없으면 이 줄도 없어야 한다(없는 섹션을
    가리키면 안 된다)."""
    path = write_report_md(str(tmp_path), PVT_SONNET_1_RESULT)
    with open(path) as f:
        content = f.read()
    # **Final criteria 블록 안에서만** 본다. 맨 아래 `failure_reason`이
    # "final PVT sweep failed: ..."라 전문 검색은 이 줄을 지워도 통과한다.
    block = content.split("## Final criteria", 1)[1].split("\n## ", 1)[0]
    assert "PVT sweep" in block
    assert "not the verdict" in block

    # 스윕이 없으면 없는 섹션을 가리키지 않는다.
    path = write_report_md(str(tmp_path), SAMPLE_RESULT)
    with open(path) as f:
        content = f.read()
    assert "PVT sweep" not in content


# --- 감사 2.3: result.json이 RFC 8259 JSON이 아니다 ---------------------------
#
# `judge_tools.evaluate_criteria`는 측정이 없는 기준에 `math.nan`을 싣고,
# `pvt.corner_severity`는 `-math.inf`를 낸다 - 둘 다 **정상 경로**다. 실측:
# `runs/pvt_sonnet_1/result.json`에 리터럴 `NaN`이 8개 있고 node의
# `JSON.parse`가 파일 전체를 SyntaxError로 거부한다. jq 1.7.1은 거부하지 않고
# `-Infinity`를 `-1.797e308`로 **조용히 바꿔 준다**.


def _reject_non_rfc(token):
    raise AssertionError(f"non-RFC 8259 constant in the artifact: {token}")


def test_result_json_is_valid_rfc_8259_even_with_nan_and_infinity(tmp_path):
    """**어떤 변형을 잡는가**: `write_result_json`에서 정규화나
    `allow_nan=False` 중 하나를 빼는 변형. 둘 다 있어야 한다 - 정규화가 없으면
    `allow_nan=False`가 리포트까지 날리는 ValueError가 되고, `allow_nan=False`가
    없으면 나중에 정규화를 우회하는 경로가 조용히 비표준 JSON을 낸다."""
    result = {
        **SAMPLE_FAIL_RESULT,
        "pvt_sweep": PVT_SONNET_1_SWEEP,
        "per_corner_severity": [-math.inf, 0.5, math.inf],
    }
    path = write_result_json(str(tmp_path), result)
    text = open(path).read()

    # 파이썬 엄격 파서: 비-RFC 상수가 하나라도 있으면 터진다.
    json.loads(text, parse_constant=_reject_non_rfc)
    # 리터럴 토큰이 남아 있지 않은지 직접 확인 - `"NaN"`(따옴표 포함)은 유효한
    # JSON 문자열이므로 bare 토큰만 잡아야 한다.
    assert ": NaN" not in text and ": Infinity" not in text and ": -Infinity" not in text


def test_a_non_finite_measurement_is_not_flattened_into_null(tmp_path):
    """`null`은 "그 필드가 없다", `NaN`은 "쟀는데 값이 안 나왔다"로 **다른
    사실**이다. 이 저장소는 그 구별로 여러 번 값을 치렀다
    (`corner_unattributed_failure`, `deltas_between`이 없는 기준을 0.0으로 안
    읽는 것). 산출물 형식에서 그 실수를 다시 하지 않는다.

    같은 파일 안에 진짜 `null`(측정이 없어서 최악 코너에 값이 안 붙은 항목)과
    NaN이 함께 있으므로, 둘이 여전히 구별되는지를 한 파일에서 확인한다.
    """
    result = {**SAMPLE_FAIL_RESULT, "pvt_sweep": PVT_SONNET_1_SWEEP}
    path = write_result_json(str(tmp_path), result)
    loaded = json.loads(open(path).read(), parse_constant=_reject_non_rfc)

    ugbw = loaded["pvt_sweep"]["criteria"][1]
    assert ugbw["name"] == "unity_gain_bandwidth"
    assert ugbw["actual"] is not None          # null로 접히지 않았다
    assert ugbw["actual"] == "NaN"             # "쟀는데 값이 안 나왔다"
    # ...그리고 "그 필드에 값이 없다"는 여전히 null이다.
    assert loaded["pvt_sweep"]["worst_case_corners"]["unity_gain_bandwidth"]["value"] is None


def test_a_finite_result_round_trips_unchanged(tmp_path):
    """정규화가 유한 값을 건드리면 안 된다 - 이 저장소의 산출물 대부분이
    그것이다."""
    path = write_result_json(str(tmp_path), SAMPLE_RESULT)
    assert json.loads(open(path).read(), parse_constant=_reject_non_rfc) == SAMPLE_RESULT


@pytest.mark.skipif(
    subprocess.run(["which", "node"], capture_output=True).returncode != 0,
    reason="node not on PATH",
)
def test_result_json_parses_in_node(tmp_path):
    """감사가 실제로 실패를 관측한 파서. 파이썬의 `json.loads`는 bare `NaN`을
    받아 주기 때문에 파이썬만으로는 이 결함이 보이지 않는다."""
    result = {**SAMPLE_FAIL_RESULT, "pvt_sweep": PVT_SONNET_1_SWEEP}
    path = write_result_json(str(tmp_path), result)
    proc = subprocess.run(
        ["node", "-e", f"JSON.parse(require('fs').readFileSync({path!r}, 'utf8')); console.log('ok')"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def _dir(tmp_path, name):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _result(**overrides):
    base = {
        "status": "FAIL",
        "iterations_used": 3,
        "final_netlist_paths": {"ac": "/runs/r1/netlist_v0_ac.cir"},
        "final_criteria": [],
    }
    base.update(overrides)
    return base


def test_a_run_blocked_at_every_gate_reads_differently_from_one_that_tuned(tmp_path):
    """감사 §3.8. 이 표가 없을 때 두 실행의 리포트는 구조적으로 **동일**했다 -
    상태와 기준 표만으로는 "덱이 한 번도 안 바뀌었다"를 알 수 없다."""
    blocked = write_report_md(_dir(tmp_path, "a"), _result(attempt_summary={
        "changes": 30,
        "by_outcome": {"kept": 0, "rolled_back": 0, "rejected": 30},
        "rejected_by_reason": {"area": 30, "refdes": 0, "param": 0, "stimulus": 0, "verify_pre": 0},
    }))
    tuned = write_report_md(_dir(tmp_path, "b"), _result(attempt_summary={
        "changes": 26,
        "by_outcome": {"kept": 12, "rolled_back": 8, "rejected": 6},
        "rejected_by_reason": {"area": 6, "refdes": 0, "param": 0, "stimulus": 0, "verify_pre": 0},
    }))

    a, b = open(blocked).read(), open(tuned).read()
    assert a != b
    assert "| kept | 0 |" in a and "| kept | 12 |" in b
    assert "| area | 30 |" in a


def test_an_all_zero_summary_is_still_drawn(tmp_path):
    """값이 전부 0이어도 그린다. 침묵이 "안 돌았다"를 뜻하는 규칙은 키의
    **부재**에만 걸린다 - 0을 접으면 "거부가 0건"과 "집계가 사라졌다"가 다시
    같은 침묵이 된다."""
    path = write_report_md(_dir(tmp_path, "c"), _result(attempt_summary={
        "changes": 0,
        "by_outcome": {"kept": 0, "rolled_back": 0, "rejected": 0},
        "rejected_by_reason": {"area": 0, "refdes": 0, "param": 0, "stimulus": 0, "verify_pre": 0},
    }))

    text = open(path).read()
    assert "## Tuning attempts" in text
    assert "**Component changes proposed:** 0" in text


def test_a_result_without_the_key_draws_no_section(tmp_path):
    """키의 부재는 "이 결과를 만든 코드가 집계를 아예 안 쓴다"이고, 그것은
    값이 0인 것과 다른 사실이다."""
    path = write_report_md(_dir(tmp_path, "d"), _result())

    assert "## Tuning attempts" not in open(path).read()


# --- Task 5: 면적 최소화 단계가 리포트에도 그려져야 한다 ----------------------
#
# 브리프의 원래 테스트 본문은 `iterations_used`/`final_netlist_paths`가 없는
# 맨 dict를 바로 `write_report_md`에 넘겼다 - 실제로 돌려 보면 의도한
# `AssertionError: assert '면적 최소화' in ...`가 아니라 `KeyError:
# 'iterations_used'`로 죽는다(write_report_md가 그 두 키를 무조건 읽는다).
# 이 파일의 기존 관례(`_result`/`_dir` 헬퍼)를 따라 필수 키를 채워 넣는다 -
# 아래는 그 관례대로 고친 버전이다.


def test_the_report_draws_the_area_phase_including_when_it_changed_nothing(tmp_path):
    """아무것도 못 줄인 실행에서도 절이 나와야 한다.

    안 나오면 "면적 단계가 아무것도 못 했다"와 "면적 단계가 없다"가 보고서에서
    같은 모양이 된다. 이 저장소가 코너 스윕에서 이미 겪은 실수다.

    수락/거절 카운트는 진짜 키 이름(`steps_accepted`/`steps_rejected` -
    `optimizer._result`가 실제로 찍는 이름)으로 채우고 **렌더된 숫자까지**
    읽는다. 틀린 키 이름(`accepted`/`rejected`)을 썼던 원래 버전은
    `status`만 확인하는 단언으로는 안 잡혔다 - `.get(..., 0)`이 조용히 0을
    돌려주고, 마침 이 테스트가 기대하던 값과도 우연히 안 겹쳐서 처음
    발견됐지, 대부분의 실행에서는 진짜 0과 구별되지 않는다."""
    result = _result(
        status="PASS",
        area_optimization={
            "status": "UNCHANGED", "steps_accepted": 2, "steps_rejected": 3,
            "area_before": 41.0, "area_after": 41.0,
        },
    )
    path = write_report_md(_dir(tmp_path, "area_unchanged"), result)
    md = open(path, encoding="utf-8").read()
    assert "면적 최소화" in md
    assert "UNCHANGED" in md
    assert "수락 2건 / 거절 3건" in md


def test_the_report_says_when_the_area_phase_was_refused(tmp_path):
    """REFUSED는 UNCHANGED와 다른 문장으로 나와야 한다."""
    result = _result(
        status="PASS",
        area_optimization={
            "status": "REFUSED",
            "reason": "area model resolved no device on this deck (counted=0, skipped=2)",
        },
    )
    path = write_report_md(_dir(tmp_path, "area_refused"), result)
    md = open(path, encoding="utf-8").read()
    assert "REFUSED" in md and "counted=0" in md


def test_the_report_distinguishes_a_crashed_area_phase_from_a_clean_no_op(tmp_path):
    """`status="UNCHANGED"`는 두 다른 사실을 가릴 수 있다: "돌았고 줄일 것이
    없었다"와 "이 단계 자체가 터져서 아무것도 못 쟀다"(`run_area_optimization`의
    준비 구간이 `AgentExecutionError`/`ValueError`/`OSError`로 접히거나,
    `_optimize`의 기준선 시뮬레이션 자체가 실패한 경우 - 둘 다 status는
    UNCHANGED로 남는다. REFUSED와는 다른 경로다). 리포트가 그 차이를 아는
    유일한 자리이므로, `failure`가 있을 때만 나오는 문장이 있어야 한다.

    두 렌더를 `failure` 유무만 다르게 만들고 비교한다 - "그 절이 있다"만
    보는 단언은 아무것도 못 잡는다. `failure` 값 자체가 어느 쪽에만
    나타나는지를 잰다."""
    clean = {
        "status": "UNCHANGED", "steps_accepted": 0, "steps_rejected": 0,
        "area_before": 41.0, "area_after": 41.0,
    }
    crashed = {**clean, "failure": "AgentExecutionError: boom"}

    clean_path = write_report_md(
        _dir(tmp_path, "area_clean_noop"), _result(status="PASS", area_optimization=clean)
    )
    crashed_path = write_report_md(
        _dir(tmp_path, "area_crashed"), _result(status="PASS", area_optimization=crashed)
    )
    clean_md = open(clean_path, encoding="utf-8").read()
    crashed_md = open(crashed_path, encoding="utf-8").read()

    assert "AgentExecutionError: boom" not in clean_md
    assert "AgentExecutionError: boom" in crashed_md


def test_the_report_renders_the_area_phase_s_corner_failure_and_guard_infeasibility(tmp_path):
    """`corner_failure`/`guard_infeasible`은 튜닝 단계처럼 이 단계에서도 실제로
    채워질 수 있다 - 코너 확인 자체가 죽을 수 있고, 이 단계는 `guard_band=None`
    (`AREA_PHASE`)이라 비율 폴백이 없어 코너로 못 잰 기준은 여유분 0.0으로
    읽힌다(`guard_band_violations`). 렌더하지 않으면 이 두 사실은 result.json을
    열지 않는 한 어디에도 보이지 않는다."""
    result = _result(
        status="PASS",
        area_optimization={
            "status": "UNCHANGED", "steps_accepted": 0, "steps_rejected": 0,
            "area_before": 41.0, "area_after": 41.0,
            "corner_failure": "corner sweep raised RuntimeError: ngspice died",
            "guard_infeasible": ["iq_ua: measurement 'iq_ua' is missing"],
        },
    )
    path = write_report_md(_dir(tmp_path, "area_corner_guard"), result)
    md = open(path, encoding="utf-8").read()
    assert "ngspice died" in md
    assert "iq_ua" in md


def test_a_run_without_an_area_phase_gets_no_area_section(tmp_path):
    """키의 부재는 "이 실행에 면적 단계가 없었다"이고, 그것은 값이 아니다."""
    path = write_report_md(_dir(tmp_path, "area_absent"), _result(status="PASS"))
    assert "면적 최소화" not in open(path, encoding="utf-8").read()


def test_the_provenance_names_the_area_phase_when_only_it_moved_the_deck(tmp_path):
    """전류 단계가 없는 스펙에서도 표는 면적 단계의 덱을 설명해야 한다.

    `evaluate_criteria`라는 낱말은 네 출처 전부에 들어 있으므로 그것을 검사하는
    것은 아무것도 고정하지 못한다. 고정할 것은 **어느 단계를 지목하는가**다."""
    base = _result(status="PASS")
    only_area = write_report_md(
        _dir(tmp_path, "provenance_area_only"),
        {**base, "area_optimization": {"status": "ACCEPTED", "final_criteria": [{"name": "x"}]}},
    )
    both = write_report_md(
        _dir(tmp_path, "provenance_both"),
        {
            **base,
            "area_optimization": {"status": "ACCEPTED", "final_criteria": [{"name": "x"}]},
            "optimization": {"status": "ACCEPTED", "final_criteria": [{"name": "x"}]},
        },
    )
    assert "area phase landed on" in open(only_area, encoding="utf-8").read()
    # 전류 단계가 뒤에 돌므로 그쪽이 이긴다.
    both_md = open(both, encoding="utf-8").read()
    assert "optimization phase landed on" in both_md
    assert "area phase landed on" not in both_md
