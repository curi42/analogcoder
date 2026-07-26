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


@dataclass
class Component:
    refdes: str
    ctype: str
    nodes: list[str]
    value: str | None
    params: dict[str, str] = field(default_factory=dict)
    raw_line: str = ""
    scope: str | None = None


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
        if current_subckt is not None:
            component.scope = current_subckt.name
            current_subckt.components.append(component)
        else:
            top_components.append(component)

    return ParsedNetlist(top_components=top_components, subckts=subckts)


def apply_changes(text: str, changes: list[dict]) -> str:
    lines = text.splitlines()
    for change in changes:
        refdes = change["refdes"]
        param = change["param"]
        new_value = change["new_value"]
        for i, raw_line in enumerate(lines):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("*") or stripped.startswith("."):
                continue
            tokens = stripped.split()
            if tokens[0] != refdes:
                continue
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
            break
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
