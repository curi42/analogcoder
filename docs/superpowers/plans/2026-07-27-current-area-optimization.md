# 소모 전류·면적 최적화 (C) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 회로가 스펙을 통과한 뒤, 남은 마진을 써서 소모 전류를 줄이는 2단계 최적화를 추가한다.

**Architecture:** `run_orchestration`이 PASS를 반환한 뒤 `cli.py`가 `run_optimization`을
부른다 — `run_full_pvt_sweep`과 같은 자리이자 같은 패턴이다. LLM 에이전트는
*어느 노브를 어느 방향으로* 줄일지 순위만 매기고, 파이썬이 적용·측정·수락을
전부 결정론적으로 처리한다. 수락 규칙은 `verify_post`를 쓰지 않는다.
코너 확인과 이분 탐색은 `run_optimization`의 계약 안에 있다 — `cli.py`를
루프로 만들지 않기 위해서다.

**Tech Stack:** Python 3, dataclasses, pytest. 새 런타임 의존성 없음.

## Global Constraints

- 스펙: `docs/superpowers/specs/2026-07-27-current-area-optimization-design.md`
- **TDD.** 실패하는 테스트 → 실패 확인 → 구현 → 통과 확인 → 커밋.
- **수락/기각은 결정론적이다.** `verify_post`를 재사용하지 않는다. 그 계약은
  "악화되면 롤백"인데 좋은 최적화 단계는 마진을 의도적으로 소비한다.
- **최적화에는 FAIL이 없다.** 개선 못 하면 원래 통과하던 넷리스트를 돌려준다.
- **네 게이트를 전부 통과시킨다.** 특히 `check_stimulus_untouched` — 전류를
  줄이는 가장 쉬운 길은 공급 전압을 낮추는 것이고, 그건 E2 최종 리뷰가 잡은
  "자극을 키워 이득을 조작" 과 같은 모양이다. 재사용이 아니라 필수 조건.
- **LLM에게 수치를 맡기지 않는다.** 후보와 방향만 받는다.
- 토큰화는 `netlist.split_tokens`, 줄 읽기는 `netlist.logical_lines`.
- 테스트: `.venv/bin/python -m pytest -q`. 현재 495 passed, 2 skipped.
- 커밋 메시지는 영어, 주석과 문서는 한글.

## 파일 구조

**신규**

| 파일 | 책임 |
|---|---|
| `src/analogcoder/area.py` | 총 면적 계산 (`w × l × m` 합) |
| `src/analogcoder/optimizer.py` | 결정론적 탐색 루프, 수락 판정, 기록 |
| `src/analogcoder/agents/optimizer.py` | 순위 매긴 후보 제안 |
| `tests/unit/test_area_total.py` | Task 2 |
| `tests/unit/test_guard_band.py` | Task 3 |
| `tests/unit/test_optimizer_agent.py` | Task 4 |
| `tests/unit/test_optimizer.py` | Task 5 |
| `tests/unit/test_optimizer_corners.py` | Task 6 |
| `tests/unit/test_optimizer_bandgap_ngspice.py` | Task 8 |

**수정**

| 파일 | 변경 |
|---|---|
| `src/analogcoder/spec.py` | `OptimizeSpec` + `TargetSpec.optimize` |
| `src/analogcoder/judge_tools.py` | `guard_band_violations` |
| `src/analogcoder/schemas.py` | `OPTIMIZER_SCHEMA` |
| `src/analogcoder/cli.py` | `AGENT_NAMES`에 `"optimizer"`, `run_optimization` 배선 |
| `CLAUDE.md` | 아키텍처 절 |

## Task 순서의 이유

값싸고 독립적인 결정론 조각(면적, 여유분, 스펙 표면)을 먼저 만든다. 그것들이
있어야 루프의 수락 규칙을 테스트할 수 있다. 에이전트는 루프보다 먼저 만들어
루프가 가짜가 아닌 실제 스키마를 상대로 테스트되게 한다.

**Task 5와 6을 나눈 이유:** 5는 코너를 모르는 탐색이고 6은 그 위에 앵커·확인·
이분 탐색을 얹는다. 나누면 5의 수락 규칙을 코너 없이 단독으로 테스트할 수
있고, 6이 붙어도 5의 테스트가 전부 그대로 통과해야 한다는 것이 회귀 검사가
된다. 실제 시뮬레이션이 필요한 종단 테스트는 마지막이다.

---

### Task 1: `spec.yaml`의 `optimize` 블록

**Files:**
- Modify: `src/analogcoder/spec.py`
- Test: `tests/unit/test_spec.py`

**Interfaces:**
- Produces: `OptimizeSpec(objective: str, area_budget: float, guard_band: float)`,
  `TargetSpec.optimize: OptimizeSpec | None`

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/unit/test_spec.py`에 추가:

```python
def test_a_spec_without_an_optimize_block_has_none(tmp_path):
    # 선언이 없으면 최적화를 건너뛴다. None이어야 호출부가 그것을 구분한다.
    path = tmp_path / "s.yaml"
    path.write_text(
        "circuit_name: demo\ntestbenches:\n  - name: tb\n    netlist: n.cir\n"
        "    analyses: ['ac']\n    control_block: '.control\\n.endc\\n'\n"
        "    criteria: []\n"
    )
    assert load_spec(str(path)).optimize is None


def test_an_optimize_block_is_loaded_with_its_three_fields(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(
        "circuit_name: demo\n"
        "optimize:\n  objective: iq_ua\n  area_budget: 1.10\n  guard_band: 0.2\n"
        "testbenches:\n  - name: tb\n    netlist: n.cir\n"
        "    analyses: ['ac']\n    control_block: '.control\\n.endc\\n'\n"
        "    criteria: []\n"
    )
    opt = load_spec(str(path)).optimize

    assert opt.objective == "iq_ua"
    assert opt.area_budget == 1.10
    assert opt.guard_band == 0.2
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_spec.py -q`
Expected: FAIL — `AttributeError: 'TargetSpec' object has no attribute 'optimize'`

- [ ] **Step 3: 구현한다**

`src/analogcoder/spec.py`:

```python
@dataclass
class OptimizeSpec:
    """스펙에 여유가 있을 때 무엇을 어디까지 줄일지. 선언이 없으면
    최적화 단계 자체를 돌리지 않는다 - 조용히 안 도는 것과 명시적으로
    안 도는 것은 다르다."""

    objective: str
    area_budget: float
    guard_band: float
```

`TargetSpec`에 `optimize: OptimizeSpec | None = None`을 더하고, `load_spec`에
로더를 추가한다:

```python
def _load_optimize(raw: dict) -> OptimizeSpec | None:
    raw_opt = raw.get("optimize")
    if raw_opt is None:
        return None
    return OptimizeSpec(
        objective=raw_opt["objective"],
        area_budget=float(raw_opt["area_budget"]),
        guard_band=float(raw_opt["guard_band"]),
    )
```

`return TargetSpec(...)`에 `optimize=_load_optimize(raw)`를 더한다.

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_spec.py -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add src/analogcoder/spec.py tests/unit/test_spec.py
git commit -m "feat: load an optional optimize block from the spec"
```

---

### Task 2: `area.py` — 총 면적

**Files:**
- Create: `src/analogcoder/area.py`
- Test: `tests/unit/test_area_total.py`

**Interfaces:**
- Consumes: `netlist.parse_netlist`, `area_limits._resolved_token`(비공개 —
  필요하면 공개 헬퍼로 승격하고 `area_limits.py`의 호출부를 함께 고칠 것),
  `params.has_token`
- Produces: `total_area(netlist_text: str) -> AreaTotal`,
  `AreaTotal(area: float, counted: int, skipped: int)`

**면적의 정의:** 해소 가능한 소자에 대한 `w × l × m`의 합. `.option scale`을
반영한다(`Component.geometry_scale`). `nf`는 **포함하지 않는다** — 핑거 분할은
총 폭을 바꾸지 않으므로 면적 중립이다.

**부분 합계도 비교 가능하다:** 최적화는 값만 바꾸고 소자를 추가·삭제하지
않으므로, 해소되는 소자 집합이 단계 전후로 같다. 그래서 `skipped`가 0이 아니어도
두 총합의 *비율*은 의미가 있다. `counted`/`skipped`를 함께 돌려주어 호출부가
커버리지를 알 수 있게 한다.

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/unit/test_area_total.py`:

```python
from analogcoder.area import total_area

DECK = (
    "* t\n"
    "M1 d g s b NCH w=2e-6 l=1e-6 m=2\n"
    "M2 d g s b NCH w=4e-6 l=1e-6 m=1\n"
    ".end\n"
)


def test_area_is_w_times_l_times_m_summed():
    # M1: 2u*1u*2 = 4e-12,  M2: 4u*1u*1 = 4e-12
    result = total_area(DECK)

    assert result.area == pytest.approx(8e-12)
    assert result.counted == 2
    assert result.skipped == 0


def test_nf_does_not_change_area():
    # nf는 총 폭을 나누기만 한다 - w=2u nf=2는 1u 핑거 둘, 총 폭은 그대로 2u.
    with_nf = DECK.replace("m=2\n", "m=2 nf=4\n")

    assert total_area(with_nf).area == pytest.approx(total_area(DECK).area)


def test_option_scale_is_honoured():
    scaled = "* t\n.option scale=1.0u\nM1 d g s b NCH w=2 l=1 m=2\n.end\n"

    assert total_area(scaled).area == pytest.approx(4e-12)


def test_an_unresolvable_device_is_skipped_and_counted_as_such():
    # 조용히 0으로 치면 총합이 거짓이 된다. 건너뛴 개수를 드러낸다.
    deck = DECK.replace("M2 d g s b NCH w=4e-6", "M2 d g s b NCH w='wx*2'")

    result = total_area(deck)

    assert result.counted == 1
    assert result.skipped == 1
    assert result.area == pytest.approx(4e-12)


def test_a_device_without_m_counts_as_one():
    deck = "* t\nM1 d g s b NCH w=2e-6 l=1e-6\n.end\n"

    assert total_area(deck).area == pytest.approx(2e-12)


def test_a_device_with_an_unresolvable_m_is_skipped_not_guessed():
    # area_limits가 같은 이유로 m을 추측하지 않는다 - 여기서도 같다.
    deck = "* t\nM1 d g s b NCH w=2e-6 l=1e-6 m=mm\n.end\n"

    result = total_area(deck)

    assert result.counted == 0
    assert result.skipped == 1
```

`import pytest`를 파일 상단에 넣는다.

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_area_total.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'analogcoder.area'`

- [ ] **Step 3: 구현한다**

`src/analogcoder/area.py`:

```python
from dataclasses import dataclass

from analogcoder.netlist import Component, parse_netlist
from analogcoder.params import has_token


@dataclass(frozen=True)
class AreaTotal:
    """해소 가능한 소자의 면적 합과 그 커버리지.

    최적화는 값만 바꾸고 소자를 더하거나 빼지 않으므로, 해소되는 소자 집합이
    단계 전후로 같다. 그래서 skipped가 0이 아니어도 두 총합의 *비율*은
    의미가 있다. 그래도 개수를 드러내는 이유는, 커버리지가 낮은 채로 비율만
    믿는 상황을 호출부가 알아차릴 수 있어야 하기 때문이다."""

    area: float
    counted: int
    skipped: int


def _dimension(component: Component, token: str) -> float | None:
    from analogcoder.area_limits import _resolved_token

    value = _resolved_token(component, token)
    if value is None:
        return None
    return value * component.geometry_scale


def _multiplicity(component: Component) -> float | None:
    from analogcoder.area_limits import _multiplicity as area_multiplicity

    return area_multiplicity(component)


def total_area(netlist_text: str) -> AreaTotal:
    """소자별 `w × l × m`의 합. nf는 제외한다 - 핑거 분할은 총 폭을
    바꾸지 않으므로 면적 중립이다."""
    parsed = parse_netlist(netlist_text)
    components = list(parsed.top_components) + [
        c for subckt in parsed.subckts.values() for c in subckt.components
    ]

    area = 0.0
    counted = 0
    skipped = 0
    for component in components:
        if not (has_token(component, "w") and has_token(component, "l")):
            continue
        width = _dimension(component, "w")
        length = _dimension(component, "l")
        multiplicity = _multiplicity(component)
        if width is None or length is None or multiplicity is None:
            skipped += 1
            continue
        area += width * length * multiplicity
        counted += 1

    return AreaTotal(area=area, counted=counted, skipped=skipped)
```

`_resolved_token`과 `_multiplicity`를 `area_limits`에서 끌어 쓰는 것이
비공개 이름 참조라 마음에 걸리면, 두 함수를 공개 이름으로 승격하고
`area_limits.py`의 기존 호출부를 함께 고친 뒤 여기서 정상 import하라. 둘 중
하나를 고르고 이유를 커밋 메시지에 적을 것. 함수 안 import는 순환 의존을
피하려는 임시방편이므로 남기지 말 것.

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_area_total.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: 실제 벤치마크에서 커버리지를 확인한다**

Run:
```bash
.venv/bin/python -c "
from analogcoder.area import total_area
for f in ['benchmarks/bandgap/netlist.cir','benchmarks/two_stage_opamp/netlist.cir']:
    r = total_area(open(f).read())
    print(f, r.area, 'counted', r.counted, 'skipped', r.skipped)
"
```
Expected: 두 덱 모두 `counted`가 0이 아니고 면적이 물리적으로 그럴듯한 크기
(bandgap은 수천 µm² 규모). `skipped`가 `counted`보다 크면 커버리지가 너무
낮은 것이니 왜 그런지 보고서에 적을 것.

- [ ] **Step 6: 커밋**

```bash
git add src/analogcoder/area.py tests/unit/test_area_total.py
git commit -m "feat: compute total device area without simulating"
```

---

### Task 3: 가드밴드 판정

**Files:**
- Modify: `src/analogcoder/judge_tools.py`
- Test: `tests/unit/test_guard_band.py`

**Interfaces:**
- Consumes: `spec.Criterion`
- Produces:
  - `guard_band_violations(measurements: dict, criteria: list[Criterion], allowances: dict[str, float]) -> list[str]`
    — 빈 목록이면 전부 여유분을 지킨 것. `allowances`는 **기준 이름 → 남겨야 할
    절대량**(측정 단위)이며, 없는 기준은 여유분 0으로 본다
  - `corner_allowances(nominal: dict, sweep: dict) -> dict[str, float]` —
    코너 스윕 결과와 nominal 측정에서 기준별 실측 스프레드를 뽑는다
  - `ratio_allowances(criteria: list[Criterion], guard_band: float) -> dict[str, float]` —
    코너를 잴 수 없는 스펙용 대체물, `g·|T|`

**여유분은 절대량이지 비율이 아니다.** 비율을 임계값에 곱하는 형태(`T·(1±g)`)는
음수 임계값에서 뒤집힌다: `psr_plus_db <= -10`에 `T·(1-0.2)`를 적용하면 `<= -8`
이 되어 원래보다 **느슨해진다**. 절대량을 빼고 더하는 형태는 부호와 무관하게
항상 엄격해지는 방향이다. `ratio_allowances`가 `g·|T|`로 절대량을 만들어 주므로
`guard_band_violations` 자신은 부호 문제를 아예 만나지 않는다.

각 criterion을 **자기 임계값에 대해 따로** 판정한다. 같은 measurement에 `>=`와
`<=`가 걸리는 양쪽 창을 하나로 뭉개면 한쪽이 사라지는데, `pvt.py`에서 정확히
그 결함이 두 번 있었다.

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/unit/test_guard_band.py`:

```python
from analogcoder.judge_tools import guard_band_violations
from analogcoder.spec import Criterion


def _c(name, measurement, operator, threshold):
    return Criterion(name=name, measurement=measurement, operator=operator, threshold=threshold)


def test_a_comfortable_measurement_does_not_violate():
    crit = [_c("iq", "iq_ua", "<=", 300.0)]

    assert guard_band_violations({"iq_ua": 200.0}, crit, {"iq": 60.0}) == []


def test_a_measurement_inside_the_allowance_violates_even_though_it_passes():
    # 250은 기준을 통과하지만 240이라는 여유선 안에 있다.
    crit = [_c("iq", "iq_ua", "<=", 300.0)]

    violations = guard_band_violations({"iq_ua": 250.0}, crit, {"iq": 60.0})

    assert len(violations) == 1 and "iq" in violations[0]


def test_an_allowance_tightens_a_negative_threshold_instead_of_loosening_it():
    # psr <= -10, 여유분 2 이면 허용선은 -12 이다. 비율을 곱하는 형태였다면
    # -8 이 되어 원래보다 느슨해졌을 것이다.
    crit = [_c("psr", "psr_db", "<=", -10.0)]

    assert guard_band_violations({"psr_db": -11.0}, crit, {"psr": 2.0}) != []
    assert guard_band_violations({"psr_db": -13.0}, crit, {"psr": 2.0}) == []


def test_a_lower_bound_tightens_upward():
    crit = [_c("gain", "gain_db", ">=", 20.0)]

    assert guard_band_violations({"gain_db": 22.0}, crit, {"gain": 4.0}) != []
    assert guard_band_violations({"gain_db": 25.0}, crit, {"gain": 4.0}) == []


def test_both_sides_of_a_two_sided_window_are_judged_separately():
    # 같은 measurement에 걸린 두 기준을 하나로 뭉개면 한쪽이 사라진다 -
    # pvt.py에서 이 모양의 결함이 두 번 있었다.
    crit = [
        _c("vbg_min", "vbg", ">=", 1.20),
        _c("vbg_max", "vbg", "<=", 1.28),
    ]

    violations = guard_band_violations(
        {"vbg": 1.21}, crit, {"vbg_min": 0.02, "vbg_max": 0.02}
    )

    assert len(violations) == 1 and "vbg_min" in violations[0]


def test_a_missing_measurement_is_a_violation_not_a_pass():
    crit = [_c("iq", "iq_ua", "<=", 300.0)]

    assert guard_band_violations({}, crit, {"iq": 60.0}) != []


def test_a_criterion_without_an_allowance_only_has_to_pass():
    crit = [_c("iq", "iq_ua", "<=", 300.0)]

    assert guard_band_violations({"iq_ua": 299.0}, crit, {}) == []


def test_corner_allowances_are_the_measured_spread_per_criterion():
    # 스윕의 criteria[].actual 은 기준별 최악 코너 값이다. nominal 과의 거리가
    # 그 기준이 코너에서 밀려나는 양이고, 그것이 곧 남겨야 할 여유분이다.
    from analogcoder.judge_tools import corner_allowances

    nominal = {"iq_ua": 235.0, "vbg": 1.24}
    sweep = {
        "criteria": [
            {"name": "iq", "actual": 268.0},
            {"name": "vbg_min", "actual": 1.196},
        ]
    }
    crit = [_c("iq", "iq_ua", "<=", 300.0), _c("vbg_min", "vbg", ">=", 1.20)]

    allowances = corner_allowances(nominal, sweep, crit)

    assert allowances["iq"] == pytest.approx(33.0)
    assert allowances["vbg_min"] == pytest.approx(0.044)


def test_a_corner_value_that_is_missing_yields_no_allowance_rather_than_zero():
    # 0 을 넣으면 "코너가 이 기준을 전혀 안 움직인다"는 거짓 사실이 된다.
    # 없는 것은 없는 채로 둔다 - 호출부가 그것을 구분할 수 있어야 한다.
    from analogcoder.judge_tools import corner_allowances

    crit = [_c("iq", "iq_ua", "<=", 300.0)]

    assert corner_allowances({"iq_ua": 235.0}, {"criteria": []}, crit) == {}


def test_ratio_allowances_are_the_fallback_when_corners_cannot_be_measured():
    from analogcoder.judge_tools import ratio_allowances

    crit = [_c("iq", "iq_ua", "<=", 300.0), _c("psr", "psr_db", "<=", -10.0)]

    allowances = ratio_allowances(crit, 0.2)

    assert allowances["iq"] == pytest.approx(60.0)
    assert allowances["psr"] == pytest.approx(2.0)  # |T| 를 쓰므로 부호와 무관
```

`import pytest`를 파일 상단에 넣는다.

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_guard_band.py -q`
Expected: FAIL — `ImportError: cannot import name 'guard_band_violations'`

- [ ] **Step 3: 구현한다**

`src/analogcoder/judge_tools.py`에 추가:

```python
_LOWER_BOUND = (">=", ">")
_UPPER_BOUND = ("<=", "<")


def guard_band_violations(
    measurements: dict, criteria: list[Criterion], allowances: dict[str, float]
) -> list[str]:
    """여유분을 지키지 못한 기준의 설명 목록. 빈 목록이면 전부 지킨 것.

    최적화는 마진을 의도적으로 소비하므로 "통과했는가"만으로는 부족하다.
    임계값에 바짝 붙은 채로 멈추면 코너와 모델 변동에서 무너진다.

    allowances는 기준 이름 → 남겨야 할 **절대량**이다. 비율을 임계값에 곱하는
    형태였다면 음수 임계값에서 뒤집혔을 것이다 - `psr <= -10`에 `T·(1-0.2)`는
    `<= -8`이라 원래보다 느슨하다. 절대량을 빼고 더하는 형태는 부호와 무관하게
    항상 엄격해지는 방향이다.

    여유분이 없는 기준은 통과만 하면 된다. 각 criterion을 자기 임계값에 대해
    따로 판정한다 - 같은 measurement에 `>=`와 `<=`가 걸린 양쪽 창을 하나로
    뭉개면 한쪽이 사라지는데, pvt.py에서 그 모양의 결함이 두 번 있었다."""
    violations: list[str] = []

    for c in criteria:
        actual = measurements.get(c.measurement)
        if actual is None:
            violations.append(f"{c.name}: measurement {c.measurement!r} is missing")
            continue

        allowance = allowances.get(c.name, 0.0)
        if c.operator in _UPPER_BOUND:
            limit = c.threshold - allowance
            if actual > limit:
                violations.append(
                    f"{c.name}: {actual:g} exceeds the guarded limit {limit:g} "
                    f"(threshold {c.threshold:g}, allowance {allowance:g})"
                )
        elif c.operator in _LOWER_BOUND:
            limit = c.threshold + allowance
            if actual < limit:
                violations.append(
                    f"{c.name}: {actual:g} is below the guarded limit {limit:g} "
                    f"(threshold {c.threshold:g}, allowance {allowance:g})"
                )
        # "==" 에는 의미 있는 여유분이 없다 - 통과 여부는 evaluate_criteria가 본다.

    return violations


def corner_allowances(
    nominal: dict, sweep: dict, criteria: list[Criterion]
) -> dict[str, float]:
    """기준별로 코너가 nominal에서 밀어내는 실측 거리.

    균일한 비율을 추측하는 대신, 이미 값을 치른 코너 스윕에서 읽는다. 코너에
    둔감한 기준은 여유를 더 쓸 수 있고 민감한 기준은 자동으로 보수적이 된다 -
    숫자 하나로는 못 하는 구분이다.

    스윕에 값이 없는 기준은 **넣지 않는다.** 0을 넣으면 "코너가 이 기준을
    전혀 안 움직인다"는 거짓 사실이 되고, 그건 이 저장소가 반복해서 당한
    조용한 무력화와 같은 모양이다."""
    by_name = {c.name: c for c in criteria}
    allowances: dict[str, float] = {}

    for entry in sweep.get("criteria", []):
        criterion = by_name.get(entry.get("name"))
        worst = entry.get("actual")
        if criterion is None or worst is None:
            continue
        nominal_value = nominal.get(criterion.measurement)
        if nominal_value is None or math.isnan(worst) or math.isnan(nominal_value):
            continue
        allowances[criterion.name] = abs(worst - nominal_value)

    return allowances


def ratio_allowances(criteria: list[Criterion], guard_band: float) -> dict[str, float]:
    """코너를 잴 수 없는 스펙용 대체 여유분, `g·|T|`.

    `|T|`를 쓰므로 임계값의 부호와 무관하게 양수 절대량이 나오고, 그래서
    guard_band_violations 쪽이 부호 문제를 아예 만나지 않는다."""
    return {c.name: guard_band * abs(c.threshold) for c in criteria}
```

`import math`가 파일 상단에 이미 있다.

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_guard_band.py -q`
Expected: PASS (10 passed)

- [ ] **Step 5: 커밋**

```bash
git add src/analogcoder/judge_tools.py tests/unit/test_guard_band.py
git commit -m "feat: derive each criterion's margin allowance from measured corners"
```

---

### Task 4: 최적화 후보 에이전트

**Files:**
- Create: `src/analogcoder/agents/optimizer.py`
- Modify: `src/analogcoder/schemas.py`
- Test: `tests/unit/test_optimizer_agent.py`

**Interfaces:**
- Consumes: `agents.agent_runtime.run_agent`, `agents.backend.AgentBackend`
- Produces: `OPTIMIZER_SCHEMA`,
  `propose_candidates(structure_view: str, margins: list[dict], objective: str, netlist_view: str, backend: AgentBackend) -> dict`
  — 반환 dict는 `{"candidates": [{"refdes", "param", "direction", "reasoning"}], "overall_reasoning": str}`

`direction`은 `"increase"` 또는 `"decrease"`다. 전류를 줄이는 방향이 항상
축소는 아니다 — 채널 길이를 늘리면 같은 폭에서 전류가 줄어든다. 방향은 열어
두고, 커지는 쪽은 면적 예산과 에어리어 게이트가 막는다.

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/unit/test_optimizer_agent.py`:

```python
import pytest

from analogcoder.agents.optimizer import propose_candidates
from analogcoder.schemas import OPTIMIZER_SCHEMA


class FakeBackend:
    def __init__(self):
        self.calls = []

    async def run(self, *, system_prompt, user_prompt, output_schema, tools=None):
        self.calls.append({"system": system_prompt, "user": user_prompt, "schema": output_schema})
        return {
            "candidates": [
                {"refdes": "AMP.M1", "param": "m", "direction": "decrease",
                 "reasoning": "tail current source"}
            ],
            "overall_reasoning": "cut the tail first",
        }


@pytest.mark.asyncio
async def test_the_agent_receives_the_objective_and_the_margins():
    backend = FakeBackend()

    await propose_candidates(
        "circuit: demo\nblocks:\n  AMP …",
        [{"name": "iq", "actual": 235.0, "target": "<=300.0"}],
        "iq_ua",
        "* deck\nM1 d g s b NCH w=2e-6\n",
        backend,
    )

    user = backend.calls[0]["user"]
    assert "iq_ua" in user
    assert "235" in user
    assert "AMP" in user


@pytest.mark.asyncio
async def test_the_agent_is_told_not_to_propose_numbers():
    backend = FakeBackend()

    await propose_candidates("s", [], "iq_ua", "n", backend)

    system = backend.calls[0]["system"]
    assert "direction" in system.lower()
    # 수치를 내지 말라는 지시가 프롬프트에 있어야 한다. 약한 모델이
    # two_stage_opamp에서 Cc를 거꾸로 움직여 10 iteration을 태운 전력이 있다.
    assert "value" in system.lower()


def test_the_schema_forbids_a_numeric_proposal():
    item = OPTIMIZER_SCHEMA["properties"]["candidates"]["items"]

    assert set(item["required"]) == {"refdes", "param", "direction", "reasoning"}
    assert "new_value" not in item["properties"]
    assert item["properties"]["direction"]["enum"] == ["increase", "decrease"]
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_optimizer_agent.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'analogcoder.agents.optimizer'`

- [ ] **Step 3: 구현한다**

`src/analogcoder/schemas.py`에 추가:

```python
OPTIMIZER_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "refdes": {
                        "type": "string",
                        "pattern": r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$",
                    },
                    "param": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
                    "direction": {"enum": ["increase", "decrease"]},
                    "reasoning": {"type": "string"},
                },
                "required": ["refdes", "param", "direction", "reasoning"],
            },
        },
        "overall_reasoning": {"type": "string"},
    },
    "required": ["candidates", "overall_reasoning"],
}
```

`src/analogcoder/agents/optimizer.py`:

```python
from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend
from analogcoder.schemas import OPTIMIZER_SCHEMA

OPTIMIZER_SYSTEM_PROMPT = """You are an analog circuit optimization specialist. The
circuit already meets every criterion in its specification. Your job is to find where
its remaining margin can be spent to reduce the stated objective - normally the
quiescent current.

Propose a RANKED list of candidate knobs, best first. For each, give the refdes, the
parameter, and the direction ("decrease" or "increase") - and nothing else.

Do NOT propose a numeric value. You are choosing WHICH knob and WHICH direction; a
deterministic search decides how far to move it and measures the result. A proposal
carrying a value will be rejected.

Direction is not always "decrease": lengthening a channel reduces current at a fixed
width, so "increase" on an `l` is a legitimate way to cut current. Reason about the
circuit, not about the word.

Rank by expected effect on the objective. A device that sets a bias current - a
current-mirror leg, a tail source - moves the objective directly. A device that only
sets matching or drive strength usually does not. The derived structure below reports
matched patterns (differential pairs, current mirrors, stacked pairs) and which block
drives or senses each net; use them.

Do not propose a change to the testbench's own sources. Lowering a supply reduces
current without improving the circuit, and it will be rejected by a deterministic gate.

refdes must identify exactly one component, qualified by its full subckt path when it
sits inside one (e.g. "BUF_N.Xcc"). param must be exactly a parameter name as it
appears on that component's line in the netlist below.

Respond via the structured output schema."""


async def propose_candidates(
    structure_view: str,
    margins: list[dict],
    objective: str,
    netlist_view: str,
    backend: AgentBackend,
) -> dict:
    user_prompt = (
        f"Objective to minimise: {objective}\n"
        f"Current netlist:\n{netlist_view}\n"
        f"Circuit structure (derived deterministically):\n{structure_view}\n"
        f"Criteria and how much margin each has left: {margins}"
    )
    return await run_agent(
        system_prompt=OPTIMIZER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=OPTIMIZER_SCHEMA,
        backend=backend,
    )
```

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_optimizer_agent.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: 커밋**

```bash
git add src/analogcoder/agents/optimizer.py src/analogcoder/schemas.py tests/unit/test_optimizer_agent.py
git commit -m "feat: add an optimizer agent that ranks knobs without naming values"
```

---

### Task 5: `optimizer.py` — 탐색 루프

**Files:**
- Create: `src/analogcoder/optimizer.py`
- Test: `tests/unit/test_optimizer.py`

**Interfaces:**
- Consumes: Task 1–4 전부, `netlist.apply_changes`,
  `netlist.check_refdes_resolution`, `netlist.check_param_applicability`,
  `netlist.check_stimulus_untouched`, `area_limits.check_area_growth`,
  `area_limits.index_baseline_components`, `judge_tools.evaluate_criteria`,
  `structure.derive_structure`, `signal_path.build_signal_paths`,
  `patterns.find_patterns`, `structure_view.*`, `state.RunState`
- Produces:
  `run_optimization(netlist_texts: dict[str, str], spec, state: RunState, agents: OptimizerAgents) -> dict`
  where `OptimizerAgents(propose: Callable, simulate: Callable, verify_corners: Callable | None = None)`
  and the result is
  `{"status": "OPTIMIZED" | "UNCHANGED" | "SKIPPED", "objective_before": float | None,
    "objective_after": float | None, "area_before": float, "area_after": float,
    "steps_accepted": int, "steps_rejected": int, "corner_confirmed": bool,
    "final_netlist_paths": dict}`

이 Task는 **코너를 모르는 탐색**까지만 만든다. 여유분은
`judge_tools.ratio_allowances(spec.all_criteria, spec.optimize.guard_band)`로
구하고, `corner_confirmed`는 항상 `False`다. 코너 확인과 이분 탐색, 그리고
실측 여유분으로의 전환은 Task 6이 이 위에 얹는다.

`verify_corners`는 여기서 쓰이지 않지만 dataclass에 미리 둔다 — Task 6이
시그니처를 바꾸지 않게 하려는 것이다.

**가짜 spec에 `pvt_corners=None`을 반드시 넣을 것.** 없으면 Task 6이 붙는
순간 `AttributeError`가 난다.

**단계 크기:** 감소는 ×0.9, 증가는 ÷0.9. 정수 파라미터(`m`, `nf`)는 그 방향으로
다음 정수로 가고 1 미만으로 내려가지 않는다. `m=4`의 감소는 3이다. 더 갈 수
없으면 그 후보를 소진 처리한다.

**정수 판정은 이름이 아니라 현재 값으로 한다** — 현재 값이 정수로 파싱되고
파라미터 이름이 `m` 또는 `nf`이면 정수로 다룬다. `area_limits`가 이미 같은 두
이름에 정수성을 요구하므로 두 곳이 어긋나면 안 된다.

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/unit/test_optimizer.py`:

```python
import json
from types import SimpleNamespace

import pytest

from analogcoder.optimizer import OptimizerAgents, run_optimization
from analogcoder.spec import Criterion, OptimizeSpec
from analogcoder.state import RunState

DECK = (
    "* t\n"
    ".subckt AMP a b vss\n"
    "M1 a b vss vss NCH w=2e-6 l=1e-6 m=4\n"
    ".ends AMP\n"
    "Xa p q 0 AMP\n"
    "Vdd vdd 0 DC 1.8\n"
    ".end\n"
)


def _spec(**overrides):
    tb = SimpleNamespace(
        name="tb",
        criteria=[Criterion(name="iq", measurement="iq_ua", operator="<=", threshold=300.0)],
        control_block=".control\nmeas dc iq_ua FIND i(Vdd) AT=27\n.endc\n",
    )
    base = dict(
        circuit_name="demo",
        testbenches=[tb],
        pvt_corners=None,   # Task 6이 이 속성을 읽는다. 없으면 AttributeError.
        optimize=OptimizeSpec(objective="iq_ua", area_budget=1.10, guard_band=0.2),
    )
    base.update(overrides)
    base["canonical"] = base["testbenches"][0]
    base["all_criteria"] = list(base["testbenches"][0].criteria)
    return SimpleNamespace(**base)


def _agents(measure_sequence, candidates=None):
    """measure_sequence: 시뮬레이션 호출마다 돌려줄 iq_ua 값."""
    seq = list(measure_sequence)
    calls = {"n": 0}

    async def simulate(netlist_texts, spec_arg):
        value = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return {"measurements": {"iq_ua": value}, "status": "success", "warnings": []}

    async def propose(structure_view, margins, objective, netlist_view):
        return {
            "candidates": candidates
            if candidates is not None
            else [{"refdes": "AMP.M1", "param": "m", "direction": "decrease",
                   "reasoning": "tail"}],
            "overall_reasoning": "x",
        }

    return OptimizerAgents(propose=propose, simulate=simulate), calls


@pytest.mark.asyncio
async def test_a_spec_without_an_optimize_block_is_skipped_and_says_so(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([200.0])

    result = await run_optimization({"tb": DECK}, _spec(optimize=None), state, agents)

    assert result["status"] == "SKIPPED"
    events = [json.loads(line) for line in open(state.history_path)]
    assert any(e["step"] == "optimize_skipped" for e in events)


@pytest.mark.asyncio
async def test_a_step_that_lowers_the_objective_is_accepted(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    # 기준선 235, 첫 단계 후 200 -> 개선이므로 수락, 그 다음은 정체.
    agents, _ = _agents([235.0, 200.0, 200.0, 200.0, 200.0])

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["status"] == "OPTIMIZED"
    assert result["objective_after"] < result["objective_before"]
    assert result["steps_accepted"] >= 1
    assert "m=3" in state.current_netlist_texts()["tb"]


@pytest.mark.asyncio
async def test_a_step_that_raises_the_objective_is_reverted(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 260.0, 260.0, 260.0])

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert "m=4" in state.current_netlist_texts()["tb"]


@pytest.mark.asyncio
async def test_a_step_that_breaks_the_guard_band_is_reverted(tmp_path):
    # 290은 iq<=300을 통과하지만 가드밴드 240을 넘는다. 목적값이 내려가도
    # 수락하면 안 된다 - 마진을 다 태워버린 상태가 된다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([295.0, 290.0, 290.0, 290.0])

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["status"] == "UNCHANGED"


@pytest.mark.asyncio
async def test_a_proposal_against_the_testbench_supply_never_reaches_simulation(tmp_path):
    # 전류를 줄이는 가장 쉬운 길은 공급을 낮추는 것이다. 게이트가 막아야 하고,
    # 시뮬레이션을 쓰기 전에 막아야 한다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, calls = _agents(
        [235.0],
        candidates=[{"refdes": "Vdd", "param": "value", "direction": "decrease",
                     "reasoning": "less supply, less current"}],
    )

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert calls["n"] == 1  # 기준선 측정 한 번뿐
    events = [json.loads(line) for line in open(state.history_path)]
    assert any(e["step"] == "optimize_step" and e.get("gate") for e in events)


@pytest.mark.asyncio
async def test_an_integer_parameter_steps_by_one_and_stops_at_one(tmp_path):
    deck = DECK.replace("m=4", "m=1")
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": deck})
    agents, calls = _agents([235.0])

    result = await run_optimization({"tb": deck}, _spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert calls["n"] == 1  # 후보가 소진되어 시뮬레이션이 더 돌지 않는다


@pytest.mark.asyncio
async def test_every_step_is_recorded_with_its_outcome(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 200.0, 200.0, 200.0])

    await run_optimization({"tb": DECK}, _spec(), state, agents)

    events = [json.loads(line) for line in open(state.history_path)]
    steps = [e for e in events if e["step"] == "optimize_step"]
    assert steps
    for e in steps:
        assert "refdes" in e and "param" in e and "accepted" in e
        assert "objective" in e and "area" in e
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_optimizer.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'analogcoder.optimizer'`

- [ ] **Step 3: 구현한다**

`src/analogcoder/optimizer.py`를 쓴다. 뼈대는 아래와 같고, 세부는 테스트가
정한다.

```python
from dataclasses import dataclass
from typing import Callable

from analogcoder.area import total_area
from analogcoder.area_limits import check_area_growth, index_baseline_components
from analogcoder.judge_tools import evaluate_criteria, guard_band_violations
from analogcoder.netlist import (
    apply_changes,
    check_param_applicability,
    check_refdes_resolution,
    check_stimulus_untouched,
    parse_spice_value,
)
from analogcoder.patterns import find_patterns
from analogcoder.signal_path import build_signal_paths
from analogcoder.structure import derive_structure
from analogcoder.structure_view import render_netlist, render_structure, select_focus

MAX_OPTIMIZE_STEPS = 20
STEP_RATIO = 0.9
_INTEGER_PARAMS = ("m", "nf")


@dataclass
class OptimizerAgents:
    propose: Callable
    simulate: Callable


def _is_integer_param(param: str) -> bool:
    """정수로 다룰 파라미터인가. area_limits가 이미 같은 두 이름에 정수성을
    요구하므로 두 곳이 어긋나면 안 된다."""
    return param.lower() in _INTEGER_PARAMS


def _next_value(current: float, param: str, direction: str) -> float | None:
    """한 단계 이동한 값. 더 갈 수 없으면 None (후보 소진)."""
    if _is_integer_param(param):
        step = -1 if direction == "decrease" else 1
        nxt = int(current) + step
        return None if nxt < 1 else float(nxt)
    return current * STEP_RATIO if direction == "decrease" else current / STEP_RATIO
```

루프의 뼈대:

1. `spec.optimize is None`이면 `optimize_skipped` 이벤트를 남기고
   `{"status": "SKIPPED", ...}`를 반환한다.
2. 기준선을 측정한다(`agents.simulate`). 목적값과 `total_area`를 기록한다.
   `index_baseline_components`는 **최적화 시작 시점의 넷리스트**로 만든다 —
   에어리어 게이트가 여기서 막아야 할 것은 최적화가 만든 성장이다.
3. `derive_structure` → `build_signal_paths` → `find_patterns` → `select_focus`
   (실패 넷이 없으므로 초점 씨앗도 없다; 전 블록 폴백이 정상 동작이다) →
   `render_structure` / `render_netlist`.
4. `agents.propose(...)`로 후보 목록을 받는다.
5. 후보를 순서대로 돌며, 각 후보에 대해 소진될 때까지 반복:
   - 현재 값을 넷리스트에서 읽고 `_next_value`로 다음 값을 만든다. `None`이면
     소진.
   - 변경 dict를 만들어 **네 게이트를 전부** 통과시킨다. 하나라도 막으면
     `optimize_step` 이벤트에 `gate`를 담아 기록하고 그 후보를 소진 처리한다.
   - `apply_changes`로 모든 테스트벤치에 적용하고 `state.push_netlist_version`.
   - `total_area`가 `area_budget`을 넘으면 **시뮬레이션 없이** 되돌린다.
   - `agents.simulate` → `evaluate_criteria`로 전 기준 통과 확인 →
     `guard_band_violations`가 비어 있는지 확인 → 목적값이 감소했는지 확인.
   - 셋 다 만족하면 수락(기준선 갱신), 아니면 `state.rollback()`.
   - 매 단계 `optimize_step` 이벤트를 남긴다: refdes, param, 이전/이후 값,
     objective, area, accepted, 기각 사유.
6. `MAX_OPTIMIZE_STEPS`에 도달하거나 후보가 모두 소진되면 종료한다.
7. 수락이 한 번이라도 있었으면 `OPTIMIZED`, 없으면 `UNCHANGED`.

현재 값 읽기는 `parse_netlist`로 구한 `Component.params[param]`(또는
`param == "value"`면 `Component.value`)을 `parse_spice_value`로 변환한다.
읽지 못하면 그 후보를 소진 처리하고 이유를 기록한다 — 추측하지 않는다.

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_optimizer.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: 전체 테스트를 돌린다**

Run: `.venv/bin/python -m pytest -q`
Expected: 회귀 없음.

- [ ] **Step 6: 커밋**

```bash
git add src/analogcoder/optimizer.py tests/unit/test_optimizer.py
git commit -m "feat: spend spec margin on the objective with a deterministic accept rule"
```

---

### Task 6: 코너 확인과 이분 탐색

**Files:**
- Modify: `src/analogcoder/optimizer.py`
- Test: `tests/unit/test_optimizer_corners.py`

**Interfaces:**
- Consumes: Task 5의 `run_optimization`, `judge_tools.corner_allowances`
- Produces: `OptimizerAgents.verify_corners: Callable | None`가 실제로 쓰인다.
  결과 dict에 `"corner_confirmed": bool`와 `"pvt_sweep": dict | None`이 담긴다.

**바닥 규칙: 최적화는 절대 시작보다 나쁜 결과를 내지 않는다.** 이게 없으면
최적화를 돌렸다는 이유로 통과하던 설계가 실패로 끝난다.

동작:

1. `spec.pvt_corners is None` 또는 `agents.verify_corners is None` — Task 5
   그대로. 비율 여유분, `corner_confirmed=False`. **검증하지 않은 것을 검증된
   것처럼 보고하지 않는다.**
2. 그렇지 않으면 **진입 스윕**을 한 번 돈다. 이것은 추가 비용이 아니라 앵커다 —
   "실패하면 시작점으로 되돌린다"가 안전하려면 시작점이 코너를 통과한다는 것을
   알아야 한다.
   - 진입 스윕이 실패하면 최적화를 하지 않는다. 코너를 못 버티는 설계에서
     마진을 더 깎을 이유가 없다. `UNCHANGED`, 사유 기록.
   - 통과하면 nominal 기준선 측정과 함께 `corner_allowances`로 기준별 실측
     여유분을 만든다.
3. 그 여유분으로 탐색한다(Task 5의 루프).
4. 수락이 하나도 없으면 `UNCHANGED`, 진입 스윕을 `pvt_sweep`으로 돌려준다.
5. 수락이 있으면 **확인 스윕**을 돈다. 통과하면 `OPTIMIZED`,
   `corner_confirmed=True`.
6. 실패하면 **이분 탐색한다.** `state.netlist_versions`가 수락된 버전을 전부
   들고 있다. 앵커 인덱스는 통과하고 끝 인덱스는 실패하므로, 통과하는 마지막
   인덱스를 이분 탐색한다. 착지 지점이 앵커면 `UNCHANGED`, 아니면 `OPTIMIZED`.
   스윕 횟수는 `ceil(log2(수락 단계 수)) + 2`를 넘지 않는다.

`state`를 특정 버전으로 되돌리는 수단이 `rollback()`(한 단계씩)뿐이면 반복
호출로 충분하다. 새 상태 API를 만들지 말 것.

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/unit/test_optimizer_corners.py`. Task 5의 `_spec`/`_agents` 헬퍼를
`tests/unit/test_optimizer.py`에서 import하거나, 공유 헬퍼로 옮기고 양쪽에서
쓴다(중복 정의하지 말 것 — 하나가 반드시 드리프트한다).

```python
def _corner_spec():
    spec = _spec()
    spec.pvt_corners = SimpleNamespace(process=["tt"], voltage=[1.8], temperature=[27.0])
    return spec


def _sweep(overall_pass, iq_actual):
    return {"overall_pass": overall_pass, "summary": "x",
            "criteria": [{"name": "iq", "actual": iq_actual}], "worst_case_corners": {}}


@pytest.mark.asyncio
async def test_without_corners_the_result_says_it_was_not_corner_confirmed(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 200.0, 200.0, 200.0])

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["corner_confirmed"] is False


@pytest.mark.asyncio
async def test_a_starting_design_that_fails_corners_is_not_optimized(tmp_path):
    # 코너를 못 버티는 설계에서 마진을 더 깎을 이유가 없다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, calls = _agents([235.0])
    agents.verify_corners = lambda texts: _sweep(False, 320.0)

    result = await run_optimization({"tb": DECK}, _corner_spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert result["steps_accepted"] == 0


@pytest.mark.asyncio
async def test_the_allowance_comes_from_the_measured_corner_spread(tmp_path):
    # nominal 235, 최악 코너 268 -> 여유분 33. 그러면 허용선은 267 이고,
    # 목적값이 내려가도 267 을 넘는 단계는 수락되면 안 된다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 270.0, 270.0, 270.0])
    agents.verify_corners = lambda texts: _sweep(True, 268.0)

    result = await run_optimization({"tb": DECK}, _corner_spec(), state, agents)

    # 270 은 iq<=300 을 통과하지만 267 이라는 실측 허용선을 넘는다.
    assert result["status"] == "UNCHANGED"


@pytest.mark.asyncio
async def test_a_confirmed_optimization_reports_the_sweep_it_passed(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 200.0, 200.0, 200.0])
    agents.verify_corners = lambda texts: _sweep(True, 240.0)

    result = await run_optimization({"tb": DECK}, _corner_spec(), state, agents)

    assert result["status"] == "OPTIMIZED"
    assert result["corner_confirmed"] is True
    assert result["pvt_sweep"]["overall_pass"] is True


@pytest.mark.asyncio
async def test_a_failed_confirmation_bisects_back_to_the_last_passing_version(tmp_path):
    # 진입은 통과, 확인은 실패. 이분 탐색이 통과하는 마지막 지점에 착지해야
    # 하고, 시작점보다 나빠지면 안 된다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 220.0, 210.0, 200.0, 200.0, 200.0])
    sweeps = {"n": 0}

    def verify(texts):
        sweeps["n"] += 1
        # 진입 통과, 이후 m=2 이하로 내려간 것만 실패한다고 본다.
        failing = "m=2" in texts["tb"] or "m=1" in texts["tb"]
        return _sweep(not failing, 268.0)

    agents.verify_corners = verify

    result = await run_optimization({"tb": DECK}, _corner_spec(), state, agents)

    assert result["pvt_sweep"]["overall_pass"] is True
    assert "m=3" in state.current_netlist_texts()["tb"]
    assert sweeps["n"] <= 6  # 진입 + 확인 + log2 회 정도


@pytest.mark.asyncio
async def test_when_no_step_survives_corners_the_start_is_returned_unchanged(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 200.0, 200.0, 200.0])
    entry = {"n": 0}

    def verify(texts):
        entry["n"] += 1
        return _sweep(entry["n"] == 1, 268.0)  # 진입만 통과

    agents.verify_corners = verify

    result = await run_optimization({"tb": DECK}, _corner_spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert "m=4" in state.current_netlist_texts()["tb"]
```

`OptimizerAgents`가 frozen dataclass면 `agents.verify_corners = ...` 대입이
안 되므로, 헬퍼가 `verify_corners`를 인자로 받게 고치거나 dataclass를 가변으로
둔다. 어느 쪽이든 하나를 고르고 일관되게 쓸 것.

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_optimizer_corners.py -q`
Expected: FAIL — `KeyError: 'corner_confirmed'` 등

- [ ] **Step 3: 구현한다**

Task 5의 루프를 내부 함수로 밀어 넣고, `run_optimization`이 위 1–6단계를
수행하게 만든다. 이분 탐색은 `state.netlist_versions[canonical]`의 인덱스
구간에 대해 표준 이분법으로 쓴다 — 앵커 인덱스는 통과가 보장되고 끝 인덱스는
실패가 확인된 상태이므로 불변식이 성립한다.

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_optimizer_corners.py tests/unit/test_optimizer.py -q`
Expected: PASS — Task 5의 테스트도 전부 그대로 통과해야 한다.

- [ ] **Step 5: 커밋**

```bash
git add src/analogcoder/optimizer.py tests/unit/test_optimizer_corners.py
git commit -m "feat: anchor optimization on a corner sweep and bisect back on failure"
```

---

### Task 7: CLI 배선

**Files:**
- Modify: `src/analogcoder/cli.py`
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `optimizer.run_optimization`, `optimizer.OptimizerAgents`,
  `agents.optimizer.propose_candidates`
- Produces: `AGENT_NAMES`에 `"optimizer"` 추가; `_run`의 결과 dict에
  `result["optimization"]`

**순서가 중요하다:** `run_orchestration`이 PASS를 낸 뒤, **최종 PVT 스윕 전에**
최적화를 돌린다. 그래야 기존의 최종 스윕이 최적화된 넷리스트를 확정하는
역할을 그대로 한다 — 스펙이 말한 "확정: 전 코너 스윕"이 이미 거기 있다.

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/unit/test_cli.py`에 추가:

```python
def test_optimizer_is_an_agent_whose_model_can_be_overridden():
    from analogcoder.cli import AGENT_NAMES

    assert "optimizer" in AGENT_NAMES
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_cli.py -q`
Expected: FAIL — `assert 'optimizer' in ('simulator', 'judge', 'tuner', 'verifier')`

- [ ] **Step 3: 구현한다**

`src/analogcoder/cli.py`:

```python
AGENT_NAMES = ("simulator", "judge", "tuner", "verifier", "optimizer")
```

`_run` 안, `result = await run_orchestration(...)` 바로 뒤에:

```python
    # 최적화는 PASS 뒤에만 의미가 있고, 최종 PVT 스윕 앞에 와야 한다 -
    # 그 스윕이 최적화된 넷리스트를 확정하는 역할을 그대로 하기 때문이다.
    if result["status"] == "PASS":
        async def propose_fn(structure_view, margins, objective, netlist_view):
            return await propose_candidates(
                structure_view, margins, objective, netlist_view, agent_backends["optimizer"]
            )

        def verify_corners_fn(netlist_texts):
            return run_full_pvt_sweep(netlist_texts, spec, sim_backend)

        optimization = await run_optimization(
            state.current_netlist_texts(),
            spec,
            state,
            OptimizerAgents(
                propose=propose_fn,
                simulate=simulate_fn,
                verify_corners=verify_corners_fn if spec.pvt_corners is not None else None,
            ),
        )
        result["optimization"] = optimization
        result["final_netlist_paths"] = state.current_netlist_paths()
```

**최종 스윕은 한 번만 돈다.** 최적화가 코너를 확인했으면 그 결과를 그대로
쓰고 다시 돌지 않는다 — bandgap 기준 286초짜리를 중복으로 태우지 않기 위해서다.
기존 `if spec.pvt_corners is not None:` 최종 스윕 블록을 이렇게 고친다:

```python
    if spec.pvt_corners is not None:
        confirmed = (result.get("optimization") or {}).get("pvt_sweep")
        if confirmed is not None:
            final_sweep = confirmed
        else:
            final_sweep = run_full_pvt_sweep(state.current_netlist_texts(), spec, sim_backend)
            state.log_event("pvt_final_sweep", final_sweep)
        result["pvt_sweep"] = final_sweep
        if not final_sweep["overall_pass"]:
            result["status"] = "FAIL"
            result["failure_reason"] = f"final PVT sweep failed: {final_sweep['summary']}"
```

최적화가 착지시킨 지점은 정의상 스윕을 통과한 것이므로, 이 경로에서
`status`가 `FAIL`로 바뀌는 일은 없다 — 최적화를 돌리지 않았거나 코너가
선언되지 않은 경우에만 기존 동작이 그대로 유지된다.

import를 파일 상단에 더한다:

```python
from analogcoder.agents.optimizer import propose_candidates
from analogcoder.optimizer import OptimizerAgents, run_optimization
```

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_cli.py -q`
Expected: PASS

- [ ] **Step 5: 전체 테스트를 돌린다**

Run: `.venv/bin/python -m pytest -q`
Expected: 회귀 없음.

- [ ] **Step 6: 커밋**

```bash
git add src/analogcoder/cli.py tests/unit/test_cli.py
git commit -m "feat: run optimization after PASS and before the final PVT sweep"
```

---

### Task 8: bandgap 종단 검증과 문서

**Files:**
- Create: `tests/unit/test_optimizer_bandgap_ngspice.py`
- Modify: `benchmarks/bandgap/spec.yaml`, `CLAUDE.md`
- Test: 위 파일

**Interfaces:**
- Consumes: Task 1–7 전부, `simulators.ngspice.NgspiceBackend`

`benchmarks/bandgap/spec.yaml`의 `quiescent_current`는 `iq_ua <= 300`인데
실측이 193–235 µA다. 그 스펙에는 이 작업을 위해 여유를 남겨 뒀다는 주석이
이미 달려 있다.

- [ ] **Step 1: `spec.yaml`에 `optimize` 블록을 더한다**

`benchmarks/bandgap/spec.yaml`의 `circuit_name` 다음에:

```yaml
# 서브프로젝트 C. quiescent_current 가 300uA 임계값에 대해 193-235uA 로
# 측정되므로 실제로 줄일 여유가 있다. guard_band 0.2 는 nominal 에서 확보한
# 마진이 45 코너를 버티게 하려는 것이고, 최종 확정은 전 코너 스윕이 한다.
optimize:
  objective: iq_ua
  area_budget: 1.10
  guard_band: 0.2
```

- [ ] **Step 2: 실패하는 테스트를 작성한다**

`tests/unit/test_optimizer_bandgap_ngspice.py`. 이 저장소의 `*_ngspice.py`
테스트는 skip-gate를 두지 않는다 — ngspice가 PATH에 있는 것을 전제한다
(`CLAUDE.md`의 Setup 절). `tests/unit/test_topology_swap_ngspice.py`가 경로
상수와 `NgspiceBackend` 직접 사용의 본보기다. 같은 방식을 따르고 새 skip
장치를 만들지 말 것.

```python
def test_the_optimizer_lowers_iq_while_every_criterion_still_passes(tmp_path):
    """실제 ngspice로 bandgap을 최적화한다. 목적값이 내려가고 22개 기준이
    전부 통과해야 한다. 가짜 에이전트로 후보만 고정하고, 시뮬레이션과 수락
    판정은 실제 코드가 한다."""
    spec = load_spec("benchmarks/bandgap/spec.yaml")
    texts = {tb.name: resolve_includes(open(tb.netlist_path).read(),
                                       os.path.dirname(tb.netlist_path))
             for tb in spec.testbenches}
    state = RunState(run_dir=str(tmp_path), testbench_names=[tb.name for tb in spec.testbenches])
    state.push_netlist_version(texts)

    sim_backend = NgspiceBackend()

    async def simulate(netlist_texts, spec_arg):
        merged = {}
        paths = state.current_netlist_paths()
        for tb in spec_arg.testbenches:
            merged.update(run_control_block(paths[tb.name], tb.control_block, sim_backend))
        return {"measurements": merged, "status": "success", "warnings": []}

    async def propose(structure_view, margins, objective, netlist_view):
        # 실제 노브. Xt 는 각 증폭기의 테일 전류원이고 (`Xt tail nbias vss vss
        # ... nfet_01v8 L=1 W=8`), 그 W 를 줄이면 그 단의 바이어스 전류가
        # 내려간다. TRIMAMP 의 것은 W=8 이라 줄일 여지가 있다.
        return {"candidates": [
            {"refdes": "TRIMAMP.Xt", "param": "W", "direction": "decrease",
             "reasoning": "tail current source of the trim amplifier"},
        ], "overall_reasoning": "cut a tail current first"}

    result = asyncio.run(run_optimization(texts, spec, state,
                                          OptimizerAgents(propose=propose, simulate=simulate)))

    assert result["status"] in {"OPTIMIZED", "UNCHANGED"}
    if result["status"] == "OPTIMIZED":
        assert result["objective_after"] < result["objective_before"]
        assert result["area_after"] <= result["area_before"] * spec.optimize.area_budget
```

`run_control_block`이라는 헬퍼가 없으면, 기존 ngspice 테스트가 시뮬레이터를
직접 부르는 방식을 그대로 따를 것 — 새 추상화를 만들지 말 것.

`TRIMAMP.Xt`는 `benchmarks/bandgap/netlist.cir:56`에 실재한다. 그래도 첫
단계에서 `check_refdes_resolution`과 `check_param_applicability`가 통과하는지
직접 확인할 것 — 존재하지 않거나 적용 불가능한 refdes를 쓰면 게이트가 막아
테스트가 아무것도 검증하지 못한 채 초록으로 통과한다.

- [ ] **Step 3: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_optimizer_bandgap_ngspice.py -q`
Expected: FAIL (모듈/헬퍼 부재 또는 단언 실패)

- [ ] **Step 4: 통과할 때까지 다듬는다**

Run: `.venv/bin/python -m pytest tests/unit/test_optimizer_bandgap_ngspice.py -q`
Expected: PASS

`UNCHANGED`로 끝나면 그것도 정상 결과이지만, **왜** 개선하지 못했는지 보고서에
적을 것 — 가드밴드가 너무 빡빡한지, 후보가 목적값을 안 움직이는지, 에어리어
예산에 걸리는지. 그 진단이 이 테스트의 진짜 산출물이다.

- [ ] **Step 5: `CLAUDE.md`를 갱신한다**

"Architecture" 절에 추가한다(기존 항목들과 같은 밀도로, 영어로):

```markdown
- `optimizer.py` / `agents/optimizer.py` / `area.py` — a second phase that runs
  after the loop returns PASS and before the final PVT sweep, spending the
  spec's remaining margin on the objective declared in `spec.yaml`'s
  `optimize:` block. **Its accept rule is deterministic and deliberately does
  NOT reuse `verify_post`**: that contract is "roll back if regressed", and a
  good optimization step consumes margin on purpose, so reusing it would roll
  back every successful shrink. A step is kept only if every criterion still
  passes with its guarded margin, the objective fell, and total area is inside
  the budget. Optimization has no FAIL outcome - failing to improve returns the
  design that already passed.
- **The guard band is `T ± g·|T|`, never `T·(1±g)`.** The latter inverts on a
  negative threshold: `psr_plus_db <= -10` with `g=0.2` would become `<= -8`,
  which is *looser* than the original. Each criterion is judged against its own
  threshold, so a two-sided window on one measurement keeps both sides -
  `pvt.py` lost one side of exactly that shape twice.
- **The cheapest way to cut current is to lower the supply, and that must stay
  blocked.** `check_stimulus_untouched` is a prerequisite of the optimization
  loop, not a reuse: the objective makes the degenerate solution more directly
  reachable than it ever was for tuning. All four gates run on the optimization
  path, on the full deck.
- **Area is derived, the objective is measured.** `area.total_area` sums
  `w × l × m` over resolvable devices (no `nf` - finger splitting is area
  neutral), so an over-budget candidate is discarded before it spends a
  simulation. That asymmetry is why the loop is simulation-bound and why the
  agent ranks few candidates rather than sweeping.
```

- [ ] **Step 6: 전체 테스트를 돌린다**

Run: `.venv/bin/python -m pytest -q`
Expected: 전부 통과.

- [ ] **Step 7: 커밋**

```bash
git add tests/unit/test_optimizer_bandgap_ngspice.py benchmarks/bandgap/spec.yaml CLAUDE.md
git commit -m "test+docs: optimize bandgap against real ngspice"
```

---

## 완료 기준

- [ ] `.venv/bin/python -m pytest -q` 전부 통과
- [ ] `optimize` 블록이 없는 스펙은 `SKIPPED`이고 그 사실이 기록됨
- [ ] 공급 전압을 낮추는 제안이 시뮬레이션 전에 막힘
- [ ] 가드밴드가 음수 임계값에서 엄격해지는 방향으로 동작함
- [ ] bandgap 종단 테스트가 통과하고, `UNCHANGED`면 그 이유가 기록됨
- [ ] 최적화가 시작보다 나쁜 결과를 내지 않음 — 확인 실패 시 이분 탐색이
      통과하는 마지막 지점(최악의 경우 시작점)에 착지함
- [ ] 코너가 선언되지 않은 스펙에서 `corner_confirmed`가 False로 보고됨
- [ ] `history.jsonl`에 `optimize_step`이 단계마다 남음

## 이 계획이 다루지 않는 것

- **전역 최적화.** 탐욕적 하강은 국소 최적에 빠질 수 있다. 다목적 베이즈
  최적화는 스펙의 "알려진 한계"에 기록돼 있다.
- **코너 축소.** 탐색 중 nominal만 쓰는 것을 개선하는 일은 서브프로젝트 B다.
- **면적을 목적으로 하는 모드.** `objective`는 하나이고 전류다.
- **가드밴드를 올린 재탐색.** 스펙의 초판에 있었으나 이분 탐색으로 대체됐다.
  재탐색은 더 큰 추측으로 하는 재시도이고 비용 상한이 없다.
