import os
import shutil

from analogcoder.netlist import resolve_includes
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec

BENCHMARK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "two_stage_opamp")


def test_resolve_includes_rewrites_relative_include_to_absolute():
    text = '* test\n.include "pdk_corner.inc"\nR1 in 0 1k\n'

    resolved = resolve_includes(text, "/benchmarks/two_stage_opamp")

    assert '.include "/benchmarks/two_stage_opamp/pdk_corner.inc"' in resolved


def test_resolve_includes_leaves_absolute_include_unchanged():
    text = '* test\n.include "/somewhere/else/pdk_corner.inc"\nR1 in 0 1k\n'

    resolved = resolve_includes(text, "/benchmarks/two_stage_opamp")

    assert '.include "/somewhere/else/pdk_corner.inc"' in resolved


def test_resolve_includes_handles_unquoted_include_path():
    text = "* test\n.include pdk_corner.inc\nR1 in 0 1k\n"

    resolved = resolve_includes(text, "/benchmarks/two_stage_opamp")

    assert '.include "/benchmarks/two_stage_opamp/pdk_corner.inc"' in resolved


def test_resolve_includes_leaves_netlist_without_includes_unchanged():
    text = "* test\nR1 in 0 1k\nV1 in 0 DC 1\n.end\n"

    assert resolve_includes(text, "/benchmarks/two_stage_opamp") == text


def test_benchmark_netlist_simulates_after_being_staged_into_a_run_dir(tmp_path):
    # This is the exact path the orchestration loop takes and the exact bug it
    # hit: RunState.push_netlist_version writes each testbench's netlist text
    # into the run dir, and the loop then simulates THAT copy. A bare relative
    # `.include "pdk_corner.inc"` cannot resolve from the run dir, so every
    # simulation returned status="error" with no measurements. Reading the
    # netlist through resolve_includes at load time must make the text
    # relocatable, so the staged copy simulates identically to the original.
    spec = load_spec(os.path.join(BENCHMARK_DIR, "spec.yaml"))
    tb = spec.canonical

    with open(tb.netlist_path) as f:
        text = resolve_includes(f.read(), os.path.dirname(tb.netlist_path))

    staged = tmp_path / "netlist_v0_ac_loop_gain.cir"
    staged.write_text(text)

    result = NgspiceBackend().run(str(staged), {"control_block": tb.control_block})

    assert result.status == "success"
    assert "could not find include file" not in result.raw_log.lower()
    assert result.measurements["gain_db"] > 60.0


def test_staged_benchmark_netlist_without_include_resolution_fails(tmp_path):
    # Guards the regression test above: proves the staged-copy scenario really
    # does fail without resolve_includes, so a future change that makes
    # resolve_includes a no-op cannot leave the test above passing vacuously.
    spec = load_spec(os.path.join(BENCHMARK_DIR, "spec.yaml"))
    tb = spec.canonical

    staged = tmp_path / "netlist_v0_ac_loop_gain.cir"
    shutil.copy(tb.netlist_path, staged)

    result = NgspiceBackend().run(str(staged), {"control_block": tb.control_block})

    assert result.status == "error"
    assert "could not find include file" in result.raw_log.lower()
