import os
import re
import subprocess
import tempfile

from analogcoder.simulators.base import RawSimResult, SimulatorBackend

_MEASURE_RE = re.compile(r"^(\w+)\s*=\s*([-+0-9.eE]+)\s*$")


class NgspiceBackend(SimulatorBackend):
    def __init__(self, ngspice_bin: str = "ngspice", timeout: float = 60):
        self.ngspice_bin = ngspice_bin
        self.timeout = timeout

    def run(self, netlist_path: str, testbench_config: dict) -> RawSimResult:
        with open(netlist_path) as f:
            lines = f.readlines()

        body = [ln for ln in lines if ln.strip().lower() != ".end"]
        control_block = testbench_config["control_block"]
        deck = "".join(body) + "\n" + control_block + "\n.end\n"

        with tempfile.TemporaryDirectory() as tmpdir:
            deck_path = os.path.join(tmpdir, "deck.cir")
            with open(deck_path, "w") as f:
                f.write(deck)

            try:
                proc = subprocess.run(
                    [self.ngspice_bin, "-b", deck_path],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                return RawSimResult(
                    status="error",
                    measurements={},
                    raw_log=f"ngspice timed out after {self.timeout}s",
                    warnings=[],
                )
            except FileNotFoundError:
                return RawSimResult(
                    status="error",
                    measurements={},
                    raw_log=f"ngspice binary not found: {self.ngspice_bin}",
                    warnings=[],
                )
            log_text = proc.stdout + proc.stderr

        measurements: dict[str, float] = {}
        for line in log_text.splitlines():
            m = _MEASURE_RE.match(line.strip())
            if m:
                measurements[m.group(1)] = float(m.group(2))

        warnings = [ln for ln in log_text.splitlines() if "warning" in ln.lower()]

        lower_log = log_text.lower()
        if "no convergence" in lower_log or "singular matrix" in lower_log:
            status = "convergence_failure"
        elif proc.returncode != 0 or not measurements:
            status = "error"
        else:
            status = "success"

        return RawSimResult(status=status, measurements=measurements, raw_log=log_text, warnings=warnings)
