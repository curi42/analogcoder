"""스펙이 조합형 테스트벤치를 선언할 수 있다. 그리고 그 경로에서 코너
렌더링은 정규식을 **하나도** 쓰지 않는다 - 자리 채우기다."""

import os

import pytest
import yaml

from analogcoder.compose import ComposeError, deck_for
from analogcoder.spec import CornerPoint, load_spec

SIGNALS = "Vdd vdd 0 DC 1.8\nVin in 0 AC 1\n"
CORE = "* core\nR1 in out 1k\nR2 out 0 1k\n.end\n"
CORNER_A = ".temp 27\n"
CORNER_B = ".temp 125\n"


def _write(tmp_path, *, compose_block=None, corners=None, nominal=None, netlist="core.cir"):
    (tmp_path / "signals.cir").write_text(SIGNALS)
    (tmp_path / "core.cir").write_text(CORE)
    (tmp_path / "c_a.inc").write_text(CORNER_A)
    (tmp_path / "c_b.inc").write_text(CORNER_B)
    tb = {
        "name": "tb1",
        "analyses": ["op"],
        "control_block": ".control\nop\n.endc",
        "criteria": [
            {"name": "v", "measurement": "vout", "operator": ">=", "threshold": 0.0}
        ],
    }
    if compose_block is None:
        tb["netlist"] = netlist
    else:
        tb["compose"] = compose_block
    raw = {"circuit_name": "x", "testbenches": [tb]}
    if corners is not None:
        block = {"corners": corners}
        if nominal is not None:
            block["nominal"] = nominal
        raw["pvt_corners"] = block
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(raw))
    return str(path)


DEFAULT_COMPOSE = [
    {"file": "signals.cir"},
    {"corner_slot": True},
    {"file": "core.cir", "tunable": True},
]
DEFAULT_CORNERS = [
    {"id": "sign_off_a", "include": "c_a.inc"},
    {"id": "sign_off_b", "include": "c_b.inc"},
]


# --- 선언 ------------------------------------------------------------------


def test_a_single_file_testbench_still_loads_exactly_as_before(tmp_path):
    """오늘의 형태가 그대로 동작해야 한다 - 벤치마크 11개 덱이 회귀 검사다."""
    spec = load_spec(_write(tmp_path))
    assert spec.canonical.fragments is None
    assert spec.canonical.netlist_path.endswith("core.cir")


def test_a_composed_testbench_declares_its_fragments_in_order(tmp_path):
    spec = load_spec(_write(tmp_path, compose_block=DEFAULT_COMPOSE, corners=DEFAULT_CORNERS, nominal="sign_off_a"))
    kinds = [f.kind for f in spec.canonical.fragments]
    assert kinds == ["file", "corner_slot", "file"]


def test_the_versioned_deck_is_the_tunable_fragment_only(tmp_path):
    """분석 3의 버전 관리 경계: 조각만 버전으로 남기고 조합은 시뮬레이션
    직전에 한다. `netlist_path`가 그 조각을 가리키므로 RunState·체크포인트·
    resolve_includes 소비자가 전부 그대로 동작한다."""
    spec = load_spec(_write(tmp_path, compose_block=DEFAULT_COMPOSE, corners=DEFAULT_CORNERS, nominal="sign_off_a"))
    assert spec.canonical.netlist_path.endswith("core.cir")


def test_declaring_both_netlist_and_compose_is_refused(tmp_path):
    block = list(DEFAULT_COMPOSE)
    path = _write(tmp_path, compose_block=block, corners=DEFAULT_CORNERS, nominal="sign_off_a")
    raw = yaml.safe_load(open(path))
    raw["testbenches"][0]["netlist"] = "core.cir"
    open(path, "w").write(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="both"):
        load_spec(path)


def test_a_composed_testbench_needs_exactly_one_tunable_fragment(tmp_path):
    for block in (
        [{"file": "signals.cir"}, {"file": "core.cir"}],
        [{"file": "signals.cir", "tunable": True}, {"file": "core.cir", "tunable": True}],
    ):
        with pytest.raises(ValueError, match="tunable"):
            load_spec(_write(tmp_path, compose_block=block))


def test_two_corner_slots_are_refused(tmp_path):
    block = [{"corner_slot": True}, {"corner_slot": True}, {"file": "core.cir", "tunable": True}]
    with pytest.raises(ValueError, match="corner_slot"):
        load_spec(_write(tmp_path, compose_block=block, corners=DEFAULT_CORNERS, nominal="sign_off_a"))


def test_a_composed_testbench_in_a_corner_carrying_spec_must_have_a_slot(tmp_path):
    """슬롯이 없으면 N개 코너가 전부 같은 덱을 돌면서 코너별 값으로
    보고된다 - 이 저장소가 `netlist_startup.cir`에서 이미 값을 치른 모양."""
    block = [{"file": "signals.cir"}, {"file": "core.cir", "tunable": True}]
    with pytest.raises(ValueError, match="corner_slot"):
        load_spec(_write(tmp_path, compose_block=block, corners=DEFAULT_CORNERS, nominal="sign_off_a"))


# --- 코너 선언: 라벨 형태 ---------------------------------------------------


def test_corners_can_be_declared_as_labels_with_an_include(tmp_path):
    spec = load_spec(_write(tmp_path, compose_block=DEFAULT_COMPOSE, corners=DEFAULT_CORNERS, nominal="sign_off_a"))
    ids = [c.corner_id for c in spec.pvt_corners.corners]
    assert ids == ["sign_off_a", "sign_off_b"]
    assert all(os.path.isabs(c.payload) for c in spec.pvt_corners.corners)
    assert all(c.process is None for c in spec.pvt_corners.corners)


def test_a_label_corner_and_an_axis_corner_cannot_be_mixed_in_one_entry(tmp_path):
    corners = [{"id": "a", "include": "c_a.inc", "process": "tt", "voltage": 1.8, "temperature": 27}]
    with pytest.raises(ValueError):
        load_spec(_write(tmp_path, compose_block=DEFAULT_COMPOSE, corners=corners, nominal="a"))


def test_a_duplicate_corner_id_is_refused_at_the_declaration(tmp_path):
    """`corner_sig01`/`Corner1001`/`" corner_sig01"`은 필드 기반 중복 검사가
    구별하지 못한다. 정규화는 코드가 추측하면 안 되므로 방어선은 선언 자리다."""
    corners = [{"id": "a", "include": "c_a.inc"}, {"id": "a", "include": "c_b.inc"}]
    with pytest.raises(ValueError, match="duplicate"):
        load_spec(_write(tmp_path, compose_block=DEFAULT_COMPOSE, corners=corners, nominal="a"))


def test_a_label_corner_with_no_include_is_refused(tmp_path):
    corners = [{"id": "a"}]
    with pytest.raises(ValueError):
        load_spec(_write(tmp_path, compose_block=DEFAULT_COMPOSE, corners=corners, nominal="a"))


def test_a_corner_slot_needs_every_corner_to_carry_a_payload(tmp_path):
    corners = [{"process": "tt", "voltage": 1.8, "temperature": 27}]
    with pytest.raises(ValueError, match="payload"):
        load_spec(_write(tmp_path, compose_block=DEFAULT_COMPOSE, corners=corners, nominal="tt/1.8/27.0"))


def test_a_corner_slot_needs_a_declared_nominal_corner(tmp_path):
    """조합 모델에는 '렌더링을 거치지 않은 덱'이 존재하지 않는다 - 코너가
    입력이기 때문이다. 어느 코너가 임계값을 정한 그 덱인지는 **사람이
    선언**해야 하고, 이름에서 알아내면 그것이 금지된 추측이다."""
    with pytest.raises(ValueError, match="nominal"):
        load_spec(_write(tmp_path, compose_block=DEFAULT_COMPOSE, corners=DEFAULT_CORNERS))


def test_a_nominal_naming_no_declared_corner_is_refused(tmp_path):
    with pytest.raises(ValueError, match="nominal"):
        load_spec(_write(tmp_path, compose_block=DEFAULT_COMPOSE, corners=DEFAULT_CORNERS, nominal="sign_off_z"))


# --- 코너 슬롯 채우기 (정규식 0개) ------------------------------------------


def test_the_corner_slot_is_filled_with_the_corners_own_payload(tmp_path):
    spec = load_spec(_write(tmp_path, compose_block=DEFAULT_COMPOSE, corners=DEFAULT_CORNERS, nominal="sign_off_a"))
    corner = spec.pvt_corners.corners[1]

    deck = deck_for(spec.canonical, CORE, corner)

    assert f'.include "{corner.payload}"' in deck.text
    assert deck.records["corner_slot_filled"] == 1
    assert deck.records["corner"] == "sign_off_b"


def test_two_corners_produce_two_different_decks(tmp_path):
    """지문이 단사여야 한다 - `re.sub`가 0건 매치로 조용히 같은 덱을 내던
    것이 이 저장소가 값을 치른 자리다."""
    spec = load_spec(_write(tmp_path, compose_block=DEFAULT_COMPOSE, corners=DEFAULT_CORNERS, nominal="sign_off_a"))
    a, b = spec.pvt_corners.corners
    assert deck_for(spec.canonical, CORE, a).text != deck_for(spec.canonical, CORE, b).text


def test_the_corner_file_contents_are_never_read(tmp_path):
    """코너 파일은 불투명하다. 슬롯에 들어가는 것은 그 파일을 가리키는
    절대경로 include 한 줄뿐이다."""
    spec = load_spec(_write(tmp_path, compose_block=DEFAULT_COMPOSE, corners=DEFAULT_CORNERS, nominal="sign_off_a"))
    corner = spec.pvt_corners.corners[0]
    deck = deck_for(spec.canonical, CORE, corner)
    assert CORNER_A.strip() not in deck.text


def test_a_missing_corner_payload_is_refused_rather_than_composed_without_it(tmp_path):
    """payload 없는 코너로 슬롯을 채우려 하면 그 코너는 실현될 수 없다.
    조용히 건너뛰면 그 코너가 다른 코너의 덱을 돈다."""
    spec = load_spec(_write(tmp_path, compose_block=DEFAULT_COMPOSE, corners=DEFAULT_CORNERS, nominal="sign_off_a"))
    with pytest.raises(ComposeError):
        deck_for(spec.canonical, CORE, CornerPoint(corner_id="no_payload"))


def test_the_nominal_deck_of_a_composed_testbench_is_the_declared_corner(tmp_path):
    spec = load_spec(_write(tmp_path, compose_block=DEFAULT_COMPOSE, corners=DEFAULT_CORNERS, nominal="sign_off_a"))
    nominal = spec.nominal_corner()
    assert nominal.corner_id == "sign_off_a"
    deck = deck_for(spec.canonical, CORE, None, nominal=nominal)
    assert deck.records["corner"] == "sign_off_a"


def test_composing_the_nominal_without_a_declared_one_is_refused(tmp_path):
    spec = load_spec(_write(tmp_path, compose_block=DEFAULT_COMPOSE, corners=DEFAULT_CORNERS, nominal="sign_off_a"))
    with pytest.raises(ComposeError):
        deck_for(spec.canonical, CORE, None)


def test_the_tunable_fragments_text_comes_from_the_caller_not_from_disk(tmp_path):
    """버전 스택이 들고 있는 것은 조각이고, 조합은 시뮬레이션 직전에 한다."""
    spec = load_spec(_write(tmp_path, compose_block=DEFAULT_COMPOSE, corners=DEFAULT_CORNERS, nominal="sign_off_a"))
    tuned = CORE.replace("R1 in out 1k", "R1 in out 4k")
    deck = deck_for(spec.canonical, tuned, spec.pvt_corners.corners[0])
    assert "R1 in out 4k" in deck.text
    assert "R1 in out 1k" not in deck.text


def test_a_composed_deck_carries_the_compose_records_and_report(tmp_path):
    spec = load_spec(_write(tmp_path, compose_block=DEFAULT_COMPOSE, corners=DEFAULT_CORNERS, nominal="sign_off_a"))
    deck = deck_for(spec.canonical, CORE, spec.pvt_corners.corners[0])
    assert deck.records["title_inserted"] == 1
    assert "shared_nets" in deck.report


# --- 아직 배선되지 않은 경계 -------------------------------------------------


def test_the_tuning_loop_refuses_a_composed_spec_instead_of_simulating_a_fragment(tmp_path):
    """조각만 넘기면 자극도 코너도 없는 덱이 돌고 그 결과가 판정에 들어간다.
    게다가 조각 뷰에서는 `check_stimulus_untouched`가 자극 변경을
    approved=True로 통과시킨다(게이트가 열린 채 실패한다)."""
    import types

    from analogcoder.cli import _run

    spec_path = _write(
        tmp_path, compose_block=DEFAULT_COMPOSE, corners=DEFAULT_CORNERS, nominal="sign_off_a"
    )
    args = types.SimpleNamespace(
        spec=spec_path,
        run_dir=str(tmp_path / "run"),
        max_iterations=1,
        agent_backend="claude",
        llm_base_url=None,
        llm_model=None,
        llm_api_key_env=None,
        resume=False,
    )
    with pytest.raises(ValueError, match="not wired into the tuning loop"):
        import asyncio

        asyncio.run(_run(args))
