import os
import re
from dataclasses import dataclass

from analogcoder.spec import Criterion, PVTCorners


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


@dataclass(frozen=True)
class CornerPoint:
    process: str
    voltage: float
    temperature: float


def all_corners(pvt: PVTCorners) -> list[CornerPoint]:
    return [
        CornerPoint(process=p, voltage=v, temperature=t)
        for p in pvt.process
        for v in pvt.voltage
        for t in pvt.temperature
    ]


def worst_case_measurements(
    corners: list[CornerPoint], per_corner_measurements: list[dict], criteria: list[Criterion]
) -> tuple[dict, dict]:
    """For each criterion, finds the worst-case value across
    per_corner_measurements (parallel to corners) - the minimum observed
    value if the criterion's operator is ">=" or ">", the maximum
    otherwise. Returns (worst_case_measurements, worst_case_corners), where
    worst_case_corners maps each criterion's name to the corner (plus the
    value) that produced its worst case, for diagnostics. A criterion whose
    measurement never appears in any corner's results is skipped (not an
    error here - evaluate_criteria's caller is responsible for treating a
    missing measurement as a failure)."""
    measurements: dict[str, float] = {}
    worst_corners: dict[str, dict] = {}
    for criterion in criteria:
        values_with_corner = [
            (m[criterion.measurement], corner)
            for m, corner in zip(per_corner_measurements, corners)
            if criterion.measurement in m
        ]
        if not values_with_corner:
            continue
        if criterion.operator in (">=", ">"):
            value, corner = min(values_with_corner, key=lambda vc: vc[0])
        else:
            value, corner = max(values_with_corner, key=lambda vc: vc[0])
        measurements[criterion.measurement] = value
        worst_corners[criterion.name] = {
            "process": corner.process,
            "voltage": corner.voltage,
            "temperature": corner.temperature,
            "value": value,
        }
    return measurements, worst_corners
