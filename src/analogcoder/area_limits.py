from dataclasses import dataclass

from analogcoder.netlist import Component, TracedTarget, parse_netlist
from analogcoder.params import annotate_traced_params, build_param_envs, has_token, resolve_value


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
# 소자 개수(m)를 키우는 변경에 허용하는 배수. 길이가 아니라 개수이므로
# 크기별 티어가 아니라 평평한 한 값이다 - PNP_TIERS와 같은 이유, 같은 값.
# 사용자의 흐름에서는 NMOS/PMOS 폭을 고정하고 인스턴스마다 m을 바꾸므로,
# 이 상수가 실무에서 가장 자주 걸리는 제약이다 (기하 티어가 아니라).
COUNT_ALLOWED_MULTIPLIER = 2.0
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

    # 인스턴스 줄에서 크기가 정해지는 덱(래퍼 셀 스타일)을 위해, 각
    # 인스턴스 파라미터가 실제로 도달하는 소자/토큰을 여기서 한 번 계산해
    # 둔다. check_area_growth는 넷리스트 원문이 아니라 이 표만 받는다.
    annotate_traced_params(netlist_text, parsed, envs)

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

# 소자 자신의 토큰 이름별 취급. 이름들은 SPICE 표준 소자 문법이므로 사실이지,
# 명명 규칙이 아니다 (인스턴스 파라미터 이름과 대비된다 - netlist.TracedTarget
# 참고).
_GEOMETRY_TOKENS = frozenset({"w", "l"})
# m: 병렬 소자의 **개수**. `w=2u m=2`는 2um 소자 두 개라 총 폭 4um - 면적이
# m에 비례해 늘어난다.
_COUNT_TOKENS = frozenset({"m"})
# nf: 손가락 **개수**. `w=2u nf=2`는 총 폭 2um짜리 소자 하나를 1um 손가락 둘로
# 쪼갠 것이다. 총 폭도 면적도 nf로 변하지 않으며, 손가락끼리 소스/드레인
# 확산을 공유하므로 오히려 약간 유리하다. 그래서 티어가 없고 곱에도 들어가지
# 않는다 - 이것은 "판단할 수 없다"가 아니라 "판단할 것이 없다"이다. 여기
# 제약이 없는 것을 보고 빠뜨린 것으로 오해해 티어를 붙이지 말 것.
_NEUTRAL_TOKENS = frozenset({"nf"})


def _resolved_token(component: Component, token: str) -> float | None:
    """소자가 쓴 토큰의 해소값을 대소문자 무시로 찾는다. SPICE는 대소문자를
    구분하지 않아 같은 덱에 `W=30`과 `w=1`이 함께 나온다."""
    for name, value in component.resolved_params.items():
        if name.lower() == token:
            return value
    return None


def _multiplicity(component: Component) -> float | None:
    """m이 없으면 1, m 토큰이 있는데 값을 못 풀면 None. m은 개수이므로
    `.option scale`을 곱하지 않는다.

    "m 토큰이 없다"와 "m 토큰은 있는데 모른다"를 구별하지 않으면 모르는 값을
    1로 가정하게 되고, 그 추측은 **항상 티어를 느슨한 쪽으로** 틀린다 (소자가
    실제보다 작아 보인다). params._multiplier와 같은 규칙, 같은 이유다 -
    직접 주소지정 경로와 추적 경로 중 한쪽만 고치면 같은 구멍이 반쪽 남는다."""
    m = _resolved_token(component, "m")
    if m is None:
        return None if has_token(component, "m") else 1.0
    return 1.0 if m <= 0 else m


def _tier_baseline_value(component: Component) -> float | None:
    """The dimension used to pick a size tier.

    An X-prefixed sky130 primitive is tiered on geometry scaled by the deck's
    `.option scale`: W for a transistor, w for a MiM cap, l for a poly
    resistor (its length sets both its resistance and its area). Its
    positional `value` is the subckt NAME, so there is nothing else to tier
    on - reading it raised ValueError, which check_area_growth swallowed,
    leaving the device silently unconstrained.

    A generic (non-X) transistor is still tiered on W; every other generic
    component on its own value, which is already an absolute quantity.

    소자는 폭이 아니라 **총 폭**(w x m)으로 티어를 고른다. m은 병렬 소자의
    개수라 면적이 그만큼 곱해지므로, w만 보면 m=4인 소자가 실제 크기의 1/4로
    티어링되어 가장 느슨한 티어를 받는다. 이것은 MOS만의 사실이 아니다 -
    m=4인 MiM 캡도 면적이 똑같이 네 배이므로 같은 규칙을 받는다. 예전에는
    ctype "M"에만 곱해져서 그 비대칭이 캡/저항을 한 티어 느슨하게 만들었다.
    Q만 예외인데, 그쪽은 m 자체가 티어 키(에미터 면적비)라 곱하면 이중이다."""
    ctype = _classify_ctype(component)
    if component.ctype == "X":
        param = _SKY130_GEOMETRY_PARAM.get(ctype)
        if param is None:
            return None
        raw = _resolved_token(component, param.lower())
        if raw is None:
            return None
        if ctype == "Q":
            # m is an emitter-area count, not a length - it must not be
            # scaled, and it is already the tier key itself.
            return raw
        mult = _multiplicity(component)
        return None if mult is None else raw * component.geometry_scale * mult
    if ctype == "M":
        w = _resolved_token(component, "w")
        if w is None:
            return None
        mult = _multiplicity(component)
        return None if mult is None else w * component.geometry_scale * mult
    if component.resolved_value is None:
        return None
    mult = _multiplicity(component)
    return None if mult is None else component.resolved_value * mult


@dataclass(frozen=True)
class _Target:
    """한 변경이 도달하는 **물리 소자 하나**와, 그 소자에 대한 판정 재료."""

    key: tuple  # 소자 정체성. 같은 제안 안에서 이 키가 같은 변경들이 곱해진다.
    label: str
    allowed: float | None  # None = 이 소자에 대해 이 토큰은 티어가 없다
    counts: bool  # 곱에 들어가는가
    neutral: bool = False  # 면적 중립이라 일부러 뺐는가 (nf)


@dataclass
class _Group:
    label: str
    ratio: float = 1.0
    allowed: float | None = None
    excluded_neutral: bool = False


def _direct_target(refdes: str, component: Component, param: str) -> _Target:
    """소자를 직접 주소지정한 변경. param이 곧 그 소자의 토큰이다."""
    token = param.lower()
    if token in _NEUTRAL_TOKENS:
        return _Target(key=(refdes,), label=refdes, allowed=None, counts=False, neutral=True)
    tier_baseline = _tier_baseline_value(component)
    allowed = (
        None
        if tier_baseline is None
        else allowed_multiplier_for(
            _classify_ctype(component), tier_baseline, is_sky130=component.ctype == "X"
        )
    )
    if token in _COUNT_TOKENS:
        allowed = COUNT_ALLOWED_MULTIPLIER if allowed is None else min(allowed, COUNT_ALLOWED_MULTIPLIER)
    return _Target(key=(refdes,), label=refdes, allowed=allowed, counts=True)


def _traced_targets(refdes: str, traced: list[TracedTarget]) -> list[_Target]:
    """인스턴스 줄의 파라미터가 도달한 소자들에 대한 판정 재료.

    티어는 파라미터 이름이 아니라 **도달한 토큰**으로 정해진다. 이름은
    설계자의 규칙이라 추측이 되지만 토큰은 SPICE 문법이라 사실이다.

    키에 chain(거쳐 온 중간 인스턴스 refdes들)이 들어가는 것이 핵심이다.
    한 정의를 형제로 두 번 인스턴스화하면 두 도달점의 device는 **같은 정의
    컴포넌트 객체**라, chain이 없으면 물리적으로 다른 두 소자가 한 그룹으로
    묶여 한 변경의 성장 비율이 제곱된다 (2.5x가 6.25x로 보고됐다). 티어는
    총 면적 예산이 아니라 **소자 하나의 성장 비율** 한도이므로, 같은 비율이
    두 소자에 각각 일어나도 소자당 비율은 그대로다."""
    targets: list[_Target] = []
    for traced_target in traced:
        device = traced_target.device
        token = traced_target.token.lower()
        key = (refdes, traced_target.chain, device.scope, device.refdes)
        where = f"{device.scope}.{device.refdes}" if device.scope else device.refdes
        label = " -> ".join([refdes, *traced_target.chain, where])
        if token in _NEUTRAL_TOKENS:
            targets.append(_Target(key, label, allowed=None, counts=False, neutral=True))
            continue
        if token in _COUNT_TOKENS:
            targets.append(_Target(key, label, allowed=COUNT_ALLOWED_MULTIPLIER, counts=True))
            continue
        if token == "value":
            # R/C의 크기 노브는 위치 인자 값이다 - 벌거벗은 소자를 직접
            # 주소지정했을 때와 같은 티어 표를 써야, 감쌌다는 이유만으로
            # 같은 성장이 정반대 판정을 받지 않는다.
            if traced_target.positional_value is None:
                targets.append(_Target(key, label, allowed=None, counts=False))
                continue
            targets.append(
                _Target(
                    key,
                    label,
                    allowed=allowed_multiplier_for(
                        _classify_ctype(device), traced_target.positional_value
                    ),
                    counts=True,
                )
            )
            continue
        if token in _GEOMETRY_TOKENS:
            if traced_target.total_width is None:
                # 이 소자의 총 폭을 확정하지 못했다 - 면적 영향을 판단할 수
                # 없으므로 막지 않는다 (해소 불가 베이스라인과 같은 폴백).
                # 그래도 **버리지는 않는다**: 조용히 빼면 이 도달점이 아예
                # 없었던 것처럼 보여, 반쪽만 판정한 변경이 로그에 "bounded"로
                # 남는다 (_visibility 참고). 티어도 곱도 없으니 판정은 그대로다.
                targets.append(_Target(key, label, allowed=None, counts=False))
                continue
            targets.append(
                _Target(
                    key,
                    label,
                    allowed=allowed_multiplier_for(
                        _classify_ctype(device),
                        traced_target.total_width,
                        is_sky130=device.ctype == "X",
                    ),
                    counts=True,
                )
            )
            continue
        # 크기가 아닌 토큰(geomod 등). 티어도 없고 곱에도 들어가지 않는다.
        targets.append(_Target(key, label, allowed=None, counts=False))
    return targets


def _integrality_violation(refdes: str, param: str, tokens: set[str], new_value: str) -> str | None:
    """m/nf는 개수다. 스키마는 숫자 문자열만 요구하고 이 게이트는 비율만 보므로
    m=6.5 같은 제안이 그대로 통과할 수 있었다 - 사용자의 흐름에서 m이 주된
    사이징 노브인 이상 도달 가능한 경로다."""
    counts = sorted(tokens & (_COUNT_TOKENS | _NEUTRAL_TOKENS))
    if not counts:
        return None
    value = resolve_value(new_value, {})
    if value is None or abs(value - round(value)) <= 1e-9:
        return None
    return (
        f"{refdes}.{param} sets {'/'.join(counts)}, a count of parallel devices or fingers - "
        f"it must be a whole number, and {new_value!r} is not"
    )


def _visibility(component: Component | None, targets: list[_Target], ratio_known: bool) -> str:
    """이 변경에 대해 게이트가 **무엇을 볼 수 있었는가**. 판정(승인/거부)이
    아니라 시야를 기록한다.

    세 가지 "막지 않음"은 서로 다른 사실인데 예전에는 로그에서 구별되지
    않았다 - 그래서 게이트가 통째로 무력해진 것을 두 번이나 실행 로그에서
    알아채지 못했다 (`.option scale` 미반영, MiM 캡 단위 불일치):

    - bounded : 티어가 잡혔다. 진짜로 판정한 경우.
    - neutral : 볼 것이 없다. nf(손가락 개수)는 구조적으로 면적 중립이다.
    - blind   : 볼 수 없다. 이 덱이 정의하지 않는 서브회로를 인스턴스화한
                소자라 (래퍼 셀 라이브러리는 보통 `.include`로 온다) 추적이
                원리적으로 불가능하다.
    - unjudged: 볼 수는 있었는데 숫자를 확정하지 못했다 (해소 불가 베이스라인,
                크기가 아닌 토큰, 베이스라인에 없는 refdes).

    도달점이 여럿이면 **가장 약한 상태**를 보고한다. 하나라도 티어를 못 받은
    도달점이 있으면 그 변경은 bounded가 아니다: 한 파라미터가 ma1(총 폭 확정)과
    mb1(w='wn*kfac'라 확정 불가)에 동시에 도달하면 mb1은 실제로 무제약인데,
    "티어가 하나라도 붙었으면 bounded"라고 하면 로그가 절반의 무제약을 감춘다 -
    로그로 무력화를 감사할 수 있게 하는 것이 이 상태의 유일한 존재 이유이므로
    그건 자기 부정이다. 그래서 _traced_targets도 확정 못 한 도달점을 버리지
    않고 티어 없는 _Target으로 남긴다.

    blind가 가장 먼저인 이유: 그 소자는 애초에 무엇에 도달하는지 알 수 없으므로
    파라미터 **이름**으로 nf/기하를 읽어내는 것 자체가 금지된 추측이다."""
    if component is None:
        return "unjudged"
    if component.undefined_subckt and _classify_ctype(component) == "X":
        return "blind"
    if not targets:
        return "unjudged"
    if all(t.neutral for t in targets):
        return "neutral"
    if ratio_known and all(
        t.counts and t.allowed is not None for t in targets if not t.neutral
    ):
        return "bounded"
    return "unjudged"


@dataclass(frozen=True)
class AreaCheckResult:
    approved: bool
    feedback: str | None
    # "<refdes>.<param>" -> _visibility()의 상태. 같은 키가 두 번 제안되면
    # 나중 것이 이긴다 (로그용 요약이지 판정 근거가 아니다).
    states: dict[str, str]


def check_area_growth(
    baseline_components: dict[str, Component], proposed_changes: list[dict]
) -> tuple[bool, str | None]:
    """evaluate_area_growth의 (승인, 피드백)만 필요한 호출자를 위한 얇은 껍질."""
    result = evaluate_area_growth(baseline_components, proposed_changes)
    return result.approved, result.feedback


def evaluate_area_growth(
    baseline_components: dict[str, Component], proposed_changes: list[dict]
) -> AreaCheckResult:
    """제안된 변경들이 소자를 얼마나 키우는지를 소자별로 판정한다.

    변경 하나씩이 아니라 **도달하는 물리 소자별로 묶어 곱**을 본다. 총 폭이
    w x m이므로, 한 제안 안에서 w를 3x(단독 허용) 키우고 m을 2x(단독 허용)
    키우면 총 폭은 6x가 되는데 각각을 따로 보면 아무도 보지 못한다.
    허용 배수는 그 묶음에 관여한 파라미터들의 티어 중 **가장 빡빡한 것**이다.

    승인/피드백과 함께 변경별 **시야 상태**를 돌려준다 - _visibility 참고."""
    violations: list[str] = []
    groups: dict[tuple, _Group] = {}
    states: dict[str, str] = {}

    for change in proposed_changes:
        refdes = change["refdes"]
        param = change["param"]
        state_key = f"{refdes}.{param}"
        component = baseline_components.get(refdes)
        if component is None:
            states[state_key] = _visibility(None, [], False)
            continue

        traced = component.traced_params.get(param)
        if traced:
            targets = _traced_targets(refdes, traced)
            tokens = {t.token.lower() for t in traced}
        else:
            # 추적이 안 되는 파라미터는 소자를 직접 주소지정한 것으로 본다.
            # 서브회로 인스턴스인데 추적에 실패한 경우도 여기로 오는데, 그때는
            # _tier_baseline_value가 None이라 자연히 "판단 불가, 막지 않음"이
            # 된다 - "무엇을 키우는지 알아내지 못했다"와 "아무것도 키우지
            # 않는다"는 다른 사실이고, 후자만이 nf처럼 의도적으로 무제약이다.
            targets = [_direct_target(refdes, component, param)]
            tokens = {param.lower()}

        baseline_value = _baseline_value_for(component, param)
        # 빈 환경으로 충분한 이유는 TUNER_SCHEMA가 new_value를 접미사
        # 붙은 숫자 리터럴로만 제한하기 때문이다 (식별자·연산자 불가).
        # 그 패턴이 느슨해져 파라미터 참조를 허용하게 되면 여기서 None이
        # 나와 조용히 증가율 계산에서 빠지므로, 패턴을 건드릴 때 이 줄도
        # 함께 재검토해야 한다.
        new_value = resolve_value(change["new_value"], {})
        ratio = None
        if baseline_value is not None and baseline_value > 0 and new_value is not None:
            ratio = new_value / baseline_value

        states[state_key] = _visibility(component, targets, ratio is not None)

        # 정수 조건은 면적과 무관한 별도 위반이지만, 시야 상태는 그 앞에서
        # 이미 확정해 둔다 - 상태는 판정이 아니라 "무엇을 볼 수 있었는가"다.
        integrality = _integrality_violation(refdes, param, tokens, change["new_value"])
        if integrality is not None:
            violations.append(integrality)
            continue

        for target in targets:
            group = groups.setdefault(target.key, _Group(label=target.label))
            if target.allowed is not None:
                group.allowed = (
                    target.allowed if group.allowed is None else min(group.allowed, target.allowed)
                )
            if not target.counts:
                group.excluded_neutral |= target.neutral
                continue
            if ratio is not None:
                group.ratio *= ratio

    for group in groups.values():
        if group.ratio <= 1.0 or group.allowed is None or group.ratio <= group.allowed:
            continue
        note = (
            " (nf is a finger count: area-neutral by construction, so it was not counted)"
            if group.excluded_neutral
            else ""
        )
        violations.append(
            f"{group.label}: proposed change grows area by {group.ratio:.2f}x, "
            f"exceeding the {group.allowed:.1f}x limit for its size tier{note}"
        )

    if violations:
        return AreaCheckResult(False, "; ".join(violations), states)
    return AreaCheckResult(True, None, states)
