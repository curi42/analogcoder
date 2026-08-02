from dataclasses import dataclass
from typing import Protocol

from analogcoder.area_limits import annotate_resolved_params, multiplicity, resolved_token
from analogcoder.netlist import Component, parse_netlist
from analogcoder.params import build_param_envs, has_token


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


def _dimension(component: Component, token: str) -> float | None:
    value = resolved_token(component, token)
    if value is None:
        return None
    return value * component.geometry_scale


def total_area(netlist_text: str) -> AreaTotal:
    """소자별 `w x l x m`의 합. nf는 제외한다 - 핑거 분할은 총 폭을
    바꾸지 않으므로 면적 중립이다.

    파라미터 해소는 area_limits.annotate_resolved_params를 쓴다 -
    index_baseline_components(면적 게이트)도 같은 함수를 쓴다. 예전에는
    이 배선을 여기서 따로 복제했는데, 그 복제가 오늘은 우연히 똑같아도
    한쪽에만 새 규칙이 붙는 순간 두 총합이 말없이 갈라질 수 있었다 - 그래서
    한 함수로 합쳤다."""
    parsed = parse_netlist(netlist_text)
    envs = build_param_envs(netlist_text)
    components = list(parsed.top_components) + [
        c for subckt in parsed.subckts.values() for c in subckt.components
    ]
    for component in components:
        annotate_resolved_params(component, envs)

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


class AreaModel(Protocol):
    """덱 하나의 총 면적을 내는 것. **회사 이식 시 교체되는 경계다.**

    지금 저장소에는 PDK가 없어 기본 구현이 `w x l x m` 파생 근사이고, 그
    근사는 서브회로 **정의**를 N번 인스턴스화해도 1번만 센다. PDK 유도
    모델은 거의 확실히 다르게 세므로, **모델이 바뀌면 면적 단계의 결과가
    바뀐다** - 이 경계를 넘는 것은 함수 하나가 아니라 그 사실이다."""

    def __call__(self, netlist_text: str) -> AreaTotal: ...


DEFAULT_AREA_MODEL: AreaModel = total_area
