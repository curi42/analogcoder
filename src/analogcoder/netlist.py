import os
import re
from dataclasses import dataclass, field

_INCLUDE_RE = re.compile(r'^(\s*\.include\s+)"?([^"\s]+)"?\s*$', re.IGNORECASE | re.MULTILINE)


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


@dataclass
class Subckt:
    name: str
    ports: list[str]
    components: list[Component] = field(default_factory=list)


@dataclass
class ParsedNetlist:
    top_components: list[Component]
    subckts: dict[str, Subckt]


_PARAM_RE = re.compile(r"^(\w+)=(\S+)$")


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
    current_subckt: Subckt | None = None
    scale = netlist_scale(text)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("*"):
            continue
        lower = line.lower()
        if lower.startswith(".subckt"):
            tokens = line.split()
            name = tokens[1]
            ports = tokens[2:]
            current_subckt = Subckt(name=name, ports=ports)
            subckts[name] = current_subckt
            continue
        if lower.startswith(".ends"):
            current_subckt = None
            continue
        if line.startswith("."):
            continue
        component = _parse_component_line(line)
        component.geometry_scale = scale
        if current_subckt is not None:
            component.scope = current_subckt.name
            current_subckt.components.append(component)
        else:
            top_components.append(component)

    return ParsedNetlist(top_components=top_components, subckts=subckts)


def split_scoped_refdes(scoped: str) -> tuple[str | None, str]:
    """Splits "BUF_N.Xcc" into ("BUF_N", "Xcc") and a bare "Xcc" into
    (None, "Xcc"). One level only: the scope is a subckt DEFINITION name,
    which is unique within a netlist, so nesting never needs more."""
    scope, sep, refdes = scoped.rpartition(".")
    if not sep:
        return None, scoped
    return scope, refdes


def _line_scopes(lines: list[str]) -> list[str | None]:
    """For each line, the name of the .subckt it sits inside, or None at
    top level. Directive lines themselves are reported as None; they are
    skipped by every caller anyway."""
    scopes: list[str | None] = []
    current: str | None = None
    for raw_line in lines:
        stripped = raw_line.strip()
        lower = stripped.lower()
        if lower.startswith(".subckt"):
            scopes.append(None)
            current = stripped.split()[1]
            continue
        if lower.startswith(".ends"):
            scopes.append(None)
            current = None
            continue
        scopes.append(current)
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
        tokens = stripped.split()
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
            raise ValueError(
                f"refdes {change['refdes']!r} is ambiguous - it matches components in {', '.join(where)}; "
                f"qualify it as <subckt>.{refdes}"
            )

        i, tokens = matches[0]
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
        lines[i] = " ".join(tokens)
    return "\n".join(lines) + "\n"


def apply_topology_swap(text: str, subckt_name: str, new_body: str) -> str:
    lines = text.splitlines()
    start = end = None
    for i, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if stripped.lower().startswith(".subckt") and stripped.split()[1] == subckt_name:
            start = i
        elif start is not None and stripped.lower().startswith(".ends"):
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
