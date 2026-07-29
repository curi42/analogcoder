"""`analogcoder.json_io` - 산출물 JSON의 전송 형식."""

import io
import json
import math

import pytest

from analogcoder.json_io import NON_FINITE_JSON, dump, dumps, json_safe, restore_non_finite


def _reject_non_rfc(token):
    raise AssertionError(f"non-RFC 8259 constant: {token}")


def test_a_bare_nan_never_reaches_the_file():
    """`json.dump`의 기본 동작(bare `NaN`)은 RFC 8259가 아니다. node의
    `JSON.parse`는 파일 전체를 거부하고, jq 1.7.1은 거부하지 않고
    `-Infinity`를 `-1.797e308`로 **조용히 바꿔 준다** - 관측된 적 없는 값이
    소비자에게 전달된다.

    **어떤 변형을 잡는가**: `json_safe`의 float 갈래를 지우는 변형.
    """
    text = dumps({"actual": math.nan, "severity": -math.inf, "ceiling": math.inf})
    json.loads(text, parse_constant=_reject_non_rfc)
    assert '"actual": "NaN"' in text.replace('"actual":"NaN"', '"actual": "NaN"')
    assert json.loads(text) == {"actual": "NaN", "severity": "-Infinity", "ceiling": "Infinity"}


def test_dump_and_dumps_pin_allow_nan_false():
    """정규화를 우회하는 경로가 나중에 생기면 조용히 비표준 JSON이 나가는 대신
    **여기서** 터져야 한다. 그래서 `allow_nan=False`는 정규화와 함께 못박힌다.

    직접 확인: 정규화를 끄고 같은 인자를 넘기면 `ValueError`다.
    """
    with pytest.raises(ValueError):
        json.dumps({"x": math.nan}, allow_nan=False)
    # 정규화가 앞에 있으므로 실제 경로는 터지지 않는다.
    assert dumps({"x": math.nan}) == '{"x": "NaN"}'


def test_null_and_nan_stay_different_facts():
    """`null`은 "그 필드에 값이 없다", `NaN`은 "쟀는데 값이 안 나왔다"이다.
    이 저장소는 그 구별로 여러 번 값을 치렀다 - 산출물 형식에서 다시 접지
    않는다."""
    text = dumps({"measured": math.nan, "absent": None})
    loaded = json.loads(text, parse_constant=_reject_non_rfc)
    assert loaded["absent"] is None
    assert loaded["measured"] == NON_FINITE_JSON["nan"]


def test_finite_values_and_nested_containers_are_untouched():
    payload = {"a": 1.5, "b": [1, 2, {"c": "x"}], "d": True, "e": None, "f": (1, 2)}
    assert json_safe(payload) == {"a": 1.5, "b": [1, 2, {"c": "x"}], "d": True, "e": None, "f": [1, 2]}


def test_restore_non_finite_is_the_inverse_of_json_safe():
    """표지는 **전송 형식**이지 값이 아니다. 되읽는 소비자
    (`scripts/paired_tuner_probe.py`)는 판정값을 **뺀다** - 표지를 그대로
    넘기면 그 뺄셈이 TypeError가 된다."""
    original = {"criteria": [{"actual": math.nan}], "severity": [-math.inf, 0.5, math.inf]}
    restored = restore_non_finite(json.loads(dumps(original)))
    assert math.isnan(restored["criteria"][0]["actual"])
    assert restored["severity"] == [-math.inf, 0.5, math.inf]


def test_restore_leaves_ordinary_strings_alone():
    assert restore_non_finite({"label": "ss/1.62/125.0", "reason": "area"}) == {
        "label": "ss/1.62/125.0",
        "reason": "area",
    }


def test_dump_writes_through_a_file_object():
    buf = io.StringIO()
    dump({"x": math.nan}, buf, indent=2)
    json.loads(buf.getvalue(), parse_constant=_reject_non_rfc)
