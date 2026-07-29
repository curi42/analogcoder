"""코너의 **정체성**은 라벨이고 좌표는 선택이다.

대상 흐름의 서명 코너는 이 저장소 밖의 코드가 고르고, 코너 파일은
불투명하다 - 안을 들여다보고 축을 해석하면 그것이 금지된 추측이다. 그래서
좌표를 필수로 두면 라벨로만 오는 코너를 표현할 수 없고, 좌표를 지우면
벤치마크 경로의 렌더링이 되돌릴 수 없이 사라진다."""

import pytest

from analogcoder.checkpoint import CheckpointRejected, _corner_from_payload, _corner_payload
from analogcoder.corner_selection import _as_point, label, raw_label
from analogcoder.pvt import corner_fields as _corner_fields
from analogcoder.spec import CornerPoint, axis_corner_id


def test_a_corner_declared_by_axes_derives_its_identity_from_them():
    point = CornerPoint(process="tt", voltage=1.8, temperature=27.0)
    assert point.corner_id == "tt/1.8/27.0"
    assert label(point) == "tt/1.8/27.0"


def test_the_derived_identity_is_byte_identical_to_the_old_label_format():
    """이것이 R1/R2의 회귀 기준값이다 - 로더가 같은 f-string으로 채우므로
    코너 목록도 산출물의 라벨도 바이트 동일하게 유지된다."""
    point = CornerPoint(process="sf", voltage=1.62, temperature=-40.0)
    assert axis_corner_id("sf", 1.62, -40.0) == "sf/1.62/-40.0"
    assert point.corner_id == f"{point.process}/{point.voltage}/{point.temperature}"


def test_a_corner_can_be_a_label_with_no_coordinates_at_all():
    point = CornerPoint(corner_id="sig_corner_01", payload="/abs/corners/c01.inc")
    assert point.process is None
    assert label(point) == "sig_corner_01"


def test_a_corner_with_neither_identity_nor_coordinates_is_refused():
    with pytest.raises(ValueError):
        CornerPoint()


def test_partial_coordinates_cannot_stand_in_for_an_identity():
    with pytest.raises(ValueError):
        CornerPoint(process="tt", voltage=1.8)


# --- 산출물 dict 왕복 -------------------------------------------------------


def test_an_axis_corner_reports_exactly_its_three_coordinates():
    """R2의 바이트 동일성이 여기 걸려 있다 - `corner_id`를 여기 더하면 45코너
    스윕 산출물이 바뀐다."""
    fields = _corner_fields(CornerPoint(process="tt", voltage=1.8, temperature=27.0))
    assert fields == {"process": "tt", "voltage": 1.8, "temperature": 27.0}
    assert list(fields) == ["process", "voltage", "temperature"]


def test_a_label_corner_reports_its_identity_and_payload():
    fields = _corner_fields(CornerPoint(corner_id="sig_01", payload="/abs/c.inc"))
    assert fields == {"corner_id": "sig_01", "payload": "/abs/c.inc"}


def test_the_unrendered_deck_no_longer_puts_a_name_in_a_coordinate_field():
    """`"(deck)"`가 process 칸의 값으로 사는 것이 오늘의 모양이다 - 좌표가
    아닌 것이 좌표 자리에 앉아 있으면 그것을 읽는 다음 사람이 코너로 읽는다."""
    assert _corner_fields(None) == {"corner_id": None}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"process": "tt", "voltage": 1.8, "temperature": 27.0}, "tt/1.8/27.0"),
        ({"corner_id": "sig_01", "payload": "/abs/c.inc"}, "sig_01"),
        ({"corner_id": None}, "(deck)"),
        (None, None),
    ],
)
def test_raw_label_reads_every_shape_corner_fields_can_write(raw, expected):
    assert raw_label(raw) == expected


def test_as_point_rebuilds_an_axis_corner_with_its_identity():
    point = _as_point({"process": "tt", "voltage": 1.8, "temperature": 27.0})
    assert point == CornerPoint(process="tt", voltage=1.8, temperature=27.0)
    assert point.corner_id == "tt/1.8/27.0"


def test_as_point_rebuilds_a_label_corner_including_its_payload():
    """payload가 없으면 조합 경로가 그 코너를 실현할 파일을 못 찾는다."""
    point = _as_point({"corner_id": "sig_01", "payload": "/abs/c.inc"})
    assert point.corner_id == "sig_01"
    assert point.payload == "/abs/c.inc"


def test_as_point_still_refuses_the_unrendered_deck():
    """`(deck)`이 조용히 좌표로 둔갑하면 `.include ".../pdk_corner_(deck).inc"`가
    ngspice에 넘어간다."""
    with pytest.raises(ValueError):
        _as_point({"corner_id": None})


def test_as_point_and_raw_label_agree_on_every_shape():
    """한쪽만 고치면 `_argmax_drift`의 moved_count가 영구히 0이 된다 - D1의
    반복제안률 0.000과 정확히 같은 무효 지표다."""
    for raw in (
        {"process": "tt", "voltage": 1.8, "temperature": 27.0},
        {"corner_id": "sig_01", "payload": "/abs/c.inc"},
    ):
        assert raw_label(raw) == label(_as_point(raw))


# --- 체크포인트 -------------------------------------------------------------


def test_a_label_corner_survives_the_checkpoint_round_trip():
    point = CornerPoint(corner_id="sig_01", payload="/abs/c.inc")
    assert _corner_from_payload(_corner_payload(point)) == point


def test_an_axis_corner_survives_the_checkpoint_round_trip():
    point = CornerPoint(process="ff", voltage=1.98, temperature=125.0)
    assert _corner_from_payload(_corner_payload(point)) == point


def test_an_unknown_corner_payload_shape_is_rejected_not_a_traceback():
    """오늘은 `KeyError: 'process'`가 cli.py의 잡에 안 걸려 트레이스백이 되고
    result.json도 report.md도 안 나온다. 재개는 최적화이지 정확성이 아니므로
    알 수 없는 모양이면 체크포인트를 버리고 처음부터 돈다."""
    with pytest.raises(CheckpointRejected):
        _corner_from_payload({"flavour": "ss"})
