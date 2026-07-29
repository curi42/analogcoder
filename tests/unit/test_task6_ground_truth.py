"""단계 0 · 태스크 6 의 측정 장치를 못박는다.

이 파일이 지키는 것은 거리 정의의 품질이 아니라 **채점이 가능하다는 사실**이다.
정답표가 조용히 비거나, 벤치마크 덱이 움직여 분모가 달라지거나, 근거 없는 항목이
근거 있는 것처럼 섞여 들어가면 이후의 모든 채점이 무의미해진다 — 그런데 그 셋
전부 아무 예외도 내지 않는다.

`scripts/score_knob_distance.py` 의 결정 지표도 여기서 못박는다. 그 숫자가
클린 체크아웃에서 재현되지 않으면 사전 등록이 가리킬 대상이 없다.
"""

from __future__ import annotations

import importlib.util
import json
import os

import pytest

from analogcoder.structure import derive_structure

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FIXTURE = os.path.join(REPO, "tests", "fixtures", "task6_ground_truth.json")
SCORER = os.path.join(REPO, "scripts", "score_knob_distance.py")

# 근거를 실어야 하는 항목 리스트. 여기 없는 리스트를 새로 추가하면 근거 검사가
# 그 리스트를 건너뛴다 - 새 리스트를 만들면 이 튜플에도 넣어라.
ENTRY_LISTS = (
    "correct_knobs",
    "wrong_block_controls",
    "inferred_knobs",
    "secondary_knobs",
    "known_wrong_knobs",
    "best_parameter_knobs_all_insufficient",
    "run_accepted_changes",
)


@pytest.fixture(scope="module")
def ground_truth() -> dict:
    with open(FIXTURE) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def scorer():
    spec = importlib.util.spec_from_file_location("score_knob_distance", SCORER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_ground_truth_parses_and_is_not_empty(ground_truth):
    assert ground_truth["schema_version"] >= 2
    # 여섯 케이스는 조사가 만든 수다. 줄어들면 그것이 사실의 삭제이고, 이 테스트가
    # 잡아야 하는 바로 그 조용한 손실이다.
    assert len(ground_truth["cases"]) == 6
    assert {c["case_id"] for c in ground_truth["cases"]} == {
        "bandgap_seed_buf0_droop",
        "bandgap_seed_tc",
        "bandgap_seed_trim_pm",
        "bandgap_seed_topology",
        "two_stage_opamp_phase_margin",
        "bandgap_optimizer_iq_knob_ranking",
    }


def test_every_case_names_files_that_exist(ground_truth):
    for case in ground_truth["cases"]:
        assert os.path.exists(os.path.join(REPO, case["spec_path"])), case["case_id"]
        if "canonical_netlist" in case:
            assert os.path.exists(os.path.join(REPO, case["canonical_netlist"])), case["case_id"]
        for crit in case.get("failing_criteria", []):
            assert os.path.exists(os.path.join(REPO, crit["netlist"])), crit["name"]


def test_the_denominator_matches_the_deck_it_claims_to_count(ground_truth):
    """분모는 정답표의 주장이 아니라 덱에서 파생되는 사실이다.

    벤치마크 덱에 소자가 하나 늘면 167 이 168 이 되고, 그러면 모든 순위가
    같은 이름 아래 다른 것을 뜻하게 된다. 조용히 그렇게 되는 것을 막는다.
    """
    for case in ground_truth["cases"]:
        if "canonical_netlist" not in case:
            continue
        with open(os.path.join(REPO, case["canonical_netlist"])) as f:
            structure = derive_structure(f.read(), "x")
        assert len(structure.tunable) == case["n_tunable_total"], case["case_id"]
        if "denominator" in case.get("scoring", {}):
            assert case["scoring"]["denominator"] == case["n_tunable_total"], case["case_id"]


def test_every_evidence_bearing_entry_declares_where_it_came_from(ground_truth):
    codes = set(ground_truth["provenance"]["codes"])
    seen = 0
    for case in ground_truth["cases"]:
        for key in ENTRY_LISTS:
            for entry in case.get(key, []):
                if not isinstance(entry, dict):
                    continue
                seen += 1
                where = f"{case['case_id']}.{key}"
                assert entry.get("evidence"), where
                assert entry.get("provenance"), where
                assert set(entry["provenance"]) <= codes, (where, entry["provenance"])
                assert entry.get("certainty"), where
    assert seen >= 25


def test_an_entry_with_no_source_says_so_rather_than_being_absent(ground_truth):
    """근거가 없다는 것은 필드의 부재가 아니라 기록되는 사실이다.

    `provenance == ["none"]` 항목이 0 개가 되면 그것은 표가 깨끗해진 것이
    아니라 표시가 사라진 것일 가능성이 높다 - 조사가 실제로 근거 없는 항목을
    셋 남겼다.
    """
    nones = [
        (case["case_id"], key, entry.get("refdes"))
        for case in ground_truth["cases"]
        for key in ENTRY_LISTS
        for entry in case.get(key, [])
        if isinstance(entry, dict) and entry.get("provenance") == ["none"]
    ]
    assert len(nones) == 3, nones


def test_run_log_evidence_is_marked_as_absent_from_a_clean_checkout(ground_truth):
    """`runs/` 는 .gitignore 에 있다.

    그 아래를 인용하는 근거는 이 기계의 작업 트리에서만 확인된다. 그 사실이
    provenance 문서에 적혀 있지 않으면, 다음 세션이 확인 불가능한 근거를
    확인된 것으로 읽는다.
    """
    doc = ground_truth["provenance"]["codes"]["run_log"]
    assert "gitignore" in doc
    assert any(
        "run_log" in entry.get("provenance", [])
        for case in ground_truth["cases"]
        for key in ENTRY_LISTS
        for entry in case.get(key, [])
        if isinstance(entry, dict)
    )


def test_every_knob_named_by_the_scoring_rules_exists_in_its_deck(ground_truth):
    """정답표가 존재하지 않는 (refdes, param) 을 정답이라 부르면, 채점기는
    그 노브를 영원히 못 찾고 그 케이스는 조용히 채점되지 않는다."""
    for case in ground_truth["cases"]:
        if "canonical_netlist" not in case:
            continue
        with open(os.path.join(REPO, case["canonical_netlist"])) as f:
            structure = derive_structure(f.read(), "x")
        index = {(t.refdes, t.param) for t in structure.tunable}
        sc = case.get("scoring", {})
        named: list[tuple[str, str]] = []
        for key in ("correct_set_refdes_param", "must_not_be_top_ranked",
                    "acceptable_secondary", "acceptable_but_insufficient"):
            named += [tuple(x) for x in sc.get(key, [])]
        for rset in sc.get("repair_sets", []):
            named += [tuple(x) for x in rset]
        for knob in named:
            assert knob in index, (case["case_id"], knob)


def test_the_repair_sets_are_present_for_exactly_the_scoreable_repair_cases(ground_truth):
    with_sets = {
        c["case_id"] for c in ground_truth["cases"] if c["scoring"].get("repair_sets")
    }
    assert with_sets == {
        "bandgap_seed_buf0_droop",
        "bandgap_seed_tc",
        "bandgap_seed_trim_pm",
        "two_stage_opamp_phase_margin",
    }
    for case in ground_truth["cases"]:
        sets = case["scoring"].get("repair_sets")
        if not sets:
            continue
        # 파생 근거가 없는 수리 집합은 새 판단이지 정답표의 사실이 아니다.
        assert case["scoring"].get("repair_sets_derivation"), case["case_id"]
        assert all(len(s) >= 1 for s in sets)


def test_the_decision_metric_reproduces_the_numbers_the_investigation_reported(scorer):
    """세 정의의 결정 지표를 못박는다 (2026-07-29 실측, 재현 가능).

    `hop` 과 `logdeg` 가 바이트 동일하다는 것이 `uniform` 을 기본값으로 고른
    근거이므로, 그 동일성이 깨지면 그 선택의 근거도 깨진다.
    """
    rows = {(r["case_id"], r["definition"]): r for r in scorer.score_all()}
    expected = {
        ("bandgap_seed_buf0_droop", "hop"): 10,
        ("bandgap_seed_buf0_droop", "logdeg"): 10,
        ("bandgap_seed_buf0_droop", "signal_flow"): 98,
        ("bandgap_seed_tc", "hop"): 10,
        ("bandgap_seed_tc", "logdeg"): 10,
        ("bandgap_seed_tc", "signal_flow"): 6,
        ("bandgap_seed_trim_pm", "hop"): 24,
        ("bandgap_seed_trim_pm", "logdeg"): 24,
        ("bandgap_seed_trim_pm", "signal_flow"): 12,
        ("two_stage_opamp_phase_margin", "hop"): 9,
        ("two_stage_opamp_phase_margin", "logdeg"): 9,
        ("two_stage_opamp_phase_margin", "signal_flow"): 9,
    }
    got = {k: rows[k]["repair_set_worst_rank"] for k in expected}
    assert got == expected


def test_the_focus_baseline_the_metric_has_to_beat_is_pinned_too(scorer):
    """비교 대상이 움직이면 '이겼다' 가 뜻을 잃는다. `tc` 의 103 -> 10 이 이
    태스크의 존재 이유이므로 103 쪽도 함께 못박는다."""
    rows = {r["case_id"]: r for r in scorer.score_all(("hop",))}
    assert {k: v["focus_knob_count"] for k, v in rows.items()} == {
        "bandgap_seed_buf0_droop": 32,
        "bandgap_seed_tc": 103,
        "bandgap_seed_trim_pm": 65,
        "two_stage_opamp_phase_margin": 30,
    }


def test_the_testbench_apparatus_ties_with_the_answer_in_every_definition(scorer):
    """전제 T1: 측정 노드에 직접 붙은 테스트벤치 장치(`Cload`, `Lfb`)는 어떤
    거리 정의에서도 정답과 동률이다. 이것은 고칠 결함이 아니라 사전에 인정하는
    사실이므로, 사라지면 그때 전제가 바뀐 것이다."""
    for definition in ("hop", "logdeg", "signal_flow"):
        row = next(
            r for r in scorer.score_all((definition,))
            if r["case_id"] == "two_stage_opamp_phase_margin"
        )
        answer = row["correct_knobs"]["OPAMP2STAGE.Xcc.w"]
        for name in ("Cload.value", "Lfb.value"):
            forbidden = row["forbidden_knobs"][name]
            assert forbidden["rank_best"] == answer["rank_best"], (definition, name)
            assert forbidden["rank_worst"] == answer["rank_worst"], (definition, name)


def test_the_optimizer_phase_has_no_source_net_at_all(scorer):
    """로드맵 176 행이 A/B 지점으로 지목한 자리의 부정 결과.

    목적함수 `iq_ua` 는 `i(Vdd)` 에서 나오고 `measurement_nets` 는 넷이 아니라
    전압원 **이름** 을 돌려준다. 구제 없이는 소스가 0 개다 - 순위가 없는 것이지
    나쁜 순위가 나오는 것이 아니다.
    """
    gt_case = next(
        c for c in scorer.load_ground_truth()["cases"]
        if c.get("phase") == "optimization"
    )
    for definition in ("hop", "logdeg", "signal_flow"):
        row = scorer.score_optimizer_case(gt_case, definition, rescue=False)
        assert row["source_names"] == ["Vdd"]
        assert row["source_names_unresolved"] == ["Vdd"]
        assert row["ranking_exists"] is False
        assert row["reference_rank_worst"] is None


def test_the_optimizer_phase_is_at_or_below_random_even_with_the_rescue(scorer):
    """구제(전압원 이름을 그 소자의 nodes[:2] 로 치환)를 켜도 열위다.

    채택 정의는 그 치환을 거부한다. 여기서만 켜는 이유는 '소스가 없어서 못
    쟀다' 가 '재 봤더니 나빴다' 를 가리지 않게 하기 위해서다.
    """
    gt_case = next(
        c for c in scorer.load_ground_truth()["cases"]
        if c.get("phase") == "optimization"
    )
    worst = {}
    for definition in ("hop", "logdeg", "signal_flow"):
        row = scorer.score_optimizer_case(gt_case, definition, rescue=True)
        worst[definition] = row["reference_rank_worst"]
        assert row["reference_rank_worst"] > row["random_expectation"], definition
    assert worst == {"hop": 158, "logdeg": 90, "signal_flow": 108}


def test_the_negative_control_still_points_at_the_block_the_swap_targets(scorer):
    """음성 대조군: 노브 수준 정답은 없지만 블록 판정은 유효하다."""
    gt_case = next(
        c for c in scorer.load_ground_truth()["cases"]
        if c["case_id"] == "bandgap_seed_topology"
    )
    for definition in ("hop", "logdeg", "signal_flow"):
        row = scorer.score_negative_control(gt_case, definition)
        assert row["nearest_blocks"] == ["BUF_P"], definition
        assert row["block_verdict_correct"] is True
