import types

import pytest

from analogcoder.corner_selection import (
    NOMINAL,
    CornerSet,
    grown_with,
    label,
    next_probe,
    promote,
    seed_from_sweep,
)
from analogcoder.pvt import CornerPoint
from analogcoder.spec import PVTCorners

FS = CornerPoint(process="fs", voltage=1.98, temperature=125.0)
SF = CornerPoint(process="sf", voltage=1.62, temperature=-40.0)
SS = CornerPoint(process="ss", voltage=1.62, temperature=125.0)


@pytest.fixture
def _spec():
    # seed_from_sweep only needs a spec that carries pvt_corners, matching
    # tests/unit/test_pvt.py's SimpleNamespace pattern. testbenches/canonical/
    # all_criteria are left minimal since this module never reads them.
    return types.SimpleNamespace(
        testbenches=[],
        canonical=None,
        all_criteria=[],
        pvt_corners=PVTCorners(process=["tt", "fs", "sf", "ss"], voltage=[1.62, 1.8, 1.98], temperature=[-40, 27, 125]),
    )


def _sweep(worst_corners, per_corner=()):
    return {"worst_case_corners": worst_corners, "per_corner": list(per_corner)}


def _wc(corner, value):
    return {"process": corner.process, "voltage": corner.voltage,
            "temperature": corner.temperature, "value": value}


def test_the_seed_is_the_union_of_every_criterion_s_worst_corner(_spec):
    cs = seed_from_sweep(_sweep({"gain": _wc(FS, 41.0), "psr": _wc(SF, -9.0)}), _spec)
    assert set(cs.corners) == {NOMINAL, FS, SF}


def test_two_criteria_sharing_a_worst_corner_do_not_duplicate_it(_spec):
    cs = seed_from_sweep(_sweep({"gain": _wc(FS, 41.0), "pm": _wc(FS, 55.0)}), _spec)
    assert list(cs.corners).count(FS) == 1


def test_nominal_is_always_first_even_when_no_criterion_names_it(_spec):
    # 임계값이 덱 그대로의 상태에서 정해졌다. 최악 코너 목록에 안 나온다고
    # 빼면 기존 동작의 기준점이 사라진다.
    cs = seed_from_sweep(_sweep({"gain": _wc(FS, 41.0)}), _spec)
    assert cs.corners[0] is NOMINAL


def test_a_corner_with_no_measurement_is_that_criterion_s_worst(_spec):
    # value=None은 그 코너에서 측정값이 아예 안 나왔다는 뜻이고,
    # worst_case_corners가 이미 그 코너를 지목하고 있다. 값이 없다고
    # 건너뛰는 변형은 회로가 동작하지 않는 코너를 집합에서 빠뜨린다.
    cs = seed_from_sweep(_sweep({"gain": _wc(SS, None)}), _spec)
    assert SS in cs.corners


def test_a_failing_entry_sweep_still_seeds(_spec):
    # 진입 스윕은 비-게이팅이고 그대로 둔다. 실패한 설계의 최악 코너도 최악
    # 코너이며, 오히려 그 코너들이야말로 중간 루프가 봐야 할 것이다.
    # overall_pass를 보고 씨앗을 건너뛰는 변형은, 코너에서 실패하는 설계로
    # 시작한 실행에서 축소를 통째로 꺼 버린다.
    failing = {"worst_case_corners": {"gain": _wc(FS, 12.0)},
               "per_corner": [], "overall_pass": False}
    cs = seed_from_sweep(failing, _spec)
    assert FS in cs.corners


def test_the_probe_order_is_most_severe_first(_spec):
    # 가장 아슬한 코너부터 훑어야 낡음을 빨리 잡는다. 정렬을 빼거나 뒤집는
    # 변형을 이 단언이 잡는다.
    sweep = {
        "worst_case_corners": {},
        "per_corner": [
            {"corner": {"process": "fs", "voltage": 1.98, "temperature": 125.0}, "severity": 0.5},
            {"corner": {"process": "sf", "voltage": 1.62, "temperature": -40.0}, "severity": 0.01},
        ],
    }
    assert seed_from_sweep(sweep, _spec).probe_order[0] == SF


def test_a_corner_already_in_the_set_is_not_also_a_probe(_spec):
    # 매 반복 도는 코너를 탐침으로 또 돌면 시뮬레이션 하나를 그냥 버린다.
    sweep = {
        "worst_case_corners": {"gain": _wc(FS, 41.0)},
        "per_corner": [
            {"corner": {"process": "fs", "voltage": 1.98, "temperature": 125.0}, "severity": 0.01},
            {"corner": {"process": "sf", "voltage": 1.62, "temperature": -40.0}, "severity": 0.5},
        ],
    }
    cs = seed_from_sweep(sweep, _spec)
    assert FS not in cs.probe_order and SF in cs.probe_order


def test_growth_adds_the_failing_criteria_s_worst_corners(_spec):
    cs = CornerSet(corners=(NOMINAL, FS), probe_order=())
    failing = _sweep({"gain": _wc(SF, -1.0)})
    grown, added = grown_with(cs, failing, ["gain"])
    assert SF in grown.corners and added == [SF]


def test_growth_never_removes_a_corner(_spec):
    cs = CornerSet(corners=(NOMINAL, FS), probe_order=())
    grown, _ = grown_with(cs, _sweep({"gain": _wc(SF, -1.0)}), ["gain"])
    assert NOMINAL in grown.corners and FS in grown.corners


def test_a_failure_at_a_corner_already_in_the_set_adds_nothing(_spec):
    # 이것이 경로 불일치 신호다. 집합이 자라지 않으면 재진입은 같은 정보로
    # 같은 결과를 낼 뿐이므로, 호출부는 added가 비었을 때 재시도하지 않는다.
    # added를 항상 실패 코너 전체로 돌려주는 변형은 무한 재시도를 만든다.
    cs = CornerSet(corners=(NOMINAL, FS), probe_order=())
    grown, added = grown_with(cs, _sweep({"gain": _wc(FS, -1.0)}), ["gain"])
    assert added == []
    assert grown.corners == cs.corners


def test_growth_only_looks_at_the_named_failing_criteria(_spec):
    # 통과한 기준의 최악 코너까지 끌어오면 집합이 불필요하게 커진다.
    cs = CornerSet(corners=(NOMINAL,), probe_order=())
    sweep = _sweep({"gain": _wc(FS, -1.0), "psr": _wc(SF, -20.0)})
    _, added = grown_with(cs, sweep, ["gain"])
    assert added == [FS]


def test_the_probe_walks_the_order_and_wraps():
    cs = CornerSet(corners=(NOMINAL,), probe_order=(FS, SF))
    first, cs = next_probe(cs)
    second, cs = next_probe(cs)
    third, _ = next_probe(cs)
    assert (first, second, third) == (FS, SF, FS)


def test_there_is_no_probe_when_the_set_covers_everything():
    assert next_probe(CornerSet(corners=(NOMINAL, FS), probe_order=()))[0] is None


def test_promotion_moves_a_corner_out_of_the_probe_order():
    # 승격된 코너가 탐침 순서에 남아 있으면 이미 매 반복 도는 코너를 또 돈다.
    cs = CornerSet(corners=(NOMINAL,), probe_order=(FS, SF))
    promoted = promote(cs, FS)
    assert FS in promoted.corners and FS not in promoted.probe_order


def test_growth_also_drops_the_added_corners_from_the_probe_order():
    # 성장으로 들어온 코너를 탐침이 계속 돌면 매 반복 시뮬레이션 하나가 낭비된다.
    cs = CornerSet(corners=(NOMINAL,), probe_order=(FS, SF))
    grown, _ = grown_with(cs, _sweep({"gain": _wc(FS, -1.0)}), ["gain"])
    assert grown.probe_order == (SF,)


def test_label_of_nominal_is_deck():
    assert label(NOMINAL) == "(deck)"


def test_label_of_a_real_corner_shows_process_voltage_temperature():
    assert label(FS) == "fs/1.98/125.0"
