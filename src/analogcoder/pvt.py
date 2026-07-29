import math
import os
import re
import tempfile
from dataclasses import dataclass

from analogcoder.judge_tools import evaluate_criteria
from analogcoder.simulators.cache import attach_log_event
from analogcoder.simulators.parallel import map_points
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


def _corner_fields(corner: CornerPoint | None) -> dict:
    """The reported coordinates of one point in a corner list.

    `None` is the deck as it is - rendered through no corner at all - which is
    how corner_selection.NOMINAL travels. It has no process/voltage/temperature
    to read, so it is reported as "(deck)" (the same name corner_selection.label
    gives it) with no numbers. Substituting a stand-in CornerPoint here instead
    would put fabricated coordinates into worst_case_corners, where every
    consumer reads them as a real corner - and tt/27 IS a real corner, distinct
    from the unrendered deck. run_full_pvt_sweep never passes None, so this
    changes nothing for it."""
    if corner is None:
        return {"process": "(deck)", "voltage": None, "temperature": None}
    return {
        "process": corner.process,
        "voltage": corner.voltage,
        "temperature": corner.temperature,
    }


def worst_case_measurements(
    corners: list[CornerPoint | None], per_corner_measurements: list[dict], criteria: list[Criterion]
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
    excluded from the worst-case pool while other corners paper over it.

    **Two criteria can share one measurement name** - a two-sided window
    (`vbgout_v >= 1.20` and `vbgout_v <= 1.28`) is exactly that, with opposite
    operators and therefore opposite worst cases. The returned dict is one
    float per measurement name (the judge's contract, and evaluate_criteria's,
    guard_band_violations', optimizer._search's), so the two worst cases cannot
    both be carried and the slot has to be **resolved**, not overwritten. The
    rule below is: if any criterion sharing the name is violated by its own
    worst case, the slot carries that value; otherwise it carries the
    last-declared criterion's value, which is what this function always did and
    which no verdict depends on when nothing is violated.

    Two properties make this safe, and both are load-bearing:

    - **Nothing is fabricated.** Every candidate is a real measurement taken at
      a real corner of the passed-in list. The slot never holds a synthesised
      or interpolated number.
    - **It can only surface a violation, never invent one.** Every candidate
      lies in [min, max] over the corner list, and a threshold comparison is
      monotone: if a "<=" criterion's own worst case (the max) passes, every
      other candidate - all <= that max - passes it too, and symmetrically for
      ">=" against the min. So substituting another criterion's worst case can
      never flip a genuinely-passing criterion to failing. It can only reveal
      the violation the shared slot was hiding.

    That is what keeps the reduced-corner-set claim intact in the direction it
    is claimed: a mid-loop FAIL is genuine (some real corner in the selected
    set really violates that criterion), while a mid-loop PASS is still merely
    optimistic (a corner outside the selected set may be worse). Before this,
    a violation on the losing half of a window could not be seen **at all** -
    growing the set re-derived the same PASS, so the loop could not converge on
    that half and burned the whole retry_budget."""
    measurements: dict[str, float] = {}
    worst_corners: dict[str, dict] = {}
    # measurement name -> [(criterion, that criterion's own worst value), ...]
    # in declaration order, so the fallback below is the same last-writer the
    # per-criterion assignment used to produce.
    candidates: dict[str, list[tuple[Criterion, float]]] = {}
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
            worst_corners[criterion.name] = {**_corner_fields(corner), "value": None}
            continue  # withhold the measurement so evaluate_criteria fails it as missing

        if criterion.operator in (">=", ">"):
            value, corner = min(values_with_corner, key=lambda vc: vc[0])
        else:
            value, corner = max(values_with_corner, key=lambda vc: vc[0])
        candidates.setdefault(criterion.measurement, []).append((criterion, value))
        worst_corners[criterion.name] = {**_corner_fields(corner), "value": value}

    for name, entries in candidates.items():
        violating = [
            value
            for criterion, value in entries
            if not evaluate_criteria({name: value}, [criterion])["overall_pass"]
        ]
        # The single-criterion case (every measurement in every other spec here)
        # goes through both branches identically: one entry, so the slot holds
        # that entry's value whether or not it violates.
        measurements[name] = violating[0] if violating else entries[-1][1]
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


def _simulate_rendered(sim_backend, rendered: str, control_block: str):
    """한 점을 자기 임시 디렉터리에서 돌린다.

    임시 디렉터리가 호출마다 새로 파이는 것이 **워커 격리 그 자체**다 -
    ngspice는 중간 파일을 쓰고, NgspiceBackend는 CWD를 덱이 놓인 디렉터리로
    잡는다. 병렬화를 위해 새로 만든 격리가 아니라 원래 있던 격리이고, 그래서
    include 해석 규칙도 그대로다(최상위 include는 cli.py에서 이미 절대 경로로
    바뀌어 들어온다)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        netlist_path = os.path.join(tmpdir, "corner.cir")
        with open(netlist_path, "w") as f:
            f.write(rendered)
        return sim_backend.run(netlist_path, {"control_block": control_block})


def run_full_pvt_sweep(
    netlist_texts: dict[str, str], spec, sim_backend, max_workers: int | None = None, log_event=None
) -> dict:
    """Runs spec.pvt_corners' full cross product against every testbench,
    directly via sim_backend (no LLM agent involved - corner variation is
    purely mechanical). Returns the worst-case-per-criterion result in the
    same shape evaluate_criteria() returns, plus a worst_case_corners
    breakdown mapping each criterion's name to the corner that produced its
    worst-case value, for diagnostics, and a per_corner breakdown (parallel
    to all_corners(spec.pvt_corners)) exposing each corner's own merged
    measurements and severity - the data a later probe-ordering task needs
    and that worst_case_measurements alone discards.

    **모든 (테스트벤치, 코너) 점은 서로 독립이므로 한 풀에서 함께 돈다.**
    이 함수가 순차 이중 루프였을 때 45코너 스윕이 286초였고, 최적화의 코너
    확정 런은 그것을 여섯 번 돌아 1790초였다. 병렬화가 바꾸는 것은 실행
    순서뿐이다: 결과는 `(테스트벤치 이름, 코너 인덱스)`로 색인해 모으고 아래에서
    **선언 순서대로** 다시 읽으므로, 완료 순서는 어떤 값에도 닿지 않는다.
    `max_workers=1`이면 풀을 만들지 않고 순차로 돈다(A/B의 대조군)."""
    benchmark_dir = os.path.dirname(spec.canonical.netlist_path)
    corners = all_corners(spec.pvt_corners)
    attach_log_event(sim_backend, log_event)
    # Indexed by corner, filled in across the testbench loop below: one
    # corner's full measurement set is spread across testbench iterations
    # (the loop is testbenches-outside, corners-inside), so this has to be
    # merged incrementally rather than built per testbench.
    per_corner_merged: list[dict] = [{} for _ in corners]

    # 렌더링은 순차로, 시뮬레이션만 병렬로. 렌더링은 정규식 몇 개라 비용이 없고,
    # 여기서 만들어 두면 워커가 spec/netlist_texts를 건드리지 않는다.
    points = []
    for tb in spec.testbenches:
        netlist_text = netlist_texts[tb.name]
        for index, corner in enumerate(corners):
            rendered = render_corner_netlist(
                netlist_text, corner.process, corner.voltage, corner.temperature, benchmark_dir
            )
            points.append(((tb.name, index), (rendered, tb.control_block)))

    raw_results = map_points(
        lambda payload: _simulate_rendered(sim_backend, payload[0], payload[1]),
        points,
        max_workers,
    )

    combined_measurements: dict[str, float] = {}
    combined_worst_corners: dict[str, dict] = {}
    for tb in spec.testbenches:
        per_corner_measurements = []
        for index, _corner in enumerate(corners):
            result = raw_results[(tb.name, index)]
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
