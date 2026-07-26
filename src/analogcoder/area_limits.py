from dataclasses import dataclass

from analogcoder.netlist import Component, parse_netlist
from analogcoder.params import build_param_envs, resolve_value


_SKY130_CTYPE_MARKERS: list[tuple[str, str]] = [
    ("fet", "M"),
    ("cap", "C"),
    ("res", "R"),
    ("pnp", "Q"),
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
# Tiers for an X-prefixed sky130 primitive, keyed on the GEOMETRY dimension in
# metres (W for a transistor, w for a MiM cap, l for a poly resistor) rather
# than on a value in ohms/farads. A subckt-instantiated primitive has no
# numeric value to tier on - its positional value is the subckt name - so the
# device-value tiers above simply do not apply to it.
#
# The 25um first boundary is load-bearing: benchmarks/bandgap's
# spec_seed_buf0_droop.yaml is only solvable if BUF_P.Xcl can grow from W=20
# to W=50 (2.5x), which needs the 3.0x tier.
SKY130_GEOMETRY_TIERS: list[SizeTier] = [
    SizeTier(max_value=25e-6, allowed_multiplier=3.0),
    SizeTier(max_value=50e-6, allowed_multiplier=2.0),
    SizeTier(max_value=None, allowed_multiplier=1.5),
]
# A bipolar's tuning knob is its emitter-area multiplier m, a count rather
# than a length, so it gets one flat tier instead of a size-graded table.
PNP_TIERS: list[SizeTier] = [SizeTier(max_value=None, allowed_multiplier=2.0)]
TIERS_BY_CTYPE: dict[str, list[SizeTier]] = {
    "M": TRANSISTOR_TIERS,
    "C": CAPACITOR_TIERS,
    "R": RESISTOR_TIERS,
    "Q": PNP_TIERS,
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


def allowed_multiplier_for(ctype: str, baseline_value: float, is_sky130: bool = False) -> float | None:
    if ctype == "Q":
        # A bipolar is X-prefixed but tiered on a count, not on geometry.
        tiers = PNP_TIERS
    else:
        tiers = SKY130_GEOMETRY_TIERS if is_sky130 else TIERS_BY_CTYPE.get(ctype)
    if tiers is None:
        return None
    for tier in tiers:
        if tier.max_value is None or baseline_value < tier.max_value:
            return tier.allowed_multiplier
    return tiers[-1].allowed_multiplier


def index_baseline_components(netlist_text: str) -> dict[str, Component]:
    """Keyed by "<path>.<refdes>" for components declared inside a subckt
    (path is the dotted nesting path, e.g. "OUTER.INNER"), plus a plain
    refdes alias - for both top-level and subckt-declared
    components alike - for any refdes occurring exactly once netlist-wide.
    A refdes occurring more than once (whether top-level vs. subckt, or
    across two subckts) gets no plain key from either side: this mirrors
    apply_changes' ambiguity rule exactly, so the area gate and the editor
    always agree on what an unqualified refdes means. An unqualified
    proposal against an existing single-subckt benchmark (no collisions)
    still finds its baseline instead of silently bypassing the area gate
    (check_area_growth treats a missing baseline as unconstrained)."""
    parsed = parse_netlist(netlist_text)
    envs = build_param_envs(netlist_text)

    def _annotate(component: Component) -> None:
        env = envs.get(component.scope, envs[None])
        for name, raw in component.params.items():
            value = resolve_value(raw, env)
            if value is not None:
                component.resolved_params[name] = value
        if component.value is not None:
            component.resolved_value = resolve_value(component.value, env)

    for component in parsed.top_components:
        _annotate(component)
    for subckt in parsed.subckts.values():
        for component in subckt.components:
            _annotate(component)

    plain_counts: dict[str, int] = {}
    for component in parsed.top_components:
        plain_counts[component.refdes] = plain_counts.get(component.refdes, 0) + 1
    for subckt in parsed.subckts.values():
        for component in subckt.components:
            plain_counts[component.refdes] = plain_counts.get(component.refdes, 0) + 1

    indexed: dict[str, Component] = {}
    for component in parsed.top_components:
        if plain_counts[component.refdes] == 1:
            indexed[component.refdes] = component
    for path, subckt in parsed.subckts.items():
        for component in subckt.components:
            indexed[f"{path}.{component.refdes}"] = component
            if plain_counts[component.refdes] == 1:
                indexed[component.refdes] = component
    return indexed


def _baseline_value_for(component: Component, param: str) -> float | None:
    """해소된 수치. 확정할 수 없으면 None.

    원본 토큰이 아니라 해소값을 돌려주는 것이 핵심이다. W='wn*2'를 문자열로
    읽으면 parse_spice_value가 ValueError를 내고, check_area_growth가 그것을
    '판단 불가, 막지 않음'으로 처리해 파라미터화된 덱 전체에서 게이트가
    사라진다."""
    if param == "value":
        return component.resolved_value
    return component.resolved_params.get(param)


# The geometry dimension each X-prefixed sky130 primitive is tiered on.
_SKY130_GEOMETRY_PARAM: dict[str, str] = {"M": "W", "C": "w", "R": "l", "Q": "m"}


def _tier_baseline_value(component: Component) -> float | None:
    """The dimension used to pick a size tier.

    An X-prefixed sky130 primitive is tiered on geometry scaled by the deck's
    `.option scale`: W for a transistor, w for a MiM cap, l for a poly
    resistor (its length sets both its resistance and its area). Its
    positional `value` is the subckt NAME, so there is nothing else to tier
    on - reading it raised ValueError, which check_area_growth swallowed,
    leaving the device silently unconstrained.

    A generic (non-X) transistor is still tiered on W; every other generic
    component on its own value, which is already an absolute quantity."""
    ctype = _classify_ctype(component)
    if component.ctype == "X":
        param = _SKY130_GEOMETRY_PARAM.get(ctype)
        if param is None:
            return None
        raw = component.resolved_params.get(param)
        if raw is None:
            return None
        # m is an emitter-area count, not a length - it must not be scaled.
        scale = 1.0 if ctype == "Q" else component.geometry_scale
        return raw * scale
    if ctype == "M":
        w = component.resolved_params.get("W")
        return w * component.geometry_scale if w is not None else None
    return component.resolved_value


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
            baseline_value = _baseline_value_for(component, change["param"])
            if baseline_value is None or baseline_value <= 0:
                continue
            # 빈 환경으로 충분한 이유는 TUNER_SCHEMA가 new_value를 접미사
            # 붙은 숫자 리터럴로만 제한하기 때문이다 (식별자·연산자 불가).
            # 그 패턴이 느슨해져 파라미터 참조를 허용하게 되면 여기서 None이
            # 나와 조용히 증가율 계산에서 빠지므로, 패턴을 건드릴 때 이 줄도
            # 함께 재검토해야 한다.
            new_value = resolve_value(change["new_value"], {})
            if new_value is None:
                continue
            combined_ratio *= new_value / baseline_value

        if combined_ratio <= 1.0:
            continue

        tier_baseline = _tier_baseline_value(component)
        if tier_baseline is None:
            continue
        allowed = allowed_multiplier_for(
            _classify_ctype(component), tier_baseline, is_sky130=component.ctype == "X"
        )
        if allowed is not None and combined_ratio > allowed:
            violations.append(
                f"{refdes}: proposed change grows area by {combined_ratio:.2f}x, "
                f"exceeding the {allowed:.1f}x limit for its size tier"
            )

    if violations:
        return False, "; ".join(violations)
    return True, None
