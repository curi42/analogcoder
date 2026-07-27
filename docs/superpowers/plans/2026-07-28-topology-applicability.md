# 토폴로지 스왑 적용 가능성 (F1) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 토폴로지 스왑을 다중 블록 덱에서 쓸 수 있게 만들고, 어떤 블록에 어떤 항목이 왜 적용 가능/불가능한지를 결정론적으로 판정해 로그로 남긴다.

**Architecture:** 호환성 판정을 새 모듈 `topology_match.py`에 두고, 오케스트레이터가 그것을 **후보 생성기**로 쓴다 — 에이전트에게는 이미 적용 가능한 `(블록, 토폴로지)` 쌍만 제시된다. 판정 규칙은 포트/모델/스케일 세 가지이며 전부 파스된 사실이다.

**Tech Stack:** Python 3, dataclasses, pytest, ngspice (실 시뮬레이션 테스트 1개)

**설계 문서:** `docs/superpowers/specs/2026-07-28-topology-applicability-design.md` (커밋 `9c12936`). 측정 데이터는 전부 거기 있다 — 다시 측정하지 말 것.

## Global Constraints

- **테스트가 요구사항이고, 브리프의 코드 스케치는 제안일 뿐이다.** 스케치가 산문 규칙과 어긋나면 **산문이 이긴다**. 이 저장소의 지난 두 계획에서 8개 브리프 중 6~7개가 결함 있는 스케치를 담았다.
- **새 테스트마다 "이 테스트는 어떤 변형(mutation)을 잡는가"를 답할 것.** 답이 없으면 그 테스트는 아무것도 검증하지 않는다. 한 브랜치에서만 이런 테스트가 네 번 나왔다.
- **추측 금지.** 이름으로 의미를 알아보는 규칙(`vdd`니까 전원 레일)은 이 저장소가 금지한다. 판정은 파스된 사실(refdes 접두, 스코프, 헤더 포트, 덱에 등장하는 모델 이름)에만 근거한다.
- **게이트가 아무것도 하지 않은 경우를 반드시 로그로 남긴다.** 조건부 로깅 금지 — "검사했고 문제없음"과 "검사가 사라짐"이 로그에서 구별되어야 한다.
- 기존 `two_stage_opamp` 경로의 동작은 바뀌지 않아야 한다.
- 파이썬 실행은 전부 `.venv/bin/python`. 테스트는 `.venv/bin/python -m pytest`.
- 커밋 메시지는 한글, 마지막 줄에 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## 파일 구조

| 파일 | 책임 |
|---|---|
| `src/analogcoder/topologies.py` (수정) | 라이브러리 **데이터**만. `Topology`에 `ports`/`assumes_scale` 추가, 신규 항목 2개 |
| `src/analogcoder/topology_match.py` (신규) | 호환성 **판정**. 틀릴 수 있는 쪽이라 데이터와 분리 (`patterns.py`가 `structure.py`와 분리된 것과 같은 이유) |
| `src/analogcoder/netlist.py` (수정) | `apply_topology_swap`이 점 표기 경로를 받는다 |
| `src/analogcoder/schemas.py` (수정) | `TOPOLOGY_SCHEMA`에 `block_path` (선택 필드) |
| `src/analogcoder/agents/tuner.py` (수정) | 후보 쌍을 제시하는 프롬프트 |
| `src/analogcoder/orchestrator.py` (수정) | 가용성 판단 교체, `tried` 튜플화, `block_path` 해소, 로깅 |
| `benchmarks/bandgap/netlist_seed_topology.cir` (신규) | 시드 덱 |
| `benchmarks/bandgap/spec_seed_topology.yaml` (신규) | 시드 스펙 |

---

### Task 1: 라이브러리 표면과 신규 항목

**Files:**
- Modify: `src/analogcoder/topologies.py`
- Test: `tests/unit/test_topologies.py`

**Interfaces:**
- Produces: `Topology(id, description, subckt_body, addresses, ports: list[str], assumes_scale: float)`; 라이브러리 키 `folded_cascode_nmos_in_cs`, `folded_cascode_pmos_in_cs` 추가

**배경:** 지금 라이브러리 본문은 5포트 인터페이스와 sky130 모델과 µm 스케일을 **가정**할 뿐 선언하지 않는다. Task 3의 판정이 읽을 수 있도록 선언으로 바꾼다.

**계획 오류 정정 #1 (중요, 스펙 본문보다 이것이 맞다):** 설계 문서는 "라이브러리 테스트가 본문에서 파생한 포트 참조 집합과 선언을 대조한다"고 썼는데, 그 대조는 **한 방향만 판정 가능하다**. 본문은 포트를 이름으로 참조하고, 선언되지 않은 이름은 그냥 내부 노드가 되므로 "본문이 필요로 하는데 선언 안 된 포트"는 구조적으로 내부 노드와 구별할 수 없다. 그래서 불변식은 둘로 나눈다:

- (a) **선언된 모든 포트가 본문에서 최소 한 번 참조된다** — 구조적으로 판정 가능. 선언에 없는 포트를 넣는 실수를 잡는다.
- (b) **각 항목의 `ports`가 그 항목이 유래한 벤치마크 덱 블록의 `.subckt` 헤더와 정확히 같다** — 핀 고정. 이것이 (a)의 역방향을 실질적으로 닫는다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/unit/test_topologies.py`에 추가:

```python
import pytest
from analogcoder.netlist import parse_netlist
from analogcoder.topologies import TOPOLOGY_LIBRARY


def _wrapped(topology):
    header = ".subckt TMP " + " ".join(topology.ports)
    return parse_netlist(f"{header}\n{topology.subckt_body}.ends TMP\n")


@pytest.mark.parametrize("topology_id", sorted(TOPOLOGY_LIBRARY))
def test_every_declared_port_is_referenced_by_the_body(topology_id):
    topology = TOPOLOGY_LIBRARY[topology_id]
    parsed = _wrapped(topology)
    referenced = {n for c in parsed.subckts["TMP"].components for n in c.nodes}
    missing = set(topology.ports) - referenced
    assert missing == set(), f"{topology_id} declares unreferenced ports: {sorted(missing)}"


@pytest.mark.parametrize("topology_id", sorted(TOPOLOGY_LIBRARY))
def test_assumes_scale_is_positive(topology_id):
    assert TOPOLOGY_LIBRARY[topology_id].assumes_scale > 0


@pytest.mark.parametrize(
    "topology_id,deck,block",
    [
        ("miller_basic", "benchmarks/two_stage_opamp/netlist.cir", "OPAMP2STAGE"),
        ("miller_nulling_resistor", "benchmarks/two_stage_opamp/netlist.cir", "OPAMP2STAGE"),
        ("folded_cascode_nmos_in_cs", "benchmarks/bandgap/netlist.cir", "TRIMAMP"),
        ("folded_cascode_pmos_in_cs", "benchmarks/bandgap/netlist.cir", "BUF_P"),
    ],
)
def test_declared_ports_match_the_source_block_header(topology_id, deck, block):
    from pathlib import Path
    parsed = parse_netlist(Path(deck).read_text())
    assert TOPOLOGY_LIBRARY[topology_id].ports == parsed.subckts[block].ports
```

**잡는 변형:** 첫 테스트는 `ports`에 존재하지 않는 포트를 넣는 변형을 잡는다. 셋째 테스트는 포트 목록의 **순서나 원소**를 바꾸는 변형을 잡는다 — 이것이 없으면 `ports`를 아무렇게나 써도 통과한다.

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_topologies.py -q`
Expected: FAIL — `Topology`에 `ports` 인자가 없다 (`TypeError`).

- [ ] **Step 3: `Topology`에 필드를 추가한다**

```python
@dataclass(frozen=True)
class Topology:
    id: str
    description: str
    subckt_body: str
    addresses: list[str]
    ports: list[str]          # 이 본문이 요구하는 .subckt 포트, 소스 블록 헤더 순서 그대로
    assumes_scale: float      # 이 본문의 기하 수치가 전제하는 .option scale (m 단위)
```

기존 두 항목에 추가:

```python
ports=["vinp", "vinn", "vout", "vdd", "vss"],
assumes_scale=1e-6,
```

- [ ] **Step 4: 신규 항목 둘을 추가한다**

본문은 `benchmarks/bandgap/netlist.cir`의 `TRIMAMP`/`BUF_P` 정의 **그대로** 옮긴다. 기억으로 다시 쓰지 말고 파일에서 복사할 것 — 두 본문 모두 45코너를 통과한 검증된 값이다. 주석 줄은 옮기지 않는다.

```python
"folded_cascode_nmos_in_cs": Topology(
    id="folded_cascode_nmos_in_cs",
    description=(
        "NMOS-input folded cascode first stage with a PMOS common-source output "
        "stage, Miller-compensated with a nulling resistor. The 9-port bias "
        "interface (nbias/ncas/pbias/pcas) is supplied externally. Use when the "
        "input common mode sits comfortably above an NMOS pair's Vgs."
    ),
    addresses=[],
    ports=["vinp", "vinn", "vout", "vdd", "vss", "nbias", "ncas", "pbias", "pcas"],
    assumes_scale=1e-6,
    subckt_body="""\
<benchmarks/bandgap/netlist.cir 의 .subckt TRIMAMP 본문 그대로>
""",
),
"folded_cascode_pmos_in_cs": Topology(
    id="folded_cascode_pmos_in_cs",
    description=(
        "PMOS-input COMPLEMENTARY folded cascode (NMOS folding sinks, NMOS "
        "cascodes, cascoded PMOS mirror on top) with an NMOS common-source "
        "output stage. Same 9-port bias interface as the NMOS-input variant. "
        "Use when the input common mode is too low for an NMOS pair: measured "
        "on this deck, an NMOS-input fold buffering a 0.5V node leaves only "
        "10.1mV across its tail current source, and widening the input pair "
        "cannot recover it (Vgs_n has a Vth floor)."
    ),
    addresses=["buf0_loop_gain"],
    ports=["vinp", "vinn", "vout", "vdd", "vss", "nbias", "ncas", "pbias", "pcas"],
    assumes_scale=1e-6,
    subckt_body="""\
<benchmarks/bandgap/netlist.cir 의 .subckt BUF_P 본문 그대로>
""",
),
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/unit/test_topologies.py -q`
Expected: PASS. 라이브러리 항목 수를 고정하는 기존 테스트가 있으면 2 → 4로 갱신한다.

- [ ] **Step 6: 전체 스위트 회귀 확인**

Run: `.venv/bin/python -m pytest -m "not slow" -q`
Expected: PASS (`Topology` 생성 지점이 다른 곳에 있으면 그것도 고친다).

- [ ] **Step 7: 커밋**

```bash
git add -A && git commit -m "feat: 토폴로지 항목이 포트와 스케일을 선언한다"
```

---

### Task 2: `apply_topology_swap` 점 표기 경로

**Files:**
- Modify: `src/analogcoder/netlist.py` (`apply_topology_swap`)
- Test: `tests/unit/test_netlist.py`

**Interfaces:**
- Consumes: 없음
- Produces: `apply_topology_swap(text: str, block_path: str, new_body: str) -> str` — 두 번째 인자가 이제 **점 표기 경로**를 받는다 (`"BUF_P"` 또는 `"OUTER.INNER"`)

**배경:** 현재 구현은 맨 이름으로 **첫 매치**를 잡는다. 중첩 정의가 있는 덱(생산 덱은 다단 중첩(수십 블록))에서 잘못된 블록을 교체할 수 있다. `netlist.py`는 이미 점 표기 스코프 모델을 갖고 있다(`ParsedNetlist.subckts`의 키가 점 표기 경로) — 여기에 맞춘다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
NESTED = """* t
.subckt OUTER a b
.subckt INNER a b
R1 a b 1k
.ends INNER
Xi a b INNER
.ends OUTER
.subckt INNER a b
R9 a b 9k
.ends INNER
.end
"""


def test_a_dotted_path_targets_the_nested_definition_not_the_top_level_one():
    out = apply_topology_swap(NESTED, "OUTER.INNER", "R2 a b 2k\n")
    assert "R2 a b 2k" in out
    assert "R9 a b 9k" in out          # 최상위 동명 정의는 건드리지 않는다
    assert "R1 a b 1k" not in out


def test_a_bare_name_targets_the_top_level_definition():
    out = apply_topology_swap(NESTED, "INNER", "R3 a b 3k\n")
    assert "R1 a b 1k" in out          # 중첩된 쪽은 그대로
    assert "R9 a b 9k" not in out
    assert "R3 a b 3k" in out


def test_a_partial_path_is_rejected_rather_than_guessed_at():
    with pytest.raises(ValueError):
        apply_topology_swap(NESTED, "INNER.DEEPER", "R4 a b 4k\n")


def test_an_unknown_path_raises():
    with pytest.raises(ValueError):
        apply_topology_swap(NESTED, "NOPE", "R5 a b 5k\n")


def test_the_header_and_footer_lines_are_preserved_verbatim():
    out = apply_topology_swap(NESTED, "OUTER.INNER", "R2 a b 2k\n")
    assert ".subckt INNER a b" in out
    assert ".ends INNER" in out
```

**잡는 변형:** 첫 두 테스트가 "첫 매치를 잡는" 현재 구현을 잡는다 — 현재 구현으로는 `"INNER"`가 중첩된 쪽(`R1`)을 교체하므로 둘 다 실패한다.

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_netlist.py -k topology -q`
Expected: FAIL

- [ ] **Step 3: 구현한다**

기존 구현은 이미 `depth`로 중첩을 세고 있다. 여기에 **스코프 경로 추적**을 더한다: `.subckt` 헤더를 만날 때마다 이름을 스택에 push하고 `.ends`에서 pop, 현재 경로를 `".".join(stack)`으로 만들어 `block_path`와 **정확히** 비교한다. 매치한 지점의 시작 줄과, 그 정의가 닫히는 `.ends` 줄 사이를 교체한다.

산문 규칙(스케치보다 우선):
- 경로는 **정확히** 일치해야 한다. 부분 경로(`"INNER.DEEPER"`)는 추측하지 않고 `ValueError`.
- 같은 경로가 두 번 나오는 덱은 이미 `parse_netlist` 층의 문제이므로 여기서 새로 판정하지 않는다. **첫 정확 매치**를 쓴다.
- 주석 줄과 인라인 주석 처리, `split_tokens` 사용은 기존 구현 그대로 유지한다 — `str.split()`로 되돌리지 말 것.

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/unit/test_netlist.py -q`
Expected: PASS

- [ ] **Step 5: 회귀 확인 후 커밋**

Run: `.venv/bin/python -m pytest -m "not slow" -q`

```bash
git add -A && git commit -m "fix: apply_topology_swap이 점 표기 경로를 정확히 지목한다"
```

---

### Task 3: `topology_match.compatible_swaps`

**Files:**
- Create: `src/analogcoder/topology_match.py`
- Test: `tests/unit/test_topology_match.py`

**Interfaces:**
- Consumes: `Topology`(Task 1의 `ports`/`assumes_scale`), `netlist.parse_netlist`, `netlist.netlist_scale`, `netlist.parse_spice_value`
- Produces:
  ```python
  @dataclass(frozen=True)
  class SwapCandidate:
      block_path: str
      topology_id: str

  @dataclass(frozen=True)
  class SwapRejection:
      block_path: str
      topology_id: str
      reason: str      # "ports" | "models" | "scale" | "missing_in_testbench"
      detail: str

  def compatible_swaps(
      netlist_texts: dict[str, str],
      library: dict[str, Topology],
      tried: set[tuple[str, str]],
  ) -> tuple[list[SwapCandidate], list[SwapRejection]]:
  ```

**판정 규칙 (산문이 구속력을 갖는다):**

1. **포트** — `set(topology.ports) == set(parsed.subckts[path].ports)`. **양방향 집합 동등**이며, 순서는 보지 않는다. SPICE 본문은 포트를 이름으로 참조하므로 순서는 사실이 아니다. 한 방향만 보면 9포트 대상에 5포트 본문이 통과해 바이어스 포트 4개가 조용히 뜬 노드가 된다.
2. **모델** — `{본문이 쓰는 모델 이름} ⊆ {대상 덱에 등장하는 모델 이름}`. 역방향은 요구하지 않는다. `.include`를 따라가지 않아도 판정 가능한 이유: 덱이 이미 그 모델을 인스턴스화한다면 그것을 제공하는 include가 존재한다.
3. **스케일** — `topology.assumes_scale == netlist_scale(deck_text)`.
4. **모든 테스트벤치에서 판정한다.** 후보는 **그 블록을 정의하는 모든 덱**에서 호환일 때만 후보다. 어떤 덱이 그 블록을 정의하지 않으면 `missing_in_testbench`로 기각한다 — `push_netlist_version`이 원자적이어야 하므로 일부 덱만 스왑된 상태를 만들 수 없다.
5. `tried`에 든 `(block_path, topology_id)` 쌍은 후보에도 기각 목록에도 넣지 않는다(이미 시도한 것은 판정 대상이 아니다).
6. 반환 순서는 **결정론적**이어야 한다 — `block_path`, 그다음 `topology_id`로 정렬.

**모델 이름 추출:** 컴포넌트의 위치 값(`Component.value`)이 **숫자로 파싱되지 않을 때** 그것이 모델/서브회로 이름이다. `parse_spice_value`를 try/except로 감싸 판정한다 (`structure.py`의 `_is_numeric_value`와 같은 규칙이지만 그것은 private이므로 재사용하지 말고 이 모듈에 두거나 공용화한다). 토폴로지 본문은 `.subckt TMP <topology.ports>`로 감싸 파싱한다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
import pytest
from analogcoder.topologies import Topology
from analogcoder.topology_match import compatible_swaps, SwapCandidate

FIVE_PORT = Topology(
    id="five_port", description="d", addresses=[],
    ports=["vinp", "vinn", "vout", "vdd", "vss"], assumes_scale=1e-6,
    subckt_body="M1 vout vinp vdd vss NMOSG W=2 L=1\nM2 vout vinn vss vss NMOSG W=2 L=1\n",
)
NINE_PORT = Topology(
    id="nine_port", description="d", addresses=[],
    ports=["vinp", "vinn", "vout", "vdd", "vss", "nbias", "ncas", "pbias", "pcas"],
    assumes_scale=1e-6,
    subckt_body=(
        "M1 vout vinp vdd vss NMOSG W=2 L=1\n"
        "M2 vout vinn vss vss NMOSG W=2 L=1\n"
        "M3 nbias ncas pbias pcas NMOSG W=2 L=1\n"
    ),
)

DECK_9 = """* t
.option scale=1.0u
.subckt AMP vinp vinn vout vdd vss nbias ncas pbias pcas
M1 vout vinp vdd vss NMOSG W=2 L=1
.ends AMP
.end
"""
DECK_5 = """* t
.option scale=1.0u
.subckt AMP vinp vinn vout vdd vss
M1 vout vinp vdd vss NMOSG W=2 L=1
.ends AMP
.end
"""

LIB = {"five_port": FIVE_PORT, "nine_port": NINE_PORT}


def _reasons(rejections, topology_id):
    return {r.reason for r in rejections if r.topology_id == topology_id}


def test_a_five_port_topology_is_rejected_for_a_nine_port_block():
    cands, rej = compatible_swaps({"tb": DECK_9}, LIB, set())
    assert cands == [SwapCandidate(block_path="AMP", topology_id="nine_port")]
    assert _reasons(rej, "five_port") == {"ports"}


def test_a_nine_port_topology_is_rejected_for_a_five_port_block():
    """양방향 확인 - 한 방향만 보는 구현은 여기서 걸린다."""
    cands, rej = compatible_swaps({"tb": DECK_5}, LIB, set())
    assert cands == [SwapCandidate(block_path="AMP", topology_id="five_port")]
    assert _reasons(rej, "nine_port") == {"ports"}


def test_a_model_the_deck_never_instantiates_is_rejected():
    other = Topology(
        id="other", description="d", addresses=[],
        ports=["vinp", "vinn", "vout", "vdd", "vss"], assumes_scale=1e-6,
        subckt_body="M1 vout vinp vdd vss PMOS_NOT_IN_DECK W=2 L=1\n"
                    "M2 vout vinn vss vss PMOS_NOT_IN_DECK W=2 L=1\n",
    )
    cands, rej = compatible_swaps({"tb": DECK_5}, {"other": other}, set())
    assert cands == []
    assert _reasons(rej, "other") == {"models"}


def test_a_scale_mismatch_is_rejected():
    deck = DECK_5.replace(".option scale=1.0u", ".option scale=1.0n")
    cands, rej = compatible_swaps({"tb": deck}, {"five_port": FIVE_PORT}, set())
    assert cands == []
    assert _reasons(rej, "five_port") == {"scale"}


def test_a_block_missing_from_one_testbench_is_not_a_candidate():
    other_deck = DECK_5.replace("AMP", "OTHER")
    cands, rej = compatible_swaps({"a": DECK_5, "b": other_deck}, {"five_port": FIVE_PORT}, set())
    assert cands == []
    assert _reasons(rej, "five_port") == {"missing_in_testbench"}


def test_a_tried_pair_is_dropped_but_its_siblings_survive():
    two_blocks = DECK_9 + DECK_9.replace("AMP", "AMP2").replace("* t\n.option scale=1.0u\n", "")
    cands, _ = compatible_swaps({"tb": two_blocks}, LIB, {("AMP", "nine_port")})
    assert cands == [SwapCandidate(block_path="AMP2", topology_id="nine_port")]


def test_the_candidate_order_is_deterministic():
    two_blocks = DECK_9 + DECK_9.replace("AMP", "AMP2").replace("* t\n.option scale=1.0u\n", "")
    first, _ = compatible_swaps({"tb": two_blocks}, LIB, set())
    second, _ = compatible_swaps({"tb": two_blocks}, LIB, set())
    assert first == second == sorted(first, key=lambda c: (c.block_path, c.topology_id))
```

**잡는 변형:** 두 번째 테스트가 포트 규칙을 부분집합 한 방향으로 바꾸는 변형을 잡는다. 다섯 번째가 canonical 덱만 읽는 변형을 잡는다. 여섯 번째가 `tried`를 `set[str]`로 되돌리는 변형을 잡는다.

**주의:** 위 픽스처의 덱 문자열 조립(`DECK_9 + DECK_9.replace(...)`)이 유효한 SPICE가 아니면 **테스트 픽스처를 고치되 단언의 의미는 유지할 것**. 두 블록을 가진 덱을 만드는 것이 목적이다.

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/unit/test_topology_match.py -q`
Expected: FAIL — 모듈 없음

- [ ] **Step 3: 구현한다**

산문 규칙 1~6을 따른다. 스케치:

```python
def _model_names(components) -> set[str]:
    names = set()
    for c in components:
        if c.value is None:
            continue
        try:
            parse_spice_value(c.value)
        except ValueError:
            names.add(c.value)
    return names
```

덱 쪽 모델 집합은 **모든 스코프**(최상위 + 모든 서브회로)의 컴포넌트에서 모은다.

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/unit/test_topology_match.py -q`

- [ ] **Step 5: 실제 벤치마크에 대한 확인 테스트를 추가한다**

이 모듈이 실제 덱에서 무엇을 내는지 고정한다 — 합성 픽스처만으로는 F1이 목적을 달성했는지 알 수 없다:

```python
def test_the_bandgap_deck_offers_swaps_that_the_old_rule_made_impossible():
    from pathlib import Path
    from analogcoder.topologies import TOPOLOGY_LIBRARY
    text = Path("benchmarks/bandgap/netlist.cir").read_text()
    cands, rej = compatible_swaps({"dc": text}, TOPOLOGY_LIBRARY, set())
    paths = {c.block_path for c in cands}
    assert {"ERRAMP", "TRIMAMP", "BUF_N", "BUF_P"} <= paths
    assert all(c.topology_id.startswith("folded_cascode_") for c in cands)
    # 5포트 항목은 전부 포트 사유로 기각되어야 한다
    assert {r.reason for r in rej if r.topology_id == "miller_basic"} == {"ports"}


def test_the_two_stage_opamp_deck_behaves_as_before():
    from pathlib import Path
    from analogcoder.topologies import TOPOLOGY_LIBRARY
    text = Path("benchmarks/two_stage_opamp/netlist.cir").read_text()
    cands, _ = compatible_swaps({"ac": text}, TOPOLOGY_LIBRARY, set())
    assert {c.topology_id for c in cands} == {"miller_basic", "miller_nulling_resistor"}
    assert {c.block_path for c in cands} == {"OPAMP2STAGE"}
```

**측정된 값이 위 단언과 다르면, 단언이 아니라 사실을 보고할 것** — 실제 값을 리포트에 적고 단언을 그 값에 맞춘 뒤 왜 다른지 설명한다. 특히 `BGR_CORE`/`BANDGAP` 블록은 포트가 달라 후보에 없어야 한다.

- [ ] **Step 6: 커밋**

```bash
git add -A && git commit -m "feat: (블록, 토폴로지) 호환성을 결정론적으로 판정한다"
```

---

### Task 4: 에이전트 표면

**Files:**
- Modify: `src/analogcoder/schemas.py` (`TOPOLOGY_SCHEMA`)
- Modify: `src/analogcoder/agents/tuner.py` (`TOPOLOGY_TUNER_SYSTEM_PROMPT`, `propose_topology_swap`)
- Test: `tests/unit/test_tuner_agent.py`

**Interfaces:**
- Consumes: `SwapCandidate`(Task 3), `Topology`(Task 1)
- Produces:
  ```python
  async def propose_topology_swap(
      structure_view: str,
      judge_result: dict,
      candidates: list[SwapCandidate],
      library: dict[str, Topology],
      rejection_feedback: str | None,
      backend: AgentBackend,
  ) -> dict
  ```
  세 번째 인자가 `list[Topology]` → `list[SwapCandidate]`로 바뀌고, 설명 문구를 위해 `library`가 추가된다.

**`block_path`는 `required`에 넣지 않는다.** 필수 필드를 늘리면 약한 모델이 그것을 빠뜨렸을 때 모든 스펙이 하드 FAIL한다 — 하위 프로젝트 B에서 `control_block`으로 실제로 겪었다. 해소는 오케스트레이터(Task 5)가 한다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
@pytest.mark.asyncio
async def test_the_prompt_lists_block_and_topology_pairs_not_the_whole_library():
    backend = FakeBackend({"topology_id": "nine_port", "block_path": "AMP",
                           "reasoning": "r", "confidence": 80})
    await propose_topology_swap(
        "sv", {"criteria": []},
        [SwapCandidate(block_path="AMP", topology_id="nine_port")],
        {"nine_port": NINE_PORT, "five_port": FIVE_PORT},
        None, backend,
    )
    prompt = backend.calls[0]["user_prompt"]
    assert "AMP" in prompt and "nine_port" in prompt
    assert "five_port" not in prompt      # 후보가 아닌 항목은 새어 나가면 안 된다


@pytest.mark.asyncio
async def test_the_schema_does_not_require_block_path():
    assert "block_path" in TOPOLOGY_SCHEMA["properties"]
    assert "block_path" not in TOPOLOGY_SCHEMA["required"]


def test_the_system_prompt_does_not_assume_a_single_amplifier():
    assert "the amplifier's internal structure" not in TOPOLOGY_TUNER_SYSTEM_PROMPT
```

**잡는 변형:** 첫 테스트는 후보 필터링을 없애고 라이브러리 전체를 프롬프트에 넣는 변형을 잡는다.

- [ ] **Step 2: 실패 확인 → Step 3: 구현**

`TOPOLOGY_SCHEMA`에 `"block_path": {"type": "string"}`를 `properties`에만 추가한다.

`TOPOLOGY_TUNER_SYSTEM_PROMPT`를 고친다. 두 가지만:
1. 후보가 `(블록, 토폴로지)` 쌍이며 목록에 있는 쌍만 고를 수 있다고 명시. `block_path`는 그 쌍의 블록 이름을 그대로 쓴다고 명시.
2. "the amplifier's internal structure"라는 단수 표현 제거 — 덱에 앰프가 넷일 수 있다.

**호환성 규칙 자체는 프롬프트에 적지 않는다.** 후보가 이미 걸러진 집합이므로 되풀이할 규칙이 없고, 이 저장소에는 게이트와 어긋난 프롬프트가 승인 가능한 제안을 런 종료 사유로 바꾼 전례가 있다.

`propose_topology_swap`의 후보 렌더링은 `f"- {c.block_path} / {c.topology_id}: {library[c.topology_id].description} (addresses: {...})"` 꼴로 한다.

- [ ] **Step 4: 통과 확인 → Step 5: 커밋**

```bash
git add -A && git commit -m "feat: 토폴로지 제안이 블록과 토폴로지 쌍을 다룬다"
```

---

### Task 5: 오케스트레이터 배선

**Files:**
- Modify: `src/analogcoder/orchestrator.py`
- Test: `tests/unit/test_orchestrator.py`

**Interfaces:**
- Consumes: `compatible_swaps`(Task 3), `propose_topology_swap`(Task 4), `apply_topology_swap`(Task 2)
- Produces: `OrchestratorAgents.propose_topology`의 호출 규약이 `(structure_view, judge_result, candidates, library, rejection_feedback)`로 바뀐다. `cli.py`의 `propose_topology_fn`도 함께 고친다.

**바뀌는 것:**

1. `topology_swap_available` (`len(subckts) == 1`) **삭제**. 매 iteration `compatible_swaps(netlist_texts, TOPOLOGY_LIBRARY, tried)`를 호출한다.
2. `tried_topologies: set[str]` → `set[tuple[str, str]]`.
3. **`topology_candidates` 이벤트를 무조건 로깅한다** — 트리거가 걸린 iteration마다, 승인·기각과 무관하게. `{outer_iter, candidates: [{block_path, topology_id}], rejections: [{block_path, topology_id, reason, detail}]}`.
4. 트리거가 걸렸는데 후보가 0개면 **`topology_unavailable`을 로깅하고** 파라미터 모드를 계속한다(오늘의 "라이브러리 소진" 동작과 같다).
5. `block_path` 해소:
   - 응답에 있으면 그 값과 `topology_id`로 후보를 찾는다.
   - 없으면 `topology_id`가 맞는 후보들의 `block_path`가 **하나로 결정될 때만** 그것을 쓴다.
   - 결정되지 않으면(또는 쌍이 후보에 없으면) 기존 형태의 재시도 피드백. 후보 쌍 목록을 피드백에 담는다.
   - `MAX_TUNING_RETRIES` 소진 시 기존 FAIL 사유 문자열 유지.
6. 스왑 적용은 **그 블록을 정의하는 덱 전부**에 대해 `apply_topology_swap(text, block_path, body)`. (Task 3의 규칙 4 덕분에 후보라면 모든 덱이 정의하고 있다.)
7. `topology_swap` 이벤트에 `block_path`와, **그 스왑으로 에어리어 제약이 사라진 refdes 목록**을 추가한다. 후자는 스왑 후 그 블록의 컴포넌트 중 `baseline_components`에 없는 refdes다.
8. `consecutive_rollbacks` 리셋, `verify_post` 재사용, 롤백 처리는 **전부 그대로**.

**에어리어 게이트 기준선은 갱신하지 않는다.** 기존 주석이 의도된 설계라고 명시한다 — 바꾸는 것은 로깅뿐이다.

- [ ] **Step 1: 실패하는 테스트를 쓴다** (전부 에이전트 모킹)

```python
async def test_a_multi_block_deck_can_now_swap():
    """오늘은 구조적으로 불가능하다 - len(subckts) == 1 때문에."""
    # 9포트 블록 둘을 가진 덱, 3연속 롤백 후 스왑이 일어남을 확인

async def test_no_compatible_candidate_logs_topology_unavailable_and_stays_in_parameter_mode():
    ...

async def test_topology_candidates_is_logged_even_when_a_swap_is_approved():
    ...

async def test_an_omitted_block_path_resolves_when_only_one_block_is_a_candidate():
    ...

async def test_an_omitted_block_path_with_two_candidate_blocks_retries_with_feedback():
    # 피드백에 두 블록 이름이 모두 들어가는지 확인
    ...

async def test_a_swap_replaces_only_the_target_block_and_keeps_other_blocks_tuning():
    # 다른 블록에 먼저 파라미터 튜닝을 적용한 뒤 스왑하고, 그 값이 남아 있는지 확인
    ...

async def test_the_swap_event_records_which_refdes_the_area_gate_can_no_longer_bound():
    ...
```

**잡는 변형:** 세 번째가 조건부 로깅으로 되돌리는 변형을 잡는다. 여섯 번째가 스왑이 덱 전체를 갈아엎는 변형을 잡는다.

기존 오케스트레이터 토폴로지 테스트들은 새 호출 규약에 맞게 고친다. **"라이브러리 소진 시 파라미터 모드로 돌아간다"는 기존 테스트는 반드시 살려 둘 것** — 지금은 `tried`가 튜플이 되면서 소진 조건이 달라진다.

- [ ] **Step 2~4: 실패 확인 → 구현 → 통과 확인**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py -q`

- [ ] **Step 5: `cli.py`의 `propose_topology_fn` 갱신 + 전체 회귀**

Run: `.venv/bin/python -m pytest -m "not slow" -q`

- [ ] **Step 6: 커밋**

```bash
git add -A && git commit -m "feat: 다중 블록 덱에서 토폴로지 스왑이 가능해진다"
```

---

### Task 6: 시드 벤치마크와 실-ngspice 증명

**Files:**
- Create: `benchmarks/bandgap/netlist_seed_topology.cir`
- Create: `benchmarks/bandgap/spec_seed_topology.yaml`
- Create: `tests/unit/test_topology_seed_ngspice.py`

**Interfaces:**
- Consumes: 전 태스크 전부

**시드 덱:** `benchmarks/bandgap/netlist_loops.cir`를 복사하고 **`BUF_P`의 본문만** `BUF_N`의 본문으로 바꾼다. 다른 것은 전부 동일.

**중대 주의:** `BUF_N`의 본문을 쓴다 — 라이브러리 항목 `folded_cascode_nmos_in_cs`(출처 `TRIMAMP`)가 **아니다**. 둘은 `Xcl` 부하 커패시터와 `Xcc`/`XRz` 크기가 다르고, 설계 문서의 측정값(73.52 dB, 테일 10.1 mV)은 `BUF_N` 본문으로 잰 값이다. "라이브러리 항목으로 통일"하는 정리는 측정값을 무효로 만든다.

덱 상단 주석에 시드의 목적을 적되, **"그 노브"라고 쓰지 말 것** — 입력쌍 폭이 포화한다는 것은 측정됐지만, 유능한 에이전트가 다른 노브를 찾을 가능성을 배제한 주장은 이 저장소가 두 번 틀렸다.

**시드 스펙:** `spec.yaml`의 `amp_loops` 테스트벤치 **하나만** 선언하고 `netlist: netlist_seed_topology.cir`를 가리킨다. `buf0_loop_gain` 임계값을 **90.0**으로 올린다. 나머지 기준은 `spec.yaml`의 `amp_loops` 값 그대로. `optimize:`와 `corner_reduction:` 블록은 **넣지 않는다**.

- [ ] **Step 1: 덱과 스펙을 만든다**

- [ ] **Step 2: 실패하는 테스트를 쓴다**

```python
def test_the_seeded_deck_fails_buf0_loop_gain_and_the_swap_fixes_it(tmp_path):
    seeded = Path("benchmarks/bandgap/netlist_seed_topology.cir").read_text()
    before = _measure(seeded, tmp_path / "before.cir")["buf0_gain_db"]
    assert before < 90.0

    fixed = apply_topology_swap(
        seeded, "BUF_P", TOPOLOGY_LIBRARY["folded_cascode_pmos_in_cs"].subckt_body
    )
    after = _measure(fixed, tmp_path / "after.cir")["buf0_gain_db"]
    assert after >= 90.0


def test_the_seeded_deck_offers_exactly_the_fixing_swap_as_a_candidate():
    seeded = Path("benchmarks/bandgap/netlist_seed_topology.cir").read_text()
    cands, _ = compatible_swaps({"loops": seeded}, TOPOLOGY_LIBRARY, set())
    assert SwapCandidate("BUF_P", "folded_cascode_pmos_in_cs") in cands
```

`_measure`는 스펙의 `amp_loops` 컨트롤 블록을 덱에 붙여 `ngspice -b`로 돌리고 `meas` 결과를 파싱하는 헬퍼다. **`.include "pdk_corner.inc"`가 상대 경로이므로 `cwd=benchmarks/bandgap`으로 실행하거나, 덱을 그 디렉터리에 임시로 쓴다.** `tmp_path`에 쓰면 include를 못 찾는다.

**잡는 변형:** 첫 테스트가 `apply_topology_swap` 호출을 no-op으로 바꾸는 변형을 잡는다(before가 이미 90 미만이므로 after도 미만이 된다).

**기대값(설계 문서 실측):** before ≈ 73.52 dB, after ≈ 100.16 dB. **실제 값이 다르면 단언이 아니라 사실을 보고할 것.**

- [ ] **Step 3: 통과 확인**

Run: `.venv/bin/python -m pytest tests/unit/test_topology_seed_ngspice.py -q`
10초 미만이어야 한다. 넘으면 리포트에 실제 시간을 적을 것 — `slow` 마커 여부는 컨트롤러가 판단한다.

- [ ] **Step 4: 전체 회귀 + 커밋**

Run: `.venv/bin/python -m pytest -m "not slow" -q`

```bash
git add -A && git commit -m "test: 스왑으로만 풀리는 bandgap 시드 벤치마크"
```

---

## 자체 점검

- **스펙 커버리지:** 아키텍처(생성기) → Task 3+5. 호환성 세 규칙 → Task 3. 모든 테스트벤치 판정 → Task 3 규칙 4. 점 표기 → Task 2. 라이브러리 표면 → Task 1. 에이전트 표면 → Task 4. 로깅 → Task 5. 에어리어 게이트 → Task 5 항목 7. 벤치마크 → Task 6. 테스트 5종 → Task 1/3/2/5/6. 누락 없음.
- **정정 하나를 계획에 반영했다** (Task 1 계획 오류 정정 #1): 포트 선언 대조는 한 방향만 구조적으로 판정 가능하다. 스펙 본문의 표현이 그보다 강했다.
- **타입 일관성:** `SwapCandidate`/`SwapRejection`은 Task 3에서 정의되고 Task 4·5·6이 소비한다. `tried`는 Task 3·5 모두 `set[tuple[str, str]]`.
