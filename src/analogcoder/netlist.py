import os
import re
from dataclasses import dataclass, field

_INCLUDE_RE = re.compile(r'^(\s*\.inc(?:lude)?\s+)"?([^"\s]+)"?\s*$', re.IGNORECASE | re.MULTILINE)


def resolve_includes(text: str, base_dir: str) -> str:
    """Rewrites every top-level relative `.include` path in text to an
    absolute path anchored at base_dir, making the netlist text relocatable.

    Necessary because the pipeline moves netlist TEXT away from the directory
    it was read from: RunState stages each version into the run dir and
    NgspiceBackend then stages that into its own temp dir. ngspice resolves a
    top-level .include against the process CWD (which NgspiceBackend sets to
    the netlist's own directory), so a bare relative include silently stops
    resolving the moment the text is written anywhere else.

    Only applies to the top-level deck. A nested .include inside an already-
    included file (e.g. pdk_corner.inc's own "../../third_party/..." lines)
    resolves against THAT file's directory, not the CWD, so those must be
    left alone - and are, since this never rewrites the included files
    themselves."""

    def _absolutize(match: re.Match) -> str:
        prefix, path = match.group(1), match.group(2)
        if os.path.isabs(path):
            return match.group(0)
        return f'{prefix}"{os.path.join(base_dir, path)}"'

    return _INCLUDE_RE.sub(_absolutize, text)


_SCALE_RE = re.compile(r"^\s*\.option[s]?\b.*?\bscale\s*=\s*(\S+)", re.IGNORECASE | re.MULTILINE)


def netlist_scale(text: str) -> float:
    """The `.option scale=` multiplier applied to device geometry, or 1.0 when
    the deck sets none.

    sky130 decks in this repo set `scale=1.0u` and then write bare geometry
    (`W=30` meaning 30um). Any code that reads W/L as an absolute size is off
    by six orders of magnitude without this - which is exactly what made
    area_limits' size tiers inert on every PDK-backed benchmark."""
    match = _SCALE_RE.search(text)
    if not match:
        return 1.0
    try:
        return parse_spice_value(match.group(1))
    except ValueError:
        return 1.0


# 최상위 스코프의 독립 소스는 구조상 테스트벤치의 자극/전원이지 DUT가 아니다.
# 이 판정에는 PDK 지식도 이름 규칙도 필요 없다 - refdes 접두 V/I는 SPICE의
# 보장이고, "최상위에 놓였다"는 것은 파서가 아는 사실이다. 서브회로 안의
# 소스는 DUT의 일부(내부 바이어스)일 수 있으므로 여기 해당하지 않는다.
INDEPENDENT_SOURCE_CTYPES = frozenset({"V", "I"})


def is_top_level_stimulus(scope: str | None, ctype: str) -> bool:
    """최상위 스코프에 직접 놓인 독립 소스(V/I)인가.

    한 판정을 세 곳이 공유한다: structure.py(주소록에서 제외),
    structure_view.py("stimulus (not tunable)" 줄), check_stimulus_untouched
    (적용 거부). 셋이 갈라지면 "광고하지 않는데 적용은 된다" 또는 그 반대가
    생긴다."""
    return scope is None and ctype in INDEPENDENT_SOURCE_CTYPES


@dataclass
class Component:
    refdes: str
    ctype: str
    nodes: list[str]
    value: str | None
    params: dict[str, str] = field(default_factory=dict)
    raw_line: str = ""
    scope: str | None = None
    geometry_scale: float = 1.0
    resolved_params: dict[str, float] = field(default_factory=dict)
    resolved_value: float | None = None
    # 이 소자가 서브회로 인스턴스일 때, 인스턴스 줄의 파라미터 이름 ->
    # 그 값이 실제로 도달하는 소자/토큰들. params.annotate_traced_params가 채운다.
    traced_params: "dict[str, list[TracedTarget]]" = field(default_factory=dict)


@dataclass(frozen=True)
class TracedTarget:
    """인스턴스 줄의 파라미터 하나가 도달하는 소자와, 그 소자 자신의 토큰 이름.

    이 자료구조가 있는 이유는 "추측하지 말고 추적하라"는 규칙 때문이다.
    인스턴스 파라미터 이름(`wn`, `ma1`, `nf_n` …)은 설계자의 명명 규칙이므로
    거기서 의미를 읽어내는 것은 넷 이름 `vdd`를 보고 전원이라고 단정하는 것과
    같은 종류의 추측이다. 반면 그 값이 도달하는 **본문 토큰 이름**(`w`, `l`,
    `m`, `nf`)은 SPICE 표준 소자 문법이므로 사실이다. 그래서 면적 게이트는
    `wn`이 폭이라고 가정하지 않고, `wn`이 어떤 MOSFET의 `w`에 도착한다는 것을
    관측한다.

    total_width는 이 인스턴스에서 그 소자의 총 폭 = w x m (`.option scale`
    반영). m은 병렬 소자의 개수이므로 scale을 곱하지 않는다. 확정할 수 없으면
    None이고, 그때는 "면적 영향을 판단할 수 없다"로 취급한다."""

    device: Component
    token: str
    total_width: float | None


@dataclass
class Subckt:
    name: str
    ports: list[str]
    components: list[Component] = field(default_factory=list)
    path: str = ""
    defaults: dict[str, str] = field(default_factory=dict)


@dataclass
class ParsedNetlist:
    top_components: list[Component]
    subckts: dict[str, Subckt]


# 값 쪽이 `\S+`가 아닌 이유: split_tokens가 `W='wn * 2'`를 토큰 하나로
# 유지하므로 값에 공백이 들어올 수 있다. 토큰 하나에만 적용되는 패턴이라
# 탐욕적 `.+`로 충분하다.
_PARAM_RE = re.compile(r"^(\w+)=(.+)$")

# 인용을 여는 문자와 그에 대응해 닫는 문자. HSPICE는 표현식을 '...'로,
# ngspice는 {...}로 감싼다.
_QUOTE_PAIRS = {"'": "'", "{": "}"}

# HSPICE는 "$", ngspice는 ";"로 줄 끝 주석을 연다. 두 문자 모두 SPICE
# 식별자에 등장할 수 없으므로 첫 출현 위치에서 자르면 충분하다.
_COMMENT_MARKERS = "$;"


def strip_inline_comment(line: str) -> tuple[str, str]:
    """줄을 (코드부, 주석부)로 나눈다. 주석이 없으면 주석부는 빈 문자열.

    분리해서 돌려주는 이유는 apply_changes 때문이다. 그쪽은 코드를 토큰으로
    쪼개 다시 합치는데, 주석을 코드에 남겨두면 param="value"가 마지막 위치
    토큰(주석의 마지막 단어)을 소자 값으로 착각해 교체한다.

    이 함수는 `*` 줄 전체 주석을 모른다 - 그냥 첫 `$`/`;` 위치에서 자를
    뿐이다. 벤치마크 넷리스트의 `*` 주석 줄은 대부분 `$`나 `;`를 포함하므로,
    호출자가 `*` 줄을 먼저 걸러내지 않고 이 함수에 그대로 넘기면 주석
    내용이 코드부로 오인되어 잘못 잘린다."""
    positions = [line.find(marker) for marker in _COMMENT_MARKERS]
    positions = [p for p in positions if p != -1]
    if not positions:
        return line, ""
    index = min(positions)
    return line[:index].rstrip(), line[index:].strip()


def split_tokens(code: str) -> list[str]:
    """SPICE 줄의 코드부를 토큰으로 나눈다. `str.split()`과 달리 `'...'`와
    `{...}` 안의 공백은 토큰을 끊지 않는다.

    `str.split()`을 쓰면 `W='wn * 2'`가 `W='wn`, `*`, `2'` 세 토큰이 되어
    모델명이 노드 목록으로 밀려나고 value가 `2'`가 된다 - `$` 주석 버그와
    똑같이, 예외 하나 없이 디바이스 종류와 에어리어 티어가 함께 틀어진다.
    apply_changes가 토큰을 다시 `" "`로 잇기 때문에 편집도 덱을 망가뜨린다
    (`W='wn * 2'` -> `W=50` 이 `W=50 * 2'`를 남긴다).

    인용이 닫히지 않은 줄은 남은 전부를 한 토큰으로 돌려준다. 그런 줄은
    이미 SPICE로서 유효하지 않으므로, 여기서 추측해 봐야 얻을 것이 없다.

    주석은 다루지 않는다 - 호출자가 strip_inline_comment로 먼저 코드부만
    떼어 넘겨야 한다."""
    tokens: list[str] = []
    current: list[str] = []
    depth = 0  # 중괄호 중첩 깊이. 첫 '}'에서 닫으면 W={wn * {m + 1} } 가
    # 두 토큰으로 쪼개져 모델명이 노드로 밀려난다.
    quoted = False  # '...' 는 중첩하지 않으므로 깊이가 아니라 상태다.

    for char in code:
        if quoted:
            current.append(char)
            if char == "'":
                quoted = False
            continue
        if depth:
            current.append(char)
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            continue
        if char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            continue
        if char == "'":
            quoted = True
        elif char == "{":
            depth = 1
        current.append(char)

    if current:
        tokens.append("".join(current))
    return tokens


def logical_lines(lines: list[str]) -> list[tuple[str, list[int]]]:
    """물리 줄 목록을 논리 줄로 접는다. 각 항목은 (코드부, 물리 줄 인덱스들).
    빈 줄과 `*` 전체 주석 줄은 빠지고, 인라인 주석은 코드부에서 제거된다.

    SPICE에서 `+`로 시작하는 줄은 앞 문장의 연속이다. 접지 않으면 그 줄이
    새 문장으로 파싱되어 refdes가 `+`인 가짜 소자가 생기고, 원래 소자는
    자기 파라미터를 그 가짜에게 빼앗겨 `params={}`가 된다 - 에어리어 게이트에
    베이스라인이 없어진다는 뜻이다. 게다가 apply_changes가 그 소자에
    `W=99`를 덧붙이면 연속 줄의 `W=10`이 그대로 남아 덱에 `W`가 두 번
    나오는 상태가 된다.

    물리 줄 인덱스를 함께 돌려주는 이유는 편집 때문이다: 파싱은 접힌
    코드로 하지만, apply_changes는 토큰이 실제로 있는 물리 줄을 고쳐야
    하고 `_line_scopes`도 물리 줄 단위로 정렬되어 있다."""
    groups: list[tuple[list[str], list[int]]] = []
    for i, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("*"):
            continue
        code, _ = strip_inline_comment(stripped)
        if not code:
            continue
        if code.startswith("+"):
            # 앞선 문장이 없는 연속 줄은 이어붙일 곳이 없다. 유효한 SPICE가
            # 아니므로 버린다 - 가짜 소자를 만드는 것보다 낫다.
            if groups:
                groups[-1][0].append(code[1:].strip())
                groups[-1][1].append(i)
            continue
        groups.append(([code], [i]))
    return [(" ".join(parts), indices) for parts, indices in groups]


def _is_subckt_open(lower: str) -> bool:
    return lower.startswith(".subckt") or lower.startswith(".macro")


def _is_subckt_close(lower: str) -> bool:
    return lower.startswith(".ends") or lower.startswith(".eom")


def _parse_component_line(line: str) -> Component:
    tokens = split_tokens(line)
    refdes = tokens[0]
    ctype = refdes[0].upper()
    params: dict[str, str] = {}
    positional: list[str] = []
    for tok in tokens[1:]:
        m = _PARAM_RE.match(tok)
        if m:
            params[m.group(1)] = m.group(2)
        else:
            positional.append(tok)
    nodes = positional[:-1] if positional else []
    value = positional[-1] if positional else None
    return Component(refdes=refdes, ctype=ctype, nodes=nodes, value=value, params=params, raw_line=line)


def parse_netlist(text: str) -> ParsedNetlist:
    top_components: list[Component] = []
    subckts: dict[str, Subckt] = {}
    stack: list[Subckt] = []
    scale = netlist_scale(text)

    for line, _indices in logical_lines(text.splitlines()):
        lower = line.lower()
        if _is_subckt_open(lower):
            tokens = split_tokens(line)
            name = tokens[1]
            ports = [t for t in tokens[2:] if "=" not in t]
            defaults = dict(t.split("=", 1) for t in tokens[2:] if "=" in t)
            path = ".".join([s.name for s in stack] + [name])
            subckt = Subckt(name=name, ports=ports, path=path, defaults=defaults)
            subckts[path] = subckt
            stack.append(subckt)
            continue
        if _is_subckt_close(lower):
            if stack:
                stack.pop()
            continue
        if line.startswith("."):
            continue
        component = _parse_component_line(line)
        component.geometry_scale = scale
        if stack:
            component.scope = stack[-1].path
            stack[-1].components.append(component)
        else:
            top_components.append(component)

    return ParsedNetlist(top_components=top_components, subckts=subckts)


def split_scoped_refdes(scoped: str) -> tuple[str | None, str]:
    """"OUTER.INNER.M1"을 ("OUTER.INNER", "M1")로, 맨 refdes "Xcc"를
    (None, "Xcc")로 나눈다. 스코프는 서브회로 정의의 전체 경로이며 임의
    깊이로 중첩될 수 있으므로, 마지막 점에서 자른다."""
    scope, sep, refdes = scoped.rpartition(".")
    if not sep:
        return None, scoped
    return scope, refdes


def _line_scopes(lines: list[str]) -> list[str | None]:
    """각 줄이 속한 .subckt의 전체 경로("OUTER.INNER"), 최상위면 None.
    디렉티브 줄 자체는 None으로 보고하며, 모든 호출자가 어차피 건너뛴다."""
    scopes: list[str | None] = []
    stack: list[str] = []
    for raw_line in lines:
        pre_strip = raw_line.strip()
        if pre_strip.startswith("*"):
            # strip_inline_comment는 `*` 줄 전체 주석을 모른다 (Task 2) - 여기
            # 넘기면 주석 본문을 코드로 오인할 수 있다. `*` 줄은 절대
            # .subckt/.ends일 수 없으므로 그냥 현재 스코프를 그대로 기록한다.
            scopes.append(".".join(stack) if stack else None)
            continue
        stripped, _ = strip_inline_comment(pre_strip)
        lower = stripped.lower()
        if _is_subckt_open(lower):
            scopes.append(None)
            stack.append(split_tokens(stripped)[1])
            continue
        if _is_subckt_close(lower):
            scopes.append(None)
            if stack:
                stack.pop()
            continue
        scopes.append(".".join(stack) if stack else None)
    return scopes


def _find_matches(
    lines: list[str], scopes: list[str | None], scope: str | None, refdes: str
) -> list[tuple[list[int], list[str]]]:
    """Every non-directive logical line whose first token is refdes, restricted
    to scope when scope is not None. Shared by apply_changes (which acts on the
    match) and check_refdes_resolution (which only classifies it) so the two
    can never disagree about what a given <refdes> or <scope>.<refdes> means.

    논리 줄 단위로 돌되 물리 줄 인덱스 목록을 함께 돌려준다: 소자가 `+`
    연속 줄에 걸쳐 있으면 토큰은 접힌 것을 봐야 맞고, 편집은 그 토큰이 실제로
    있는 물리 줄에 가야 맞다. 스코프는 첫 물리 줄 것을 쓴다 - `_line_scopes`가
    물리 줄 단위이기 때문이다."""
    matches: list[tuple[list[int], list[str]]] = []
    for code, indices in logical_lines(lines):
        if code.startswith("."):
            continue
        tokens = split_tokens(code)
        if tokens[0] != refdes:
            continue
        if scope is not None and scopes[indices[0]] != scope:
            continue
        matches.append((indices, tokens))
    return matches


def check_refdes_resolution(text: str, changes: list[dict]) -> tuple[bool, str | None]:
    """Deterministic pre-apply gate: classifies each proposed change's refdes
    against text as resolving to exactly one component, matching nothing (including
    a scope that names no subckt, e.g. "M1.W"), or - unqualified - matching more
    than one scope. Run in the orchestrator's tuning retry loop immediately after
    check_area_growth and before verify_pre, same position/philosophy as the area
    gate, so an unresolvable or ambiguous proposal never spends an LLM call and
    never reaches apply_changes (which raises ValueError on the ambiguous case)."""
    parsed = parse_netlist(text)
    lines = text.splitlines()
    scopes = _line_scopes(lines)

    violations: list[str] = []
    for change in changes:
        scoped_refdes = change["refdes"]
        scope, refdes = split_scoped_refdes(scoped_refdes)

        if scope is not None and scope not in parsed.subckts:
            violations.append(f"{scoped_refdes!r} matches no component: no subckt named {scope!r} exists")
            continue

        matches = _find_matches(lines, scopes, scope, refdes)

        if not matches:
            violations.append(f"{scoped_refdes!r} matches no component in this netlist")
            continue

        if len(matches) > 1:
            where = sorted({scopes[idx[0]] or "<top-level>" for idx, _ in matches})
            qualified = ", ".join(f"{s}.{refdes}" for s in where)
            violations.append(
                f"{scoped_refdes!r} is ambiguous - it matches components in {', '.join(where)}; "
                f"qualify it as one of: {qualified}"
            )

    if violations:
        return False, "; ".join(violations)
    return True, None


def _numeric_or_none(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return parse_spice_value(raw)
    except ValueError:
        return None


def _peer_key(component: Component) -> str:
    """동료 판정에 쓸 정체성 키. X 인스턴스는 마지막 위치 토큰이 모델/서브회로
    이름이라 그대로 쓸 수 있지만, 일반 R/C/L은 같은 자리가 리터럴 값
    ("10k")이다 - 그걸 키로 쓰면 값이 다른 두 저항(R1=10k, R2=5k)이 다른
    그룹으로 갈라져 Xq1.m이 도달 불가능해지는 것과 똑같은 실패가 제네릭
    소자에서 재현되고, 반대로 값이 우연히 같은 저항과 커패시터(둘 다
    "10k")는 서로 무관한데 동료로 오인된다. 위치 값이 숫자로 파싱되면
    그건 모델명이 아니라 소자 값이므로 ctype으로 떨어뜨린다."""
    if component.value is not None and _numeric_or_none(component.value) is None:
        return component.value
    return component.ctype


def check_param_applicability(text: str, changes: list[dict]) -> tuple[bool, str | None]:
    """결정론적 사전 게이트: 제안된 param이 그 소자에 실제로 적용될 수 있는가.

    오케스트레이터의 튜닝 재시도 루프에서 check_refdes_resolution 직후,
    verify_pre 직전에 돈다 - 에어리어/refdes 게이트와 같은 자리이자 같은
    철학이다. 적용 불가능한 제안은 LLM 호출을 쓰지 않는다.

    잡는 결함은 실측된 것이다: param="width"인 제안이 refdes 게이트를 통과해
    `X6 ... L=1 W=20 width=55`를 만들고, 넷리스트는 바뀌었는데 소자는 그대로라
    시뮬레이션에 변화가 없고 verify_post가 롤백한다 - 아무도 볼 수 없는
    이유로 iteration 하나를 태운다.

    줄에 없는 param을 무조건 거부하지는 않는다. bandgap의 Xq1에는 m=이
    없지만 같은 모델의 Xq8이 m=8을 쓰고, m은 이 회로의 이미터 면적비를 정하는
    유일한 노브다. 동료 인스턴스가 쓰는 이름은 정당한 것으로 본다 - 하드코딩된
    PDK 표 없이 덱만 보고 판정하므로 정확하고, width 같은 헛소리는 여전히
    걸린다.

    refdes가 아예 매칭되지 않는 경우는 이 게이트가 말하지 않는다 - 그건
    check_refdes_resolution의 몫이고, 이 게이트 바로 앞에서 이미 걸렀어야
    한다. 같은 결함을 두 게이트가 다른 말로 보고하면 하나만 보고하는 것보다
    나쁘다."""
    parsed = parse_netlist(text)
    everything = list(parsed.top_components) + [
        c for subckt in parsed.subckts.values() for c in subckt.components
    ]
    by_refdes: dict[str, Component] = {}
    for component in everything:
        by_refdes[component.refdes] = component
        if component.scope:
            by_refdes[f"{component.scope}.{component.refdes}"] = component

    # 같은 모델명(없으면 같은 ctype)을 쓰는 소자들이 실제로 쓰는 param 이름.
    peers: dict[str, set[str]] = {}
    for component in everything:
        peers.setdefault(_peer_key(component), set()).update(component.params)

    violations: list[str] = []
    for change in changes:
        scoped_refdes = change["refdes"]
        param = change["param"]
        component = by_refdes.get(scoped_refdes)
        if component is None:
            # refdes 게이트가 앞서 걸렀어야 한다. 여기서는 판단하지 않는다.
            continue

        if param == "value":
            if _numeric_or_none(component.value) is None:
                violations.append(
                    f"{scoped_refdes!r}: param=\"value\" would overwrite the positional token "
                    f"{component.value!r}, which is not a number - it is this component's model "
                    f"or subckt name. Change a named parameter instead."
                )
            continue

        if param in component.params:
            continue

        key = _peer_key(component)
        if param in peers.get(key, set()):
            continue

        available = sorted(component.params) or ["<none>"]
        peer_names = sorted(peers.get(key, set()) - set(component.params))
        violations.append(
            f"{scoped_refdes!r}: {param!r} is not a parameter of this component. It writes "
            f"{available}; other {key!r} instances in this netlist write {peer_names or ['<none>']}. "
            f"Adding an unknown name changes the netlist text without changing the device."
        )

    if violations:
        return False, "; ".join(violations)
    return True, None


def check_stimulus_untouched(text: str, changes: list[dict]) -> tuple[bool, str | None]:
    """결정론적 사전 게이트: 최상위 테스트벤치의 독립 소스(V/I)를 건드리는가.

    다른 게이트들과 같은 자리(튜닝 재시도 루프, verify_pre 앞)에서 돌고, 같은
    방식으로 재시도 가능한 피드백을 낸다.

    리뷰어가 실측한 시나리오를 막는다: `Vin in 0 AC 1`을 `AC 100`으로 바꾸면
    `gain_db = vdb(vout)`가 20dB에서 60dB로 뛴다. 면적 게이트는 V/I 티어가
    없어 통과시키고, refdes/param 게이트는 적용 가능성만 보므로 역시
    통과시키며, judge는 모든 기준이 좋아졌으니 PASS를 내고 verify_post는
    롤백할 이유를 못 찾는다 - **회로를 하나도 안 고친 채로 PASS가 난다.**

    structure.py가 같은 판정으로 이 소자들을 주소록에서 빼지만, 주소록은
    LLM에게 하는 권고일 뿐이라 약한 모델이 그대로 제안할 수 있다. 결과가
    "안 고친 회로에 PASS"인 이상 권고만으로는 부족하다.

    이름(vdd/vss/gnd)으로 알아보지 않는다 - 그건 추측이다. refdes 접두 V/I는
    SPICE의 보장이고 "최상위에 놓였다"는 것은 파서가 아는 사실이므로, 이
    판정은 정확하다. 서브회로 안의 소스는 DUT의 내부 바이어스일 수 있으므로
    건드리지 않는다."""
    parsed = parse_netlist(text)
    top_sources = {
        c.refdes for c in parsed.top_components if is_top_level_stimulus(None, c.ctype)
    }

    violations: list[str] = []
    for change in changes:
        scope, refdes = split_scoped_refdes(change["refdes"])
        if scope is not None or refdes not in top_sources:
            continue
        violations.append(
            f"{change['refdes']!r} is a top-level independent source - it is the testbench "
            f"stimulus or supply, not part of the circuit under test. Changing it changes "
            f"what is measured rather than the design. Propose a change to a component "
            f"inside the circuit instead."
        )

    if violations:
        return False, "; ".join(violations)
    return True, None


def resolve_change_scopes(text: str, changes: list[dict]) -> set[str]:
    """제안된 변경들이 실제로 위치한 서브회로 정의 경로의 집합(최상위 스코프는
    담지 않는다 - 최상위는 언제나 초점이므로 호출자가 신경 쓸 필요가 없다).

    check_refdes_resolution/apply_changes와 같은 조회(_find_matches +
    _line_scopes)를 그대로 써서 판정한다 - refdes 앞의 점만 잘라 스코프로
    읽으면(문자열 분리) 언스코프 refdes("M6")가 실제로는 비초점 서브회로
    안에 있어도 그 사실을 알 수 없다. 이미 스코프가 붙은 refdes
    ("AMP.M6")는 그 서브회로가 실제로 존재하는지만 확인한다.

    이 함수를 부르는 시점에는 보통 check_refdes_resolution이 이미 통과한
    뒤라(따라서 각 refdes가 유일하게 해석된다) 결과가 스코프 하나뿐이지만,
    방어적으로 여러 매치가 와도 전부의 스코프를 모아 돌려준다 - 판단은
    호출자의 몫이다."""
    parsed = parse_netlist(text)
    lines = text.splitlines()
    scopes = _line_scopes(lines)

    resolved: set[str] = set()
    for change in changes:
        scope, refdes = split_scoped_refdes(change["refdes"])
        if scope is not None:
            if scope in parsed.subckts:
                resolved.add(scope)
            continue
        for indices, _tokens in _find_matches(lines, scopes, None, refdes):
            line_scope = scopes[indices[0]]
            if line_scope is not None:
                resolved.add(line_scope)
    return resolved


def _rewrite_line(lines: list[str], index: int, mutate) -> bool:
    """물리 줄 하나를 토큰 단위로 고쳐 쓴다. mutate(tokens)가 False를 돌려주면
    그 줄에는 대상이 없다는 뜻이므로 아무것도 바꾸지 않는다.

    `+` 연속 접두와 인라인 주석은 보존한다 - 접두를 잃으면 그 줄이 새 문장이
    되어 다음 파싱에서 가짜 소자가 된다."""
    code, comment = strip_inline_comment(lines[index].strip())
    prefix, body = ("+ ", code[1:].strip()) if code.startswith("+") else ("", code)
    tokens = split_tokens(body)
    if not mutate(tokens):
        return False
    lines[index] = prefix + " ".join(tokens) + (f" {comment}" if comment else "")
    return True


def _replace_positional(new_value: str):
    def mutate(tokens: list[str]) -> bool:
        positional = [j for j, t in enumerate(tokens) if "=" not in t]
        if not positional:
            return False
        tokens[positional[-1]] = new_value
        return True

    return mutate


def _replace_param(param: str, new_value: str):
    """마지막 출현을 고친다. `_parse_component_line`이 접힌 줄을 last-wins로
    읽으므로(뒤에 나온 `W=`가 앞의 것을 덮는다) 편집도 같은 것을 골라야
    한다. 첫 출현을 고치면 게이트와 튜너가 믿는 값과 ngspice가 실제로 쓰는
    값이 갈린다 - 하필 이 파일이 방금 없앤 `W` 중복 덱에서 재발한다."""

    def mutate(tokens: list[str]) -> bool:
        for j in range(len(tokens) - 1, -1, -1):
            if tokens[j].startswith(f"{param}="):
                tokens[j] = f"{param}={new_value}"
                return True
        return False

    return mutate


def _append_param(param: str, new_value: str):
    def mutate(tokens: list[str]) -> bool:
        tokens.append(f"{param}={new_value}")
        return True

    return mutate


def apply_changes(text: str, changes: list[dict]) -> str:
    lines = text.splitlines()
    scopes = _line_scopes(lines)
    for change in changes:
        scope, refdes = split_scoped_refdes(change["refdes"])
        param = change["param"]
        new_value = change["new_value"]

        matches = _find_matches(lines, scopes, scope, refdes)

        if not matches:
            continue
        if len(matches) > 1:
            where = sorted({scopes[idx[0]] or "<top-level>" for idx, _ in matches})
            qualified = ", ".join(f"{s}.{refdes}" for s in where)
            raise ValueError(
                f"refdes {change['refdes']!r} is ambiguous - it matches components in {', '.join(where)}; "
                f"qualify it as one of: {qualified}"
            )

        indices, _tokens = matches[0]
        if param == "value":
            # 마지막 위치 토큰을 가진 물리 줄을 뒤에서부터 찾는다. 연속 줄은
            # 보통 name=value 뿐이라 위치 토큰이 없고, 값은 첫 줄에 있다.
            for index in reversed(indices):
                if _rewrite_line(lines, index, _replace_positional(new_value)):
                    break
        elif not any(
            _rewrite_line(lines, i, _replace_param(param, new_value)) for i in reversed(indices)
        ):
            # 어느 물리 줄에도 없는 param은 마지막 줄에 덧붙인다. 첫 줄에
            # 붙이면 연속 줄에 같은 이름이 있을 때 덱에 두 번 나오게 된다.
            _rewrite_line(lines, indices[-1], _append_param(param, new_value))
    return "\n".join(lines) + "\n"


def apply_topology_swap(text: str, subckt_name: str, new_body: str) -> str:
    lines = text.splitlines()
    start = end = None
    depth = 0
    for i, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if stripped.startswith("*"):
            continue
        stripped, _ = strip_inline_comment(stripped)
        lower = stripped.lower()
        opens = _is_subckt_open(lower)
        closes = _is_subckt_close(lower)
        if start is None:
            if opens and split_tokens(stripped)[1] == subckt_name:
                start = i
                depth = 1
            continue
        if opens:
            depth += 1
        elif closes:
            depth -= 1
            if depth == 0:
                end = i
                break
    if start is None or end is None:
        raise ValueError(f"subckt {subckt_name!r} not found or not closed")
    new_lines = lines[: start + 1] + new_body.splitlines() + lines[end:]
    return "\n".join(new_lines) + "\n"


_SPICE_VALUE_RE = re.compile(r"^(-?[0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)([a-zA-Z]*)$")

# Longest/most-specific suffix first: "meg" must be checked before "m", or
# "1.5meg" would incorrectly match "m" (milli) since "meg".startswith("m").
_SPICE_SUFFIXES = [
    ("meg", 1e6),
    ("t", 1e12),
    ("g", 1e9),
    ("k", 1e3),
    ("m", 1e-3),
    ("u", 1e-6),
    ("n", 1e-9),
    ("p", 1e-12),
    ("f", 1e-15),
]


def parse_spice_value(s: str) -> float:
    match = _SPICE_VALUE_RE.match(s.strip())
    if not match:
        raise ValueError(f"not a valid SPICE numeric literal: {s!r}")
    number_str, suffix = match.groups()
    number = float(number_str)
    suffix_lower = suffix.lower()
    for name, multiplier in _SPICE_SUFFIXES:
        if suffix_lower.startswith(name):
            return number * multiplier
    return number
