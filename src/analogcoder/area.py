from dataclasses import dataclass

from analogcoder.area_limits import multiplicity, resolved_token
from analogcoder.netlist import Component, parse_netlist
from analogcoder.params import build_param_envs, has_token, resolve_value


@dataclass(frozen=True)
class AreaTotal:
    """해소 가능한 소자의 면적 합과 그 커버리지.

    최적화는 값만 바꾸고 소자를 더하거나 빼지 않으므로, 해소되는 소자 집합이
    단계 전후로 같다. 그래서 skipped가 0이 아니어도 두 총합의 *비율*은
    의미가 있다. 그래도 개수를 드러내는 이유는, 커버리지가 낮은 채로 비율만
    믿는 상황을 호출부가 알아차릴 수 있어야 하기 때문이다."""

    area: float
    counted: int
    skipped: int


def _annotate(component: Component, envs: dict[str | None, dict[str, float]]) -> None:
    """component.params(원본 문자열)를 component.resolved_params(수치)로 채운다.

    parse_netlist는 파싱만 하고 해소는 하지 않는다 - resolved_params는
    이 단계 전까지 항상 비어 있다. area_limits.index_baseline_components의
    내부 _annotate와 같은 구조다 (build_param_envs/resolve_value 호출도
    동일): 그쪽은 이 함수를 공개하지 않으므로, 같은 배선을 새로 얻는 대신
    여기서 다시 쓴다. 두 자리 다 build_param_envs/resolve_value라는
    같은 해소기를 호출하므로 결과가 갈릴 여지는 없다 - 갈릴 수 있는 별도
    해소 로직을 새로 만드는 것과는 다르다."""
    env = envs.get(component.scope, envs[None])
    for name, raw in component.params.items():
        value = resolve_value(raw, env)
        if value is not None:
            component.resolved_params[name] = value
    if component.value is not None:
        component.resolved_value = resolve_value(component.value, env)


def _dimension(component: Component, token: str) -> float | None:
    value = resolved_token(component, token)
    if value is None:
        return None
    return value * component.geometry_scale


def total_area(netlist_text: str) -> AreaTotal:
    """소자별 `w x l x m`의 합. nf는 제외한다 - 핑거 분할은 총 폭을
    바꾸지 않으므로 면적 중립이다."""
    parsed = parse_netlist(netlist_text)
    envs = build_param_envs(netlist_text)
    components = list(parsed.top_components) + [
        c for subckt in parsed.subckts.values() for c in subckt.components
    ]
    for component in components:
        _annotate(component, envs)

    area = 0.0
    counted = 0
    skipped = 0
    for component in components:
        if not (has_token(component, "w") and has_token(component, "l")):
            continue
        width = _dimension(component, "w")
        length = _dimension(component, "l")
        mult = multiplicity(component)
        if width is None or length is None or mult is None:
            skipped += 1
            continue
        area += width * length * mult
        counted += 1

    return AreaTotal(area=area, counted=counted, skipped=skipped)
