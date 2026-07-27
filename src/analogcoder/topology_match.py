"""(block, topology) 호환성을 결정론적으로 판정한다.

오케스트레이터가 토폴로지 스왑을 제안할 때, "정의가 딱 하나뿐인 덱"이라는
낡은 제약 대신 실제로 안전하게 갈아끼울 수 있는 (block_path, topology_id)
쌍만 후보로 내놓기 위한 순수 함수 모듈이다. 이 모듈은 게이트가 아니라
**후보 생성기**다 - 오케스트레이터(Task 5)는 여기서 나온 후보만 LLM에
제시한다.

세 판정 규칙은 전부 파싱된 사실에만 근거한다 - 이름에서 의미를 추측하지
않는다:

1. 포트: `set(topology.ports) == set(block.ports)` (양방향 집합 동등,
   순서 무시). 한 방향만 보면 9포트 대상에 5포트 본문이 통과해 바이어스
   포트 4개가 조용히 뜬 노드가 된다.
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
"""

from dataclasses import dataclass

from analogcoder.netlist import Component, ParsedNetlist, netlist_scale, parse_netlist, parse_spice_value
from analogcoder.topologies import Topology


@dataclass(frozen=True)
class SwapCandidate:
    block_path: str
    topology_id: str


@dataclass(frozen=True)
class SwapRejection:
    block_path: str
    topology_id: str
    reason: str  # "ports" | "models" | "scale" | "missing_in_testbench"
    detail: str


def _is_model_name(value: str | None) -> bool:
    """위치 값이 모델/서브회로 이름인가 - 숫자로 파싱되지 않을 때 그렇다.

    structure.py의 `_is_numeric_value`와 같은 규칙이지만 그것은 private라
    모듈 경계를 넘어 재사용하지 않는다 (요구사항의 명시적 지시) - 이 모듈에
    같은 것을 다시 둔다."""
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


def _topology_model_names(topology: Topology) -> set[str]:
    """토폴로지 본문이 쓰는 모델 이름 집합. 본문을 `.subckt TMP <ports>` ...
    `.ends TMP`로 감싸 parse_netlist로 파싱한다 - 본문 자체는 독립된 SPICE
    조각(포트 헤더가 없는 컴포넌트 나열)이라 그대로는 파싱 대상이 아니다."""
    wrapped = f".subckt TMP {' '.join(topology.ports)}\n{topology.subckt_body}\n.ends TMP\n"
    parsed = parse_netlist(wrapped)
    return _all_model_names(parsed)


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

                if set(topology.ports) != set(subckt.ports):
                    rejections.append(
                        SwapRejection(
                            block_path=block_path,
                            topology_id=topology_id,
                            reason="ports",
                            detail=(
                                f"topology ports {sorted(topology.ports)} != block ports "
                                f"{sorted(subckt.ports)} in testbench {tb!r}"
                            ),
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
                candidates.append(SwapCandidate(block_path=block_path, topology_id=topology_id))

    return candidates, rejections
