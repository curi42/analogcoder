# 면적 우선 최적화 2·3단계 구현 계획 — 게이트 강등 + 대안 정렬, 파레토 공선

> **에이전트 작업자에게:** 필수 하위 스킬 — `superpowers:subagent-driven-development`
> 로 태스크 단위로 구현한다. 각 단계는 체크박스(`- [ ]`)로 추적한다.

**목표:** 튜닝 루프가 착지하는 점을 면적 기준으로 고르게 만들고(2단계), 세 출처의
수락점을 파레토 공선으로 보고한다(3단계).

**설계:** `docs/superpowers/specs/2026-08-02-area-first-optimization-design.md`.
1단계(면적 최소화 단계)는 이미 출하됐다 —
`docs/superpowers/plans/2026-08-02-area-optimization-phase.md`.

**기술 스택:** Python 3.14, pytest, ngspice-46, sky130.

---

## 착수 전에 결정이 필요한 것 하나 — **설계 스펙의 비용 분석이 틀렸다**

스펙 §비용은 이렇게 적는다:

> iteration당 `verify_pre` 3배, 시뮬레이션 3배. … **벽시계는 3배가 아니다** —
> 세 대안은 독립이므로 기존 워커 풀에 그대로 태운다. CPU 비용이 3배이고 체감
> 시간은 그보다 훨씬 작다.

**이 문장은 `simulate`가 SPICE 호출이라고 가정하는데, 실제로는 LLM 에이전트다.**
`cli.py:509`의 `agent_simulate_fn`은 `agents/simulator_agent.py:simulate`를 부르고,
그것은 도구를 든 `run_agent` 호출이다. `orchestrator.py`는 그것을 테스트벤치마다
팬아웃한다. 그러므로 bandgap(5 테스트벤치) 기준 iteration당 시뮬레이터 **LLM 호출이
5 → 15**, `verify_pre`가 **1 → 3**이 되어 LLM 호출이 대략 **2.5배**가 된다.

그리고 이 저장소는 **LLM 지연이 벽시계를 지배한다는 것을 이미 실측했다**
(`2026-08-03-reduction45-benefit-results.md`: 반복당 약 10분인데 그중 SPICE는 수십
초). 워커 풀은 SPICE를 병렬화하지 SPICE가 1%인 벽시계를 병렬화하지 않는다.
**스펙의 비용 결론은 v1 코너 축소 사전 등록이 4배 틀렸던 것과 같은 모양의 오류다.**

### 권고 — Task 5에서 구현하고, 착수 전에 사람이 승인한다

**대안 **선별**용 시뮬레이션은 시뮬레이터 에이전트를 거치지 않는다.** 에이전트의
일은 컨트롤 블록을 수렴·복구하는 것이고, 컨트롤 블록은 **테스트벤치의 성질이지
파라미터 값의 성질이 아니다** — 그래서 `corner_sim`이 이미 한 번 수렴한 것을 모든
코너에 재사용한다. 선별 3회는 그 수렴된 컨트롤 블록으로 `sim_backend`를 **직접**
돌린다(LLM 0회). 승자에 대해서만 오늘의 `agents.simulate` 경로를 그대로 한 번
돌려 판정과 `verify_post`에 쓴다.

그러면 iteration당 추가 LLM 호출은 `verify_pre` 2회뿐이고, SPICE는 3배가 되지만
그것은 캐시와 워커 풀이 있는 축이다. **설계의 의도(대안 셋을 다 재서 규칙으로
고른다)는 그대로 보존된다.**

**이것은 스펙 본문과 어긋나므로 구현 전에 사람이 승인해야 한다.** 승인 전까지
Task 5를 시작하지 않는다. 거절되면 대안은 스펙대로 `agents.simulate`로 재고,
그때는 **§측정에 "iteration 벽시계"를 반드시 추가한다**(2.5배가 실측되는지).

---

## Global Constraints

이 절의 모든 줄은 **모든** 태스크의 요구사항에 포함된다.

- **문서·주석·커밋 메시지는 한글.** 코드 식별자는 영어.
- **`alternatives`는 스키마의 `required`가 아니다.** 약한 모델이 빠뜨린 필수 필드가
  스펙 전체를 하드 FAIL시키는 것을 `TOPOLOGY_SCHEMA`에서 이미 겪었다. 없거나 1개면
  **오늘 동작과 바이트 동일**해야 한다.
- **대안은 최대 3개.** 4개 이상을 받으면 앞의 3개만 쓰고 **버린 개수를 로그에 남긴다**
  (조용한 절단 금지).
- **강등 대상은 면적 게이트뿐.** `check_refdes_resolution` / `check_param_applicability`
  / `check_stimulus_untouched`는 그대로 거부한다. 다만 **대안별로** 돌려 걸린 대안만
  버린다.
- **면적 게이트의 계산은 하나도 줄이지 않는다.** `evaluate_area_growth`의 반환을
  그대로 쓰고 `area_check` 이벤트에 `blocking: false`를 **무조건** 싣는다 —
  `blocking` 키의 부재와 `false`가 구별되어야 한다.
- **`verify_pre`는 시뮬레이션 *전*에 살아남은 모든 대안에 돈다.** 측정값으로 고르면
  `Vin`의 AC 진폭이나 `Cload` 축소 같은 치팅이 1등을 한다.
- **분기 발화 횟수를 무조건 로그에 남긴다.** "통과 대안 ≥ 2" 분기가 0회면 튜너
  단계의 면적 정렬은 무력하고, 그때 정직한 결론은 되돌리는 것이다 — 그것을 보려면
  0도 기록돼야 한다.
- **`COMPARISON_REL_TOLERANCE = 1e-3`을 새로 정의하지 않는다.** `curation.py`의
  것을 import 한다. 값의 근거(잡음 4.2e-5, 실차 0.102)는 거기 있다.
- **파레토 공선에 착지점(`entry`)을 반드시 넣는다.** 큐레이션에서 "현직의 점이
  비교에서 빠져 있던" 것이 조용히 무력한 게이트 12건 중 하나다.
- **최적화에 FAIL은 없다.** 어느 단계든 예외는 롤백 + `*_failed` + well-formed
  `UNCHANGED`.
- **결과는 자기가 반환하는 덱을 설명해야 한다.** 공선의 각 행은 출하 여부와 코너
  확인 여부를 스스로 말한다.
- **`re.sub`/`str.replace`를 쓰면 `re.subn`으로 세고 결과를 기록한다.**
- **드리프트 가드는 한 순서로만 갱신한다** — 새 입력이 게이트를 통과함을 먼저
  확인하고, 그 다음에 수를 올린다.
- **테스트 수 실측 줄**(`CLAUDE.md`)은 실측한 값으로만 갱신한다.
- 현재 기준선: `pytest -m "not slow"` → **1613 passed, 2 skipped, 9 deselected**.
- **`two_stage_opamp`의 노브 수는 33이 아니라 30이다.** 스펙의 표는
  2026-08-04 바이어스 수정 이전 값이다(`2026-08-04-tso-bias-fix-results.md`).
  bandgap의 167은 그대로다.

---

## 파일 구조

| 파일 | 책임 | 변경 |
|---|---|---|
| `src/analogcoder/schemas.py` | `TUNER_SCHEMA`에 `alternatives` | 수정 |
| `src/analogcoder/agents/tuner.py` | 대안을 요구하는 프롬프트 문단 | 수정 |
| `src/analogcoder/alternatives.py` | **신규** — 대안 정규화·게이트 적용·선택 규칙 | 생성 |
| `src/analogcoder/judge_tools.py` | `normalized_violation` / `violation_sum` | 수정 |
| `src/analogcoder/orchestrator.py` | 재시도 루프를 대안 경로로 배선 | 수정 |
| `src/analogcoder/area_limits.py` | 강등(호출부에서 결정, 함수는 불변) | 무변경 |
| `src/analogcoder/pareto.py` | **신규** — 공선 수집·지배 판정·출하점 선정 | 생성 |
| `src/analogcoder/cli.py` | 공선 조립, `result["pareto_front"]` | 수정 |
| `src/analogcoder/report.py` | 공선 표 | 수정 |

**왜 `alternatives.py`를 새로 만드는가:** `orchestrator.py`는 이미 800줄이 넘고
재시도 루프가 그 안에서 가장 복잡한 블록이다. 선택 규칙은 **시뮬레이션 없이
단위 테스트가 가능한 순수 함수**이므로 분리하면 스펙이 "반드시 핀하라"고 적은
양 분기를 오케스트레이터 없이 핀할 수 있다.

**왜 `pareto.py`를 새로 만드는가:** `curation.py`의 지배 판정을 **재사용**하되
공선 조립은 큐레이션의 관심사가 아니다. `curation.py`는 이미 1000줄이 넘는다.

---

# 2단계: 게이트 강등 + 대안 3개

### Task 1: `TUNER_SCHEMA`에 `alternatives`를 더한다

**Files:**
- Modify: `src/analogcoder/schemas.py`
- Test: `tests/unit/test_schemas.py`

**Interfaces:**
- Produces: `TUNER_SCHEMA`가 `alternatives`를 optional array로 받는다. 각 원소는
  `proposed_changes`와 같은 모양의 `{changes, reasoning}`.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_tuner_schema_accepts_alternatives_and_does_not_require_them():
    import jsonschema
    from analogcoder.schemas import TUNER_SCHEMA

    base = {
        "proposed_changes": [
            {"refdes": "M1", "param": "W", "old_value": "8",
             "new_value": "10", "reasoning": "x"}
        ],
        "overall_reasoning": "x",
        "confidence": 90,
    }
    # 오늘의 모양이 그대로 유효해야 한다 - alternatives 는 required 가 아니다.
    jsonschema.validate(base, TUNER_SCHEMA)

    with_alts = dict(base, alternatives=[
        {"changes": [{"refdes": "M2", "param": "W", "old_value": "4",
                      "new_value": "5", "reasoning": "y"}],
         "reasoning": "대안 1"},
    ])
    jsonschema.validate(with_alts, TUNER_SCHEMA)


def test_an_alternative_change_obeys_the_same_refdes_and_param_patterns():
    """대안이 느슨한 문법을 통과하면 게이트가 뒤에서 잡아야 하고, 그러면
    대안 하나가 재시도를 태운다. 같은 패턴을 쓴다."""
    import jsonschema
    import pytest
    from analogcoder.schemas import TUNER_SCHEMA

    bad = {
        "proposed_changes": [
            {"refdes": "M1", "param": "W", "old_value": "8",
             "new_value": "10", "reasoning": "x"}
        ],
        "overall_reasoning": "x", "confidence": 90,
        # param 에 점이 들어간 형태 - 오늘 proposed_changes 가 거절하는 것
        "alternatives": [
            {"changes": [{"refdes": "M2", "param": "X.W", "old_value": "4",
                          "new_value": "5", "reasoning": "y"}],
             "reasoning": "대안 1"},
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, TUNER_SCHEMA)
```

- [ ] **Step 2: 실패를 확인한다**

`.venv/bin/python -m pytest tests/unit/test_schemas.py -k alternatives -v`
→ 둘째 테스트가 FAIL (스키마가 `alternatives`를 모르므로 통과시킨다).

- [ ] **Step 3: 스키마를 고친다**

`_CHANGE_SCHEMA`를 밖으로 빼서 `proposed_changes`와 `alternatives[].changes`가
**같은 객체를 참조**하게 한다 — 손으로 두 번 쓰면 갈라진다(`compose.py`가
`netlist.py`의 include 규칙을 베껴 겪은 일).

```python
_CHANGE_SCHEMA = {
    "type": "object",
    "properties": {
        "refdes": {
            "type": "string",
            "pattern": r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$",
        },
        "param": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
        "old_value": {"type": "string", "pattern": r"^-?[0-9]*\.?[0-9]+([eE][+-]?[0-9]+)?[a-zA-Z]*$"},
        "new_value": {"type": "string", "pattern": r"^-?[0-9]*\.?[0-9]+([eE][+-]?[0-9]+)?[a-zA-Z]*$"},
        "reasoning": {"type": "string"},
    },
    "required": ["refdes", "param", "old_value", "new_value", "reasoning"],
}

TUNER_SCHEMA = {
    "type": "object",
    "properties": {
        "proposed_changes": {"type": "array", "items": _CHANGE_SCHEMA},
        # 대안은 **required 가 아니다**. 약한 모델이 빠뜨린 필수 필드가 스펙
        # 전체를 하드 FAIL 시키는 것을 TOPOLOGY_SCHEMA 의 block_path 에서 이미
        # 겪었다. 없거나 1개면 오늘 동작과 바이트 동일해야 한다.
        "alternatives": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "changes": {"type": "array", "items": _CHANGE_SCHEMA},
                    "reasoning": {"type": "string"},
                },
                "required": ["changes", "reasoning"],
            },
        },
        "overall_reasoning": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["proposed_changes", "overall_reasoning", "confidence"],
}
```

- [ ] **Step 4: 통과를 확인한다**
- [ ] **Step 5: 커밋** — `feat: TUNER_SCHEMA 에 optional alternatives`

---

### Task 2: 개선량 — `normalized_violation`

**Files:**
- Modify: `src/analogcoder/judge_tools.py`
- Test: `tests/unit/test_judge_tools.py`

**Interfaces:**
- Produces:
  `violation_sum(criteria, before: dict[str, float], after: dict[str, float]) -> ViolationSum`
  where `ViolationSum = (total_before: float, total_after: float, improvement: float, zero_scale_count: int)`

**왜 `relative_slack`을 그대로 쓰지 않는가:** 그 함수의 스케일은
`max(|threshold|, |actual|)`로 **점마다 다르다**. 적용 전과 후를 비교하려면 같은
자로 재야 하므로 스케일이 `max(|threshold|, |actual_before|, |actual_after|)`여야
한다. 이것이 스펙이 고정한 정의다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_violation_sum_uses_one_scale_for_before_and_after():
    """적용 전과 후를 다른 자로 재면 개선량이 뜻을 잃는다."""
    from analogcoder.judge_tools import violation_sum
    from analogcoder.spec import Criterion

    c = Criterion(name="gain", measurement="g", operator=">=", threshold=60.0, unit="dB")
    # scale = max(60, 40, 50) = 60.  before = (60-40)/60,  after = (60-50)/60
    r = violation_sum([c], {"g": 40.0}, {"g": 50.0})
    assert r.total_before == pytest.approx(20.0 / 60.0)
    assert r.total_after == pytest.approx(10.0 / 60.0)
    assert r.improvement == pytest.approx(10.0 / 60.0)
    assert r.zero_scale_count == 0


def test_a_passing_criterion_contributes_zero_violation_not_a_negative_one():
    """통과한 기준의 여유는 개선량이 아니다. max(0, ...) 가 그것을 자른다 -
    자르지 않으면 이미 통과한 기준을 더 통과시키는 변경이 실패한 기준을
    고치는 변경을 이긴다."""
    from analogcoder.judge_tools import violation_sum
    from analogcoder.spec import Criterion

    c = Criterion(name="gain", measurement="g", operator=">=", threshold=60.0, unit="dB")
    r = violation_sum([c], {"g": 70.0}, {"g": 90.0})
    assert r.total_before == 0.0 and r.total_after == 0.0
    assert r.improvement == 0.0


def test_a_zero_scale_criterion_contributes_zero_and_is_counted():
    """근거 없는 상수로 나누지 않는다. 대신 몇 건인지 센다 - 0 기여와
    '스케일이 없어서 잴 수 없었다' 가 구별되어야 한다."""
    from analogcoder.judge_tools import violation_sum
    from analogcoder.spec import Criterion

    c = Criterion(name="z", measurement="z", operator=">=", threshold=0.0, unit="")
    r = violation_sum([c], {"z": 0.0}, {"z": 0.0})
    assert r.total_before == 0.0 and r.zero_scale_count == 1


def test_a_missing_or_nan_measurement_is_counted_not_read_as_zero():
    """측정이 없는 것은 위반 0 이 아니다. LLM judge 가 없는 측정을 0 으로
    쓴 것이 그 에이전트를 제거한 이유다."""
    import math
    from analogcoder.judge_tools import violation_sum
    from analogcoder.spec import Criterion

    c = Criterion(name="gain", measurement="g", operator=">=", threshold=60.0, unit="dB")
    r = violation_sum([c], {}, {"g": 50.0})
    assert r.unmeasured_count == 1
    assert r.total_before == 0.0  # 기여하지 않는다
    r2 = violation_sum([c], {"g": math.nan}, {"g": 50.0})
    assert r2.unmeasured_count == 1
```

- [ ] **Step 2: 실패를 확인한다** — `ImportError: violation_sum`

- [ ] **Step 3: 구현한다**

```python
@dataclass(frozen=True)
class ViolationSum:
    total_before: float
    total_after: float
    improvement: float
    zero_scale_count: int
    unmeasured_count: int


def normalized_violation(criterion: Criterion, actual: float, scale: float) -> float:
    """정규화 위반량. 통과했으면 0, 실패했으면 (부족분 / scale) 이다.

    `relative_slack` 과 스케일이 다르다: 저쪽은 `max(|threshold|, |actual|)` 로
    **점마다** 계산하고, 여기는 적용 전과 후가 **같은 자**를 써야 하므로
    스케일을 인자로 받는다. 두 점을 다른 자로 재면 개선량이 뜻을 잃는다.
    """
    if scale == 0.0:
        return 0.0
    slack = (actual - criterion.threshold if criterion.operator in _LOWER_BOUND
             else criterion.threshold - actual)
    return max(0.0, -slack / scale)


def violation_sum(criteria, before, after) -> ViolationSum:
    tb = ta = 0.0
    zero_scale = unmeasured = 0
    for c in criteria:
        a0, a1 = before.get(c.measurement), after.get(c.measurement)
        # 없는 측정과 NaN 은 같은 사실이다 - "재지 않았다". 0 으로 읽지 않는다.
        if a0 is None or a1 is None or math.isnan(a0) or math.isnan(a1):
            unmeasured += 1
            continue
        scale = max(abs(c.threshold), abs(a0), abs(a1))
        if scale == 0.0:
            zero_scale += 1
            continue
        tb += normalized_violation(c, a0, scale)
        ta += normalized_violation(c, a1, scale)
    return ViolationSum(tb, ta, tb - ta, zero_scale, unmeasured)
```

- [ ] **Step 4: 통과를 확인한다**
- [ ] **Step 5: 커밋** — `feat: 개선량 - 공통 스케일 정규화 위반량 합`

---

### Task 3: 선택 규칙 — `alternatives.py`

**Files:**
- Create: `src/analogcoder/alternatives.py`
- Test: `tests/unit/test_alternatives.py`

**Interfaces:**
- Consumes: Task 2의 `violation_sum`, `area.total_area`, `judge_tools.evaluate_criteria`.
- Produces:
  - `normalize(proposal: dict) -> list[Alternative]` — 1차 제안 + `alternatives`를
    하나의 목록으로. `Alternative = (index, changes, reasoning, source)` where
    `source ∈ {"primary", "alternative"}`.
  - `select(candidates: list[Measured]) -> Selection` — 스펙의 두 분기.
    `Measured = (alt, passed: bool, area_after: float | None, improvement: float)`.
    `Selection = (winner: Alternative, rule: str, passing_count: int)` with
    `rule ∈ {"min_area_among_passing", "max_improvement"}`.

- [ ] **Step 1: 실패하는 테스트를 쓴다 — 양 분기 전부**

스펙: "선택 규칙 양 분기 전부 — 통과 2개면 면적이 이기고, 통과 0개면 개선량이
이긴다. 한쪽만 핀하면 나머지가 조용히 죽어도 모른다."

```python
def test_when_two_alternatives_pass_the_smaller_area_wins():
    from analogcoder.alternatives import Alternative, Measured, select
    a = Alternative(0, [], "a", "primary")
    b = Alternative(1, [], "b", "alternative")
    sel = select([
        Measured(a, passed=True, area_after=9.0, improvement=5.0),
        Measured(b, passed=True, area_after=4.0, improvement=0.1),
    ])
    assert sel.winner is b                      # 개선량이 훨씬 작아도 면적이 이긴다
    assert sel.rule == "min_area_among_passing"
    assert sel.passing_count == 2


def test_when_nothing_passes_the_largest_improvement_wins():
    from analogcoder.alternatives import Alternative, Measured, select
    a = Alternative(0, [], "a", "primary")
    b = Alternative(1, [], "b", "alternative")
    sel = select([
        Measured(a, passed=False, area_after=9.0, improvement=5.0),
        Measured(b, passed=False, area_after=1.0, improvement=0.1),
    ])
    assert sel.winner is a                      # 면적이 훨씬 작아도 개선량이 이긴다
    assert sel.rule == "max_improvement"
    assert sel.passing_count == 0


def test_one_passing_alternative_still_reports_the_area_rule():
    """분기 발화 계측이 뜻을 가지려면 '통과 1개' 와 '통과 2개 이상' 이
    구별되어야 한다 - passing_count 가 그것을 싣는다."""
    from analogcoder.alternatives import Alternative, Measured, select
    a = Alternative(0, [], "a", "primary")
    b = Alternative(1, [], "b", "alternative")
    sel = select([
        Measured(a, passed=True, area_after=9.0, improvement=0.0),
        Measured(b, passed=False, area_after=1.0, improvement=5.0),
    ])
    assert sel.winner is a and sel.rule == "min_area_among_passing"
    assert sel.passing_count == 1


def test_an_unmeasurable_area_loses_to_a_measurable_one_and_never_wins_by_default():
    """area_after 가 None 인 것은 '면적 0' 이 아니라 '못 쟀다' 다.
    None 을 0 으로 읽으면 잴 수 없는 대안이 항상 이긴다."""
    from analogcoder.alternatives import Alternative, Measured, select
    a = Alternative(0, [], "a", "primary")
    b = Alternative(1, [], "b", "alternative")
    sel = select([
        Measured(a, passed=True, area_after=None, improvement=0.0),
        Measured(b, passed=True, area_after=9.0, improvement=0.0),
    ])
    assert sel.winner is b


def test_all_areas_unmeasurable_falls_back_to_improvement_and_says_so():
    from analogcoder.alternatives import Alternative, Measured, select
    a = Alternative(0, [], "a", "primary")
    b = Alternative(1, [], "b", "alternative")
    sel = select([
        Measured(a, passed=True, area_after=None, improvement=1.0),
        Measured(b, passed=True, area_after=None, improvement=5.0),
    ])
    assert sel.winner is b
    assert sel.rule == "max_improvement_area_unmeasurable"


def test_normalize_puts_the_primary_first_and_caps_at_three():
    from analogcoder.alternatives import normalize
    proposal = {
        "proposed_changes": [{"refdes": "M1", "param": "W",
                              "old_value": "1", "new_value": "2", "reasoning": "p"}],
        "alternatives": [
            {"changes": [{"refdes": "M2", "param": "W", "old_value": "1",
                          "new_value": "2", "reasoning": "a"}], "reasoning": "a"},
            {"changes": [{"refdes": "M3", "param": "W", "old_value": "1",
                          "new_value": "2", "reasoning": "b"}], "reasoning": "b"},
            {"changes": [{"refdes": "M4", "param": "W", "old_value": "1",
                          "new_value": "2", "reasoning": "c"}], "reasoning": "c"},
        ],
    }
    alts, dropped = normalize(proposal)
    assert [a.source for a in alts] == ["primary", "alternative", "alternative"]
    assert dropped == 1          # 조용히 자르지 않는다


def test_normalize_without_alternatives_is_todays_behaviour():
    from analogcoder.alternatives import normalize
    proposal = {"proposed_changes": [{"refdes": "M1", "param": "W",
                "old_value": "1", "new_value": "2", "reasoning": "p"}]}
    alts, dropped = normalize(proposal)
    assert len(alts) == 1 and alts[0].source == "primary" and dropped == 0
```

- [ ] **Step 2: 실패를 확인한다**
- [ ] **Step 3: 구현한다** — 위 시그니처 그대로. `select`는 순수 함수이고
      시뮬레이션·LLM을 모르는 상태로 둔다.
- [ ] **Step 4: 통과를 확인한다**
- [ ] **Step 5: 커밋** — `feat: 대안 정규화와 선택 규칙 (양 분기)`

---

### Task 4: 면적 게이트 강등

**Files:**
- Modify: `src/analogcoder/orchestrator.py` (호출부만)
- Test: `tests/unit/test_orchestrator.py`

**`area_limits.py`는 한 줄도 바꾸지 않는다.** 강등은 "게이트가 무엇을 계산하는가"가
아니라 "호출부가 그 결과로 무엇을 하는가"의 변경이다. 함수를 바꾸면 최적화 단계와
큐레이션이 쓰는 같은 함수의 의미가 함께 움직인다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
@pytest.mark.asyncio
async def test_an_area_rejected_proposal_now_passes_but_the_record_survives(tmp_path):
    """강등은 '기록을 지운다' 가 아니다. 예전에 거부됐을 제안이 지나가되
    무엇을 얼마나 키웠는지가 그대로 남아야 한다."""
    # 소자를 6배 키우는 제안 - 오늘의 게이트가 거부하는 것
    ...
    events = [json.loads(l) for l in open(state.history_path)]
    area = [e for e in events if e["step"] == "area_check"]
    assert len(area) == 1
    assert area[0]["blocking"] is False          # 무조건 실린다
    assert area[0]["approved"] is False          # 계산 결과는 그대로다
    assert area[0]["states"]                     # 가시성 상태도 그대로다
    # 그리고 제안은 실제로 적용됐다
    assert result["status"] == "PASS"


@pytest.mark.asyncio
async def test_an_area_rejection_no_longer_burns_a_retry(tmp_path):
    """오늘은 면적 거부가 재시도를 태운다. 강등 후에는 태우지 않는다 -
    tuning_retries 의 failures 가 그것을 말해야 한다."""
    ...
    retries = [e for e in events if e["step"] == "tuning_retries"][0]
    assert retries["failures"] == 0
    assert retries["by_reason"]["area"] == 0
```

- [ ] **Step 2: 실패를 확인한다**
- [ ] **Step 3: 구현한다** — `if not area_ok: ... continue`를 삭제하고,
      `area_check` 이벤트에 `"blocking": False`를 더한다.
      `REJECTION_REASONS`에서 `"area"`는 **지우지 않는다** — 과거 실행의
      `history.jsonl`이 그 코드를 싣고 있고 `attempt_log` 렌더가 그것을 읽는다.
      대신 새 실행에서는 발생하지 않는다는 것이 테스트로 핀된다.
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** — `feat: 면적 게이트를 거부에서 알림으로 강등`

---

### Task 5: 오케스트레이터 배선 — 대안 셋을 재고 고른다

> **착수 조건:** 이 문서 맨 위의 "설계 스펙의 비용 분석이 틀렸다" 절에 대한 사람의
> 결정이 있어야 한다. 승인 없이 시작하지 않는다.

**Files:**
- Modify: `src/analogcoder/orchestrator.py`
- Test: `tests/unit/test_orchestrator.py`

**Interfaces:**
- Consumes: `alternatives.normalize` / `alternatives.select`, Task 2의 `violation_sum`.
- Produces: `tuning_alternatives` 이벤트 (아래).

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
@pytest.mark.asyncio
async def test_the_alternatives_event_is_written_every_retry_including_when_there_is_one(tmp_path):
    """'분기가 한 번도 안 불렸다' 와 '계측이 사라졌다' 가 구별되어야 한다.
    이것이 이 저장소의 첫 번째 상비 질문이다."""
    ...
    ev = [e for e in events if e["step"] == "tuning_alternatives"]
    assert len(ev) >= 1
    assert ev[0]["offered"] == 1              # 튜너가 대안을 안 줬다
    assert ev[0]["dropped_over_cap"] == 0
    assert ev[0]["survived_gates"] == 1
    assert ev[0]["survived_verify_pre"] == 1
    assert ev[0]["simulated"] == 1
    assert ev[0]["passing_count"] == 0
    assert ev[0]["rule"] == "max_improvement"
    assert ev[0]["multi_pass_branch_fired"] is False   # 무조건 실린다


@pytest.mark.asyncio
async def test_one_alternative_failing_a_hard_gate_drops_only_that_alternative(tmp_path):
    """오늘은 게이트에 걸린 제안 하나가 재시도를 통째로 태운다."""
    ...
    assert ev[0]["survived_gates"] == 2       # 셋 중 하나만 떨어졌다
    assert result["status"] == "PASS"


@pytest.mark.asyncio
async def test_verify_pre_runs_on_every_surviving_alternative_before_any_simulation(tmp_path):
    """측정값으로 고르면 Cload 축소 같은 치팅이 1등을 한다."""
    order = []
    # verify_pre 와 simulate 를 감싸 호출 순서를 기록한다
    ...
    assert order.count("verify_pre") == 3
    assert order.index("simulate") > max(i for i, x in enumerate(order) if x == "verify_pre")


@pytest.mark.asyncio
async def test_verify_post_runs_once_on_the_winner_only(tmp_path):
    ...
    assert verify_post_calls == 1
```

- [ ] **Step 2: 실패를 확인한다**
- [ ] **Step 3: 구현한다**

재시도 루프의 본문을 이렇게 바꾼다(구조만 — 이름은 위 인터페이스를 따른다):

```python
proposal = await agents.tune(...)
state.log_event("tuning_proposal", {...})

alts, dropped_over_cap = normalize(proposal)

# 1) 하드 게이트 - 대안별. 걸린 것만 버린다.
surviving = []
for alt in alts:
    ok, reason, feedback = _hard_gates(alt.changes, ...)   # refdes/param/stimulus
    if ok:
        surviving.append(alt)
    else:
        _record_rejected(tuning_history, outer_iter, retry, alt.as_proposal(), reason, feedback)

# 2) 면적 - 계산만. 거부하지 않는다.
areas = {alt.index: evaluate_area_growth(baseline_components, alt.changes) for alt in surviving}
for alt in surviving:
    state.log_event("area_check", {..., "alternative": alt.index, "blocking": False, ...})

# 3) verify_pre - 재기 **전에**, 살아남은 전부에.
approved = []
for alt in surviving:
    review = await agents.verify_pre(...)
    state.log_event("verify_pre", {..., "alternative": alt.index, **review})
    if review["approved"]:
        approved.append(alt)
    else:
        verify_pre_rejected_any = True
        _record_rejected(..., "verify_pre", review["feedback"])

if not approved:
    rejection_feedback = ...
    continue            # 오늘의 재시도 경로 그대로, 예산 3

# 4) 측정 - 선별용은 LLM 을 거치지 않는다(맨 위 결정 참조)
measured = []
for alt in approved:
    texts = _apply_to_all(netlist_texts, alt.changes)
    meas = await screen_simulate(texts, spec)         # sim_backend 직접
    verdict = evaluate_criteria(meas, spec.all_criteria)
    vs = violation_sum(spec.all_criteria, judge_result_measurements, meas)
    measured.append(Measured(alt, verdict["overall_pass"],
                             total_area(texts), vs.improvement))

sel = select(measured)
state.log_event("tuning_alternatives", {
    "outer_iter": outer_iter, "retry": retry,
    "offered": len(alts), "dropped_over_cap": dropped_over_cap,
    "survived_gates": len(surviving), "survived_verify_pre": len(approved),
    "simulated": len(measured),
    "passing_count": sel.passing_count,
    "rule": sel.rule,
    # 무조건 싣는다. 0 이면 튜너 단계의 면적 정렬은 무력하고,
    # 그때 정직한 결론은 그 부분을 되돌리는 것이다.
    "multi_pass_branch_fired": sel.passing_count >= 2,
    "areas": {a.index: (areas[a.index].ratio if a.index in areas else None) for a in approved},
})
approved_proposal = sel.winner.as_proposal()
approved_retry = retry
break
```

**주의 셋:**
1. `screen_simulate`는 승자에게는 쓰지 않는다. 승자는 오늘의 `agents.simulate`를
   그대로 한 번 타서 판정·`verify_post`·`push_netlist_version`에 들어간다 —
   그래야 컨트롤 블록 수렴과 그 게이트 기록이 오늘과 같은 자리에 남는다.
2. `_apply_to_all`은 부작용이 없어야 한다(현재도 새 dict을 만든다). 선별이
   `push_netlist_version`을 부르면 안 된다 — 버전은 승자에 대해서만 올라간다.
3. `Measured.area_after`는 `total_area`가 `counted == 0`을 낼 수 있다. 그때는
   `None`이지 `0.0`이 아니다(Task 3의 테스트가 핀한다).

- [ ] **Step 4: 통과 확인** — `pytest -m "not slow"` 전체
- [ ] **Step 5: 커밋** — `feat: 대안 셋을 재고 규칙으로 고른다`

---

### Task 6: 튜너 프롬프트

**Files:**
- Modify: `src/analogcoder/agents/tuner.py`
- Test: `tests/unit/test_tuner_agent.py`

**게이트와 프롬프트가 어긋나면 승인될 제안이 실행을 끝낸다.** 면적 게이트가
강등됐으므로 프롬프트가 그것을 **제약이 아니라 사실로** 제시해야 한다.

- [ ] **Step 1: 테스트를 쓴다**

```python
def test_the_prompt_asks_for_alternatives_and_says_they_are_optional():
    from analogcoder.agents.tuner import TUNER_SYSTEM_PROMPT
    assert "alternatives" in TUNER_SYSTEM_PROMPT
    assert "up to" in TUNER_SYSTEM_PROMPT or "at most" in TUNER_SYSTEM_PROMPT


def test_the_prompt_presents_area_as_a_fact_not_a_restriction():
    """시도 기록과 같은 규율이다. 제약으로 쓰면 초점이 틀렸을 때 답을 지운다."""
    from analogcoder.agents.tuner import TUNER_SYSTEM_PROMPT
    assert "area" in TUNER_SYSTEM_PROMPT.lower()
    # 금지형 문장이 없어야 한다
    for banned in ["do not grow", "must not increase the area", "only propose changes that shrink"]:
        assert banned not in TUNER_SYSTEM_PROMPT.lower()


def test_the_prompt_does_not_promise_that_area_blocks():
    """예전 프롬프트는 면적 게이트가 거부한다고 적었다. 강등 뒤에 그 문장이
    남아 있으면 프롬프트가 게이트와 모순된다."""
    from analogcoder.agents.tuner import TUNER_SYSTEM_PROMPT
    assert "rejected by a deterministic area gate" not in TUNER_SYSTEM_PROMPT
```

- [ ] **Step 2~4: 실패 확인 → 프롬프트 수정 → 통과 확인**

프롬프트에 더할 문단(요지):

> You may propose **up to three alternatives** in addition to your primary
> proposal. They are optional — one proposal is a complete answer. Alternatives
> are most useful when several different knobs could plausibly fix the same
> criterion: all of them are simulated and the one that passes with the smallest
> total area is applied. Do not pad the list with variations of one value.
>
> A deterministic area model reports how much each proposal grows the circuit.
> **It does not block anything.** The number is a fact about your proposal, in
> the same way the past-attempt table is a fact about this run.

- [ ] **Step 5: 커밋** — `feat: 튜너 프롬프트 - 대안 요청과 면적 사실 제시`

---

### Task 7: ngspice 실측 — 2단계 기준선 대비

**Files:**
- Create: `tests/unit/test_alternatives_ngspice.py` (`slow` 마크)
- Create: `scripts/alternatives_ab.py`

**측정 규칙은 데이터를 보기 전에 고정한다** (스펙 §구현 뒤에 측정할 것 넷):

1. **대안 3개가 수렴 속도를 바꾸는가** — 착지까지의 `outer_iter` 수
2. **면적이 실제로 주는가** — 착지 면적. 1단계 실측이 기준선이다
   (bandgap 1.10546e-08 → 8.93292e-09, two_stage_opamp UNCHANGED)
3. **재시도 여유분이 줄어드는가** — `tuning_retries.headroom`, 공짜
4. **"통과 대안 ≥ 2" 분기가 발화하는가** — `multi_pass_branch_fired`의 합

**사전 등록한 판정:** 4번의 합이 **0이면 튜너 단계의 면적 정렬은 무력하고, 되돌린다.**
그것이 스펙이 미리 적어 둔 정직한 결론이다. 되돌리는 범위를 여기서 못박는다:

| 태스크 | 0 발화 시 |
|---|---|
| 1 (`alternatives` 스키마) | 되돌린다 |
| 2 (`violation_sum`) | **남긴다** — 순수 함수이고 선택 규칙 밖에서도 쓸 수 있다 |
| 3 (선택 규칙) | 되돌린다 |
| 4 (면적 게이트 강등) | **남긴다** — 강등의 근거는 대안 정렬이 아니라 "상한 숫자에 근거가 없다"이고, 그것은 4번 측정과 독립이다 |
| 5 (오케스트레이터 배선) | 되돌린다 |
| 6 (튜너 프롬프트의 대안 요청) | 되돌린다. **면적을 사실로 제시하는 문단은 남긴다**(Task 4가 남으므로) |

또한 **1번과 2번은 arm 하나로 판정하지 않는다** — LLM 튜너는 실행마다 다르다.
`k = 3`, 통계 검정 없음, 이산 사건에 판정을 건다. 상한은 런당 **120분**
(reduction45 v2의 실측 비용 모형).

- [ ] **Step 1: 하니스를 쓴다** — `scripts/reduction45_ab.py`의 짝 병렬 구조를
      재사용한다(파동마다 old/new 동시 실행, `ANALOGCODER_SIM_WORKERS=5`).
      **런별 `stdout`/`stderr`를 갈라 저장한다** — 그것이 없어서 백엔드 실패를
      귀속시키지 못한 전례가 있다.
- [ ] **Step 2: "관측"의 정의에 진행 증거를 요구한다** —
      `orchestration_attempt.iterations_used >= 1`. v2의 다섯 번째 결함이 이것이다.
- [ ] **Step 3: 사전 등록 문서를 쓰고 커밋한다** (실행 0회 시점)
- [ ] **Step 4: 실행하고 집계한다**
- [ ] **Step 5: 결과 문서 + `CLAUDE.md`**

---

# 3단계: 파레토 공선과 보고

### Task 8: 공선 수집 — `pareto.py`

**Files:**
- Create: `src/analogcoder/pareto.py`
- Test: `tests/unit/test_pareto.py`

**Interfaces:**
- Consumes: `curation.COMPARISON_REL_TOLERANCE`, `curation._is_better`의 비교 규칙.
- Produces:
  - `Point = (label: str, source: str, area: float | None, objective: float | None,
     netlist_version: str, criteria: list[dict], corner_verified: bool, shipped: bool)`
  - `build_front(entry, area_points, objective_points) -> Front`
  - `Front = (points: list[Point], shipped_index: int, single_axis: bool)`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_the_entry_point_is_always_in_the_front():
    """큐레이션에서 '현직의 점이 비교에서 빠져 있던' 것이 조용히 무력한
    게이트 12 건 중 하나다. 아무것도 바꾸지 않는 것이 최선일 수 있다."""
    from analogcoder.pareto import build_front
    f = build_front(entry=_pt("entry", area=10.0, obj=100.0), area_points=[], objective_points=[])
    assert [p.source for p in f.points] == ["entry"]


def test_the_shipped_point_is_the_minimum_area_across_all_three_sources():
    """1 단계의 결과로 못박지 않는다 - 전류 단계가 소자를 줄여 전류와 면적을
    함께 낮추는 일이 실제로 가능하다."""
    from analogcoder.pareto import build_front
    f = build_front(
        entry=_pt("entry", area=10.0, obj=100.0),
        area_points=[_pt("area", area=8.0, obj=100.0)],
        objective_points=[_pt("objective", area=7.0, obj=90.0)],   # 더 작다
    )
    assert f.points[f.shipped_index].source == "objective"


def test_only_the_shipped_point_claims_corner_verification():
    from analogcoder.pareto import build_front
    f = build_front(...)
    assert sum(p.corner_verified for p in f.points) <= 1
    assert f.points[f.shipped_index].shipped is True
    assert all(not p.shipped for i, p in enumerate(f.points) if i != f.shipped_index)


def test_without_an_objective_the_front_is_one_axis_and_says_so():
    """키를 빼면 '공선 기능이 없다' 와 구별되지 않는다."""
    from analogcoder.pareto import build_front
    f = build_front(entry=_pt("entry", area=10.0, obj=None), area_points=[], objective_points=[])
    assert f.single_axis is True


def test_the_tolerance_actually_rejects_something():
    """영-허용치의 반대 방향 결함도 핀한다. 1e-3 안쪽의 차이는 지배가 아니다."""
    from analogcoder.pareto import dominates
    from analogcoder.curation import COMPARISON_REL_TOLERANCE
    assert COMPARISON_REL_TOLERANCE == 1e-3
    # 상대차 1e-4 - 잡음이지 개선이 아니다
    assert not dominates(_pt("a", area=1.0000, obj=1.0), _pt("b", area=1.0001, obj=1.0))
    # 상대차 1e-2 - 실제 개선
    assert dominates(_pt("a", area=1.00, obj=1.0), _pt("b", area=1.01, obj=1.0))
```

- [ ] **Step 2~4:** 실패 확인 → 구현 → 통과 확인
- [ ] **Step 5: 커밋** — `feat: 파레토 공선 조립과 지배 판정`

---

### Task 9: `cli.py` 배선과 `result["pareto_front"]`

**Files:**
- Modify: `src/analogcoder/cli.py`
- Test: `tests/unit/test_cli_pareto.py`

- [ ] **Step 1: 테스트**

```python
def test_the_front_is_written_even_when_no_phase_moved_the_deck():
    """'못 줄였다' 와 '공선 기능이 없다' 를 구별한다."""
    ...
    assert result["pareto_front"]["points"]           # entry 한 점은 항상 있다
    assert result["pareto_front"]["single_axis"] is True


def test_the_shipped_point_matches_final_criteria_and_final_netlist_paths():
    """결과는 자기가 반환하는 덱을 설명해야 한다 - 다섯 번 재발한 규칙이다."""
    ...
    shipped = result["pareto_front"]["points"][result["pareto_front"]["shipped_index"]]
    assert shipped["criteria"] == result["final_criteria"]
```

- [ ] **Step 2~4:** 실패 확인 → 배선 → 통과 확인. **면적 단계와 전류 단계의 수락점은
      이미 `_search`의 `records`에 있다** — 새로 시뮬레이션하지 않는다(스펙: 추가
      비용 0).
- [ ] **Step 5: 커밋**

---

### Task 10: `report.md`의 공선 표

**Files:**
- Modify: `src/analogcoder/report.py`
- Test: `tests/unit/test_report.py`

- [ ] **Step 1: 테스트**

```python
def test_each_front_row_says_whether_it_ships_and_whether_corners_were_checked():
    md = render_report(result)
    assert "## 파레토 공선" in md
    assert "출하" in md and "코너 확인" in md
    # None 은 값이 아니라 문장으로 그려진다
    assert "코너에서 확인되지 않음" in md


def test_a_single_axis_front_says_it_is_not_a_front():
    md = render_report(result_without_objective)
    assert "축이 하나여서 공선이 아니다" in md
```

- [ ] **Step 2~4:** 실패 확인 → 구현 → 통과 확인
- [ ] **Step 5: 커밋**

---

### Task 11: ngspice 실측 — 3단계

**Files:**
- Modify: `tests/unit/test_optimizer_area_phase_ngspice.py` 또는 신규 (`slow`)

- [ ] **Step 1:** bandgap `spec_pvt.yaml`(면적·전류 단계 둘 다 도는 유일한 스펙)에서
      공선이 실제로 2점 이상 나오는지, 출하점이 세 출처 중 어디인지를 실측하고 핀한다.
- [ ] **Step 2:** `CLAUDE.md`의 테스트 수 실측 줄과 슬로우 테스트 목록을 갱신한다
      (**측정한 값으로만**).
- [ ] **Step 3: 커밋**

---

## 자기 검토

**스펙 커버리지.** 스펙의 세 변경 중 ①(면적 단계)은 출하 완료, ②는 Task 1·3·4·5·6,
③은 Task 8·9·10. 스펙 §테스트의 8개 항목 대응: 선택 규칙 양 분기(Task 3),
게이트 강등(Task 4), 면적 단계 LLM 없음(**1단계에서 이미 핀됨** —
`test_the_area_phase_calls_no_agent_at_all`), `nf` 이득 0 / unknown 구별(1단계),
1e-3 허용치(Task 8), 착지점 포함(Task 8), `counted == 0` → refused(1단계),
ngspice 실측(Task 7·11).

**미해결로 남기는 것 — 명시한다.**

- 스펙 §실패 모드의 "면적 모델이 소자를 하나도 못 읽음 → **refused**"는 1단계가
  `AreaTotal.counted/skipped`로 이미 기록한다. 3단계 공선에서 그 상태가 어떻게
  보이는지는 Task 8의 `area=None` 경로가 덮는다.
- **`unit` 필드는 여전히 아무 데서도 쓰이지 않는다** — 스펙이 범위 밖으로 명시했다.
- **결합 탐색은 하지 않는다** — 로드맵 단계 4이고 별도 사전 등록이 필요하다.
