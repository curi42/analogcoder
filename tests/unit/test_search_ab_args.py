"""`search_ab.py`의 인자 파싱. 스크립트를 import 해서 순수 함수만 부른다 -
시뮬레이션은 돌리지 않는다."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from search_ab import (  # noqa: E402
    _corner_capability,
    _count_compound_accepted,
    _eligibility_verdict,
    _steps_from_history,
    main,
    parse_corner_regime,
)


def test_argmax_means_no_coverage_config():
    assert parse_corner_regime("argmax") is None


def test_coverage_carries_epsilon_and_tau():
    cfg = parse_corner_regime("coverage:0.03:1.0")

    assert cfg.epsilon == 0.03 and cfg.tau == 1.0


@pytest.mark.parametrize("text", ["coverage", "coverage:0.03", "coverage:a:1.0", "nope"])
def test_an_unreadable_regime_is_refused(text):
    """조용히 argmax 로 떨어지면 두 쪽이 같은 체제로 돌면서 기록에는 다른
    이름이 실린다 - 격자의 셀 하나가 통째로 거짓이 된다."""
    with pytest.raises(ValueError):
        parse_corner_regime(text)


def _base_argv(tmp_path, **extra):
    argv = [
        "--spec", "does-not-need-to-exist.yaml",
        "--strategy", "coordinate_descent",
        "--strategy", "coordinate_descent",
        "--knob", "R1:value:increase",
        "--out-dir", str(tmp_path / "search_ab"),
    ]
    for flag, values in extra.items():
        for v in values:
            argv += [f"--{flag.replace('_', '-')}", v]
    return argv


def test_a_coverage_regime_is_refused_by_the_cli(tmp_path):
    """CRITICAL 1: 이 하니스는 run_optimization을 직접 부르고 corner_reduction의
    중간-루프를 거치지 않으므로 corner_reduction.coverage는 이 경로 어디에서도
    읽히지 않는다. 조용히 받아 놓으면 기록의 corner_regime 문자열만 다른 이름을
    달 뿐 실제로는 두 쪽이 같은(argmax) 코너로 돈다 - 그래서 argmax 외의
    체제는 시작 전에 거부되어야 한다."""
    argv = _base_argv(tmp_path, corner_regime=["coverage:0.03:1.0", "argmax"])

    with pytest.raises(SystemExit):
        main(argv)


def test_run_side_itself_refuses_a_non_argmax_regime(tmp_path):
    """T3(a): 거부가 `main()`에만 있으면 이 모듈을 직접 import해서 `run_side`를
    부르는 경로가 뚫린다 - 두 쪽이 같은 회로를 돌면서 기록의 `corner_regime`
    문자열만 다른 체제라고 주장하게 된다(#11이 정확히 이 모양이었다).
    `run_side`는 스펙을 읽기도 전에 이 자리에서 스스로 거부해야 한다."""
    from search_ab import run_side

    from analogcoder.spec import CoverageConfig

    class FakeArgs:
        spec = "does-not-need-to-exist.yaml"
        max_steps = 5
        sim_timeout = 10

    with pytest.raises(ValueError):
        run_side(
            "a", "coordinate_descent", FakeArgs(), [],
            str(tmp_path), CoverageConfig(epsilon=0.03, tau=1.0),
        )


def test_help_text_admits_that_coverage_is_rejected_before_it_is_wired_up(capsys):
    """T3(b): `metavar`와 help가 coverage를 받아들이는 것처럼 광고하는데
    `main()`은 그것을 거부한다 - 문구가 사실과 맞아야 한다. `parse_corner_regime`
    자체는 계속 coverage 문자열을 파싱할 수 있어야 하므로(위 테스트들), 여기서
    확인하는 것은 CLI가 사용자에게 하는 약속이 실제 동작과 맞는지다."""
    with pytest.raises(SystemExit):
        main(["--help"])
    help_text = capsys.readouterr().out
    assert "coverage" in help_text
    # coverage가 오늘 실제로 거부된다는 사실이 help 문구 자체에 있어야 한다 -
    # "받는 것처럼" 광고만 하고 그 뒤 main()이 조용히 막는 것은 문구가 거짓말을
    # 하는 것과 같다.
    assert "거부" in help_text or "reject" in help_text.lower()


def test_argmax_on_both_sides_is_still_accepted_by_the_parser(tmp_path):
    """회귀 안전선: 위 테스트가 --corner-regime 자체를 항상 거부하도록
    지나치게 넓게 고쳐지지 않았는지 확인한다."""
    argv = _base_argv(tmp_path, corner_regime=["argmax", "argmax"])

    # parser는 통과한다 - 그다음 실제 실행이 존재하지 않는 스펙 파일을 열려다
    # 실패하므로(load_spec), main() 자체는 예외로 끝난다. 여기서 보는 것은
    # "corner-regime 검증에서 거부되지 않았다"는 사실뿐이다.
    with pytest.raises(Exception) as exc_info:
        main(argv)
    assert not isinstance(exc_info.value, SystemExit)


# --- F1/F2/F3 리뷰 반영: 적격성 게이트의 순수 판정, --phase area + --knob
# 거부, _steps_from_history의 이벤트 이름 선택성, compound_steps_accepted의
# 셈법. 전부 ngspice 없이 순수 함수만 부른다. ---

_CAPABLE = {"pvt_corners_declared": True, "verify_corners_wired": True, "corner_capable": True}
_NO_PVT = {"pvt_corners_declared": False, "verify_corners_wired": False, "corner_capable": False}
_NO_WIRING = {"pvt_corners_declared": True, "verify_corners_wired": False, "corner_capable": False}

_ELIGIBILITY_KEYS = {
    "checked", "control_strategy", "control_side", "control_steps_accepted",
    "pvt_corners_declared", "verify_corners_wired", "corner_confirmed",
    "verdict", "reason",
}


def test_corner_capability_reads_pvt_corners_and_verify_corners_separately():
    """F1: optimizer.py:1757의 `corner_capable` 정의를 재유도하지 않고 그대로
    읽는다 - 두 사실이 따로 나와야 나중에 "무엇이 없어서 명목 전용이
    됐는가"를 가를 수 있다."""

    class FakeSpec:
        pvt_corners = None

    class FakeAgentsNoCorners:
        verify_corners = None

    result = _corner_capability(FakeSpec(), FakeAgentsNoCorners())
    assert result == {
        "pvt_corners_declared": False,
        "verify_corners_wired": False,
        "corner_capable": False,
    }

    class FakeSpecWithCorners:
        pvt_corners = object()

    class FakeAgentsWired:
        verify_corners = lambda *a, **k: None  # noqa: E731

    result = _corner_capability(FakeSpecWithCorners(), FakeAgentsWired())
    assert result == {
        "pvt_corners_declared": True,
        "verify_corners_wired": True,
        "corner_capable": True,
    }


@pytest.mark.parametrize(
    "corner_capability,control_side,control_steps,expected_verdict",
    [
        (_NO_PVT, None, None, "nominal_only"),
        (_NO_PVT, "a", None, "nominal_only"),
        (_NO_WIRING, "a", None, "nominal_only"),
        (_CAPABLE, None, None, "not_applicable"),
        (_CAPABLE, "a", 0, "void"),
        (_CAPABLE, "b", 0, "void"),
        (_CAPABLE, "a", 3, "eligible"),
    ],
)
def test_eligibility_verdict_picks_the_right_case(
    corner_capability, control_side, control_steps, expected_verdict
):
    """F1: 네 판정 모두 검사한다. `nominal_only`가 `not_applicable`/`void`보다
    먼저 판정돼야 한다 - corner_capable이 거짓이면 control_side/steps가 무엇을
    들고 있든 상관없이 nominal_only다(이 실행은 애초에 코너 인식 수락으로
    돌지 않는다)."""
    verdict = _eligibility_verdict(corner_capability, control_side, control_steps, None)
    assert verdict["verdict"] == expected_verdict


@pytest.mark.parametrize(
    "corner_capability,control_side,control_steps",
    [
        (_NO_PVT, None, None),
        (_NO_WIRING, "a", None),
        (_CAPABLE, None, None),
        (_CAPABLE, "a", 0),
        (_CAPABLE, "a", 3),
    ],
)
def test_every_eligibility_verdict_carries_the_same_keys(
    corner_capability, control_side, control_steps
):
    """표준 질문 3번: `checked`/`verdict`/`reason`이 통과·거부·미적용 어느
    경우에도 같은 모양으로 쓰여야 "게이트가 통과했다"와 "게이트가 없다"가
    history 만으로 구별된다."""
    verdict = _eligibility_verdict(corner_capability, control_side, control_steps, None)
    assert set(verdict.keys()) == _ELIGIBILITY_KEYS
    assert verdict["verdict"] is not None
    if verdict["verdict"] != "eligible":
        assert verdict["reason"] is not None


def test_eligibility_verdict_nominal_only_beats_void_mutation_would_be_caught():
    """변이 확인 1: `_eligibility_verdict`에서 `if not
    corner_capability["corner_capable"]:` 분기를 없애면(nominal_only를 판정
    안 하면) 이 시험이 실패해야 한다 - 실제로 그 분기를 주석 처리하고 돌려
    `void`가 나오는 것을 확인했다(아래 보고서 참고). 코드는 원복돼 있다."""
    verdict = _eligibility_verdict(_NO_PVT, "a", 0, None)
    assert verdict["verdict"] == "nominal_only"
    assert verdict["reason"] is not None and "pvt_corners" in verdict["reason"]


def test_steps_from_history_reads_the_event_name_given(tmp_path):
    """F3: 틀린 이름을 넘기면 빈 목록이 나오고, 그것이 "조합이 한 번도
    수락되지 않았다"와 구별되지 않는다는 것이 이 시험의 요지다."""
    history_file = tmp_path / "history.jsonl"
    history_file.write_text(
        '{"step": "optimize_step", "refdes": "R1", "accepted": true}\n'
        '{"step": "optimize_area_step", "refdes": "R2", "accepted": true}\n'
        '{"step": "some_other_event", "refdes": "R3"}\n'
    )

    class FakeState:
        history_path = str(history_file)

    objective_steps = _steps_from_history(FakeState(), "optimize_step")
    assert [s["refdes"] for s in objective_steps] == ["R1"]

    area_steps = _steps_from_history(FakeState(), "optimize_area_step")
    assert [s["refdes"] for s in area_steps] == ["R2"]

    # 틀린 이름(예: 상수 하나로 두 단계를 같이 읽으려는 실수)을 주면 실제로
    # 있는 스텝인데도 빈 목록이 나온다 - "조합이 한 번도 수락되지 않았다"와
    # 구별되지 않으므로 이것이 이 시험이 지키려는 것이다.
    assert _steps_from_history(FakeState(), "optimize_nonexistent_step") == []


def test_count_compound_accepted_ignores_single_knob_and_rejected_compound_steps():
    """F3: 단일 노브 수락(`changes` 없음)과 거절된 조합(`accepted=False`)을
    섞지 않는다."""
    steps = [
        {"accepted": True, "changes": None},              # 단일 노브 수락 - 제외
        {"accepted": True, "changes": []},                 # changes가 있어도 빈 리스트면 falsy - 제외
        {"accepted": False, "changes": [{"refdes": "X"}]},  # 거절된 조합 - 제외
        {"accepted": True, "changes": [{"refdes": "X"}, {"refdes": "Y"}]},  # 수락된 조합 - 포함
    ]
    assert _count_compound_accepted(steps) == 1


def test_count_compound_accepted_mutation_would_be_caught():
    """변이 확인 2: `_count_compound_accepted`에서 `s["accepted"] and`를
    빼면(수락 여부를 안 보면) 이 시험이 실패해야 한다 - 실제로 그렇게 바꿔
    돌려 2가 나오는 것을 확인했다(아래 보고서 참고). 코드는 원복돼 있다."""
    steps = [
        {"accepted": False, "changes": [{"refdes": "X"}, {"refdes": "Y"}]},
        {"accepted": True, "changes": [{"refdes": "X"}, {"refdes": "Y"}]},
    ]
    assert _count_compound_accepted(steps) == 1


def test_phase_area_with_knob_is_rejected_by_the_cli(tmp_path):
    """F2: run_area_optimization은 순위를 스스로 계산해 --knob으로 준 값을
    아무 데도 읽지 않는다 - 조용히 버리는 대신 경계에서 거부한다."""
    argv = [
        "--spec", "does-not-need-to-exist.yaml",
        "--strategy", "coordinate_descent",
        "--strategy", "compound_fallback_1",
        "--knob", "R1:value:increase",
        "--phase", "area",
        "--out-dir", str(tmp_path / "search_ab"),
    ]
    with pytest.raises(SystemExit):
        main(argv)


def test_phase_area_without_knob_passes_the_cli_gate(tmp_path):
    """회귀 안전선: 위 시험이 --phase area 자체를 항상 거부하도록 지나치게
    넓게 고쳐지지 않았는지 확인한다."""
    argv = [
        "--spec", "does-not-need-to-exist.yaml",
        "--strategy", "coordinate_descent",
        "--strategy", "compound_fallback_1",
        "--phase", "area",
        "--out-dir", str(tmp_path / "search_ab"),
    ]
    # parser는 통과한다 - 그다음 존재하지 않는 스펙 파일을 열려다 실패한다
    # (main()이 적격성 전제를 확인하려고 load_spec을 부른다). 여기서 보는
    # 것은 "--phase area + --knob 없음이 CLI 검증에서 거부되지 않았다"는
    # 사실뿐이다.
    with pytest.raises(Exception) as exc_info:
        main(argv)
    assert not isinstance(exc_info.value, SystemExit)
