# 튜닝 시도 기록 (D1) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 같은 런 안의 튜닝 시도가 **무엇이 얼마나 움직였는지**와 **게이트가 무엇을 막았는지**를 담아 다음 제안까지 살아남게 만든다.

**Architecture:** 순수 모듈 하나(`attempt_log.py`)와 오케스트레이터 배선. LLM은 추가되지 않고, 에이전트 호출 수도 그대로다. 튜너에게 가는 것은 dict 덤프가 아니라 렌더된 텍스트가 된다.

**Tech Stack:** Python 3, dataclasses, pytest, ngspice(측정 태스크만)

**설계 문서:** `docs/superpowers/specs/2026-07-28-tuning-attempt-record-design.md`. 세 개의 코드 사실(런 내 히스토리는 이미 전달됨 / 측정값 없음 / 거부는 이터레이션을 못 넘음)이 거기 있다 — **다시 조사하지 말 것.**

## Global Constraints

- **회귀 기준선:** `.venv/bin/python -m pytest -m "not slow" -q` → 904 passed, 2 skipped, 6 deselected. 태스크마다 이 값을 유지하거나 늘린다.
- **`verify_post`의 `regressed_criteria`를 쓰지 않는다.** 회귀는 judge 결과의 `pass` 뒤집힘에서 결정론적으로 계산한다. `recommendation`(`keep`/`rollback`)은 계속 흐름을 결정한다 — 바뀌는 것은 "무엇이 회귀했는가"를 누가 말하느냐뿐이다.
- **사유 코드는 게이트 함수의 반환값에서 받는다. 이벤트를 다시 파싱하지 않는다.** `area_check`와 `refdes_check`가 둘 다 `feedback` 키를 쓰므로 이벤트 스트림에서는 어느 게이트였는지 복원할 수 없다.
- **기존 이벤트를 지우거나 바꾸지 않는다.** `tuning_proposal`, `area_check`, `refdes_check`, `param_check`, `stimulus_check`, `verify_pre`, `verify_post`는 그대로다. `attempt_log`는 파생 요약이지 대체가 아니다.
- **`orchestrator.py:267`의 재시도 루프는 토폴로지 제안용이다. 건드리지 않는다.** 이 계획이 다루는 것은 `orchestrator.py:443`의 파라미터 튜닝 재시도 루프뿐이다.
- **히스토리를 제한으로 서술하지 않는다.** 프롬프트에 재제안 금지 문장을 넣지 않는다 — 초점을 필터로 바꿨다가 값을 치른 것과 같은 오류다.
- 새 테스트마다 "이 테스트는 어떤 변형을 잡는가"를 답하고 **변형을 실제로 적용해 확인한다.**

---

### Task 1: `attempt_log.py` — 항목, 델타, 회귀, 렌더러

**Files:**
- Create: `src/analogcoder/attempt_log.py`
- Test: `tests/unit/test_attempt_log.py`

**Interfaces:**
- Consumes: 없음(순수 모듈). judge 결과 dict은 `JUDGE_SCHEMA` 모양 — `{"overall_pass": bool, "criteria": [{"name","target","actual","pass","margin"}], "summary": str}`.
- Produces: `Attempt`, `deltas_between`, `regressed_between`, `render_attempts`, `ATTEMPT_RENDER_LIMIT` — Task 2·3·4가 전부 쓴다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/unit/test_attempt_log.py`:

```python
from analogcoder.attempt_log import (
    ATTEMPT_RENDER_LIMIT,
    Attempt,
    deltas_between,
    regressed_between,
    render_attempts,
)


def judge(*criteria):
    return {
        "overall_pass": all(c["pass"] for c in criteria),
        "criteria": list(criteria),
        "summary": "",
    }


def crit(name, actual, passing):
    return {"name": name, "target": ">=0", "actual": actual, "pass": passing, "margin": 0.0}


def applied(refdes="TRIMAMP.XRz", param="l", outcome="kept", **kw):
    base = dict(
        outer_iter=1, retry=1, refdes=refdes, param=param,
        old_value="15", new_value="45", outcome=outcome,
    )
    base.update(kw)
    return Attempt(**base)


def test_deltas_cover_only_criteria_present_in_both_judgements():
    """어느 변형을 잡는가: 한쪽에만 있는 기준을 0.0으로 채워 넣는 구현.
    없는 측정을 0으로 읽는 것은 corner_allowances에서 이미 값을 치른 모양이다."""
    before = judge(crit("pm", 60.0, True), crit("gone", 1.0, True))
    after = judge(crit("pm", 78.4, True), crit("fresh", 2.0, True))

    assert deltas_between(before, after) == (("pm", 18.4),)


def test_regression_is_pass_to_fail_only():
    """어느 변형을 잡는가: 'after에서 실패한 것 전부'로 계산하는 구현.
    fail -> fail 은 이미 실패하고 있던 것이지 이 시도가 망친 것이 아니다."""
    before = judge(crit("pm", 60.0, True), crit("ugbw", 1.0, False))
    after = judge(crit("pm", 40.0, False), crit("ugbw", 1.1, False))

    assert regressed_between(before, after) == ("pm",)


def test_an_empty_history_renders_to_nothing_rather_than_an_empty_table():
    """어느 변형을 잡는가: 항목이 없어도 머리글을 그리는 구현.
    빈 표는 튜너에게 '시도가 없었다'가 아니라 '무언가 있었다'로 읽힌다."""
    assert render_attempts([]) == ""


def test_an_applied_attempt_renders_its_measured_deltas():
    text = render_attempts([applied(deltas=(("pm", 18.4), ("ugbw", -1.2e6)))])

    assert "TRIMAMP.XRz l" in text
    assert "15 -> 45" in text
    assert "kept" in text
    assert "pm +18.4" in text
    assert "ugbw -1.2e+06" in text


def test_a_rolled_back_attempt_renders_the_criteria_it_regressed():
    text = render_attempts([applied(outcome="rolled_back", regressed=("pm",))])

    assert "rolled_back" in text
    assert "regressed [pm]" in text


def test_a_rejected_attempt_renders_its_gate_reason_and_detail():
    """어느 변형을 잡는가: 사유 코드를 버리고 detail만 쓰는 구현.
    '6.00x exceeds the limit'만으로는 어느 게이트였는지 복원되지 않는다."""
    text = render_attempts([
        applied(outcome="rejected", reason="area", detail="6.00x exceeds the 3.0x limit")
    ])

    assert "rejected" in text
    assert "area:" in text
    assert "6.00x exceeds the 3.0x limit" in text


def test_the_renderer_keeps_the_most_recent_attempts_and_says_how_many_it_dropped():
    """어느 변형을 잡는가: 앞에서부터 자르는 구현(--max-knobs가 알파벳순으로
    결정적 노브를 잘라 낸 것과 같은 모양), 그리고 조용히 자르는 구현."""
    attempts = [applied(refdes=f"R{i}") for i in range(ATTEMPT_RENDER_LIMIT + 5)]

    text = render_attempts(attempts)

    assert "R0 " not in text
    assert f"R{ATTEMPT_RENDER_LIMIT + 4} " in text
    assert "5" in text and "omitted" in text
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_attempt_log.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'analogcoder.attempt_log'`

- [ ] **Step 3: 최소 구현**

`src/analogcoder/attempt_log.py`:

```python
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
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_attempt_log.py -q`
Expected: PASS (7 tests)

- [ ] **Step 5: 변형을 실제로 적용해 확인한다**

각각 하나씩 적용하고 지목한 테스트가 **실패하는지** 본 뒤 되돌린다:
1. `deltas_between`에서 `if c["name"] in before_by`를 지운다(그리고 `.get`으로 0 대체) → `test_deltas_cover_only_criteria_present_in_both_judgements` 실패해야 한다.
2. `regressed_between`의 `before_by[...]["pass"] and`를 지운다 → `test_regression_is_pass_to_fail_only` 실패해야 한다.
3. `render_attempts`의 `[-limit:]`를 `[:limit]`로 바꾼다 → `test_the_renderer_keeps_the_most_recent_attempts...` 실패해야 한다.
4. `if not attempts: return ""`를 지운다 → `test_an_empty_history_renders_to_nothing...` 실패해야 한다.

- [ ] **Step 6: 커밋**

```bash
git add src/analogcoder/attempt_log.py tests/unit/test_attempt_log.py
git commit -m "feat: 튜닝 시도 항목, 결정론적 델타/회귀, 렌더러"
```

---

### Task 2: 적용된 시도를 배선한다

**Files:**
- Modify: `src/analogcoder/orchestrator.py` (167-206, 441-450, 569-573 부근)
- Modify: `src/analogcoder/agents/tuner.py` (`propose_tuning`)
- Modify: `src/analogcoder/cli.py:199-202` (`tune_fn`)
- Test: `tests/unit/test_orchestrator.py` (확장)

**Interfaces:**
- Consumes: Task 1의 `Attempt`, `deltas_between`, `regressed_between`, `render_attempts`.
- Produces: `tuning_history`가 `list[Attempt]`가 된다(더 이상 dict 리스트가 아니다). `propose_tuning`의 세 번째 인자가 `list[dict]` → `str`(렌더된 텍스트)로 바뀐다. Task 3이 같은 리스트에 거부 항목을 추가한다.

**주의 — 이 태스크가 깨뜨리는 기존 코드:** `orchestrator.py:202-206`의 `touched_refdes`가 `entry["proposal"]["proposed_changes"]`를 읽는다. 항목 모양이 바뀌므로 **같은 태스크 안에서** 고쳐야 한다. 그 밖에 `tuning_history`를 읽는 곳은 `agents.tune` 호출뿐이다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/unit/test_orchestrator.py` 끝에 추가. 이 파일의 기존 관례를 그대로 쓴다: `@pytest.mark.asyncio` + `tmp_path`, `make_agents(**overrides)`, `RunState(run_dir=..., testbench_names=[...])`, `await run_orchestration(...)`, 이벤트 키는 **`"step"`**(`"event"`가 아니다), 항상 실패하는 판정은 `judge=lambda m, s: _async(FAIL_JUDGE)`.

```python
TWO_CRITERION_BEFORE = {
    "overall_pass": False,
    "criteria": [
        {"name": "pm", "target": ">=60", "actual": 50.0, "pass": False, "margin": -10.0},
        {"name": "ugbw", "target": ">=1e6", "actual": 2e6, "pass": True, "margin": 1e6},
    ],
}
TWO_CRITERION_AFTER = {
    "overall_pass": False,
    "criteria": [
        {"name": "pm", "target": ">=60", "actual": 58.0, "pass": False, "margin": -2.0},
        {"name": "ugbw", "target": ">=1e6", "actual": 0.5e6, "pass": False, "margin": -0.5e6},
    ],
}


@pytest.mark.asyncio
async def test_a_rolled_back_attempt_carries_its_measured_deltas_to_the_next_proposal(tmp_path):
    """어느 변형을 잡는가: 히스토리에 recommendation만 남기는 원래 구현.
    "롤백됨"만으로는 무엇이 얼마나 움직였는지 알 수 없고, 그 숫자는
    new_judge_result 안에 이미 있다. verify_post의 regressed_criteria를
    일부러 비워 둔 것도 변형 탐지다 - 회귀가 거기서 온다면 이 테스트가 통과할
    수 없다."""
    seen = []
    calls = {"n": 0}

    async def judge(measurements, spec):
        calls["n"] += 1
        return TWO_CRITERION_BEFORE if calls["n"] == 1 else TWO_CRITERION_AFTER

    async def tune(structure_view, judge_result, attempts_view, rejection_feedback, netlist_view):
        seen.append(attempts_view)
        return FAKE_PROPOSAL

    async def rollback_verify_post(prev_judge, new_judge, applied_changes):
        return {
            "improved": False,
            "regressed_criteria": [],  # 비워 둔다 - 우리는 이것을 쓰지 않는다
            "recommendation": "rollback",
            "feedback": "regressed",
        }

    agents = make_agents(judge=judge, tune=tune, verify_post=rollback_verify_post)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    assert seen[0] == ""                  # 첫 제안에는 히스토리가 없다
    assert "rolled_back" in seen[1]
    assert "pm +8" in seen[1]             # 측정된 델타
    assert "ugbw -1.5e+06" in seen[1]
    assert "regressed [ugbw]" in seen[1]  # verify_post가 아니라 judge에서 나온 회귀


@pytest.mark.asyncio
async def test_the_attempt_log_event_is_written_even_before_any_attempt_exists(tmp_path):
    """어느 변형을 잡는가: 항목이 있을 때만 로그를 남기는 구현.
    "기록했고 0건"과 "기록 자체가 사라졌다"가 history.jsonl에서 구별되어야
    한다 - 이 저장소에서 조용히 무력해진 게이트가 아홉 번 나왔고, 그 중 여섯
    번은 실행 로그로 알아챌 수 없었다."""
    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE))
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    events = [json.loads(line) for line in open(state.history_path)]
    logs = [e for e in events if e["step"] == "attempt_log"]

    assert logs, "attempt_log가 하나도 없다"
    assert logs[0]["total"] == 0
    assert logs[0]["rendered"] == 0
    assert logs[0]["dropped"] == 0
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py -k "measured_deltas or attempt_log_event" -q`
Expected: FAIL — 렌더된 히스토리도 `attempt_log` 이벤트도 아직 없다.

- [ ] **Step 3: 구현**

`orchestrator.py` 상단에 import 추가:

```python
from analogcoder.attempt_log import Attempt, deltas_between, regressed_between, render_attempts
```

`touched_refdes`(202-206 부근)를 교체:

```python
            touched_refdes = {attempt.refdes for attempt in tuning_history}
```

`tuning_history` 선언(153 부근)에 타입을 맞춘다:

```python
        tuning_history: list[Attempt] = []
```

승인된 재시도 번호를 기억한다. 443 부근의 재시도 루프 앞:

```python
            approved_proposal = None
            approved_retry = 0
            rejection_feedback = None
            verify_pre_rejected_any = False
```

그리고 승인 지점(`if review["approved"]:`):

```python
                if review["approved"]:
                    approved_proposal = proposal
                    approved_retry = retry
                    break
```

`agents.tune` 호출(445 부근)을 교체 — 렌더와 로그가 **호출 직전에** 함께 일어난다:

```python
                attempts_view = render_attempts(tuning_history)
                rendered = len(tuning_history[-ATTEMPT_RENDER_LIMIT:])
                # 무조건 남긴다. 항목이 0건인 이터레이션에도 남겨야
                # "기록했고 0건"과 "기록이 사라졌다"가 구별된다.
                state.log_event(
                    "attempt_log",
                    {
                        "outer_iter": outer_iter,
                        "retry": retry,
                        "total": len(tuning_history),
                        "by_outcome": _outcome_counts(tuning_history),
                        "rendered": rendered,
                        "dropped": len(tuning_history) - rendered,
                    },
                )
                proposal = await agents.tune(
                    structure_view, judge_result, attempts_view, rejection_feedback, netlist_view
                )
```

`ATTEMPT_RENDER_LIMIT`도 import에 추가하고, 모듈 수준에 헬퍼를 둔다:

```python
def _outcome_counts(attempts: list[Attempt]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for attempt in attempts:
        counts[attempt.outcome] = counts.get(attempt.outcome, 0) + 1
    return counts
```

`tuning_history.append({...})`(569-573)를 교체. **위치는 그대로다** — keep이든 rollback이든 남아야 하므로 롤백 분기 앞이다:

```python
            outcome = "rolled_back" if post_review["recommendation"] == "rollback" else "kept"
            deltas = deltas_between(judge_result, new_judge_result)
            regressed = regressed_between(judge_result, new_judge_result)
            for change in approved_proposal["proposed_changes"]:
                tuning_history.append(
                    Attempt(
                        outer_iter=outer_iter,
                        retry=approved_retry,
                        refdes=change["refdes"],
                        param=change["param"],
                        old_value=change["old_value"],
                        new_value=change["new_value"],
                        outcome=outcome,
                        deltas=deltas,
                        regressed=regressed,
                    )
                )
```

`agents/tuner.py`의 `propose_tuning` 시그니처와 프롬프트 본문:

```python
async def propose_tuning(
    structure_view: str,
    judge_result: dict,
    attempts_view: str,
    rejection_feedback: str | None,
    netlist_text: str,
    backend: AgentBackend,
) -> dict:
    user_prompt = (
        f"Current netlist:\n{netlist_text}\n"
        f"Circuit structure (derived deterministically): {structure_view}\n"
        f"Judge result: {judge_result}\n"
        f"{attempts_view}\n"
        f"Rejection feedback (if retrying): {rejection_feedback}"
    )
```

`cli.py:199-202`의 `tune_fn` 인자 이름도 맞춘다(위치 인자이므로 동작은 같지만 이름이 어긋나면 읽는 사람이 틀린다):

```python
    async def tune_fn(structure_view, judge_result, attempts_view, rejection_feedback, netlist_text_arg):
        return await propose_tuning(
            structure_view, judge_result, attempts_view, rejection_feedback, netlist_text_arg, agent_backends["tuner"]
        )
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py tests/unit/test_tuner_agent.py -q`
Expected: PASS. 기존 테스트가 `history` 인자에 dict 리스트를 기대하고 있었다면 그 기대만 렌더 텍스트로 고친다 — **테스트를 지우지 않는다.**

- [ ] **Step 5: 전체 회귀**

Run: `.venv/bin/python -m pytest -m "not slow" -q`
Expected: 904 이상 passed, 실패 0

- [ ] **Step 6: 변형 확인**

`tuning_history.append(...)`를 `if outcome == "kept":` 뒤로 옮긴다 → `test_a_rolled_back_attempt_carries_its_measured_deltas...` 실패해야 한다. 되돌린다.

- [ ] **Step 7: 커밋**

```bash
git add -A
git commit -m "feat: 적용된 시도가 측정된 델타와 회귀를 담아 다음 제안까지 간다"
```

---

### Task 3: 거부된 시도를 배선한다

**Files:**
- Modify: `src/analogcoder/orchestrator.py` (파라미터 튜닝 재시도 루프, 452-540 부근)
- Test: `tests/unit/test_orchestrator.py` (확장)

**Interfaces:**
- Consumes: Task 2의 `tuning_history: list[Attempt]`, Task 1의 `Attempt`.
- Produces: 없음(마지막 배선 태스크).

**이 태스크가 고치는 사실:** 거부는 지금 이터레이션을 못 넘는다. 바로 다음 재시도는 `rejection_feedback`으로 마지막 거부 하나를 보지만, 그 변수는 매 outer 이터레이션마다 `None`으로 리셋되고 할당마다 덮어써진다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
STIMULUS_NETLIST = "* tb\nVin in 0 1\nRf vminus vout 10k\n.end\n"


def one_change(refdes, param, old_value, new_value):
    return {
        "proposed_changes": [
            {"refdes": refdes, "param": param, "old_value": old_value,
             "new_value": new_value, "reasoning": "x"}
        ],
        "overall_reasoning": "x",
        "confidence": 90,
    }


@pytest.mark.parametrize(
    "reason, netlist, proposal, reject_verify_pre",
    [
        # 면적: 40u -> 100u 는 2.5x 로 티어를 넘는다 (기존 oversized_tune 와 동일)
        ("area", AREA_TEST_NETLIST, one_change("M6", "W", "40u", "100u"), False),
        # refdes: 어느 컴포넌트와도 안 맞는다
        ("refdes", BASE_NETLIST, one_change("Znope", "value", "1k", "2k"), False),
        # param: "width" 는 Rf 줄에도 동일 모델 peer 에도 없는 이름이다
        ("param", BASE_NETLIST, one_change("Rf", "width", "10k", "11k"), False),
        # stimulus: 최상위 V 원이다
        ("stimulus", STIMULUS_NETLIST, one_change("Vin", "value", "1", "100"), False),
        # verify_pre: 게이트는 전부 통과하고 LLM 검토자가 거부한다
        ("verify_pre", BASE_NETLIST, FAKE_PROPOSAL, True),
    ],
)
@pytest.mark.asyncio
async def test_each_gate_records_its_own_reason_code(
    tmp_path, reason, netlist, proposal, reject_verify_pre
):
    """어느 변형을 잡는가: 다섯 게이트의 사유를 하나로 뭉개는 구현 -
    "rejected"만 남기거나, 이벤트 스트림에서 사유를 다시 파싱하는 구현
    (area_check와 refdes_check가 둘 다 feedback 키를 쓰므로 그쪽에서는
    복원되지 않는다). 다섯 파라미터가 다섯을 구별한다."""
    seen = []

    async def tune(structure_view, judge_result, attempts_view, rejection_feedback, netlist_view):
        seen.append(attempts_view)
        return proposal

    overrides = {"judge": lambda m, s: _async(FAIL_JUDGE), "tune": tune}
    if reject_verify_pre:
        async def reject(structure_view, judge_result, proposal_, netlist_view):
            return {"approved": False, "concerns": [], "feedback": "not justified"}
        overrides["verify_pre"] = reject

    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])
    await run_orchestration(
        {"ac_loop_gain": netlist}, FAKE_SPEC, state, make_agents(**overrides)
    )

    assert any(f"{reason}:" in view for view in seen), f"{reason} 사유가 튜너에게 안 보인다"


@pytest.mark.asyncio
async def test_a_gate_rejection_survives_into_the_next_outer_iteration(tmp_path):
    """어느 변형을 잡는가: 거부를 rejection_feedback으로만 나르는 원래 구현.
    그 변수는 outer 이터레이션마다 None으로 리셋되고 할당마다 덮어써지므로,
    이터레이션 1에서 막힌 노브는 이터레이션 2에서 존재하지 않는다.
    이 테스트가 사라지면 그 회귀가 조용해진다."""
    seen = []

    async def tune(structure_view, judge_result, attempts_view, rejection_feedback, netlist_view):
        seen.append(attempts_view)
        return one_change("M6", "W", "40u", "100u")  # 항상 면적 게이트에 막힌다

    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE), tune=tune)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": AREA_TEST_NETLIST}, FAKE_SPEC, state, agents)

    # 이터레이션 1은 재시도 MAX_TUNING_RETRIES(3)회로 끝난다 -> seen[0..2].
    # seen[3]은 이터레이션 2의 첫 호출이고, 원래 구현에서는 여기가 "" 였다.
    assert seen[0] == ""
    assert seen[3].count("area:") == 3


@pytest.mark.asyncio
async def test_a_rejected_attempt_puts_its_block_into_focus(tmp_path):
    """어느 변형을 잡는가: 거부 항목을 touched_refdes에서 빼는 구현.
    튜너에게 "이 블록에서 거부당했다"고 말하면서 그 블록을 접어서 보여 주는
    것은, verify_pre에 접힌 덱을 주면서 "덱에 없는 것은 거부하라"고
    지시했던 것과 같은 모양이다."""
    async def tune(structure_view, judge_result, attempts_view, rejection_feedback, netlist_view):
        return one_change("AMP.R1", "width", "1k", "2k")  # param 게이트가 막는다

    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE), tune=tune)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": SUBCKT_NETLIST}, FAKE_SPEC, state, agents)

    events = [json.loads(line) for line in open(state.history_path)]
    focus_events = [e for e in events if e["step"] == "focus"]

    assert "AMP" not in focus_events[0]["blocks"]   # 아직 아무것도 안 건드렸다
    assert "AMP" in focus_events[1]["blocks"]       # 거부가 초점을 끌어왔다
```

> 구현자 주의: 위 세 테스트는 **실행 가능한 코드로 쓰였지만 가정이 두 개 있다.**
> (1) `param` 케이스와 `stimulus` 케이스가 그 게이트에 정말 도달하려면 앞선
> 게이트들을 통과해야 한다. (2) `focus` 테스트는 `focus_events[0]`이 아직
> `AMP`를 안 담고 있다고 가정한다 — 실패 기준이 다른 경로로 `AMP`를 이미
> 초점에 넣을 수 있다. **먼저 돌려서 어디서 걸리는지 보고**, 가정이 틀리면
> 픽스처를 고치되 **주장은 약하게 하지 않는다**(예: 초점 테스트가 애초에
> 성립하지 않으면 `touched_refdes` 집합을 직접 검사하는 형태로 바꾼다).

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py -k "reason_code or survives_into or puts_its_block" -q`
Expected: FAIL

- [ ] **Step 3: 구현**

모듈 수준 헬퍼를 추가한다:

```python
def _record_rejected(
    history: list[Attempt], outer_iter: int, retry: int, proposal: dict, reason: str, detail: str
) -> None:
    """게이트는 제안 **전체**를 거부하므로 모든 변경이 같은 사유로 항목이 된다.

    어느 변경이 게이트를 촉발했는지는 게이트가 알려 주지 않으므로 추측하지
    않는다 - detail에 게이트가 낸 피드백이 그대로 들어가고, 그 문자열이 보통
    refdes를 이름으로 담고 있다.
    """
    for change in proposal["proposed_changes"]:
        history.append(
            Attempt(
                outer_iter=outer_iter,
                retry=retry,
                refdes=change["refdes"],
                param=change["param"],
                old_value=change["old_value"],
                new_value=change["new_value"],
                outcome="rejected",
                reason=reason,
                detail=detail,
            )
        )
```

다섯 거부 지점에 한 줄씩 추가한다. **사유 코드는 호출 지점이 알고 있는 것을 쓴다 — 이벤트를 다시 읽지 않는다.**

```python
                if not area_ok:
                    rejection_feedback = area_feedback
                    _record_rejected(tuning_history, outer_iter, retry, proposal, "area", area_feedback)
                    continue
```

```python
                if not refdes_ok:
                    rejection_feedback = refdes_feedback
                    _record_rejected(tuning_history, outer_iter, retry, proposal, "refdes", refdes_feedback)
                    continue
```

```python
                if not param_ok:
                    rejection_feedback = param_feedback
                    _record_rejected(tuning_history, outer_iter, retry, proposal, "param", param_feedback)
                    continue
```

```python
                if not stimulus_ok:
                    rejection_feedback = stimulus_feedback
                    _record_rejected(tuning_history, outer_iter, retry, proposal, "stimulus", stimulus_feedback)
                    continue
```

```python
                verify_pre_rejected_any = True
                rejection_feedback = review["feedback"]
                _record_rejected(
                    tuning_history, outer_iter, retry, proposal, "verify_pre", review["feedback"]
                )
```

`touched_refdes`는 Task 2에서 이미 `tuning_history` 전체를 읽으므로 **추가 변경 없이** 거부된 refdes를 포함한다. 그 사실을 주석으로 남긴다:

```python
            # 거부된 시도의 refdes도 들어간다 - 튜너에게 "이 블록에서
            # 거부당했다"고 말하면서 그 블록을 접어서 보여 줄 수는 없다.
            touched_refdes = {attempt.refdes for attempt in tuning_history}
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py -q`
Expected: PASS

- [ ] **Step 5: 전체 회귀**

Run: `.venv/bin/python -m pytest -m "not slow" -q`
Expected: 실패 0

- [ ] **Step 6: 변형 확인**

다섯 `_record_rejected` 호출의 `reason` 인자를 전부 `"rejected"`로 바꾼다 → 사유 코드 테스트가 실패해야 한다. `verify_pre`의 호출 하나만 지운다 → 그 케이스가 실패해야 한다. 둘 다 되돌린다.

- [ ] **Step 7: 커밋**

```bash
git add -A
git commit -m "feat: 게이트 거부가 사유와 함께 이터레이션을 넘어 남는다"
```

---

### Task 4: 튜너 프롬프트 — 사실로, 제한으로 아님

**Files:**
- Modify: `src/analogcoder/agents/tuner.py` (`TUNER_SYSTEM_PROMPT`)
- Test: `tests/unit/test_tuner_agent.py` (확장)

**Interfaces:**
- Consumes: Task 2가 넘기는 `attempts_view` 텍스트.
- Produces: 없음.

**이 태스크가 브랜치에서 줄당 위험이 가장 높다.** 이 저장소는 **틀린 프롬프트가 없는 프롬프트보다 나쁘다**는 것을 이미 겪었다(param 게이트가 peer 규칙으로 넓어졌는데 `verify_pre`의 프롬프트는 안 따라가서, 게이트가 통과시키는 제안을 프롬프트가 거부하게 만들고 런을 끝냈다).

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`TUNER_SYSTEM_PROMPT`를 import에 추가하고, 이 파일의 기존 `FakeBackend`를 그대로 쓴다(`backend.calls[0]["user_prompt"]`에서 사용자 프롬프트를 읽는다):

```python
@pytest.mark.asyncio
async def test_the_rendered_attempt_log_reaches_the_user_prompt():
    """어느 변형을 잡는가: attempts_view를 인자로 받아 놓고 프롬프트에 안 넣는
    구현. 시그니처만 바뀌고 아무것도 전달되지 않으면 이 브랜치 전체가 무의미해진다."""
    backend = FakeBackend({"proposed_changes": [], "overall_reasoning": "x", "confidence": 50})
    rendered = "Past attempts this run:\n  iter 1.1  TRIMAMP.XRz l  15 -> 45  kept  pm +18.4"

    await propose_tuning("structure", {"overall_pass": False}, rendered, None, "* deck", backend)

    assert rendered in backend.calls[0]["user_prompt"]


def test_the_tuner_prompt_presents_past_attempts_as_facts_not_as_a_restriction():
    """어느 변형을 잡는가: "이미 시도한 노브를 다시 제안하지 마라"류의 문장을
    프롬프트에 넣는 구현. 히스토리를 필터로 바꾸는 것은 초점을 필터로 바꿨다가
    값을 치른 것과 같은 오류이고, 이 저장소에는 과거의 롤백이 지금은 옳은
    실측이 있다(TRIMAMP.XRz.l 15->60은 위상 여유 81°->125°, 120에서 다시 무너짐)."""
    prompt = TUNER_SYSTEM_PROMPT.lower()

    assert "past attempts this run" in prompt
    assert "you may propose the same component" in prompt
    for banned in ("do not propose", "never propose", "must not repeat", "avoid proposing"):
        assert banned not in prompt, f"히스토리가 제한으로 서술되었다: {banned!r}"
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_tuner_agent.py -k "as_facts or reaches_the_user_prompt" -q`
Expected: FAIL — 프롬프트에 아직 해당 문단이 없다.

- [ ] **Step 3: 구현**

`TUNER_SYSTEM_PROMPT`의 stimulus 문단 뒤에 정확히 이 문단을 넣는다:

```
A "Past attempts this run" list may appear below. Each line is one component
change that was already tried in this run, with what actually happened: "kept"
or "rolled_back" with the measured change in each criterion, or "rejected" with
the deterministic gate that blocked it and that gate's own message. These are
facts about what happened, not instructions. You MAY propose the same component
and parameter again - a criterion's response to a knob is not monotonic in these
circuits, and the rest of the netlist has moved since an earlier attempt. What
the list buys you is knowing what a value already produced, so a repeat should
be a deliberate choice with a different value or a different reason, not a
rediscovery. A "rejected" line is a deterministic gate's ruling on that exact
proposal, so re-proposing the identical change will be blocked again.
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_tuner_agent.py -q`
Expected: PASS

- [ ] **Step 5: 전체 회귀 + 커밋**

```bash
.venv/bin/python -m pytest -m "not slow" -q
git add -A
git commit -m "feat: 튜너 프롬프트가 과거 시도를 사실로 제시한다"
```

---

### Task 5: 측정 — D2의 진입 기준

**Files:**
- Create: `scripts/measure_repeat_rate.py`
- Create: `docs/superpowers/specs/2026-07-28-tuning-attempt-record-measurement.md`

**Interfaces:**
- Consumes: `history.jsonl`의 `tuning_proposal` 이벤트만. **D1 이전 커밋에서도 똑같이 계산된다** — 그것이 이 지표를 고른 이유다.
- Produces: 측정 보고서. D2를 여는지 여부의 근거.

- [ ] **Step 1: 지표 스크립트를 쓴다**

`scripts/measure_repeat_rate.py`:

```python
#!/usr/bin/env python3
"""반복 제안률 - D1 이전 커밋과 이후 커밋에서 **같은 방식으로** 계산된다.

정의: 같은 런 안에서, 이미 rolled_back 또는 rejected 로 끝난 (refdes, param)을
다시 제안한 변경 수 / 전체 제안 변경 수.

D1이 추가한 attempt_log 이벤트를 쓰지 않는다 - 쓰면 이전 커밋에서 잴 수 없고,
그러면 비교 자체가 성립하지 않는다.
"""

import json
import sys
from pathlib import Path

GATES = ("area_check", "refdes_check", "param_check", "stimulus_check")


def measure(history_path: Path) -> dict:
    failed: set[tuple[str, str]] = set()
    pending: dict[tuple[int, int], list[tuple[str, str]]] = {}
    last_approved: tuple[int, int] | None = None
    proposals = repeats = iterations = 0

    for line in open(history_path):
        event = json.loads(line)
        step = event.get("step")

        if step == "tuning_proposal":
            key = (event["outer_iter"], event["retry"])
            knobs = [(c["refdes"], c["param"]) for c in event["proposed_changes"]]
            pending[key] = knobs
            last_approved = key
            for knob in knobs:
                proposals += 1
                if knob in failed:
                    repeats += 1

        elif step in GATES and not event["approved"]:
            failed.update(pending.get((event["outer_iter"], event["retry"]), []))

        elif step == "verify_pre" and not event["approved"]:
            failed.update(pending.get((event["outer_iter"], event["retry"]), []))

        elif step == "verify_post" and event["recommendation"] == "rollback":
            failed.update(pending.get(last_approved, []))

        elif step == "judge":
            iterations = max(iterations, event.get("outer_iter", 0))

    return {
        "proposals": proposals,
        "repeats": repeats,
        "rate": repeats / proposals if proposals else 0.0,
        "iterations": iterations,
    }


def main(run_dirs: list[str]) -> None:
    for run_dir in run_dirs:
        history = Path(run_dir) / "history.jsonl"
        if not history.exists():
            print(f"{run_dir}: history.jsonl 없음")
            continue
        m = measure(history)
        print(
            f"{run_dir}: proposals={m['proposals']} repeats={m['repeats']} "
            f"rate={m['rate']:.3f} iterations={m['iterations']}"
        )


if __name__ == "__main__":
    main(sys.argv[1:])
```

`last_approved`가 `verify_post`에 쓰이는 이유: `verify_post` 이벤트에는 `retry`가
없다(`orchestrator.py`가 `{"outer_iter": outer_iter, **post_review}`로 남긴다).
승인된 제안은 그 이터레이션의 마지막 `tuning_proposal`이므로 그것을 쓴다.

- [ ] **Step 2: 스크립트를 기존 런에 대해 검증한다**

`runs/` 아래 기존 런 디렉터리에 돌려 보고, 숫자를 손으로 한 번 확인한다.
(기존 런은 삭제된 analyzer 시대의 것이라 **기준선으로 쓰지 않는다** — 스크립트가
크래시 없이 도는지와 계산이 맞는지만 본다.)

- [ ] **Step 3: 기준선을 잰다 (D1 이전)**

```bash
git stash list  # 작업 트리가 깨끗한지 확인
git checkout <D1 이전 커밋>
```

각각 3회씩:
- `benchmarks/two_stage_opamp/spec.yaml`
- `benchmarks/bandgap/spec_seed_buf0_droop.yaml`

런 디렉터리는 `runs/measure/before/<spec>-<n>`에 둔다.

- [ ] **Step 4: D1 이후를 잰다**

브랜치로 돌아와 같은 6회를 `runs/measure/after/<spec>-<n>`에 돌린다.

- [ ] **Step 5: 보고서를 쓴다**

`docs/superpowers/specs/2026-07-28-tuning-attempt-record-measurement.md`에:

- 커밋별·스펙별 반복 제안률과 PASS까지의 이터레이션 수, **관측된 분산**
- 실제 소요 시간
- **개선을 주장하지 않는다.** 3회는 통계가 아니며 그렇게 적는다. LLM은
  결정론적이지 않다.
- **결정:** 반복률이 유의미하게 남으면 D2(결정론적 억제 피드백)의 근거가 된다.
  거의 0이면 D2는 필요 없고, 그 사실을 적고 넘어간다.
- 회상이 아무 행동도 바꾸지 않았다면 **그것도 결과로 적는다.**

- [ ] **Step 6: 커밋**

```bash
git add scripts/measure_repeat_rate.py docs/superpowers/specs/2026-07-28-tuning-attempt-record-measurement.md
git commit -m "docs: 반복 제안률 측정 - D1 전후"
```

---

## 완료 후

`CLAUDE.md`의 "Deterministic netlist derivation, and what the tuner is shown" 절에
다음을 더한다 — **측정된 수와 함께**:

- 런 내 히스토리가 무엇을 담는지, 그리고 왜 `verify_post`의 `regressed_criteria`가
  아니라 judge의 `pass` 뒤집힘에서 회귀를 계산하는지
- 다섯 사유 코드와, 이벤트에서 다시 파싱하면 안 되는 이유(`area_check`와
  `refdes_check`가 같은 `feedback` 키를 쓴다)
- 반복 제안률 측정값과 D2를 여는지 여부
- 원래의 런 간 D가 왜 보류됐는지 한 줄(상세는 두 설계 문서에)
