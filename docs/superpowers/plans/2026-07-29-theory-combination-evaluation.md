# 이론 조합 평가 체제 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 단계 1(ε-근접 피복 코너 선택)을 **토글 가능한 인자**로 구현하고,
`scripts/search_ab.py`를 factorial로 확장해 이론들을 **단독이 아니라 조합으로**
평가할 수 있게 한다.

**Architecture:** 스펙이 `corner_reduction.coverage: {epsilon, tau}`를 선언하면
`corner_selection.seed_from_sweep`이 argmax 합집합 대신 ε-근접 피복 위의 탐욕을
쓴다. 블록이 없으면 오늘 동작과 **바이트 동일**이다. `search_ab.py`는
`--corner-regime`을 받아 두 쪽을 서로 다른 코너 체제로 돌린다.

**Tech Stack:** Python 3.14, pytest, ngspice (실시뮬 테스트), 의존성 추가 없음.

## Global Constraints

설계 문서 `docs/superpowers/specs/2026-07-29-theory-combination-evaluation-design.md`
에서 그대로 옮긴다. 모든 태스크의 요구사항에 암묵적으로 포함된다.

- **잠긴 제약을 깨는 변경은 어떤 성능 개선으로도 정당화되지 않는다:** 축소 집합의
  FAIL은 진짜, PASS는 낙관적, **전체 스윕이 최종 판정**.
- **`coverage:` 블록이 없으면 오늘 동작과 바이트 동일이어야 한다.** 회귀 위험을
  구조적으로 0으로 만드는 것이 이 설계의 전제다.
- **ε과 τ는 스펙에 선언한다. 코드에 박지 않는다.** 생산 덱으로 옮기는 것이 코드
  변경이 아니라 재선언이어야 한다.
- **측정값이 없는(None/NaN) 코너는 ε으로 근사되지 않는다.** 그 기준의 최악이고,
  값이 있는 어떤 코너도 그것을 덮지 못한다.
- **`max_corners` 정수 상한은 만들지 않는다.** 예산 k는 τ에서 유도된다.
- **게이트가 아무것도 안 할 때 로그가 어떻게 보이는지**를 각 게이트마다 답한다.
  이 저장소는 조용히 무력한 게이트를 열 번 셌다.
- **테스트 관례:** TDD, 모든 모듈에 짝 테스트 파일, 에이전트 테스트는
  `run_agent`/`AgentBackend`를 목킹하고 실제 LLM을 부르지 않는다.
- **문서는 한글로 쓴다.**
- 정상 TDD 사이클: `.venv/bin/python -m pytest -m "not slow" -q` (~100 s, 1405 tests).

---

### Task 1: `coverage:` 스펙 블록

**Files:**
- Modify: `src/analogcoder/spec.py` — `CornerReduction` (160-169), `_load_corner_reduction` (466-)
- Test: `tests/unit/test_spec.py`

**Interfaces:**
- Consumes: 없음 (첫 태스크)
- Produces:
  - `spec.CoverageConfig` — frozen dataclass, `epsilon: float`, `tau: float`
  - `spec.CornerReduction.coverage: CoverageConfig | None` (기본 `None`)

- [ ] **Step 1: 실패 테스트를 쓴다**

`tests/unit/test_spec.py` 끝에 추가한다. 이 파일의 기존 헬퍼(`_write_spec` 등)를
쓰지 말고 아래처럼 자족적으로 쓴다 — 다른 테스트의 픽스처 모양에 의존하지 않는다.

```python
def test_a_corner_reduction_without_a_coverage_block_declares_no_coverage(tmp_path):
    """블록이 없으면 `None`이다. 기본값 객체를 넣으면 '선언하지 않았다'와
    '기본값으로 선언했다'가 구별되지 않고, 이 설계의 전제(블록이 없으면 오늘
    동작과 바이트 동일)가 코드에서 보이지 않게 된다."""
    from analogcoder.spec import _load_corner_reduction

    cr = _load_corner_reduction({"corner_reduction": {"enabled": True}})

    assert cr is not None
    assert cr.coverage is None


def test_a_coverage_block_carries_epsilon_and_tau(tmp_path):
    from analogcoder.spec import _load_corner_reduction

    cr = _load_corner_reduction(
        {"corner_reduction": {"enabled": True, "coverage": {"epsilon": 0.03, "tau": 1.0}}}
    )

    assert cr.coverage.epsilon == 0.03
    assert cr.coverage.tau == 1.0


def test_a_coverage_block_needs_both_epsilon_and_tau():
    """기본값을 주지 않는다. epsilon 은 이 덱에서 **유도**해야 하는 값이고,
    코드가 하나 골라 두면 그 숫자가 근거 없이 생산 덱까지 따라간다."""
    from analogcoder.spec import _load_corner_reduction

    for block in ({"epsilon": 0.03}, {"tau": 1.0}, {}):
        with pytest.raises(ValueError, match="epsilon|tau"):
            _load_corner_reduction({"corner_reduction": {"coverage": block}})


@pytest.mark.parametrize("epsilon", [-0.1, 1.5])
def test_an_epsilon_outside_zero_to_one_is_refused(epsilon):
    """음수는 뜻이 없고, 1.0 초과는 '최악값의 100% 이상 떨어져도 덮는다'는
    뜻이라 사실상 전 코너가 전 기준을 덮는다 - 씨앗이 1개로 붕괴하면서
    로그는 정상으로 읽힌다."""
    from analogcoder.spec import _load_corner_reduction

    with pytest.raises(ValueError, match="epsilon"):
        _load_corner_reduction(
            {"corner_reduction": {"coverage": {"epsilon": epsilon, "tau": 1.0}}}
        )


@pytest.mark.parametrize("tau", [0.0, -0.5, 1.5])
def test_a_tau_outside_zero_exclusive_to_one_is_refused(tau):
    from analogcoder.spec import _load_corner_reduction

    with pytest.raises(ValueError, match="tau"):
        _load_corner_reduction(
            {"corner_reduction": {"coverage": {"epsilon": 0.03, "tau": tau}}}
        )
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_spec.py -q -k "coverage"`
Expected: FAIL — `TypeError: _load_corner_reduction() ... 'coverage'` 또는
`AttributeError: 'CornerReduction' object has no attribute 'coverage'`

- [ ] **Step 3: 구현한다**

`src/analogcoder/spec.py`의 `CornerReduction` **바로 위**에 추가한다:

```python
@dataclass(frozen=True)
class CoverageConfig:
    """ε-근접 피복으로 코너 씨앗을 고를 때의 두 값.

    **둘 다 스펙이 선언해야 하고 기본값이 없다.** `epsilon`은 이 덱에서
    유도되는 값이지 상수가 아니다 - 코드가 하나 골라 두면 근거 없는 숫자가
    다른 덱까지 따라간다. 이 저장소가 가드 밴드와
    `COMPARISON_REL_TOLERANCE`를 정한 방식과 같은 규율이다.

    `tau`는 목표 피복률이고 예산 k는 여기서 **유도**된다. 정수 상한
    (`max_corners`)을 두지 않는 이유는 사람이 고르는 것이 숫자가 아니라
    "기준의 몇 %를 보겠는가"라는 뜻이 있는 값이어야 하기 때문이다."""

    epsilon: float
    tau: float
```

`CornerReduction`에 필드를 더한다:

```python
    enabled: bool = True
    retry_budget: int = 2
    probe: bool = True
    coverage: CoverageConfig | None = None
    """`None`이면 오늘의 argmax 합집합 - 씨앗이 바이트 동일하다. 기본값
    객체를 넣지 않는 것은 '선언하지 않았다'와 '기본값으로 선언했다'를
    구별하기 위해서다."""
```

`_load_corner_reduction`의 `return CornerReduction(...)` 직전에 넣는다:

```python
    raw_coverage = block.get("coverage")
    coverage = None
    if raw_coverage is not None:
        if not isinstance(raw_coverage, dict):
            raise ValueError(
                f"corner_reduction.coverage must be a mapping with 'epsilon' and 'tau', "
                f"not {type(raw_coverage).__name__}: {raw_coverage!r}"
            )
        for key in ("epsilon", "tau"):
            if key not in raw_coverage:
                raise ValueError(
                    f"corner_reduction.coverage.{key} is required - it is derived from "
                    f"this deck's own measurements, so a code-side default would carry "
                    f"an ungrounded number to every other deck"
                )
            if isinstance(raw_coverage[key], bool) or not isinstance(
                raw_coverage[key], (int, float)
            ):
                raise ValueError(
                    f"corner_reduction.coverage.{key} must be a number, not "
                    f"{type(raw_coverage[key]).__name__}: {raw_coverage[key]!r}"
                )
        epsilon = float(raw_coverage["epsilon"])
        tau = float(raw_coverage["tau"])
        if not 0.0 <= epsilon <= 1.0:
            raise ValueError(
                f"corner_reduction.coverage.epsilon must be in [0, 1], got {epsilon}: "
                f"a negative tolerance has no meaning, and above 1.0 every corner covers "
                f"every criterion, collapsing the seed to one corner while the log still "
                f"reads normal"
            )
        if not 0.0 < tau <= 1.0:
            raise ValueError(
                f"corner_reduction.coverage.tau must be in (0, 1], got {tau}: "
                f"tau is the fraction of criteria the seed must cover, and 0 covers none"
            )
        coverage = CoverageConfig(epsilon=epsilon, tau=tau)
```

그리고 `CornerReduction(...)` 생성에 `coverage=coverage`를 더한다.

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_spec.py -q`
Expected: PASS (신규 5개 포함)

- [ ] **Step 5: 커밋**

```bash
git add src/analogcoder/spec.py tests/unit/test_spec.py
git commit -m "feat: 스펙이 ε-근접 피복의 두 값을 선언한다 — 기본값은 없다

epsilon 은 이 덱에서 유도되는 값이지 상수가 아니다. 코드가 하나 골라 두면
근거 없는 숫자가 생산 덱까지 따라가고, 그것을 바꾸는 것이 재선언이 아니라
코드 변경이 된다 — 그러면 검증을 처음부터 다시 해야 한다.

블록이 없으면 coverage 는 None 이다. 기본값 객체를 넣으면 '선언하지 않았다'와
'기본값으로 선언했다'가 구별되지 않는다."
```

---

### Task 2: ε-근접 피복 씨앗 함수

**Files:**
- Modify: `src/analogcoder/corner_selection.py`
- Test: `tests/unit/test_corner_selection.py`

**Interfaces:**
- Consumes: `spec.CoverageConfig` (Task 1)
- Produces:
  - `corner_selection.coverage_seed(sweep: dict, criteria: list, coverage) -> tuple[list[CornerPoint], dict]`
    — 고른 코너 목록과 기록용 dict `{"covered": int, "total": int, "dropped": list[str]}`.
    `dropped`는 argmax 씨앗에는 있는데 이 씨앗에는 없는 코너의 라벨.

- [ ] **Step 1: 실패 테스트를 쓴다**

`tests/unit/test_corner_selection.py`에 추가한다. 이 파일에는 이미
`FS`/`SS`/`SF` 코너 상수와 `_sweep`/`_wc` 헬퍼가 있다 — 그것을 쓰되, 피복은
`per_corner`를 읽으므로 아래 헬퍼를 새로 더한다.

```python
from analogcoder.spec import CoverageConfig, Criterion


def _per_corner(rows):
    """rows: [(CornerPoint, {measurement: value}), ...] -> per_corner 항목들."""
    from analogcoder.pvt import corner_fields

    return [{"corner": corner_fields(c), "measurements": m, "severity": 0.0}
            for c, m in rows]


_GAIN = Criterion(name="gain", measurement="g", operator=">=", threshold=40.0)
_PM = Criterion(name="pm", measurement="p", operator=">=", threshold=60.0)


def test_two_corners_within_epsilon_of_each_others_worst_collapse_to_one():
    """이것이 이 함수의 존재 이유다. argmax 피복에서 집합은 서로소이므로
    (기준마다 argmax 가 하나) 코너를 줄일 수 없다. ε-근접이 집합을 겹치게
    만든다: FS 가 gain 의 최악이고 SS 가 pm 의 최악인데, SS 의 gain 이 FS 의
    gain 에서 ε 이내이면 SS 하나가 둘 다 덮는다."""
    sweep = {"per_corner": _per_corner([
        (FS, {"g": 41.0, "p": 70.0}),
        (SS, {"g": 41.02, "p": 65.0}),
    ])}

    chosen, record = coverage_seed(sweep, [_GAIN, _PM], CoverageConfig(epsilon=0.01, tau=1.0))

    assert chosen == [SS]
    assert record["covered"] == 2 and record["total"] == 2
    assert label(FS) in record["dropped"]


def test_epsilon_zero_reproduces_the_argmax_union():
    """ε=0 이면 각 기준의 최악값과 **정확히** 같은 코너만 덮으므로 오늘의
    씨앗과 같은 집합이 나온다. 이것이 회귀 안전선이다."""
    sweep = {"per_corner": _per_corner([
        (FS, {"g": 41.0, "p": 70.0}),
        (SS, {"g": 45.0, "p": 65.0}),
    ])}

    chosen, record = coverage_seed(sweep, [_GAIN, _PM], CoverageConfig(epsilon=0.0, tau=1.0))

    assert set(chosen) == {FS, SS}
    assert record["dropped"] == []


def test_a_corner_with_no_measurement_is_not_approximated_by_any_other():
    """측정값이 없다는 것은 회로가 거기서 동작하지 않는다는 가장 강한 증거다.
    값이 있는 코너가 그것을 ε 으로 덮으면 그 사실이 사라진다."""
    sweep = {"per_corner": _per_corner([
        (FS, {"g": 41.0}),
        (SS, {}),          # g 측정값 없음
    ])}

    chosen, _ = coverage_seed(sweep, [_GAIN], CoverageConfig(epsilon=0.9, tau=1.0))

    assert SS in chosen


def test_tau_below_one_stops_early():
    """예산 k 는 τ 에서 유도된다 - 정수 상한을 따로 두지 않는 이유다."""
    sweep = {"per_corner": _per_corner([
        (FS, {"g": 41.0, "p": 99.0}),
        (SS, {"g": 99.0, "p": 65.0}),
    ])}

    chosen, record = coverage_seed(sweep, [_GAIN, _PM], CoverageConfig(epsilon=0.0, tau=0.5))

    assert len(chosen) == 1
    assert record["covered"] == 1 and record["total"] == 2


def test_an_empty_per_corner_yields_an_empty_seed_rather_than_guessing():
    sweep = {"per_corner": []}

    chosen, record = coverage_seed(sweep, [_GAIN], CoverageConfig(epsilon=0.03, tau=1.0))

    assert chosen == []
    assert record["covered"] == 0 and record["total"] == 1
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_corner_selection.py -q -k "coverage or epsilon or tau or approximated"`
Expected: FAIL — `NameError: name 'coverage_seed' is not defined`

- [ ] **Step 3: 구현한다**

`src/analogcoder/corner_selection.py`의 `seed_from_sweep` **위**에 추가한다.
`math`를 import 한다.

```python
def _worst_of(values: list[float], operator: str) -> float:
    """이 연산자에서 '가장 나쁜' 값. `>=`/`>` 면 작을수록 나쁘다."""
    return min(values) if operator in (">=", ">") else max(values)


def _is_missing(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def coverage_seed(sweep: dict, criteria: list, coverage) -> tuple[list, dict]:
    """ε-근접 피복 위의 탐욕으로 코너 씨앗을 고른다.

    **왜 argmax 합집합이 아닌가.** `worst_case_corners`는 기준 -> 코너
    **하나**의 매핑이므로, 각 코너가 덮는 집합은 그 매핑의 역상이고 어떤 두
    코너도 같은 기준을 덮지 않는다. 서로소이면 피복 함수가 가법적이라 탐욕이
    정확히 최적이고 - 즉 부분모듈성이 내용을 갖지 않고 - 무엇보다 **피복률을
    유지한 채 코너를 줄이는 것이 불가능하다.**

    ε-근접("이 코너에서의 값이 최악값으로부터 상대 ε 이내")은 집합을 겹치게
    만들어 그 자리를 살린다. 목적에도 더 충실하다 - 중간 루프가 원하는 것은
    argmax 그 자체가 아니라 **위반을 드러내는 코너**이고, 최악에서 아주 조금
    떨어진 코너는 같은 위반을 드러낸다(실측: 위반 11건이 ε=10%에서도 전부
    보존됐다. 위반은 칼날이 아니라 띠다).

    **측정값이 없는 코너는 근사되지 않는다.** 그 기준의 최악이고, 값이 있는
    어떤 코너도 그것을 덮지 못한다 - 회로가 거기서 동작하지 않는다는 사실을
    ε 으로 뭉개지 않는다.

    돌려주는 기록의 `dropped`가 이 게이트의 무력 상태를 보이게 한다: ε 이
    겹침을 전혀 만들지 못하면 `[]`이고, 그것이 "줄일 것이 없었다"를 말한다."""
    entries = sweep.get("per_corner", [])
    points = [_as_point(entry["corner"]) for entry in entries]
    measurements = [entry.get("measurements", {}) for entry in entries]

    # 코너 index -> 이 코너가 덮는 기준 이름들
    sets: dict[int, set] = {i: set() for i in range(len(points))}
    for criterion in criteria:
        values = [m.get(criterion.measurement) for m in measurements]
        missing = [i for i, v in enumerate(values) if _is_missing(v)]
        if missing:
            for i in missing:
                sets[i].add(criterion.name)
            continue
        if not values:
            continue
        worst = _worst_of(values, criterion.operator)
        scale = abs(worst) if worst else 1.0
        for i, value in enumerate(values):
            if abs(value - worst) <= coverage.epsilon * scale:
                sets[i].add(criterion.name)

    total = len(criteria)
    target = math.ceil(coverage.tau * total)
    covered: set = set()
    chosen: list = []
    remaining = dict(sets)
    while len(covered) < target:
        best, gain = None, 0
        for i in sorted(remaining):
            new = len(remaining[i] - covered)
            if new > gain:
                best, gain = i, new
        if best is None:
            break
        chosen.append(points[best])
        covered |= remaining.pop(best)

    argmax_points = _argmax_points(sweep, criteria, points, measurements)
    dropped = [label(p) for p in argmax_points if p not in chosen]
    return chosen, {"covered": len(covered), "total": total, "dropped": dropped}


def _argmax_points(sweep: dict, criteria: list, points: list, measurements: list) -> list:
    """오늘의 씨앗이 골랐을 코너들. `dropped`를 계산하기 위해서만 쓴다."""
    chosen = []
    for criterion in criteria:
        values = [m.get(criterion.measurement) for m in measurements]
        if not values:
            continue
        missing = [i for i, v in enumerate(values) if _is_missing(v)]
        index = missing[0] if missing else values.index(
            _worst_of(values, criterion.operator)
        )
        if points[index] not in chosen:
            chosen.append(points[index])
    return chosen
```

파일 맨 위에 `import math`를 더한다.

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_corner_selection.py -q`
Expected: PASS (기존 테스트 전부 + 신규 5개)

- [ ] **Step 5: 커밋**

```bash
git add src/analogcoder/corner_selection.py tests/unit/test_corner_selection.py
git commit -m "feat: ε-근접 피복 위의 탐욕으로 코너 씨앗을 고른다

argmax 피복에서 집합은 서로소다 — worst_case_corners 가 기준 -> 코너 하나의
매핑이므로 어떤 두 코너도 같은 기준을 덮지 않는다. 그러면 피복 함수가
가법적이라 (1-1/e) 보장이 빈 칸이고, 무엇보다 **피복률을 유지한 채 코너를
줄이는 것이 불가능하다** — 로드맵의 채택 조건이 참이 될 수 있는 경우가
존재하지 않았다.

ε-근접이 집합을 겹치게 만든다. 실측으로 위반 11건이 ε=10% 에서도 전부
보존됐다 — 위반은 칼날이 아니라 띠여서, 정확한 argmax 를 버려도 근방 코너가
그것을 드러낸다.

측정값이 없는 코너는 근사하지 않는다. dropped 가 이 게이트의 무력 상태를
보이게 한다."
```

---

### Task 3: 배선과 `corner_seed` 기록

**Files:**
- Modify: `src/analogcoder/corner_selection.py` — `seed_from_sweep` (155-175)
- Modify: `src/analogcoder/cli.py:561` — `seed_from_sweep(baseline_sweep, spec)` 호출부
- Test: `tests/unit/test_corner_selection.py`, `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `coverage_seed` (Task 2), `spec.CornerReduction.coverage` (Task 1)
- Produces:
  - `seed_from_sweep(sweep, spec) -> tuple[CornerSet, dict]` — **반환형이 바뀐다.**
    두 번째 항목이 `corner_seed` 이벤트에 그대로 실릴 기록 dict:
    `{"mode": "argmax"|"coverage", "epsilon": float|None, "tau": float|None,
      "seed_size": int, "points_per_tb": int, "covered": int, "total": int,
      "dropped": list[str]}`

- [ ] **Step 1: 실패 테스트를 쓴다**

`tests/unit/test_corner_selection.py` 맨 위에 `import types`가 없으면 더한다.
`_GAIN`/`_PM`/`_per_corner`는 Task 2에서 같은 파일에 이미 정의했다.

```python
def test_seed_from_sweep_reports_argmax_mode_when_no_coverage_is_declared():
    """기록은 **무조건** 나온다. 피복을 쓸 때만 적으면 '오늘 방식으로 골랐다'와
    '기록하는 코드가 사라졌다'가 같은 침묵이 된다 - 이 저장소가 열 번 센
    실패 모양이다."""
    cs, record = seed_from_sweep(_sweep({"gain": _wc(FS, 41.0)}), _spec)

    assert record["mode"] == "argmax"
    assert record["epsilon"] is None and record["tau"] is None
    assert record["dropped"] == []
    assert record["seed_size"] == 1
    assert cs.corners[0] is NOMINAL


def test_seed_from_sweep_uses_coverage_when_the_spec_declares_it():
    from analogcoder.spec import CornerReduction, CoverageConfig

    spec = types.SimpleNamespace(
        pvt_corners=_spec.pvt_corners,
        all_criteria=[_GAIN, _PM],
        testbenches=[object(), object()],
        corner_reduction=CornerReduction(coverage=CoverageConfig(epsilon=0.01, tau=1.0)),
    )
    sweep = {
        "per_corner": _per_corner([(FS, {"g": 41.0, "p": 70.0}),
                                   (SS, {"g": 41.02, "p": 65.0})]),
        "worst_case_corners": {},
    }

    cs, record = seed_from_sweep(sweep, spec)

    assert record["mode"] == "coverage"
    assert record["epsilon"] == 0.01
    assert cs.corners == (NOMINAL, SS)
    # NOMINAL + 씨앗 + 탐침 1
    assert record["points_per_tb"] == 3
    assert label(FS) in record["dropped"]
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_corner_selection.py -q -k "reports_argmax_mode or uses_coverage"`
Expected: FAIL — `ValueError: too many values to unpack` (지금은 `CornerSet` 하나만 돌려준다)

- [ ] **Step 3: 구현한다**

`seed_from_sweep`을 바꾼다. 기존 독스트링은 유지하고 아래를 더한다:

```python
def seed_from_sweep(sweep: dict, spec) -> tuple[CornerSet, dict]:
    """... (기존 독스트링 유지) ...

    **반환형이 `(CornerSet, record)`다.** record 는 `corner_seed` 이벤트에
    그대로 실리고, 호출부는 그것을 **무조건** 적는다 - 피복을 쓸 때만 적으면
    "오늘 방식으로 골랐다"와 "기록하는 코드가 사라졌다"가 같은 침묵이 된다.

    `spec` 인자를 이제 **실제로 읽는다**(`corner_reduction.coverage`,
    `all_criteria`, `testbenches`). 예전 독스트링이 "이 함수는 spec 안의 어떤
    것도 읽지 않는다"고 적고 있었는데, 그 문장은 이 커밋으로 낡았다."""
    coverage = getattr(getattr(spec, "corner_reduction", None), "coverage", None)
    n_tb = len(getattr(spec, "testbenches", ()) or ()) or 1

    if coverage is None:
        chosen: list = []
        for raw in sweep.get("worst_case_corners", {}).values():
            point = _as_point(raw)
            if point not in chosen:
                chosen.append(point)
        record = {
            "mode": "argmax", "epsilon": None, "tau": None,
            "covered": len(chosen), "total": len(chosen), "dropped": [],
        }
    else:
        chosen, cover_record = coverage_seed(sweep, list(spec.all_criteria), coverage)
        record = {
            "mode": "coverage", "epsilon": coverage.epsilon, "tau": coverage.tau,
            **cover_record,
        }

    corners = (NOMINAL, *chosen)
    cs = CornerSet(corners=corners, probe_order=_probe_order(sweep, corners))
    record["seed_size"] = len(chosen)
    # NOMINAL + 씨앗 + 탐침 1. 벽시계는 이 값과 워커 수의 관계로 결정되므로
    # (테스트벤치가 병렬 바깥이다) 판정 지표는 이것이지 웨이브가 아니다.
    record["points_per_tb"] = len(corners) + (1 if cs.probe_order else 0)
    record["testbenches"] = n_tb
    return cs, record
```

- [ ] **Step 4: 호출부를 고친다**

`src/analogcoder/cli.py:561`을 바꾼다:

```python
            seed_cs, seed_record = seed_from_sweep(baseline_sweep, spec)
            state.log_event("corner_seed", seed_record)
            corner_state = CornerState(seed_cs)
```

`tests/unit/test_corner_reduction_bandgap_ngspice.py:82`의
`return spec, seed_from_sweep(entry_sweep, spec)`를
`return spec, seed_from_sweep(entry_sweep, spec)[0]`으로 고친다.
`tests/unit/test_corner_selection.py`의 기존 `seed_from_sweep(...)` 호출도
전부 `[0]`을 붙이거나 언패킹으로 고친다.

- [ ] **Step 5: 통과를 확인한다**

Run: `.venv/bin/python -m pytest -m "not slow" -q`
Expected: PASS (1405 + 신규)

- [ ] **Step 6: 커밋**

```bash
git add src/analogcoder/corner_selection.py src/analogcoder/cli.py tests/unit/
git commit -m "feat: 씨앗이 무엇으로 골라졌는지가 history.jsonl 에 남는다

corner_seed 이벤트를 **무조건** 쓴다. 피복을 쓸 때만 적으면 '오늘 방식으로
골랐다'와 '기록하는 코드가 사라졌다'가 같은 침묵이 된다.

dropped 가 핵심 칸이다 — ε 이 겹침을 전혀 만들지 못하면 [] 이고, 그것이
'줄일 것이 없었다'를 말한다. points_per_tb 를 싣는 이유는 그것이 알고리즘
주장의 지표이기 때문이다(웨이브는 워커 수에 걸린 계단이라 기계가 바뀌면
뒤집힌다)."
```

---

### Task 4: `search_ab.py`에 코너 체제 인자

**Files:**
- Modify: `scripts/search_ab.py`
- Test: `tests/unit/test_search_ab_args.py` (신규)

**Interfaces:**
- Consumes: `spec.CoverageConfig` (Task 1)
- Produces:
  - `search_ab.parse_corner_regime(text: str) -> CoverageConfig | None`
    — `"argmax"` → `None`, `"coverage:0.03:1.0"` → `CoverageConfig(0.03, 1.0)`
  - `--corner-regime` 인자 (`action="append"`, 정확히 두 번)

- [ ] **Step 1: 실패 테스트를 쓴다**

```python
"""`search_ab.py`의 인자 파싱. 스크립트를 import 해서 순수 함수만 부른다 -
시뮬레이션은 돌리지 않는다."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from search_ab import parse_corner_regime  # noqa: E402


def test_argmax_means_no_coverage_config():
    assert parse_corner_regime("argmax") is None


def test_coverage_carries_epsilon_and_tau():
    cfg = parse_corner_regime("coverage:0.03:1.0")

    assert cfg.epsilon == 0.03 and cfg.tau == 1.0


@pytest.mark.parametrize("text", ["coverage", "coverage:0.03", "coverage:a:1.0", "nope"])
def test_an_unreadable_regime_is_refused(text):
    """조용히 argmax 로 떨어지면 두 쪽이 같은 체제로 돌면서 기록에는 다른
    이름이 실린다 - 격자의 셀 하나가 통째로 거짓이 된다."""
    with pytest.raises(ValueError):
        parse_corner_regime(text)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_search_ab_args.py -q`
Expected: FAIL — `ImportError: cannot import name 'parse_corner_regime'`

- [ ] **Step 3: 구현한다**

`scripts/search_ab.py`에 추가한다(`parse_knob` 근처):

```python
def parse_corner_regime(text: str):
    """`argmax` 또는 `coverage:<epsilon>:<tau>`.

    **읽을 수 없으면 거부한다.** 조용히 argmax 로 떨어지면 두 쪽이 같은
    체제로 돌면서 기록에는 다른 이름이 실리고, 격자의 셀 하나가 통째로
    거짓이 된다."""
    from analogcoder.spec import CoverageConfig

    if text == "argmax":
        return None
    parts = text.split(":")
    if len(parts) != 3 or parts[0] != "coverage":
        raise ValueError(
            f"unreadable corner regime {text!r}: use 'argmax' or 'coverage:<eps>:<tau>'"
        )
    try:
        epsilon, tau = float(parts[1]), float(parts[2])
    except ValueError:
        raise ValueError(
            f"unreadable corner regime {text!r}: epsilon and tau must be numbers"
        ) from None
    return CoverageConfig(epsilon=epsilon, tau=tau)
```

`main()`의 인자에 더한다:

```python
    parser.add_argument(
        "--corner-regime",
        action="append",
        type=parse_corner_regime,
        default=[],
        metavar="argmax|coverage:EPS:TAU",
        help="쪽마다의 코너 체제. 전략과 마찬가지로 정확히 두 번 준다. "
             "생략하면 양쪽 다 argmax.",
    )
```

검증과 기본값 채우기를 `--strategy` 검증 바로 아래에 더한다:

```python
    if not args.corner_regime:
        args.corner_regime = [None, None]
    if len(args.corner_regime) != 2:
        parser.error("--corner-regime을 정확히 두 번 주어야 한다 (또는 아예 주지 않는다)")
```

`run_side`에 `corner_regime`을 넘겨, 그 쪽의 `spec.corner_reduction.coverage`를
바꿔 끼운다. `run_side` 안에서 spec 을 만든 직후:

```python
    if corner_regime is not None:
        from dataclasses import replace as _replace
        from analogcoder.spec import CornerReduction

        base = spec.corner_reduction or CornerReduction()
        spec.corner_reduction = _replace(base, coverage=corner_regime)
```

그리고 기록(`record`)에 `"corner_regime": "argmax" if corner_regime is None
else f"coverage:{corner_regime.epsilon}:{corner_regime.tau}"`를 더한다.
자기 검사 조건도 넓힌다:

```python
        should_assert = (args.strategy[0] == args.strategy[1]
                         and args.corner_regime[0] == args.corner_regime[1])
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_search_ab_args.py -q`
Expected: PASS (5개)

- [ ] **Step 5: 자기 검사를 실제로 돌린다**

Run:
```bash
.venv/bin/python scripts/search_ab.py \
  --spec benchmarks/bandgap/spec.yaml \
  --knob TRIMAMP.Xt:W:decrease \
  --strategy coordinate_descent --strategy coordinate_descent \
  --corner-regime argmax --corner-regime argmax \
  --out-dir runs/search_ab --name selfcheck_regime
```
Expected: 두 기록이 완전히 같다(`--assert-identical`이 자동으로 켜진다).
다르면 통제되지 않은 무언가가 남아 있다는 뜻이고, 그 상태의 격자는 아무것도
증명하지 못한다.

- [ ] **Step 6: 커밋**

```bash
git add scripts/search_ab.py tests/unit/test_search_ab_args.py
git commit -m "feat: A/B 하니스가 코너 체제를 인자로 받는다 — factorial 의 첫 축

단계 1 은 중간 루프가 보는 코너 집합을 바꾸고, 최적화기의 가드 밴드 허용치가
그 집합의 최악값에서 유도된다. 즉 단계 1 이 단계 3 의 입력을 바꾼다 — 단독
평가로는 안 보이는 상호작용이다.

읽을 수 없는 체제는 거부한다. 조용히 argmax 로 떨어지면 두 쪽이 같은 체제로
돌면서 기록에는 다른 이름이 실려, 격자의 셀 하나가 통째로 거짓이 된다."
```

---

### Task 5: 격자 실행과 조합 판정

**Files:**
- Create: `docs/superpowers/specs/2026-07-29-theory-combination-results.md`

**Interfaces:**
- Consumes: Task 3의 `corner_seed` 기록, Task 4의 `--corner-regime`
- Produces: 판정 문서 (코드 없음)

- [ ] **Step 1: 선행(무효) 조건을 먼저 확인한다**

각 셀을 돌리기 **전에**, 그 셀에서 그 인자가 발화할 수 있는지 확인한다.

Run:
```bash
.venv/bin/python scripts/coverage_feasibility.py benchmarks/bandgap/spec_pvt.yaml 4 3 2
```

확인할 것: 쓰려는 ε에서 `|seed|`가 argmax 씨앗보다 작을 것. 같으면 그 셀은
**판정하지 않고 무효로 기록한다** — D1과 이 프로젝트의 첫 시도가 걸린 자리다.

- [ ] **Step 2: 2×2 격자를 돌린다 (노브 2개 이상)**

**`--knob`을 반드시 두 번 이상 준다.** 단계 3의 불채택이 좁았던 이유가
노브 하나짜리 순위였다.

```bash
for regime in argmax coverage:0.03:1.0; do
  .venv/bin/python scripts/search_ab.py \
    --spec benchmarks/bandgap/spec_pvt.yaml \
    --knob TRIMAMP.Xt:W:decrease --knob TRIMAMP.XRz:l:increase \
    --strategy coordinate_descent --strategy mads \
    --corner-regime "$regime" --corner-regime "$regime" \
    --out-dir runs/search_ab --name "grid_${regime//:/_}"
done
```

Expected: 셀 4개(체제 2 × 전략 2). 단계 3 실측 기준 한 쪽당 ~362초이므로
**약 25분**.

- [ ] **Step 3: 필요조건 2·3을 실제 루프 실행으로 잰다**

**격자로는 이 둘을 잴 수 없다.** `search_ab.py`는 **최적화 단계**를 돌리고,
잠긴 비대칭과 재진입은 **튜닝 루프**의 성질이다. 그래서 별도 실행이 필요하고,
그것은 LLM이 끼므로 결정론이 아니다 — 그 사실을 결과 문서에 적는다.

`corner_reduction:` 블록을 이미 가진 스펙은 `spec_corner_reduction.yaml`
하나뿐이다. 거기에 `coverage:`를 더한 사본으로 돈다.

```bash
# 사본을 만들고 coverage 블록을 더한다 (원본은 argmax 대조군으로 남긴다)
cp benchmarks/bandgap/spec_corner_reduction.yaml \
   benchmarks/bandgap/spec_corner_coverage.yaml
# spec_corner_coverage.yaml 의 corner_reduction: 아래에 다음을 더한다:
#   coverage:
#     epsilon: 0.03
#     tau: 1.0

for spec in spec_corner_reduction spec_corner_coverage; do
  .venv/bin/analogcoder --spec "benchmarks/bandgap/${spec}.yaml" \
    --run-dir "runs/regime_${spec}"
done
```

두 실행의 `history.jsonl`에서 확인한다:

- **필요조건 2 (잠긴 비대칭):** `corner_path_disagreement` 이벤트가 **0건**일
  것. 이 이벤트는 판정 스윕이 실패한 코너가 전부 이미 중간 집합 안에 있을 때
  나온다 — 즉 두 실행 경로가 같은 덱의 같은 코너를 두고 다른 말을 한 것이고,
  중간 FAIL이 전체 스윕에서 재현되지 않은 경우다.
  **주의:** CLAUDE.md가 기록한 대로, 두 갈래 창(`_min`/`_max` 쌍)에서는 이
  이벤트가 LLM 때문이 아니라 **구조적으로** 날 수 있다. 하나라도 나오면 그
  기준이 두 갈래 창의 한쪽인지 **먼저** 본다.
- **필요조건 3 (재진입 불변):** `corner_reduction` 재진입 횟수가 두 실행에서
  같을 것. CLAUDE.md 실측으로 이 스펙에서는 재진입이 **0회**이므로, 기대값은
  양쪽 0이다. 한쪽만 0이 아니면 동작이 바뀐 것이다.

`spec_corner_reduction.yaml`은 §2 표에서 **단계 1의 여지가 없는** 스펙이다
(점/tb가 이미 워커 수 안). 그것이 오히려 이 확인에 맞다 — **성능이 바뀌지 않는
자리에서 안전성만 본다.**

- [ ] **Step 4: 사전 등록 규칙대로 판정한다**

설계 문서 §4-3에서 그대로 옮긴다. 결과를 본 뒤 바꾸지 않는다.

- **필요조건 1:** 놓친 위반 0건 — Step 1의 스크립트가 잰다
- **필요조건 2·3:** Step 3이 잰다
- **1차 판정:** 최적 조합에 그 인자가 들어 있고,
  `(목적값_without − 목적값_with) / |목적값_without| > 1e-3`일 것
- **동률:** 목적값이 같고 `points_per_tb`가 유의하게 적으면 채택
- **불채택:** 위 중 하나라도 어긋나면

- [ ] **Step 5: 결과 문서를 쓴다**

`docs/superpowers/specs/2026-07-29-theory-combination-results.md`에 적는다.
**불채택이어도 쓴다** — 이 저장소에는 그런 기록이 이미 넷 있고(밴딧, Ahuja
보상, D1, 단계 3), 부정 결과도 산출물이다.

반드시 담을 것:
- 셀별 코너 확인 통과 목적값, `points_per_tb`, 시뮬레이션 수, 벽시계
- **무효로 기록된 셀과 그 이유**
- 판정과, 그 부정 결과가 규칙 문장보다 좁다면 **얼마나 좁은지**
  (단계 3의 커밋이 이 형식을 세워 뒀다)
- **필요조건 2·3은 LLM이 낀 실행으로 쟀다는 것** — 격자와 달리 결정론이
  아니므로, 한 번의 실행이 그 조건의 증명이 아니라 **증거 하나**다
- 한계: 벤치마크 하나, 기계 하나, 워커 9개

- [ ] **Step 6: 커밋**

판정을 커밋 제목에 그대로 적는다 — "채택" / "불채택" / "무효" 중 하나.

```bash
git add docs/superpowers/specs/2026-07-29-theory-combination-results.md \
        benchmarks/bandgap/spec_corner_coverage.yaml
git commit -m "measure: 단계 1 × 단계 3 조합 격자 — 채택|불채택|무효"
```

---

## 완료 후

`CLAUDE.md`에 반영한다 — 이 저장소는 원장을 코드와 함께 갱신한다:

- `corner_reduction.coverage` 선언과 그것이 없을 때의 동작
- `corner_seed` 이벤트와 `dropped` 칸의 뜻
- 격자 판정 결과 (채택이든 불채택이든)
- **`seed_from_sweep`의 반환형이 바뀌었다는 것** — 옛 독스트링의 "이 함수는
  spec 안의 어떤 것도 읽지 않는다"가 낡았다
