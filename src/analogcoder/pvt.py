import os
import re


def render_corner_netlist(
    netlist_text: str, process: str, voltage: float, temperature: float, benchmark_dir: str
) -> str:
    """Renders netlist_text for one PVT corner: swaps which process-corner
    PDK include file is used, injects a .temp directive, and sets Vdd's DC
    value - all via absolute paths / targeted regexes, not the tuner's
    apply_changes (verified unsafe here: apply_changes's generic positional-
    token targeting would hit the AC magnitude, not the DC value, on a Vdd
    line with a trailing "AC 1" clause, e.g. netlist_psr_plus.cir)."""
    include_name = "pdk_corner.inc" if process == "tt" else f"pdk_corner_{process}.inc"
    abs_include = os.path.join(benchmark_dir, include_name)
    text = netlist_text.replace('.include "pdk_corner.inc"', f'.include "{abs_include}"')

    include_line_pattern = re.compile(r'(\.include "' + re.escape(abs_include) + r'"\n)')
    text = include_line_pattern.sub(lambda m: m.group(1) + f".temp {temperature}\n", text, count=1)

    text = re.sub(r"^(Vdd\s+\S+\s+\S+\s+DC\s+)\S+", rf"\g<1>{voltage}", text, flags=re.MULTILINE)

    return text
