"""LLM이 돌려준 control block을 실행 전에 판정하는 게이트의 짝 테스트.

이 게이트가 왜 있는지는 `control_block_gate.py`의 모듈 독스트링에 적혀 있다.
여기서 고정하는 것은 두 가지다: **실행 표면**(ngspice `.control`의 `shell`은
임의 명령을 돌린다)과 **측정 무결성**(측정값을 보고하는 에이전트가 `meas`
줄 자체를 다시 쓸 수 있다).
"""

import glob
import os

import pytest
import yaml

from analogcoder.control_block_gate import (
    ALLOWED_COMMANDS,
    GATE_NAME,
    check_control_block,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# two_stage_opamp의 ac_loop_gain 테스트벤치 원문. 아래 여러 테스트의 기준이다.
REFERENCE = """.control
set units=degrees
ac dec 20 1 100meg
meas ac gain_db find vdb(vout) at=1
meas ac ugbw_hz when vdb(vout)=0
meas ac phase_margin_deg find vp(vout) when vdb(vout)=0
.endc"""


def _benchmark_control_blocks() -> list[tuple[str, str, str]]:
    """(spec 경로, 테스트벤치 이름, control block) 전부."""
    blocks = []
    for spec_path in sorted(glob.glob(os.path.join(_REPO_ROOT, "benchmarks", "*", "spec*.yaml"))):
        document = yaml.safe_load(open(spec_path))
        for testbench in document.get("testbenches", []):
            blocks.append((spec_path, testbench["name"], testbench["control_block"]))
    return blocks


# --------------------------------------------------------------------------
# 실행 표면
# --------------------------------------------------------------------------


def test_a_shell_command_is_rejected():
    """감사가 실증한 그 덱이다 - `.control` 안의 `shell`은 임의 명령을 돌리고
    시뮬레이션은 정상 종료한다. 여기서는 **ngspice를 부르지 않는다**: 게이트가
    그 전에 막아야 하고, 막는다면 파일은 만들어질 기회조차 없다."""
    candidate = REFERENCE.replace(
        ".endc", "shell touch /tmp/analogcoder-control-block-gate-probe\n.endc"
    )

    verdict = check_control_block(candidate, REFERENCE)

    assert verdict.accepted is False
    assert verdict.reason == "command_not_allowed"
    assert "shell" in verdict.detail


def test_the_audits_probe_deck_is_stopped_before_it_becomes_a_deck(tmp_path):
    """감사가 실제로 돌린 덱을 그대로 조립해서 게이트에 먹인다.

    `NgspiceBackend.run`이 만드는 것과 같은 모양(`body + control_block +
    ".end"`)으로 파일까지 쓰되 **ngspice를 부르지 않는다** - 게이트가 그 전에
    막아야 하는 것이 이 테스트의 내용이고, 부르면 그 주장이 아니라 정반대의
    것을 실증하게 된다. 덱은 디스크에 남겨 두어, 막힌 것이 "덱을 못 만들어서"가
    아님을 분명히 한다.
    """
    marker = tmp_path / "PWNED"
    reference = ".control\nop\n.endc"
    candidate = f".control\nop\nshell touch {marker}\n.endc"
    deck = tmp_path / "deck.cir"
    deck.write_text(
        "* rce probe\nV1 a 0 DC 1\nR1 a 0 1k\n" + candidate + "\n.end\n"
    )

    verdict = check_control_block(candidate, reference)

    assert deck.exists(), "덱 자체는 조립됐다 - 막은 것은 게이트다"
    assert verdict.accepted is False
    assert verdict.reason == "command_not_allowed"
    assert not marker.exists()


@pytest.mark.parametrize(
    "command_line",
    [
        "shell touch /tmp/pwned",
        "sh touch /tmp/pwned",
        "system touch /tmp/pwned",
        "source /tmp/evil.cir",
        "write /tmp/out.raw",
        "wrdata /tmp/out.csv v(vout)",
        "cd /",
        "echo gain_db = 999.0",
        ".include /etc/passwd",
        ".end",
        ".temp 100",
    ],
)
def test_commands_outside_the_allowlist_are_rejected(command_line):
    """`echo`가 여기 있는 이유는 RCE가 아니라 측정 무결성이다:
    `NgspiceBackend`는 로그에서 `^name = value$`를 측정값으로 읽으므로
    `echo gain_db = 999.0` 한 줄이 측정값을 위조한다.

    `.end`는 ngspice.py가 덱 끝에 스스로 붙이는 것이고, 컨트롤 블록 안의
    `.end`는 그 뒤 전부를 조용히 삼킨다. `.temp`는 코너 렌더링이 하는 일을
    가로챈다."""
    candidate = REFERENCE.replace(".endc", command_line + "\n.endc")

    verdict = check_control_block(candidate, REFERENCE)

    assert verdict.accepted is False
    assert verdict.reason == "command_not_allowed"
    assert command_line.split()[0] in verdict.detail


def test_the_allowlist_does_not_contain_any_of_the_commands_the_audit_named():
    """허용 목록이 나중에 넓혀질 때 이 다섯이 조용히 합류하는 것을 막는다."""
    assert ALLOWED_COMMANDS.isdisjoint(
        {"shell", "sh", "system", "source", "write", "wrdata", "echo", "cd", "spawn"}
    )


# --------------------------------------------------------------------------
# 측정 무결성
# --------------------------------------------------------------------------


def test_rewriting_a_meas_line_is_rejected():
    """회로를 안 고치고 **측정을 고치는** 경로. `check_stimulus_untouched`가
    막으려던 `Vin AC 1 -> AC 100`과 정확히 같은 부류다."""
    candidate = REFERENCE.replace("find vdb(vout) at=1", "find vdb(vout) at=1e6")

    verdict = check_control_block(candidate, REFERENCE)

    assert verdict.accepted is False
    assert verdict.reason == "measurements_altered"
    assert "gain_db" in verdict.detail


def test_deleting_a_meas_line_is_rejected():
    candidate = "\n".join(
        line for line in REFERENCE.splitlines() if "phase_margin_deg" not in line
    )

    verdict = check_control_block(candidate, REFERENCE)

    assert verdict.accepted is False
    assert verdict.reason == "measurements_altered"


def test_adding_a_let_that_could_feed_a_forged_measurement_is_rejected():
    """`print`은 허용 명령이지만 `let gain_db = 999` + `print gain_db`의 조합이
    측정값을 위조한다. 그 조합의 앞 절반이 여기서 끊긴다."""
    candidate = REFERENCE.replace(
        ".endc", "let gain_db = 999\nprint gain_db\n.endc"
    )

    verdict = check_control_block(candidate, REFERENCE)

    assert verdict.accepted is False
    assert verdict.reason == "measurements_altered"


def test_reordering_measurement_lines_is_rejected():
    """`control_block.measurement_nets`가 순서에 의존한다고 명시한다 -
    `let`은 재정의되고, 각 재정의 직후의 `meas`가 그 시점 값을 참조한다."""
    lines = REFERENCE.splitlines()
    lines[3], lines[4] = lines[4], lines[3]

    verdict = check_control_block("\n".join(lines), REFERENCE)

    assert verdict.accepted is False
    assert verdict.reason == "measurements_altered"


def test_changing_an_alter_line_is_rejected():
    """`alter`는 부품 값을 바꾼다 - 시스템 프롬프트가 "Never modify component
    values"라고 말하는 바로 그것이다. 벤치마크에서 56줄이 정당하게 쓰므로
    명령 자체는 허용되지만, **원문에 없던 값으로 바꾸는 것**은 막힌다."""
    reference = REFERENCE.replace("set units=degrees", "alter @r1[resistance]=1k")
    candidate = reference.replace("=1k", "=1meg")

    verdict = check_control_block(candidate, reference)

    assert verdict.accepted is False
    assert verdict.reason == "measurements_altered"


def test_deleting_a_set_line_is_rejected():
    """`set units=degrees`가 빠지면 `vp()`가 라디안을 내고 phase_margin_deg가
    조용히 다른 양이 된다. `set`은 수렴 노브가 아니라 출력 형식 명령이므로
    프롬프트가 허가한 `.options` 편집에 포함되지 않는다."""
    candidate = REFERENCE.replace("set units=degrees\n", "")

    verdict = check_control_block(candidate, REFERENCE)

    assert verdict.accepted is False
    assert verdict.reason == "control_block_altered"


# --------------------------------------------------------------------------
# 허가된 수렴 재시도는 통과해야 한다
# --------------------------------------------------------------------------


def test_adding_an_options_line_for_convergence_is_accepted():
    """시스템 프롬프트가 명시적으로 허가하는 유일한 편집이다."""
    candidate = REFERENCE.replace(
        ".control", ".control\n.options gmin=1e-10 method=gear"
    )

    verdict = check_control_block(candidate, REFERENCE)

    assert verdict.accepted is True
    assert verdict.reason is None


def test_changing_an_existing_options_value_is_accepted():
    reference = REFERENCE.replace(".control", ".control\n.options reltol=1e-3")
    candidate = reference.replace("reltol=1e-3", "reltol=1e-4")

    verdict = check_control_block(candidate, reference)

    assert verdict.accepted is True


def test_whitespace_and_case_only_differences_are_accepted():
    """ngspice는 이 이름들에 대소문자를 가리지 않는다. 재포매팅만으로 거부하면
    게이트가 정당한 재시도를 잡아먹는다."""
    candidate = "\n".join("   " + line.upper() + "  " for line in REFERENCE.splitlines())

    verdict = check_control_block(candidate, REFERENCE)

    assert verdict.accepted is True


def test_an_identical_control_block_is_accepted():
    verdict = check_control_block(REFERENCE, REFERENCE)

    assert verdict.accepted is True


# --------------------------------------------------------------------------
# S2: 출하된 컨트롤 블록 42개 전수
# --------------------------------------------------------------------------


def test_every_shipped_benchmark_control_block_is_accepted():
    """게이트가 정상 동작을 막으면 불채택이다. 스펙 12개 · 컨트롤 블록 42개
    전부를 자기 자신에 대해(에이전트가 원문을 그대로 되돌려준 경우) 먹인다."""
    blocks = _benchmark_control_blocks()

    assert len(blocks) == 42, f"컨트롤 블록 수가 42에서 {len(blocks)}로 바뀌었다"

    rejected = [
        (spec, name, check_control_block(block, block).detail)
        for spec, name, block in blocks
        if not check_control_block(block, block).accepted
    ]
    assert rejected == []


def test_the_shipped_control_blocks_use_only_ten_distinct_commands():
    """허용 목록이 왜 가능한가에 대한 근거를 고정한다 - 실제 어휘가 이만큼
    작기 때문이다. 이 집합이 커지면 허용 목록도 같이 커져야 하고, 이 테스트가
    그때 그것을 알려 준다."""
    commands: set[str] = set()
    for _, _, block in _benchmark_control_blocks():
        for raw in block.splitlines():
            line = raw.strip()
            if line:
                commands.add(line.split()[0].lower())

    assert commands == {
        ".control",
        ".endc",
        "ac",
        "alter",
        "dc",
        "let",
        "meas",
        "print",
        "set",
        "tran",
    }
    assert commands <= ALLOWED_COMMANDS


# --------------------------------------------------------------------------
# 기록 - 아무것도 안 했을 때 어떻게 보이는가
# --------------------------------------------------------------------------


def test_an_accepted_verdict_still_carries_evidence_that_the_gate_ran():
    """"검사했고 통과"와 "검사가 사라졌다"가 구별돼야 한다. 통과한 판정도
    무엇을 몇 줄 봤는지를 싣는다 - `lines_checked=0`이면 게이트가 빈 문자열을
    봤다는 뜻이고, 키 자체가 없으면 게이트가 안 돌았다는 뜻이다."""
    verdict = check_control_block(REFERENCE, REFERENCE)
    event = verdict.as_event()

    assert event["gate"] == GATE_NAME
    assert event["accepted"] is True
    assert event["reason"] is None
    assert event["lines_checked"] == 7
    assert event["measurement_lines"] == 3
    assert event["commands"] == [".control", ".endc", "ac", "meas", "set"]


def test_the_event_names_its_own_gate_so_two_gates_are_not_confused_after_the_fact():
    """CLAUDE.md가 기록하는 실수를 반복하지 않는다: `area_check`와
    `refdes_check`가 같은 `feedback` 키를 써서 사후에 구별이 안 된다."""
    rejected = check_control_block(
        REFERENCE.replace(".endc", "shell id\n.endc"), REFERENCE
    ).as_event()

    assert rejected["gate"] == GATE_NAME
    assert rejected["reason"] == "command_not_allowed"
    assert rejected["detail"]


def test_an_empty_control_block_is_visible_as_such():
    """빈 블록은 "게이트가 아무것도 못 봤다"이지 "통과"가 아니다 - 원문의
    meas 줄이 사라졌으므로 거부되고, lines_checked가 0으로 남아 그 사실이
    기록에 보인다."""
    verdict = check_control_block("", REFERENCE)

    assert verdict.accepted is False
    assert verdict.as_event()["lines_checked"] == 0


def test_a_reference_and_candidate_that_are_both_empty_is_accepted_and_says_so():
    verdict = check_control_block("", "")

    assert verdict.accepted is True
    assert verdict.as_event()["lines_checked"] == 0
    assert verdict.as_event()["commands"] == []


def test_comment_only_lines_are_not_commands():
    candidate = REFERENCE.replace(".control", ".control\n* just a note")

    verdict = check_control_block(candidate, REFERENCE)

    assert verdict.accepted is True
