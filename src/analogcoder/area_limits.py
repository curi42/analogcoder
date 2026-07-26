from dataclasses import dataclass

from analogcoder.netlist import Component, parse_netlist, parse_spice_value


_SKY130_CTYPE_MARKERS: list[tuple[str, str]] = [
    ("fet", "M"),
    ("cap", "C"),
    ("res", "R"),
]


@dataclass(frozen=True)
class SizeTier:
    max_value: float | None  # None = this is the top/unbounded tier
    allowed_multiplier: float


TRANSISTOR_TIERS: list[SizeTier] = [
    SizeTier(max_value=30e-6, allowed_multiplier=3.0),
    SizeTier(max_value=80e-6, allowed_multiplier=2.0),
    SizeTier(max_value=None, allowed_multiplier=1.5),
]
CAPACITOR_TIERS: list[SizeTier] = [
    SizeTier(max_value=3e-12, allowed_multiplier=3.0),
    SizeTier(max_value=10e-12, allowed_multiplier=2.0),
    SizeTier(max_value=None, allowed_multiplier=1.5),
]
RESISTOR_TIERS: list[SizeTier] = [
    SizeTier(max_value=1e3, allowed_multiplier=3.0),
    SizeTier(max_value=10e3, allowed_multiplier=2.0),
    SizeTier(max_value=None, allowed_multiplier=1.5),
]
TIERS_BY_CTYPE: dict[str, list[SizeTier]] = {
    "M": TRANSISTOR_TIERS,
    "C": CAPACITOR_TIERS,
    "R": RESISTOR_TIERS,
}


def _classify_ctype(component: Component) -> str:
    """Effective device-type for tiering. Generic-device refdes prefixes
    (M/C/R) pass through unchanged. An X-prefixed sky130 PDK primitive
    instantiation carries its subckt/model name as component.value (the
    last positional token on the line) - classify by that name instead,
    since sky130 transistors and MiM caps are both X-prefixed and would
    otherwise be indistinguishable from an unconstrained subckt
    instantiation like "Xdut ... OPAMP2STAGE"."""
    if component.ctype != "X" or component.value is None:
        return component.ctype
    lowered = component.value.lower()
    for marker, ctype in _SKY130_CTYPE_MARKERS:
        if marker in lowered:
            return ctype
    return component.ctype


def allowed_multiplier_for(ctype: str, baseline_value: float) -> float | None:
    tiers = TIERS_BY_CTYPE.get(ctype)
    if tiers is None:
        return None
    for tier in tiers:
        if tier.max_value is None or baseline_value < tier.max_value:
            return tier.allowed_multiplier
    return tiers[-1].allowed_multiplier


def index_baseline_components(netlist_text: str) -> dict[str, Component]:
    parsed = parse_netlist(netlist_text)
    components = list(parsed.top_components)
    for subckt in parsed.subckts.values():
        components.extend(subckt.components)
    return {c.refdes: c for c in components}


def _baseline_value_for(component: Component, param: str) -> str | None:
    if param == "value":
        return component.value
    return component.params.get(param)


def _tier_baseline_value(component: Component) -> float | None:
    """The dimension used to pick a size tier: baseline W for transistors
    (L rarely varies in this project); for a sky130 MiM cap (X-prefixed,
    classified as "C"), baseline w= (its "value" is a subckt name, not a
    numeric literal, so it can't be used directly); the component's own
    value for every other C/R."""
    ctype = _classify_ctype(component)
    if ctype == "M":
        w = component.params.get("W")
        return parse_spice_value(w) if w is not None else None
    if component.ctype == "X" and ctype == "C":
        w = component.params.get("w")
        return parse_spice_value(w) if w is not None else None
    if component.value is not None:
        return parse_spice_value(component.value)
    return None


def check_area_growth(
    baseline_components: dict[str, Component], proposed_changes: list[dict]
) -> tuple[bool, str | None]:
    by_refdes: dict[str, list[dict]] = {}
    for change in proposed_changes:
        by_refdes.setdefault(change["refdes"], []).append(change)

    violations: list[str] = []
    for refdes, changes in by_refdes.items():
        component = baseline_components.get(refdes)
        if component is None:
            continue

        combined_ratio = 1.0
        for change in changes:
            baseline_str = _baseline_value_for(component, change["param"])
            if baseline_str is None:
                continue
            try:
                baseline_value = parse_spice_value(baseline_str)
            except ValueError:
                # e.g. param="value" misapplied to a non-numeric positional
                # token (a transistor's model name, a subckt instance's
                # subckt name). Not something we can judge area impact on -
                # treat this change as unconstrained, same as a missing
                # baseline value.
                continue
            if baseline_value <= 0:
                continue
            try:
                new_value = parse_spice_value(change["new_value"])
            except ValueError:
                continue
            combined_ratio *= new_value / baseline_value

        if combined_ratio <= 1.0:
            continue

        try:
            tier_baseline = _tier_baseline_value(component)
        except ValueError:
            continue
        if tier_baseline is None:
            continue
        allowed = allowed_multiplier_for(_classify_ctype(component), tier_baseline)
        if allowed is not None and combined_ratio > allowed:
            violations.append(
                f"{refdes}: proposed change grows area by {combined_ratio:.2f}x, "
                f"exceeding the {allowed:.1f}x limit for its size tier"
            )

    if violations:
        return False, "; ".join(violations)
    return True, None
