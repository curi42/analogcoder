import os

from analogcoder.netlist import apply_topology_swap, parse_netlist
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.topologies import TOPOLOGY_LIBRARY

BENCHMARK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "two_stage_opamp")


def _run_topology(topology_id: str, tmp_path):
    netlist_path = os.path.join(BENCHMARK_DIR, "netlist.cir")
    with open(netlist_path) as f:
        netlist_text = f.read()

    spec = load_spec(os.path.join(BENCHMARK_DIR, "spec_topology_required.yaml"))

    subckt_name = next(iter(parse_netlist(netlist_text).subckts))
    topology = TOPOLOGY_LIBRARY[topology_id]
    swapped_text = apply_topology_swap(netlist_text, subckt_name, topology.subckt_body)

    swapped_path = tmp_path / f"{topology_id}.cir"
    swapped_path.write_text(swapped_text)

    backend = NgspiceBackend()
    return backend.run(str(swapped_path), {"control_block": spec.canonical.control_block})


def test_miller_basic_topology_cannot_meet_phase_margin_spec(tmp_path):
    # miller_basic is the two_stage_opamp benchmark's original topology. Parameter
    # tuning alone cannot push its phase margin past the 65 deg threshold required
    # by spec_topology_required.yaml - this is the whole reason topology-swap
    # tuning exists. This test proves the starting topology genuinely fails it.
    result = _run_topology("miller_basic", tmp_path)

    assert result.status == "success"
    assert result.measurements["phase_margin_deg"] < 65.0


def test_miller_nulling_resistor_topology_meets_all_criteria(tmp_path):
    # miller_nulling_resistor adds a nulling resistor in series with Cc, cancelling
    # the right-half-plane zero. It should meet all three spec_topology_required.yaml
    # criteria simultaneously (not just phase margin, which is the one it targets).
    result = _run_topology("miller_nulling_resistor", tmp_path)

    assert result.status == "success"
    assert result.measurements["phase_margin_deg"] >= 65.0
    assert result.measurements["ugbw_hz"] >= 20_000_000.0
    assert result.measurements["gain_db"] >= 70.0
