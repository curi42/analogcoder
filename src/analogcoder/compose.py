"""조각들을 하나의 덱으로 잇는다 - 그리고 잇는 동안 조용히 틀릴 수 있는
모든 자리에서 시끄럽게 실패한다.

**왜 이 모듈이 있는가.** 대상 흐름의 최종 덱은 `신호 선언부 + 코너 +
넷리스트`를 조합해서 만들어진다. 그러면 `pvt.render_corner_report`의 정규식
세 개(include 교체, `.temp` 주입, `^Vdd` 전압 치환)가 **사라진다** - 코너가
슬롯이면 (1)은 문자열 찾기가 아니라 자리 채우기이고, (2)·(3)은 코너 파일이
자기 안에서 한다. 그 세 정규식은 이 저장소가 값을 치른 자리다:
`netlist_startup.cir`의 PWL 전원 때문에 45코너가 실은 15조건이었고 아무 말도
없었다. `re.sub`는 구조적으로 조용하다.

**그러나 그 자리에 더 조용한 실패군이 들어온다.** 아래 검사는 전부 실제
ngspice-46으로 재현한 실패에서 나왔다(2026-07-29):

- 조각 1의 첫 줄이 문장이면 SPICE가 그것을 **제목**으로 먹고 회로에서
  사라진다(측정: gain_db 19.999 -> 100.0, 경고 0건). -> 조합기가 자기 제목을
  넣고 **센다**.
- 지시자 충돌의 승자 규칙이 지시자마다 다르다: `.model`/`.option scale`/
  `.subckt`은 **먼저** 것이, `.param`/`.temp`는 **나중** 것이 이기고 전부
  침묵한다. 안전한 조각 순서가 존재하지 않으므로 **충돌 자체를 금지**한다.
- 상대 `.include`는 cwd가 조합 덱의 디렉터리를 가린다(실측: 덱 옆에 놓은 ss
  코너가 무시되고 tt 값이 그대로, 경고 0건). -> 절대경로만 받는다.
- 조각 사이 개행이 빠지고 앞 조각이 주석으로 끝나면 뒤 조각의 첫 줄이 그
  주석에 흡수된다. -> 경계 개행을 **세어서** 넣는다.
- `.ends`의 **이름** 불일치는 ngspice가 아무 말도 하지 않는다(개수 불일치는
  시끄럽다). -> 조각별로 depth와 이름을 본다.
- 중간 `.end`는 ngspice-46에서는 합쳐지지만 **HSPICE는 미확인**이고, 잘린다면
  N개 코너가 넷리스트 없는 덱을 돈다. -> 정적으로 하나·마지막을 요구한다.
  "ngspice에서 돌아갔다"는 근거로 삼지 않는다.
- 넷 이름 충돌은 **원리상** 시뮬레이터가 잡을 수 없고 의도를 모르면 옳고
  그름을 판정할 수 없다. -> 게이트가 아니라 **보고**다.

**세는 것이 규칙이다.** 이 저장소는 요청한 재작성을 전부 세고(`re.subn`) 그
결과를 기록한다. 여기서는 삽입·정규화가 그 자리를 차지하므로 `records`가
언제나 모든 검사의 수를 싣는다 - 하나도 발화하지 않은 조합에서도. 그래야
"검사했고 괜찮다"와 "검사가 통째로 사라졌다"가 구별된다.

**이 검사들은 오늘의 벤치마크 11개 덱에서 거의 전부 통과한다** - 즉 그것만
보고 출하하면 조용히 무력한 게이트가 하나 더 늘어난다. 그래서 각 항목마다
음성 픽스처가 `tests/unit/test_compose.py`에 고정되어 있다.
"""

import os
from collections import defaultdict
from dataclasses import dataclass, replace

from analogcoder.netlist import logical_lines, parse_netlist, resolve_includes, split_tokens


class ComposeError(ValueError):
    """조각들을 이을 수 없다.

    **`ValueError` 하위인 것은 의도다** - `run_orchestration`과
    `run_optimization`이 이미 `ValueError`를 트레이스백이 아니라 깨끗한
    FAIL / `optimize_failed`로 접는다. `CornerRenderError`가 같은 이유로
    같은 선택을 했다."""


@dataclass(frozen=True)
class Fragment:
    """조합 덱을 이루는 조각 하나.

    `name`은 **진단의 전부**다. ngspice는 합쳐진 덱의 줄 번호만 말하므로
    어느 조각이 충돌을 냈는지는 조합기만 안다."""

    name: str
    text: str


@dataclass(frozen=True)
class ComposedDeck:
    """조합된 덱과, 조합이 실제로 **무엇을 했는지**.

    `records`는 센 것(삽입·정규화·검사 대상 수)이고 `report`는 판정하지 않고
    적기만 하는 사실(조각 간 공유 넷, 조각별 최상위 기여)이다. 둘을 나눈
    이유는 후자가 게이트가 아니기 때문이다 - 넷 이름 충돌의 옳고 그름은
    의도를 모르면 판정할 수 없다."""

    text: str
    records: dict
    report: dict


def _fragment_statements(fragment: Fragment) -> list[str]:
    """조각의 논리 줄(코드부만, `+` 연속 접힘)."""
    return [code for code, _ in logical_lines(fragment.text.split("\n"))]


def _check_subckt_balance(fragment: Fragment, statements: list[str]) -> int:
    """조각 안에서 `.subckt`가 열리고 닫히는지, 이름이 맞는지.

    개수 불일치는 ngspice가 시끄럽게 잡지만 **이름 불일치는 완전히 침묵**한다
    (실측: `.subckt AMP`를 `.ends BUF`로 닫아도 정상 실행, 값도 동일). 조각
    경계가 subckt 본문 안을 지나가는 경우의 유일한 신호이므로 조합기가 본다.

    돌려주는 것은 이 조각이 최상위에 연 subckt 정의의 수다."""
    stack: list[str] = []
    definitions = 0
    for code in statements:
        lower = code.lower()
        if lower.startswith(".subckt") or lower.startswith(".macro"):
            tokens = split_tokens(code)
            name = tokens[1] if len(tokens) > 1 else ""
            if not stack:
                definitions += 1
            stack.append(name)
        elif lower.startswith(".ends") or lower.startswith(".eom"):
            if not stack:
                raise ComposeError(
                    f"fragment {fragment.name!r} closes a .subckt it never opened: {code!r}"
                )
            opened = stack.pop()
            tokens = split_tokens(code)
            closing = tokens[1] if len(tokens) > 1 else None
            if closing is not None and opened and closing != opened:
                raise ComposeError(
                    f"fragment {fragment.name!r} opens '.subckt {opened}' and closes it "
                    f"with '.ends {closing}': ngspice accepts this silently and runs the "
                    f"deck, so a fragment boundary drawn through a subckt body has no "
                    f"other signal"
                )
    if stack:
        raise ComposeError(
            f"fragment {fragment.name!r} leaves .subckt {stack[-1]!r} open at its end: a "
            f"fragment must be balanced on its own, or the next fragment's top level "
            f"silently becomes that subckt's body"
        )
    return definitions


def _include_paths(code: str) -> list[str]:
    """`.include` / `.lib` 가 가리키는 경로들."""
    tokens = split_tokens(code)
    if len(tokens) < 2:
        return []
    return [tokens[1].strip('"').strip("'")]


def _directive_keys(code: str, depth: int) -> list[tuple]:
    """이 문장이 **최상위에** 선언하는 지시자의 정체성 키들.

    스코프가 다르면 충돌이 아니므로 `depth == 0`만 본다. 키를 이렇게 잘게
    나누는 이유는 승자 규칙이 지시자마다 다르기 때문이다 - 같은 이름의
    `.model` 두 개는 먼저 것이 이기고, 같은 이름의 `.param` 두 개는 나중
    것이 이긴다. 둘 다 침묵한다."""
    if depth != 0:
        return []
    lower = code.lower()
    tokens = split_tokens(code)
    if lower.startswith(".model") and len(tokens) > 1:
        return [("model", tokens[1])]
    if lower.startswith(".subckt") or lower.startswith(".macro"):
        return [("subckt", tokens[1])] if len(tokens) > 1 else []
    if lower.startswith(".temp"):
        return [("temp",)]
    if lower.startswith(".option"):
        # `.option scale=1.0u` 와 `.option scale 1.0u` 둘 다 이름만 본다.
        keys = []
        for token in tokens[1:]:
            keys.append(("option", token.split("=")[0].lower()))
        return keys
    if lower.startswith(".param"):
        keys = []
        for token in tokens[1:]:
            if "=" in token:
                keys.append(("param", token.split("=")[0]))
        return keys
    if lower.startswith(".include") or lower.startswith(".lib"):
        return [("include", path) for path in _include_paths(code)]
    return []


def _is_directive(code: str) -> bool:
    return code.startswith(".")


def compose(fragments, *, title: str) -> ComposedDeck:
    """조각들을 순서대로 이어 하나의 덱으로 만든다.

    올린 예외는 전부 `ComposeError`이고, 통과한 조합은 무엇을 검사했는지를
    `records`에 남긴다."""
    fragments = list(fragments)
    if not fragments:
        raise ComposeError("compose() needs at least one fragment")

    names = [f.name for f in fragments]
    if len(set(names)) != len(names):
        raise ComposeError(f"fragment names must be unique for attribution: {names}")

    # ---- 조각별 정적 검사 --------------------------------------------------
    statements_by_fragment: dict[str, list[str]] = {}
    directive_owner: dict[tuple, str] = {}
    refdes_owner: dict[str, str] = {}
    nets_by_fragment: dict[str, set[str]] = {}
    contributions: dict[str, dict] = {}
    directives_checked = 0
    includes_checked = 0
    end_lines: list[tuple[str, str]] = []  # (fragment name, code)

    for fragment in fragments:
        statements = _fragment_statements(fragment)
        statements_by_fragment[fragment.name] = statements
        definitions = _check_subckt_balance(fragment, statements)

        depth = 0
        for code in statements:
            lower = code.lower()
            if lower == ".end" or lower.startswith(".end "):
                end_lines.append((fragment.name, code))
                continue
            if depth == 0 and (lower.startswith(".include") or lower.startswith(".lib")):
                for path in _include_paths(code):
                    includes_checked += 1
                    if not os.path.isabs(path):
                        raise ComposeError(
                            f"fragment {fragment.name!r} includes {path!r}, which must be "
                            f"an absolute path: ngspice resolves a relative include "
                            f"against the CWD, which is not the composed deck's directory "
                            f"- a same-named file beside the CWD is read instead, "
                            f"silently, and every corner then runs the same models"
                        )
            for key in _directive_keys(code, depth):
                directives_checked += 1
                owner = directive_owner.get(key)
                if owner is not None:
                    raise ComposeError(
                        f"'{key[0]} {key[1] if len(key) > 1 else ''}'".rstrip(" '")
                        + f"' is declared by both fragment {owner!r} and "
                        f"{fragment.name!r}: which one wins differs per directive "
                        f"(.model/.option/.subckt keep the first, .param/.temp keep the "
                        f"last) and ngspice says nothing either way, so there is no safe "
                        f"fragment order - the collision itself is refused"
                    )
                directive_owner[key] = fragment.name
            # depth 는 지시자 검사 **뒤에** 움직인다 - `.subckt` 자기 자신은
            # 최상위 선언이고, 그 몸통이 depth 1 이다.
            if lower.startswith(".subckt") or lower.startswith(".macro"):
                depth += 1
                continue
            if lower.startswith(".ends") or lower.startswith(".eom"):
                depth -= 1
                continue

        # 소자와 넷은 이 저장소의 파서를 그대로 쓴다 - 위치 토큰의 마지막이
        # 값이고 나머지가 노드라는 규칙은 `signal_path`가 이미 딛고 선 것이라,
        # 여기서 따로 세는 것은 두 번째 규칙을 만드는 일이 된다.
        parsed = parse_netlist(fragment.text)
        nets: set[str] = set()
        for component in parsed.top_components:
            owner = refdes_owner.get(component.refdes)
            if owner is not None:
                raise ComposeError(
                    f"refdes {component.refdes!r} is contributed at the top level by both "
                    f"fragment {owner!r} and {fragment.name!r}: ngspice bails out with "
                    f"'device already exists' but reports only the composed deck's "
                    f"line number, so the fragment attribution exists only here"
                )
            refdes_owner[component.refdes] = fragment.name
            nets.update(component.nodes)
        components = len(parsed.top_components)

        nets_by_fragment[fragment.name] = nets
        contributions[fragment.name] = {
            "components": components,
            "nets": len(nets),
            "subckt_definitions": definitions,
        }

    # ---- `.end` ------------------------------------------------------------
    if len(end_lines) > 1:
        owners = sorted({name for name, _ in end_lines})
        raise ComposeError(
            f"more than one '.end' across fragments {owners}: ngspice-46 merges them into "
            f"one circuit, but HSPICE's behaviour is unverified and a truncating engine "
            f"would run every corner on a deck with no netlist in it"
        )
    if end_lines and end_lines[0][0] != fragments[-1].name:
        raise ComposeError(
            f"fragment {end_lines[0][0]!r} carries '.end' but is not the last fragment: "
            f"'.end' must be the last code line of the composed deck"
        )

    # ---- 이어붙이기 --------------------------------------------------------
    boundary_newlines = 0
    pieces: list[str] = []
    for fragment in fragments:
        text = fragment.text
        if text and not text.endswith("\n"):
            # 앞 조각이 주석 배너로 끝나면 뒤 조각의 첫 줄이 그 주석에
            # 흡수된다 - 사람이 쓴 조각의 흔한 모양이고 완전히 침묵한다.
            boundary_newlines += 1
            text += "\n"
        pieces.append(text)

    # 제목은 **언제나** 넣는다. 조각의 첫 줄이 제목으로 먹히는 것이 이
    # 모델에서 가장 조용한 실패이고, "안 넣어도 되는 경우"를 판정하려면
    # 조각 1의 첫 줄이 문장인지 주석인지를 넘어 그것이 제목으로 **의도된
    # 것인지**를 알아야 하는데 그것은 추측이다.
    body = "".join(pieces)
    if end_lines:
        end_appended = 0
    else:
        end_appended = 1
        body += ".end\n"

    text = f"* {title}\n{body}"

    # ---- 게이트가 아닌 보고 -------------------------------------------------
    net_owners: dict[str, list[str]] = defaultdict(list)
    for name, nets in nets_by_fragment.items():
        for net in nets:
            net_owners[net].append(name)
    shared_nets = [
        {"net": net, "fragments": owners}
        for net, owners in sorted(net_owners.items())
        if len(owners) > 1
    ]

    records = {
        "fragments": names,
        "title_inserted": 1,
        "end_appended": end_appended,
        "end_lines_found": len(end_lines),
        "boundary_newlines_inserted": boundary_newlines,
        "directives_checked": directives_checked,
        "top_refdes_checked": len(refdes_owner),
        "includes_checked": includes_checked,
    }
    report = {
        "shared_nets": shared_nets,
        "top_level_contributions": contributions,
    }
    return ComposedDeck(text=text, records=records, report=report)


def deck_for(tb, netlist_text: str, corner, *, nominal=None) -> ComposedDeck:
    """조합형 테스트벤치 하나의 덱을, 이 코너에 대해.

    **여기에는 정규식이 하나도 없다.** 코너는 슬롯에 **채워지는** 조각이지
    텍스트에서 찾아 고쳐 쓰는 것이 아니다. 오늘의 단일 파일 경로가 쓰는
    `pvt.render_corner_report`의 재작성 세 개 - include 교체, `.temp` 주입,
    `^Vdd` 전압 치환 - 는 이 경로에 존재하지 않는다: 코너 파일이 자기 안에서
    모델·온도·공급 전압을 정하고, 우리는 그 파일을 가리키는 `.include` 한
    줄을 자리에 넣을 뿐이다. 그 줄이 **셋**이고(`records`), 그래서 0건 매치가
    조용히 지나갈 자리가 없다.

    **코너 파일의 내용은 읽지 않는다.** 불투명한 파일이고, 안을 들여다보고
    축을 해석하는 것은 파일명에서 뜻을 읽는 것과 같은 부류의 추측이다.

    조각의 출처가 둘로 갈리는 것이 버전 관리 경계다: tunable 조각의 텍스트는
    **호출자가 주는 것**(버전 스택이 들고 있는 것)이고, 나머지는 디스크에서
    각자 자기 디렉터리 기준으로 `resolve_includes`를 거쳐 읽는다 -
    `benchmark_dir`처럼 canonical 하나의 디렉터리를 모든 조각에 쓰면 조각이
    여러 디렉터리에 있을 때 그 유도가 성립하지 않는다.

    `corner`가 `None`이면 nominal - 조합 모델에는 "렌더링을 거치지 않은 덱"이
    없으므로, 그 자리는 스펙이 **선언한** nominal 코너가 채운다."""
    if tb.fragments is None:
        raise ComposeError(f"testbench {tb.name!r} is not composed")

    point = corner if corner is not None else nominal
    fragments: list[Fragment] = []
    slot_filled = 0
    for index, ref in enumerate(tb.fragments):
        if ref.kind == "corner_slot":
            if point is None:
                raise ComposeError(
                    f"testbench {tb.name!r} has a corner_slot but no corner to fill it "
                    f"with (and no declared nominal corner): composing without it would "
                    f"run this point on whatever the other fragments happen to set"
                )
            if point.payload is None:
                raise ComposeError(
                    f"corner {point.corner_id!r} carries no payload, so the corner_slot of "
                    f"testbench {tb.name!r} cannot be filled: skipping it silently would "
                    f"run this corner on another corner's deck under this corner's name"
                )
            fragments.append(
                Fragment(name="<corner>", text=f'.include "{point.payload}"\n')
            )
            slot_filled += 1
            continue
        if ref.tunable:
            fragments.append(Fragment(name=f"{index}:{ref.path}", text=netlist_text))
            continue
        with open(ref.path) as f:
            text = f.read()
        fragments.append(
            Fragment(name=f"{index}:{ref.path}", text=resolve_includes(text, os.path.dirname(ref.path)))
        )

    deck = compose(fragments, title=f"{tb.name} (composed)")
    return replace(
        deck,
        records={
            **deck.records,
            "corner_slot_filled": slot_filled,
            "corner": point.corner_id if point is not None else None,
        },
    )
