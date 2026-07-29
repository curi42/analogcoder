"""`search_ab.py`의 인자 파싱. 스크립트를 import 해서 순수 함수만 부른다 -
시뮬레이션은 돌리지 않는다."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from search_ab import main, parse_corner_regime  # noqa: E402


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
