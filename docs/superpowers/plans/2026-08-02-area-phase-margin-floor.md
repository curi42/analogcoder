# 면적 단계 여유분 하한 구현 계획

> **에이전트 작업자에게:** 필수 하위 스킬 — `superpowers:subagent-driven-development`
> 로 태스크 단위 실행. 단계는 체크박스(`- [ ]`)로 추적한다.

**목표:** 사전 등록
`docs/superpowers/specs/2026-08-02-area-phase-margin-floor-design.md`(커밋
`0c6d7f3`)가 고정한 규칙 계열 F1·F2·F3 을 구현하고, 14 조합을 돌려 값을 고른다.

**구조:** 세 규칙은 **전부 "어떤 `allowances` dict 를 만드느냐"로 환원된다.**
`_optimize` 가 `allowances` 를 한 번 만들어 `SearchOracle` 에 넘기고,
`guard_band_violations` 가 그것을 읽어 `evaluation.violations` 를 채우고,
`accept_step` 이 그것을 본다. 이 이음매가 이미 있으므로 새 분기를 만들지 않는다.

**기술 스택:** 기존 그대로. 새 의존성 없음.

## Global Constraints

- **`allowances` 를 정하는 곳은 정확히 한 곳이어야 한다.** 이 저장소의 반복
  결함이 "같은 규칙이 두 곳에 복사되어 양방향으로 갈라진 것"이다
  (`compose.py` 가 `netlist.py` 의 include 규칙을 손으로 베낀 건). 규칙이
  셋으로 늘어도 **결정 지점은 하나**다.
- **최적화에는 FAIL 이 없다.** 하한이 무엇이든 실행은 계속되고
  `result.json`/`report.md` 가 나온다.
- **`0`/`unknown`/키 부재는 서로 다른 사실이다.** 빈 리스트와 없는 키를 같은
  칸에 넣지 않는다.
- **기존 이벤트 이름을 바꾸지 않는다.** 새 사실은 새 키로 넣는다.
- **부호를 곱하지 않는다.** 여유분은 **절대량**이고, 임계값의 부호와 무관하게
  양수다(`ratio_allowances` 가 `|T|` 를 쓰는 이유). `pvt.py` 가 이 모양으로
  두 번, 가드 밴드가 한 번 더 대가를 치렀다.
- **격자를 넓히지 않는다.** F1 `f ∈ {0.02, 0.05, 0.10, 0.20}`,
  F2 `r ∈ {0.25, 0.50, 0.75}`. 사전 등록이 고정했다.
- 문서·주석·독스트링은 **한글**. 테스트 이름은 영어, 테스트 독스트링은 한글.
- `pytest -m "not slow"` 기준선: **1500 passed, 2 skipped, 9 deselected, ~98 s**.

## 이미 있는 것 — 새로 만들지 말 것

읽고 확인한 사실이다. 다르면 **작업 전에 보고**한다.

| 것 | 위치 | 하는 일 |
|---|---|---|
| `ratio_allowances(criteria, g)` | `judge_tools.py:129` | `g·\|T\|` — **F1 그 자체** |
| `corner_allowances(reference, sweep, criteria)` | `judge_tools.py:94` | 코너 실측 거리 |
| `guard_band_violations(measurements, criteria, allowances)` | `judge_tools.py:74` | 없는 이름은 여유분 `0.0` |
| `PhaseConfig.guard_band` | `optimizer.py:69` | `None` 이면 비율 폴백 없음 |
| allowances 조립 | `optimizer.py:~1398`(비율), `~1435`(코너 덮어쓰기) | **결정 지점** |
| `SearchOracle._allowances` | `optimizer.py:628`, 소비 `:736` | 오라클이 읽는 곳 |
| `accept_step` | `optimizer.py:565`, `violations` 검사 `:582` | 순서가 규칙의 일부 |

**F1 은 새 코드가 아니다.** `AREA_PHASE.guard_band` 를 `None` 에서 `f` 로 바꾸면
`ratio_allowances` 가 모든 이름을 채운다. Task 2 는 그 배선만 한다.

**알려진 주의 하나**: `CLAUDE.md` 는 비율 폴백만으로는 실제 스펙에서 쓸 수 없다고
실측을 적어 두었다 — bandgap 에서 `g=0.2` 는 `vbgout_v >= 1.44` **그리고**
`<= 1.024` 를 요구하는 **빈 구간**이다. 그 측정은 **전류 최적화 단계**에서 나온
것이므로 면적 단계에서 재현될지는 다른 질문이고, **격자를 깎지 않는다** —
예상으로 격자를 줄이는 것은 사후 규칙 변경이다.

---

### Task 1: F2 여유분 생성자

**Files:**
- Modify: `src/analogcoder/judge_tools.py` (`ratio_allowances` 바로 아래)
- Test: `tests/unit/test_judge_tools.py`

**Interfaces:**
- Produces:
  `baseline_ratio_allowances(baseline_measurements: dict, criteria: list[Criterion], r: float) -> tuple[dict[str, float], list[str]]`
  — `(allowances, excluded_names)`.

F2 는 "상대 여유가 기준선 값의 `r` 배 아래로 떨어지지 않는다"이다. 여유분은
절대량이므로 `allowance_j = r · |기준선에서 임계값까지의 거리|` 이다.

**사전 등록이 박은 제외 규칙을 그대로 구현한다**: 기준선 여유가 **음수(이미
실패 중)이거나 정확히 0** 인 기준은 F2 를 적용하지 않고 이름을 돌려준다.
음수에 `r` 을 곱하면 하한이 위로 올라가 규칙이 반대로 작동하고, 0 은 어떤 `r`
을 곱해도 0 이라 규칙이 침묵한다 — 침묵을 규칙인 척하지 않는다.
**측정값이 없는 기준도 제외**하고 같은 목록에 넣는다(그 기준은
`evaluate_criteria` 가 `NaN` 으로 판정한다).

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_f2_allowance_is_a_ratio_of_the_baseline_distance_to_the_threshold():
    """여유분은 절대량이므로 기준선의 임계값까지 거리에 r 을 곱한 값이다."""
    criteria = [
        Criterion(name="gain", measurement="gain_db", operator=">=", threshold=60.0),
        Criterion(name="iq", measurement="iq_ua", operator="<=", threshold=300.0),
    ]
    allowances, excluded = baseline_ratio_allowances(
        {"gain_db": 80.0, "iq_ua": 200.0}, criteria, 0.5
    )
    assert allowances == {"gain": 10.0, "iq": 50.0}
    assert excluded == []


def test_f2_excludes_a_criterion_that_is_already_failing():
    """음수 여유에 r 을 곱하면 하한이 **위로** 올라가 규칙이 뒤집힌다.

    psrr_dc <= -25 에 비율을 곱했다가 <= -20 이 되어 더 느슨해졌던 사고와 같은
    모양이고, pvt.py 는 그것으로 두 번 대가를 치렀다. 제외하고 이름을 남긴다 -
    그 기준은 overall_pass 가 이미 판정한다."""
    criteria = [
        Criterion(name="psrr", measurement="psrr_db", operator="<=", threshold=-25.0),
    ]
    allowances, excluded = baseline_ratio_allowances({"psrr_db": -20.0}, criteria, 0.5)
    assert allowances == {}
    assert excluded == ["psrr"]


def test_f2_excludes_a_criterion_sitting_exactly_on_its_threshold():
    """여유 0 에는 어떤 r 을 곱해도 0 이다 - 침묵을 규칙인 척하지 않는다."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=60.0)]
    allowances, excluded = baseline_ratio_allowances({"gain_db": 60.0}, criteria, 0.5)
    assert allowances == {}
    assert excluded == ["gain"]


def test_f2_excludes_a_criterion_with_no_measurement():
    """측정값이 없으면 거리를 잴 수 없다. 0 으로 읽지 않는다."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=60.0)]
    allowances, excluded = baseline_ratio_allowances({}, criteria, 0.5)
    assert allowances == {}
    assert excluded == ["gain"]


def test_f2_allowance_is_positive_regardless_of_threshold_sign():
    """임계값이 음수여도 여유분은 양수 절대량이다 - guard_band_violations 가
    부호 문제를 만나지 않게 하는 것이 ratio_allowances 와 같은 이유다."""
    criteria = [Criterion(name="psrr", measurement="psrr_db", operator="<=", threshold=-25.0)]
    allowances, excluded = baseline_ratio_allowances({"psrr_db": -35.0}, criteria, 0.5)
    assert allowances == {"psrr": 5.0}
    assert excluded == []
```

`Criterion` 의 실제 생성자 인자는 `spec.py` 에서 확인하고 맞춘다. 기존
`test_judge_tools.py` 가 `Criterion` 을 어떻게 만드는지 **먼저 읽고 그 관례를
따른다.**

- [ ] **Step 2: 실패를 확인한다**

```bash
.venv/bin/python -m pytest tests/unit/test_judge_tools.py -k f2 -v
```
기대: `ImportError` 또는 `NameError: baseline_ratio_allowances`.

- [ ] **Step 3: 구현한다**

```python
def baseline_ratio_allowances(
    baseline_measurements: dict, criteria: list[Criterion], r: float
) -> tuple[dict[str, float], list[str]]:
    """기준선 여유의 `r` 배를 남기라는 여유분, 그리고 **적용할 수 없는 기준들**.

    `ratio_allowances` 가 `g·|T|`(임계값에 비례)인 것과 달리 이쪽은
    `r·|기준선 - T|`(회로가 실제로 갖고 있던 여유에 비례)다. 고를 상수가
    비율 하나뿐이고 기준의 단위·부호·크기와 무관한 것이 이 규칙을 후보에
    넣은 이유다.

    셋을 제외하고 그 이름을 **돌려준다**(조용히 빼지 않는다):
    측정값이 없는 기준(거리를 잴 수 없다), 이미 실패 중인 기준(음수에 r 을
    곱하면 하한이 위로 올라가 규칙이 뒤집힌다), 임계값에 정확히 붙은 기준
    (여유 0 에 무엇을 곱해도 0 이라 규칙이 침묵한다). 앞 둘은 `overall_pass`
    가 이미 판정하므로 이 규칙이 할 일이 없다.
    """
    allowances: dict[str, float] = {}
    excluded: list[str] = []
    for c in criteria:
        actual = baseline_measurements.get(c.measurement)
        if actual is None:
            excluded.append(c.name)
            continue
        slack = (actual - c.threshold) if c.operator in _LOWER_BOUND else (c.threshold - actual)
        if slack <= 0.0 or slack != slack:  # NaN 도 여기서 걸린다
            excluded.append(c.name)
            continue
        allowances[c.name] = r * slack
    return allowances, excluded
```

`_LOWER_BOUND`/`_UPPER_BOUND` 의 실제 이름과 내용은 같은 파일에서 확인해
맞춘다(`guard_band_violations` 가 쓰는 것과 **같은 상수를 쓴다** — 두 곳이
연산자 집합을 따로 들면 갈라진다).

- [ ] **Step 4: 통과를 확인한다**

```bash
.venv/bin/python -m pytest tests/unit/test_judge_tools.py -v
```

- [ ] **Step 5: 커밋**

```bash
git add src/analogcoder/judge_tools.py tests/unit/test_judge_tools.py
git commit -m "$(cat <<'EOF'
feat: F2 여유분 생성자 — 기준선 여유의 r 배를 남긴다

ratio_allowances 가 g|T|(임계값 비례)인 것과 달리 r|기준선-T|(회로가 실제로
갖고 있던 여유에 비례)다. 고를 상수가 비율 하나뿐이고 기준의 단위·부호·
크기와 무관하다.

적용할 수 없는 셋을 **이름과 함께** 돌려준다: 측정값 없음, 이미 실패 중,
임계값에 정확히 붙음. 음수 여유에 r 을 곱하면 하한이 위로 올라가 규칙이
뒤집히고, 그것은 pvt.py 가 두 번 치른 사고다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N8njT49yMwXwcYsaNnW2KP
EOF
)"
```

---

### Task 2: 하한을 `PhaseConfig` 에 데이터로 싣고, 결정 지점을 하나로 유지한다

**Files:**
- Modify: `src/analogcoder/optimizer.py` (`PhaseConfig`, `AREA_PHASE`, `_optimize` 의 allowances 조립)
- Test: `tests/unit/test_optimizer_area_phase.py`

**Interfaces:**
- Consumes: Task 1 의 `baseline_ratio_allowances`
- Produces: `MarginFloor(rule: str, value: float)` (frozen dataclass),
  `PhaseConfig.margin_floor: MarginFloor | None`

`PhaseConfig` 는 **분기가 아니라 데이터**라는 것이 그 클래스의 독스트링에
적힌 계약이다. 하한도 데이터로 싣는다. `rule` 은 `"f1"` / `"f2"` / `"f3"`.

**결정 지점은 하나다.** `_optimize` 안에 allowances 를 만드는 함수를 하나 두고,
세 규칙이 그 함수 안에서만 갈린다. 호출부가 규칙을 다시 묻지 않는다.

세 규칙의 의미:

| rule | 코너 스윕이 있을 때 | 없을 때 |
|---|---|---|
| `f1` | 기존 그대로(비율 위에 코너 덮어쓰기) | `ratio_allowances(criteria, value)` |
| `f2` | 기존 그대로 | `baseline_ratio_allowances(baseline, criteria, value)` |
| `f3` | 기존 그대로 | F1 또는 F2 중 하나와 **정확히 같다** |

**F3 은 구별되는 규칙이 아니다 — 계획을 쓰면서 드러났고, 여기 기록한다.**
F3 의 정의는 "코너 실측이 있으면 그것, 없으면 F1/F2"인데 **코너 실측이 있으면
그것을 쓰는 것은 세 규칙 전부가 이미 하는 일이다**(`_optimize` 가 비율 위에
`corner_allowances` 를 덮어쓴다). 그래서 F3 은 코너 없는 절반에서 F1 이나 F2 와
같은 dict 를 만들고, **값이 갈릴 수 있는 입력이 없다.**

사전 등록이 "F3 은 F1·F2 의 우승자를 그대로 쓰므로 별도 격자가 없다"고 적은
것이 이미 그 신호였다.

**사전 등록은 잠겨 있으므로 규칙을 지우지 않는다.** `rule="f3"` 은 받아들이되
F1/F2 중 어느 것으로 환원되는지를 **명시적으로 기록**하고, Task 4 의 결과
문서는 F3 을 "F1/F2 와 구별되지 않음(같은 allowances)"으로 보고한다. 사전
등록의 "셋 다의 숫자를 적는다"는 그렇게 충족된다 — 숫자를 지어내지 않고,
같다는 사실을 적는 것이 그 요구에 대한 정직한 답이다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_the_area_phase_with_no_floor_leaves_every_criterion_unguarded():
    """하한이 없으면 코너 없는 스펙에서 모든 기준이 무방비다 - 이것이
    2026-08-02 측정이 코너에서 깨지는 것을 확인한 출하 상태다."""


def test_f1_fills_every_criterion_from_the_threshold():
    """F1 은 ratio_allowances 그 자체이므로 모든 이름이 채워진다."""


def test_f2_fills_from_the_baseline_distance_and_names_what_it_could_not():
    """F2 는 적용 못 한 기준을 **이름으로** 남긴다 - 조용히 빠지면
    '하한이 걸렸다'와 '이 기준에는 하한이 없다'가 같아 보인다."""


def test_the_three_rules_are_decided_in_exactly_one_place():
    """규칙이 셋으로 늘어도 결정 지점은 하나여야 한다.

    소스를 읽어 `MarginFloor.rule` 을 분기하는 곳이 하나뿐임을 확인한다.
    compose.py 가 netlist.py 의 규칙을 손으로 베껴 양방향으로 갈라진 것이
    이 저장소가 이미 치른 대가다."""
```

각 테스트의 본문은 구현자가 채우되 **위 독스트링의 주장을 실제로 고정**해야
한다. 마지막 테스트는 `inspect.getsource` 로 `optimizer.py` 를 읽어
`rule ==` / `rule in` 패턴의 출현 횟수를 세는 형태를 권한다 —
`tests/unit/test_area_ranking.py` 에 소스를 스캔하는 선례가 있으니 먼저 읽는다.

- [ ] **Step 2: 실패를 확인한다**

```bash
.venv/bin/python -m pytest tests/unit/test_optimizer_area_phase.py -k floor -v
```

- [ ] **Step 3: 구현한다**

`PhaseConfig` 에 `margin_floor: MarginFloor | None = None` 을 더하고,
`AREA_PHASE` 는 **오늘 그대로 `None`** 을 유지한다(값은 Task 4 의 측정이
정한다 — 지금 고르면 사후 규칙 변경이다). `_optimize` 의 allowances 조립을
함수 하나로 모으고 세 규칙을 그 안에서 가른다.

`guard_band` 필드는 **건드리지 않는다.** 전류 최적화 단계가 쓰고 있고, F1 은
그 필드를 재사용하는 것이 아니라 `margin_floor` 를 통해 같은 함수를 부른다 —
한 필드가 두 단계에서 다른 뜻을 갖지 않게 한다.

- [ ] **Step 4: 통과와 회귀 없음을 확인한다**

```bash
.venv/bin/python -m pytest tests/unit/test_optimizer_area_phase.py tests/unit/test_optimizer.py tests/unit/test_optimizer_corners.py -q
```

- [ ] **Step 5: 커밋**

```bash
git add src/analogcoder/optimizer.py tests/unit/test_optimizer_area_phase.py
git commit -m "$(cat <<'EOF'
feat: 여유분 하한을 PhaseConfig 에 데이터로 싣는다 — 결정 지점은 하나다

PhaseConfig 는 분기가 아니라 데이터라는 것이 그 클래스의 계약이다. 하한도
데이터로 싣고, 세 규칙(f1/f2/f3)은 allowances 를 만드는 함수 **한 곳**에서만
갈린다. 규칙이 두 곳에 복사되면 양방향으로 갈라지고, compose.py 가 이미
그 대가를 치렀다.

AREA_PHASE 는 아직 None 이다 - 값은 측정이 정한다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N8njT49yMwXwcYsaNnW2KP
EOF
)"
```

---

### Task 3: 새 트리거 — 상대 여유 최솟값을 기록한다

**Files:**
- Modify: `src/analogcoder/optimizer.py` (`_result`, `_optimize`), `src/analogcoder/report.py`
- Test: `tests/unit/test_optimizer_area_phase.py`, `tests/unit/test_report.py`

사전 등록의 마지막 절이 요구한 것이고, **하한 채택 여부와 독립적으로** 넣는다.
원래 트리거("코너 스윕에서 깨지면")는 코너를 선언하지 않은 스펙에서 관측
불가능하고, 그 스펙이야말로 하한이 필요한 곳이다.

각 수락 스텝 이후 모든 기준의 상대 여유를 재고, 실행 종료 시 **최솟값과 그
기준 이름**을 `result` 와 `report.md` 에 남긴다.

상대 여유의 정의는 측정 스크립트가 이미 쓰는 것과 **같아야 한다**:
`slack / max(|threshold|, |actual|)`, 스케일이 0 이면 0.
`scripts/area_guard_measurement.py:_relative_slack` 을 읽고 **그 함수를 옮겨
공유한다** — 두 곳에 같은 식을 두면 갈라진다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_the_result_carries_the_tightest_relative_slack_and_its_criterion():
    """최솟값만으로는 어느 기준인지 모른다. 이름이 없으면 다음 사람이
    45 코너를 다시 돌려야 알아낸다."""


def test_a_phase_that_accepted_nothing_reports_the_baseline_s_tightest_slack():
    """수락이 0 이어도 값이 있어야 한다 - 그때의 최솟값은 기준선의 것이고,
    '태우지 않았다'와 '재지 않았다'는 다른 사실이다."""


def test_a_phase_that_could_not_measure_reports_unknown_not_zero():
    """크래시 경로에서는 알 수 없음이지 0 이 아니다. 이 브랜치는 같은
    붕괴를 unguarded_criteria 에서 이미 한 번 고쳤다."""
```

- [ ] **Step 2: 실패를 확인한다**

```bash
.venv/bin/python -m pytest tests/unit/test_optimizer_area_phase.py tests/unit/test_report.py -k slack -v
```

- [ ] **Step 3: 구현한다**

`_relative_slack` 을 `judge_tools.py` 로 옮기고 스크립트가 그것을 import 하게
바꾼다. `_result` 에 `tightest_slack: dict | None` 을 더한다
(`{"criterion": str, "value": float}` 또는 `None`). **`None` 을 `{}` 로 접지
않는다** — 이 브랜치가 `unguarded_criteria` 에서 이미 고친 붕괴다.
`report.py` 의 두 절에 모두 그린다.

- [ ] **Step 4: 통과 확인**

- [ ] **Step 5: 커밋**

---

### Task 4: 14 조합 측정

**Files:**
- Modify: `scripts/area_guard_measurement.py` (조합 루프로 확장)
- Create: `docs/superpowers/specs/2026-08-02-area-phase-margin-floor-results.md`

사전 등록의 판정 규칙을 **그대로** 구현한다. 규칙을 다시 쓰지 않는다 —
문서를 읽고 코드가 그것을 따르게 한다.

- 조합: F1 `f ∈ {0.02, 0.05, 0.10, 0.20}` × 쌍 {P1, P2}, F2 `r ∈ {0.25, 0.50, 0.75}` × 쌍 {P1, P2} = **14**
- 각 조합: 코너 없는 스펙에서 하한을 켠 면적 단계 → 착지 덱을 짝의 45 코너
  그리드로 전체 스윕
- **안전** = 스윕 `overall_pass`, **유용** = 면적 감소율 > 0
- 조합 하나가 **10 분**을 넘기면 `timeout` 으로 기록하고 다음으로. `timeout`
  은 안전도 불안전도 아니다
- P2(`two_stage_opamp`)는 각 수락 스텝에서 `degn` 을 기록하고, 시작값에서
  **2 배 이상** 벗어난 스텝이 하나라도 있으면 P2 전체를 `contaminated` 로
  판정에서 제외한다

- [ ] **Step 1: 계측기 검증을 먼저 통과시킨다**

기존 스크립트의 검증(기준선 시뮬레이션이 기준이 요구하는 측정값을 실제로
내놓는가)을 **각 쌍에 대해** 돌린다. 이 검증이 없어서 첫 측정이 가짜 VOID 를
냈고, 두 번째가 `resolve_includes` 누락을 잡았다.

- [ ] **Step 2: 14 조합을 돌린다**

각 조합의 결과를 즉시 `measurement.json` 에 append 한다 — 중간에 끊겨도
돌린 것은 남아야 한다.

- [ ] **Step 3: 판정을 적용하고 결과 문서를 쓴다**

사전 등록의 규칙 1~4 중 어느 것이 발화했는지 명시한다. **F1·F2·F3 셋 다의
숫자를 적는다**(사전 등록이 요구했다). 규칙 2(안전하지만 기능을 끔)나
규칙 3(전체 기각)으로 끝나도 그것은 실패가 아니라 답이다.

`CLAUDE.md` 가 적어 둔 "비율 폴백만으로는 쓸 수 없다"(bandgap `g=0.2` 가 빈
구간) 가 F1 에서 재현되는지 여부를 **반드시** 적는다. 재현되면 그 항목의
근거가 두 단계로 넓어지고, 재현되지 않으면 그 항목이 전류 단계에 한정된
사실이라는 것이 새로 밝혀진다.

- [ ] **Step 4: 커밋**

---

### Task 5: 채택된 값을 배선하고 문서를 맞춘다

**Files:**
- Modify: `src/analogcoder/optimizer.py` (`AREA_PHASE.margin_floor`), `CLAUDE.md`,
  `docs/superpowers/plans/2026-08-02-area-optimization-phase.md`

Task 4 의 판정이 규칙 1 로 끝났을 때만 값을 넣는다. 규칙 2·3 이면 값을 넣지
않고 그 사실을 `CLAUDE.md` 에 적는다.

`CLAUDE.md` 에 반드시 들어갈 것:

- 채택된 규칙과 값, 그리고 **그 값이 유도된 조건**(덱 몇 개, 그리드 1 종).
  사전 등록이 "그리드 축은 시험되지 않는다"를 미리 인정했으므로 꼬리표를
  붙인다.
- P2 가 오염되어 제외됐으면 `single_deck` 한정과 재측정 조건.
- 1 단계의 "확정됨" 절이 **되돌려졌다**는 사실과 그 근거 측정.
- 새 트리거(상대 여유 최솟값)가 무엇이고 어디서 읽는지.

**드리프트 가드 순서를 지킨다** — 먼저 새 입력이 게이트를 통과하는지 확인하고,
그다음에 숫자를 올린다.

---

## 이 계획이 다루지 않는 것

- **2 단계**(면적 게이트 강등, 튜너 대안 3 개, 선택 규칙)와 **3 단계**(파레토
  공선과 보고)는 별도 계획이다. 하한이 정해져야 2 단계의 전제가 선다 —
  게이트를 풀면서 하한도 없으면 면적을 키우는 쪽과 줄이는 쪽 양쪽에 제동이
  사라진다.
- `verify_post` 3 차 측정은 독립이다.
