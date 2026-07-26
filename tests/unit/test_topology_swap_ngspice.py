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

    # Convert relative include paths to absolute paths so netlist works from tmp_path
    abs_benchmark_dir = os.path.abspath(BENCHMARK_DIR)
    third_party_path = os.path.join(abs_benchmark_dir, "..", "..", "third_party")
    pdk_corner_path = os.path.join(abs_benchmark_dir, "pdk_corner.inc")
    swapped_text = swapped_text.replace(
        '.include "../../third_party',
        f'.include "{third_party_path}'
    )
    # Also handle the pdk_corner.inc include to use absolute path
    swapped_text = swapped_text.replace(
        '.include "pdk_corner.inc"',
        f'.include "{pdk_corner_path}"'
    )

    swapped_path = tmp_path / f"{topology_id}.cir"
    swapped_path.write_text(swapped_text)

    backend = NgspiceBackend()
    return backend.run(str(swapped_path), {"control_block": spec.canonical.control_block})


def test_miller_basic_topology_cannot_meet_phase_margin_spec(tmp_path):
    # miller_basic is the two_stage_opamp benchmark's original topology. Real
    # ngspice measures its phase margin at 34.56 deg (see the sky130 PDK
    # migration design spec's Validation section) - far below the 62 deg
    # threshold required by spec_topology_required.yaml, and four rounds of
    # real parameter search (documented in that spec) never found a
    # parameter-only combination that closes this gap without regressing PSR
    # or gain below threshold. This is the whole reason topology-swap tuning
    # exists. This test proves the starting topology genuinely fails it.
    result = _run_topology("miller_basic", tmp_path)

    assert result.status == "success"
    assert result.measurements["phase_margin_deg"] < 62.0


def test_miller_nulling_resistor_topology_meets_all_criteria(tmp_path):
    # miller_nulling_resistor adds a nulling resistor (Rz=220kOhm, empirically
    # validated - see the design spec's Rz sweep) in series with Cc,
    # cancelling the right-half-plane zero. It should meet all three
    # spec_topology_required.yaml criteria simultaneously (not just phase
    # margin, which is the one it targets) - and on this sizing, actually
    # improves unity-gain bandwidth too rather than trading it away.
    result = _run_topology("miller_nulling_resistor", tmp_path)

    assert result.status == "success"
    assert result.measurements["phase_margin_deg"] >= 62.0
    assert result.measurements["ugbw_hz"] >= 2_500_000.0
    assert result.measurements["gain_db"] >= 70.0
