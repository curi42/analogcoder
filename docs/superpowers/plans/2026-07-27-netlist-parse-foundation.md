# 넷리스트 파스 기반(E1) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 넷리스트를 읽어서 나온 값이 실제 소자의 값과 일치하도록 파스 계층을 고친다 — 중첩 스코프 전체 추적, HSPICE 어휘 방언 내성, `.param` 해석.

**Architecture:** `netlist.py`는 줄과 소자를 다루고, 새 모듈 `params.py`가 파라미터 환경과 표현식 평가를 다룬다. `Component`는 원본 토큰과 해소된 수치를 **둘 다** 갖는다 — `apply_changes`는 원본을 편집하는 텍스트 연산으로 남고, `check_area_growth`는 수치를 읽는다. 서브회로 스코프는 단일 이름에서 점으로 구분된 경로가 된다.

**Tech Stack:** Python 3, 표준 라이브러리만(`re`, `ast`, `dataclasses`, `json`). 새 의존성 없음. pytest.

## 설계 문서

`docs/superpowers/specs/2026-07-27-netlist-parse-foundation-design.md`. 이 계획이 다루는 세 결함의 재현 코드와 근거가 거기 있다.

## Global Constraints

- **골든 스냅샷은 반드시 파서를 건드리기 전에 생성·커밋한다.** 새 코드로 만든 골든 파일은 아무것도 증명하지 못한다. Task 1이 이것이고, 나머지 모든 Task가 이 스냅샷을 그린으로 유지해야 한다.
- **해소 불가는 절대 추측하지 않는다.** 값을 확정할 수 없으면 `None`을 내고 기존 폴백("판단 불가, 막지 않음")을 탄다. 조용히 틀린 숫자보다 명시적 "모름"이 낫다.
- **`apply_changes`는 텍스트 연산으로 남는다.** 해소된 수치는 읽기 전용 파생물이며, 편집은 언제나 원본 토큰에 한다.
- 기존 10개 벤치마크 넷리스트(bandgap 5, two_stage_opamp 4, inverting_amp 1)의 파스 결과는 이 작업 전체에서 **바이트 단위로 불변**이어야 한다.
- 새 외부 의존성을 추가하지 않는다. 표현식 평가는 `ast` 화이트리스트로 하며 `eval`을 쓰지 않는다.
- HSPICE **시뮬레이션**은 범위 밖이다. 순수 텍스트로 테스트되는 어휘 항목만 다룬다.
- 테스트는 TDD로 작성한다: 실패하는 테스트 → 실패 확인 → 최소 구현 → 통과 확인 → 커밋.

## 파일 구조

| 파일 | 책임 | Task |
|---|---|---|
| `tests/unit/test_netlist_golden.py` | 10개 벤치마크 파스 결과 불변 검증 | 1 |
| `tests/fixtures/netlist_golden/*.json` | 골든 스냅샷 (Task 1에서 현재 코드로 생성) | 1 |
| `src/analogcoder/netlist.py` | 줄·소자 파싱, 스코프 경로, 주석 분리, 변경 적용 | 2, 3, 5 |
| `tests/unit/test_netlist_dialect.py` | `$`/`;` 주석, `.macro`/`.eom`/`.inc` | 2 |
| `tests/unit/test_netlist_nested_scope.py` | 중첩 스코프 추적, `apply_topology_swap` 중첩 | 3 |
| `src/analogcoder/params.py` | 파라미터 환경 수집, 경계가 명시된 표현식 평가 | 4 |
| `tests/unit/test_params.py` | 해석 규칙과 해소 불가 경계 | 4 |
| `tests/fixtures/hspice_flavoured.cir` | 방언·중첩·파라미터화를 담은 합성 덱 | 4 |
| `src/analogcoder/area_limits.py` | 해소된 수치로 티어 판정 | 5 |
| `src/analogcoder/schemas.py` | `TUNER_SCHEMA` refdes 정규식 완화 | 6 |
| `src/analogcoder/agents/tuner.py` | 중첩 경로 주소지정 프롬프트 | 6 |

---

## Task 1: 골든 스냅샷을 현재 코드로 고정

이 Task는 **파서를 전혀 건드리지 않는다.** 목적은 이후 Task들이 무엇을 깨뜨리는지 알 수 있게 현재 동작을 박제하는 것이다.

**Files:**
- Create: `tests/unit/test_netlist_golden.py`
- Create: `tests/fixtures/netlist_golden/` (JSON 스냅샷 10개, 스크립트로 생성)

**Interfaces:**
- Consumes: 기존 `analogcoder.netlist.parse_netlist`
- Produces: `_snapshot(text: str) -> dict` — 이후 Task에서 재사용하지 않는다(테스트 내부 헬퍼)

- [ ] **Step 1: 스냅샷 직렬화와 검증 테스트를 작성한다**

`tests/unit/test_netlist_golden.py`:

```python
import json
import os

import pytest

from analogcoder.netlist import parse_netlist

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures", "netlist_golden")

# 10개 벤치마크 넷리스트. 이 목록이 줄어들면 커버리지가 조용히 사라지므로
# 개수도 함께 단언한다.
NETLISTS = [
    "benchmarks/bandgap/netlist.cir",
    "benchmarks/bandgap/netlist_startup.cir",
    "benchmarks/bandgap/netlist_psrr.cir",
    "benchmarks/bandgap/netlist_settling.cir",
    "benchmarks/bandgap/netlist_loops.cir",
    "benchmarks/two_stage_opamp/netlist.cir",
    "benchmarks/two_stage_opamp/netlist_psr_plus.cir",
    "benchmarks/two_stage_opamp/netlist_psr_minus.cir",
    "benchmarks/two_stage_opamp/netlist_settling.cir",
    "benchmarks/inverting_amp/netlist.cir",
]


def _component_dict(component) -> dict:
    return {
        "refdes": component.refdes,
        "ctype": component.ctype,
        "nodes": component.nodes,
        "value": component.value,
        "params": dict(sorted(component.params.items())),
        "scope": component.scope,
        "geometry_scale": component.geometry_scale,
    }


def _snapshot(text: str) -> dict:
    parsed = parse_netlist(text)
    return {
        "top_components": [_component_dict(c) for c in parsed.top_components],
        "subckts": {
            key: {
                "ports": subckt.ports,
                "components": [_component_dict(c) for c in subckt.components],
            }
            for key, subckt in sorted(parsed.subckts.items())
        },
    }


def _golden_path(rel_netlist: str) -> str:
    return os.path.join(GOLDEN_DIR, rel_netlist.replace("/", "__") + ".json")


def test_the_golden_set_covers_every_benchmark_netlist():
    # 목록이 조용히 줄어들면 이 파일 전체가 무의미해진다.
    assert len(NETLISTS) == 10
    for rel in NETLISTS:
        assert os.path.exists(os.path.join(REPO, rel)), rel


@pytest.mark.parametrize("rel", NETLISTS)
def test_parse_result_matches_the_golden_snapshot(rel):
    with open(os.path.join(REPO, rel)) as f:
        actual = _snapshot(f.read())
    with open(_golden_path(rel)) as f:
        expected = json.load(f)

    assert actual == expected, (
        f"{rel}의 파스 결과가 골든 스냅샷과 다르다. 의도한 변경이라면 "
        f"스냅샷을 다시 만들되, 무엇이 왜 바뀌었는지 커밋 메시지에 적을 것."
    )
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_netlist_golden.py -q`
Expected: `test_the_golden_set_covers_every_benchmark_netlist`는 PASS(파일 경로가 실재한다면), 10개 스냅샷 테스트는 골든 파일이 없어 `FileNotFoundError`로 FAIL.

경로가 틀렸다면 `ls benchmarks/*/netlist*.cir`로 실제 파일명을 확인하고 `NETLISTS`를 고친다.

- [ ] **Step 3: 현재 코드로 골든 파일을 생성한다**

```bash
mkdir -p tests/fixtures/netlist_golden
.venv/bin/python - <<'PY'
import json, os, sys
sys.path.insert(0, "tests/unit")
from test_netlist_golden import NETLISTS, REPO, _snapshot, _golden_path
for rel in NETLISTS:
    with open(os.path.join(REPO, rel)) as f:
        snap = _snapshot(f.read())
    with open(_golden_path(rel), "w") as f:
        json.dump(snap, f, indent=2, sort_keys=True)
        f.write("\n")
    print("wrote", _golden_path(rel))
PY
```

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_netlist_golden.py -q`
Expected: 11 passed.

전체 스위트도 함께: `.venv/bin/python -m pytest -q` → 이전과 같은 수 + 11.

- [ ] **Step 5: 커밋**

```bash
git add tests/unit/test_netlist_golden.py tests/fixtures/netlist_golden
git commit -m "test: pin the current parse result of all ten benchmark netlists

Generated from the CURRENT parser, before any change to it. A golden file
generated from the new code would prove nothing; this is the guarantee that
the E1 parse rewrite does not silently move a value in a benchmark that took
real ngspice measurement to characterise."
```

---

## Task 2: 인라인 주석과 디렉티브 별칭

**Files:**
- Modify: `src/analogcoder/netlist.py`
- Test: `tests/unit/test_netlist_dialect.py`

**Interfaces:**
- Produces: `strip_inline_comment(line: str) -> tuple[str, str]` — `(코드부, 주석부)`. 주석이 없으면 `(line, "")`. 주석부는 마커를 포함하고 앞뒤 공백은 정규화된다. Task 3의 `_line_scopes`/`apply_topology_swap`, Task 4의 `.param` 수집이 모두 이걸 쓴다.

**왜 단순 치환이 아닌가:** `apply_changes`는 줄을 토큰으로 쪼개 다시 합친다. 주석을 그냥 두면 `M1 d g 0 0 nch $ note`에서 `param="value"`가 마지막 위치 토큰인 `note`를 모델명으로 착각해 교체한다. 그래서 코드부와 주석부를 분리해 토큰 조작은 코드부에만 하고, 합칠 때 주석부를 되붙인다.

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/unit/test_netlist_dialect.py`:

```python
from analogcoder.netlist import (
    apply_changes,
    parse_netlist,
    resolve_includes,
    strip_inline_comment,
)


def test_strip_inline_comment_splits_code_from_comment():
    assert strip_inline_comment("M1 d g 0 0 nch W=1") == ("M1 d g 0 0 nch W=1", "")
    assert strip_inline_comment("M1 d g 0 0 nch $ note") == ("M1 d g 0 0 nch", "$ note")
    assert strip_inline_comment("Rf a b 10k ; note") == ("Rf a b 10k", "; note")


def test_a_dollar_comment_does_not_swallow_the_model_name():
    # 회귀: 예전에는 nodes가 ['d','g','0','0','nch','$','hspice','comment']가
    # 되고 value가 'comment'가 되어 디바이스 종류가 통째로 사라졌다.
    deck = "* t\nM1 d g 0 0 nch W=1 L=1 $ hspice comment\n.end\n"

    component = parse_netlist(deck).top_components[0]

    assert component.nodes == ["d", "g", "0", "0"]
    assert component.value == "nch"
    assert component.params == {"W": "1", "L": "1"}


def test_a_semicolon_comment_is_stripped_the_same_way():
    deck = "* t\nRf a b 10k ; ngspice comment\n.end\n"

    component = parse_netlist(deck).top_components[0]

    assert component.nodes == ["a", "b"]
    assert component.value == "10k"


def test_applying_a_value_change_edits_the_device_not_the_comment():
    # param="value"는 마지막 위치 토큰을 바꾼다. 주석이 남아 있으면 그게
    # 마지막 위치 토큰이 되어버린다.
    deck = "* t\nRf a b 10k $ feedback resistor\n.end\n"

    out = apply_changes(deck, [{"refdes": "Rf", "param": "value", "new_value": "15k"}])

    assert "Rf a b 15k" in out
    assert "$ feedback resistor" in out


def test_macro_and_eom_are_accepted_as_subckt_and_ends():
    deck = "* t\n.macro AMP a b\nM1 a b 0 0 nch W=1\n.eom\n.end\n"

    parsed = parse_netlist(deck)

    assert list(parsed.subckts) == ["AMP"]
    assert parsed.subckts["AMP"].ports == ["a", "b"]
    assert [c.refdes for c in parsed.subckts["AMP"].components] == ["M1"]
    assert parsed.top_components == []


def test_inc_is_accepted_as_an_alias_for_include(tmp_path):
    out = resolve_includes('.inc "models.lib"\n', "/base/dir")

    assert out.strip() == '.inc "/base/dir/models.lib"'
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_netlist_dialect.py -q`
Expected: FAIL — `ImportError: cannot import name 'strip_inline_comment'`.

- [ ] **Step 3: 구현한다**

`src/analogcoder/netlist.py`의 `_INCLUDE_RE`를 교체:

```python
_INCLUDE_RE = re.compile(r'^(\s*\.inc(?:lude)?\s+)"?([^"\s]+)"?\s*$', re.IGNORECASE | re.MULTILINE)
```

`parse_spice_value` 위쪽, `_PARAM_RE` 근처에 추가:

```python
# HSPICE는 "$", ngspice는 ";"로 줄 끝 주석을 연다. 두 문자 모두 SPICE
# 식별자에 등장할 수 없으므로 첫 출현 위치에서 자르면 충분하다.
_COMMENT_MARKERS = "$;"


def strip_inline_comment(line: str) -> tuple[str, str]:
    """줄을 (코드부, 주석부)로 나눈다. 주석이 없으면 주석부는 빈 문자열.

    분리해서 돌려주는 이유는 apply_changes 때문이다. 그쪽은 코드를 토큰으로
    쪼개 다시 합치는데, 주석을 코드에 남겨두면 param="value"가 마지막 위치
    토큰(주석의 마지막 단어)을 소자 값으로 착각해 교체한다."""
    positions = [line.find(marker) for marker in _COMMENT_MARKERS]
    positions = [p for p in positions if p != -1]
    if not positions:
        return line, ""
    index = min(positions)
    return line[:index].rstrip(), line[index:].strip()
```

`.subckt`/`.ends` 판정을 공유 헬퍼로 뽑는다(Task 3에서 재사용):

```python
def _is_subckt_open(lower: str) -> bool:
    return lower.startswith(".subckt") or lower.startswith(".macro")


def _is_subckt_close(lower: str) -> bool:
    return lower.startswith(".ends") or lower.startswith(".eom")
```

`parse_netlist`의 줄 루프에서, `*` 판정 **다음에** 주석을 벗기고 `.subckt`/`.ends` 판정을 헬퍼로 바꾼다:

```python
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("*"):
            continue
        line, _ = strip_inline_comment(line)
        if not line:
            continue
        lower = line.lower()
        if _is_subckt_open(lower):
            ...
        if _is_subckt_close(lower):
            ...
```

`*` 검사를 먼저 하는 순서가 중요하다: 벤치마크의 `;`는 전부 이미 `*` 주석 줄 안에 있으므로, 이 순서라야 골든 스냅샷이 움직이지 않는다.

`_find_matches`와 `apply_changes`도 코드부만 다루도록 고친다. `_find_matches`:

```python
    for i, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("*") or stripped.startswith("."):
            continue
        code, _ = strip_inline_comment(stripped)
        if not code:
            continue
        tokens = code.split()
        if tokens[0] != refdes:
            continue
        ...
```

`apply_changes`의 줄 재조립부:

```python
        i, tokens = matches[0]
        _, comment = strip_inline_comment(lines[i].strip())
        # ... 기존 토큰 조작 ...
        lines[i] = " ".join(tokens) + (f" {comment}" if comment else "")
```

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_netlist_dialect.py tests/unit/test_netlist_golden.py -q`
Expected: 모두 PASS. **골든 스냅샷이 깨지면 안 된다** — 깨졌다면 `*` 판정 순서를 확인할 것.

Run: `.venv/bin/python -m pytest -q` → 전체 그린.

- [ ] **Step 5: 커밋**

```bash
git add src/analogcoder/netlist.py tests/unit/test_netlist_dialect.py
git commit -m "fix: strip inline comments and accept HSPICE directive aliases

A trailing '\$ comment' used to be parsed as netlist tokens: the model name
was swallowed into the node list and 'comment' became the component's value,
so device class, area tier and terminal roles were all wrong, silently.

Code and comment are now split, so apply_changes keeps operating on tokens
without param=\"value\" mistaking the comment's last word for the device
value. .macro/.eom and .inc are accepted as aliases."
```

---

## Task 3: 중첩 스코프 전체 추적

**Files:**
- Modify: `src/analogcoder/netlist.py`
- Modify: `src/analogcoder/area_limits.py` (`index_baseline_components`의 키)
- Test: `tests/unit/test_netlist_nested_scope.py`

**Interfaces:**
- Consumes: Task 2의 `strip_inline_comment`, `_is_subckt_open`, `_is_subckt_close`
- Produces:
  - `Subckt`에 필드 추가: `path: str` (점으로 구분된 전체 경로), `defaults: dict[str, str]` (`.subckt` 줄의 `name=value` 토큰)
  - `ParsedNetlist.subckts`의 키가 **경로**가 된다. 최상위 선언은 경로 == 이름이므로 기존 키는 그대로다.
  - `Component.scope`가 경로가 된다.
  - `_line_scopes(lines) -> list[str | None]`가 경로를 돌려준다.

**주소지정 규칙:** 한정된 refdes는 스코프 경로와 **정확히** 일치해야 한다. 부분 한정(`OUTER.INNER`인 소자를 `INNER.M1`로 지칭)은 거부한다 — 접미사 매칭은 `check_refdes_resolution`이 없애려던 모호성을 되살린다. 거부 피드백에 유효한 전체 경로를 나열하므로 튜너가 한 번의 재시도로 교정한다.

`split_scoped_refdes`는 이미 `rpartition(".")`으로 **마지막** 점에서 자르므로 코드 변경이 필요 없다. 독스트링만 고친다.

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/unit/test_netlist_nested_scope.py`:

```python
import pytest

from analogcoder.area_limits import index_baseline_components
from analogcoder.netlist import (
    apply_topology_swap,
    check_refdes_resolution,
    parse_netlist,
    split_scoped_refdes,
)

NESTED = """* t
.subckt OUTER a b
.subckt INNER c d
M1 c d 0 0 nch W=1 L=1
.ends
Xi a b INNER
M2 a b 0 0 nch W=2 L=1
.ends
.end
"""


def test_a_nested_definition_does_not_reparent_its_enclosing_components():
    # 회귀: 예전에는 OUTER가 빈 채로 파싱되고 M2/Xi가 최상위로 올라갔다.
    parsed = parse_netlist(NESTED)

    assert sorted(parsed.subckts) == ["OUTER", "OUTER.INNER"]
    assert [c.refdes for c in parsed.subckts["OUTER"].components] == ["Xi", "M2"]
    assert [c.refdes for c in parsed.subckts["OUTER.INNER"].components] == ["M1"]
    assert parsed.top_components == []


def test_scope_is_the_full_path():
    parsed = parse_netlist(NESTED)

    assert parsed.subckts["OUTER.INNER"].components[0].scope == "OUTER.INNER"
    assert parsed.subckts["OUTER"].components[0].scope == "OUTER"


def test_split_scoped_refdes_splits_on_the_last_dot():
    assert split_scoped_refdes("OUTER.INNER.M1") == ("OUTER.INNER", "M1")
    assert split_scoped_refdes("BUF_P.X6") == ("BUF_P", "X6")
    assert split_scoped_refdes("Rf") == (None, "Rf")


def test_a_full_path_resolves_and_a_partial_one_is_rejected():
    ok, _ = check_refdes_resolution(NESTED, [{"refdes": "OUTER.INNER.M1", "param": "W"}])
    assert ok is True

    ok, feedback = check_refdes_resolution(NESTED, [{"refdes": "INNER.M1", "param": "W"}])
    assert ok is False
    assert "INNER" in feedback


def test_an_unqualified_refdes_colliding_across_nesting_levels_is_ambiguous():
    deck = NESTED.replace("M2 a b 0 0 nch W=2 L=1", "M1 a b 0 0 nch W=2 L=1")

    ok, feedback = check_refdes_resolution(deck, [{"refdes": "M1", "param": "W"}])

    assert ok is False
    assert "ambiguous" in feedback
    assert "OUTER.INNER" in feedback


def test_subckt_line_parameters_are_defaults_not_ports():
    deck = "* t\n.subckt SUB a b W=10 L=1\nM1 a b 0 0 nch W=1\n.ends\n.end\n"

    subckt = parse_netlist(deck).subckts["SUB"]

    assert subckt.ports == ["a", "b"]
    assert subckt.defaults == {"W": "10", "L": "1"}


def test_the_area_index_keys_nested_components_by_path():
    indexed = index_baseline_components(NESTED)

    assert "OUTER.INNER.M1" in indexed
    assert "OUTER.M2" in indexed


def test_topology_swap_spans_a_nested_subckt_instead_of_stopping_at_its_ends():
    # 회귀: 첫 .ends를 OUTER의 끝으로 보아 본문을 잘라먹고 중첩 서브회로의
    # 꼬리를 고아로 남겼다.
    out = apply_topology_swap(NESTED, "OUTER", "M9 a b 0 0 nch W=9")

    parsed = parse_netlist(out)
    assert [c.refdes for c in parsed.subckts["OUTER"].components] == ["M9"]
    assert "OUTER.INNER" not in parsed.subckts
    assert out.count(".ends") == 1


def test_topology_swap_still_works_on_a_flat_subckt():
    deck = "* t\n.subckt AMP a b\nM1 a b 0 0 nch W=1\n.ends\nX1 p q AMP\n.end\n"

    out = apply_topology_swap(deck, "AMP", "M9 a b 0 0 nch W=9")

    assert [c.refdes for c in parse_netlist(out).subckts["AMP"].components] == ["M9"]
    assert "X1 p q AMP" in out
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_netlist_nested_scope.py -q`
Expected: FAIL. `test_a_nested_definition_does_not_reparent...`는 `sorted(parsed.subckts) == ['INNER', 'OUTER']`로, `test_subckt_line_parameters_are_defaults_not_ports`는 `AttributeError: 'Subckt' object has no attribute 'defaults'`로 실패한다.

- [ ] **Step 3: 구현한다**

`Subckt` 데이터클래스:

```python
@dataclass
class Subckt:
    name: str
    ports: list[str]
    components: list[Component] = field(default_factory=list)
    path: str = ""
    defaults: dict[str, str] = field(default_factory=dict)
```

`parse_netlist`를 스택 기반으로:

```python
def parse_netlist(text: str) -> ParsedNetlist:
    top_components: list[Component] = []
    subckts: dict[str, Subckt] = {}
    stack: list[Subckt] = []
    scale = netlist_scale(text)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("*"):
            continue
        line, _ = strip_inline_comment(line)
        if not line:
            continue
        lower = line.lower()
        if _is_subckt_open(lower):
            tokens = line.split()
            name = tokens[1]
            ports = [t for t in tokens[2:] if "=" not in t]
            defaults = dict(t.split("=", 1) for t in tokens[2:] if "=" in t)
            path = ".".join([s.name for s in stack] + [name])
            subckt = Subckt(name=name, ports=ports, path=path, defaults=defaults)
            subckts[path] = subckt
            stack.append(subckt)
            continue
        if _is_subckt_close(lower):
            if stack:
                stack.pop()
            continue
        if line.startswith("."):
            continue
        component = _parse_component_line(line)
        component.geometry_scale = scale
        if stack:
            component.scope = stack[-1].path
            stack[-1].components.append(component)
        else:
            top_components.append(component)

    return ParsedNetlist(top_components=top_components, subckts=subckts)
```

`ports`에서 `name=value` 토큰을 걸러내는 것은 새 동작이다. `.subckt SUB a b W=10`에서 `W=10`이 포트로 세어지던 것을 고친다. 기존 벤치마크는 `.subckt` 줄에 파라미터를 쓰지 않으므로 골든 스냅샷은 움직이지 않는다.

`_line_scopes`도 스택으로:

```python
def _line_scopes(lines: list[str]) -> list[str | None]:
    """각 줄이 속한 .subckt의 전체 경로("OUTER.INNER"), 최상위면 None.
    디렉티브 줄 자체는 None으로 보고하며, 모든 호출자가 어차피 건너뛴다."""
    scopes: list[str | None] = []
    stack: list[str] = []
    for raw_line in lines:
        stripped, _ = strip_inline_comment(raw_line.strip())
        lower = stripped.lower()
        if _is_subckt_open(lower):
            scopes.append(None)
            stack.append(stripped.split()[1])
            continue
        if _is_subckt_close(lower):
            scopes.append(None)
            if stack:
                stack.pop()
            continue
        scopes.append(".".join(stack) if stack else None)
    return scopes
```

`split_scoped_refdes`의 독스트링을 교체(코드는 그대로):

```python
def split_scoped_refdes(scoped: str) -> tuple[str | None, str]:
    """"OUTER.INNER.M1"을 ("OUTER.INNER", "M1")로, 맨 refdes "Xcc"를
    (None, "Xcc")로 나눈다. 스코프는 서브회로 정의의 전체 경로이며 임의
    깊이로 중첩될 수 있으므로, 마지막 점에서 자른다."""
```

`apply_topology_swap`을 깊이 추적으로:

```python
def apply_topology_swap(text: str, subckt_name: str, new_body: str) -> str:
    lines = text.splitlines()
    start = end = None
    depth = 0
    for i, raw_line in enumerate(lines):
        stripped, _ = strip_inline_comment(raw_line.strip())
        lower = stripped.lower()
        opens = _is_subckt_open(lower)
        closes = _is_subckt_close(lower)
        if start is None:
            if opens and stripped.split()[1] == subckt_name:
                start = i
                depth = 1
            continue
        if opens:
            depth += 1
        elif closes:
            depth -= 1
            if depth == 0:
                end = i
                break
    if start is None or end is None:
        raise ValueError(f"subckt {subckt_name!r} not found or not closed")
    new_lines = lines[: start + 1] + new_body.splitlines() + lines[end:]
    return "\n".join(new_lines) + "\n"
```

`area_limits.index_baseline_components`에서 서브회로 키를 경로로:

```python
    for path, subckt in parsed.subckts.items():
        for component in subckt.components:
            indexed[f"{path}.{component.refdes}"] = component
            if plain_counts[component.refdes] == 1:
                indexed[component.refdes] = component
```

(`for subckt in parsed.subckts.values():` 두 곳 중 인덱스를 만드는 쪽만 바꾼다. `plain_counts`를 세는 첫 루프는 그대로 둔다.)

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_netlist_nested_scope.py tests/unit/test_netlist_golden.py -q`
Expected: 모두 PASS, 골든 불변.

Run: `.venv/bin/python -m pytest -q` → 전체 그린. `tests/unit/test_bandgap_benchmark_ngspice.py`의 `set(parsed.subckts) == {...}` 단언은 bandgap이 서브회로를 전부 최상위에 선언하므로 경로 == 이름이라 그대로 통과한다.

- [ ] **Step 5: 커밋**

```bash
git add src/analogcoder/netlist.py src/analogcoder/area_limits.py tests/unit/test_netlist_nested_scope.py
git commit -m "fix: track subckt scope at any nesting depth

A nested .subckt definition used to close its enclosing subckt: OUTER
parsed as empty and its own components were attributed to the top level,
where check_refdes_resolution then accepted them as unambiguous.

Scope becomes a dotted path and ParsedNetlist.subckts is keyed by it,
because in SPICE a nested definition is local to its enclosing subckt and
two outer subckts may each define their own INNER. apply_topology_swap had
the same defect from the other side - it took the first .ends as the end of
the body - and now tracks depth. A qualified refdes must match a scope path
exactly; suffix matching would reintroduce the ambiguity the gate exists to
remove."
```

---

## Task 4: `.param` 해석

**Files:**
- Create: `src/analogcoder/params.py`
- Create: `tests/fixtures/hspice_flavoured.cir`
- Test: `tests/unit/test_params.py`

**Interfaces:**
- Consumes: `analogcoder.netlist.parse_netlist`, `strip_inline_comment`, `parse_spice_value`, `Subckt.defaults`, `Subckt.path`
- Produces:
  - `resolve_value(raw: str, env: dict[str, float]) -> float | None`
  - `build_param_envs(text: str) -> dict[str | None, dict[str, float]]` — 스코프 경로(최상위는 `None`) → 해소된 파라미터 환경
  - Task 5가 이 둘을 쓴다.

**해석 우선순위** (낮음 → 높음): 전역 `.param` → 서브회로 `.subckt` 줄 기본값 → 인스턴스 오버라이드.

**의도적 경계.** 다음은 모두 `None`(해소 불가)이며 **추측하지 않는다**:

- 함수 호출(`sqrt(...)`, `max(...)`)과 조건식
- 정의되지 않은 파라미터 이름
- 순환 참조
- 표현식 안의 SPICE 접미사 숫자(`2k*wn`) — `ast`가 파싱하지 못한다
- **같은 서브회로의 인스턴스들이 한 파라미터를 서로 다른 값으로 오버라이드하는 경우** — 값이 진짜로 인스턴스마다 다른데 이 프로젝트는 소자를 서브회로 *정의*로 주소지정하므로 단일 정답이 없다. 기존의 "다르게 튜닝된 두 인스턴스는 두 개의 서브회로가 필요하다"와 같은 제약이다.

**추가 경계(문서화할 것):** 인스턴스 오버라이드는 **한 단계만** 전파한다. 서브회로 S가 T 안에서 인스턴스화되고 T 자신이 서로 다른 파라미터로 두 번 인스턴스화되는 경우, S의 환경은 해소 불가로 표시한다. 전체 트리 전파는 E2의 `signal_path.py`가 인스턴스 트리를 만든 뒤에 가능하다.

- [ ] **Step 1: 합성 덱 픽스처를 만든다**

`tests/fixtures/hspice_flavoured.cir`:

```spice
* HSPICE 방언 합성 덱 - E1 전용 픽스처
*
* 생산 HSPICE 덱은 이 저장소에 없다. 이 파일은 그 대역이며,
* E1이 다루기로 한 방언 항목만 담는다: 중첩 정의, $ 주석, .macro/.eom,
* 전역/서브회로 .param, 인스턴스 오버라이드, 그리고 의도적으로 해소
* 불가능한 값 하나. 실제 덱에는 여기 없는 구문이 있을 수 있다.
.param wn=4
.param wp='wn*2'

.subckt CORE a b W=10
M1 a b 0 0 nch W='W*2' L=1 $ 서브회로 기본값을 참조
M2 a b 0 0 nch W=wp L=1 ; 전역을 참조
M3 a b 0 0 nch W='sqrt(wn)' L=1 $ 해소 불가 - 함수
.ends

.macro WRAP p q
.subckt DEEP r s
M4 r s 0 0 nch W=wn L=1
.ends
Xd p q DEEP
M5 p q 0 0 nch W=3 L=1
.eom

Xc1 n1 n2 CORE W=20
Xw1 n3 n4 WRAP
.end
```

- [ ] **Step 2: 실패하는 테스트를 작성한다**

`tests/unit/test_params.py`:

```python
import os

from analogcoder.netlist import parse_netlist
from analogcoder.params import build_param_envs, resolve_value

FIXTURE = os.path.join(
    os.path.dirname(__file__), "..", "fixtures", "hspice_flavoured.cir"
)


def _fixture_text() -> str:
    with open(FIXTURE) as f:
        return f.read()


def test_resolve_value_reads_plain_and_suffixed_literals():
    assert resolve_value("30", {}) == 30.0
    assert resolve_value("4.7u", {}) == 4.7e-6
    assert resolve_value("10k", {}) == 10000.0


def test_resolve_value_evaluates_bounded_arithmetic_over_the_environment():
    env = {"wn": 4.0}
    assert resolve_value("'wn*2'", env) == 8.0
    assert resolve_value("{wn + 1}", env) == 5.0
    assert resolve_value("'(wn - 1) * 3'", env) == 9.0
    assert resolve_value("'-wn'", env) == -4.0
    assert resolve_value("'wn**2'", env) == 16.0


def test_resolve_value_refuses_to_guess_outside_the_bounded_subset():
    env = {"wn": 4.0}
    assert resolve_value("'sqrt(wn)'", env) is None       # 함수
    assert resolve_value("'undefined_name'", env) is None  # 미정의
    assert resolve_value("'2k*wn'", env) is None           # 표현식 속 접미사
    assert resolve_value("'wn > 2 ? 1 : 0'", env) is None  # 조건식


def test_a_circular_reference_resolves_to_nothing():
    deck = "* t\n.param a='b*2'\n.param b='a*2'\nM1 x y 0 0 nch W=a\n.end\n"

    envs = build_param_envs(deck)

    assert "a" not in envs[None]
    assert "b" not in envs[None]


def test_global_params_resolve_transitively():
    envs = build_param_envs(_fixture_text())

    assert envs[None]["wn"] == 4.0
    assert envs[None]["wp"] == 8.0


def test_a_subckt_default_is_overridden_by_its_instance():
    # CORE는 W=10을 기본값으로 선언하고 Xc1이 W=20으로 오버라이드한다.
    envs = build_param_envs(_fixture_text())

    assert envs["CORE"]["W"] == 20.0


def test_a_subckt_without_an_override_keeps_its_default():
    deck = "* t\n.subckt SUB a b W=10\nM1 a b 0 0 nch W=W\n.ends\nX1 p q SUB\n.end\n"

    envs = build_param_envs(deck)

    assert envs["SUB"]["W"] == 10.0


def test_instances_disagreeing_on_a_parameter_make_it_unresolvable():
    # 값이 진짜로 인스턴스마다 다르고, 이 프로젝트는 정의 단위로 주소지정하므로
    # 단일 정답이 없다.
    deck = (
        "* t\n.subckt SUB a b W=10\nM1 a b 0 0 nch W=W\n.ends\n"
        "X1 p q SUB W=20\nX2 r s SUB W=40\n.end\n"
    )

    envs = build_param_envs(deck)

    assert "W" not in envs["SUB"]


def test_a_nested_subckt_sees_the_global_environment():
    envs = build_param_envs(_fixture_text())

    assert envs["WRAP.DEEP"]["wn"] == 4.0


def test_the_fixture_parses_without_losing_any_block():
    parsed = parse_netlist(_fixture_text())

    assert sorted(parsed.subckts) == ["CORE", "WRAP", "WRAP.DEEP"]
    assert [c.refdes for c in parsed.subckts["WRAP"].components] == ["Xd", "M5"]
    assert [c.refdes for c in parsed.top_components] == ["Xc1", "Xw1"]
```

- [ ] **Step 3: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_params.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'analogcoder.params'`.

- [ ] **Step 4: 구현한다**

`src/analogcoder/params.py`:

```python
import ast
import re

from analogcoder.netlist import parse_netlist, parse_spice_value, strip_inline_comment

_PARAM_DIRECTIVE_RE = re.compile(r"^\s*\.param\b(.*)$", re.IGNORECASE)
_ASSIGN_RE = re.compile(r"(\w+)\s*=\s*('[^']*'|\{[^}]*\}|\S+)")


class _Unresolvable(Exception):
    """평가가 이 모듈이 다루기로 한 범위를 벗어났다."""


_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Pow: lambda a, b: a**b,
}


def _eval(node: ast.AST, env: dict[str, float]) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise _Unresolvable()
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise _Unresolvable()
        return env[node.id]
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            return -_eval(node.operand, env)
        if isinstance(node.op, ast.UAdd):
            return _eval(node.operand, env)
        raise _Unresolvable()
    if isinstance(node, ast.BinOp):
        handler = _BINOPS.get(type(node.op))
        if handler is None:
            raise _Unresolvable()
        try:
            return handler(_eval(node.left, env), _eval(node.right, env))
        except ZeroDivisionError:
            raise _Unresolvable() from None
    raise _Unresolvable()


def resolve_value(raw: str, env: dict[str, float]) -> float | None:
    """raw를 수치로 해소하거나, 이 모듈이 다루기로 한 범위 밖이면 None.

    범위는 의도적으로 좁다: 산술(+ - * / **), 단항 부호, 괄호, SPICE 접미사
    리터럴, 다른 파라미터 참조까지. 함수·조건식·미정의 이름·순환 참조는 전부
    None이다. 조용히 틀린 숫자를 내놓는 것보다 명시적 '모름'이 낫다는
    것이 이 프로젝트에서 세 번 반복된 교훈이다.

    평가는 ast 화이트리스트로 하며 eval을 쓰지 않는다."""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] == "'":
        s = s[1:-1].strip()
    elif s.startswith("{") and s.endswith("}"):
        s = s[1:-1].strip()
    if not s:
        return None
    try:
        return parse_spice_value(s)
    except ValueError:
        pass
    try:
        tree = ast.parse(s, mode="eval")
    except (SyntaxError, ValueError):
        return None
    try:
        return _eval(tree.body, env)
    except _Unresolvable:
        return None
    except RecursionError:
        return None


def _resolve_environment(raw_params: dict[str, str], seed: dict[str, float]) -> dict[str, float]:
    """raw_params를 고정점에 도달할 때까지 반복 해소한다.

    한 번의 통과로는 부족하다: `.param wp='wn*2'`가 `.param wn=4`보다 먼저
    선언될 수 있다. 통과 한 번에 최소 하나는 새로 풀리므로 파라미터 수만큼
    반복하면 충분하고, 그래도 남는 것은 순환이거나 범위 밖이다."""
    env = dict(seed)
    for _ in range(len(raw_params) + 1):
        progressed = False
        for name, raw in raw_params.items():
            if name in env:
                continue
            value = resolve_value(raw, env)
            if value is not None:
                env[name] = value
                progressed = True
        if not progressed:
            break
    return env


def _collect_global_raw_params(text: str) -> dict[str, str]:
    raw: dict[str, str] = {}
    depth = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue
        code, _ = strip_inline_comment(stripped)
        lower = code.lower()
        if lower.startswith(".subckt") or lower.startswith(".macro"):
            depth += 1
            continue
        if lower.startswith(".ends") or lower.startswith(".eom"):
            depth = max(0, depth - 1)
            continue
        if depth:
            continue
        match = _PARAM_DIRECTIVE_RE.match(code)
        if match:
            for name, value in _ASSIGN_RE.findall(match.group(1)):
                raw[name] = value
    return raw


def _instance_overrides(parsed, subckt_name: str) -> dict[str, str] | None:
    """subckt_name의 모든 직접 인스턴스가 합의하는 오버라이드.

    한 파라미터를 인스턴스마다 다르게 주면 그 파라미터는 결과에서 빠진다 -
    값이 진짜로 인스턴스마다 다른데 이 프로젝트는 소자를 서브회로 정의로
    주소지정하므로 단일 정답이 없다."""
    components = list(parsed.top_components)
    for subckt in parsed.subckts.values():
        components.extend(subckt.components)

    seen: dict[str, set[str]] = {}
    found = False
    for component in components:
        if component.value != subckt_name:
            continue
        found = True
        for name, value in component.params.items():
            seen.setdefault(name, set()).add(value)
    if not found:
        return None
    return {name: next(iter(values)) for name, values in seen.items() if len(values) == 1}


def build_param_envs(text: str) -> dict[str | None, dict[str, float]]:
    """스코프 경로(최상위는 None) → 해소된 파라미터 환경.

    우선순위는 낮은 것부터: 전역 .param, 서브회로 .subckt 줄 기본값,
    인스턴스 오버라이드.

    인스턴스 오버라이드는 한 단계만 전파한다. 서브회로가 다른 서브회로 안에서
    인스턴스화되고 그 바깥쪽이 서로 다른 파라미터로 여러 번 인스턴스화되는
    경우까지는 따라가지 않는다 - 전체 트리 전파는 E2가 인스턴스 트리를 만든
    뒤에 가능하다."""
    parsed = parse_netlist(text)
    global_env = _resolve_environment(_collect_global_raw_params(text), {})

    envs: dict[str | None, dict[str, float]] = {None: global_env}
    for path, subckt in parsed.subckts.items():
        raw = dict(subckt.defaults)
        overrides = _instance_overrides(parsed, subckt.name)
        if overrides:
            raw.update(overrides)
        envs[path] = _resolve_environment(raw, global_env)
    return envs
```

- [ ] **Step 5: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_params.py -q`
Expected: 모두 PASS.

`test_instances_disagreeing...`가 실패하면 `_instance_overrides`가 값 문자열을 정규화하고 있지 않은지 확인한다 — `"20"`과 `"20"`은 같고 `"20"`과 `"40"`은 달라야 한다.

Run: `.venv/bin/python -m pytest -q` → 전체 그린(골든 포함).

- [ ] **Step 6: 커밋**

```bash
git add src/analogcoder/params.py tests/unit/test_params.py tests/fixtures/hspice_flavoured.cir
git commit -m "feat: resolve .param declarations into numeric environments

Global .param, subckt defaults and instance overrides resolve in that
priority order, with a deliberately narrow expression subset: arithmetic,
unary sign, parentheses, SPICE-suffixed literals and parameter references.
Evaluation goes through an ast whitelist, never eval.

Everything outside that subset resolves to None rather than a guess -
functions, conditionals, undefined names, circular references, and a
parameter that a subckt's instances override to different values (the
value is genuinely instance-dependent, and this project addresses
components by subckt definition).

Instance overrides propagate one level only; full tree propagation needs
E2's instance tree."
```

---

## Task 5: 에어리어 게이트가 해소된 값을 쓴다

**Files:**
- Modify: `src/analogcoder/netlist.py` (`Component`에 해소 필드, `parse_netlist`가 채움)
- Modify: `src/analogcoder/area_limits.py`
- Test: `tests/unit/test_area_limits.py` (기존 파일에 추가)

**Interfaces:**
- Consumes: Task 4의 `build_param_envs`, `resolve_value`
- Produces: `Component.resolved_params: dict[str, float]`, `Component.resolved_value: float | None`

**순환 임포트 주의.** `params.py`가 `netlist.py`를 임포트하므로 `netlist.py`는 모듈 최상단에서 `params`를 임포트할 수 없다. `parse_netlist`가 해소를 직접 하지 않고, `area_limits.index_baseline_components`가 파싱 후에 채우는 방식으로 푼다 — 해소는 파싱의 부수효과가 아니라 별도 단계다.

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/unit/test_area_limits.py` 끝에 추가:

```python
def test_a_parameterised_width_is_tiered_like_a_literal_one():
    # 회귀: W=30 -> 300은 차단되는데 W='wn*2' -> 'wn*20'은 같은 10x인데도
    # 승인됐다. 리터럴 덱에서는 드문 예외지만 파라미터화가 기본인 HSPICE
    # 덱에서는 모든 소자에 발동해 게이트가 사실상 사라진다.
    deck = (
        "* t\n.option scale=1.0u\n.param wn=20\n"
        "M1 d g 0 0 nch W='wn*2' L=1\n.end\n"
    )
    indexed = index_baseline_components(deck)

    ok, feedback = check_area_growth(
        indexed, [{"refdes": "M1", "param": "W", "old_value": "'wn*2'", "new_value": "400"}]
    )

    assert ok is False
    assert "10.00x" in feedback


def test_an_unresolvable_value_still_falls_back_to_not_blocking():
    # 의도된 폴백이다. 값을 확정할 수 없으면 면적 영향을 판단할 수 없고,
    # 판단할 수 없는 것을 막지는 않는다.
    deck = (
        "* t\n.option scale=1.0u\n"
        "M1 d g 0 0 nch W='sqrt(nope)' L=1\n.end\n"
    )
    indexed = index_baseline_components(deck)

    ok, _ = check_area_growth(
        indexed, [{"refdes": "M1", "param": "W", "old_value": "x", "new_value": "300"}]
    )

    assert ok is True


def test_the_resolved_tier_baseline_uses_the_parameterised_geometry():
    # 티어 선택도 해소된 값을 봐야 한다. M1은 일반(비-sky130) 소자이므로
    # TRANSISTOR_TIERS를 타고, wn*2 = 40um는 30um 초과 80um 이하 구간이라
    # 2.0x 티어다. 해소가 안 되면 티어 베이스라인 자체가 None이 되어 게이트가
    # 아예 판정하지 않는다.
    deck = (
        "* t\n.option scale=1.0u\n.param wn=20\n"
        "M1 d g 0 0 nch W='wn*2' L=1\n.end\n"
    )
    indexed = index_baseline_components(deck)

    ok, feedback = check_area_growth(
        indexed, [{"refdes": "M1", "param": "W", "old_value": "'wn*2'", "new_value": "100"}]
    )

    assert ok is False
    assert "2.0x" in feedback
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_area_limits.py -q`
Expected: 세 개 중 첫째와 셋째가 FAIL(게이트가 승인해버림), 둘째는 이미 PASS(기존 폴백).

- [ ] **Step 3: 구현한다**

`netlist.py`의 `Component`에 필드 추가:

```python
@dataclass
class Component:
    refdes: str
    ctype: str
    nodes: list[str]
    value: str | None
    params: dict[str, str] = field(default_factory=dict)
    raw_line: str = ""
    scope: str | None = None
    geometry_scale: float = 1.0
    resolved_params: dict[str, float] = field(default_factory=dict)
    resolved_value: float | None = None
```

`area_limits.py` 최상단에 임포트 추가:

```python
from analogcoder.params import build_param_envs, resolve_value
```

`index_baseline_components`에서 파싱 직후 해소값을 채운다:

```python
    parsed = parse_netlist(netlist_text)
    envs = build_param_envs(netlist_text)

    def _annotate(component: Component) -> None:
        env = envs.get(component.scope, envs[None])
        for name, raw in component.params.items():
            value = resolve_value(raw, env)
            if value is not None:
                component.resolved_params[name] = value
        if component.value is not None:
            component.resolved_value = resolve_value(component.value, env)

    for component in parsed.top_components:
        _annotate(component)
    for subckt in parsed.subckts.values():
        for component in subckt.components:
            _annotate(component)
```

(이 블록은 기존 `plain_counts` 루프 **앞에** 넣는다.)

`_baseline_value_for`를 수치 반환으로 바꾼다:

```python
def _baseline_value_for(component: Component, param: str) -> float | None:
    """해소된 수치. 확정할 수 없으면 None.

    원본 토큰이 아니라 해소값을 돌려주는 것이 핵심이다. W='wn*2'를 문자열로
    읽으면 parse_spice_value가 ValueError를 내고, check_area_growth가 그것을
    '판단 불가, 막지 않음'으로 처리해 파라미터화된 덱 전체에서 게이트가
    사라진다."""
    if param == "value":
        return component.resolved_value
    return component.resolved_params.get(param)
```

`check_area_growth`의 베이스라인 처리에서 문자열 파싱을 걷어낸다:

```python
        combined_ratio = 1.0
        for change in changes:
            baseline_value = _baseline_value_for(component, change["param"])
            if baseline_value is None or baseline_value <= 0:
                continue
            new_value = resolve_value(change["new_value"], {})
            if new_value is None:
                continue
            combined_ratio *= new_value / baseline_value
```

`_tier_baseline_value`도 해소값을 쓰도록:

```python
    if component.ctype == "X":
        param = _SKY130_GEOMETRY_PARAM.get(ctype)
        if param is None:
            return None
        raw = component.resolved_params.get(param)
        if raw is None:
            return None
        scale = 1.0 if ctype == "Q" else component.geometry_scale
        return raw * scale
    if ctype == "M":
        w = component.resolved_params.get("W")
        return w * component.geometry_scale if w is not None else None
    return component.resolved_value
```

`_tier_baseline_value`가 더 이상 `ValueError`를 던지지 않으므로 `check_area_growth`의 `try/except ValueError` 감싸기는 남겨두되(방어), 주석에 "이제 해소 단계에서 걸러지므로 도달하지 않는다"고 적는다.

**CLAUDE.md의 기존 함정 항목을 보존할 것:** "`param="value"`가 비수치 위치 토큰에 잘못 적용되어도 막지 않는다"는 동작은 그대로 유지된다 — `resolved_value`가 `None`이 되어 같은 폴백을 탄다.

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_area_limits.py tests/unit/test_netlist_golden.py -q`
Expected: 모두 PASS.

Run: `.venv/bin/python -m pytest -q` → 전체 그린. 기존 에어리어 테스트가 깨지면 해소값이 원본 토큰과 같은 수치를 주는지 확인한다(리터럴 덱에서는 같아야 한다).

- [ ] **Step 5: 커밋**

```bash
git add src/analogcoder/netlist.py src/analogcoder/area_limits.py tests/unit/test_area_limits.py
git commit -m "fix: tier a parameterised device value like a literal one

W=30 -> 300 was rejected at 10x while W='wn*2' -> 'wn*20' was approved at
the same 10x, because parse_spice_value raised on the expression and
check_area_growth treats an unparseable baseline as 'cannot judge, do not
block'. That guard is correct and stays; on a literal deck it fires rarely,
but on a parameterised deck - the norm in HSPICE - it fired on every device,
so the gate was effectively absent.

Component now carries resolved numerics alongside the raw tokens.
apply_changes keeps editing the raw text; the gate reads the numbers.
Resolution is a separate step in index_baseline_components rather than a
side effect of parse_netlist, which also keeps netlist.py free of an import
cycle with params.py."
```

---

## Task 6: 중첩 경로를 튜너가 쓸 수 있게 한다

**Files:**
- Modify: `src/analogcoder/schemas.py`
- Modify: `src/analogcoder/agents/tuner.py`
- Test: `tests/unit/test_schemas.py` (없으면 생성), `tests/unit/test_tuner_agent.py`

**Interfaces:**
- Consumes: Task 3의 경로 주소지정 규칙
- Produces: 없음(최종 Task)

**왜 필요한가:** `TUNER_SCHEMA`의 refdes 정규식이 점을 **정확히 하나만** 허용한다. 중첩 스코프를 추적해도 `OUTER.INNER.M1`은 게이트에 닿기 전에 스키마 검증에서 거부된다.

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/unit/test_schemas.py`:

```python
import re

from analogcoder.schemas import TUNER_SCHEMA

REFDES_PATTERN = TUNER_SCHEMA["properties"]["proposed_changes"]["items"]["properties"][
    "refdes"
]["pattern"]


def test_the_refdes_pattern_accepts_any_nesting_depth():
    regex = re.compile(REFDES_PATTERN)

    assert regex.match("Rf")
    assert regex.match("BUF_P.X6")
    assert regex.match("OUTER.INNER.M1")
    assert regex.match("A.B.C.D.M1")


def test_the_refdes_pattern_still_rejects_malformed_names():
    regex = re.compile(REFDES_PATTERN)

    assert not regex.match("")
    assert not regex.match(".M1")
    assert not regex.match("M1.")
    assert not regex.match("A..M1")
    assert not regex.match("1M.X")
    assert not regex.match("A B")
```

`tests/unit/test_tuner_agent.py`에 추가:

```python
def test_the_tuner_prompt_explains_full_path_addressing():
    from analogcoder.agents.tuner import TUNER_SYSTEM_PROMPT

    assert "OUTER.INNER" in TUNER_SYSTEM_PROMPT
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_schemas.py tests/unit/test_tuner_agent.py -q`
Expected: `test_the_refdes_pattern_accepts_any_nesting_depth`가 `OUTER.INNER.M1`에서 FAIL, 튜너 프롬프트 테스트도 FAIL.

- [ ] **Step 3: 구현한다**

`schemas.py`의 `TUNER_SCHEMA`에서 refdes 패턴을 교체:

```python
                    "refdes": {
                        "type": "string",
                        "pattern": r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$",
                    },
```

(`?`가 `*`로 바뀐 것이 전부다 — 점으로 구분된 식별자 하나 이상.)

`agents/tuner.py`의 `TUNER_SYSTEM_PROMPT`에서 refdes 문단을 교체:

```python
refdes MUST identify exactly one component. When the component sits inside a
.subckt, qualify it with the subckt's full path as "<PATH>.<refdes>" (e.g.
"BUF_N.Xcc" for the Xcc inside ".subckt BUF_N ...", or "OUTER.INNER.M1" for
an M1 inside a .subckt INNER nested within .subckt OUTER). The path must be
complete: a partial path such as "INNER.M1" for a component in OUTER.INNER
is rejected. An unqualified refdes that appears in more than one scope is
also rejected as ambiguous. Note the scope is the subckt definition:
changing it changes every instance of that subckt.
```

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_schemas.py tests/unit/test_tuner_agent.py -q`
Expected: 모두 PASS.

Run: `.venv/bin/python -m pytest -q` → 전체 그린.

- [ ] **Step 5: CLAUDE.md를 갱신한다**

"Known limitations / gotchas" 절의 중첩 서브회로 항목을 교체한다. 기존 문장:

> **`netlist.py` tracks subckt scope.** ... Nested subckts are still not scope-tracked (`Component.scope`/`_line_scopes` are single-level) — a refdes inside a subckt nested within another subckt is not distinguishable from one at the outer subckt's level.

마지막 문장을 다음으로 바꾼다:

> Nested subckts **are** scope-tracked: `Component.scope` and the `ParsedNetlist.subckts` key are dotted paths (`OUTER.INNER`), and a qualified refdes must match a path exactly — a partial path like `INNER.M1` is rejected rather than guessed at.

같은 절에 새 항목 두 개를 추가한다:

> - **An inline `$` or `;` comment is stripped before parsing, and re-appended
>   by `apply_changes`.** Leaving it in used to swallow the model name into the
>   node list, and made `param="value"` replace the comment's last word instead
>   of the device value.
> - **A parameterised value is resolved before the area gate reads it**
>   (`params.py`). Without this, `W='wn*2'` was unparseable, and
>   `check_area_growth`'s "cannot judge, do not block" fallback fired on every
>   device — so the gate was absent on any parameterised deck. The resolver's
>   subset is deliberately narrow (arithmetic only); anything else resolves to
>   `None` and takes that same fallback, which is now reached only when it is
>   genuinely true.

- [ ] **Step 6: 커밋**

```bash
git add src/analogcoder/schemas.py src/analogcoder/agents/tuner.py tests/unit/test_schemas.py tests/unit/test_tuner_agent.py CLAUDE.md
git commit -m "feat: let the tuner address a component at any nesting depth

TUNER_SCHEMA's refdes pattern permitted at most one dot, so
'OUTER.INNER.M1' failed schema validation before any gate could see it -
which would have made the nested scope tracking unreachable from the tuner.
The prompt now explains full-path addressing and that a partial path is
rejected."
```

---

## Self-review 결과

**스펙 커버리지.** 설계 문서의 각 요구사항과 Task 대응:

| 스펙 항목 | Task |
|---|---|
| 중첩 스코프 전체 추적, 경로 키 | 3 |
| `split_scoped_refdes` 마지막 점 분리 | 3 (코드는 이미 맞음, 독스트링만) |
| 정확 일치 규칙, 부분 한정 거부 | 3 |
| `TUNER_SCHEMA` 정규식 완화 | 6 |
| `$`/`;` 인라인 주석 | 2 |
| `.macro`/`.eom`, `.inc` | 2 |
| `.param` 3단 우선순위 해석 | 4 |
| 경계 명시된 표현식 평가(ast 화이트리스트) | 4 |
| 해소 불가 5종(함수·미정의·순환·인스턴스 불일치·접미사) | 4 |
| `Component`가 원본과 해소값 둘 다 | 5 |
| 에어리어 게이트가 해소값 사용 | 5 |
| 소자 토큰을 리터럴로 교체 | 기존 `apply_changes` 동작이 이미 그러함 — Task 2가 주석 보존만 추가 |
| 골든 스냅샷을 변경 전에 생성 | 1 |
| 합성 HSPICE 덱 | 4 |
| 세 결함 회귀 테스트 | 2(주석), 3(중첩), 5(파라미터화) |

**스펙에 없었으나 계획에 추가한 것 두 가지.** 계획을 쓰며 현재 코드를 다시 읽다 발견했다.

- `apply_topology_swap`이 첫 `.ends`를 본문의 끝으로 보므로 중첩 서브회로가 있으면 본문을 잘라먹는다. Task 3의 중첩 결함과 같은 원인이라 같은 Task에 넣었다.
- `.subckt SUB a b W=10`의 `W=10`이 포트로 세어진다. `.param` 해석에 서브회로 기본값이 필요하므로 Task 3에서 함께 고친다.

**플레이스홀더 스캔.** 없음. 모든 코드 단계에 실제 코드가 있다.

**타입 일관성.** `strip_inline_comment`(Task 2) → `_line_scopes`/`apply_topology_swap`(Task 3)/`_collect_global_raw_params`(Task 4)에서 동일 시그니처로 사용. `Subckt.path`/`defaults`(Task 3) → `build_param_envs`(Task 4)에서 사용. `resolve_value`/`build_param_envs`(Task 4) → `index_baseline_components`(Task 5)에서 사용. `Component.resolved_params`/`resolved_value`(Task 5)를 `_baseline_value_for`/`_tier_baseline_value`가 사용. 순환 임포트는 Task 5에서 명시적으로 다뤘다.
