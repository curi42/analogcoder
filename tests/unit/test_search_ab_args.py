"""`search_ab.py`의 인자 파싱. 스크립트를 import 해서 순수 함수만 부른다 -
시뮬레이션은 돌리지 않는다."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from search_ab import parse_corner_regime  # noqa: E402


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
