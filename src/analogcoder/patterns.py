from dataclasses import dataclass
from itertools import combinations

from analogcoder.structure import BlockStructure, ComponentFact, NetlistStructure


@dataclass(frozen=True)
class PatternMatch:
    kind: str
    block: str | None
    members: tuple[str, ...]
    detail: str


def _is_mos(fact: ComponentFact) -> bool:
    return len(fact.terminals) == 4 and [t.name for t in fact.terminals] == ["d", "g", "s", "b"]


def _nets(fact: ComponentFact) -> dict[str, str]:
    return {t.name: net for t, net in zip(fact.terminals, fact.nodes)}


def _same_kind(a: ComponentFact, b: ComponentFact) -> bool:
    """같은 소자 종류인가. 모델 이름이 있으면 그것으로, 없으면 ctype으로 본다.
    nfet과 pfet을 짝지으면 안 되므로 이 비교는 느슨해서는 안 된다."""
    return (a.model or a.ctype) == (b.model or b.ctype)


def _two_terminal_nets(fact: ComponentFact) -> tuple[str, str] | None:
    if len(fact.nodes) != 2:
        return None
    return fact.nodes[0], fact.nodes[1]


def _source_fanout(net: str, mos: list[ComponentFact]) -> int:
    """이 블록에서 net을 소스 단자로 무는 MOS 개수. 진짜 차동쌍의 tail은
    정확히 둘(다리 두 개)이 물고, 진짜 캐스코드의 스택 노드는 정확히 하나
    (위에 얹힌 그 소자)만 문다 - vss 같은 전원 레일은 훨씬 많은 소자가
    문다. 이 수를 세는 것이 "우연히 같은 소스" 대 "구조적으로 그 소스"를
    가르는 유일한 형태-무관 신호다 - 실제 bandgap 넷리스트의 startup 체인
    (Xsu_d, Xsu_i)이 우연히 같은 W/L로 vss를 공유해 diff_pair로 오판되는
    것을 이 카운트로 막는다."""
    return sum(1 for m in mos if _nets(m)["s"] == net)


def _find_in_block(block: BlockStructure) -> list[PatternMatch]:
    matches: list[PatternMatch] = []
    mos = [f for f in block.components if _is_mos(f)]

    for a, b in combinations(mos, 2):
        na, nb = _nets(a), _nets(b)
        if not _same_kind(a, b):
            continue
        if (
            na["s"] == nb["s"]
            and na["g"] != nb["g"]
            and na["d"] != nb["d"]
            and a.params.get("W") == b.params.get("W")
            and a.params.get("L") == b.params.get("L")
            # 소스 팬아웃이 정확히 2(이 둘뿐)여야 한다 - 아니면 흔한 레일을
            # 우연히 같은 크기로 공유하는 무관한 두 소자를 차동쌍으로
            # 잘못 부르게 된다.
            and _source_fanout(na["s"], mos) == 2
        ):
            matches.append(PatternMatch(
                kind="diff_pair", block=block.path,
                members=tuple(sorted((a.refdes, b.refdes))),
                detail=f"common source {na['s']}, gates {na['g']}/{nb['g']}",
            ))
        if na["g"] == nb["g"] and na["s"] == nb["s"] and (
            na["g"] == na["d"] or nb["g"] == nb["d"]
        ):
            diode = a if na["g"] == na["d"] else b
            matches.append(PatternMatch(
                kind="current_mirror", block=block.path,
                members=tuple(sorted((a.refdes, b.refdes))),
                detail=f"shared gate {na['g']}, {diode.refdes} is diode-connected",
            ))

    for upper, lower in combinations(mos, 2):
        for top, bottom in ((upper, lower), (lower, upper)):
            nt, nbm = _nets(top), _nets(bottom)
            if not _same_kind(top, bottom):
                continue
            if (
                nt["s"] == nbm["d"]
                and nt["g"] != nt["s"]
                and nt["g"] != nbm["g"]
                # 스택 노드를 소스로 무는 소자가 top 하나뿐이어야 진짜 직렬
                # 스택이다. 차동쌍 두 다리가 같은 tail 전류원 드레인 위에
                # 나란히 앉는 경우(실제 두 벤치마크 모두에 있음) 이 조건이
                # 없으면 각 다리가 tail 소자와 캐스코드 쌍으로 오판된다 -
                # 그건 스택이 아니라 분기다.
                #
                # 드레인 쪽은 대칭으로 세지 않는다: 폴디드 캐스코드의 폴드
                # 노드는 구조적으로 드레인이 정확히 둘(입력 소자 + 접는
                # 소자, 서로 다른 극성)이다 - bandgap 벤치마크로 직접 확인한
                # 사실이다. 그쪽에 팬아웃==1을 요구하면 진짜 폴디드 캐스코드
                # 매칭 자체가 사라진다. _same_kind가 이미 그 둘 중 극성이
                # 맞는 하나만 top과 짝짓게 걸러 준다.
                and _source_fanout(nt["s"], mos) == 1
            ):
                matches.append(PatternMatch(
                    kind="cascode", block=block.path,
                    members=tuple(sorted((top.refdes, bottom.refdes))),
                    detail=f"{top.refdes} stacked on {bottom.refdes} at {nt['s']}, bias {nt['g']}",
                ))

    # 밀러 보상: 커패시터가 어떤 이득단의 입력 게이트와 출력 드레인을 잇는다.
    # 직렬 저항이 끼어 있으면 저항 너머까지 한 단계 따라간다.
    caps = [f for f in block.components if f.ctype == "C" or (f.device_class == "cap")]
    resistors = [f for f in block.components if f.ctype == "R" or (f.device_class == "res")]
    for cap in caps:
        endpoints = _two_terminal_nets(cap)
        if endpoints is None:
            continue
        # 커패시터의 두 끝은 대칭이다. 한쪽만 고정하면 Cc가 어느 방향으로
        # 적혔는지에 따라 매칭이 되기도 하고 안 되기도 한다.
        for near, other in (endpoints, tuple(reversed(endpoints))):
            for far_side, extra in _reachable(other, resistors):
                for device in mos:
                    nets = _nets(device)
                    if {near, far_side} != {nets["g"], nets["d"]}:
                        continue
                    members = tuple(sorted((cap.refdes, device.refdes) + tuple(extra)))
                    matches.append(PatternMatch(
                        kind="miller_compensation", block=block.path, members=members,
                        detail=f"{cap.refdes} bridges {nets['g']} and {nets['d']} of {device.refdes}",
                    ))

    return matches


def _reachable(net: str, resistors: list[ComponentFact]) -> list[tuple[str, tuple[str, ...]]]:
    """넷 자신과, 직렬 저항 하나를 건넌 넷들. 두 개 이상은 따라가지 않는다 -
    보상 회로가 아닌 저항 네트워크를 밀러로 오인하기 시작하는 지점이다."""
    out = [(net, ())]
    for resistor in resistors:
        pair = _two_terminal_nets(resistor)
        if pair is None:
            continue
        if pair[0] == net:
            out.append((pair[1], (resistor.refdes,)))
        elif pair[1] == net:
            out.append((pair[0], (resistor.refdes,)))
    return out


def find_patterns(structure: NetlistStructure) -> list[PatternMatch]:
    """네 가지 지역 서브그래프 매칭. **절대 추측하지 않는다** - 매칭되면
    사실이고, 매칭되지 않으면 침묵이다. LLM이 스키마를 만족시키려고
    {"a": "b"}를 채워 넣던 것의 정확한 반대편이며, 받아들이는 기준도 재현율이
    아니라 거짓 양성 0이다.

    세 파생 모듈 중 유일하게 틀릴 수 있는 부분이므로 따로 두었다.
    서브프로젝트 F(토폴로지 라이브러리 확장)가 자라날 자리이기도 하다."""
    matches: list[PatternMatch] = []
    for path in sorted(structure.blocks, key=lambda p: (p is not None, p or "")):
        matches += _find_in_block(structure.blocks[path])
    # 대칭 탐색은 같은 매칭을 두 번 낼 수 있다. PatternMatch가 frozen이라
    # dict.fromkeys로 순서를 지키며 중복만 걷어낼 수 있다.
    return sorted(dict.fromkeys(matches), key=lambda m: (m.kind, m.block or "", m.members))
