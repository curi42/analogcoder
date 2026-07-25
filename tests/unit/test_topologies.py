from analogcoder.netlist import parse_netlist
from analogcoder.topologies import TOPOLOGY_LIBRARY, Topology


def test_library_has_exactly_the_v1_entries():
    assert set(TOPOLOGY_LIBRARY.keys()) == {"miller_basic", "miller_nulling_resistor"}
    for topology_id, topology in TOPOLOGY_LIBRARY.items():
        assert isinstance(topology, Topology)
        assert topology.id == topology_id


def test_miller_basic_body_has_expected_components():
    body = TOPOLOGY_LIBRARY["miller_basic"].subckt_body
    wrapped = f".subckt TEST vinp vinn vout vdd vss\n{body}.ends TEST\n"
    parsed = parse_netlist(wrapped)
    refdes = {c.refdes for c in parsed.subckts["TEST"].components}
    assert refdes == {"Iref", "M9", "M1", "M2", "M3", "M4", "M5", "M6", "M7", "Cc", "Ca"}


def test_miller_nulling_resistor_body_has_rz_in_series_with_cc():
    body = TOPOLOGY_LIBRARY["miller_nulling_resistor"].subckt_body
    wrapped = f".subckt TEST vinp vinn vout vdd vss\n{body}.ends TEST\n"
    parsed = parse_netlist(wrapped)
    subckt = parsed.subckts["TEST"]
    refdes = {c.refdes for c in subckt.components}
    assert refdes == {"Iref", "M9", "M1", "M2", "M3", "M4", "M5", "M6", "M7", "Cc", "Rz", "Ca"}
    cc = next(c for c in subckt.components if c.refdes == "Cc")
    rz = next(c for c in subckt.components if c.refdes == "Rz")
    assert cc.nodes[1] == rz.nodes[0]  # Cc's second node feeds directly into Rz's first node
    assert rz.nodes[1] == "vout"


def test_miller_nulling_resistor_addresses_phase_margin():
    assert "phase_margin" in TOPOLOGY_LIBRARY["miller_nulling_resistor"].addresses
