# 조합 스텝 탐색 구현 계획

> **에이전트 작업자에게:** 필수 하위 스킬 — 이 계획은 superpowers:subagent-driven-development 로 태스크 단위 실행한다.

**목표:** 좌표별 하강이 거절한 지점에서 **부호가 섞인 2노브 스텝**을 시도하는 탐색 전략을 추가하고, 사전 등록된 격자로 9런을 재서 채택/기각한다.

**설계 문서(잠김, 절대 수정 금지):** `docs/superpowers/specs/2026-08-02-compound-step-search-design.md` — 개정 1 포함. **규칙을 여기에 다시 쓰지 말고 그 문서를 읽고 따른다.**

**기술 스택:** Python, ngspice, 기존 `optimizer.py` 탐색 이음매.

## Global Constraints

- **잠긴 사전 등록을 편집하지 않는다.** 값·격자·판정 규칙은 그 문서가 정한다.
- **새 상수를 도입하지 않는다.** 확대 폭은 `_next_value(v, integer, "increase")` 가 이미 준다(`current / STEP_RATIO`). `MAX_OPTIMIZE_STEPS` 를 늘리지 않는다.
- **`AREA_PHASE` 를 바꾸지 않는다.** 전략은 `OptimizerAgents.search_strategy` 로 주입한다.
- **기본 동작 불변:** `search_strategy=None` 이면 `coordinate_descent` 그대로. 이것을 깨는 변경은 실패다.
- 테스트는 노드 ID 단위 전경 실행. 전체 스위트는 태스크 5 에서 한 번만.
- 문서는 한글.

## 파일 구조

| 파일 | 책임 |
|---|---|
| `src/analogcoder/optimizer.py` | `_compound_fallback(partners)` 팩토리, `SEARCH_STRATEGIES` 등록 |
| `tests/unit/test_optimizer_search_strategy.py` (신규) | 전략 단위 테스트 |
| `benchmarks/two_stage_opamp/spec_search_slot.yaml` (신규) | 슬롯 C |
| `scripts/search_ab.py` | `--phase area` 추가, 적격성 선행 확인 |
| `docs/superpowers/specs/2026-08-02-compound-step-search-results.md` (신규) | 결과 |
| `CLAUDE.md` | 규칙으로 기록 |

---

### Task 1: `compound_fallback` 전략

**Files:**
- Modify: `src/analogcoder/optimizer.py` (`coordinate_descent` 바로 아래, `SEARCH_STRATEGIES` 위)
- Test: `tests/unit/test_optimizer_search_strategy.py` (신규)

**Interfaces:**
- Consumes: `Knob(refdes, param, direction)`, `KnobState(token, value, integer)`, `ProposedStep(knob, state, value)`, `StepOutcome(accepted, reason, objective)`, `SearchRun.knobs/spend_step/knob_state/attempt/exhausted`, `_next_value(current, integer, direction) -> float | None` (모두 frozen dataclass, 이미 존재)
- Produces: `_compound_fallback(partners: int) -> SearchStrategy`, `SEARCH_STRATEGIES["compound_fallback_1"]`, `["compound_fallback_3"]`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/unit/test_optimizer_search_strategy.py
import pytest
from analogcoder import optimizer as opt


class FakeRun:
    """SearchRun 중 전략이 쓰는 표면만 흉내낸다.

    `accept` 는 (refdes, param) 튜플의 frozenset -> bool. 이 제안 집합이
    수락되는가를 시험이 정한다."""

    def __init__(self, knobs, accept, budget=50):
        self.knobs = knobs
        self._accept = accept
        self._budget = budget
        self.attempts = []          # list[list[ProposedStep]]
        self.exhausted_calls = []

    def spend_step(self, knob):
        if self._budget <= 0:
            return False
        self._budget -= 1
        return True

    def knob_state(self, knob):
        return opt.KnobState(token=knob.param, value=10.0, integer=False)

    def exhausted(self, knob, state, reason):
        self.exhausted_calls.append((knob, reason))

    async def attempt(self, steps):
        self.attempts.append(list(steps))
        key = frozenset((s.knob.refdes, s.knob.param) for s in steps)
        ok = self._accept(key)
        return opt.StepOutcome(accepted=ok, reason=None if ok else "no", objective=1.0)


def _knobs(*names):
    return [opt.Knob(refdes=n, param="W", direction="decrease") for n in names]


@pytest.mark.asyncio
async def test_partners_zero_is_byte_for_byte_coordinate_descent():
    """`partners=0` 이 기존 전략과 같다는 것은 주장이 아니라 시험 대상이다.

    사전 등록이 대조군으로 `coordinate_descent` 자신을 쓰기로 한 이유가
    이것이다 - 같다고 적어 두고 다른 코드를 돌리면 A/B 의 대조군이 A/B 밖에
    있게 된다."""
    knobs = _knobs("A", "B", "C")
    accept = lambda key: key == frozenset({("A", "W")})  # A만 단독 수락

    base = FakeRun(_knobs("A", "B", "C"), accept, budget=8)
    await opt.coordinate_descent(base)

    comp = FakeRun(knobs, accept, budget=8)
    await opt._compound_fallback(0)(comp)

    assert [[(s.knob.refdes, s.value) for s in a] for a in comp.attempts] == \
           [[(s.knob.refdes, s.value) for s in a] for a in base.attempts]


@pytest.mark.asyncio
async def test_a_rejected_knob_is_retried_paired_with_the_next_ranked_knob():
    knobs = _knobs("A", "B")
    # A 단독은 거절, {A,B} 조합은 수락
    accept = lambda key: key == frozenset({("A", "W"), ("B", "W")})
    run = FakeRun(knobs, accept, budget=6)
    await opt._compound_fallback(1)(run)

    pairs = [a for a in run.attempts if len(a) == 2]
    assert pairs, "조합 스텝이 한 번도 시도되지 않았다"


@pytest.mark.asyncio
async def test_the_partner_moves_in_the_opposite_direction():
    """이 전략의 전부다. 두 노브를 같은 방향으로 움직이면 면적 단계에서는
    원리적으로 거절을 구제할 수 없다 - 축소로 깨진 기준은 둘을 같이 축소하면
    더 깨진다. 설계 문서가 초안에서 그 형태를 빼면서 남긴 이유다."""
    knobs = _knobs("A", "B")
    run = FakeRun(knobs, lambda key: len(key) == 2, budget=6)
    await opt._compound_fallback(1)(run)

    pair = next(a for a in run.attempts if len(a) == 2)
    lead, partner = pair[0], pair[1]
    assert lead.knob.direction == "decrease"
    assert partner.knob.direction == "increase"
    assert lead.value < lead.state.value      # 줄었다
    assert partner.value > partner.state.value  # 늘었다


@pytest.mark.asyncio
async def test_compound_attempts_spend_the_same_budget():
    """예산을 늘리면 '조합이 좋아서'와 '더 많이 시도해서'를 가를 수 없다."""
    knobs = _knobs("A", "B", "C", "D")
    run = FakeRun(knobs, lambda key: False, budget=3)   # 전부 거절
    await opt._compound_fallback(3)(run)
    assert len(run.attempts) == 3, "조합 시도가 예산을 쓰지 않았다"
```

- [ ] **Step 2: 실패를 확인한다**

```
.venv/bin/python -m pytest tests/unit/test_optimizer_search_strategy.py -q
```
기대: `AttributeError: module 'analogcoder.optimizer' has no attribute '_compound_fallback'`

- [ ] **Step 3: 최소 구현**

`coordinate_descent` 정의 **직후**, `SEARCH_STRATEGIES` 표 **직전**에 넣는다.

```python
def _compound_fallback(partners: int) -> SearchStrategy:
    """좌표별 하강 + 거절 시 **부호가 섞인** 2노브 스텝 되시도.

    왜 반대 방향인가: 면적 단계는 목적이 면적이므로 순위의 모든 방향이
    "decrease"다. 어떤 노브를 축소해서 기준이 깨졌다면 둘을 같이 축소하면
    **더** 깨진다 - 같은 방향 조합은 실행되고 로그도 남지만 거절을 구제할 수
    없다. 좌표별 하강이 결합 문제에서 막히는 이유는 개선 방향이 부호가 섞인
    대각선이기 때문이고(밀러 캡을 줄이되 출력단을 키운다), 축만 따라가는
    탐색은 그 방향을 원리적으로 보지 못한다.

    확대 폭은 새 상수가 아니다 - `_next_value`가 이미 `direction="increase"`를
    `current / STEP_RATIO`로 처리한다. 순 면적이 떨어져야 한다는 것은
    `accept_step`이 이미 요구하므로 여기서 검사하지 않는다.

    `partners=0`은 `coordinate_descent`와 같아야 하며, 그것은 주장이 아니라
    `test_partners_zero_is_byte_for_byte_coordinate_descent`가 못박는다."""

    async def strategy(run: SearchRun) -> None:
        for index, knob in enumerate(run.knobs):
            while True:
                if not run.spend_step(knob):
                    return
                state = run.knob_state(knob)
                if state is None:
                    break
                value = _next_value(state.value, state.integer, knob.direction)
                if value is None:
                    run.exhausted(
                        knob,
                        state,
                        f"{knob.refdes}.{knob.param} cannot move further in "
                        f"direction {knob.direction!r}",
                    )
                    break
                outcome = await run.attempt([ProposedStep(knob, state, value)])
                if outcome.accepted:
                    continue
                if not await _try_partners(run, index, knob, state, value, partners):
                    break

    return strategy


async def _try_partners(
    run: SearchRun,
    index: int,
    knob: Knob,
    state: KnobState,
    value: float,
    partners: int,
) -> bool:
    """순위상 다음 `partners` 개를 **반대 방향**으로 짝지어 시도한다.

    상대를 결합 스캔에서 고르지 않는 이유: 스캔은 덱 하나·테스트벤치 하나에만
    있고, 스캔을 전제하는 전략은 스캔이 없는 덱에서 돌 수 없다. 순위는 모든
    실행이 이미 만든다."""
    for partner in run.knobs[index + 1 : index + 1 + partners]:
        if not run.spend_step(partner):
            return False
        partner_state = run.knob_state(partner)
        if partner_state is None:
            continue
        opposite = "increase" if knob.direction == "decrease" else "decrease"
        partner_value = _next_value(partner_state.value, partner_state.integer, opposite)
        if partner_value is None:
            continue
        outcome = await run.attempt(
            [
                ProposedStep(knob, state, value),
                ProposedStep(replace(partner, direction=opposite), partner_state, partner_value),
            ]
        )
        if outcome.accepted:
            return True
    return False
```

`replace` 는 **이미 import 되어 있다**(`optimizer.py:2`,
`from dataclasses import dataclass, field, replace` — 확인함). 새 import 없음.
`@pytest.mark.asyncio` 가 이 저장소의 async 테스트 관례다(`test_optimizer_area_phase.py`
전체가 그렇다 — 확인함).

- [ ] **Step 4: 레지스트리에 등록**

```python
SEARCH_STRATEGIES: dict[str, SearchStrategy] = {
    "coordinate_descent": coordinate_descent,
    # 사전 등록 격자 partners ∈ {0,1,3}. 0은 coordinate_descent와 같으므로
    # 표에 넣지 않는다 - 같은 것을 두 이름으로 넣으면 A/B 표에 대조군이 둘로
    # 보인다. 동일성은 단위 테스트가 못박는다.
    "compound_fallback_1": _compound_fallback(1),
    "compound_fallback_3": _compound_fallback(3),
}
```

- [ ] **Step 5: 통과 확인 + 기본 경로 회귀**

```
.venv/bin/python -m pytest tests/unit/test_optimizer_search_strategy.py tests/unit/test_optimizer.py tests/unit/test_optimizer_area_phase.py -q
```

- [ ] **Step 6: 커밋**

```bash
git add src/analogcoder/optimizer.py tests/unit/test_optimizer_search_strategy.py
git commit -m "feat: 조합 스텝 탐색 전략 — 거절된 노브를 반대 방향 상대와 짝짓는다"
```

---

### Task 2: 슬롯 C 스펙 authoring

**Files:**
- Create: `benchmarks/two_stage_opamp/spec_search_slot.yaml`
- Test: `tests/unit/test_spec.py` (드리프트 가드 확인만)

**Interfaces:** Consumes `benchmarks/two_stage_opamp/spec_pvt.yaml`. Produces 슬롯 C.

- [ ] **Step 1: 기준선 실측값을 확인한다** (임계값을 규칙으로 유도하기 위해)

```
.venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from pathlib import Path
from analogcoder.spec import load_spec
from analogcoder.netlist import resolve_includes
from analogcoder.simulators.ngspice import NgspiceBackend
s=load_spec('benchmarks/two_stage_opamp/spec_pvt.yaml'); b=NgspiceBackend()
tb=s.testbenches[0]
t=resolve_includes(Path(tb.netlist_path).read_text(), str(Path(tb.netlist_path).parent))
Path('/tmp/sc.cir').write_text(t)
print(b.run('/tmp/sc.cir', {'control_block': tb.control_block}).measurements)
"
```
기대: `phase_margin_deg` ≈ **34.5636**.

- [ ] **Step 2: 스펙을 만든다**

`spec_pvt.yaml` 을 그대로 복사하고 **두 곳만** 바꾼다:
1. `phase_margin` 의 `threshold: 60.0` → **`30.0`** (사전 등록 규칙: 기준선 실측 34.5636 을 5° 단위 내림)
2. 파일 머리에 주석으로 — 이것이 탐색 단계용 슬롯이고, 튜닝 루프 벤치마크가 아니며, **여기서 나온 면적 절감률을 출하 스펙의 성능으로 인용하면 안 된다**는 것

다른 기준·테스트벤치·코너 격자는 **한 글자도 바꾸지 않는다.**

- [ ] **Step 3: 로드되고 기준선이 통과하는지 확인**

```
.venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from analogcoder.spec import load_spec
s=load_spec('benchmarks/two_stage_opamp/spec_search_slot.yaml')
print('테스트벤치',len(s.testbenches),'기준',len(s.all_criteria),'코너',len(s.pvt_corners.corners))
"
```
기대: 코너 45, 기준 7.

- [ ] **Step 4: 드리프트 가드**

`test_every_shipped_benchmark_control_block_is_accepted` 가 스펙 수를 센다.
**순서 구속:** 먼저 새 입력이 게이트를 통과하는지 확인하고, **그다음** 숫자를 올린다.

```
.venv/bin/python -m pytest tests/unit/ -k control_block_is_accepted -q
```
실패하면 그 실패 메시지가 새 기대값을 알려준다. 0 거절을 확인한 뒤에만 숫자를 고친다.

- [ ] **Step 5: 커밋**

---

### Task 3: 하니스에 면적 단계 추가

**Files:**
- Modify: `scripts/search_ab.py`

**Interfaces:** Consumes `optimizer.run_area_optimization(netlist_texts, spec, state, agents)`, `SEARCH_STRATEGIES`. Produces `--phase {objective,area}`.

- [ ] **Step 1: 현재 구조를 읽는다**

`run_side` 가 `run_optimization` 을 직접 부르고, `verify_corners` 를
`spec.pvt_corners is not None` 일 때만 배선한다(204-220 근처). 그 배선은 그대로 쓴다 —
**면적 단계의 `corner_capable` 도 같은 조건**(`optimizer.py:1644`)이다.

- [ ] **Step 2: `--phase` 플래그**

```python
parser.add_argument(
    "--phase", choices=("objective", "area"), default="objective",
    help="area 는 run_area_optimization(결정론적 순위, optimize: 불필요). "
         "탐색 전략 A/B 의 기본 선택이다 - LLM 분산이 0이므로 실행 하나가 "
         "판정에 쓰일 수 있다.",
)
```

`run_side` 에서:

```python
if phase == "area":
    result = asyncio.run(run_area_optimization(texts, spec, state, agents))
else:
    result = asyncio.run(run_optimization(texts, spec, state, agents))
```

`run_area_optimization` 을 import 에 더한다.

- [ ] **Step 3: 적격성 선행 확인**

**대조군을 먼저 돌리고 수락이 0 이면 그 슬롯을 `void` 로 두고 나머지를 돌리지
않는다.** 사전 등록의 「코너 경로의 적격성」절이 요구하는 것이다.

```python
# 대조군(coordinate_descent)이 스텝을 하나도 수락하지 못하면 비교할 기준선이
# 없다. 기준선이 자기 가드를 못 지키는 경우(optimize_guard_infeasible)가
# 그렇고, 그때는 어떤 전략도 0을 낸다 - 여유분 하한 측정이 P2 에서 정확히
# 이렇게 무효가 됐다. void 는 실패가 아니라 "조건이 발생하지 않았다"이다.
if control_result.get("steps_accepted", 0) == 0:
    record["verdict"] = "void"
    record["void_reason"] = (
        "control (coordinate_descent) accepted 0 steps on this slot; "
        "there is no baseline to compare against"
    )
    return record   # partners>0 을 돌리지 않는다
```

- [ ] **Step 4: 조합 스텝 발화 여부를 기록**

사전 등록의 **선행 조건**이다 — 조합 스텝이 한 번도 수락되지 않으면 그 슬롯은
`void`. `{label}_step` 이벤트에서 변경이 2개인 수락 스텝을 센다.

```python
# "조합이 한 번도 수락되지 않았다"와 "이 기록이 없다"가 같아 보이면 안 되므로
# 0 일 때도 키를 쓴다.
record["compound_steps_accepted"] = <센 값>
```

- [ ] **Step 5: 짧은 스모크**

```
.venv/bin/python scripts/search_ab.py --spec benchmarks/bandgap/spec_corner_reduction.yaml \
  --phase area --strategies coordinate_descent compound_fallback_1 --max-steps 3 \
  --out-dir /tmp/ab_smoke --force
```
목적: 배선이 도는지만 본다. **이 결과는 판정에 쓰지 않는다**(`--max-steps 3`).

- [ ] **Step 6: 커밋**

---

### Task 4: 9런 실행과 결과 문서

**Files:**
- Create: `docs/superpowers/specs/2026-08-02-compound-step-search-results.md`

- [ ] **Step 1: 잠긴 사전 등록을 먼저 읽는다.** 규칙을 재진술하지 말고 따른다.

- [ ] **Step 2: 슬롯마다 대조군 먼저**

| 슬롯 | 스펙 |
|---|---|
| A | `benchmarks/bandgap/spec_pvt.yaml` |
| B | `benchmarks/bandgap/spec_corner_reduction.yaml` |
| C | `benchmarks/two_stage_opamp/spec_search_slot.yaml` |

각 슬롯에서 `coordinate_descent` → 수락 0 이면 `void`, 그 슬롯 종료.

- [ ] **Step 3: `partners ∈ {1,3}` 실행.** 조합마다 결과를 **끝나는 대로 디스크에
  append** 한다. 중간에 죽어도 끝난 것은 남아야 한다. 런 하나가 15분을 넘기면
  `timeout` 으로 기록하고 다음으로 간다.

- [ ] **Step 4: 슬롯 C 의 바이스테이블 확인.** 각 수락 스텝에서 `degn` 을 재고,
  시작값에서 2배 이상 벗어난 스텝이 하나라도 있으면 슬롯 C 전체를
  `contaminated` 로 기록하고 판정에서 제외한다. 그러면 `single_deck` 을 붙인다.

- [ ] **Step 5: 결과 문서.** 사전 등록의 판정 규칙 중 **어느 것이 발화했는지**를
  이름으로 적고, 표의 모든 숫자가 런 산출물에서 재현되는지 확인한다. 부수 관찰
  (시뮬 수, 벽시계, 조합 수락률, `{X5,X7,Xcc}` 겹침)은 **판정에 쓰지 않는다**고
  명시한다.

- [ ] **Step 6: 커밋**

---

### Task 5: `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`, 필요하면 `docs/superpowers/specs/2026-07-29-theory-adoption-roadmap.md`

- [ ] **Step 1:** 채택이면 채택된 전략과 `partners` 값, **그리고 유도 조건**
  (덱 2개 중 하나는 authoring 된 슬롯, 결합이 실측된 회로는 하나뿐). 기각이면
  **부정 결과를 규칙으로** 적고, 안 (c) 모델 기반의 근거가 되는지도 적는다.
  `void` 면 조건이 발생하지 않았음을 적는다 — 셋 다 다른 사실이다.

- [ ] **Step 2:** 재매개화(안 a)가 **기각된 근거**를 적는다: 유효 확인 38쌍 중
  같은 소자 5 · 서로 다른 소자 26 · 테스트벤치 7, 즉 DUT 31쌍의 16%뿐.

- [ ] **Step 3: 드리프트 가드.** 순서 구속 — `pytest -m "not slow"` 를 전경에서
  한 번 돌려 통과를 확인한 **뒤에** 숫자를 고친다. 현재 1548 / 2 / 9 / 97.98 s.

- [ ] **Step 4: 커밋**

---

## 이 계획이 다루지 않는 것

- **LLM 튜너의 조합 제안 귀속.** 튜너는 이미 여러 노브를 동시에 제안하지만
  시뮬레이션 하나가 덱 하나를 재므로 어느 변경이 기여했는지 귀속할 수 없다.
  별개의 결함이고 여기서 고치지 않는다.
- **안 (a) 재매개화**와 **안 (c) 모델 기반 BO.** 사전 등록이 대상에서 뺐다.
- **`two_stage_opamp` 바이스테이블 자체의 해결.** `.nodeset` 적용 여부는
  문서화된 미결 설계 질문이고 이 계획이 정하지 않는다.
