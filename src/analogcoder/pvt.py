import math
import os
import re
import tempfile
from dataclasses import dataclass

from analogcoder.judge_tools import evaluate_criteria
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

    # Matched by basename, not by the exact relative string, because the
    # netlist text reaching here has already been through
    # netlist.resolve_includes - so its include is an absolute path, and on
    # the FINAL sweep it comes back out of RunState in that same absolute
    # form. An exact-match on the bare relative form would silently no-op,
    # leaving all 45 corners running the tt models at the default temperature.
    corner_include_pattern = re.compile(r'^\s*\.include\s+"?\S*pdk_corner\.inc"?\s*$', re.MULTILINE)
    text = corner_include_pattern.sub(f'.include "{abs_include}"', netlist_text, count=1)

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
    value) that produced its worst case, for diagnostics.

    If ANY corner fails to produce a criterion's measurement (not just all
    of them), that criterion's measurement is withheld entirely from the
    returned dict, so evaluate_criteria's existing missing-measurement
    handling fails it - a corner that doesn't produce an expected
    measurement (e.g. an AC response that never crosses 0dB) is itself
    evidence the circuit doesn't function there, and must not be silently
    excluded from the worst-case pool while other corners paper over it."""
    measurements: dict[str, float] = {}
    worst_corners: dict[str, dict] = {}
    for criterion in criteria:
        values_with_corner = []
        missing_corners = []
        for m, corner in zip(per_corner_measurements, corners):
            if criterion.measurement in m:
                values_with_corner.append((m[criterion.measurement], corner))
            else:
                missing_corners.append(corner)

        if not values_with_corner:
            continue  # measurement never appears anywhere - nothing to report a corner for

        if missing_corners:
            corner = missing_corners[0]
            worst_corners[criterion.name] = {
                "process": corner.process,
                "voltage": corner.voltage,
                "temperature": corner.temperature,
                "value": None,
            }
            continue  # withhold the measurement so evaluate_criteria fails it as missing

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


def corner_severity(measurements: dict, criteria: list[Criterion]) -> float:
    """The tightest normalised margin among criteria at this corner. Smaller
    is worse.

    Each corner's worst criterion differs, but the probe order (a later task)
    needs one number per corner, so a corner has to be summarised down to a
    single value. Normalising by threshold magnitude (rather than raw
    difference) makes criteria with different units and thresholds
    comparable; the sign is corrected so that passing is always positive
    regardless of the criterion's operator direction.

    Any criterion missing its measurement makes this -inf. Skipping that
    criterion instead would let a corner where the circuit didn't even
    produce a measurement read as "comfortable" - the same logic
    worst_case_measurements already applies by withholding a measurement
    that's missing at any corner."""
    worst = math.inf
    for criterion in criteria:
        value = measurements.get(criterion.measurement)
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return -math.inf
        denominator = abs(criterion.threshold) or 1.0
        margin = (value - criterion.threshold) / denominator
        if criterion.operator in ("<=", "<"):
            margin = -margin
        worst = min(worst, margin)
    return worst


def run_full_pvt_sweep(netlist_texts: dict[str, str], spec, sim_backend) -> dict:
    """Runs spec.pvt_corners' full cross product against every testbench,
    directly via sim_backend (no LLM agent involved - corner variation is
    purely mechanical). Returns the worst-case-per-criterion result in the
    same shape evaluate_criteria() returns, plus a worst_case_corners
    breakdown mapping each criterion's name to the corner that produced its
    worst-case value, for diagnostics, and a per_corner breakdown (parallel
    to all_corners(spec.pvt_corners)) exposing each corner's own merged
    measurements and severity - the data a later probe-ordering task needs
    and that worst_case_measurements alone discards."""
    benchmark_dir = os.path.dirname(spec.canonical.netlist_path)
    corners = all_corners(spec.pvt_corners)
    # Indexed by corner, filled in across the testbench loop below: one
    # corner's full measurement set is spread across testbench iterations
    # (the loop is testbenches-outside, corners-inside), so this has to be
    # merged incrementally rather than built per testbench.
    per_corner_merged: list[dict] = [{} for _ in corners]

    combined_measurements: dict[str, float] = {}
    combined_worst_corners: dict[str, dict] = {}
    for tb in spec.testbenches:
        netlist_text = netlist_texts[tb.name]
        per_corner_measurements = []
        for index, corner in enumerate(corners):
            rendered = render_corner_netlist(
                netlist_text, corner.process, corner.voltage, corner.temperature, benchmark_dir
            )
            with tempfile.TemporaryDirectory() as tmpdir:
                netlist_path = os.path.join(tmpdir, "corner.cir")
                with open(netlist_path, "w") as f:
                    f.write(rendered)
                result = sim_backend.run(netlist_path, {"control_block": tb.control_block})
            per_corner_measurements.append(result.measurements)
            per_corner_merged[index].update(result.measurements)

        tb_measurements, tb_worst_corners = worst_case_measurements(corners, per_corner_measurements, tb.criteria)
        combined_measurements.update(tb_measurements)
        combined_worst_corners.update(tb_worst_corners)

    # Evaluated one criterion at a time, each against ITS OWN worst-case value,
    # rather than by handing evaluate_criteria one dict keyed by measurement
    # name. A two-sided window (vbgout >= 1.20 and vbgout <= 1.28) is two
    # criteria over one measurement with opposite operators, so a
    # name-keyed dict can only hold one of the two worst cases and the other
    # side is silently evaluated against the wrong corner - hiding, for
    # instance, a low-side violation behind a passing high-side value.
    results: list[dict] = []
    overall_pass = True
    for criterion in spec.all_criteria:
        worst = combined_worst_corners.get(criterion.name)
        value = worst.get("value") if worst else None
        # A None value means some corner produced no measurement at all; an
        # empty dict makes evaluate_criteria fail it as missing, which is the
        # same handling the nominal path gives it.
        measurements = {} if value is None else {criterion.measurement: value}
        evaluation = evaluate_criteria(measurements, [criterion])
        results.extend(evaluation["criteria"])
        overall_pass = overall_pass and evaluation["overall_pass"]

    summary = "all criteria passed" if overall_pass else "one or more criteria failed"
    return {
        "overall_pass": overall_pass,
        "criteria": results,
        "summary": summary,
        "worst_case_corners": combined_worst_corners,
        "per_corner": [
            {
                "corner": {"process": c.process, "voltage": c.voltage, "temperature": c.temperature},
                "measurements": m,
                "severity": corner_severity(m, spec.all_criteria),
            }
            for c, m in zip(corners, per_corner_merged)
        ],
    }
