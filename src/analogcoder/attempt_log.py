"""튜닝 시도 기록 - 한 항목 = 한 컴포넌트 변경.

제안 단위로 묶으면 "어느 노브가 무엇을 했는가"를 다시 못 읽어 내는데,
그것이 튜너가 알아야 하는 유일한 것이다.
"""

from dataclasses import dataclass

# 프롬프트에 넣는 항목 수 상한. 한 제안이 여러 변경이고 재시도가 최대
# MAX_TUNING_RETRIES이므로 이터레이션당 항목이 빠르게 늘어난다.
ATTEMPT_RENDER_LIMIT = 30


@dataclass(frozen=True)
class Attempt:
    outer_iter: int
    retry: int
    refdes: str
    param: str
    old_value: str
    new_value: str
    outcome: str  # "kept" | "rolled_back" | "rejected"
    reason: str | None = None  # 거부일 때만: 사유 코드
    detail: str | None = None  # 거부일 때만: 게이트가 낸 피드백
    # dict이 아니라 쌍의 튜플인 이유: frozen 안의 dict은 여전히 바뀌므로
    # frozen이 약속하는 것을 지키지 않는다. 렌더러는 어차피 순회한다.
    deltas: tuple[tuple[str, float], ...] = ()
    regressed: tuple[str, ...] = ()


def deltas_between(before: dict, after: dict) -> tuple[tuple[str, float], ...]:
    """양쪽 judge 결과에 **다 있는** 기준만 변화량을 낸다.

    한쪽에만 있는 이름은 빠진다 - 없는 측정을 0으로 읽는 것은
    corner_allowances에서 이미 값을 치른 모양이다.
    """
    before_by = {c["name"]: c for c in before["criteria"]}
    return tuple(
        (c["name"], c["actual"] - before_by[c["name"]]["actual"])
        for c in after["criteria"]
        if c["name"] in before_by
    )


def regressed_between(before: dict, after: dict) -> tuple[str, ...]:
    """통과 -> 실패로 뒤집힌 기준만.

    verify_post의 regressed_criteria를 쓰지 않는 이유: 그것은 스키마가 붙은
    필드이지만 여전히 LLM이 만든 주장이고, 이 두 줄은 judge가 낸 숫자에서
    나오는 사실이다. 둘이 갈라지면 사실이 이긴다.
    """
    before_by = {c["name"]: c for c in before["criteria"]}
    return tuple(
        c["name"]
        for c in after["criteria"]
        if c["name"] in before_by and before_by[c["name"]]["pass"] and not c["pass"]
    )


def render_attempts(attempts, limit: int = ATTEMPT_RENDER_LIMIT) -> str:
    """시도를 사실 목록으로 그린다. 항목이 없으면 빈 문자열 - 빈 표를 그리면
    튜너에게 '시도가 없었다'가 아니라 '무언가 있었다'로 읽힌다."""
    if not attempts:
        return ""
    shown = list(attempts)[-limit:]
    dropped = len(attempts) - len(shown)
    lines = ["Past attempts this run:"]
    if dropped:
        lines.append(f"  ({dropped} earlier attempt(s) omitted, {len(shown)} most recent shown)")
    for a in shown:
        lines.append(
            f"  iter {a.outer_iter}.{a.retry}  {a.refdes} {a.param}  "
            f"{a.old_value} -> {a.new_value}  {a.outcome}{_tail(a)}"
        )
    return "\n".join(lines)


def _tail(a: Attempt) -> str:
    if a.outcome == "rejected":
        return f"  {a.reason}: {a.detail}"
    parts = []
    if a.deltas:
        parts.append(", ".join(f"{name} {value:+.4g}" for name, value in a.deltas))
    if a.regressed:
        parts.append(f"regressed [{', '.join(a.regressed)}]")
    return ("  " + "; ".join(parts)) if parts else ""
