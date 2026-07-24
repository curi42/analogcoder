import os

from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec

BENCHMARK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "inverting_amp")


def test_ngspice_backend_runs_inverting_amp_benchmark():
    netlist_path = os.path.join(BENCHMARK_DIR, "netlist.cir")
    spec = load_spec(os.path.join(BENCHMARK_DIR, "spec.yaml"))

    backend = NgspiceBackend()
    result = backend.run(netlist_path, {"control_block": spec.control_block})

    assert result.status == "success"
    assert "gain_db" in result.measurements
    assert 19.0 <= result.measurements["gain_db"] <= 21.0


def test_ngspice_backend_reports_error_on_bad_netlist(tmp_path):
    bad_netlist = tmp_path / "bad.cir"
    bad_netlist.write_text("Rin in vminus\n.end\n")  # missing value token

    backend = NgspiceBackend()
    result = backend.run(str(bad_netlist), {"control_block": ".control\nac dec 10 1 1meg\n.endc"})

    assert result.status == "error"
