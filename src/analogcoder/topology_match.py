"""(block, topology) 호환성을 결정론적으로 판정한다.

오케스트레이터가 토폴로지 스왑을 제안할 때, "정의가 딱 하나뿐인 덱"이라는
낡은 제약 대신 실제로 안전하게 갈아끼울 수 있는 (block_path, topology_id)
쌍만 후보로 내놓기 위한 순수 함수 모듈이다. 이 모듈은 게이트가 아니라
**후보 생성기**다 - 오케스트레이터(Task 5)는 여기서 나온 후보만 LLM에
제시한다.

세 판정 규칙은 전부 파싱된 사실에만 근거한다 - 이름에서 의미를 추측하지
않는다:

1. 포트: `set(topology.ports) <= set(block.ports)` (토폴로지가 요구하는
   포트는 블록에 전부 있어야 하지만, 블록이 더 많은 포트를 가져도 된다).
   한 방향만 요구하는 이유는 F1 때의 완전 동등 규칙이 9포트 폴디드-캐스코드
   블록에 5포트 밀러 후보를 아예 못 대게 만들었기 때문이다 - bandgap의
   네 앰프가 전부 9포트인데 5포트 항목 둘뿐이라 그 규칙 아래서는 후보가
   0개였다. 완화는 **남는 포트가 안전한지**를 확인해야만 성립한다:
   블록에 남는 포트(nbias/ncas/pbias/pcas 같은)가 있으면, `apply_topology_swap`은
   `.subckt` 헤더가 아니라 본문만 교체하므로 헤더는 그 포트를 여전히
   선언하고 호출부 인스턴스 줄도 그대로 실제 넷을 넘긴다 - 새 본문만 그
   포트를 내부에서 안 쓴다. 그 넷이 같은 스코프의 **다른** 소자에도
   쓰이고 있으면(bandgap처럼 바이어스 체인을 여러 앰프 인스턴스가 공유하는
   경우) 이 인스턴스의 연결을 끊어도 그 넷은 여전히 의미가 있다 - 안전하다.
   반대로 그 넷을 이 인스턴스 하나만 참조하면, 스왑 후 그 넷은 회로 어디에도
   안 붙어 뜬 넷이 된다 - 그래서 이 경우 `ports` 사유로 거부한다(새 사유
   코드를 만들지 않는다 - 포트 규칙의 일부다). 이 부동 넷 검사는 **지역적**
   질문이다: `signal_path.net_blocks`는 최상위 넷만 담아 bandgap의 바이어스
   넷(`BANDGAP`/`BGR_CORE` 정의 내부에 있다)을 보지 못하므로 쓰지 않고,
   대신 인스턴스 줄과 같은 스코프의 소자 목록만 직접 본다. 그 블록의
   인스턴스가 이 테스트벤치 어디에도 없으면(정의만 있고 아무도 부르지 않는
   블록) 남는 포트가 안전한지 판정할 근거가 없으므로 추측하지 않고
   거부한다.
2. 모델: 토폴로지 본문이 쓰는 모델 이름 집합이 덱 전체(모든 스코프)가
   인스턴스화하는 모델 이름 집합의 부분집합이어야 한다. 역방향은 요구하지
   않는다. `.include`를 따라가지 않아도 판정 가능한 이유는, 덱이 이미 그
   모델을 인스턴스화한다면 그것을 제공하는 include가 어딘가에 존재한다는
   사실 자체이기 때문이다.
3. 스케일: `topology.assumes_scale == netlist_scale(deck_text)`.

그리고 한 가지 다중-테스트벤치 규칙: 후보는 그 블록을 정의하는 **모든**
테스트벤치에서 호환일 때만 후보다. `RunState.push_netlist_version`이
테스트벤치 전체를 원자적으로 버저닝하므로, 일부 테스트벤치만 스왑된 상태를
만들 수 없다 - 그래서 한 테스트벤치라도 그 블록을 정의하지 않으면
`missing_in_testbench`로 기각한다.

**No-op 검출 (identical_body).** 위 세 규칙을 모두 통과해도, 토폴로지
본문이 그 블록의 현재 본문과 구조적으로 완전히 같으면 스왑은 회로를 하나도
바꾸지 않는다. 실측: `folded_cascode_nmos_in_cs`의 본문은 `benchmarks/
bandgap`의 `TRIMAMP` 본문과 컴포넌트 시퀀스가 완전히 같다. 이런 쌍을
후보로 내놓으면 에이전트가 "TRIMAMP를 TRIMAMP로 교체"를 골라 iteration
하나를 태우고 `consecutive_rollbacks`를 리셋해, 정작 스왑이 필요했던
이유(반복된 파라미터 튜닝 실패)에 대한 에스컬레이션만 늦춘다. 그래서 모든
테스트벤치에서 (refdes, ctype, value, nodes, params) 시퀀스가 완전히
같으면 `identical_body`로 기각한다 - 한 테스트벤치에서만 같고 다른
테스트벤치에서는 다르면 스왑이 적어도 하나의 덱은 실제로 바꾸므로 no-op이
아니다.
"""

from dataclasses import dataclass

from analogcoder.netlist import Component, ParsedNetlist, Subckt, netlist_scale, parse_netlist, parse_spice_value
from analogcoder.topologies import Topology


@dataclass(frozen=True)
class SwapCandidate:
    block_path: str
    topology_id: str


@dataclass(frozen=True)
class SwapRejection:
    block_path: str
    topology_id: str
    # "ports" | "models" | "scale" | "missing_in_testbench" | "identical_body"
    # | "already_tried"
    reason: str
    detail: str


# `topology_unavailable`이 실을 사유 코드. 후보가 0개라는 **하나의 관측**이
# 서로 다른 사실 넷을 덮고 있었다: 덱에 `.subckt` 정의가 아예 없다 / 라이브러리가
# 비었다 / 모든 쌍을 이미 시도했다 / 호환성 규칙이 전부 기각했다. 넷 다
# `{"outer_iter": N}` 한 줄로 나가면 "검사했고 후보가 없음"과 "검사가 사라짐"이
# 로그에서 구별되지 않는다 - 이 저장소가 다섯 번 반복한 침묵한 게이트의 모양이다.
NO_SUBCKT_DEFINITIONS = "no_subckt_definitions"
EMPTY_LIBRARY = "empty_library"
ALL_PAIRS_ALREADY_TRIED = "all_pairs_already_tried"
ALL_PAIRS_REJECTED = "all_pairs_rejected"


def unavailable_reason(
    netlist_texts: dict[str, str],
    library: dict[str, Topology],
    rejections: list[SwapRejection],
) -> str:
    """`compatible_swaps`가 후보를 하나도 내지 못했을 때 그 사유 코드.

    파싱된 사실만 읽는다 - 추측이 없다. 순서가 중요하다: 정의가 없으면
    라이브러리가 무엇이든 쌍이 열거되지 않고, 라이브러리가 비어 있으면 정의가
    무엇이든 마찬가지다. 두 경우 모두 `rejections`가 비어 있어 사유를 기각
    목록에서 되읽을 수 없다."""
    if not any(parse_netlist(text).subckts for text in netlist_texts.values()):
        return NO_SUBCKT_DEFINITIONS
    if not library:
        return EMPTY_LIBRARY
    if rejections and all(r.reason == "already_tried" for r in rejections):
        return ALL_PAIRS_ALREADY_TRIED
    return ALL_PAIRS_REJECTED


def _is_model_name(value: str | None) -> bool:
    """위치 값이 모델/서브회로 이름인가 - 숫자로 파싱되지 않을 때 그렇다.

    structure.py의 `_is_numeric_value`와 같은 규칙이지만 그것은 private라
    모듈 경계를 넘어 재사용하지 않는다 (요구사항의 명시적 지시) - 이 모듈에
    같은 것을 다시 둔다.

    알려진 한계: 아직 풀리지 않은 파라미터 표현식(`{rv*2}`, `'rv*2'`)도
    `parse_spice_value`에 실패하므로 모델 이름으로 오분류된다. 그런 문자열은
    어떤 덱의 모델 집합에도 나타날 수 없으므로 실제로는 항상 허위
    `models` 기각으로 이어진다. 여기서 표현식을 풀려고 하지 않는 이유는
    토폴로지 본문에는 풀 대상이 되어 줄 인스턴스 컨텍스트가 아예 없기
    때문이다 - 추측하느니 침묵하는 편이 이 계층의 규칙이다. 대신
    `TOPOLOGY_LIBRARY`의 각 본문이 이런 값을 갖지 않도록
    `test_topologies.py`의 큐레이션 불변식 테스트가 지켜준다."""
    if value is None:
        return False
    try:
        parse_spice_value(value)
    except ValueError:
        return True
    return False


def _model_names(components: list[Component]) -> set[str]:
    return {c.value for c in components if _is_model_name(c.value)}


def _all_model_names(parsed: ParsedNetlist) -> set[str]:
    """덱이 인스턴스화하는 모든 모델 이름 - 최상위 + 모든 서브회로 스코프."""
    names = _model_names(parsed.top_components)
    for subckt in parsed.subckts.values():
        names |= _model_names(subckt.components)
    return names


def _wrap_topology_body(topology: Topology) -> ParsedNetlist:
    """토폴로지 본문을 `.subckt TMP <ports>` ... `.ends TMP`로 감싸 파싱한다.
    본문 자체는 독립된 SPICE 조각(포트 헤더가 없는 컴포넌트 나열)이라 그대로는
    파싱 대상이 아니다."""
    wrapped = f".subckt TMP {' '.join(topology.ports)}\n{topology.subckt_body}\n.ends TMP\n"
    return parse_netlist(wrapped)


def _topology_model_names(topology: Topology) -> set[str]:
    """토폴로지 본문이 쓰는 모델 이름 집합."""
    return _all_model_names(_wrap_topology_body(topology))


def _instances_of(parsed: ParsedNetlist, subckt_name: str) -> list[tuple[str | None, Component]]:
    """이 서브회로를 인스턴스화하는 모든 (scope, component) - 최상위와 모든
    서브회로 스코프를 훑는다. 인스턴스는 `ctype == "X"`이고 위치 값(서브회로
    이름)이 `subckt_name`과 같은 소자다. scope는 최상위일 때 None."""
    found: list[tuple[str | None, Component]] = []
    for component in parsed.top_components:
        if component.ctype == "X" and component.value == subckt_name:
            found.append((None, component))
    for path, sub in parsed.subckts.items():
        for component in sub.components:
            if component.ctype == "X" and component.value == subckt_name:
                found.append((path, component))
    return found


def _leftover_ports_float_reason(
    parsed: ParsedNetlist,
    block_path: str,
    subckt: Subckt,
    leftover_ports: list[str],
) -> str | None:
    """남는 포트들이 스왑 후 뜬 넷이 되지 않는지 판정한다. 문제 없으면 None,
    있으면 `SwapRejection.detail`에 실을 문자열을 돌려준다.

    `apply_topology_swap`은 `.subckt` 헤더가 아니라 본문만 바꾸므로, 헤더가
    선언하는 남는 포트와 호출부의 실제 넷은 스왑 후에도 그대로 남는다 - 새
    본문만 그 포트를 내부에서 안 쓴다. 그 넷이 인스턴스와 **같은 스코프의
    다른 소자**에도 쓰이면 이 인스턴스의 연결을 끊어도 안전하고, 이 인스턴스
    하나만 그 넷을 참조하면 스왑 후 그 넷은 회로 어디에도 안 붙어 뜬 넷이
    된다. `signal_path.net_blocks`를 쓰지 않는 이유는 최상위 넷만 담기
    때문이다 - bandgap의 바이어스 넷은 `BGR_CORE`/`BANDGAP` 정의 내부에
    있다. 대신 인스턴스 줄과 같은 스코프의 소자 목록만 직접 본다(지역적
    질문)."""
    instances = _instances_of(parsed, subckt.name)
    if not instances:
        return (
            f"block {block_path!r} has no instance anywhere in this testbench, so whether "
            f"leftover port(s) {leftover_ports} would float cannot be judged"
        )

    port_index = {name: idx for idx, name in enumerate(subckt.ports)}
    for scope, instance in instances:
        siblings = parsed.top_components if scope is None else parsed.subckts[scope].components
        for port_name in leftover_ports:
            idx = port_index[port_name]
            if idx >= len(instance.nodes):
                return (
                    f"instance {instance.refdes!r} of {block_path!r} in scope "
                    f"{scope if scope is not None else '<top-level>'!r} has fewer nodes than "
                    f"{block_path!r} declares ports; cannot judge leftover port {port_name!r}"
                )
            net = instance.nodes[idx]
            if not any(other is not instance and net in other.nodes for other in siblings):
                return (
                    f"leftover port {port_name!r} of {block_path!r} is tied to net {net!r} on "
                    f"instance {instance.refdes!r} in scope "
                    f"{scope if scope is not None else '<top-level>'!r}, and no other component in "
                    f"that scope references {net!r} - it would float if this port were dropped"
                )
    return None


def _component_key(component: Component) -> tuple:
    """구조적 동등 비교용 키. 스코프/raw_line/geometry_scale 등 파생/장식
    필드는 뺀다 - 덱의 블록 본문은 주석과 공백을 갖지만 토폴로지 본문은
    갖지 않으므로, 파싱으로 이미 정규화된 (refdes, ctype, value, nodes,
    params)만 비교한다."""
    return (
        component.refdes,
        component.ctype,
        component.value,
        tuple(component.nodes),
        tuple(sorted(component.params.items())),
    )


def _body_sequence(components: list[Component]) -> list[tuple]:
    return [_component_key(c) for c in components]


def _is_identical_body(topology: Topology, subckt_components: list[Component]) -> bool:
    """토폴로지 본문이 그 블록의 현재 본문과 컴포넌트 시퀀스가 완전히
    같은가 - 순서를 포함해 비교한다(다중집합이 아니라 시퀀스). 소자 순서를
    바꿔도 회로는 같을 수 있지만, 그 등가성을 판단하려면 SPICE 수준의 회로
    동등성 검사가 필요하고 이는 이 모듈의 범위 밖이다. 여기서는 보수적으로
    "재배열조차 없이 완전히 같다"만 no-op으로 판정한다 - 놓치는 no-op은
    있을 수 있어도(false negative), 진짜로 다른 회로를 no-op이라 잘못
    기각하는 일(false positive)은 없다."""
    topology_components = _wrap_topology_body(topology).subckts["TMP"].components
    return _body_sequence(topology_components) == _body_sequence(subckt_components)


def compatible_swaps(
    netlist_texts: dict[str, str],
    library: dict[str, Topology],
    tried: set[tuple[str, str]],
) -> tuple[list[SwapCandidate], list[SwapRejection]]:
    parsed_by_tb = {tb: parse_netlist(text) for tb, text in netlist_texts.items()}
    scale_by_tb = {tb: netlist_scale(text) for tb, text in netlist_texts.items()}
    models_by_tb = {tb: _all_model_names(parsed) for tb, parsed in parsed_by_tb.items()}
    topology_models = {topology_id: _topology_model_names(topology) for topology_id, topology in library.items()}

    all_block_paths = sorted({path for parsed in parsed_by_tb.values() for path in parsed.subckts})

    candidates: list[SwapCandidate] = []
    rejections: list[SwapRejection] = []

    for block_path in all_block_paths:
        for topology_id in sorted(library):
            if (block_path, topology_id) in tried:
                # 기각으로 **기록한다**. 그냥 continue하면 "이미 써 본 쌍이라
                # 뺐다"가 로그에서 부재로만 나타나, 라이브러리 소진과
                # "판정이 사라짐"이 똑같이 `candidates: [], rejections: []`로
                # 보인다.
                rejections.append(
                    SwapRejection(
                        block_path=block_path,
                        topology_id=topology_id,
                        reason="already_tried",
                        detail=(
                            f"({block_path!r}, {topology_id!r}) was already attempted "
                            f"earlier in this run"
                        ),
                    )
                )
                continue

            topology = library[topology_id]
            required_models = topology_models[topology_id]
            is_compatible = True

            for tb in sorted(netlist_texts):
                parsed = parsed_by_tb[tb]
                subckt = parsed.subckts.get(block_path)

                if subckt is None:
                    rejections.append(
                        SwapRejection(
                            block_path=block_path,
                            topology_id=topology_id,
                            reason="missing_in_testbench",
                            detail=f"testbench {tb!r} does not define a block at {block_path!r}",
                        )
                    )
                    is_compatible = False
                    continue

                topo_ports = set(topology.ports)
                block_ports = set(subckt.ports)
                if not topo_ports <= block_ports:
                    rejections.append(
                        SwapRejection(
                            block_path=block_path,
                            topology_id=topology_id,
                            reason="ports",
                            detail=(
                                f"topology needs port(s) {sorted(topo_ports - block_ports)} that block "
                                f"{block_path!r} does not declare in testbench {tb!r}"
                            ),
                        )
                    )
                    is_compatible = False
                    continue

                # 남는 포트(블록에는 있지만 토폴로지 본문은 안 쓰는 포트)마다
                # 부동 넷 검사. subckt.ports 순서를 보존한다 - _leftover_ports_
                # float_reason이 위치로 인스턴스 노드를 찾는다.
                leftover_ports = [p for p in subckt.ports if p not in topo_ports]
                if leftover_ports:
                    float_reason = _leftover_ports_float_reason(parsed, block_path, subckt, leftover_ports)
                    if float_reason is not None:
                        rejections.append(
                            SwapRejection(
                                block_path=block_path,
                                topology_id=topology_id,
                                reason="ports",
                                detail=f"{float_reason} (testbench {tb!r})",
                            )
                        )
                        is_compatible = False
                        continue

                deck_models = models_by_tb[tb]
                if not required_models <= deck_models:
                    missing = sorted(required_models - deck_models)
                    rejections.append(
                        SwapRejection(
                            block_path=block_path,
                            topology_id=topology_id,
                            reason="models",
                            detail=f"model(s) {missing} never instantiated anywhere in testbench {tb!r}",
                        )
                    )
                    is_compatible = False
                    continue

                deck_scale = scale_by_tb[tb]
                if topology.assumes_scale != deck_scale:
                    rejections.append(
                        SwapRejection(
                            block_path=block_path,
                            topology_id=topology_id,
                            reason="scale",
                            detail=(
                                f"topology assumes scale {topology.assumes_scale!r} but testbench "
                                f"{tb!r} has scale {deck_scale!r}"
                            ),
                        )
                    )
                    is_compatible = False
                    continue

            if is_compatible:
                # 세 규칙을 전부 통과한 뒤에만 no-op 여부를 본다 - ports가
                # 이미 다르면 시퀀스 비교는 의미가 없다. 모든 테스트벤치에서
                # 완전히 같을 때만 no-op이다: 한 곳에서만 같고 다른 곳에서는
                # 다르면 그 스왑은 적어도 한 덱은 실제로 바꾸므로 no-op이
                # 아니다.
                identical_everywhere = all(
                    _is_identical_body(topology, parsed_by_tb[tb].subckts[block_path].components)
                    for tb in sorted(netlist_texts)
                )
                if identical_everywhere:
                    for tb in sorted(netlist_texts):
                        rejections.append(
                            SwapRejection(
                                block_path=block_path,
                                topology_id=topology_id,
                                reason="identical_body",
                                detail=(
                                    f"topology {topology_id!r} body is structurally identical to the "
                                    f"existing {block_path!r} body in testbench {tb!r}; swapping would "
                                    f"not change the circuit"
                                ),
                            )
                        )
                    is_compatible = False

            if is_compatible:
                candidates.append(SwapCandidate(block_path=block_path, topology_id=topology_id))

    return candidates, rejections
