"""measurement 이름 -> 그것이 관측하는 넷 이름 집합.

criterion(spec.yaml)은 넷이 아니라 measurement 이름(`vbg0_v` 등)을 참조한다.
그 measurement가 실제로 어떤 넷을 보는지는 테스트벤치의 ngspice control block
(`meas`/`.meas`, `let`)에만 적혀 있다 - 이 모듈은 그 둘을 잇는 다리다. 다운스트림
(초점 블록 선정)이 이 매핑에 없는 이름을 만나면 "전 블록 노출" 폴백으로
빠지므로, 여기서 놓치면 초점 메커니즘 자체가 조용히 무력화된다.
"""

import re

# v(a), v(a,b), vdb(a), i(Vdd) 같은 참조. ngspice 함수명은 v/i 뒤에 db/p/r/m
# 같은 접미사가 붙는 형태(vdb, vp, ...)이므로 [vi]가 접미사 앞이 아니라 맨
# 앞에 와야 한다 - 접두사 자리에 두면 vdb(...)의 마지막 글자 b가 걸려 매치가
# 안 된다. 넷 이름과 전류원 이름 모두 여기로 잡힌다 - i(Vdd)는 넷이 아니라
# 전압원 이름이지만, 다운스트림은 "이 measurement가 관측하는 대상"을 넷/소스
# 구분 없이 동일하게 취급하므로 그대로 반환한다.
_REFERENCE_RE = re.compile(r"\b[vi][a-z]*\s*\(\s*([^)]*)\)", re.IGNORECASE)
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_MEAS_PREFIXES = ("meas", ".meas")


def _direct_references(text: str) -> set[str]:
    nets: set[str] = set()
    for inner in _REFERENCE_RE.findall(text):
        for part in inner.split(","):
            name = part.strip()
            if name:
                nets.add(name)
    return nets


def _resolve(text: str, known: dict[str, set[str]]) -> set[str]:
    """`text` 하나가 관측하는 넷 집합.

    먼저 `v(...)`/`i(...)` 형태로 직접 언급된 넷을 찾는다. 하나도 없으면
    (예: `FIND tmag AT=1`처럼 중간 변수만 참조하는 meas, 또는
    `(vmax-vmin)/...`처럼 다른 measurement를 참조하는 let), 그 안의
    식별자 중 지금까지 알려진 이름과 겹치는 것을 통해 넷 집합을 물려받는다.
    """
    direct = _direct_references(text)
    if direct:
        return direct
    nets: set[str] = set()
    for token in _IDENTIFIER_RE.findall(text):
        nets |= known.get(token, set())
    return nets


def measurement_nets(control_block: str) -> dict[str, set[str]]:
    """measurement 이름 -> 그것이 관측하는 넷 이름 집합.

    한 줄씩 등장 순서대로 처리하며 그때까지 알려진 이름을 누적한다. 순서가
    중요한 이유는 실제 컨트롤 블록에서 `let tmag = ...`가 (다른 alter/ac 블록
    사이에서) 여러 번 재정의되고, 각 재정의 직후의 `meas ... tmag ...`가 그
    시점의 값을 참조하기 때문이다 - 재정의 전체를 한 이름에 뭉뚱그리면 마지막
    정의만 남아 앞쪽 measurement들이 엉뚱한 넷을 가리키게 된다. `meas`/`let`
    모두 같은 두 단계로 해소한다: 넷을 직접 언급하면 그것을 쓰고, 아니면 이미
    알려진 다른 이름(대개 앞서 정의된 meas/let)을 통해 물려받는다. 두 방법
    모두 실패하면 빈 집합으로 남는다 - "모른다"를 부재가 아니라 명시적인
    사실로 남기기 위함이다.
    """
    result: dict[str, set[str]] = {}

    for raw_line in control_block.splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if lowered.startswith(_MEAS_PREFIXES):
            tokens = line.split()
            # meas <analysis> <name> <func> <ref...>
            if len(tokens) < 3:
                continue
            name = tokens[2]
            result[name] = _resolve(" ".join(tokens[3:]), result)
        elif lowered.startswith("let "):
            name, sep, expression = line[4:].partition("=")
            if not sep:
                continue
            result[name.strip()] = _resolve(expression, result)

    return result
