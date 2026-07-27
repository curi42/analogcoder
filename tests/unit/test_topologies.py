import pytest

from analogcoder.netlist import parse_netlist
from analogcoder.topologies import TOPOLOGY_LIBRARY, Topology

_SELF_BIAS_REFDES = {"Xp3", "Xp4", "Xn1", "Xn2", "Rdeg", "Rstart"}
_INPUT_PAIR_REFDES = {"X1", "X2", "X3", "X4", "X5", "X6", "X7"}
_MIM_CAP_REFDES = {"Xcc", "Xca"}


def test_library_has_exactly_the_v1_entries():
    assert set(TOPOLOGY_LIBRARY.keys()) == {
        "miller_basic",
        "miller_nulling_resistor",
        "folded_cascode_nmos_in_cs",
        "folded_cascode_pmos_in_cs",
    }
    for topology_id, topology in TOPOLOGY_LIBRARY.items():
        assert isinstance(topology, Topology)
        assert topology.id == topology_id


def test_miller_basic_body_has_expected_components():
    body = TOPOLOGY_LIBRARY["miller_basic"].subckt_body
    wrapped = f".subckt TEST vinp vinn vout vdd vss\n{body}.ends TEST\n"
    parsed = parse_netlist(wrapped)
    refdes = {c.refdes for c in parsed.subckts["TEST"].components}
    assert refdes == _SELF_BIAS_REFDES | _INPUT_PAIR_REFDES | _MIM_CAP_REFDES


def test_miller_nulling_resistor_body_has_rz_in_series_with_cc():
    body = TOPOLOGY_LIBRARY["miller_nulling_resistor"].subckt_body
    wrapped = f".subckt TEST vinp vinn vout vdd vss\n{body}.ends TEST\n"
    parsed = parse_netlist(wrapped)
    subckt = parsed.subckts["TEST"]
    refdes = {c.refdes for c in subckt.components}
    assert refdes == _SELF_BIAS_REFDES | _INPUT_PAIR_REFDES | _MIM_CAP_REFDES | {"Rz"}
    cc = next(c for c in subckt.components if c.refdes == "Xcc")
    rz = next(c for c in subckt.components if c.refdes == "Rz")
    assert cc.nodes[1] == rz.nodes[0]  # Xcc's second node feeds directly into Rz's first node
    assert rz.nodes[1] == "vout"
    assert rz.value == "220000"


def test_miller_nulling_resistor_addresses_phase_margin():
    assert "phase_margin" in TOPOLOGY_LIBRARY["miller_nulling_resistor"].addresses


def _wrapped(topology):
    header = ".subckt TMP " + " ".join(topology.ports)
    return parse_netlist(f"{header}\n{topology.subckt_body}.ends TMP\n")


@pytest.mark.parametrize("topology_id", sorted(TOPOLOGY_LIBRARY))
def test_every_declared_port_is_referenced_by_the_body(topology_id):
    topology = TOPOLOGY_LIBRARY[topology_id]
    parsed = _wrapped(topology)
    referenced = {n for c in parsed.subckts["TMP"].components for n in c.nodes}
    missing = set(topology.ports) - referenced
    assert missing == set(), f"{topology_id} declares unreferenced ports: {sorted(missing)}"


@pytest.mark.parametrize("topology_id", sorted(TOPOLOGY_LIBRARY))
def test_assumes_scale_is_positive(topology_id):
    assert TOPOLOGY_LIBRARY[topology_id].assumes_scale > 0


@pytest.mark.parametrize("topology_id", sorted(TOPOLOGY_LIBRARY))
def test_no_positional_value_is_an_unresolved_parameter_expression(topology_id):
    """topology_match._is_model_name는 위치 값이 parse_spice_value로 파싱되지
    않으면 모델/서브회로 이름으로 본다. 아직 풀리지 않은 파라미터 표현식
    (`{rv*2}`, `'rv*2'`)도 똑같이 파싱에 실패하므로 모델 이름으로 오분류된다 -
    그런 문자열은 어떤 실제 덱의 모델 집합에도 나타날 수 없으므로 항상 허위
    `models` 기각으로 이어진다. 토폴로지 본문에는 그 표현식을 풀어 줄 인스턴스
    컨텍스트가 없으므로 topology_match는 풀려고 시도하지 않는다(추측 금지) -
    대신 라이브러리 본문 자체가 이런 값을 갖지 않도록 여기서 큐레이션
    불변식으로 막는다. 이 테스트가 없으면 그런 본문을 추가해도 아무 경고 없이
    `models` 오탐이 조용히 발생한다."""
    topology = TOPOLOGY_LIBRARY[topology_id]
    parsed = _wrapped(topology)
    for component in parsed.subckts["TMP"].components:
        if component.value is None:
            continue
        offending_chars = {ch for ch in "{}'" if ch in component.value}
        assert not offending_chars, (
            f"{topology_id}: component {component.refdes!r} has a parameter expression as its "
            f"positional value ({component.value!r}) - topology_match._is_model_name would "
            f"misclassify this as a model name"
        )


@pytest.mark.parametrize(
    "topology_id,deck,block",
    [
        ("miller_basic", "benchmarks/two_stage_opamp/netlist.cir", "OPAMP2STAGE"),
        ("miller_nulling_resistor", "benchmarks/two_stage_opamp/netlist.cir", "OPAMP2STAGE"),
        ("folded_cascode_nmos_in_cs", "benchmarks/bandgap/netlist.cir", "TRIMAMP"),
        ("folded_cascode_pmos_in_cs", "benchmarks/bandgap/netlist.cir", "BUF_P"),
    ],
)
def test_declared_ports_match_the_source_block_header(topology_id, deck, block):
    from pathlib import Path

    parsed = parse_netlist(Path(deck).read_text())
    assert TOPOLOGY_LIBRARY[topology_id].ports == parsed.subckts[block].ports
