import re
from dataclasses import dataclass, field


@dataclass
class Component:
    refdes: str
    ctype: str
    nodes: list[str]
    value: str | None
    params: dict[str, str] = field(default_factory=dict)
    raw_line: str = ""


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
