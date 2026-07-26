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


_PARAM_RE = re.compile(r"^(\w+)=(\S+)$")

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


def _is_subckt_open(lower: str) -> bool:
    return lower.startswith(".subckt") or lower.startswith(".macro")


def _is_subckt_close(lower: str) -> bool:
    return lower.startswith(".ends") or lower.startswith(".eom")


def _parse_component_line(line: str) -> Component:
    tokens = line.split()
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

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("*"):
            continue
        line, _ = strip_inline_comment(line)
        if not line:
            continue
        lower = line.lower()
        if _is_subckt_open(lower):
            tokens = line.split()
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
            stack.append(stripped.split()[1])
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
) -> list[tuple[int, list[str]]]:
    """Every non-directive line whose first token is refdes, restricted to
    scope when scope is not None. Shared by apply_changes (which acts on the
    match) and check_refdes_resolution (which only classifies it) so the two
    can never disagree about what a given <refdes> or <scope>.<refdes> means."""
    matches: list[tuple[int, list[str]]] = []
    for i, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("*") or stripped.startswith("."):
            continue
        code, _ = strip_inline_comment(stripped)
        if not code:
            continue
        tokens = code.split()
        if tokens[0] != refdes:
            continue
        if scope is not None and scopes[i] != scope:
            continue
        matches.append((i, tokens))
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
            where = sorted({scopes[i] or "<top-level>" for i, _ in matches})
            qualified = ", ".join(f"{s}.{refdes}" for s in where)
            violations.append(
                f"{scoped_refdes!r} is ambiguous - it matches components in {', '.join(where)}; "
                f"qualify it as one of: {qualified}"
            )

    if violations:
        return False, "; ".join(violations)
    return True, None


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
            where = sorted({scopes[i] or "<top-level>" for i, _ in matches})
            qualified = ", ".join(f"{s}.{refdes}" for s in where)
            raise ValueError(
                f"refdes {change['refdes']!r} is ambiguous - it matches components in {', '.join(where)}; "
                f"qualify it as one of: {qualified}"
            )

        i, tokens = matches[0]
        _, comment = strip_inline_comment(lines[i].strip())
        if param == "value":
            positional_idx = [j for j, t in enumerate(tokens) if "=" not in t]
            tokens[positional_idx[-1]] = new_value
        else:
            replaced = False
            for j, tok in enumerate(tokens):
                if tok.startswith(f"{param}="):
                    tokens[j] = f"{param}={new_value}"
                    replaced = True
                    break
            if not replaced:
                tokens.append(f"{param}={new_value}")
        lines[i] = " ".join(tokens) + (f" {comment}" if comment else "")
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
            if opens and stripped.split()[1] == subckt_name:
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
