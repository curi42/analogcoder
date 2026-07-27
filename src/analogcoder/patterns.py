from dataclasses import dataclass
from itertools import combinations

from analogcoder.structure import BlockStructure, ComponentFact, NetlistStructure


@dataclass(frozen=True)
class PatternMatch:
    kind: str
    block: str | None
    members: tuple[str, ...]
    detail: str

    def __post_init__(self) -> None:
        # 한 소자가 자기 자신과 짝지어지는 매치는 어떤 매처가 어떻게
        # 만들어내든 사실일 수 없다 - 분류 버그(모델명이 ctype과 다른
        # 소자 클래스를 시사) 하나를 고치는 것으로는 다음 매처가 같은
        # 실수를 반복하지 않는다는 보장이 안 된다. 여기서 생성 자체를
        # 막아 개별 매처의 조건 목록에 의존하지 않게 한다.
        if len(set(self.members)) != len(self.members):
            raise ValueError(
                f"PatternMatch members must not repeat a refdes: {self.members} ({self.kind})"
            )


def _is_mos(fact: ComponentFact) -> bool:
    return len(fact.terminals) == 4 and [t.name for t in fact.terminals] == ["d", "g", "s", "b"]


def _nets(fact: ComponentFact) -> dict[str, str]:
    return {t.name: net for t, net in zip(fact.terminals, fact.nodes)}


def _same_kind(a: ComponentFact, b: ComponentFact) -> bool:
    """같은 소자 종류인가. 모델 이름이 있으면 그것으로, 없으면 ctype으로 본다.
    nfet과 pfet을 짝지으면 안 되므로 이 비교는 느슨해서는 안 된다."""
    return (a.model or a.ctype) == (b.model or b.ctype)


def _param_ci(fact: ComponentFact, name: str) -> str | None:
    """파라미터를 대소문자 구분 없이 찾는다. SPICE 파라미터 이름은 대소문자를
    구분하지 않는데, 이 저장소도 한 덱 안에서 FET은 W=/L=, sky130의
    res_high_po/cap_mim_m3_1은 w=/l=을 쓴다 (bandgap/netlist.cir,
    two_stage_opamp/netlist.cir). params.get("W")를 그대로 쓰면 소문자로만
    적힌 소자는 항상 None을 돌려주고, 두 소자 다 None이면 None == None이
    그냥 통과해 크기가 실제로는 6배 다른 두 소자를 "크기가 같다"고
    잘못 판정한다 - CLAUDE.md가 이미 두 번 기록한 결함(area_limits의
    바인닝 안 된 W=30 문제)과 같은 종류다."""
    lowered = name.lower()
    for key, value in fact.params.items():
        if key.lower() == lowered:
            return value
    return None


def _matching_size(a: ComponentFact, b: ComponentFact, name: str) -> bool:
    """두 소자의 파라미터가 대소문자 무시하고 "둘 다 선언했고 같다"인가.
    둘 다 모르면(예: 크기가 .model 카드에만 있어 여기 안 보이면) 같다고
    보지 않는다 - 모르는 값끼리의 우연한 동등을 "같다"로 세면 그건 사실이
    아니라 추측이다. 침묵이 정답이다."""
    va, vb = _param_ci(a, name), _param_ci(b, name)
    return va is not None and va == vb


def _two_terminal_nets(fact: ComponentFact) -> tuple[str, str] | None:
    """소자의 두 신호 단자 넷. len(fact.nodes)로 세면 sky130의 res_high_po
    같은 3노드 소자(포트 두 개 + 몸체/웰)가 조건을 만족하지 못해 밀러
    널링 저항 홉이 PDK 덱에서는 죽는다. structure.py가 이미 res/cap
    클래스에 2단자 표(_TWO_TERMINALS)를 매겨 두므로, 그 표(terminals)의
    개수로 판정하고 앞의 두 노드(실제 신호 단자, 몸체는 항상 마지막
    위치)만 취한다."""
    if len(fact.terminals) < 2:
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
            and _matching_size(a, b, "W")
            and _matching_size(a, b, "L")
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
        if (
            na["g"] == nb["g"]
            and na["s"] == nb["s"]
            and (na["g"] == na["d"] or nb["g"] == nb["d"])
            # 드레인과 소스가 같은 넷에 묶인 소자는 도통 방향이 없다 - MOS를
            # 커패시터로 쓸 때(d=s=b를 한 넷에 묶는 sky130 관용구) 나오는
            # 모양이다. 그런 소자가 우연히 다이오드 노드에 게이트를 얹으면
            # 이 조건이 없을 때 진짜 미러처럼 잡힌다 - bandgap의
            # BGR_CORE.Xcc/BUF_N.Xcl/BUF_P.Xcl이 정확히 이 모양이고, 이번
            # 덱에서는 게이트가 우연히 다이오드 노드가 아니라서 피했을
            # 뿐이다(넷 이름 하나만 바뀌어도 거짓 매칭이 난다).
            and na["d"] != na["s"]
            and nb["d"] != nb["s"]
        ):
            diode = a if na["g"] == na["d"] else b
            matches.append(PatternMatch(
                kind="current_mirror", block=block.path,
                members=tuple(sorted((a.refdes, b.refdes))),
                detail=f"shared gate {na['g']}, {diode.refdes} is diode-connected",
            ))

    # 직렬 스택(stacked_pair): 한 소자의 소스가 다른 소자의 드레인에 얹혀
    # 있다. 이것을 "cascode"라 부르지 않는 이유가 있다 - source follower
    # (신호가 게이트로 들어와 소스로 나오는 소자) 위에 전류원이 얹힌 모양,
    # 파워 게이팅 스위치가 소자 하나를 켜고 끄는 모양, 그리고 진짜 캐스코드는
    # 이 지역 서브그래프만 보면 완전히 같다. 셋을 가르려면 어느 넷이
    # "바이어스"고 어느 넷이 "신호"인지를 알아야 하는데 그건 명명 규칙을
    # 보는 일이고, 이 모듈이 금지하는 바로 그 추측이다("전원 레일"을 이름으로
    # 알아보는 것도 마찬가지라 채택하지 않는다).
    #
    # 예전에는 그래서 "cascode"라고 부르고 두 오탐을 테스트로 문서화만 했다.
    # 그러나 거짓 양성 0이 기준이라면 답은 "침묵 아니면 참인 이름"이지
    # "틀린 이름 + 각주"가 아니다. stacked_pair는 셋 모두에 대해 참이고,
    # 매처가 추측하면 안 되는 명명 지식(이게 캐스코드인가 소스 팔로워인가)은
    # 넷리스트 원문을 함께 받는 LLM이 얹으면 된다. detail도 같은 규율을
    # 따른다 - 게이트 넷을 "bias"라 부르지 않고 연결 관계만 낸다.
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
                    kind="stacked_pair", block=block.path,
                    members=tuple(sorted((top.refdes, bottom.refdes))),
                    detail=(
                        f"{top.refdes}.s == {bottom.refdes}.d at {nt['s']}, "
                        f"{top.refdes}.g on {nt['g']}"
                    ),
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
                target = {near, far_side}
                candidates = [d for d in mos if {_nets(d)["g"], _nets(d)["d"]} == target]
                if len(candidates) != 1:
                    # 후보가 없으면 침묵. 둘 이상이면 어느 소자가 "그" 이득단인지
                    # 추측하는 셈이라 역시 침묵한다 - 캐스코드나 미러의 출력
                    # 레그를 우연히 가로지르는 커패시터가 통과하지 못하게 막는다.
                    continue
                device = candidates[0]
                nets = _nets(device)
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

    내는 kind는 diff_pair, current_mirror, stacked_pair, miller_compensation
    넷이다. 세 파생 모듈 중 유일하게 틀릴 수 있는 부분이므로 따로 두었다.
    서브프로젝트 F(토폴로지 라이브러리 확장)가 자라날 자리이기도 하다.

    stacked_pair가 "cascode"가 아닌 이유는 이 규율의 직접적인 귀결이다:
    캐스코드, source follower 위의 전류원, 파워 게이팅 스위치는 지역
    서브그래프가 완전히 같아 구별할 수 없고, 구별하려면 넷 이름을 읽어야
    하는데 그건 금지된 추측이다. 거짓 양성 0이 기준이면 답은 "침묵 아니면
    참인 이름"이므로, 셋 모두에 대해 참인 이름을 낸다 - 어느 쪽인지는
    넷리스트 원문을 함께 받는 LLM이 판단할 몫이다."""
    matches: list[PatternMatch] = []
    for path in sorted(structure.blocks, key=lambda p: (p is not None, p or "")):
        matches += _find_in_block(structure.blocks[path])
    # 대칭 탐색은 같은 매칭을 두 번 낼 수 있다. PatternMatch가 frozen이라
    # dict.fromkeys로 순서를 지키며 중복만 걷어낼 수 있다.
    return sorted(dict.fromkeys(matches), key=lambda m: (m.kind, m.block or "", m.members))
