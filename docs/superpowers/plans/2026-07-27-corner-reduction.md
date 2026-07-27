# 코너 축소 + PVT 인지 자동 재튜닝 구현 계획 (하위 프로젝트 B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 튜닝 반복이 nominal 한 점 대신 측정으로 고른 소수의 코너를 보게 하고,
판정 스윕이 실패하면 그 코너를 배워 다시 튜닝한다.

**Architecture:** `run_orchestration`은 코너를 모르고 `agents.simulate`가 주는
측정값만 본다. 그래서 코너 선택은 **`simulate`가 무엇을 의미하는지**의 문제로
다루고 오케스트레이터는 수정하지 않는다(V1과 같은 판단). 새 모듈 둘 —
`corner_selection.py`가 코너 집합과 그 불변식을, `corner_sim.py`가 기존
`simulate_fn` 계약과 호환되는 corner-aware 시뮬레이터를 소유한다. `cli.py`는
재진입 루프만 얻는다.

**Tech Stack:** Python 3.11+, pytest, ngspice, 기존 `pvt.py` / `judge_tools.py` /
`optimizer.py`.

**설계 문서:** `docs/superpowers/specs/2026-07-27-corner-reduction-design.md`
(커밋 `26d3d64`). 읽지 않아도 각 태스크는 자족적이지만, 근거가 필요하면 거기 있다.

## Global Constraints

- **첫 검증과 최종 판정은 언제나 전 코너를 돈다.** 축소는 중간 반복에만. 이
  계획의 어떤 태스크도 `run_full_pvt_sweep`이 도는 코너 수를 줄이지 않는다.
- **축소 집합은 항상 낙관적이다.** 부분집합의 최악값은 전체의 최악값보다 나쁠 수
  없다. 중간 루프의 FAIL은 언제나 진짜고, 중간 루프의 PASS는 틀릴 수 있다.
- **nominal은 이름이 아니라 덱 자체다.** `tt`/`27`을 이름으로 집는 것은 금지된
  추측. nominal = 코너 렌더링을 거치지 않은 덱. 코드에서는 `None` 센티널로
  표현한다.
- **`CornerPoint`에 NaN을 넣어 nominal을 표현하지 말 것.** frozen dataclass의
  `__eq__`는 필드를 비교하고 `NaN != NaN`이므로 그런 값은 **자기 자신과도 같지
  않다** — 집합 멤버십·중복 제거·딕셔너리 키가 전부 조용히 깨진다.
- **탐침은 판정에 참여하지 않는다.** judge에 넘기는 최악값은 선택 집합에서만
  뽑는다. 탐침이 하는 일은 승격뿐이다.
- **어느 선택 코너에서든 값이 없으면 그 측정값을 withhold한다.** `pvt.py`의
  `worst_case_measurements`가 이미 그렇게 되어 있으므로 그대로 호출한다. V1의
  전체 리뷰가 잡았던 버그 클래스라 재발명하지 않는다.
- **새 코너를 하나도 더하지 못하는 판정 실패는 재시도하지 않는다.** 그것은 경로
  불일치이며 진단으로 보고한다.
- 테스트는 `.venv/bin/python -m pytest`로 돌린다. TDD — 실패하는 테스트 먼저.
- **각 테스트마다 "어떤 변형을 잡는가"를 커밋 메시지나 주석에 남긴다.** 이
  저장소는 통과하면서 아무것도 검증하지 않는 테스트를 네 번 출하했다.
- **이 문서의 예시 코드는 구속력이 없다. 테스트가 요구사항이고 스케치는
  제안이다.** 스케치가 실제 코드베이스와 어긋나면 코드베이스가 옳다.

## 파일 구조

| 파일 | 책임 |
|---|---|
| `src/analogcoder/spec.py` (수정) | `CornerReduction` 선언 로딩 |
| `src/analogcoder/pvt.py` (수정) | 코너별 측정값 노출, `corner_severity` |
| `src/analogcoder/corner_selection.py` (신규) | `CornerSet`과 그 불변식 — 시뮬레이션 없음, 순수 데이터 |
| `src/analogcoder/schemas.py` (수정) | `SIMULATION_SCHEMA`에 `control_block` |
| `src/analogcoder/agents/simulator_agent.py` (수정) | control block을 돌려주도록 프롬프트 |
| `src/analogcoder/corner_sim.py` (신규) | corner-aware `simulate_fn` 빌더 |
| `src/analogcoder/judge_tools.py` (수정) | 여유분 기준점 인자 이름 |
| `src/analogcoder/optimizer.py` (수정) | 축소 집합 최악값을 여유분 기준점으로 |
| `src/analogcoder/cli.py` (수정) | 재진입 루프, argmax drift 로깅 |
| `benchmarks/bandgap/spec_corner_reduction.yaml` (신규) | 코너 3개짜리 종단 테스트용 스펙 |

---

### Task 1: 스펙 표면 (`corner_reduction:` 블록)

**Files:**
- Modify: `src/analogcoder/spec.py`
- Test: `tests/unit/test_spec.py`

**Interfaces:**
- Consumes: 없음
- Produces: `CornerReduction(enabled: bool, retry_budget: int, probe: bool)`,
  `TargetSpec.corner_reduction: CornerReduction | None`

기존 `_load_optimize` / `OptimizeSpec`와 정확히 같은 모양으로 만든다. 그 코드를
먼저 읽고 그 패턴을 따른다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/unit/test_spec.py`에 추가:

```python
def test_a_spec_can_declare_corner_reduction(tmp_path):
    path = tmp_path / "spec.yaml"
    path.write_text("""
circuit_name: t
corner_reduction:
  enabled: true
  retry_budget: 3
  probe: false
testbenches:
  - name: tb
    netlist: n.cir
    analyses: [ac]
    control_block: ".ac dec 10 1 1G"
    criteria:
      - name: gain
        measurement: g
        operator: ">="
        threshold: 40
""")
    spec = load_spec(str(path))
    assert spec.corner_reduction.enabled is True
    assert spec.corner_reduction.retry_budget == 3
    assert spec.corner_reduction.probe is False


def test_corner_reduction_defaults_are_on_with_a_budget_of_two(tmp_path):
    # 블록만 있고 필드가 없으면 기본값. 기본을 끄는 쪽으로 두면 스펙에 블록을
    # 적어 두고도 아무 일이 안 일어난다 - 이 저장소가 반복해서 당한 모양이다.
    path = tmp_path / "spec.yaml"
    path.write_text("""
circuit_name: t
corner_reduction: {}
testbenches:
  - name: tb
    netlist: n.cir
    analyses: [ac]
    control_block: ".ac dec 10 1 1G"
    criteria:
      - name: gain
        measurement: g
        operator: ">="
        threshold: 40
""")
    spec = load_spec(str(path))
    assert spec.corner_reduction.enabled is True
    assert spec.corner_reduction.retry_budget == 2
    assert spec.corner_reduction.probe is True


def test_a_spec_without_the_block_has_no_corner_reduction(tmp_path):
    path = tmp_path / "spec.yaml"
    path.write_text("""
circuit_name: t
testbenches:
  - name: tb
    netlist: n.cir
    analyses: [ac]
    control_block: ".ac dec 10 1 1G"
    criteria:
      - name: gain
        measurement: g
        operator: ">="
        threshold: 40
""")
    assert load_spec(str(path)).corner_reduction is None
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_spec.py -k corner_reduction -v`
Expected: FAIL — `AttributeError: 'TargetSpec' object has no attribute 'corner_reduction'`

- [ ] **Step 3: 구현한다**

`spec.py`에 (`OptimizeSpec` 바로 아래, `_load_optimize` 바로 아래 패턴을 따라):

```python
@dataclass(frozen=True)
class CornerReduction:
    """중간 반복의 코너 축소 설정.

    enabled=False면 오늘 동작(nominal 한 점)이 그대로다. pvt_corners가 선언되지
    않은 스펙에서는 축소할 것이 없으므로 이 블록이 있어도 아무 일도 하지
    않으며, 그 사실은 cli가 로그로 남긴다."""

    enabled: bool = True
    retry_budget: int = 2
    probe: bool = True


def _load_corner_reduction(raw: dict) -> CornerReduction | None:
    block = raw.get("corner_reduction")
    if block is None:
        return None
    return CornerReduction(
        enabled=bool(block.get("enabled", True)),
        retry_budget=int(block.get("retry_budget", 2)),
        probe=bool(block.get("probe", True)),
    )
```

`TargetSpec`에 `corner_reduction: CornerReduction | None = None`을 더하고
`load_spec`에서 `corner_reduction=_load_corner_reduction(raw)`로 채운다.

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_spec.py -v`
Expected: PASS (기존 테스트 전부 포함)

- [ ] **Step 5: 커밋**

```bash
git add src/analogcoder/spec.py tests/unit/test_spec.py
git commit -m "feat: spec에 corner_reduction 블록"
```

---

### Task 2: 코너별 측정값 노출 + `corner_severity`

**Files:**
- Modify: `src/analogcoder/pvt.py`
- Test: `tests/unit/test_pvt.py`

**Interfaces:**
- Consumes: `CornerPoint`, `worst_case_measurements` (기존)
- Produces:
  - `run_full_pvt_sweep(...)`의 반환 dict에 `"per_corner": list[dict]` 추가.
    각 원소는 `{"corner": {"process": str, "voltage": float, "temperature": float},
    "measurements": dict[str, float], "severity": float}`이며 측정값은 **모든
    테스트벤치의 것이 그 코너에 대해 합쳐진 것**이다. 순서는
    `all_corners(spec.pvt_corners)`와 평행.
  - `corner_severity(measurements: dict, criteria: list[Criterion]) -> float`

`per_corner`를 더하는 이유: 탐침 순서가 코너별 심각도를 요구하는데 지금은 최악값만
남기고 코너별 측정값을 버린다. 새 시뮬레이션은 필요 없다 — 이미 루프 안에서
계산하고 있다.

- [ ] **Step 1: `corner_severity`의 실패하는 테스트를 쓴다**

`tests/unit/test_pvt.py`에 추가:

```python
from analogcoder.pvt import corner_severity
from analogcoder.spec import Criterion
import math

GE = Criterion(name="gain", measurement="g", operator=">=", threshold=40.0)
LE = Criterion(name="iq", measurement="i", operator="<=", threshold=300.0)


def test_severity_is_the_tightest_normalised_margin():
    # gain 44 vs >=40  -> +0.10
    # iq   270 vs <=300 -> +0.10
    # 둘이 같으면 어느 쪽을 골라도 같은 값이므로, 한쪽을 더 아슬하게 만들어
    # "min을 취한다"가 실제로 검증되게 한다.
    assert corner_severity({"g": 44.0, "i": 288.0}, [GE, LE]) == pytest.approx(0.04)


def test_a_failing_criterion_makes_severity_negative():
    # 부호 규칙이 뒤집히면(<= 를 음수화하지 않으면) iq 330은 +0.1로 읽혀
    # 통과처럼 보인다. 이 단언이 그 변형을 잡는다.
    assert corner_severity({"g": 44.0, "i": 330.0}, [GE, LE]) < 0


def test_a_missing_measurement_is_the_most_severe_possible():
    # 값이 없다는 것은 그 코너에서 회로가 동작하지 않는다는 가장 강한 증거다.
    # 빠진 기준을 건너뛰는 변형은 이 코너를 "여유 있음"으로 읽는다.
    assert corner_severity({"g": 44.0}, [GE, LE]) == -math.inf


def test_a_zero_threshold_falls_back_to_an_absolute_margin():
    # |T| == 0 이면 정규화가 0으로 나눈다. 절대 여유로 후퇴한다.
    zero = Criterion(name="off", measurement="o", operator=">=", threshold=0.0)
    assert corner_severity({"o": 0.5}, [zero]) == pytest.approx(0.5)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_pvt.py -k severity -v`
Expected: FAIL — `ImportError: cannot import name 'corner_severity'`

- [ ] **Step 3: `corner_severity`를 구현한다**

`pvt.py`에:

```python
def corner_severity(measurements: dict, criteria: list[Criterion]) -> float:
    """이 코너에서 가장 아슬한 기준의 정규화 여유. 작을수록 나쁘다.

    코너별 최악은 기준마다 다른데 탐침 순서는 코너별이어야 하므로, 코너 하나를
    수 하나로 요약해야 한다. 정규화는 임계값 크기로 하고(기준마다 단위가 다르다),
    통과 방향이 양수가 되도록 부호를 맞춘다.

    값이 없는 기준이 하나라도 있으면 -inf다. 그 기준을 건너뛰면 회로가 동작조차
    하지 않는 코너가 '여유 있음'으로 읽힌다 - worst_case_measurements가 어느
    코너에서든 빠진 측정값을 withhold하는 것과 같은 논리다."""
    worst = math.inf
    for criterion in criteria:
        value = measurements.get(criterion.measurement)
        if value is None or math.isnan(value):
            return -math.inf
        denominator = abs(criterion.threshold) or 1.0
        margin = (value - criterion.threshold) / denominator
        if criterion.operator in ("<=", "<"):
            margin = -margin
        worst = min(worst, margin)
    return worst
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_pvt.py -k severity -v`
Expected: PASS

- [ ] **Step 5: `per_corner` 노출의 실패하는 테스트를 쓴다**

`tests/unit/test_pvt.py`에 추가. 기존 `run_full_pvt_sweep` 테스트가 쓰는 가짜
백엔드 패턴을 그대로 따른다(파일 상단을 먼저 읽을 것):

```python
def test_the_sweep_exposes_every_corner_s_own_measurements(tmp_path):
    # 탐침 순서가 코너별 심각도를 요구한다. 최악값만 남기면 그 계산을 할
    # 데이터가 없다. per_corner를 통째로 지우는 변형은 이 테스트가 잡고,
    # 순서를 뒤집는 변형은 마지막 단언이 잡는다.
    spec = _spec_with_corners(tmp_path, process=["tt", "ss"], voltage=[1.8], temperature=[27])
    backend = _sequenced_backend([{"g": 50.0}, {"g": 41.0}])

    sweep = run_full_pvt_sweep({"tb": DECK}, spec, backend)

    assert [e["measurements"]["g"] for e in sweep["per_corner"]] == [50.0, 41.0]
    assert sweep["per_corner"][0]["corner"]["process"] == "tt"
    assert sweep["per_corner"][1]["corner"]["process"] == "ss"


def test_each_corner_entry_carries_its_own_severity(tmp_path):
    # 탐침 순서가 이 값으로 정렬된다. severity를 빼거나 상수로 두는 변형은
    # 탐침이 임의 순서로 돌게 만들고, 그러면 낡음을 늦게 발견한다.
    spec = _spec_with_corners(tmp_path, process=["tt", "ss"], voltage=[1.8], temperature=[27])
    backend = _sequenced_backend([{"g": 50.0}, {"g": 41.0}])   # 기준은 g >= 40

    sweep = run_full_pvt_sweep({"tb": DECK}, spec, backend)

    assert sweep["per_corner"][0]["severity"] == pytest.approx(0.25)   # (50-40)/40
    assert sweep["per_corner"][1]["severity"] == pytest.approx(0.025)  # (41-40)/40
```

- [ ] **Step 6: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_pvt.py -k per_corner -v`
Expected: FAIL — `KeyError: 'per_corner'`

- [ ] **Step 7: `per_corner`를 구현한다**

`run_full_pvt_sweep`의 테스트벤치 루프에서 코너별 측정값을 코너 인덱스로 모은다.
지금 루프는 테스트벤치 바깥, 코너 안쪽이므로 코너 인덱스별 dict를 하나 두고
테스트벤치마다 `update`한다:

```python
    corners = all_corners(spec.pvt_corners)
    per_corner_merged: list[dict] = [{} for _ in corners]
    ...
    for tb in spec.testbenches:
        ...
        for index, corner in enumerate(corners):
            ...
            per_corner_measurements.append(result.measurements)
            per_corner_merged[index].update(result.measurements)
```

그리고 반환 dict에:

```python
        "per_corner": [
            {
                "corner": {"process": c.process, "voltage": c.voltage, "temperature": c.temperature},
                "measurements": m,
                "severity": corner_severity(m, spec.all_criteria),
            }
            for c, m in zip(corners, per_corner_merged)
        ],
```

- [ ] **Step 8: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_pvt.py -v`
Expected: PASS (기존 테스트 전부 포함 — `per_corner`는 추가 키이므로 기존 소비자에
영향이 없어야 한다)

- [ ] **Step 9: 커밋**

```bash
git add src/analogcoder/pvt.py tests/unit/test_pvt.py
git commit -m "feat: 코너별 측정값 노출과 corner_severity"
```

---

### Task 3: `CornerSet` — 씨앗, 성장, 탐침 회전

**Files:**
- Create: `src/analogcoder/corner_selection.py`
- Test: `tests/unit/test_corner_selection.py`

**Interfaces:**
- Consumes: `pvt.CornerPoint`, `pvt.all_corners`, `pvt.corner_severity`,
  `run_full_pvt_sweep`의 반환 dict(`worst_case_corners`, `per_corner`)
- Produces:

```python
NOMINAL = None                      # 코너 렌더링을 거치지 않은 덱 그대로

@dataclass(frozen=True)
class CornerSet:
    corners: tuple[CornerPoint | None, ...]       # NOMINAL이 항상 [0]
    probe_order: tuple[CornerPoint, ...]          # 집합 밖, severity 오름차순
    probe_index: int = 0

def seed_from_sweep(sweep: dict, spec) -> CornerSet
def grown_with(cs: CornerSet, sweep: dict, failing_names: list[str]) -> tuple[CornerSet, list[CornerPoint]]
def next_probe(cs: CornerSet) -> tuple[CornerPoint | None, CornerSet]
def promote(cs: CornerSet, corner: CornerPoint) -> CornerSet
def label(point: CornerPoint | None) -> str        # NOMINAL -> "(deck)"
```

**nominal은 `None`이다.** `CornerPoint(process="(deck)", voltage=nan, ...)`로
표현하지 말 것 — frozen dataclass의 `__eq__`가 필드를 비교하는데 `NaN != NaN`
이므로 그 값은 자기 자신과도 같지 않고, 집합 멤버십과 중복 제거가 조용히 깨진다.

이 모듈은 시뮬레이션을 하지 않는다. 순수 데이터 변환이라 전부 빠른 단위
테스트로 고정된다.

- [ ] **Step 1: 씨앗의 실패하는 테스트를 쓴다**

`tests/unit/test_corner_selection.py` (신규):

```python
import pytest
from analogcoder.corner_selection import (
    NOMINAL, CornerSet, seed_from_sweep, grown_with, next_probe, promote, label,
)
from analogcoder.pvt import CornerPoint

FS = CornerPoint(process="fs", voltage=1.98, temperature=125.0)
SF = CornerPoint(process="sf", voltage=1.62, temperature=-40.0)
SS = CornerPoint(process="ss", voltage=1.62, temperature=125.0)


def _sweep(worst_corners, per_corner=()):
    return {"worst_case_corners": worst_corners, "per_corner": list(per_corner)}


def _wc(corner, value):
    return {"process": corner.process, "voltage": corner.voltage,
            "temperature": corner.temperature, "value": value}


def test_the_seed_is_the_union_of_every_criterion_s_worst_corner(_spec):
    cs = seed_from_sweep(_sweep({"gain": _wc(FS, 41.0), "psr": _wc(SF, -9.0)}), _spec)
    assert set(cs.corners) == {NOMINAL, FS, SF}


def test_two_criteria_sharing_a_worst_corner_do_not_duplicate_it(_spec):
    cs = seed_from_sweep(_sweep({"gain": _wc(FS, 41.0), "pm": _wc(FS, 55.0)}), _spec)
    assert list(cs.corners).count(FS) == 1


def test_nominal_is_always_first_even_when_no_criterion_names_it(_spec):
    # 임계값이 덱 그대로의 상태에서 정해졌다. 최악 코너 목록에 안 나온다고
    # 빼면 기존 동작의 기준점이 사라진다.
    cs = seed_from_sweep(_sweep({"gain": _wc(FS, 41.0)}), _spec)
    assert cs.corners[0] is NOMINAL


def test_a_corner_with_no_measurement_is_that_criterion_s_worst(_spec):
    # value=None은 그 코너에서 측정값이 아예 안 나왔다는 뜻이고,
    # worst_case_corners가 이미 그 코너를 지목하고 있다. 값이 없다고
    # 건너뛰는 변형은 회로가 동작하지 않는 코너를 집합에서 빠뜨린다.
    cs = seed_from_sweep(_sweep({"gain": _wc(SS, None)}), _spec)
    assert SS in cs.corners


def test_a_failing_entry_sweep_still_seeds(_spec):
    # 진입 스윕은 비-게이팅이고 그대로 둔다. 실패한 설계의 최악 코너도 최악
    # 코너이며, 오히려 그 코너들이야말로 중간 루프가 봐야 할 것이다.
    # overall_pass를 보고 씨앗을 건너뛰는 변형은, 코너에서 실패하는 설계로
    # 시작한 실행에서 축소를 통째로 꺼 버린다.
    failing = {"worst_case_corners": {"gain": _wc(FS, 12.0)},
               "per_corner": [], "overall_pass": False}
    cs = seed_from_sweep(failing, _spec)
    assert FS in cs.corners


def test_the_probe_order_is_most_severe_first(_spec):
    # 가장 아슬한 코너부터 훑어야 낡음을 빨리 잡는다. 정렬을 빼거나 뒤집는
    # 변형을 이 단언이 잡는다.
    sweep = {
        "worst_case_corners": {},
        "per_corner": [
            {"corner": {"process": "fs", "voltage": 1.98, "temperature": 125.0}, "severity": 0.5},
            {"corner": {"process": "sf", "voltage": 1.62, "temperature": -40.0}, "severity": 0.01},
        ],
    }
    assert seed_from_sweep(sweep, _spec).probe_order[0] == SF


def test_a_corner_already_in_the_set_is_not_also_a_probe(_spec):
    # 매 반복 도는 코너를 탐침으로 또 돌면 시뮬레이션 하나를 그냥 버린다.
    sweep = {
        "worst_case_corners": {"gain": _wc(FS, 41.0)},
        "per_corner": [
            {"corner": {"process": "fs", "voltage": 1.98, "temperature": 125.0}, "severity": 0.01},
            {"corner": {"process": "sf", "voltage": 1.62, "temperature": -40.0}, "severity": 0.5},
        ],
    }
    cs = seed_from_sweep(sweep, _spec)
    assert FS not in cs.probe_order and SF in cs.probe_order
```

`_spec` 픽스처는 `pvt_corners`를 가진 최소 스펙이면 된다. 기존
`tests/unit/test_pvt.py`가 스펙을 만드는 방식을 먼저 읽고 맞춘다.

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_corner_selection.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analogcoder.corner_selection'`

- [ ] **Step 3: 씨앗을 구현한다**

```python
NOMINAL = None


def label(point) -> str:
    if point is NOMINAL:
        return "(deck)"
    return f"{point.process}/{point.voltage}/{point.temperature}"


def _as_point(raw: dict) -> CornerPoint:
    return CornerPoint(
        process=raw["process"], voltage=raw["voltage"], temperature=raw["temperature"]
    )


def seed_from_sweep(sweep: dict, spec) -> CornerSet:
    """진입 스윕에서 기준별 최악 코너를 뽑아 합집합. 새 시뮬레이션은 없다.

    value가 None인 항목도 포함한다 - 그 코너에서 측정값이 아예 안 나왔다는
    뜻이고, 회로가 거기서 동작하지 않는다는 가장 강한 증거다."""
    chosen: list[CornerPoint] = []
    for raw in sweep.get("worst_case_corners", {}).values():
        point = _as_point(raw)
        if point not in chosen:
            chosen.append(point)
    corners = (NOMINAL, *chosen)
    return CornerSet(corners=corners, probe_order=_probe_order(sweep, corners))


def _probe_order(sweep: dict, corners) -> tuple[CornerPoint, ...]:
    """집합 밖 코너를 severity 오름차순(가장 아슬한 것부터)으로.

    severity는 Task 2가 per_corner 항목에 실어 준다. per_corner가 없으면 빈
    튜플 - 탐침 없이 도는 것이 조용히 잘못된 순서로 도는 것보다 낫다."""
    entries = []
    for entry in sweep.get("per_corner", []):
        point = _as_point(entry["corner"])
        if point in corners:
            continue
        entries.append((entry.get("severity"), point))
    entries.sort(key=lambda e: (e[0] is None, e[0]))
    return tuple(point for _, point in entries)
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_corner_selection.py -v`
Expected: PASS

- [ ] **Step 5: 성장·경로 불일치·탐침 회전의 실패하는 테스트를 쓴다**

아래 두 블록을 **모두** 먼저 쓴다. 구현은 Step 7이다.

```python
def test_growth_adds_the_failing_criteria_s_worst_corners(_spec):
    cs = CornerSet(corners=(NOMINAL, FS), probe_order=())
    failing = _sweep({"gain": _wc(SF, -1.0)})
    grown, added = grown_with(cs, failing, ["gain"])
    assert SF in grown.corners and added == [SF]


def test_growth_never_removes_a_corner(_spec):
    cs = CornerSet(corners=(NOMINAL, FS), probe_order=())
    grown, _ = grown_with(cs, _sweep({"gain": _wc(SF, -1.0)}), ["gain"])
    assert NOMINAL in grown.corners and FS in grown.corners


def test_a_failure_at_a_corner_already_in_the_set_adds_nothing(_spec):
    # 이것이 경로 불일치 신호다. 집합이 자라지 않으면 재진입은 같은 정보로
    # 같은 결과를 낼 뿐이므로, 호출부는 added가 비었을 때 재시도하지 않는다.
    # added를 항상 실패 코너 전체로 돌려주는 변형은 무한 재시도를 만든다.
    cs = CornerSet(corners=(NOMINAL, FS), probe_order=())
    grown, added = grown_with(cs, _sweep({"gain": _wc(FS, -1.0)}), ["gain"])
    assert added == []
    assert grown.corners == cs.corners


def test_growth_only_looks_at_the_named_failing_criteria(_spec):
    # 통과한 기준의 최악 코너까지 끌어오면 집합이 불필요하게 커진다.
    cs = CornerSet(corners=(NOMINAL,), probe_order=())
    sweep = _sweep({"gain": _wc(FS, -1.0), "psr": _wc(SF, -20.0)})
    _, added = grown_with(cs, sweep, ["gain"])
    assert added == [FS]
```

그리고 탐침 회전:

```python
def test_the_probe_walks_the_order_and_wraps():
    cs = CornerSet(corners=(NOMINAL,), probe_order=(FS, SF))
    first, cs = next_probe(cs)
    second, cs = next_probe(cs)
    third, _ = next_probe(cs)
    assert (first, second, third) == (FS, SF, FS)


def test_there_is_no_probe_when_the_set_covers_everything():
    assert next_probe(CornerSet(corners=(NOMINAL, FS), probe_order=()))[0] is None


def test_promotion_moves_a_corner_out_of_the_probe_order():
    # 승격된 코너가 탐침 순서에 남아 있으면 이미 매 반복 도는 코너를 또 돈다.
    cs = CornerSet(corners=(NOMINAL,), probe_order=(FS, SF))
    promoted = promote(cs, FS)
    assert FS in promoted.corners and FS not in promoted.probe_order


def test_growth_also_drops_the_added_corners_from_the_probe_order():
    # 성장으로 들어온 코너를 탐침이 계속 돌면 매 반복 시뮬레이션 하나가 낭비된다.
    cs = CornerSet(corners=(NOMINAL,), probe_order=(FS, SF))
    grown, _ = grown_with(cs, _sweep({"gain": _wc(FS, -1.0)}), ["gain"])
    assert grown.probe_order == (SF,)
```

- [ ] **Step 6: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_corner_selection.py -v`
Expected: FAIL — `ImportError` 또는 `NameError: name 'grown_with' is not defined`

- [ ] **Step 7: 성장과 탐침 회전을 구현한다**

```python
def grown_with(cs: CornerSet, sweep: dict, failing_names) -> tuple[CornerSet, list[CornerPoint]]:
    """실패한 기준들의 최악 코너를 집합에 더한다. 새로 더해진 것만 함께 돌려준다.

    빈 목록은 **경로 불일치**다: 판정 스윕이 실패한 코너가 전부 이미 중간 루프
    집합 안에 있다면, 같은 덱의 같은 코너를 두고 두 실행 경로가 서로 다른 말을
    하고 있는 것이다. 호출부는 그때 재시도하지 않고 그 사실을 보고한다."""
    worst = sweep.get("worst_case_corners", {})
    added: list[CornerPoint] = []
    for name in failing_names:
        raw = worst.get(name)
        if raw is None:
            continue
        point = _as_point(raw)
        if point not in cs.corners and point not in added:
            added.append(point)
    if not added:
        return cs, []
    corners = (*cs.corners, *added)
    remaining = tuple(p for p in cs.probe_order if p not in corners)
    return CornerSet(corners=corners, probe_order=remaining, probe_index=0), added


def next_probe(cs: CornerSet):
    """다음 탐침 코너와 회전이 진행된 CornerSet. 집합 밖이 비면 (None, cs)."""
    if not cs.probe_order:
        return None, cs
    index = cs.probe_index % len(cs.probe_order)
    return cs.probe_order[index], replace(cs, probe_index=index + 1)


def promote(cs: CornerSet, corner: CornerPoint) -> CornerSet:
    """탐침에서 실패한 코너를 선택 집합으로 올린다."""
    if corner in cs.corners:
        return cs
    return CornerSet(
        corners=(*cs.corners, corner),
        probe_order=tuple(p for p in cs.probe_order if p != corner),
        probe_index=0,
    )
```

- [ ] **Step 8: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_corner_selection.py tests/unit/test_pvt.py -v`
Expected: PASS

- [ ] **Step 9: 커밋**

```bash
git add src/analogcoder/corner_selection.py src/analogcoder/pvt.py tests/unit/test_corner_selection.py
git commit -m "feat: CornerSet - 씨앗, 단조 성장, 탐침 회전

새 코너를 하나도 못 더하는 성장은 경로 불일치 신호이며, 호출부가
그것으로 재진입 여부를 정한다."
```

---

### Task 4: 시뮬레이터 에이전트가 확정된 control block을 돌려준다

**Files:**
- Modify: `src/analogcoder/schemas.py`, `src/analogcoder/agents/simulator_agent.py`
- Test: `tests/unit/test_simulator_agent.py`

**Interfaces:**
- Produces: `agent_simulate(...)`의 반환 dict에 `"control_block": str`.
  에이전트가 **실제로 마지막에 사용한** control block이며, 수렴 실패로 `.options`를
  고쳤다면 고친 것이다.

이것이 이 계획이 요구하는 유일한 에이전트 계약 변경이다. 코너들은 이 control
block을 물려받아 직접 경로로 돌므로, 수렴 재시도의 이득이 코너까지 간다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/unit/test_simulator_agent.py`의 기존 mock 패턴을 먼저 읽고 맞춘다.

```python
@pytest.mark.asyncio
async def test_the_agent_returns_the_control_block_it_settled_on(tmp_path):
    # 코너들이 이것을 물려받는다. 돌려주지 않으면 코너는 수렴 재시도의 이득을
    # 못 받고, 스펙 원문을 그대로 쓰게 된다.
    backend = _backend_returning({
        "measurements": {"g": 42.0},
        "status": "success",
        "warnings": [],
        "control_block": ".options gmin=1e-10\n.ac dec 10 1 1G",
    })
    result = await simulate("n.cir", ".ac dec 10 1 1G", _sim_backend(), backend)
    assert result["control_block"] == ".options gmin=1e-10\n.ac dec 10 1 1G"


@pytest.mark.asyncio
async def test_the_schema_requires_the_control_block():
    assert "control_block" in SIMULATION_SCHEMA["required"]
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_simulator_agent.py -k control_block -v`
Expected: FAIL — `KeyError: 'control_block'` / `assert 'control_block' in [...]`

- [ ] **Step 3: 구현한다**

`schemas.py`:

```python
SIMULATION_SCHEMA = {
    "type": "object",
    "properties": {
        "measurements": {"type": "object", "additionalProperties": {"type": "number"}},
        "status": {"enum": ["success", "convergence_failure", "error"]},
        "warnings": {"type": "array", "items": {"type": "string"}},
        # 코너 시뮬레이션이 이것을 물려받아 직접 경로로 돈다. 수렴을 위해
        # .options를 고쳤다면 고친 것을 그대로 돌려줘야 코너도 같은 이득을
        # 받는다.
        "control_block": {"type": "string"},
    },
    "required": ["measurements", "status", "warnings", "control_block"],
}
```

`SIMULATION_SYSTEM_PROMPT`에 한 문장 추가:

```
Always report the control block you actually used in your final structured
output's control_block field - the original if you did not change it, or the
adjusted one if you retried. Other simulations reuse it verbatim.
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_simulator_agent.py tests/unit/test_cli.py -v`
Expected: PASS. `test_cli.py`의 시뮬레이터 더미가 새 required 키를 안 내면 여기서
드러난다 — 그때는 더미를 고친다(단언을 약화하지 말 것).

- [ ] **Step 5: 커밋**

```bash
git add src/analogcoder/schemas.py src/analogcoder/agents/simulator_agent.py tests/unit/
git commit -m "feat: 시뮬레이터 에이전트가 확정된 control block을 돌려준다"
```

---

### Task 5: `corner_sim.py` — corner-aware `simulate_fn`

**Files:**
- Create: `src/analogcoder/corner_sim.py`
- Test: `tests/unit/test_corner_sim.py`

**Interfaces:**
- Consumes: `corner_selection.{CornerSet, NOMINAL, next_probe, promote, label}`,
  `pvt.{render_corner_netlist, worst_case_measurements}`, Task 4의 `control_block`
- Produces:

```python
@dataclass
class CornerState:
    corner_set: CornerSet          # 탐침 승격과 성장으로 바뀐다

def build_corner_simulate(agent_simulate, sim_backend, state, corner_state, log_event) -> Callable
```

반환 함수는 `async (netlist_texts, spec) -> dict`이며 기존 `simulate_fn` 계약을
지킨다: `{"status", "measurements", "by_testbench"}`. 추가로 `"corner_worst"`와
`"probe"`를 싣는다(추가 키이므로 기존 소비자는 영향받지 않는다).

한 반복의 흐름:

```
테스트벤치마다:
  agent_simulate(경로, tb.control_block)     -> status + 확정된 control_block   [LLM]
  sim_backend.run(덱 그대로, 그 control_block)                                  [nominal, 직접]
  선택 집합의 코너마다: sim_backend.run(렌더링된 덱, 그 control_block)
  탐침이 있으면 한 번 더 (판정 불참)
worst_case_measurements(선택 집합, 코너별 측정값, spec.all_criteria) -> judge
```

**nominal을 직접 경로로 한 번 더 도는 이유**는 키 집합 때문이다. 에이전트가
내놓는 측정 키와 `sim_backend`가 내놓는 키가 다를 수 있다는 것은 이미 기록된
사실이고, 최악값을 두 경로에 걸쳐 뽑으면 그 차이가 판정에 들어간다. nominal도
직접 경로로 돌면 모든 코너가 같은 경로에서 나오므로 **키 집합이 구조적으로
동일**해진다.

- [ ] **Step 1: 최악값 병합의 실패하는 테스트를 쓴다**

`tests/unit/test_corner_sim.py` (신규):

```python
@pytest.mark.asyncio
async def test_the_judge_sees_the_worst_across_the_selected_corners(tmp_path):
    # nominal 50, 코너 41 -> judge는 41을 봐야 한다. nominal만 넘기는 변형,
    # 혹은 평균/최대를 취하는 변형을 이 단언이 잡는다.
    state = _state(tmp_path)
    cs = CornerSet(corners=(NOMINAL, FS), probe_order=())
    sim = build_corner_simulate(
        _agent(measurements={"g": 999.0}),          # 에이전트 값은 쓰이지 않는다
        _backend([{"g": 50.0}, {"g": 41.0}]),       # 직접 nominal, 코너
        state, CornerState(cs), _noop_log,
    )
    result = await sim({"tb": DECK}, _spec_ge_40())
    assert result["measurements"]["g"] == 41.0


@pytest.mark.asyncio
async def test_the_agent_s_own_measurements_are_not_used(tmp_path):
    # 에이전트 경로와 직접 경로의 키 집합이 다를 수 있다는 것이 이 설계가
    # nominal을 직접 경로로 한 번 더 도는 이유다. 에이전트 값을 섞는 변형은
    # 그 이유를 무효로 만든다.
    state = _state(tmp_path)
    sim = build_corner_simulate(
        _agent(measurements={"g": 1.0, "agent_only": 7.0}),
        _backend([{"g": 50.0}]),
        state, CornerState(CornerSet(corners=(NOMINAL,), probe_order=())), _noop_log,
    )
    result = await sim({"tb": DECK}, _spec_ge_40())
    assert result["measurements"] == {"g": 50.0}
    assert "agent_only" not in result["measurements"]


@pytest.mark.asyncio
async def test_a_measurement_missing_at_any_selected_corner_is_withheld(tmp_path):
    # V1의 규칙. 한 코너에서 값이 안 나오면 다른 코너가 그것을 가려서는 안 된다.
    state = _state(tmp_path)
    cs = CornerSet(corners=(NOMINAL, FS), probe_order=())
    sim = build_corner_simulate(
        _agent(), _backend([{"g": 50.0}, {}]), state, CornerState(cs), _noop_log,
    )
    result = await sim({"tb": DECK}, _spec_ge_40())
    assert "g" not in result["measurements"]


@pytest.mark.asyncio
async def test_the_corners_reuse_the_control_block_the_agent_settled_on(tmp_path):
    # 코너가 스펙 원문을 쓰면 수렴 재시도의 이득을 못 받는다.
    state = _state(tmp_path)
    cs = CornerSet(corners=(NOMINAL, FS), probe_order=())
    backend = _recording_backend([{"g": 50.0}, {"g": 41.0}])
    sim = build_corner_simulate(
        _agent(control_block=".options gmin=1e-10\n.ac dec 10 1 1G"),
        backend, state, CornerState(cs), _noop_log,
    )
    await sim({"tb": DECK}, _spec_ge_40())
    assert all(c["control_block"].startswith(".options gmin=1e-10") for c in backend.calls)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_corner_sim.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analogcoder.corner_sim'`

- [ ] **Step 3: 구현한다**

```python
@dataclass
class CornerState:
    corner_set: CornerSet


def build_corner_simulate(agent_simulate, sim_backend, state, corner_state, log_event):
    """기존 simulate_fn 계약을 지키면서, 판정을 선택 코너들의 최악값으로 바꾼다.

    에이전트는 nominal에서만 돌고 그 기여분은 정확히 두 가지다 - 수렴하는
    control block을 찾는 것과 status를 보고하는 것. 측정값은 전부 직접 경로에서
    나온다. 그래야 모든 코너의 키 집합이 같다."""

    async def simulate_fn(netlist_texts, spec):
        benchmark_dir = os.path.dirname(spec.canonical.netlist_path)
        cs = corner_state.corner_set
        probe_point, cs = next_probe(cs) if _probe_enabled(spec) else (None, cs)

        status = "success"
        by_testbench: dict = {}
        per_point: dict = {point: {} for point in cs.corners}
        probe_measurements: dict = {}

        paths = state.current_netlist_paths()
        for tb in spec.testbenches:
            agent_result = await agent_simulate(paths[tb.name], tb.control_block)
            by_testbench[tb.name] = agent_result
            if status == "success" and agent_result.get("status", "success") != "success":
                status = agent_result["status"]
            control_block = agent_result.get("control_block") or tb.control_block

            for point in cs.corners:
                raw = _run_point(sim_backend, netlist_texts[tb.name], point,
                                 control_block, benchmark_dir)
                per_point[point].update(raw.measurements)
                if status == "success" and raw.status != "success":
                    status = raw.status

            if probe_point is not None:
                raw = _run_point(sim_backend, netlist_texts[tb.name], probe_point,
                                 control_block, benchmark_dir)
                probe_measurements.update(raw.measurements)

        measurements, corner_worst = worst_case_measurements(
            list(cs.corners), [per_point[p] for p in cs.corners], spec.all_criteria
        )

        probe_record = None
        if probe_point is not None:
            probe_record = _judge_probe(probe_point, probe_measurements, spec)
            if probe_record["failed"]:
                cs = promote(cs, probe_point)
            log_event("corner_probe", probe_record)

        corner_state.corner_set = cs
        return {
            "status": status,
            "measurements": measurements,
            "by_testbench": by_testbench,
            "corner_worst": corner_worst,
            "probe": probe_record,
        }

    return simulate_fn
```

`_run_point`는 `point is NOMINAL`이면 `state.current_netlist_paths()`의 경로를
그대로 쓰고(렌더링 없음), 아니면 `render_corner_netlist`로 임시 파일을 만든다.
`run_full_pvt_sweep`이 하는 임시 디렉터리 패턴을 그대로 따른다.

`_judge_probe`는 `evaluate_criteria(probe_measurements, spec.all_criteria)`를
불러 `{"corner": label(point), "failed": not overall_pass, "promoted": ...}`를
만든다.

**`worst_case_measurements`는 `CornerPoint`의 필드를 읽는다.** `NOMINAL`이 `None`
이므로 그대로 넘기면 터진다. 이 함수에 넘길 때만 `NOMINAL`을 덱 좌표를 나타내는
표시용 `CornerPoint`로 바꾸거나, `pvt.py` 쪽에서 `None`을 받도록 한 줄
방어한다 — **어느 쪽이든 NaN을 쓰지 말 것**(Global Constraints 참조). 구현자가
둘 중 덜 침습적인 쪽을 고르고 그 이유를 보고서에 적는다.

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_corner_sim.py -v`
Expected: PASS

- [ ] **Step 5: 탐침의 실패하는 테스트를 쓴다**

```python
@pytest.mark.asyncio
async def test_a_failing_probe_does_not_change_what_the_judge_sees(tmp_path):
    # 탐침이 판정에 섞이면 축소 집합의 낙관성 논증이 흐려진다. 탐침 값을
    # worst_case에 넣는 변형을 이 단언이 잡는다.
    state = _state(tmp_path)
    cs = CornerSet(corners=(NOMINAL,), probe_order=(FS,))
    sim = build_corner_simulate(
        _agent(), _backend([{"g": 50.0}, {"g": 10.0}]),  # nominal, 탐침
        state, CornerState(cs), _noop_log,
    )
    result = await sim({"tb": DECK}, _spec_ge_40())
    assert result["measurements"]["g"] == 50.0


@pytest.mark.asyncio
async def test_a_failing_probe_is_promoted_into_the_selected_set(tmp_path):
    state = _state(tmp_path)
    holder = CornerState(CornerSet(corners=(NOMINAL,), probe_order=(FS,)))
    sim = build_corner_simulate(
        _agent(), _backend([{"g": 50.0}, {"g": 10.0}]), state, holder, _noop_log,
    )
    await sim({"tb": DECK}, _spec_ge_40())
    assert FS in holder.corner_set.corners


@pytest.mark.asyncio
async def test_a_passing_probe_is_not_promoted(tmp_path):
    state = _state(tmp_path)
    holder = CornerState(CornerSet(corners=(NOMINAL,), probe_order=(FS,)))
    sim = build_corner_simulate(
        _agent(), _backend([{"g": 50.0}, {"g": 49.0}]), state, holder, _noop_log,
    )
    await sim({"tb": DECK}, _spec_ge_40())
    assert FS not in holder.corner_set.corners


@pytest.mark.asyncio
async def test_a_probe_that_raises_does_not_stop_the_iteration(tmp_path):
    # 탐침은 판정에 참여하지 않으므로 실패가 루프를 멈출 이유가 없다.
    state = _state(tmp_path)
    holder = CornerState(CornerSet(corners=(NOMINAL,), probe_order=(FS,)))
    sim = build_corner_simulate(
        _agent(), _backend_raising_on_call(2, [{"g": 50.0}]), state, holder, _noop_log,
    )
    result = await sim({"tb": DECK}, _spec_ge_40())
    assert result["measurements"]["g"] == 50.0
```

- [ ] **Step 6: 탐침을 완성하고 통과를 확인한다**

탐침 시뮬레이션을 `try/except Exception`으로 감싸고, 실패하면 그 반복의 탐침을
없던 것으로 하고 `log_event("corner_probe", {"error": ...})`를 남긴다.

Run: `.venv/bin/python -m pytest tests/unit/test_corner_sim.py -v`
Expected: PASS

- [ ] **Step 7: 커밋**

```bash
git add src/analogcoder/corner_sim.py tests/unit/test_corner_sim.py
git commit -m "feat: corner-aware simulate - 선택 집합 최악값 + 회전 탐침

탐침은 판정에 불참한다. 승격만 한다."
```

---

### Task 6: 여유분의 기준점을 축소 집합 최악값으로

**Files:**
- Modify: `src/analogcoder/judge_tools.py`, `src/analogcoder/optimizer.py`
- Test: `tests/unit/test_guard_band.py`, `tests/unit/test_optimizer_corners.py`

**Interfaces:**
- Consumes: `judge_tools.corner_allowances(reference, sweep, criteria)`
- Produces: 없음 (내부 배선)

C의 여유분은 `|전체 스윕 최악 − nominal|`이고, 탐색이 nominal에서 이뤄질 때
"nominal이 보지 못하는 간격"을 뜻한다. 탐색이 축소 집합의 최악값에서 이뤄지면
같은 여유분을 또 요구하는 것이 되어 **같은 간격을 두 번 세게 된다** — 가드가
과도하게 조여져 탐색이 한 발짝도 못 나간다. C에서 비율 폴백이 빈 구간을 만들었던
것과 같은 실패 모양이다.

`corner_allowances`의 **시그니처는 바뀌지 않는다.** 첫 인자는 측정값 dict이고
축소 집합의 최악값도 같은 모양이다. 인자 이름만 `nominal` → `reference`로 바꾸고
docstring에 무엇의 기준점인지 적는다. 호출부가 넘기는 것이 바뀔 뿐이다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/unit/test_guard_band.py`에:

```python
def test_the_allowance_is_measured_from_whatever_reference_it_is_given():
    # 같은 스윕에 대해, 기준점이 최악에 가까울수록 여유분이 작아야 한다.
    # 기준점을 무시하고 nominal을 어딘가에서 다시 읽는 변형을 이 단언이 잡는다.
    criteria = [Criterion(name="gain", measurement="g", operator=">=", threshold=40.0)]
    sweep = {"criteria": [{"name": "gain", "actual": 41.0}]}

    from_nominal = corner_allowances({"g": 50.0}, sweep, criteria)
    from_reduced = corner_allowances({"g": 43.0}, sweep, criteria)

    assert from_nominal["gain"] == pytest.approx(9.0)
    assert from_reduced["gain"] == pytest.approx(2.0)
    assert from_reduced["gain"] < from_nominal["gain"]
```

`tests/unit/test_optimizer_corners.py`에:

```python
@pytest.mark.asyncio
async def test_the_allowance_reference_is_the_measurement_the_search_actually_sees(tmp_path):
    # 탐색이 축소 집합 최악값을 보는데 여유분이 nominal 기준이면 같은 간격을
    # 두 번 센다. run_optimization이 기준선 시뮬레이션의 측정값을 그대로
    # 기준점으로 넘기는지 확인한다 - 그 측정값이 곧 탐색이 보는 값이다.
    ...
```

이 두 번째 테스트의 구체적 형태는 `test_optimizer_corners.py`의 기존
기준선-측정 테스트를 읽고 그 픽스처를 재사용해 쓴다. 핵심 단언은 **여유분 계산에
들어간 기준점이 `_run_simulation`이 돌려준 측정값과 같다**는 것이다.

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_guard_band.py tests/unit/test_optimizer_corners.py -v`
Expected: FAIL

- [ ] **Step 3: 구현한다**

`judge_tools.py`에서 인자 이름을 바꾸고 docstring을 고친다:

```python
def corner_allowances(
    reference: dict, sweep: dict, criteria: list[Criterion]
) -> dict[str, float]:
    """기준별로 코너가 **기준점**에서 밀어내는 실측 거리.

    기준점은 탐색이 실제로 보는 측정값이다. 탐색이 nominal 한 점을 보면
    nominal이고, 축소 코너 집합의 최악값을 보면 그 최악값이다. 둘을 섞으면
    같은 간격을 두 번 세어 가드가 과도하게 조여진다 - 축소 집합은 이미
    최악에 가깝기 때문이다.
    ...(기존 문단 유지)"""
```

`optimizer.py`에서 이 함수를 부르는 자리가 이미 기준선 시뮬레이션의 측정값을
넘기고 있는지 확인한다. 넘기고 있으면 **코드 변경 없이** corner-aware simulate가
배선되는 순간 자동으로 옳아진다 — 그때는 그 사실을 주석으로 명시하고 테스트로
고정한다. 넘기고 있지 않으면 그렇게 고친다.

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_guard_band.py tests/unit/test_optimizer_corners.py tests/unit/test_optimizer.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add src/analogcoder/judge_tools.py src/analogcoder/optimizer.py tests/unit/
git commit -m "fix: 여유분의 기준점은 탐색이 실제로 보는 측정값이다

축소 집합 최악값을 보면서 nominal 기준 여유분을 요구하면 같은 간격을
두 번 센다."
```

---

### Task 7: `cli.py` 배선 — 재진입 루프, 경로 불일치, argmax drift

**Files:**
- Modify: `src/analogcoder/cli.py`
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: Task 1·3·5·6의 전부
- Produces: `result["corner_reduction"]` — `{"active": bool, "reason": str | None,
  "final_set": list[str], "attempts": int, "grown": list[list[str]],
  "path_disagreement": dict | None, "argmax_drift": dict}`

`cli.py:155-235`의 현재 배선을 먼저 읽는다. 새 흐름:

```
corner_capable = spec.pvt_corners is not None
reduction_active = corner_capable and spec.corner_reduction and spec.corner_reduction.enabled

baseline_sweep = run_full_pvt_sweep(...)            # 이미 있음, 비-게이팅
corner_state = CornerState(seed_from_sweep(baseline_sweep, spec))   # 축소가 켜졌을 때만
agents.simulate = corner-aware simulate_fn          # 축소가 켜졌을 때만

attempt = 0
while True:
    result = await run_orchestration(...)
    ... 최적화 (오늘과 같은 자리) ...
    판정 스윕 (오늘과 같은 자리)
    if 통과: break
    if not reduction_active or attempt >= retry_budget: break     # 오늘처럼 FAIL
    grown, added = grown_with(corner_state.corner_set, 판정스윕, 실패한 기준 이름들)
    if not added: 경로 불일치로 기록하고 break
    corner_state.corner_set = grown
    attempt += 1
argmax drift 기록
```

- [ ] **Step 1: 재진입의 실패하는 테스트를 쓴다**

`tests/unit/test_cli.py`의 기존 `_orchestration`(89행), `_pass_result`,
`_one_history_event` 헬퍼를 먼저 읽는다. `_orchestration`은 **v0을 push하고 그
위에 튜닝된 v1을 push하는** 대역이며, 그 동작을 흉내내지 않는 mock은 프로덕션에
없는 상태를 만든다 — 그 이유가 그 함수의 docstring에 적혀 있으니 읽을 것.

재진입 테스트는 `run_orchestration`이 **여러 번** 불리므로 호출 순서대로 결과를
주는 대역이 필요하다. 기존 `_orchestration`을 바꾸지 말고(다른 테스트 20여 개가
쓴다) 그 옆에 하나 더 만든다:

```python
def _orchestration_sequence(results, calls: list):
    """호출마다 다음 결과를 주는 run_orchestration 대역. 재진입을 재려면
    같은 결과를 반복해 주는 대역으로는 부족하다 - 몇 번 불렸는지가 요점이다."""

    async def fake(initial_netlist_texts, spec, state, agents):
        state.push_netlist_version(initial_netlist_texts)
        state.push_netlist_version({name: TUNED_TEXT for name in initial_netlist_texts})
        calls.append(state.current_netlist_texts())
        return results[min(len(calls) - 1, len(results) - 1)]

    return fake


def _sweep_sequence(sweeps, calls: list):
    """run_full_pvt_sweep 대역. 진입 스윕이 첫 호출이고 그 뒤가 판정 스윕들이다."""

    def fake(netlist_texts, spec, sim_backend):
        calls.append(netlist_texts)
        return sweeps[min(len(calls) - 1, len(sweeps) - 1)]

    return fake


def _sweep(overall_pass, worst_corners, per_corner=()):
    return {
        "overall_pass": overall_pass,
        "criteria": [{"name": n, "passed": overall_pass} for n in worst_corners],
        "summary": "ok" if overall_pass else "one or more criteria failed",
        "worst_case_corners": worst_corners,
        "per_corner": list(per_corner),
    }
```

그 위에서:

```python
@pytest.mark.asyncio
async def test_a_failing_verdict_sweep_grows_the_set_and_retunes(tmp_path, monkeypatch):
    # 오늘은 여기서 FAIL 보고하고 끝난다. 재진입을 지우는 변형은 attempts==0과
    # status=="FAIL"을 남기므로 두 단언이 함께 잡는다.
    entry = _sweep(True, {"gain": _wc("fs", 41.0)})
    verdict_fail = _sweep(False, {"gain": _wc("ff", 12.0)})
    verdict_pass = _sweep(True, {"gain": _wc("ff", 45.0)})
    sweep_calls: list = []
    monkeypatch.setattr(cli, "run_full_pvt_sweep",
                        _sweep_sequence([entry, verdict_fail, verdict_pass], sweep_calls))
    orch_calls: list = []
    monkeypatch.setattr(cli, "run_orchestration",
                        _orchestration_sequence([_pass_result(str(tmp_path))], orch_calls))

    result = await cli._run(_args(tmp_path, spec="spec_with_corner_reduction.yaml"))

    assert len(orch_calls) == 2                       # 재진입이 실제로 일어났다
    assert result["corner_reduction"]["attempts"] == 1
    assert "ff/1.98/125.0" in result["corner_reduction"]["final_set"]
    assert result["status"] == "PASS"


@pytest.mark.asyncio
async def test_the_retry_is_seeded_from_the_converged_deck_not_the_original(tmp_path, monkeypatch):
    # 되돌리면 앞선 튜닝의 진전을 통째로 버린다. 재진입에 원본을 넘기는
    # 변형을 이 단언이 잡는다 - 두 번째 호출이 받은 덱이 v1이어야 한다.
    ...  # 위와 같은 배선, 마지막에:
    assert orch_calls[1]["ac_loop_gain"] == TUNED_TEXT


@pytest.mark.asyncio
async def test_the_retry_budget_is_respected(tmp_path, monkeypatch):
    # 예산을 무시하는 변형은 스윕이 계속 실패하는 시나리오에서 끝나지 않는다.
    # 매번 다른 코너가 실패해야 집합이 계속 자라고 경로 불일치로 빠지지 않는다.
    entry = _sweep(True, {"gain": _wc("fs", 41.0)})
    fails = [_sweep(False, {"gain": _wc(p, 12.0)}) for p in ("ff", "ss", "sf")]
    ...
    assert result["corner_reduction"]["attempts"] == 2      # retry_budget=2
    assert result["status"] == "FAIL"


@pytest.mark.asyncio
async def test_a_failure_that_adds_no_new_corner_is_reported_as_a_path_disagreement(
    tmp_path, monkeypatch
):
    # 중간 루프가 코너 c에서 통과라 했는데 판정 스윕이 같은 덱의 같은 c에서
    # 실패했다면 두 경로가 다른 말을 하고 있는 것이다. 재시도하면 같은 정보로
    # 같은 결과를 낼 뿐이다. 무조건 재시도하는 변형은 예산을 다 태우므로
    # attempts 단언이 잡는다.
    entry = _sweep(True, {"gain": _wc("fs", 41.0)})       # 씨앗 = {NOMINAL, fs}
    verdict = _sweep(False, {"gain": _wc("fs", 12.0)})    # 이미 집합 안이다
    ...
    assert result["corner_reduction"]["attempts"] == 0
    assert result["corner_reduction"]["path_disagreement"] is not None
    assert "path disagreement" in result["failure_reason"]


@pytest.mark.asyncio
async def test_reduction_is_inactive_and_says_why_without_pvt_corners(tmp_path, monkeypatch):
    # 조용히 아무것도 안 하는 것이 이 저장소가 반복해서 당한 실패 모양이다.
    # reason을 None으로 두는 변형은 run 결과만 보고는 축소가 왜 꺼졌는지
    # 알 수 없게 만든다.
    ...
    assert result["corner_reduction"]["active"] is False
    assert result["corner_reduction"]["reason"] is not None
    assert _one_history_event(str(tmp_path), "corner_reduction_inactive")
```

`_args`와 스펙 픽스처는 기존 CLI 테스트가 쓰는 방식을 그대로 따른다.
`_wc(process, value)`는 Task 3 테스트의 `_wc`와 같은 모양이되 voltage/temperature는
스펙의 `pvt_corners`에 실제로 있는 값을 쓴다.

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_cli.py -k corner -v`
Expected: FAIL — `KeyError: 'corner_reduction'`

- [ ] **Step 3: 재진입 루프를 구현한다**

`_run`의 오케스트레이션 부분을 `while True` 루프로 감싼다. **최적화와 판정
스윕은 루프 안에 남는다** — 최적화기는 이미 "진입 스윕이 실패한 설계는
최적화하지 않는다"는 규칙이 있어 실패 시 그 스윕만 돌고 즉시 돌아오므로, 실패한
시도의 비용은 어차피 필요했던 판정 스윕 하나뿐이다. 루프 밖에 두면 판정 스윕이
하나 더 필요해진다.

재진입은 **수렴된 덱에서 시작한다** — 롤백하지 않는다. 되돌리면 진전을 버린다.
따라서 `run_orchestration`에 넘기는 것은 `state.current_netlist_texts()`이지
파일에서 읽은 원본이 아니다(최적화 배선이 같은 이유로 같은 값을 넘기고 있으니
그 주석을 참고할 것).

재진입마다 `MAX_OUTER_ITERATIONS` 예산은 **새로 받는다.** `run_orchestration`이
호출마다 0에서 세므로 별도 작업은 없지만, 그 결과 최악의 경우 비용이
`(R+1) × MAX_OUTER_ITERATIONS × 반복당 비용`이 된다는 사실은 주석으로 명시한다.
예산을 이어받게 만들면 예산이 소진된 상태로 재진입해 아무 일도 일어나지 않는다.

**corner-aware `simulate_fn`은 오케스트레이터와 최적화기 **양쪽**에 간다.**
`cli.py`는 같은 `simulate_fn`을 `OrchestrationAgents`와 `OptimizerAgents`에 각각
넘기고 있으므로, 배선을 한 곳만 바꾸면 최적화 탐색은 여전히 nominal만 본다 —
그러면 Task 6의 여유분 기준점 변경과 어긋나 가드가 잘못된 방향으로 조여진다.
두 곳 모두 같은 함수를 받는지 확인한다.

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: argmax drift 계측의 실패하는 테스트를 쓴다**

```python
@pytest.mark.asyncio
async def test_the_run_records_whether_each_criterion_s_worst_corner_moved(tmp_path):
    # 이 숫자 자체가 산출물이다 - 다음에 어떤 축소 기법을 검토할지가 여기서
    # 결정된다. moved를 항상 False로 두는 변형을 이 단언이 잡는다.
    ...
    drift = result["corner_reduction"]["argmax_drift"]
    assert drift["moved_count"] == 1
    assert drift["total"] == 2
    moved = [c for c in drift["criteria"] if c["moved"]]
    assert moved[0]["entry"] == "fs/1.98/125.0" and moved[0]["final"] == "ff/1.98/125.0"
```

- [ ] **Step 6: argmax drift를 구현하고 통과를 확인한다**

진입 스윕과 판정 스윕의 `worst_case_corners`를 기준 이름으로 대조해
`{"criteria": [...], "moved_count": int, "total": int}`를 만들고
`state.log_event("corner_argmax_drift", ...)` + `result`에 싣는다.
**판정에 아무 영향을 주지 않는다 — 순수한 기록이다.**

Run: `.venv/bin/python -m pytest tests/unit/test_cli.py -v`
Expected: PASS

- [ ] **Step 7: 나머지 로깅을 붙이고 전체 스위트를 돌린다**

`corner_set_seeded`, `corner_set_grown`, `corner_path_disagreement`,
`corner_reduction_inactive`를 `history.jsonl`에 남긴다(`corner_probe`는 Task 5가
이미 남긴다).

Run: `.venv/bin/python -m pytest -q --deselect "tests/unit/test_optimizer_bandgap_ngspice.py::test_the_optimizer_lowers_iq_while_every_criterion_still_passes"`
Expected: PASS, 경고 없음

- [ ] **Step 8: 커밋**

```bash
git add src/analogcoder/cli.py tests/unit/test_cli.py
git commit -m "feat: 판정 스윕 실패 시 코너를 배워 재튜닝

새 코너를 하나도 못 더하는 실패는 경로 불일치이므로 재시도하지 않는다."
```

---

### Task 8: 벤치마크 스펙, 실제 ngspice 종단 테스트, CLAUDE.md

**Files:**
- Create: `benchmarks/bandgap/spec_corner_reduction.yaml`
- Create: `tests/unit/test_corner_reduction_bandgap_ngspice.py`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: Task 1–7의 전부

**코너 수가 적은 전용 스펙을 쓴다.** 메커니즘은 코너 개수와 무관하므로 45개를 돌
이유가 없고, C의 30분짜리 테스트를 하나 더 만들 이유는 더더욱 없다.
`spec_pvt.yaml`의 45코너는 그대로 두되 이 테스트에서 건드리지 않는다.

- [ ] **Step 1: 스펙을 만든다**

`benchmarks/bandgap/spec_pvt.yaml`을 복사해 `pvt_corners`만 줄이고
`corner_reduction:` 블록을 더한다. `optimize:` 블록은 **뺀다** — 이 테스트가 재는
것은 축소와 재진입이지 최적화가 아니고, 최적화를 끼우면 실행 시간이 예측
불가능해진다.

```yaml
# 코너 축소·재진입의 종단 테스트용. 메커니즘은 코너 개수와 무관하므로
# process 축만 3개로 두고 V/T는 고정한다 - 45코너를 도는 spec_pvt.yaml과
# 같은 것을 재면서 30분이 아니라 초 단위로 끝난다.
pvt_corners:
  process: ["tt", "ss", "ff"]
  voltage: [1.8]
  temperature: [27]
corner_reduction:
  enabled: true
  retry_budget: 2
  probe: true
```

- [ ] **Step 2: 축소가 실제로 일어나는지 실측으로 확인한다**

먼저 손으로 한 번 돌려 진입 스윕이 뽑는 씨앗이 실제로 전체보다 작은지 본다.
3코너 전부가 씨앗이 되면 이 스펙으로는 축소를 관찰할 수 없으므로 **기준 하나를
조여** 특정 process에서만 아슬해지게 만든다. 어떤 기준을 얼마로 조였고 왜 그
값인지 스펙 파일에 주석으로 적는다.

```bash
.venv/bin/python -c "
from analogcoder.spec import load_spec
from analogcoder.pvt import run_full_pvt_sweep
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.netlist import resolve_includes
spec = load_spec('benchmarks/bandgap/spec_corner_reduction.yaml')
texts = {tb.name: resolve_includes(open(tb.netlist_path).read(), tb.netlist_path) for tb in spec.testbenches}
sweep = run_full_pvt_sweep(texts, spec, NgspiceBackend())
print(sweep['overall_pass'])
for name, wc in sweep['worst_case_corners'].items():
    print(name, wc['process'], wc['value'])
"
```

- [ ] **Step 3: 종단 테스트를 쓴다**

`ngspice`를 PATH에서 가정한다(스킵 게이트 없음 — 이 저장소의 관례).

```python
def test_the_mid_loop_sees_corners_and_the_set_is_smaller_than_the_full_sweep():
    # 축소가 실제로 일어났음을 씨앗 크기로 못박는다. 씨앗이 전체와 같으면
    # 이 서브프로젝트는 아무것도 줄이지 않은 것이다.
    ...
    assert 1 <= len(cs.corners) < 1 + len(all_corners(spec.pvt_corners))
    assert cs.corners[0] is NOMINAL


def test_the_judge_sees_a_worse_value_than_nominal_alone():
    # 중간 루프가 코너를 본다는 것의 관찰 가능한 결과. corner-aware simulate가
    # 배선되지 않으면 judge는 nominal 값을 그대로 본다.
    ...


def test_a_probe_run_records_its_corner_in_history():
    # 탐침이 조용히 꺼져 있으면 이 단언이 잡는다.
    ...
```

각 테스트의 **어떤 변형을 잡는가**를 주석으로 적는다.

- [ ] **Step 4: 테스트를 돌린다**

Run: `.venv/bin/python -m pytest tests/unit/test_corner_reduction_bandgap_ngspice.py -v`
Expected: PASS. **`UNCHANGED`나 "축소가 안 일어남"이 정당한 결과일 수 있다** —
그러면 그 진단이 산출물이다. 테스트를 초록으로 만들려고 고치지 말 것.

- [ ] **Step 5: `CLAUDE.md`를 고친다**

`### The optimization phase` 다음에 `### 코너 축소와 재진입` 절을 더한다. 적을 것:

- 축소 집합은 항상 낙관적 — 중간 FAIL은 진짜, 중간 PASS는 틀릴 수 있다.
  잠긴 제약이 지켜지는 이유
- nominal은 이름이 아니라 덱 자체이고, `CornerPoint`에 NaN을 넣으면 자기 자신과도
  같지 않아 집합 연산이 조용히 깨진다
- 탐침은 판정에 불참한다. 승격만 한다
- 새 코너를 못 더하는 판정 실패는 경로 불일치이며 재시도하지 않는다
- 여유분의 기준점이 nominal에서 축소 집합 최악값으로 옮겨간 이유
- **이번 실행에서 실제로 측정된 수치** — 씨앗 크기, argmax 이동 개수,
  재진입 횟수. 측정하지 않은 것은 적지 않는다
- best-arm identification을 기각한 이유(결정론적 평가에는 이득 구조가 없다)

`## Testing conventions`에 새 ngspice 테스트의 실행 시간을 적는다.

- [ ] **Step 6: 전체 스위트를 돌린다**

Run: `.venv/bin/python -m pytest -q --deselect "tests/unit/test_optimizer_bandgap_ngspice.py::test_the_optimizer_lowers_iq_while_every_criterion_still_passes"`
Expected: PASS, 경고 없음

- [ ] **Step 7: 커밋**

```bash
git add benchmarks/bandgap/spec_corner_reduction.yaml tests/unit/test_corner_reduction_bandgap_ngspice.py CLAUDE.md
git commit -m "test+docs: 실제 ngspice로 코너 축소와 재진입을 확인"
```
