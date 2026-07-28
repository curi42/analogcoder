"""Task 6 proof: a seeded bandgap deck whose one failing criterion
(buf0_loop_gain) is fixable ONLY by a topology swap, verified against real
ngspice with no LLM in the loop.

benchmarks/bandgap/netlist_seed_topology.cir is netlist_loops.cir with
exactly one change: BUF_P's body replaced by BUF_N's NMOS-input-fold body
(see that file's header comment for the measured rationale - vt05, the node
BUF_P buffers, sits at ~0.5V, below what an NMOS input pair can reach).
"""

import os
import shutil

import pytest

from analogcoder.netlist import apply_topology_swap, resolve_includes
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.topologies import TOPOLOGY_LIBRARY
from analogcoder.topology_match import SwapCandidate, compatible_swaps

BENCH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "bandgap"))

pytestmark = pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not on PATH")


def _measure(netlist_text: str, dest, control_block: str) -> dict:
    """Writes netlist_text to `dest` (a tmp_path file) and runs it through
    real ngspice with `control_block` appended, returning the parsed
    `meas`/`let` measurements.

    `.include "pdk_corner.inc"` in this deck is a path relative to
    benchmarks/bandgap - writing the text to tmp_path unmodified would leave
    that include unresolved (NgspiceBackend sets cwd to the directory of the
    ORIGINAL netlist_path, which would be tmp_path here, not
    benchmarks/bandgap). resolve_includes rewrites the top-level .include to
    an absolute path first, matching test_bandgap_benchmark_ngspice.py's
    existing pattern.
    """
    text = resolve_includes(netlist_text, BENCH)
    dest.write_text(text)
    return NgspiceBackend(timeout=180).run(str(dest), {"control_block": control_block}).measurements


def test_the_seeded_deck_fails_buf0_loop_gain_and_the_swap_fixes_it(tmp_path):
    spec = load_spec(os.path.join(BENCH, "spec_seed_topology.yaml"))
    control_block = spec.canonical.control_block

    with open(os.path.join(BENCH, "netlist_seed_topology.cir")) as f:
        seeded = f.read()

    before = _measure(seeded, tmp_path / "before.cir", control_block)["buf0_gain_db"]
    assert before < 90.0

    fixed = apply_topology_swap(seeded, "BUF_P", TOPOLOGY_LIBRARY["folded_cascode_pmos_in_cs"].subckt_body)
    after = _measure(fixed, tmp_path / "after.cir", control_block)["buf0_gain_db"]
    assert after >= 90.0


def test_the_seeded_deck_offers_exactly_the_fixing_swap_as_a_candidate():
    with open(os.path.join(BENCH, "netlist_seed_topology.cir")) as f:
        seeded = f.read()

    cands, _rejections = compatible_swaps({"loops": seeded}, TOPOLOGY_LIBRARY, set())

    assert SwapCandidate("BUF_P", "folded_cascode_pmos_in_cs") in cands
