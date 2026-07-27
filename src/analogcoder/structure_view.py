"""파생된 구조를 LLM 프롬프트로 좁혀 렌더링한다: 초점 선정 + 두 가지 뷰.

실제 프로덕션 넷리스트는 수백~수천 줄이라 원문을 그대로 넘기면 컨텍스트에
안 들어간다. 그렇다고 블록을 걸러 숨기면 답이 있는 블록을 지워버릴 위험이
있으므로, 대신 "계층화된 상세도"를 쓴다: 모든 블록은 항상 한 줄 요약으로
보이고, 초점(focus)에 든 블록만 전체 상세가 붙는다. 넷리스트 원문 뷰도 같은
원칙 - 모든 `.subckt` 헤더는 남기되 초점 밖 블록은 본문만 접는다. 초점이
틀려도 대가는 "관련성 저하"이지 "정답이 안 보임"이 아니다."""

from analogcoder.netlist import (
    is_top_level_stimulus,
    logical_lines,
    resolve_change_scopes,
    split_tokens,
)
from analogcoder.signal_path import SignalPaths
from analogcoder.structure import NetlistStructure


def _definition_name(path: str) -> str:
    """정의 경로의 마지막 조각. signal_path.net_blocks가 정의를 이 이름으로
    (경로가 아니라) 색인하기 때문에, structure.blocks의 경로 키와 잇는
    다리 역할을 이 헬퍼가 한다."""
    return path.rpartition(".")[2]


def _target_ports(structure: NetlistStructure, model: str | None) -> list[str] | None:
    """X 인스턴스가 가리키는 정의의 포트 목록. signal_path._definition_of와
    같은 방식(이름의 마지막 조각으로 조회)이고 같은 한계를 물려받는다.
    못 찾으면 None - 몇 개의 위치 토큰이 실제 노드인지 알 근거가 없다는
    뜻이므로 아예 아무것도 보여주지 않는다."""
    if model is None:
        return None
    for path, block in structure.blocks.items():
        if path is not None and path.rpartition(".")[2] == model:
            return block.ports
    return None


def _with_ancestors(scopes: set[str]) -> set[str]:
    """중첩 정의 경로가 하나 들어오면 그 모든 조상 경로도 함께 담는다.
    render_netlist는 중첩된 정의를 부모와 통째로 접으므로(216-218행 주석 -
    접힌 본문 안에 헤더만 남기면 그 헤더가 어디 소속인지 알 수 없는 조각이
    되기 때문), 조상이 초점 밖이면 자손 하나만 초점에 넣어봤자 부모가
    접히는 순간 그 자손의 본문까지 함께 묻힌다. "OUTER.INNER.DEEP"은
    "OUTER", "OUTER.INNER", "OUTER.INNER.DEEP" 셋 다를 낸다. 점이 없는
    경로(중첩 아님)는 자기 자신만 낸다 - 순수 no-op."""
    result: set[str] = set()
    for scope in scopes:
        parts = scope.split(".")
        result.update(".".join(parts[: i + 1]) for i in range(len(parts)))
    return result


def select_focus(
    structure: NetlistStructure,
    paths: SignalPaths,
    failing_nets: set[str],
    touched_refdes: set[str],
    netlist_text: str,
) -> set[str]:
    """상세히 렌더링할 정의 경로의 집합.

    최상위 스코프(None)는 반환값에 담지 않는다 - 테스트벤치의 자극원과 DUT
    인스턴스가 거기 있어 언제나 초점이므로, 렌더러가 무조건 포함한다.

    씨앗은 세 갈래로 모은다:
      1) 실패한 넷을 직접 건드리는(구동/감지) 블록.
      2) 그 씨앗이 "감지"하는 넷을 누가 "구동"하는가 - 역방향 1홉. 씨앗
         블록이 잘못된 값을 감지하고 있다면 그 값을 만든 상류 블록을 봐야
         튜너가 원인 쪽을 고칠 수 있다.
      3) 이번 실행에서 이미 값이 바뀐 refdes가 속한 블록 - 방금 건드린
         블록을 다음 반복에서 시야 밖으로 내보내면 그 변경의 효과를 이어서
         판단할 수 없다.
    씨앗이 하나도 안 잡히면(예: failing_nets가 measurement_nets에 없는
    이름이라 넷으로 못 옮겨졌을 때) 조용히 아무것도 안 보여주는 대신
    전 블록을 노출한다 - "모르면 침묵"의 예외로, 안전한 쪽이 더 넓게
    보여주는 쪽이기 때문이다.

    넷별 역할은 paths.roles_on으로 읽는다 - 전원/자극 넷에서는 drive 주장이
    빠지므로, 레일을 무는 2단자 소자밖에 없는 블록이 그 레일 하나로 씨앗이
    되어 초점을 번지게 하지 않는다. 그러나 그 넷을 **감지**하는 블록은
    씨앗이 된다: 자극 입력 넷에서 측정한 기준은 그 입력을 받는 블록을 봐야
    한다. 같은 이유로 역방향 홉은 레일에서 상류를 찾지 않는다 - 그 넷의
    드라이버는 블록이 아니라 소스다.

    **알려진 한계: 씨앗 블록의 입력이 중간 정의 내부의 넷이면 역방향 홉이
    발화하지 않는다.** paths.net_blocks는 최상위 좌표계의 넷만 담는다
    (signal_path.walk가 매 깊이 좌표를 부모 넷 이름으로 바꾸다가, 부모의
    포트가 아닌 넷은 밀어올릴 좌표가 없어 버린다 - 그렇게 안 하면 이름이
    같을 뿐인 무관한 최상위 넷에 역할을 잘못 붙이게 된다). 그래서 실제
    bandgap에서 BUF_P의 입력 vt05는 BANDGAP 내부 넷이라 net_blocks에 아예
    없고, BUF_P가 최상위에서 닿는 유일한 넷 vbg0은 자기가 구동하는 넷이다 -
    결과적으로 `vbg0 -> BUF_P -> 상류(저항 사다리)`라는 설계 문서의 예시는
    이 덱에서 성립하지 않는다. 홉 자체는 정확하고 합성 덱으로 고정되어
    있지만(test_the_reverse_hop_fires_from_a_block_that_both_drives_and_senses_its_net),
    이 덱이 홉을 제공하지 않는다. 그 경우 정답 노브에 닿게 하는 것은 초점이
    아니라 튜너 프롬프트다 - 접힌 블록도 전체 경로로 지목할 수 있다고
    명시되어 있다. 중간 정의 내부 넷까지 다루려면 net_blocks가 스코프 한정
    좌표를 함께 담아야 하고, 그건 이 계층의 설계 변경이다.

    netlist_text가 필요한 이유는 touched_refdes 때문이다: 언스코프
    refdes("M9")가 어느 서브회로 소속인지는 문자열만 봐서는 알 수 없고,
    check_refdes_resolution은 유일하게 해석되는 언스코프 refdes를 명시적으로
    허용한다. 형제 함수 focus_misses와 같은 원시 함수
    (netlist.resolve_change_scopes)를 쓴다."""
    definitions = {path for path in structure.blocks if path is not None}
    by_name = {_definition_name(path): path for path in definitions}

    blocks_on = paths.roles_on

    seeds: set[str] = set()
    for net in failing_nets:
        for name in blocks_on(net):
            if name in by_name:
                seeds.add(by_name[name])

    # 역방향 1홉: 씨앗 블록이 감지(sense)하는 넷을, 다른 블록이 구동(drive)
    # 하고 있으면 그 구동 블록도 초점에 넣는다.
    sensed_nets = {
        net
        for net in paths.net_blocks
        if any(
            by_name.get(name) in seeds and "sense" in roles
            for name, roles in blocks_on(net).items()
        )
    }
    upstream = {
        by_name[name]
        for net in sensed_nets
        for name, roles in blocks_on(net).items()
        if "drive" in roles and name in by_name
    }

    touched = resolve_change_scopes(netlist_text, [{"refdes": r} for r in sorted(touched_refdes)])

    # seeds/upstream도 by_name[name]을 거쳐 정의 경로를 얻는데, name은 항상
    # 정의 이름의 마지막 조각(net_blocks의 색인 방식)이라 그 경로가 중첩
    # 정의일 수 있다 - touched와 같은 모양의 문제다. 그래서 조상 보정은 셋을
    # 합친 뒤 한 번만 적용한다: 어느 갈래로 들어왔든 그 경로가 중첩이면
    # 조상까지 함께 넣어야 render_netlist의 폴딩이 그 자손을 삼키지 않는다.
    focus = _with_ancestors(seeds | upstream | (touched & definitions)) & definitions
    return focus or definitions


def render_structure(
    structure: NetlistStructure, paths: SignalPaths, patterns: list, focus: set[str]
) -> str:
    """계층화된 구조 뷰: 레벨 0에는 모든 블록이 한 줄씩, 그 아래에는 초점에
    든 블록(과 언제나 초점인 최상위)만 부품 주소록이 붙는다. 주소록은
    참조부호와 파라미터 "이름"만 낸다 - 값은 넷리스트 원문에만 산다."""
    lines = [f"circuit: {structure.circuit_name}", "", "blocks:"]

    # 정의 이름별 drive/sense 넷 목록. net_blocks가 이름으로 색인되어 있으므로
    # 레벨 0 요약도 이름 기준으로 뒤집어 만든다.
    # paths.roles_on을 거친다 - 전원/자극 넷에서는 drive 주장만 사라지고
    # sense 주장은 남는다. 이유는 SignalPaths.roles_on 참고.
    drives: dict[str, list[str]] = {}
    senses: dict[str, list[str]] = {}
    for net in sorted(paths.net_blocks):
        for name, roles in sorted(paths.roles_on(net).items()):
            for role in sorted(roles):
                (drives if role == "drive" else senses).setdefault(name, []).append(net)

    for path in sorted(p for p in structure.blocks if p is not None):
        block = structure.blocks[path]
        name = _definition_name(path)
        lines.append(
            f"  {path}  {block.instance_count} instance(s)  "
            f"{len(block.components)} comps  "
            f"drives {','.join(drives.get(name, [])) or '-'}  "
            f"senses {','.join(senses.get(name, [])) or '-'}"
        )

    for path in sorted(focus | {None}, key=lambda p: (p is not None, p or "")):
        block = structure.blocks.get(path)
        if block is None:
            continue
        label = path or "<top level>"
        lines += ["", f"{label}  ports: {' '.join(block.ports) or '-'}"]
        for fact in block.components:
            detail = f"  {fact.refdes} {fact.model or fact.ctype}"
            if fact.terminals:
                detail += "  " + " ".join(
                    f"{t.name}={net}{'(sense)' if t.role == 'sense' else ''}"
                    for t, net in zip(fact.terminals, fact.nodes)
                )
            elif fact.ctype == "X":
                # 서브회로 인스턴스는 단자 역할표가 없어도 정의의 포트 수는
                # 안다 - 그 개수만큼만 노드를 보여준다. 그 이상은 노드가
                # 아니라 구조.py의 위치 분해가 걸러내지 못한 다른 토큰(예:
                # 괄호를 쓰는 값의 잔여물)일 수 있어, 그대로 echo하면 값이
                # 새어 나온다.
                ports = _target_ports(structure, fact.model)
                if ports:
                    detail += "  " + " ".join(fact.nodes[: len(ports)])
            # 그 외(V/I 등 단자표가 없는 소자)는 refdes/ctype만 낸다. nodes를
            # 그대로 echo하던 예전 방식은 값 토큰(예: DC 1.8의 1.8)까지
            # nodes에 섞여 있는 경우가 있어 넷리스트 원문과 값이 겹쳐 버렸다.
            lines.append(detail)
        matched = [p for p in patterns if p.block == path]
        if matched:
            lines.append(
                "  patterns: " + "  ".join(f"{p.kind}({','.join(p.members)})" for p in matched)
            )
        # "BUF_P.X6.W"로 붙여 쓰면 점 하나가 스코프 구분자와 param 구분자를
        # 겸하게 되어, 스키마의 두 칸(refdes/param)이 뷰에서는 한 덩어리로
        # 보인다 - CLAUDE.md가 실제 실패로 기록한 "M1.W를 refdes 칸에 썼다"를
        # 뷰 자신이 가르치는 셈이라 두 칸을 이름 붙여 떼어 놓는다.
        addresses = [
            f"refdes={e.refdes} param={e.param}"
            for e in structure.tunable
            if (e.refdes.rpartition(".")[0] or None) == path
        ]
        if addresses:
            lines.append("  tunable: " + "  ".join(addresses) + "   (값은 넷리스트 원문에서 읽을 것)")
        # 최상위 독립 소스는 주소록에서 빠진다(structure.py). 조용히 빼면
        # "왜 Vin이 없지"를 아무도 알 수 없으므로, 빠졌다는 사실을 낸다.
        stimulus = [f.refdes for f in block.components if is_top_level_stimulus(path, f.ctype)]
        if stimulus:
            lines.append("  stimulus (not tunable): " + " ".join(stimulus))

    # 인스턴스-정의 포트 수 불일치는 유일하게 "모르면 침묵"의 예외다 - 넷리스트
    # 버그이므로 초점과 무관하게 항상 드러낸다.
    for edge in paths.instances:
        if edge.mismatch:
            lines.append(f"WARNING: {edge.mismatch}")

    return "\n".join(lines)


def render_netlist(netlist_text: str, focus: set[str]) -> str:
    """초점 블록은 본문 전문, 비초점 블록은 헤더만 남기고 본문을 접는다.
    최상위 줄(자극원, DUT 인스턴스, 지시문)은 전부 남긴다.

    축약 단위는 정의다: 중첩된 정의는 바깥이 접히면 함께 접힌다 - 접힌 본문
    안에 헤더만 남기면 그 헤더가 어디에 속하는지 알 수 없는 조각이 되기
    때문이다. 그래서 fold_start는 "몇 번째 깊이에서 접힘이 시작됐는가"만
    기억한다: 그 깊이로 돌아오는 .ends를 만날 때까지, 그 사이의 모든 중첩
    헤더/본문은 개별 초점 여부와 무관하게 통째로 묻힌다.

    지시문/부품 판정은 logical_lines가 접어 준 코드로 한다 - `+` 연속 줄을
    독립된 문장으로 보면 그 줄이 엉뚱하게 새 헤더나 별개의 "elided
    component"로 잡혀, E1이 겪은 것과 같은 모양(연속 줄을 딴 소자로 착각)의
    왜곡이 재발한다. 다만 화면에 실제로 내는 것은 원문 물리 줄이다 - 이
    뷰는 프롬프트 텍스트일 뿐 재파싱될 구조가 아니므로 원래 서식을 그대로
    보여주는 쪽이 사람이 읽기에도 맞다."""
    names = {_definition_name(path) for path in focus}
    physical_lines = netlist_text.splitlines()

    # 논리 줄의 첫 물리 줄(앵커)에서만 판정하고, 나머지 물리 줄(`+` 연속)은
    # 앵커와 운명을 같이한다 - 별개의 지시문/부품으로 세지 않는다. owner는
    # 모든 물리 줄(앵커 자신 포함)을 그 논리 줄의 앵커 인덱스로 매핑한다.
    owner: dict[int, int] = {}
    anchor_code: dict[int, str] = {}
    for code, indices in logical_lines(physical_lines):
        anchor = indices[0]
        anchor_code[anchor] = code
        for idx in indices:
            owner[idx] = anchor

    out: list[str] = []
    stack: list[str] = []
    fold_start: int | None = None  # None이면 현재 안 접는 중
    elided = 0
    # 실제로 라인을 냈던 앵커의 집합. 연속 줄은 "지금 접는 중인가"가 아니라
    # "자신의 앵커가 실제로 보였는가"로 판단해야 한다 - 헤더 자신이 여러 줄에
    # 걸쳐 있으면(`.subckt WIDE a b c\n+ d e f`), 헤더를 낸 바로 그 순간에
    # 본문 접힘이 시작되어도(fold_start가 곧장 채워져도) 헤더 자신의 나머지
    # 물리 줄까지는 여전히 보여야 한다 - 안 그러면 여러 줄짜리 헤더가 잘려
    # 포트 일부만 보이는 채로 넘어간다.
    emitted_anchors: set[int] = set()

    for idx, raw_line in enumerate(physical_lines):
        anchor = owner.get(idx)
        if anchor is None:
            # 빈 줄이나 `*` 전체 주석 줄 - 지시문도 부품도 아니다.
            if fold_start is None:
                out.append(raw_line)
            continue

        if anchor != idx:
            # `+` 연속 줄: 자신의 앵커가 실제로 출력됐을 때만 함께 낸다.
            if anchor in emitted_anchors:
                out.append(raw_line)
            continue

        code = anchor_code[idx]
        lowered = code.lower()

        if lowered.startswith((".subckt", ".macro")):
            name = split_tokens(code)[1]
            stack.append(name)
            if fold_start is None:
                # 지금 접는 중이 아니므로 이 헤더는 보인다 - 초점 여부와
                # 무관하게 헤더는 항상 남긴다.
                out.append(raw_line)
                emitted_anchors.add(idx)
                if name not in names:
                    fold_start = len(stack)
            continue

        if lowered.startswith((".ends", ".eom")):
            depth = len(stack)
            if stack:
                stack.pop()
            if fold_start == depth:
                # `*` 주석으로 쓴다 - 이 텍스트는 프롬프트 전용이고 절대
                # ngspice로 가지 않지만, SPICE로 읽어도 무해한 형태여야
                # 사람이 붙여 넣어 볼 때 오해가 없다.
                out.append(f"* ... ({elided} components elided)")
                out.append(raw_line)
                emitted_anchors.add(idx)
                elided = 0
                fold_start = None
            elif fold_start is None:
                out.append(raw_line)
                emitted_anchors.add(idx)
            continue

        if fold_start is not None:
            # 지시문(`.param`, `.model`, ...)은 부품이 아니다. 앵커를 그대로
            # 세면 `.param` + `.model` + 소자 하나뿐인 본문이 "(3 components
            # elided)"로 나와, 접힌 블록의 규모를 모델에게 잘못 알려준다.
            if not code.startswith("."):
                elided += 1
            continue

        out.append(raw_line)
        emitted_anchors.add(idx)

    if fold_start is not None:
        # `.ends` 없이 파일이 끝난 경우(잘린 덱, 발췌 붙여넣기). 닫는 지점이
        # 영영 오지 않아 마커도 `.ends`도 안 나오고 프롬프트가 그냥 끊겨,
        # 모델은 뒤에 무엇이 있었는지 알 길이 없다. 접힘의 존재만은 남긴다.
        out.append(f"* ... ({elided} components elided)")

    return "\n".join(out)


def focus_misses(focus: set[str], changes: list[dict], netlist_text: str) -> list[str]:
    """초점 밖 블록을 지목한 제안의 refdes 목록. 그런 일이 일어났다는 것은
    초점 규칙이 원인 블록을 놓쳤다는 증거이므로 기록해 둔다 - 제안 자체는
    이 함수와 무관하게 정상 적용된다.

    실제 위치는 netlist.resolve_change_scopes로 판정한다 - refdes 앞의 점만
    잘라 스코프로 읽던 이전 구현은 언스코프 refdes("M6")가 비초점 서브회로
    안에 있어도 놓쳤다(dotted 형태만 잡혔다). netlist_text가 필요한 이유가
    그것이다: 문자열만 봐서는 "M6"이 어느 서브회로 소속인지 알 수 없다."""
    misses = []
    for change in changes:
        scopes = resolve_change_scopes(netlist_text, [change])
        if any(scope not in focus for scope in scopes):
            misses.append(change["refdes"])
    return misses
