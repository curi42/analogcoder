# 면적 최소화 단계 구현 계획 (설계 1/3단계)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 튜닝 루프가 PASS한 뒤, 선언 없이 자동으로 도는 **LLM 없는** 면적 최소화 단계를 추가한다.

**Architecture:** 기존 최적화 단계의 탐색·수락 기계를 그대로 쓴다. 바뀌는 것은 셋뿐이다 — 목적값을 측정값이 아니라 파생 면적에서 읽을 수 있게 하고(표식 객체), 목적/예산/가드를 `spec.optimize`에서 직접 읽는 대신 **데이터로 넘기고**(`PhaseConfig`), 노브 순위를 LLM 대신 **계산된 면적 이득**으로 만든다. `OptimizerAgents.knob_ranking`이라는 주입 지점이 이미 있으므로 LLM을 빼는 배선은 새로 만들지 않는다.

**Tech Stack:** Python 3, pytest(+`pytest-asyncio`), ngspice(실측), 기존 `optimizer.py` / `area.py` / `cli.py` / `report.py`.

## Global Constraints

설계 문서 `docs/superpowers/specs/2026-08-02-area-first-optimization-design.md`를 따른다. 매 태스크에 암묵적으로 포함된다.

- **최적화에는 FAIL이 없다.** 이 단계의 어떤 실패도 실행을 크래시나 FAIL로 바꾸지 않는다.
- **`0`과 `unknown`을 같은 칸에 넣지 않는다.**
- **게이트가 아무것도 안 할 때 로그가 어떻게 보이는가**에 매 기록이 답한다.
- **기존 이벤트 이름을 바꾸지 않는다.** `optimize_*`는 `report.py`와 측정 스크립트가 읽는다. 새 단계는 `optimize_area_*`를 쓴다.
- **면적 이득은 비율이 아니라 절대량**이다. **`nf`에는 면적 이득이 없다.**
- **방향 문자열은 `"decrease"`다** (`_next_value`가 그것만 감산으로 읽는다). `"down"`은 증가로 해석된다.
- 문서·주석은 한글로 쓴다.
- 커밋 메시지 말미:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01N8njT49yMwXwcYsaNnW2KP
  ```

## 기존 코드에서 확인된 사실 (구현자가 다시 찾지 않도록)

- `RunState`에 `.events`는 **없다.** 이벤트는 `state.history_path` 파일을 줄 단위 JSON으로 읽는다.
- `report.py`의 공개 진입점은 `write_report_md(run_dir, result)`이며 파일을 쓴다. 절 렌더러는 `_optimization_lines(optimization) -> list[str]` 같은 사적 함수다.
- `tests/unit/test_optimizer.py`에 `_spec(**overrides)`와 `_agents(measure_sequence, candidates=None)` 픽스처가 있다. **재사용한다.**
- `run_optimization`은 `spec.optimize is None`이면 오늘 `status="SKIPPED"`를 내고 `optimize_skipped`를 남긴다.
- `_format_value(value, integer)`가 이미 `optimizer.py`에 있다. 복제하지 않는다.
- `spec.canonical`은 `testbenches[0]`, `spec.all_criteria`는 전 테스트벤치의 기준 평탄화.

## File Structure

| 파일 | 책임 |
|---|---|
| `src/analogcoder/area.py` (수정) | `AreaModel` 프로토콜과 기본 구현 표시. 회사 이식 시 교체되는 경계 |
| `src/analogcoder/area_ranking.py` (신규) | 노브별 절대 면적 이득 계산과 정렬. LLM도 시뮬레이션도 쓰지 않는다 |
| `src/analogcoder/optimizer.py` (수정) | `AREA_OBJECTIVE`, `PhaseConfig`, `run_area_optimization` |
| `src/analogcoder/cli.py` (수정) | 면적 단계를 전류 단계 **앞에** 배선 |
| `src/analogcoder/report.py` (수정) | `_area_optimization_lines` 추가 |
| `tests/unit/test_area_ranking.py` (신규) | 순위 계산 |
| `tests/unit/test_optimizer_area_phase.py` (신규) | 표식·설정·단계 조립 |
| `tests/unit/test_optimizer_area_phase_ngspice.py` (신규) | bandgap 실측 |

---

### Task 1: 면적 목적 표식과 교체 가능한 면적 모델

**Files:**
- Modify: `src/analogcoder/area.py`
- Modify: `src/analogcoder/optimizer.py` (`STEP_RATIO` 아래, 그리고 오라클의 목적값 대입 한 줄)
- Test: `tests/unit/test_optimizer_area_phase.py` (신규)

**Interfaces:**
- Consumes: `area.total_area(netlist_text) -> AreaTotal`
- Produces: `area.AreaModel`(Protocol), `area.DEFAULT_AREA_MODEL`, `optimizer.AREA_OBJECTIVE`, `optimizer._objective_value(objective, measurements, derived_area) -> float | None`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/unit/test_optimizer_area_phase.py`:

```python
"""면적 최소화 단계 - 표식, 설정, 조립."""
import json

import pytest

from analogcoder.area import DEFAULT_AREA_MODEL, total_area
from analogcoder.optimizer import AREA_OBJECTIVE, _objective_value


def test_the_area_objective_marker_is_not_a_string():
    """측정값 이름 공간과 겹칠 수 없어야 한다.

    목적 이름은 측정값 딕셔너리를 색인하는 데 쓰인다. 문자열 표식은 언젠가
    같은 이름의 진짜 measure와 부딪히고, 그 충돌은 조용하다 - 예외도 로그도
    없이 다른 양이 목적값 자리에 들어가고 탐색이 그것을 성실하게 내린다."""
    assert not isinstance(AREA_OBJECTIVE, str)
    assert AREA_OBJECTIVE != "area"


def test_the_default_area_model_is_the_shipped_total_area():
    """경계는 새로 계산하지 않는다 - 오늘의 함수를 가리킬 뿐이다."""
    assert DEFAULT_AREA_MODEL is total_area


def test_the_marker_reads_derived_area_and_a_name_reads_measurements():
    """목적값 선택 규칙 자체를 핀한다.

    오라클 밖으로 뽑는 이유는, 규칙이 오라클 안에만 있으면 시뮬레이터를
    세워야만 잴 수 있고 그러면 이 분기가 사실상 검사되지 않기 때문이다.
    덱이 `area`라는 measure를 내놓아도 표식과 섞이지 않는 것을 함께 본다."""
    measurements = {"area": 999.0, "iq_ua": 212.99}
    assert _objective_value(AREA_OBJECTIVE, measurements, derived_area=41.0) == 41.0
    assert _objective_value("iq_ua", measurements, derived_area=41.0) == 212.99
    # 없는 이름은 None이다 - 0이 아니다. 0이면 수락 규칙이 "목적값이 최선보다
    # 낮다"를 참으로 읽어 재지 못한 후보를 수락한다.
    assert _objective_value("nope", measurements, derived_area=41.0) is None
```

- [ ] **Step 2: 실패를 확인한다**

```bash
.venv/bin/python -m pytest tests/unit/test_optimizer_area_phase.py -v
```
기대: `ImportError: cannot import name 'DEFAULT_AREA_MODEL' from 'analogcoder.area'`

- [ ] **Step 3: `area.py`를 고친다**

첫 줄을 `from dataclasses import dataclass`에서 다음 두 줄로:

```python
from dataclasses import dataclass
from typing import Protocol
```

파일 **끝**에 붙인다:

```python
class AreaModel(Protocol):
    """덱 하나의 총 면적을 내는 것. **회사 이식 시 교체되는 경계다.**

    지금 저장소에는 PDK가 없어 기본 구현이 `w x l x m` 파생 근사이고, 그
    근사는 서브회로 **정의**를 N번 인스턴스화해도 1번만 센다. PDK 유도
    모델은 거의 확실히 다르게 세므로, **모델이 바뀌면 면적 단계의 결과가
    바뀐다** - 이 경계를 넘는 것은 함수 하나가 아니라 그 사실이다."""

    def __call__(self, netlist_text: str) -> AreaTotal: ...


DEFAULT_AREA_MODEL: AreaModel = total_area
```

- [ ] **Step 4: `optimizer.py`를 고친다**

`STEP_RATIO = 0.9` 바로 아래에 붙인다:

```python
class _AreaObjective:
    """목적이 **파생 면적**이라는 표식.

    문자열이 아닌 이유가 이 클래스의 전부다: 목적 이름은 측정값 딕셔너리의
    키이므로 문자열 표식은 언젠가 같은 이름의 진짜 measure와 부딪힌다."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "AREA_OBJECTIVE"


AREA_OBJECTIVE = _AreaObjective()


def _objective_value(
    objective: "str | _AreaObjective", measurements: dict, derived_area: float | None
) -> float | None:
    """목적값 하나를 고른다. 면적 표식이면 파생값, 이름이면 측정값이다."""
    if objective is AREA_OBJECTIVE:
        return derived_area
    return measurements.get(objective)
```

그리고 오라클의 마지막 줄

```python
        evaluation.objective = evaluation.measurements.get(self._spec.optimize.objective)
```

를

```python
        evaluation.objective = _objective_value(
            self._spec.optimize.objective, evaluation.measurements, evaluation.area
        )
```

로 바꾼다. (Task 2에서 `self._spec.optimize.objective` → `self._phase.objective`. 지금은 동작 불변이 목적이다.)

- [ ] **Step 5: 통과와 회귀 없음을 확인한다**

```bash
.venv/bin/python -m pytest tests/unit/test_optimizer_area_phase.py -v
.venv/bin/python -m pytest -m "not slow" -q
```
기대: 새 테스트 3 passed, 기존 테스트 0 실패

- [ ] **Step 6: 커밋**

```bash
git add src/analogcoder/area.py src/analogcoder/optimizer.py tests/unit/test_optimizer_area_phase.py
git commit -m "$(cat <<'EOF'
feat: 면적 목적 표식과 교체 가능한 면적 모델 경계

목적값을 측정값 이름으로만 고르던 자리를 _objective_value로 뽑고, 파생
면적을 목적으로 삼는 표식 AREA_OBJECTIVE를 더한다. 표식이 문자열이 아닌
이유가 핵심이다 - 목적 이름은 측정값 딕셔너리의 키이므로 문자열 표식은
언젠가 같은 이름의 진짜 measure와 조용히 충돌한다.

AreaModel 프로토콜은 회사 PDK 모델로 교체되는 경계를 표시한다. 동작은
바뀌지 않는다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N8njT49yMwXwcYsaNnW2KP
EOF
)"
```

---

### Task 2: 목적·예산·가드를 `PhaseConfig`로 데이터화

**Files:**
- Modify: `src/analogcoder/optimizer.py` (`SearchOracle.__init__`, `_search`, `_optimize`, `run_optimization`)
- Test: `tests/unit/test_optimizer_area_phase.py`

**Interfaces:**
- Consumes: `optimizer.AREA_OBJECTIVE`
- Produces: `optimizer.PhaseConfig(objective, area_budget, guard_band, label)`, `optimizer.phase_from_spec(optimize) -> PhaseConfig`, `optimizer.AREA_PHASE`, `run_optimization(netlist_texts, spec, state, agents, phase: PhaseConfig | None = None)`

`spec.optimize`는 `OptimizeSpec | None`이며, 오늘 `run_optimization`은 그것이 `None`이면 `SKIPPED`를 낸다. **면적 단계는 선언 없이 돌아야 하므로, 명시적 `phase`가 주어지면 그 조기 반환을 타지 않아야 한다.** 이것이 이 태스크에서 가장 놓치기 쉬운 한 줄이다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/unit/test_optimizer_area_phase.py`에 추가:

```python
from analogcoder.spec import OptimizeSpec


def test_phase_config_from_spec_reproduces_todays_objective_phase():
    """오늘의 전류 단계가 데이터로 정확히 표현되는지."""
    from analogcoder.optimizer import PhaseConfig, phase_from_spec

    phase = phase_from_spec(OptimizeSpec(objective="iq_ua", area_budget=1.1, guard_band=0.2))
    assert phase == PhaseConfig(
        objective="iq_ua", area_budget=1.1, guard_band=0.2, label="optimize"
    )


def test_the_area_phase_config_has_no_budget_and_no_ratio_guard():
    """면적 단계의 두 None은 서로 다른 이유를 갖는다.

    area_budget=None: 목적이 면적이고 수락 규칙이 목적의 **하강**을 요구하므로
    면적은 단조 감소한다. 예산 검사는 구조적으로 발화할 수 없고, 발화할 수 없는
    검사를 켜 두면 "검사했다"와 "검사가 무력하다"가 구별되지 않는다.

    guard_band=None: 비율 폴백은 선언에서 오는데 이 단계는 선언 없이 돈다.
    없는 숫자를 지어내지 않는다. 대신 어느 기준이 무방비인지를 Task 4가
    이벤트로 드러낸다."""
    from analogcoder.optimizer import AREA_PHASE

    assert AREA_PHASE.objective is AREA_OBJECTIVE
    assert AREA_PHASE.area_budget is None
    assert AREA_PHASE.guard_band is None
    assert AREA_PHASE.label == "optimize_area"


@pytest.mark.asyncio
async def test_an_explicit_phase_is_not_skipped_when_the_spec_declares_no_optimize(tmp_path):
    """`optimize:` 선언이 없어도 명시적 phase가 있으면 돌아야 한다.

    이것이 이 태스크의 요점이다. 오늘의 조기 반환은 "선언이 없으면 할 일이
    없다"였고, 면적 단계가 생기면 그 전제가 거짓이 된다 - 선언 없이 도는
    것이 면적 단계의 정의다. 이 한 줄을 놓치면 면적 단계가 대상 스펙 대부분에서
    조용히 SKIPPED로 끝난다."""
    from analogcoder.state import RunState
    from analogcoder.optimizer import AREA_PHASE, run_optimization
    from tests.unit.test_optimizer import DECK, _agents, _spec

    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([200.0])

    result = await run_optimization(
        {"tb": DECK}, _spec(optimize=None), state, agents, phase=AREA_PHASE
    )

    assert result["status"] != "SKIPPED"
```

`from tests.unit.test_optimizer import ...`가 임포트되지 않으면(패키지 경로 문제) 두 픽스처를 `tests/unit/_optimizer_fixtures.py`로 옮기고 양쪽이 그것을 임포트하게 한다. **복제하지 않는다.**

- [ ] **Step 2: 실패를 확인한다**

```bash
.venv/bin/python -m pytest tests/unit/test_optimizer_area_phase.py -k "phase" -v
```
기대: `ImportError: cannot import name 'PhaseConfig'`

- [ ] **Step 3: `PhaseConfig`를 더한다**

`optimizer.py`의 `_objective_value` 아래:

```python
@dataclass(frozen=True)
class PhaseConfig:
    """최적화 단계 하나의 설정. **분기가 아니라 데이터다.**

    단계가 둘이 되는 순간 `if 면적단계:`가 오라클·수락·이벤트 세 곳에
    흩어지고, 셋 중 하나를 고치지 않으면 조용히 갈라진다. 이 저장소가
    compose.py가 netlist.py의 규칙을 손으로 베껴 양방향으로 갈라진 것으로
    이미 겪은 모양이다."""

    # 문자열이면 측정값 이름, AREA_OBJECTIVE면 파생 면적.
    objective: "str | _AreaObjective"
    # None이면 예산 검사를 하지 않는다.
    area_budget: float | None
    # None이면 비율 폴백이 없다. 실측 여유분만 쓴다.
    guard_band: float | None
    # 이벤트 이름 접두사. 기존 optimize_*를 읽는 쪽이 새 단계의 이벤트를
    # 오늘의 것으로 오독하면 안 된다.
    label: str


def phase_from_spec(optimize) -> PhaseConfig:
    """오늘의 전류 단계를 데이터로. 흐르는 값이 한 글자도 다르지 않다."""
    return PhaseConfig(
        objective=optimize.objective,
        area_budget=optimize.area_budget,
        guard_band=optimize.guard_band,
        label="optimize",
    )


AREA_PHASE = PhaseConfig(
    objective=AREA_OBJECTIVE, area_budget=None, guard_band=None, label="optimize_area"
)
```

- [ ] **Step 4: 일곱 자리를 배선한다**

1~5·7은 오늘과 **같은 값**이 흐르므로 동작이 바뀌지 않는다. 6(이벤트 접두)도
전류 단계에서는 문자열이 동일하다.

1. `SearchOracle.__init__`에 `phase: PhaseConfig`를 **마지막 인자로** 더하고 `self._phase = phase`.
2. 오라클의 예산 검사를 감싼다:
   ```python
           if self._phase.area_budget is not None:
               within, budget_reason = area_within_budget(
                   evaluation.area, self._area_before, self._phase.area_budget
               )
               if not within:
                   evaluation.blocked = budget_reason
                   evaluation.blocked_by = "area_budget"
                   return evaluation
   ```
3. 오라클의 목적값 대입에서 `self._spec.optimize.objective` → `self._phase.objective`.
4. `_search`에 `phase` 인자를 더하고 `objective_name = phase.objective`로, `SearchOracle(...)` 생성에 `phase`를 넘긴다.
5. `_optimize`의 여유분 계산에서 비율 폴백을 조건부로:
   ```python
           ratio = (
               ratio_allowances(spec.all_criteria, phase.guard_band)
               if phase.guard_band is not None
               else {}
           )
   ```
   그리고 기존의 `{**ratio_allowances(...), **corner_allowances(...)}` 자리에 `ratio`를 쓴다.
6. **이벤트 이름에 `phase.label`을 접두로 붙인다.** 이것이 `label`이 존재하는
   이유이며, 붙이지 않으면 `label`은 정의만 되고 아무도 안 읽는 죽은 필드가 된다.
   더 나쁘게는, **면적 단계와 전류 단계가 한 실행에서 같은 이름의 이벤트를 쓰게
   되어** `history.jsonl`에서 구별할 수 없다. 대상은 `optimizer.py`의 `optimize_`로
   시작하는 이벤트 **10개**다:
   ```
   optimize_baseline   optimize_proposal   optimize_step        optimize_failed
   optimize_entry_sweep   optimize_confirm_sweep   optimize_bisect_probe
   optimize_bisect_result   optimize_budget_exhausted   optimize_guard_infeasible
   ```
   각 자리를 `state.log_event(f"{label}_baseline", ...)` 꼴로 바꾼다. 전류 단계는
   `label="optimize"`이므로 **문자열이 한 글자도 달라지지 않는다**(전역 제약
   "기존 이벤트 이름을 바꾸지 않는다"가 이렇게 지켜진다). 면적 단계는
   `optimize_area_baseline` 등이 된다.

   `optimize_skipped`는 **접두를 붙이지 않는다.** 그것은 단계가 정해지기 **전에**
   나오는 이벤트이고(선언이 없어 아무 단계도 안 돈다는 뜻), 붙일 `label`이 없다.

   `_bisect_last_passing`처럼 `PhaseConfig` 전체가 필요 없는 자유 함수에는
   `label: str`만 넘긴다. `_optimize`에는 `phase`를 넘긴다.

   확인 방법:
   ```bash
   grep -c 'log_event(f"{label}\|log_event(f"{phase.label}\|log_event(f"{self._phase.label}' src/analogcoder/optimizer.py
   ```
   기대: **13**. 이름은 10개이지만 `optimize_step`은 3곳, `optimize_proposal`은
   2곳에서 발화하므로 **호출 사이트 수는 13**이다(이름 수와 사이트 수를 혼동하지
   말 것). 그리고 기존 테스트가 `optimize_step` 등을 이름으로 찾고 있으므로,
   **그 테스트들이 전부 그대로 통과해야 한다** — 하나라도 깨지면 전류 단계의
   이름이 바뀐 것이다.

7. `run_optimization(netlist_texts, spec, state, agents, phase: PhaseConfig | None = None)`:
   ```python
       if phase is None:
           if spec.optimize is None:
               state.log_event("optimize_skipped", {...})   # 기존 그대로
               return {...}                                  # 기존 그대로
           phase = phase_from_spec(spec.optimize)
   ```
   **조기 반환이 `phase is None`일 때만 일어나야 한다.** 명시적 `phase`가 오면 `spec.optimize`를 보지 않는다.

- [ ] **Step 5: 이벤트 이름이 두 단계에서 갈리는지 테스트로 핀한다**

```python
@pytest.mark.asyncio
async def test_the_two_phases_do_not_share_event_names(tmp_path):
    """한 실행에 두 단계가 있으므로 이벤트 이름이 갈려야 한다.

    갈리지 않으면 history.jsonl 에서 어느 단계의 optimize_step 인지 알 수
    없고, 이 저장소의 측정 스크립트들이 두 단계를 하나로 읽는다. label 이
    존재하는 이유가 이것이며, 쓰이지 않으면 label 은 죽은 필드다."""
    from analogcoder.optimizer import AREA_PHASE, run_optimization
    from analogcoder.state import RunState
    from tests.unit.test_optimizer import DECK, _agents, _spec

    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([200.0, 190.0, 180.0])

    await run_optimization({"tb": DECK}, _spec(), state, agents)             # 전류 단계
    await run_optimization({"tb": DECK}, _spec(), state, agents, phase=AREA_PHASE)

    names = {
        json.loads(line)["step"]
        for line in open(state.history_path, encoding="utf-8")
    }
    # 전류 단계의 이름은 한 글자도 바뀌지 않았다.
    assert "optimize_baseline" in names
    # 면적 단계는 자기 이름을 쓴다.
    assert "optimize_area_baseline" in names
```

- [ ] **Step 6: 통과와 회귀 없음을 확인한다**

```bash
.venv/bin/python -m pytest tests/unit/test_optimizer_area_phase.py -v
.venv/bin/python -m pytest -m "not slow" -q
grep -c 'log_event(f"{label}\|log_event(f"{phase.label}\|log_event(f"{self._phase.label}' src/analogcoder/optimizer.py
```
기대: **기존 optimizer 테스트가 하나도 깨지지 않는다**(깨지면 전류 단계의 이벤트 이름이 바뀐 것이다), 그리고 접두 적용이 13곳(이름 10개).

- [ ] **Step 7: 커밋**

```bash
git add src/analogcoder/optimizer.py tests/unit/
git commit -m "$(cat <<'EOF'
refactor: 최적화 단계의 목적·예산·가드를 PhaseConfig 데이터로

단계가 둘이 되면 `if 면적단계:`가 오라클·수락·이벤트 세 곳에 흩어지고
셋 중 하나를 고치지 않으면 조용히 갈라진다. 분기 대신 데이터로 만든다.

가장 놓치기 쉬운 한 줄: optimize 선언이 없을 때의 조기 SKIPPED 반환은
이제 phase가 주어지지 않았을 때만 일어난다. 선언 없이 도는 것이 면적
단계의 정의이므로, 이 조건을 놓치면 면적 단계가 대상 스펙 대부분에서
조용히 SKIPPED로 끝난다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N8njT49yMwXwcYsaNnW2KP
EOF
)"
```

---

### Task 3: 노브별 면적 이득 순위 — LLM 없음

**Files:**
- Create: `src/analogcoder/area_ranking.py`
- Test: `tests/unit/test_area_ranking.py` (신규)

**Interfaces:**
- Consumes: `area.AreaModel`, `area.DEFAULT_AREA_MODEL`, `netlist.apply_changes`
- Produces:
  ```python
  @dataclass(frozen=True)
  class GainEntry:  refdes: str;  param: str;  gain: float
  @dataclass(frozen=True)
  class Ranking:    entries: list[GainEntry];  zero_gain: list[str];  unknown: list[str]
  def rank_by_area_gain(netlist_text, candidates, make_change, area_model=DEFAULT_AREA_MODEL) -> Ranking
  ```
  `candidates`는 `(refdes, param, current_value, integer)` 튜플 리스트. `make_change`는 `(refdes, param, current, integer) -> dict | None` — **한 스텝 줄인 변경 dict, 더 못 줄이면 `None`.** 스텝 규칙과 값 서식을 여기 복제하지 않기 위한 주입점이며, 호출자(`optimizer`)가 자기 `_next_value` + `_format_value`로 만든다. 콜러블 하나만 주입하는 이유는 둘을 따로 넘기면 짝이 어긋날 수 있기 때문이다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/unit/test_area_ranking.py`:

```python
"""노브별 면적 이득 순위 - 시뮬레이션도 LLM도 쓰지 않는다."""
from analogcoder.area_ranking import rank_by_area_gain
from analogcoder.optimizer import _format_value, _next_value

DECK = (
    "* t\n"
    ".subckt AMP a b vss\n"
    "Mbig  a b vss vss NCH w=100 l=10\n"
    "Msml  a b vss vss NCH w=2 l=1 nf=4\n"
    ".ends AMP\n"
    "Xa p q 0 AMP\n"
    "Vdd vdd 0 DC 1.8\n"
    ".end\n"
)


def _make_change(refdes, param, current, integer):
    """optimizer가 주입할 것과 같은 것 - 스텝 규칙을 복제하지 않는다."""
    target = _next_value(current, integer, "decrease")
    if target is None:
        return None
    return {
        "refdes": refdes,
        "param": param,
        "old_value": _format_value(current, integer),
        "new_value": _format_value(target, integer),
    }


BIG = ("AMP.Mbig", "w", 100.0, False)
SMALL = ("AMP.Msml", "w", 2.0, False)
FINGERS = ("AMP.Msml", "nf", 4.0, True)
FINGERS_AT_FLOOR = ("AMP.Msml", "nf", 1.0, True)


def test_absolute_gain_wins_not_ratio():
    """큰 소자의 10%가 작은 소자의 10%보다 앞선다.

    비율로 정렬하면 둘이 동률이 되고, 시뮬레이션 예산이 면적을 거의 못 줄이는
    후보에 먼저 쓰인다 - 이 단계의 존재 이유가 사라진다."""
    ranking = rank_by_area_gain(DECK, [SMALL, BIG], _make_change)
    assert [(e.refdes, e.param) for e in ranking.entries] == [
        ("AMP.Mbig", "w"),
        ("AMP.Msml", "w"),
    ]
    assert ranking.entries[0].gain > ranking.entries[1].gain


def test_nf_lands_in_zero_gain_and_never_in_the_ranking():
    """핑거 분할은 총 폭을 바꾸지 않으므로 면적 중립이다.

    별도의 nf 배제 규칙을 두지 않는 것이 요점이다 - 이득이 0이라는 사실이
    스스로 nf를 밀어낸다. 규칙을 손으로 적으면 그 규칙이 언젠가 진짜 면적
    중립 파라미터를 놓친다."""
    ranking = rank_by_area_gain(DECK, [FINGERS], _make_change)
    assert ranking.entries == []
    assert ranking.zero_gain == ["AMP.Msml.nf"]
    assert ranking.unknown == []


def test_zero_gain_and_unknown_are_different_lists():
    """"이득이 없다"와 "이득을 잴 수 없다"는 다른 사실이다.

    후자는 그 노브가 탐색에서 사실상 사라졌다는 뜻이고, 합쳐 두면 몇 개가
    사라졌는지 아무도 모른다."""
    ranking = rank_by_area_gain(DECK, [FINGERS_AT_FLOOR], _make_change)
    assert ranking.entries == []
    assert ranking.zero_gain == []
    assert ranking.unknown == ["AMP.Msml.nf"]


def test_a_knob_that_cannot_be_applied_is_unknown_not_a_crash():
    """적용 자체가 안 되는 노브가 단계 전체를 죽이면 안 된다."""
    ranking = rank_by_area_gain(DECK, [("NOPE", "w", 1.0, False)], _make_change)
    assert ranking.entries == []
    assert ranking.unknown == ["NOPE.w"]


def test_it_never_simulates_and_never_calls_an_llm():
    """이 모듈이 비싼 것을 부르지 않는다는 사실 자체를 핀한다.

    나중에 누군가 '더 정확한 이득'을 위해 시뮬레이션을 넣으면 167 노브 x
    시뮬레이션이 되어 이 단계가 감당 불가능해진다. 그 변경이 여기서
    깨져야 한다."""
    import analogcoder.area_ranking as mod

    source = open(mod.__file__, encoding="utf-8").read()
    for forbidden in ("simulate", "run_agent", "backend", "AgentBackend"):
        assert forbidden not in source, f"{forbidden} 이 들어왔다"
```

- [ ] **Step 2: 실패를 확인한다**

```bash
.venv/bin/python -m pytest tests/unit/test_area_ranking.py -v
```
기대: `ModuleNotFoundError: No module named 'analogcoder.area_ranking'`

- [ ] **Step 3: 구현한다**

`src/analogcoder/area_ranking.py`:

```python
"""노브별 **절대 면적 이득**을 계산해 정렬한다.

이 모듈에 LLM이 없는 것이 설계의 핵심이다. 전류 단계는 "어떤 노브가 전류를
줄이는가"를 계산할 방법이 없어 LLM 순위가 필요하지만, **면적은 계산된다** -
노브를 한 스텝 줄인 덱의 총 면적을 재면 끝이고 시뮬레이션도 필요 없다.

부수 효과 둘이 공짜로 따라온다. `nf`는 면적을 곱하지 않으므로 이득이 0이 되어
스스로 빠지고, 튜너가 키운 소자는 가장 크므로 이득도 가장 커 자동으로 앞에
온다. 둘 다 별도 규칙을 적지 않는다 - 적으면 그 규칙이 언젠가 틀린다."""

from dataclasses import dataclass
from typing import Callable

from analogcoder.area import DEFAULT_AREA_MODEL, AreaModel
from analogcoder.netlist import apply_changes


@dataclass(frozen=True)
class GainEntry:
    """한 스텝 줄였을 때의 **절대** 면적 감소량. gain은 언제나 > 0 이다."""

    refdes: str
    param: str
    gain: float


@dataclass(frozen=True)
class Ranking:
    """정렬 결과와 **빠진 것들**.

    빠진 것을 두 리스트로 나누는 이유: 이득 0은 "줄여도 면적이 안 준다"는
    사실이고, unknown은 "잴 수 없었다"는 사실이다. 합치면 탐색에서 조용히
    사라진 노브가 몇 개인지 아무도 모른다."""

    entries: list[GainEntry]
    zero_gain: list[str]
    unknown: list[str]


def rank_by_area_gain(
    netlist_text: str,
    candidates: list[tuple[str, str, float, bool]],
    make_change: Callable[[str, str, float, bool], dict | None],
    area_model: AreaModel = DEFAULT_AREA_MODEL,
) -> Ranking:
    """`candidates`는 `(refdes, param, current_value, integer)`.

    `make_change`를 **주입받는다**. 스텝 규칙(기하 x0.9, 개수 -1)과 값 서식을
    여기 복제하면 탐색이 실제로 밟는 스텝과 순위가 가정한 스텝이 갈라지고,
    그러면 순위가 일어나지 않을 이득을 기준으로 정렬한다."""
    base = area_model(netlist_text)
    entries: list[GainEntry] = []
    zero_gain: list[str] = []
    unknown: list[str] = []

    for refdes, param, current, integer in candidates:
        label = f"{refdes}.{param}"
        change = make_change(refdes, param, current, integer)
        if change is None:
            # 더 줄일 수 없는 노브. 이득 0이 아니라 잴 수 없는 것이다.
            unknown.append(label)
            continue
        try:
            moved = area_model(apply_changes(netlist_text, [change]))
        except ValueError:
            # 적용이 안 되는 노브(모호하거나 없는 refdes 등). 주소 지정
            # 게이트가 잡을 것이지만, 여기서 터지면 단계 전체가 죽는다.
            unknown.append(label)
            continue
        if moved.counted != base.counted:
            # 해소되는 소자 집합이 달라졌다 - 두 총합의 차는 이 노브의
            # 이득이 아니라 커버리지 변화다. 그것을 이득이라 부르지 않는다.
            unknown.append(label)
            continue
        gain = base.area - moved.area
        if gain <= 0.0:
            zero_gain.append(label)
            continue
        entries.append(GainEntry(refdes=refdes, param=param, gain=gain))

    # 동률에서 이름으로 갈라 놓는 것은 순서를 결정론적으로 만들기 위해서다 -
    # 순서가 실행마다 달라지면 두 실행의 차이가 탐색 때문인지 정렬 때문인지
    # 구별할 수 없다.
    entries.sort(key=lambda e: (-e.gain, e.refdes, e.param))
    return Ranking(entries=entries, zero_gain=zero_gain, unknown=unknown)
```

- [ ] **Step 4: 통과를 확인한다**

```bash
.venv/bin/python -m pytest tests/unit/test_area_ranking.py -v
.venv/bin/python -m pytest -m "not slow" -q
```
기대: 5 passed, 기존 0 실패

- [ ] **Step 5: 커밋**

```bash
git add src/analogcoder/area_ranking.py tests/unit/test_area_ranking.py
git commit -m "$(cat <<'EOF'
feat: 노브별 절대 면적 이득 순위 — LLM도 시뮬레이션도 없다

전류는 어떤 노브가 줄이는지 계산할 방법이 없어 LLM 순위가 필요하지만
면적은 계산된다. 한 스텝 줄인 덱의 총 면적을 재면 끝이다.

부수 효과 둘이 공짜다. nf는 면적을 곱하지 않아 이득 0으로 스스로 빠지고,
튜너가 키운 소자는 가장 커서 자동으로 앞에 온다. 둘 다 별도 규칙을 적지
않는다 - 적으면 그 규칙이 언젠가 틀린다.

이득 0과 unknown을 다른 리스트에 담는다. 스텝 규칙과 값 서식은 콜러블
하나로 주입받는다 - 복제하면 순위가 일어나지 않을 이득으로 정렬한다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N8njT49yMwXwcYsaNnW2KP
EOF
)"
```

---

### Task 4: 면적 단계 조립 — `run_area_optimization`

**Files:**
- Modify: `src/analogcoder/optimizer.py`
- Test: `tests/unit/test_optimizer_area_phase.py`

**Interfaces:**
- Consumes: `AREA_PHASE`, `area_ranking.rank_by_area_gain`, `SearchOracle.knob_state`, `structure.derive_structure`, `run_optimization`
- Produces: `async def run_area_optimization(netlist_texts, spec, state, agents) -> dict` — `status`는 `OPTIMIZED` / `UNCHANGED` / `REFUSED` 중 하나

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/unit/test_optimizer_area_phase.py`에 추가:

```python
UNRESOLVABLE_DECK = (
    "* t\n"
    "Rload p 0 1k\n"       # w/l 이 없어 면적 모델이 아무것도 못 읽는다
    "Vdd vdd 0 DC 1.8\n"
    ".end\n"
)


@pytest.mark.asyncio
async def test_the_area_phase_calls_no_agent_at_all(tmp_path):
    """이 단계에 LLM이 붙지 않는다는 사실을 핀한다.

    propose를 즉시 실패하는 것으로 둔다 - 나중에 누군가 "면적에도 LLM
    조언이 있으면 좋겠다"고 배선하면 이 테스트가 깨져야 한다. 안 깨지면
    LLM 없음이라는 설계의 근거가 조용히 사라진다."""
    from analogcoder.optimizer import OptimizerAgents, run_area_optimization
    from analogcoder.state import RunState
    from tests.unit.test_optimizer import DECK, _agents, _spec

    async def boom(*args, **kwargs):
        raise AssertionError("면적 단계는 에이전트를 부르면 안 된다")

    base, _ = _agents([200.0])
    agents = OptimizerAgents(propose=boom, simulate=base.simulate)
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})

    result = await run_area_optimization({"tb": DECK}, _spec(optimize=None), state, agents)

    assert result["status"] in {"OPTIMIZED", "UNCHANGED"}


@pytest.mark.asyncio
async def test_the_area_phase_records_what_it_could_not_rank(tmp_path):
    """0 이득과 unknown이 이벤트에 서로 다른 칸으로 남는지.

    무조건 남긴다 - 순위가 비어도 이벤트가 있어야 "아무것도 못 줄였다"와
    "이 단계가 없다"가 구별된다."""
    from analogcoder.optimizer import run_area_optimization
    from analogcoder.state import RunState
    from tests.unit.test_optimizer import DECK, _agents, _spec

    agents, _ = _agents([200.0])
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})

    await run_area_optimization({"tb": DECK}, _spec(optimize=None), state, agents)

    events = [json.loads(line) for line in open(state.history_path, encoding="utf-8")]
    ranked = [e for e in events if e["step"] == "optimize_area_ranking"]
    assert len(ranked) == 1
    assert set(ranked[0]) >= {"ranked", "zero_gain", "unknown", "unguarded_criteria"}


@pytest.mark.asyncio
async def test_a_deck_whose_devices_cannot_be_resolved_is_refused_not_unchanged(tmp_path):
    """`counted == 0`은 "쟀는데 못 줄임"이 아니라 "잴 수 없음"이다.

    UNCHANGED로 합치면 면적 모델이 이 덱에서 아무것도 못 읽고 있다는 사실을
    아무도 알아채지 못한다."""
    from analogcoder.optimizer import run_area_optimization
    from analogcoder.state import RunState
    from tests.unit.test_optimizer import _agents, _spec

    agents, _ = _agents([200.0])
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": UNRESOLVABLE_DECK})

    result = await run_area_optimization(
        {"tb": UNRESOLVABLE_DECK}, _spec(optimize=None), state, agents
    )

    assert result["status"] == "REFUSED"
    assert "counted" in result["reason"]
    events = [json.loads(line) for line in open(state.history_path, encoding="utf-8")]
    assert any(e["step"] == "optimize_area_refused" for e in events)
```

- [ ] **Step 2: 실패를 확인한다**

```bash
.venv/bin/python -m pytest tests/unit/test_optimizer_area_phase.py -k "area_phase or refused or agent" -v
```
기대: `ImportError: cannot import name 'run_area_optimization'`

- [ ] **Step 3: 구현한다**

`optimizer.py` 끝에. 파일 상단 import에 `from analogcoder.area import DEFAULT_AREA_MODEL`와 `from analogcoder.area_ranking import rank_by_area_gain`을 더한다.

```python
def _area_change(refdes: str, param: str, current: float, integer: bool) -> dict | None:
    """한 스텝 줄인 변경 dict. 순위 계산에 주입되는 유일한 통로다.

    `_next_value`/`_format_value`를 그대로 쓴다 - 순위가 가정하는 스텝과
    탐색이 실제로 밟는 스텝이 같아야 한다."""
    target = _next_value(current, integer, "decrease")
    if target is None:
        return None
    return {
        "refdes": refdes,
        "param": param,
        "old_value": _format_value(current, integer),
        "new_value": _format_value(target, integer),
    }


async def run_area_optimization(netlist_texts: dict[str, str], spec, state, agents) -> dict:
    """면적 최소화 단계. **선언이 필요 없고 LLM을 부르지 않는다.**

    `run_optimization`을 그대로 쓰되 둘을 바꾼다: 단계 설정을 `AREA_PHASE`로
    주고, 계산한 노브 순위를 `OptimizerAgents.knob_ranking`에 **주입**한다.
    주입된 순위가 있으면 `_knob_ranking`이 에이전트를 부르지 않으므로 LLM을
    빼기 위한 새 배선이 필요 없다.

    `counted == 0`에서 REFUSED를 내는 것은 그것이 UNCHANGED와 다른 사실이기
    때문이다 - "쟀는데 못 줄였다"와 "잴 수 없었다"를 합치면 면적 모델이 이
    덱에서 아무것도 못 읽는다는 것을 아무도 모른다."""
    canonical_name = spec.canonical.name
    start_text = netlist_texts[canonical_name]

    base = DEFAULT_AREA_MODEL(start_text)
    if base.counted == 0:
        reason = (
            f"area model resolved no device on this deck "
            f"(counted={base.counted}, skipped={base.skipped})"
        )
        state.log_event(
            "optimize_area_refused",
            {"reason": reason, "counted": base.counted, "skipped": base.skipped},
        )
        return {"status": "REFUSED", "reason": reason, "accepted": 0, "rejected": 0}

    structure = derive_structure(start_text, spec.circuit_name)
    reader = SearchOracle(
        spec, state, agents, canonical_name,
        index_baseline_components(start_text), base.area, {}, AREA_PHASE,
    )
    candidates = []
    for entry in structure.tunable:
        knob_state, _, _ = reader.knob_state(entry.refdes, entry.param)
        if knob_state is None:
            continue
        candidates.append((entry.refdes, entry.param, knob_state.value, knob_state.integer))

    ranking = rank_by_area_gain(start_text, candidates, _area_change)
    state.log_event(
        "optimize_area_ranking",
        {
            "ranked": [
                {"refdes": e.refdes, "param": e.param, "gain": e.gain} for e in ranking.entries
            ],
            "zero_gain": ranking.zero_gain,
            "unknown": ranking.unknown,
            "counted": base.counted,
            "skipped": base.skipped,
            "area_before": base.area,
            # 이 단계에는 비율 가드가 없다. 실측 여유분이 붙지 않은 기준은
            # 여유분 0으로 판정되므로 **어느 기준이 무방비인지** 드러나야
            # 한다. 여기서는 실측 여유분을 아직 모르므로 전 기준을 싣는다 -
            # 과대 보고는 읽는 사람을 놀라게 하고, 과소 보고는 속인다.
            "unguarded_criteria": [c.name for c in spec.all_criteria],
        },
    )

    area_agents = OptimizerAgents(
        propose=agents.propose,
        simulate=agents.simulate,
        verify_corners=agents.verify_corners,
        search_strategy=agents.search_strategy,
        knob_ranking=[
            {"refdes": e.refdes, "param": e.param, "direction": "decrease"}
            for e in ranking.entries
        ],
    )
    return await run_optimization(netlist_texts, spec, state, area_agents, phase=AREA_PHASE)
```

- [ ] **Step 4: 통과와 회귀 없음을 확인한다**

```bash
.venv/bin/python -m pytest tests/unit/test_optimizer_area_phase.py -v
.venv/bin/python -m pytest -m "not slow" -q
```

- [ ] **Step 5: 커밋**

```bash
git add src/analogcoder/optimizer.py tests/unit/test_optimizer_area_phase.py
git commit -m "$(cat <<'EOF'
feat: 면적 최소화 단계 조립 — 선언 없이 돌고 에이전트를 부르지 않는다

knob_ranking 주입 지점이 이미 있으므로 LLM을 빼는 새 배선을 만들지 않는다.
계산된 면적 이득 순위를 그 자리에 넣으면 _knob_ranking이 에이전트를
부르지 않는다.

counted == 0 은 REFUSED다. UNCHANGED와 합치면 면적 모델이 이 덱에서
아무것도 못 읽는다는 사실을 아무도 모른다.

이 단계는 비율 가드가 없어 실측 여유분이 없는 기준이 무방비다. 어느
기준이 그런지를 unguarded_criteria로 드러낸다 - 과대 보고 쪽에 붙였다.
과대 보고는 놀라게 하고 과소 보고는 속인다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N8njT49yMwXwcYsaNnW2KP
EOF
)"
```

---

### Task 5: `cli.py` 배선과 보고

**Files:**
- Modify: `src/analogcoder/cli.py` (PASS 분기, `run_optimization` 호출 **앞**)
- Modify: `src/analogcoder/report.py`
- Test: `tests/unit/test_report.py` (기존 파일에 추가)

**Interfaces:**
- Consumes: `optimizer.run_area_optimization`
- Produces: `result["area_optimization"]`, `report.py`의 `_area_optimization_lines(area: dict | None) -> list[str]`

`run_optimization`은 `spec.optimize is None`이면 오늘도 `SKIPPED`를 내고 이벤트를 남긴다. **그 자리를 감싸지 않는다** — 이미 올바르게 처리되고 있고, 감싸면 같은 규칙이 두 곳에 생긴다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/unit/test_report.py`에 추가(기존 파일의 임포트 관례를 따른다):

```python
def test_the_report_draws_the_area_phase_including_when_it_changed_nothing(tmp_path):
    """아무것도 못 줄인 실행에서도 절이 나와야 한다.

    안 나오면 "면적 단계가 아무것도 못 했다"와 "면적 단계가 없다"가 보고서에서
    같은 모양이 된다. 이 저장소가 코너 스윕에서 이미 겪은 실수다."""
    result = {
        "status": "PASS",
        "final_criteria": [],
        "area_optimization": {
            "status": "UNCHANGED", "accepted": 0, "rejected": 3,
            "area_before": 41.0, "area_after": 41.0,
        },
    }
    path = write_report_md(str(tmp_path), result)
    md = open(path, encoding="utf-8").read()
    assert "면적 최소화" in md
    assert "UNCHANGED" in md


def test_the_report_says_when_the_area_phase_was_refused(tmp_path):
    """REFUSED는 UNCHANGED와 다른 문장으로 나와야 한다."""
    result = {
        "status": "PASS",
        "final_criteria": [],
        "area_optimization": {
            "status": "REFUSED",
            "reason": "area model resolved no device on this deck (counted=0, skipped=2)",
        },
    }
    path = write_report_md(str(tmp_path), result)
    md = open(path, encoding="utf-8").read()
    assert "REFUSED" in md and "counted=0" in md


def test_a_run_without_an_area_phase_gets_no_area_section(tmp_path):
    """키의 부재는 "이 실행에 면적 단계가 없었다"이고, 그것은 값이 아니다."""
    path = write_report_md(str(tmp_path), {"status": "PASS", "final_criteria": []})
    assert "면적 최소화" not in open(path, encoding="utf-8").read()
```

- [ ] **Step 2: 실패를 확인한다**

```bash
.venv/bin/python -m pytest tests/unit/test_report.py -k area -v
```
기대: `AssertionError: assert '면적 최소화' in ...`

- [ ] **Step 3: `report.py`에 절 렌더러를 더한다**

`_optimization_lines` 바로 위에:

```python
def _area_optimization_lines(area: dict | None) -> list[str]:
    """면적 최소화 절. **아무것도 못 줄인 실행에서도 그린다.**

    안 그리면 "못 줄였다"와 "이 단계가 없다"가 보고서에서 같은 모양이 된다 -
    코너 스윕에서 이미 겪은 실수다. 키 자체가 없을 때만 침묵한다."""
    if area is None:
        return []
    lines = ["", "## 면적 최소화", "", f"**Status:** {area['status']}", ""]
    if area["status"] == "REFUSED":
        lines += [f"이 덱에서는 면적을 잴 수 없었다: {area.get('reason', '(사유 없음)')}", ""]
        return lines
    before, after = area.get("area_before"), area.get("area_after")
    if before is not None and after is not None:
        pct = (1.0 - after / before) * 100.0 if before else 0.0
        lines.append(f"- 면적: {before:g} → {after:g} ({pct:.2f}% 감소)")
    lines.append(f"- 수락 {area.get('accepted', 0)}건 / 거절 {area.get('rejected', 0)}건")
    lines.append("")
    return lines
```

`write_report_md`에서 `_optimization_lines(...)`를 붙이는 자리 **바로 앞**에 `lines += _area_optimization_lines(result.get("area_optimization"))`를 넣는다.

- [ ] **Step 4: `cli.py`에 배선한다**

`if result["status"] == "PASS":` 블록에서, `probe_frozen`을 세우는 `try:` **안**, 기존 `optimization = await run_optimization(` **앞**에:

```python
                # 면적 단계가 **먼저** 돈다. 전류 단계는 선언이 있을 때만 도는데,
                # 뒤에 두면 선언 없는 스펙에서 면적 단계가 영영 안 도는 배선이
                # 되기 쉽다. probe_frozen 안에 있는 이유는 전류 단계와 같다 -
                # 탐색 도중의 탐침 승격은 서로 다른 코너 집합에서 잰 목적값을
                # 비교하게 만든다.
                result["area_optimization"] = await run_area_optimization(
                    state.current_netlist_texts(),
                    spec,
                    state,
                    OptimizerAgents(
                        propose=propose_candidates_fn,
                        simulate=simulate_for_run,
                        verify_corners=verify_corners_fn if corner_capable else None,
                    ),
                )
                # 이 단계도 버전을 밀고 되돌린다. 다음 단계는 그것이 착지한
                # 덱에서 출발해야 하므로 아래 호출은 current_netlist_texts()를
                # 다시 읽는다(이미 그렇게 되어 있다).
                result["final_netlist_paths"] = state.current_netlist_paths()
```

`from analogcoder.optimizer import ... run_area_optimization`을 임포트에 더한다.

- [ ] **Step 5: 통과와 회귀 없음을 확인한다**

```bash
.venv/bin/python -m pytest tests/unit/test_report.py -v
.venv/bin/python -m pytest -m "not slow" -q
```

- [ ] **Step 6: 골든 패스 종단 확인**

```bash
rm -rf runs/area_phase_smoke
.venv/bin/analogcoder --spec benchmarks/inverting_amp/spec.yaml --run-dir runs/area_phase_smoke
grep -c optimize_area runs/area_phase_smoke/history.jsonl
grep -n "면적 최소화" runs/area_phase_smoke/report.md
```

`inverting_amp`은 이상적 op-amp(VCVS)라 `w`/`l`을 가진 소자가 없을 수 있다. **그러면 `REFUSED`가 정답이고**, 보고서에 그 문장이 있어야 한다. `UNCHANGED`가 나오면 오히려 잘못된 것이다 — 잴 수 없는 덱을 쟀다고 말하는 것이므로.

- [ ] **Step 7: 커밋**

```bash
git add src/analogcoder/cli.py src/analogcoder/report.py tests/unit/test_report.py
git commit -m "$(cat <<'EOF'
feat: 면적 최소화 단계를 파이프라인에 배선하고 보고서에 그린다

면적 단계는 전류 단계 앞에서 무조건 돈다. 전류 단계는 선언이 있을 때만
돌므로 뒤에 두면 선언 없는 스펙에서 면적 단계가 영영 안 도는 배선이
되기 쉽다.

보고서는 아무것도 못 줄인 실행에서도 절을 그린다. 안 그리면 "면적 단계가
아무것도 못 했다"와 "면적 단계가 없다"가 같은 모양이 된다 - 코너 스윕에서
이미 겪은 실수다. REFUSED는 UNCHANGED와 다른 문장으로 나온다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N8njT49yMwXwcYsaNnW2KP
EOF
)"
```

---

### Task 6: bandgap 실측 — 면적이 실제로 주는가

**Files:**
- Create: `tests/unit/test_optimizer_area_phase_ngspice.py`
- Modify: `docs/superpowers/plans/2026-08-02-area-optimization-phase.md` (아래 표), `CLAUDE.md`(테스트 시간 줄)

이 태스크가 이 계획의 **결과**다. 앞의 다섯은 배선이고, 여기서 나오는 숫자가 2단계의 기준선이 된다.

- [ ] **Step 1: 테스트를 쓴다**

```python
"""면적 최소화 단계의 bandgap 실측. ngspice가 PATH에 있다고 가정한다.

수치를 못 박지 않는 이유: 이 값이 2단계(게이트 강등 + 대안 정렬)의 기준선이
되어야 하고, 2단계의 목적이 바로 그것을 **바꾸는 것**이다. 값을 핀하면 2단계가
성공할 때마다 이 테스트가 깨진다. 핀하는 것은 방향과 부작용 없음뿐이다."""
import pytest

from analogcoder.area import total_area
from analogcoder.optimizer import OptimizerAgents, run_area_optimization
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.state import RunState

pytestmark = pytest.mark.slow

SPEC = "benchmarks/bandgap/spec.yaml"


@pytest.mark.asyncio
async def test_the_area_phase_reduces_area_on_bandgap_without_breaking_criteria(tmp_path, capsys):
    spec = load_spec(SPEC)
    state = RunState(
        run_dir=str(tmp_path), testbench_names=[tb.name for tb in spec.testbenches]
    )
    texts = {tb.name: open(tb.netlist_path, encoding="utf-8").read() for tb in spec.testbenches}
    state.push_netlist_version(texts)
    backend = NgspiceBackend()

    async def simulate(netlist_texts, spec_arg):
        measurements = {}
        for tb in spec_arg.testbenches:
            raw = backend.run(netlist_texts[tb.name], tb.control_block)
            measurements.update(raw.measurements)
        return {"measurements": measurements, "status": "success", "warnings": []}

    before = total_area(texts[spec.canonical.name]).area
    result = await run_area_optimization(
        texts, spec, state, OptimizerAgents(propose=None, simulate=simulate)
    )

    assert result["status"] in {"OPTIMIZED", "UNCHANGED"}
    after = total_area(state.current_netlist_texts()[spec.canonical.name]).area
    # 방향만 핀한다. 커지는 일은 절대 없어야 한다 - 수락 규칙이 목적의
    # 하강을 요구하므로, 커졌다면 규칙이 우회된 것이다.
    assert after <= before
    if result["status"] == "OPTIMIZED":
        assert after < before
    with capsys.disabled():
        print(f"\nAREA {before:.6g} -> {after:.6g}  ({(1 - after / before) * 100:.2f}% 감소)")
        print(f"수락 {result.get('accepted')} / 거절 {result.get('rejected')}")
```

`NgspiceBackend.run`의 정확한 시그니처는 `src/analogcoder/simulators/ngspice.py`에서 확인해 맞춘다. 기존 `tests/unit/*_ngspice.py`가 이미 이 배선을 하고 있으면 **그 패턴을 그대로 복사한다** — 새 배선을 발명하지 않는다.

- [ ] **Step 2: 돌린다**

```bash
.venv/bin/python -m pytest tests/unit/test_optimizer_area_phase_ngspice.py -v -s
```

- [ ] **Step 3: 나온 숫자를 아래 표에 적는다**

**비워 두지 않는다** — 비면 2단계가 비교할 대상이 없다. `two_stage_opamp` 행도 같은 테스트를 스펙만 바꿔 한 번 더 돌려 채운다.

- [ ] **Step 4: 테스트 시간 예산을 재측정한다**

`CLAUDE.md`의 테스트 시간 줄이 이미 세 번 밀렸다. 새 파일을 더했으니 다시 잰다:

```bash
time .venv/bin/python -m pytest -m "not slow" -q
```

**드리프트 가드 갱신 순서를 지킨다** — 먼저 새 입력이 통과하는지 확인하고, 그다음에 숫자를 올린다. 반대로 하면 가드가 주석이 된다.

- [ ] **Step 5: 커밋**

```bash
git add tests/unit/test_optimizer_area_phase_ngspice.py docs/superpowers/plans/ CLAUDE.md
git commit -m "$(cat <<'EOF'
test: bandgap에서 면적 최소화 단계 실측 — 2단계의 기준선

수치를 테스트에 못 박지 않는다. 이 값은 2단계(게이트 강등 + 대안 정렬)의
기준선이고 2단계의 목적이 그것을 바꾸는 것이므로, 값을 핀하면 2단계가
성공할 때마다 이 테스트가 깨진다. 핀하는 것은 방향과 부작용 없음뿐이다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N8njT49yMwXwcYsaNnW2KP
EOF
)"
```

---

## 측정된 기준선 (Task 6에서 채운다)

`tests/unit/test_optimizer_area_phase_ngspice.py`로 실측 (2026-08-02, 로컬 ngspice,
`.venv/bin/python -m pytest tests/unit/test_optimizer_area_phase_ngspice.py -v -s`).
`unguarded_criteria` 열은 컨트롤러 추가 지시 C — `_optimize`가 남기는
`optimize_area_baseline` 이벤트의 `unguarded_criteria` 길이 / 스펙의 전체 기준 수.

| 스펙 | 면적 before | 면적 after | 감소율 | 수락/거절 | zero_gain | unknown | unguarded_criteria | 소요 |
|---|---|---|---|---|---|---|---|---|
| `benchmarks/bandgap/spec.yaml` | 1.10546e-08 | 8.93292e-09 | 19.19% | 16/4 (steps_accepted/steps_rejected) | 1 (`BGR_CORE.Xq8.m`) | 0 | 22/22 | 123.5s (21 sims, 5 testbenches/step) |
| `benchmarks/two_stage_opamp/spec.yaml` | 2.38037e-10 | 2.38037e-10 | 0.00% (UNCHANGED) | 0/20 | 7 (`Lfb.value`, `Cin.value`, `Cload.value`, `OPAMP2STAGE.Rdeg.value`, `OPAMP2STAGE.Rstart.value`, `OPAMP2STAGE.Xcc.mf`, `OPAMP2STAGE.Xca.mf`) | 0 | 7/7 | 24.1s (21 sims, 4 testbenches/step) |

예상과 다른 점: bandgap은 예상대로 `unguarded_criteria`가 22/22(전부)였지만,
**두 스펙 모두 status가 정확히 예측되지 않았다** — bandgap은 OPTIMIZED로
19.19% 줄었고(83개 소자 중 82개가 면적 이득을 갖고, `BGR_CORE.Xq8.m`만
zero_gain), two_stage_opamp는 20스텝을 전부 시도하고도 UNCHANGED로 끝났다.
zero_gain 7개 전부가 면적 모델이 w/l/m으로 세지 않는 값 하나짜리 소자
(`Lfb`/`Cin`/`Cload`의 `value`, `Rdeg`/`Rstart`의 `value`, `Xcc`/`Xca`의
`mf`)라 순위에 오를 후보 자체가 애초에 `counted=13`짜리 소자 목록에서
갈 곳이 적었다는 뜻이다.

**거절 사유를 직접 읽으면 두 스펙 모두 무방비 가드(여유분 0)로 거절된
스텝이 하나도 없다.** `optimize_area_step`의 `reason`을 모두 확인했다 —
bandgap의 4건(`BUF_N.Xcc`/`BUF_P.Xcc`의 `L`/`W`)과 two_stage_opamp의
20건 전부가 `"criteria no longer pass: one or more criteria failed"`이고,
`accept_step`(optimizer.py:557-560)에서 이것은 `evaluation.violations`
(가드 위반, unguarded_criteria가 여유분 0으로 만드는 그 검사)보다 **먼저**
검사되는 `overall_pass`가 그냥 False였다는 뜻이다 — 즉 이 실행에서는
무방비 가드가 한 번도 실제로 발화하지 않았다. 22/22와 7/7이라는
`unguarded_criteria` 수는 "안전이 느슨해진 위험이 이만큼 존재한다"는
사실이지 "이번 거절이 그 위험 때문이었다"는 사실이 아니다 — 이 표를
읽는 2단계 구현자가 후자로 오독하지 않도록 남긴다. 2단계가 "게이트
강등 + 대안 3개"를 붙여야 할 이유는 여전히 유효하다: 순위 1위 소자를
줄이면 곧바로 기준이 깨지는 스텝이 이렇게 많다는 것 자체가, 대안 없이
1위만 시도하는 오늘의 탐색이 얕다는 증거다.

**정정 (2026-08-02, 전체 브랜치 리뷰).** 위 문단의 결론 — "무방비 가드가
한 번도 실제로 발화하지 않았다" — 은 **뒤집혔다.** 지우지 않고 정정으로
남긴다: 원래 추론이 무엇을 놓쳤는지가 다음에 같은 질문을 다시 던질 사람에게
필요하기 때문이다.

원래 추론은 **거절**만 읽었다: "24건 전부가 `overall_pass` 자체가 깨진
경우이니 무방비 가드는 발화하지 않았다"는 것. 그런데 여유분 0(=무방비)일 때
`guard_band_violations`(`judge_tools.py:74-88`)의 `limit`은 `threshold -
0.0 = threshold`가 되고, 이것은 `accept_step`(`optimizer.py:559-564`)이 그
한 줄 앞에서 이미 검사한 `overall_pass`와 **산술적으로 같은 술어**다. 즉
무방비 가드는 **거절에는 구조적으로 나타날 수 없고 수락에만 나타난다** -
거절 경로를 읽는 것은 이 위험이 원리적으로 보이지 않는 자리를 읽는 것이었다.

실제 사례는 수락 쪽에 있었다: `benchmarks/bandgap/spec.yaml`에서 이 실행이
**수락한** 16스텝 동안

```
buf0_phase_margin   104.39° → 81.89°   (>= 80.0°)   relative slack 0.305 → 0.024
buf1_phase_margin   101.56° → 82.99°   (>= 80.0°)   relative slack 0.269 → 0.037
```

로 마진이 드레인됐다 - 둘 다 통과는 유지했지만(그래서 `overall_pass`는 계속
True였다) 코너 없는 스펙이라 아무도 이 드리프트를 다시 확인하지 않았고,
실행은 PASS로 끝났다. 이것이 "위험의 크기는 쟀고 사례는 못 쟀다"던 자리의
그 사례다.

교훈: 무방비 가드처럼 "통과 판정 자체와 같은 술어가 되는" 종류의 가드는,
그 가드가 실제로 무언가를 걸러냈는지를 **거절 로그에서 찾으면 안 된다** -
정의상 걸러낼 수 없는 자리에서 찾는 것이기 때문이다. 대신 그 가드 없이는
수락되지 않았을 **수락**을 찾아야 한다.

이 정정이 Critical 수정(`unguarded_criteria`를 `result.json`/`report.md`로
끌어오는 것)의 근거다 - history.jsonl 한 곳에만 있던 사실이 실행 하나만
보고 드러나야 위 발견이 다음 실행에서는 코드를 읽지 않고도 보인다.

이 표가 **2단계 계획의 입력**이다.

## 이 계획이 다루지 않는 것

설계 문서의 3단계 분해에 따라 나머지 둘은 별도 계획으로 간다.

- **2단계**: 면적 게이트 강등, 튜너 대안 3개, 선택 규칙(통과한 것 중 면적 최소 / 없으면 개선량 최대)
- **3단계**: 파레토 공선과 보고

## 스펙에 없던 결정 하나 — **되돌려짐 (2026-08-02)**

> **이 절의 "확정됨"은 되돌려졌다.** 아래 본문은 당시 확정한 내용 그대로 남기고
> (증거를 지우지 않는다), 되돌림의 근거와 그 이후를 절 끝에 적는다.

설계 문서는 **면적 단계의 가드 밴드**를 다루지 않았다. 세 안 중 다음으로 확정했다.

> **면적 단계는 비율 가드(`guard_band`)를 갖지 않는다.** 실측 코너 여유분이 있는
> 기준은 그것으로 보호되고, **없는 기준은 여유분 0으로 판정된다** — 즉 "통과하기만
> 하면 된다". 무방비인 기준의 이름을 **전부** `unguarded_criteria`로 기록한다.

**이것은 안전을 느슨하게 하는 쪽이며, 그 사실을 여기 명시적으로 남긴다.**
`optimize:` 선언도 코너 스윕도 없는 스펙에서는 **모든 기준이 무방비**가 된다.

기각한 두 대안과 이유:

- **(a) 면적 단계에 별도 `guard_band` 선언을 요구한다** — "선언 없이 자동으로
  돈다"가 깨진다. 그것이 이 단계의 정의이므로 정의를 포기하는 대가다.
- **(b) 코너 스윕이 없으면 면적 단계를 아예 안 돌린다** — 지금 벤치마크 14개 중
  코너를 선언한 것은 소수이므로, 이 단계가 대부분의 스펙에서 영영 안 돈다.
  "안전하지만 아무 데서도 안 도는 기능"은 기능이 아니다.

**대신 침묵을 금지하는 것으로 값을 치른다.** 이 저장소의 규율은 위험을 없애는
것이 아니라 **보이게 하는 것**이므로, `unguarded_criteria`는 비어 있어도 키가
존재해야 하고(빈 리스트와 키 부재는 다른 사실이다), 보고서는 무방비 기준의
개수를 적는다. 어느 기준이 여유분 없이 판정됐는지가 실행 하나만 보고 드러나야
한다.

이 결정을 되돌려야 한다는 증거는 하나다: **면적 단계가 수락한 스텝이 나중 코너
스윕에서 기준을 깨뜨리는 일이 실제로 발생하는 것.** 최적화 단계에는 이미 코너
확인과 이분 탐색이 있으므로 그 사건은 잡히고 기록된다 — 그때 다시 연다.

### 되돌림 (2026-08-02)

**그 사건이 실제로 발생했고, 사전 등록된 규칙이 발화했다.**
`docs/superpowers/specs/2026-08-02-area-phase-guard-measurement-results.md`:
무방비 상태로 돈 면적 단계가 `benchmarks/bandgap/spec.yaml`에서 16 스텝을
수락해 면적을 19.19% 줄였고(1.10546e-08 → 8.93292e-09), 착지 덱의 45 코너
스윕이 **22개 중 2개**를 실패했다 — `buf0_phase_margin` 65.10° @ `sf/1.98/125`,
`buf1_phase_margin` 76.07° @ `sf/1.62/-40`. **면적 단계 이전 덱은 같은 45 코너를
전부 통과한다**(대조군을 재고 나서야 귀속을 말할 수 있다). 명목 한 점에서는
둘 다 통과했으므로 `accept_step`은 16 스텝을 전부 수락했다 — 실패 모양은
"가드가 느슨했다"가 아니라 **"명목 한 점의 통과가 코너의 통과를 예측하지
못한다"**이다.

되돌림의 **방향**(여유분 하한 도입)은 정해졌지만 **값은 정해지지 않았다.**
후속 사전 등록(`2026-08-02-area-phase-margin-floor-design.md`)이 세 규칙 계열
× 고정 격자 × 2 쌍 = 14 조합을 돌렸고, 그 결과
(`2026-08-02-area-phase-margin-floor-results.md`)는 **판정 규칙 3 발화 —
채택 없음**이다. `AREA_PHASE.margin_floor`는 `None`으로 남아 있고, 대안은 또
다른 사전 등록으로 열어야 한다.

**그러므로 지금 코드의 상태는 위 본문과 같다(비율 가드 없음).** 되돌려진 것은
코드가 아니라 **그 상태를 "확정"이라고 부른 판단**이다. 관측 수단은 원래
트리거(코너 스윕에서 깨진다)에서 **실행 하나 안에서 읽히는 상대 여유의
최솟값**(`result["area_optimization"]["tightest_slack"]`, `report.md`의 면적
최소화 절)으로 교체됐다 — 원래 트리거는 코너를 선언하지 않은 스펙, 즉 위험이
있는 바로 그곳에서 관측 불가능했다.
