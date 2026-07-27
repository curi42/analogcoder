from dataclasses import dataclass, field

from analogcoder.netlist import (
    Component,
    is_top_level_stimulus,
    parse_netlist,
    parse_spice_value,
)

# 모델 이름에서 디바이스 클래스를 읽는다. area_limits.py의 _SKY130_CTYPE_MARKERS와
# 같은 방식이되, 여기서는 티어가 아니라 단자 의미를 위해 쓴다. 표에 없는
# 이름은 None - 추측하지 않는다.
_MODEL_CLASS_MARKERS: list[tuple[str, str]] = [
    ("nfet", "nfet"),
    ("pfet", "pfet"),
    ("pnp", "pnp"),
    ("npn", "npn"),
    ("res", "res"),
    ("cap", "cap"),
]

# 단자 이름과 역할. 게이트는 DC 전류를 흘리지 않으므로 순수 감지 단자이고,
# bulk는 전도하지만 신호를 나르지 않으므로 따로 둔다 - drive로 묶으면 모든
# 블록이 vss를 구동하게 되어 초점 씨앗이 전 블록으로 번진다.
_MOS_TERMINALS = [("d", "drive"), ("g", "sense"), ("s", "drive"), ("b", "bulk")]
_BJT_TERMINALS = [("c", "drive"), ("b", "drive"), ("e", "drive")]
_TWO_TERMINALS = [("1", "drive"), ("2", "drive")]

_TERMINALS_BY_CLASS: dict[str, list[tuple[str, str]]] = {
    "nfet": _MOS_TERMINALS,
    "pfet": _MOS_TERMINALS,
    "pnp": _BJT_TERMINALS,
    "npn": _BJT_TERMINALS,
    "res": _TWO_TERMINALS,
    "cap": _TWO_TERMINALS,
}
# refdes 접두는 SPICE의 보장이므로 모델 이름을 몰라도 단자 의미를 안다 -
# generic level-1 M/Q/R/C/L/D는 모델 표에 없어도 침묵하지 않는다.
_TERMINALS_BY_CTYPE: dict[str, list[tuple[str, str]]] = {
    "M": _MOS_TERMINALS,
    "Q": _BJT_TERMINALS,
    "R": _TWO_TERMINALS,
    "C": _TWO_TERMINALS,
    "L": _TWO_TERMINALS,
    "D": _TWO_TERMINALS,
}


@dataclass(frozen=True)
class Terminal:
    refdes: str
    name: str
    role: str


@dataclass
class ComponentFact:
    refdes: str
    ctype: str
    device_class: str | None
    model: str | None
    nodes: list[str]
    params: dict[str, str]
    terminals: list[Terminal] = field(default_factory=list)


@dataclass
class BlockStructure:
    path: str | None
    ports: list[str]
    components: list[ComponentFact]
    instance_count: int


@dataclass(frozen=True)
class TunableEntry:
    refdes: str
    param: str


@dataclass
class NetlistStructure:
    circuit_name: str
    blocks: dict[str | None, BlockStructure]
    tunable: list[TunableEntry]


def _qualify(scope: str | None, name: str) -> str:
    return f"{scope}.{name}" if scope else name


def _classify_model(component: Component) -> str | None:
    """모델 이름 서브스트링으로 소자 클래스를 읽는다 - 단, refdes 접두(ctype)가
    이미 단자 의미를 정한 소자에는 적용하지 않는다. refdes 접두는 SPICE의
    보장이고 모델 이름은 관례일 뿐이므로 접두가 이긴다
    (area_limits._classify_ctype과 같은 규율: X 접두만 모델 이름을 본다 -
    X 인스턴스의 위치 값은 PDK 프리미티브 이름 그 자체라 ctype만으로는
    단자 의미를 알 수 없는 유일한 경우이기 때문이다). 실전 덱에서
    "TN33_DEP_CAP"이라는 모델명의 MOSFET(refdes m3)이 이 규율 없이는
    device_class="cap"이 되어 밀러 매처의 캡 목록과 MOS 목록에 동시에
    올라 자기 자신과 짝지어졌다 - M/Q/R/C/L/D는 ctype 자체가 이미
    단자 의미를 정하므로 모델 이름의 res/cap/nfet/pfet 마커를 보지 않는다."""
    if component.ctype != "X":
        return None
    if component.value is None:
        return None
    lowered = component.value.lower()
    for marker, klass in _MODEL_CLASS_MARKERS:
        if marker in lowered:
            return klass
    return None


def _is_numeric_value(raw: str | None) -> bool:
    """위치 값이 숫자인가. 모델명/서브회로명이면 False이고, 그런 값은
    tunable 인덱스에 넣지 않는다 - param="value"로 덮어쓰면 덱이 깨진다."""
    if raw is None:
        return False
    try:
        parse_spice_value(raw)
    except ValueError:
        return False
    return True


def _terminals_for(refdes: str, component: Component, device_class: str | None) -> list[Terminal]:
    layout = _TERMINALS_BY_CTYPE.get(component.ctype)
    if layout is None and device_class is not None:
        layout = _TERMINALS_BY_CLASS.get(device_class)
    if layout is None:
        return []
    if len(component.nodes) < len(layout):
        # 노드가 모자란 줄은 유효한 SPICE가 아니다. 여기서 추측하지 않는다.
        return []
    return [Terminal(refdes=refdes, name=name, role=role) for name, role in layout]


def _fact(scope: str | None, component: Component) -> ComponentFact:
    refdes = _qualify(scope, component.refdes)
    device_class = _classify_model(component)
    model = component.value if not _is_numeric_value(component.value) else None
    return ComponentFact(
        refdes=refdes,
        ctype=component.ctype,
        device_class=device_class,
        model=model,
        nodes=list(component.nodes),
        params=dict(component.params),
        terminals=_terminals_for(refdes, component, device_class),
    )


def derive_structure(netlist_text: str, circuit_name: str) -> NetlistStructure:
    """넷리스트 하나를 스코프별 평면 사실 묶음으로 바꾼다. LLM 애널라이저를
    대체하는 결정론적 파생 - 같은 입력에는 항상 같은 출력을 낸다
    (test_derivation_is_deterministic). 모델 이름 표에 없는 디바이스나
    서브회로 인스턴스는 단자 역할을 내지 않는다: 모르면 침묵한다."""
    parsed = parse_netlist(netlist_text)

    scoped: list[tuple[str | None, list[Component]]] = [(None, parsed.top_components)]
    scoped += [(path, subckt.components) for path, subckt in sorted(parsed.subckts.items())]

    # 정의 이름별 인스턴스 수. 인스턴스는 정의를 이름(경로의 마지막 조각)으로
    # 지목하므로 이름으로 센다 - X 인스턴스의 value가 subckt 이름이다.
    definition_names = {path.rpartition(".")[2]: path for path in parsed.subckts}
    instance_counts: dict[str, int] = {path: 0 for path in parsed.subckts}
    for _scope, components in scoped:
        for component in components:
            if component.ctype != "X":
                continue
            target = definition_names.get(component.value or "")
            if target is not None:
                instance_counts[target] += 1

    blocks: dict[str | None, BlockStructure] = {}
    tunable: list[TunableEntry] = []

    for scope, components in scoped:
        facts = [_fact(scope, component) for component in components]
        for fact, component in zip(facts, components):
            if is_top_level_stimulus(scope, component.ctype):
                # 최상위 자극/전원은 주소록에 올리지 않는다. 이유는
                # netlist.is_top_level_stimulus 참고.
                continue
            for name in sorted(fact.params):
                tunable.append(TunableEntry(refdes=fact.refdes, param=name))
            if _is_numeric_value(component.value):
                tunable.append(TunableEntry(refdes=fact.refdes, param="value"))

        ports = parsed.subckts[scope].ports if scope is not None else []
        blocks[scope] = BlockStructure(
            path=scope,
            ports=list(ports),
            components=facts,
            instance_count=instance_counts[scope] if scope is not None else 1,
        )

    return NetlistStructure(circuit_name=circuit_name, blocks=blocks, tunable=tunable)
