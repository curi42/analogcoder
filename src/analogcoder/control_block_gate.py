"""LLM이 돌려준 ngspice control block을 **실행 전에** 판정하는 결정론적 게이트.

## 왜 있는가

두 가지가 한 문자열에 걸려 있다.

**하나, 실행 표면.** ngspice `.control` 블록은 `shell` 명령을 실행한다. 감사가
실증했다 - `shell touch <파일>`을 담은 덱은 파일을 만들고 시뮬레이션은 정상
종료한다. 그리고 `simulator_agent`의 도구 핸들러는 LLM이 준 문자열을 아무
검증 없이 `SimulatorBackend.run`으로 넘겼다. 넷리스트 *파라미터* 변경에는
게이트가 넷(area/refdes/param/stimulus) 있는데, 같은 덱에 이어 붙는 더
강력한 이 문자열에는 0개였다. 위협 모델이 가설이 아닌 이유는 CLAUDE.md 자신이
`--agent-backend openai-compatible --llm-base-url http://localhost:11434/v1`로
임의의 로컬 서버를 붙이라고 권하고, 넷리스트 텍스트가 프롬프트에 그대로
들어가 덱 주석 한 줄이 인젝션 벡터가 되기 때문이다.

**둘, 측정 무결성.** 측정값을 보고하는 에이전트가 `meas` 줄 자체를 다시 쓸 수
있고 judge는 그 값을 판정한다. 회로를 안 고치고 **측정을 고치는** 경로이며,
`check_stimulus_untouched`가 막으려던 `Vin AC 1 -> AC 100` 사건과 정확히 같은
부류다. 게이트만 없었다.

## 왜 차단 목록이 아니라 허용 목록인가

차단 목록은 ngspice에 새 명령이 생기면 **조용히 뚫린다** - 이 저장소가 아홉
번 값을 치른 "조용히 무력한 게이트"와 같은 모양이다. 허용 목록의 반대 위험은
정당한 컨트롤 블록을 막는 것인데, 그것이 실제로 위험한지는 세어 보면 답이
나온다: 벤치마크 **스펙 12개 · 컨트롤 블록 42개**가 쓰는 명령은
`.control`/`.endc`/`ac`/`alter`/`dc`/`let`/`meas`/`print`/`set`/`tran`
**열 개**뿐이다(`test_control_block_gate.py`가 그 집합을 고정한다). ngspice의
명령 표면은 그보다 한 자릿수 크고 그 안에 `shell`/`system`/`source`/`write`/
`wrdata`/`cd`/`spawn`이 들어 있다. 그래서 허용 목록이 가능하고, 그쪽이 옳다.

허용 목록의 구성 규칙은 하나다: **출하된 42개가 쓰는 것 ∪ 시스템 프롬프트가
명시적으로 허가한 수렴 노브(`.options` 계열) ∪ 그 명령들의 표준 dot/비-dot
쌍**. 이 규칙 밖에서 넣은 항목은 `op`/`.op` 하나뿐이고, 그것은 "DC 동작점은
`ac`/`dc`/`tran`과 같은 범주의 해석 명령"이라는 ngspice 문서상의 사실에
근거한다 - 출하된 블록 중에는 쓰는 것이 없으므로 여기에 적어 둔다. 그 밖의
명령이 필요해지면 게이트는 **시끄럽게** 거부하고 사람이 목록을 넓힌다.
목록을 넓히는 것이 `shell`을 조용히 통과시키는 것보다 낫다.

## 무엇을 보존하라고 요구하는가

시뮬레이터 에이전트가 컨트롤 블록을 **수렴시키는** 것은 설계된 기능이다.
그래서 "아무것도 못 바꾼다"는 규칙은 기능을 죽인다. 시스템 프롬프트가 허가한
것은 정확히 `.options` 부분의 조정이므로, 게이트도 거기에 맞춘다:

- `option`/`options`/`.option`/`.options` 줄은 **자유롭게 추가·삭제·수정**할 수
  있다. 수렴 재시도가 하는 일이 이것이다.
- 나머지 줄은 **순서까지 그대로**여야 한다(공백·대소문자 차이는 무시).

`set`이 후자에 있는 것은 의도적이다. `set units=degrees`가 빠지면 `vp()`가
라디안을 내고 `phase_margin_deg`가 조용히 다른 양이 된다 - `set`은 수렴
노브가 아니라 출력 형식 명령이고, 프롬프트가 허가한 `.options`도 아니다.
`alter`도 마찬가지로 보존을 요구한다: 프롬프트의 "Never modify component
values"가 가리키는 바로 그 명령이고, 벤치마크의 56줄은 사람이 쓴 테스트벤치
스윕이므로 원문과 같기만 하면 통과한다.

순서까지 요구하는 근거는 `control_block.measurement_nets`의 독스트링이다 -
`let tmag = ...`이 여러 번 재정의되고 각 재정의 직후의 `meas`가 그 시점 값을
참조하므로, 순서가 바뀌면 measurement가 다른 넷을 가리킨다.

이 규칙은 프롬프트보다 **느슨하다**(프롬프트는 `.options`만 만지라고 하고,
게이트는 그 밖에 아무 것도 못 만지게 한다 - 즉 게이트가 허용하는 집합이
프롬프트가 허용하는 집합을 포함한다). CLAUDE.md가 잠근 방향이 그것이다:
프롬프트가 게이트보다 엄격한 쪽이 안전한 방향이다.

## 아무것도 안 잡았을 때 어떻게 보이는가

판정은 **통과일 때도** 근거를 싣는다(`as_event()`): 본 줄 수, 본 명령 목록,
measurement 줄 수. `lines_checked=0`은 "게이트가 빈 문자열을 봤다"이고, 키
자체의 부재는 "게이트가 안 돌았다"이다. 이 둘과 "봤고 통과"가 서로 다르게
보여야 한다는 것이 이 저장소가 아홉 번 값을 치른 규칙이다.

`gate` 필드를 판정 자체가 싣는 것도 같은 이유다 - `area_check`와
`refdes_check`가 같은 `feedback` 키를 써서 사후에 어느 게이트가 냈는지
구별되지 않는다는 것을 CLAUDE.md가 기록한다. 이벤트 이름에 기대지 않는다.
"""

from dataclasses import dataclass

GATE_NAME = "control_block"

# 수렴 재시도가 자유롭게 만질 수 있는 유일한 부류. 시스템 프롬프트가
# "adjusting the .options portion of the control block"이라고 허가한 것이다.
#
# **닷 형만이다. 비-닷 형 `option`/`options`는 실증된 임의 명령 실행
# 벡터였다.** 이 줄들은 허용 목록 안이면서 줄 보존 비교에서 **제외**되는
# 유일한 자유 표면이고, ngspice의 `cp` 셸은 비-닷 형 option 줄에서 역따옴표
# 치환을 수행한다. 실제 ngspice로 종단 실증됐다:
#
#     option  ... `touch F`  -> 실행됨        .option  ... `touch F`  -> 실행 안 됨
#     options ... `touch F`  -> 실행됨        .options ... `touch F`  -> 실행 안 됨
#
# 게이트는 `accepted=True`를 냈고 파일이 생겼으며, 산출물만으로는 무해한
# 블록과 구별되지 않았다. 반환값 게이트를 타면 `corner_sim`이 그 문자열을
# 코너마다 재사용하므로 45코너 스펙에서 45회 실행된다.
#
# 좁히는 비용은 **0**이다: 출하된 42개 제어 블록에 option 계열 줄이 형태
# 불문 0개다(`test_no_shipped_control_block_uses_a_non_dot_option_line`이
# 그 사실을 못박는다). 프롬프트와 이 모듈의 독스트링은 줄곧 닷 형만
# 말했으므로, 이것은 규칙 변경이 아니라 코드를 문서에 맞추는 것이다.
OPTION_COMMANDS = frozenset({".option", ".options"})

# 자유 표면 안에서 셸 치환을 일으킬 수 있는 문자열. **닷 형만 남긴 것으로는
# 부족하다**: 위 실증에서 시험된 벡터는 역따옴표 / `$(...)` / `;` 셋뿐이고,
# 그것은 전수 조사가 아니다. "오늘의 ngspice가 닷 형에서는 치환하지 않는다"에
# 기대는 것과, 치환 문자를 아예 안 받는 것은 다른 강도다.
_SUBSTITUTION_MARKERS = ("`", "$(", "${")

# 측정 장치. 여기 걸리는 불일치는 `control_block_altered`가 아니라
# `measurements_altered`로 보고된다 - 둘은 서로 다른 사실이다.
MEASUREMENT_COMMANDS = frozenset(
    {"meas", ".meas", "measure", ".measure", "let", "alter"}
)

_STRUCTURE_COMMANDS = frozenset({".control", ".endc"})
_ANALYSIS_COMMANDS = frozenset({"ac", "dc", "tran", "op", ".ac", ".dc", ".tran", ".op"})
_OUTPUT_COMMANDS = frozenset({"print"})
_SETTING_COMMANDS = frozenset({"set"})

ALLOWED_COMMANDS = (
    OPTION_COMMANDS
    | MEASUREMENT_COMMANDS
    | _STRUCTURE_COMMANDS
    | _ANALYSIS_COMMANDS
    | _OUTPUT_COMMANDS
    | _SETTING_COMMANDS
)

# 거부 사유 코드. 세 값은 서로 다른 사실이고, 하나로 접으면 사후에 갈리지
# 않는다("게이트가 모르는 명령을 봤다" / "측정 장치가 바뀌었다" / "그 밖의
# 줄이 바뀌었다").
REASON_COMMAND_NOT_ALLOWED = "command_not_allowed"
REASON_MEASUREMENTS_ALTERED = "measurements_altered"
REASON_CONTROL_BLOCK_ALTERED = "control_block_altered"
# 네 번째. "게이트가 모르는 명령을 봤다"와 "허가한 자유 표면 안에 셸 치환
# 문자가 있다"는 서로 다른 사실이고, 후자는 사고가 아니라 **시도**다.
REASON_SUBSTITUTION_IN_OPTION = "substitution_in_option"


@dataclass(frozen=True)
class ControlBlockVerdict:
    accepted: bool
    reason: str | None
    detail: str | None
    commands: tuple[str, ...]
    lines_checked: int
    measurement_lines: int

    def as_event(self) -> dict:
        """`history.jsonl`에 그대로 실리는 모양. 통과·거부 어느 쪽이든 같은
        키 집합을 낸다 - 거부일 때만 무언가가 나타나면 "검사했고 통과"와
        "검사가 사라졌다"가 같아 보인다."""
        return {
            "gate": GATE_NAME,
            "accepted": self.accepted,
            "reason": self.reason,
            "detail": self.detail,
            "commands": list(self.commands),
            "lines_checked": self.lines_checked,
            "measurement_lines": self.measurement_lines,
        }


@dataclass(frozen=True)
class _Statement:
    command: str  # 소문자화한 첫 토큰
    key: str  # 비교용으로 정규화한 줄 전체(공백 축약 + 소문자)
    text: str  # 사람이 읽을 원문


def _statements(text: str) -> list[_Statement]:
    """실행되는 줄만. 빈 줄과 주석 줄(`*`, `$`)은 명령이 아니다.

    여기서 `str.split()`을 쓰는 것은 CLAUDE.md의 금지에 걸리지 않는다. 그
    규칙은 **부품 줄**에 대한 것이고 `split_tokens`가 지키는 것은
    `W='wn * 2'`처럼 공백을 품은 파라미터 값이다. 컨트롤 블록에는 그런 토큰이
    없고(출하된 42개 전부 확인), 필요한 것은 첫 토큰과 공백 축약뿐이다.
    """
    statements = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line[0] in "*$":
            continue
        collapsed = " ".join(line.split())
        statements.append(
            _Statement(
                command=collapsed.split()[0].lower(),
                key=collapsed.lower(),
                text=collapsed,
            )
        )
    return statements


def _reason_for_mismatch(
    candidate: _Statement | None, reference: _Statement | None
) -> str:
    """불일치가 측정 장치를 건드렸는가. 어느 쪽이든 걸리면 측정 무결성 사유다."""
    for statement in (candidate, reference):
        if statement is not None and statement.command in MEASUREMENT_COMMANDS:
            return REASON_MEASUREMENTS_ALTERED
    return REASON_CONTROL_BLOCK_ALTERED


def check_control_block(candidate: str, reference: str) -> ControlBlockVerdict:
    """`candidate`를 시뮬레이터에 넘겨도 되는가.

    `reference`는 사람이 쓴 스펙의 컨트롤 블록이다 - 신뢰 경계의 안쪽이고,
    이 판정의 기준선이다. 두 문자열이 같으면 항상 통과한다(출하된 42개가
    그 경우이며 `test_every_shipped_benchmark_control_block_is_accepted`가
    전수로 고정한다).
    """
    candidate_statements = _statements(candidate)
    commands = tuple(sorted({statement.command for statement in candidate_statements}))
    measurement_lines = sum(
        1 for statement in candidate_statements if statement.command in MEASUREMENT_COMMANDS
    )

    def verdict(reason: str | None, detail: str | None) -> ControlBlockVerdict:
        return ControlBlockVerdict(
            accepted=reason is None,
            reason=reason,
            detail=detail,
            commands=commands,
            lines_checked=len(candidate_statements),
            measurement_lines=measurement_lines,
        )

    for statement in candidate_statements:
        if statement.command not in ALLOWED_COMMANDS:
            return verdict(
                REASON_COMMAND_NOT_ALLOWED,
                f"'{statement.command}' is not an allowed ngspice control command "
                f"(line: {statement.text!r})",
            )

    # 자유 표면 안의 셸 치환. **줄 보존 비교 앞에서** 본다 - option 줄은
    # 그 비교에서 제외되는 유일한 부류이므로, 여기서 안 잡으면 아무 데서도
    # 안 잡힌다. 그것이 실증된 벡터의 정확한 구조였다.
    for statement in candidate_statements:
        if statement.command not in OPTION_COMMANDS:
            continue
        for marker in _SUBSTITUTION_MARKERS:
            if marker in statement.text:
                return verdict(
                    REASON_SUBSTITUTION_IN_OPTION,
                    f"an option line carries the shell substitution marker {marker!r}, "
                    f"which the free surface must never receive (line: {statement.text!r})",
                )

    # `.options` 계열만 빼고 나머지는 순서까지 그대로여야 한다.
    kept_candidate = [s for s in candidate_statements if s.command not in OPTION_COMMANDS]
    kept_reference = [
        s for s in _statements(reference) if s.command not in OPTION_COMMANDS
    ]

    for index in range(max(len(kept_candidate), len(kept_reference))):
        mine = kept_candidate[index] if index < len(kept_candidate) else None
        theirs = kept_reference[index] if index < len(kept_reference) else None
        if mine is not None and theirs is not None and mine.key == theirs.key:
            continue
        return verdict(
            _reason_for_mismatch(mine, theirs),
            "the control block may only add, change or drop .options lines; "
            f"expected {theirs.text!r} but found {mine.text!r}"
            if mine is not None and theirs is not None
            else (
                f"a line was added: {mine.text!r}"
                if mine is not None
                else f"a line was dropped: {theirs.text!r}"
            ),
        )

    return verdict(None, None)
