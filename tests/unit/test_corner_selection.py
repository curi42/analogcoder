import types

import pytest

from analogcoder.corner_selection import (
    NOMINAL,
    CornerSet,
    _as_point,
    coverage_seed,
    grown_with,
    label,
    next_probe,
    promote,
    raw_label,
    seed_from_sweep,
)
from analogcoder.pvt import CornerPoint, corner_fields as _corner_fields
from analogcoder.spec import CoverageConfig, Criterion, PVTCorners

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


def test_growth_dedupes_when_two_failing_criteria_share_a_worst_corner(_spec):
    # 성장 쪽의 씨앗-쪽 test_two_criteria_sharing_a_worst_corner_do_not_duplicate_it
    # 대응 짝. gain과 pm이 둘 다 FS를 최악 코너로 지목하면 added에는 FS가
    # 한 번만 들어가야 한다.
    #
    # "and point not in added" (corner_selection.py의 grown_with)를 빼면 이
    # 테스트는 실패하지만 - list 불일치(assert added == [FS])가 아니라
    # __post_init__의 중복 ValueError로 실패한다. grown_with는 반환하기
    # 전에 corners = (*cs.corners, *added)로 CornerSet을 그 자리에서
    # 만들기 때문에, added 안의 중복은 구조적으로 corners 안의 중복이고,
    # __post_init__이 항상 먼저 가로챈다 - grown_with의 가드만 따로 떼어
    # 잡는 테스트는 이 함수 형태로는 애초에 만들 수 없다. 그래도 이건 실제
    # 판별력이다: 가드가 없으면 grown_with가 올바른 결과를 돌려주는 대신
    # 죽는다는 뜻이므로, 이 줄은 여전히 동작을 결정하는 코드고
    # __post_init__은 그 뒤를 받치는 최후 방어선일 뿐이다.
    cs = CornerSet(corners=(NOMINAL,), probe_order=())
    sweep = _sweep({"gain": _wc(FS, -1.0), "pm": _wc(FS, -2.0)})
    _, added = grown_with(cs, sweep, ["gain", "pm"])
    assert added == [FS]


def test_corner_set_rejects_a_construction_where_nominal_is_not_first():
    # CornerSet은 public이고 frozen dataclass 기본 __init__을 그대로
    # 노출한다 - seed_from_sweep/grown_with/promote를 거치지 않고 나중
    # 태스크(run state 역직렬화 등)가 직접 만들 수 있다. NOMINAL이 [0]이
    # 아니면 "덱 그대로"라는 기준점이 조용히 사라진다.
    with pytest.raises(ValueError, match="NOMINAL"):
        CornerSet(corners=(FS, SF), probe_order=())


def test_corner_set_rejects_a_duplicate_corner():
    with pytest.raises(ValueError, match="duplicate"):
        CornerSet(corners=(NOMINAL, FS, FS), probe_order=())


def test_corner_set_rejects_a_corner_in_both_the_set_and_the_probe_order():
    # 이 상태에서 next_probe를 부르면 이미 매 반복 도는 코너(FS)를 또
    # 탐침으로 골라, 이 하위 프로젝트가 막으려는 낭비된 시뮬레이션을
    # 정확히 만들어 낸다.
    with pytest.raises(ValueError, match="overlap"):
        CornerSet(corners=(NOMINAL, FS), probe_order=(FS, SF))


def test_corner_set_rejects_a_duplicate_within_the_probe_order():
    # 앞의 세 검사와 같은 부류: probe_order 안에 같은 코너가 두 번 있으면
    # next_probe의 회전이 그 코너를 실제보다 더 자주 골라, 세지 않은
    # 코너를 상대적으로 덜 훑게 된다.
    with pytest.raises(ValueError, match="duplicate"):
        CornerSet(corners=(NOMINAL,), probe_order=(FS, FS))


def test_a_deck_entry_is_rejected_rather_than_turned_into_a_corner(_spec):
    """`(deck)` 항목은 코너가 아니다 - 렌더링을 거치지 않은 덱 그 자체다.

    corner-aware simulate의 corner_worst는 선택 집합의 최악값이므로 NOMINAL이
    최악인 기준을 담을 수 있고, pvt._corner_fields는 그것을
    `{"process": "(deck)", "voltage": None, "temperature": None}`으로 적는다.
    그 dict가 성장 경로에 들어오면 CornerPoint(process="(deck)", voltage=None)이
    되어 `.include ".../pdk_corner_(deck).inc"`를 렌더링하고, 없는 파일을 ngspice가
    읽는다 - 좌표가 없다는 사실이 조용히 좌표로 둔갑한 것이다. 오늘 그 경로로
    가는 호출부는 없으므로 이것은 미래를 막는 벽이다."""
    deck_entry = {"process": "(deck)", "voltage": None, "temperature": None, "value": 41.0}

    with pytest.raises(ValueError, match=r"\(deck\)"):
        seed_from_sweep(_sweep({"gain": deck_entry}), _spec)

    cs = CornerSet(corners=(NOMINAL, FS), probe_order=())
    with pytest.raises(ValueError, match=r"\(deck\)"):
        grown_with(cs, _sweep({"gain": deck_entry}), ["gain"])


# ------------------------------------------------- 원시 dict의 코너 이름


def test_a_raw_corner_entry_gets_the_same_name_as_the_point_it_describes():
    """`raw_label`과 `label`이 갈리면 `final_set`과 `argmax_drift`를 나란히
    놓고 읽을 수 없다. 그 일치는 두 곳의 독스트링이 **주장만** 하고 아무것도
    강제하지 않던 것이다 - `cli.py`와 `report.py`가 같은 함수를 각자 복사해
    갖고 있었고, `report.py`의 사본에는 테스트가 하나도 없었다."""
    for point in (FS, SF, SS, CornerPoint(process="tt", voltage=1.8, temperature=27.0)):
        assert raw_label(_corner_fields(point)) == label(point)


def test_a_raw_entry_with_no_coordinates_is_named_but_never_a_corner(_spec):
    """같은 판별을 두 함수가 **반대 방향으로** 쓴다. `_as_point`는 거부하고
    `raw_label`은 이름만 적는다 - 계측은 순수한 기록이고 기록이 실행을 멈출
    수는 없기 때문이다. 갈라지면 안 되는 것은 판별이지 반응이 아니다."""
    deck = _corner_fields(NOMINAL)

    assert raw_label(deck) == label(NOMINAL) == "(deck)"
    with pytest.raises(ValueError, match=r"\(deck\)"):
        seed_from_sweep(_sweep({"gain": dict(deck, value=41.0)}), _spec)


def test_a_half_coordinate_entry_takes_the_same_branch_in_both():
    """조건이 한쪽은 `or`, 다른 쪽은 `and`이면 반쪽짜리 좌표에서 둘이 서로
    다른 말을 한다. 오늘 이 모양을 만드는 호출부는 없다 - 미래의 벽이다."""
    for half in (
        {"process": "ss", "voltage": 1.62, "temperature": None},
        {"process": "ss", "voltage": None, "temperature": 125.0},
    ):
        assert raw_label(half) == "(deck)"
        with pytest.raises(ValueError):
            _as_point(half)


def test_no_entry_at_all_is_no_name_at_all():
    """`None`은 "그 기준에 최악 코너 항목이 없다"이고 `(deck)`은 "항목은
    있는데 좌표가 없다"다. 서로 다른 사실이다."""
    assert raw_label(None) is None


# --------------------------------------------- ε-근접 피복 씨앗 (coverage_seed)


def _per_corner(rows):
    """rows: [(CornerPoint, {measurement: value}), ...] -> per_corner 항목들."""
    from analogcoder.pvt import corner_fields

    return [{"corner": corner_fields(c), "measurements": m, "severity": 0.0}
            for c, m in rows]


_GAIN = Criterion(name="gain", measurement="g", operator=">=", threshold=40.0)
_PM = Criterion(name="pm", measurement="p", operator=">=", threshold=60.0)


def test_two_corners_within_epsilon_of_each_others_worst_collapse_to_one():
    """이것이 이 함수의 존재 이유다. argmax 피복에서 집합은 서로소이므로
    (기준마다 argmax 가 하나) 코너를 줄일 수 없다. ε-근접이 집합을 겹치게
    만든다: FS 가 gain 의 최악이고 SS 가 pm 의 최악인데, SS 의 gain 이 FS 의
    gain 에서 ε 이내이면 SS 하나가 둘 다 덮는다."""
    sweep = {"per_corner": _per_corner([
        (FS, {"g": 41.0, "p": 70.0}),
        (SS, {"g": 41.02, "p": 65.0}),
    ])}

    chosen, record = coverage_seed(sweep, [_GAIN, _PM], CoverageConfig(epsilon=0.01, tau=1.0))

    assert chosen == [SS]
    assert record["covered"] == 2 and record["total"] == 2
    assert label(FS) in record["dropped"]


def test_epsilon_zero_reproduces_the_argmax_union():
    """ε=0 이면 각 기준의 최악값과 **정확히** 같은 코너만 덮으므로 오늘의
    씨앗과 같은 집합이 나온다. 이것이 회귀 안전선이다."""
    sweep = {"per_corner": _per_corner([
        (FS, {"g": 41.0, "p": 70.0}),
        (SS, {"g": 45.0, "p": 65.0}),
    ])}

    chosen, record = coverage_seed(sweep, [_GAIN, _PM], CoverageConfig(epsilon=0.0, tau=1.0))

    assert set(chosen) == {FS, SS}
    assert record["dropped"] == []


def test_a_corner_with_no_measurement_is_not_approximated_by_any_other():
    """측정값이 없다는 것은 회로가 거기서 동작하지 않는다는 가장 강한 증거다.
    값이 있는 코너가 그것을 ε 으로 덮으면 그 사실이 사라진다."""
    sweep = {"per_corner": _per_corner([
        (FS, {"g": 41.0}),
        (SS, {}),          # g 측정값 없음
    ])}

    chosen, _ = coverage_seed(sweep, [_GAIN], CoverageConfig(epsilon=0.9, tau=1.0))

    assert SS in chosen


def test_tau_below_one_stops_early():
    """예산 k 는 τ 에서 유도된다 - 정수 상한을 따로 두지 않는 이유다."""
    sweep = {"per_corner": _per_corner([
        (FS, {"g": 41.0, "p": 99.0}),
        (SS, {"g": 99.0, "p": 65.0}),
    ])}

    chosen, record = coverage_seed(sweep, [_GAIN, _PM], CoverageConfig(epsilon=0.0, tau=0.5))

    assert len(chosen) == 1
    assert record["covered"] == 1 and record["total"] == 2


def test_an_empty_per_corner_yields_an_empty_seed_rather_than_guessing():
    sweep = {"per_corner": []}

    chosen, record = coverage_seed(sweep, [_GAIN], CoverageConfig(epsilon=0.03, tau=1.0))

    assert chosen == []
    assert record["covered"] == 0 and record["total"] == 1


def test_a_criterion_whose_worst_is_zero_is_covered_only_by_an_exact_tie():
    """`scale = abs(worst)`이므로 최악값이 0이면 허용오차도 0이다 - 정확히
    같은 값만 덮는다. 예전에는 `or 1.0` 로 떨어져서 ε 이 그 기준에서만
    **절대** 허용오차가 됐고, 그 1.0 은 아무 데서도 유도되지 않은 상수였다.
    닫히는 방향으로 실패한다: 씨앗이 커질 뿐 빠져야 할 코너가 빠지지 않는다."""
    sweep = {"per_corner": _per_corner([
        (FS, {"z": 0.0}),
        (SS, {"z": -0.002}),
    ])}
    crit = Criterion(name="resid", measurement="z", operator="<=", threshold=1.0)

    chosen, record = coverage_seed(sweep, [crit], CoverageConfig(epsilon=0.01, tau=1.0))

    assert chosen == [FS]          # 최악은 <= 이므로 최대값, 즉 0.0 인 FS
    assert record["covered"] == 1


def test_a_measurement_absent_from_every_corner_names_no_dropped_corner():
    """`dropped` 는 '오늘의 씨앗이 골랐을 코너'와 비교한 결과다. 오늘의 씨앗은
    `worst_case_corners` 에서 오고, `pvt.worst_case_measurements` 는 어느 코너에도
    측정값이 없는 기준을 **통째로 건너뛴다**. `_argmax_points` 가 그것을
    `missing[0]` 에 귀속시키면 실재하지 않는 이유로 코너 하나가 dropped 에
    실린다 - 이 게이트의 무력 상태를 보이게 하는 바로 그 칸이 거짓이 된다."""
    sweep = {"per_corner": _per_corner([
        (FS, {"g": 45.0}),
        (SS, {"g": 41.0}),
    ])}
    ghost = Criterion(name="ghost", measurement="nowhere", operator=">=", threshold=1.0)

    _chosen, record = coverage_seed(sweep, [_GAIN, ghost], CoverageConfig(epsilon=0.0, tau=1.0))

    assert record["dropped"] == []
