"""넷리스트 계층 연결: 포트↔넷 매핑과 넷별 구동/감지 블록을 계산한다.

structure.py가 낸 평면 사실(스코프별 컴포넌트, 포트, 단자 역할)에서 각
인스턴스가 부모 스코프의 어떤 넷에 실제로 연결되는지를 위치 기반으로
풀어내고, 그 좌표를 최상위까지 밀어올려 "이 최상위 넷을 어떤 정의가
구동(drive)하고 어떤 정의가 감지(sense)하는가"를 결정론적으로 낸다.
LLM 애널라이저를 대체하는 파생 단계 - 모르면 침묵한다: 포트 수가 안 맞으면
매핑을 만들지 않고 사실로만 보고한다."""

from dataclasses import dataclass, field

from analogcoder.structure import ComponentFact, NetlistStructure

_SIGNAL_ROLES = ("drive", "sense")


@dataclass
class InstanceEdge:
    instance_refdes: str
    definition: str
    port_nets: dict[str, str] = field(default_factory=dict)
    mismatch: str | None = None


@dataclass
class SignalPaths:
    instances: list[InstanceEdge]
    net_blocks: dict[str, dict[str, str]]


def _definition_of(structure: NetlistStructure, model: str | None) -> str | None:
    """인스턴스가 지목하는 정의 경로. 인스턴스는 정의를 이름으로 부르므로
    경로의 마지막 조각으로 찾는다 - structure.py의 정의 식별 방식과 동일하고
    그 한계(같은 이름의 서브회로가 서로 다른 부모 아래 중첩되면 구분 못함)도
    그대로 물려받는다. 이 저장소의 어떤 벤치마크도 그렇게 중첩하지 않는다."""
    if model is None:
        return None
    for path in structure.blocks:
        if path is not None and path.rpartition(".")[2] == model:
            return path
    return None


def _build_instances(structure: NetlistStructure) -> list[InstanceEdge]:
    """모든 스코프의 서브회로 인스턴스를 훑어 정의의 포트를 인스턴스 라인의
    넷과 위치로 짝짓는다. 포트 수와 노드 수가 다르면 매핑을 만들지 않고
    그 자체를 사실로 남긴다 - 넷리스트 버그를 감추지 않기 위해서다."""
    instances: list[InstanceEdge] = []
    for block in structure.blocks.values():
        for fact in block.components:
            if fact.ctype != "X":
                # X가 아닌 소자는 애초에 서브회로 인스턴스일 수 없다 - value가
                # 우연히 어떤 subckt 이름과 같아도(예: 저항 값 토큰이 "PMOD"인
                # 경우) 그건 이름 충돌일 뿐 인스턴스가 아니다. structure.py의
                # instance_counts도 같은 기준(ctype == "X")으로 센다 - 두
                # 모듈이 "무엇이 인스턴스인가"에 대해 일치해야 한다.
                continue
            definition = _definition_of(structure, fact.model)
            if definition is None:
                continue
            ports = structure.blocks[definition].ports
            if len(ports) != len(fact.nodes):
                instances.append(
                    InstanceEdge(
                        instance_refdes=fact.refdes,
                        definition=definition,
                        mismatch=(
                            f"{fact.refdes} gives {len(fact.nodes)} nodes but "
                            f"{definition} declares {len(ports)} ports"
                        ),
                    )
                )
                continue
            instances.append(
                InstanceEdge(
                    instance_refdes=fact.refdes,
                    definition=definition,
                    port_nets=dict(zip(ports, fact.nodes)),
                )
            )
    return instances


def _bulk_net(fact: ComponentFact) -> str | None:
    return next(
        (net for terminal, net in zip(fact.terminals, fact.nodes) if terminal.role == "bulk"),
        None,
    )


def _signal_roles(fact: ComponentFact, ports: set[str] | None, roles: dict[str, str]) -> None:
    """한 컴포넌트의 단자들이 내는 drive/sense 역할을 roles에 병합한다
    (drive가 이긴다). ports가 None이면 모든 넷이 대상 - 최상위는 포트라는
    개념이 없으므로. 소스/드레인이 같은 컴포넌트의 벌크와 동일한 넷이면
    제외한다 - NMOS 소스가 흔히 그렇듯 벌크와 함께 vss에 묶여 있을 뿐인데
    그 컴포넌트가 vss(흔히 최상위의 0)를 '구동'하는 것처럼 보이면 초점이
    무의미해지기 때문이다."""
    bulk_net = _bulk_net(fact)
    for terminal, net in zip(fact.terminals, fact.nodes):
        if terminal.role not in _SIGNAL_ROLES:
            continue
        if ports is not None and net not in ports:
            continue
        if bulk_net is not None and net == bulk_net:
            continue
        if roles.get(net) != "drive":
            roles[net] = terminal.role


def _own_port_roles(structure: NetlistStructure) -> dict[str, dict[str, str]]:
    """정의별로 "이 컴포넌트가 자신의 포트를 직접 건드리는 역할"만 모은다.
    최상위에서 내려오며 위치를 옮기는 것은 walk()의 몫이고, 여기서는 각
    정의 자신의 원소자만 본다 - 중첩된 서브회로 인스턴스를 통해 전달되는
    역할까지 굳이 별도로 전파할 필요는 없다: walk()가 매 깊이마다 그 깊이의
    인스턴스가 부르는 정의의 own role을 직접 조회하며 내려가므로 이 한
    단계짜리 계산으로 다단계 계층이 자연히 처리된다."""
    port_roles: dict[str, dict[str, str]] = {}
    for path, block in structure.blocks.items():
        if path is None:
            continue
        roles: dict[str, str] = {}
        ports = set(block.ports)
        for fact in block.components:
            _signal_roles(fact, ports, roles)
        port_roles[path] = roles
    return port_roles


def build_signal_paths(structure: NetlistStructure) -> SignalPaths:
    instances = _build_instances(structure)
    port_roles = _own_port_roles(structure)

    net_blocks: dict[str, dict[str, str]] = {}

    def record(net: str, definition_name: str, role: str) -> None:
        bucket = net_blocks.setdefault(net, {})
        if bucket.get(definition_name) != "drive":
            bucket[definition_name] = role

    # 인스턴스를 "그 X 라인이 물리적으로 위치한 스코프"별로 묶는다. 최상위부터
    # 내려가며 좌표계를 바깥(부모) 넷 이름으로 바꿔야 하므로, 어떤 스코프에
    # 있는 인스턴스들을 다음으로 처리할지는 이 그룹으로 찾는다.
    edges_by_scope: dict[str | None, list[InstanceEdge]] = {}
    for edge in instances:
        scope = edge.instance_refdes.rpartition(".")[0] or None
        edges_by_scope.setdefault(scope, []).append(edge)

    def walk(scope: str | None, translate: dict[str, str] | None) -> None:
        for edge in edges_by_scope.get(scope, []):
            definition_name = edge.definition.rpartition(".")[2]
            inner: dict[str, str] = {}
            for port, local_net in edge.port_nets.items():
                outer = translate.get(local_net) if translate is not None else local_net
                if outer is None:
                    # 이 로컬 넷은 부모 스코프의 포트가 아니다 (그 스코프
                    # 내부에서만 쓰이는 넷) - 더 밖으로 밀어올릴 좌표가 없다.
                    continue
                inner[port] = outer
                role = port_roles.get(edge.definition, {}).get(port)
                if role is not None:
                    record(outer, definition_name, role)
            # 한 단계 안으로 들어가며 좌표계를 부모(바깥) 넷으로 바꾼다.
            walk(edge.definition, inner)

    walk(None, None)

    # 최상위에 직접 놓인 소자(정의가 없는 스코프)도 넷을 구동/감지한다.
    # 서브회로 인스턴스는 walk()의 translate=None 경로가 이미 다루므로,
    # 여기서는 그 인스턴스가 아닌 소자(저항, 커패시터, 트랜지스터 등)만
    # 추가로 기록한다.
    for fact in structure.blocks[None].components:
        roles: dict[str, str] = {}
        _signal_roles(fact, None, roles)
        for net, role in roles.items():
            record(net, fact.refdes, role)

    return SignalPaths(instances=instances, net_blocks=net_blocks)
