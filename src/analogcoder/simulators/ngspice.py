import os
import re
import shutil
import subprocess
import tempfile

from analogcoder.simulators.base import RawSimResult, SimulatorBackend

_MEASURE_RE = re.compile(r"^(\w+)\s*=\s*([-+0-9.eE]+)\s*$")


class NgspiceBackend(SimulatorBackend):
    def __init__(self, ngspice_bin: str = "ngspice", timeout: float = 60):
        self.ngspice_bin = ngspice_bin
        self.timeout = timeout

    def identity(self) -> str:
        """캐시 키에 들어가는 시뮬레이터 식별자: 해석된 바이너리 절대 경로 +
        `ngspice -v`가 보고하는 버전 + timeout.

        **timeout이 여기 있는 이유**는 그것이 결과를 바꿀 수 있기 때문이다 -
        같은 덱이 timeout=10에서는 `status="error"`, timeout=180에서는
        `success`가 된다. 그 timeout 결과는 캐시되지 않지만(`cacheable=False`),
        서로 다른 timeout의 백엔드 둘이 한 캐시를 공유하는 상황 자체를 키가
        갈라 놓는 편이 옳다.

        `shutil.which`로 해석하는 이유는 PATH가 바뀌면 같은 `"ngspice"`라는
        이름이 다른 바이너리를 가리키기 때문이다. 못 찾으면 이름을 그대로
        싣는다 - 그때는 실행 자체가 실패하므로 캐시에 남지 않는다."""
        resolved = shutil.which(self.ngspice_bin) or self.ngspice_bin
        # 순환 import를 피해 지연 import한다(cache.py가 base.py를 통해 이쪽을
        # 참조하지는 않지만, 버전 조회 헬퍼는 cache.py에 산다).
        from analogcoder.simulators.cache import ngspice_version

        return f"ngspice|{resolved}|{ngspice_version(resolved)}|timeout={self.timeout}"

    def run(self, netlist_path: str, testbench_config: dict) -> RawSimResult:
        with open(netlist_path) as f:
            lines = f.readlines()

        netlist_dir = os.path.dirname(os.path.abspath(netlist_path))

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
                    cwd=netlist_dir,
                )
            # 아래 두 실패는 **환경이 낸 결과**라 순수 함수가 아니다. 부하가
            # 몰린 한 번의 timeout이 캐시에 들어가면 그 실행 내내 같은 점이
            # 실패로 못박힌다. cacheable=False로 표시해 캐시가 담지 않게 한다.
            except subprocess.TimeoutExpired:
                return RawSimResult(
                    status="error",
                    measurements={},
                    raw_log=f"ngspice timed out after {self.timeout}s",
                    warnings=[],
                    cacheable=False,
                )
            except FileNotFoundError:
                return RawSimResult(
                    status="error",
                    measurements={},
                    raw_log=f"ngspice binary not found: {self.ngspice_bin}",
                    warnings=[],
                    cacheable=False,
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
