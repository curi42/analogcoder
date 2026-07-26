# 넷리스트 구조 파생과 analyzer 대체 (E2) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** LLM analyzer 에이전트를 결정론적 구조 파생으로 대체하고, 적용 불가능한
파라미터 제안을 LLM 호출 전에 걷어내는 게이트를 추가한다.

**Architecture:** `netlist.py`/`params.py`의 파스 결과 위에 새 모듈 다섯 개를
얹는다. `structure.py`가 스코프별 평면 사실을 만들고, `signal_path.py`가 계층
연결을, `patterns.py`가 네 가지 지역 매칭을, `control_block.py`가 measurement→넷
매핑을 담당하며, `structure_view.py`가 실패 기준에서 초점 블록을 골라 계층적
상세도로 렌더링한다. 오케스트레이터는 `agents.analyze` 대신 이 동기 함수들을
직접 호출한다.

**Tech Stack:** Python 3, dataclasses, pytest. 새 런타임 의존성 없음.

## Global Constraints

- 스펙: `docs/superpowers/specs/2026-07-27-netlist-structure-derivation-design.md`
- **TDD.** 모든 Task는 실패하는 테스트 → 실패 확인 → 구현 → 통과 확인 → 커밋.
- **모르면 침묵.** 파생은 추측하지 않는다. 디바이스 클래스를 모르면 단자 역할을
  보고하지 않고, 패턴이 매칭되지 않으면 아무것도 내지 않는다.
- **파생 뷰는 값을 반복하지 않는다.** 값의 단일 출처는 넷리스트 원문이다.
- **게이트는 언제나 넷리스트 전문을 본다.** 초점은 프롬프트에만 적용된다.
- 토큰화는 반드시 `netlist.split_tokens`, 줄 읽기는 반드시
  `netlist.logical_lines`. `str.split()`을 새로 쓰면 E1이 고친 결함이 되살아난다.
- 테스트 실행: `.venv/bin/python -m pytest -q`. 현재 312 passed, 2 skipped.
- 커밋 메시지는 영어, 문서와 주석은 한글.

## 파일 구조

**신규**

| 파일 | 책임 |
|---|---|
| `src/analogcoder/structure.py` | 블록 인벤토리, 소자 사실, tunable 인덱스, 넷별 단자 역할 |
| `src/analogcoder/signal_path.py` | 인스턴스 트리, 포트↔넷 매핑, 넷→{블록: drive/sense} |
| `src/analogcoder/patterns.py` | 차동쌍·전류 미러·캐스코드·밀러 보상 매칭 |
| `src/analogcoder/control_block.py` | control block의 `meas`/`let`에서 measurement→넷 |
| `src/analogcoder/structure_view.py` | 초점 선정, 레벨 0/1 렌더링, 원문 축약 |
| `tests/unit/test_structure.py` | Task 1 |
| `tests/unit/test_signal_path.py` | Task 2 |
| `tests/unit/test_control_block.py` | Task 3 |
| `tests/unit/test_structure_view.py` | Task 4 |
| `tests/unit/test_param_applicability.py` | Task 5 |
| `tests/unit/test_patterns.py` | Task 7 |
| `tests/unit/test_structure_golden.py` | Task 8 |
| `tests/fixtures/structure_golden/*.json` | Task 8 |

**수정**

| 파일 | 변경 |
|---|---|
| `src/analogcoder/netlist.py` | `check_param_applicability` 추가 |
| `src/analogcoder/orchestrator.py` | `agents.analyze` 제거, 파생·초점·게이트 배선 |
| `src/analogcoder/cli.py` | `analyze_fn` 제거, `AGENT_NAMES`에서 `"analyzer"` 제거 |
| `src/analogcoder/schemas.py` | `ANALYZER_SCHEMA` 삭제 |
| `tests/unit/test_orchestrator.py` | fake spec에 `circuit_name`·`control_block` 추가 |
| `tests/unit/test_cli.py`, `test_schemas.py` | analyzer 참조 제거 |
| `CLAUDE.md` | 아키텍처 절 갱신 |

**삭제**: `src/analogcoder/agents/analyzer.py`, `tests/unit/test_analyzer_agent.py`

## Task 순서의 이유

`patterns.py`가 세 파생 모듈 중 **유일하게 틀릴 수 있는** 부분이므로 마지막
직전에 둔다. Task 6까지 끝나면 analyzer 대체는 이미 완성되고 동작하며,
`patterns.py`는 순수하게 추가되는 정보다. 그래서 Task 4의
`render_structure`는 처음부터 패턴 목록을 인자로 받되 Task 6의 호출부가 빈
목록을 넘기고, Task 7이 그 호출부를 실제 매칭으로 바꾼다. 빈 목록은
자리표시자가 아니라 정당한 상태다 — 매칭이 하나도 없는 회로에서 실제로 그렇게
렌더링된다.

---

### Task 1: `structure.py` — 스코프별 평면 사실

**Files:**
- Create: `src/analogcoder/structure.py`
- Test: `tests/unit/test_structure.py`

**Interfaces:**
- Consumes: `netlist.parse_netlist`, `netlist.Component`, `netlist.Subckt`,
  `params.build_param_envs`
- Produces:
  - `Terminal(refdes: str, name: str, role: str)` — `role`은 `"drive"`,
    `"sense"`, `"bulk"` 중 하나
  - `ComponentFact(refdes, ctype, device_class, model, nodes, params, terminals)`
  - `BlockStructure(path, ports, components, instance_count)`
  - `TunableEntry(refdes, param)`
  - `NetlistStructure(circuit_name, blocks, tunable, net_terminals)`
  - `derive_structure(netlist_text: str, circuit_name: str) -> NetlistStructure`

스펙은 역할을 drive/sense 둘로 적었으나 구현은 셋으로 나눈다. MOSFET의 bulk는
전도하지만 신호를 나르지 않는다. bulk를 drive로 묶으면 모든 블록이 `vss`를
"구동"하게 되어 Task 4의 초점 씨앗이 전 블록으로 번진다. 세 번째 역할을 두는
편이 전원/접지 넷을 이름으로 추측하는 것보다 정확하다.

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/unit/test_structure.py`:

```python
from analogcoder.structure import derive_structure

TWO_BLOCK = (
    "* t\n"
    ".option scale=1.0u\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "X1 nx vinn tail vss sky130_fd_pr__nfet_01v8 W=48 L=1\n"
    "X2 ny vinp tail vss sky130_fd_pr__nfet_01v8 W=48 L=1\n"
    "Cc nx vout 1p\n"
    ".ends AMP\n"
    "Xa1 inp inn out vdd 0 AMP\n"
    "Xa2 inp inn out2 vdd 0 AMP\n"
    "Rf inn out 10k\n"
    ".end\n"
)


def test_blocks_are_keyed_by_definition_path_with_none_for_top_level():
    s = derive_structure(TWO_BLOCK, "demo")

    assert s.circuit_name == "demo"
    assert set(s.blocks) == {None, "AMP"}
    assert s.blocks["AMP"].ports == ["vinp", "vinn", "vout", "vdd", "vss"]
    assert [c.refdes for c in s.blocks[None].components] == ["Xa1", "Xa2", "Rf"]


def test_a_definition_reports_how_many_times_it_is_instantiated():
    # 정의를 튜닝하면 모든 인스턴스가 바뀐다. 그 사실이 보여야 한다.
    s = derive_structure(TWO_BLOCK, "demo")

    assert s.blocks["AMP"].instance_count == 2
    assert s.blocks[None].instance_count == 1


def test_component_refdes_is_scope_qualified():
    s = derive_structure(TWO_BLOCK, "demo")

    assert [c.refdes for c in s.blocks["AMP"].components] == ["AMP.X1", "AMP.X2", "AMP.Cc"]


def test_a_recognised_model_name_yields_terminal_roles():
    s = derive_structure(TWO_BLOCK, "demo")
    x1 = s.blocks["AMP"].components[0]

    assert x1.device_class == "nfet"
    assert [(t.name, t.role) for t in x1.terminals] == [
        ("d", "drive"), ("g", "sense"), ("s", "drive"), ("b", "bulk"),
    ]


def test_an_unrecognised_x_instance_reports_no_terminals_rather_than_guessing():
    # Xa1은 서브회로 인스턴스다. 단자 의미를 모르므로 침묵한다 -
    # 추측한 역할은 초점 선정을 조용히 틀리게 만든다.
    s = derive_structure(TWO_BLOCK, "demo")
    xa1 = s.blocks[None].components[0]

    assert xa1.device_class is None
    assert xa1.terminals == []


def test_an_m_prefix_is_a_mosfet_even_when_the_model_name_says_nothing():
    # ctype 자체가 SPICE의 보장이다. 모델 이름 표에 없다고 침묵하면
    # generic level-1 덱 전체가 단자 역할을 잃는다.
    deck = "* t\nM6 vout outA vss vss NMOSG W=40u L=1u\n.end\n"

    m6 = derive_structure(deck, "demo").blocks[None].components[0]

    assert [(t.name, t.role) for t in m6.terminals] == [
        ("d", "drive"), ("g", "sense"), ("s", "drive"), ("b", "bulk"),
    ]


def test_the_tunable_index_covers_both_named_params_and_positional_values():
    s = derive_structure(TWO_BLOCK, "demo")
    entries = {(e.refdes, e.param) for e in s.tunable}

    assert ("AMP.X1", "W") in entries
    assert ("AMP.X1", "L") in entries
    assert ("AMP.Cc", "value") in entries
    assert ("Rf", "value") in entries
    # 모델명은 튜닝 대상이 아니다 - 숫자로 덮어쓰면 덱이 깨진다.
    assert ("AMP.X1", "value") not in entries


def test_nets_are_scope_qualified_so_two_blocks_cannot_collide():
    s = derive_structure(TWO_BLOCK, "demo")

    assert "AMP.tail" in s.net_terminals
    assert {t.refdes for t in s.net_terminals["AMP.tail"]} == {"AMP.X1", "AMP.X2"}
    # Xa1은 서브회로 인스턴스라 단자 역할을 내지 않으므로 out에는 Rf만 남는다.
    assert {t.refdes for t in s.net_terminals["out"]} == {"Rf"}


def test_derivation_is_deterministic():
    # analyzer는 같은 넷리스트에 대해 roles를 93/26/1개로 냈다. 이 테스트가
    # 그것과 대비되는 지점이다.
    assert derive_structure(TWO_BLOCK, "demo") == derive_structure(TWO_BLOCK, "demo")
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_structure.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'analogcoder.structure'`

- [ ] **Step 3: 구현한다**

`src/analogcoder/structure.py`:

```python
from dataclasses import dataclass, field

from analogcoder.netlist import Component, parse_netlist, parse_spice_value

# 모델 이름에서 디바이스 클래스를 읽는다. area_limits.py의 _SKY130_CTYPE_MARKERS와
# 같은 방식이되, 여기서는 티어가 아니라 단자 의미를 위해 쓴다. 표에 없는
# 이름은 None - 추측하지 않는다.
_MODEL_CLASS_MARKERS: list[tuple[str, str]] = [
    ("nfet", "nfet"),
    ("pfet", "pfet"),
    ("pnp", "pnp"),
    ("npn", "npn"),
    ("res", "res"),
    ("cap", "cap"),
]

# 단자 이름과 역할. 게이트는 DC 전류를 흘리지 않으므로 순수 감지 단자이고,
# bulk는 전도하지만 신호를 나르지 않으므로 따로 둔다 - drive로 묶으면 모든
# 블록이 vss를 구동하게 되어 초점 씨앗이 전 블록으로 번진다.
_MOS_TERMINALS = [("d", "drive"), ("g", "sense"), ("s", "drive"), ("b", "bulk")]
_BJT_TERMINALS = [("c", "drive"), ("b", "drive"), ("e", "drive")]
_TWO_TERMINALS = [("1", "drive"), ("2", "drive")]

_TERMINALS_BY_CLASS: dict[str, list[tuple[str, str]]] = {
    "nfet": _MOS_TERMINALS,
    "pfet": _MOS_TERMINALS,
    "pnp": _BJT_TERMINALS,
    "npn": _BJT_TERMINALS,
    "res": _TWO_TERMINALS,
    "cap": _TWO_TERMINALS,
}
# refdes 접두는 SPICE의 보장이므로 모델 이름을 몰라도 단자 의미를 안다.
_TERMINALS_BY_CTYPE: dict[str, list[tuple[str, str]]] = {
    "M": _MOS_TERMINALS,
    "Q": _BJT_TERMINALS,
    "R": _TWO_TERMINALS,
    "C": _TWO_TERMINALS,
    "L": _TWO_TERMINALS,
    "D": _TWO_TERMINALS,
}


@dataclass(frozen=True)
class Terminal:
    refdes: str
    name: str
    role: str


@dataclass
class ComponentFact:
    refdes: str
    ctype: str
    device_class: str | None
    model: str | None
    nodes: list[str]
    params: dict[str, str]
    terminals: list[Terminal] = field(default_factory=list)


@dataclass
class BlockStructure:
    path: str | None
    ports: list[str]
    components: list[ComponentFact]
    instance_count: int


@dataclass(frozen=True)
class TunableEntry:
    refdes: str
    param: str


@dataclass
class NetlistStructure:
    circuit_name: str
    blocks: dict[str | None, BlockStructure]
    tunable: list[TunableEntry]
    net_terminals: dict[str, list[Terminal]]


def _qualify(scope: str | None, name: str) -> str:
    return f"{scope}.{name}" if scope else name


def _classify_model(component: Component) -> str | None:
    if component.value is None:
        return None
    lowered = component.value.lower()
    for marker, klass in _MODEL_CLASS_MARKERS:
        if marker in lowered:
            return klass
    return None


def _is_numeric_value(raw: str | None) -> bool:
    """위치 값이 숫자인가. 모델명/서브회로명이면 False이고, 그런 값은
    tunable 인덱스에 넣지 않는다 - param="value"로 덮어쓰면 덱이 깨진다."""
    if raw is None:
        return False
    try:
        parse_spice_value(raw)
    except ValueError:
        return False
    return True


def _terminals_for(refdes: str, component: Component, device_class: str | None) -> list[Terminal]:
    layout = _TERMINALS_BY_CTYPE.get(component.ctype)
    if layout is None and device_class is not None:
        layout = _TERMINALS_BY_CLASS.get(device_class)
    if layout is None:
        return []
    if len(component.nodes) < len(layout):
        # 노드가 모자란 줄은 유효한 SPICE가 아니다. 여기서 추측하지 않는다.
        return []
    return [Terminal(refdes=refdes, name=name, role=role) for name, role in layout]


def _fact(scope: str | None, component: Component) -> ComponentFact:
    refdes = _qualify(scope, component.refdes)
    device_class = _classify_model(component)
    model = component.value if not _is_numeric_value(component.value) else None
    return ComponentFact(
        refdes=refdes,
        ctype=component.ctype,
        device_class=device_class,
        model=model,
        nodes=list(component.nodes),
        params=dict(component.params),
        terminals=_terminals_for(refdes, component, device_class),
    )


def derive_structure(netlist_text: str, circuit_name: str) -> NetlistStructure:
    parsed = parse_netlist(netlist_text)

    scoped: list[tuple[str | None, list[Component]]] = [(None, parsed.top_components)]
    scoped += [(path, subckt.components) for path, subckt in sorted(parsed.subckts.items())]

    # 정의 이름별 인스턴스 수. 인스턴스는 정의를 이름(경로의 마지막 조각)으로
    # 지목하므로 이름으로 센다.
    definition_names = {path.rpartition(".")[2]: path for path in parsed.subckts}
    instance_counts: dict[str, int] = {path: 0 for path in parsed.subckts}
    for _scope, components in scoped:
        for component in components:
            if component.ctype != "X" and component.ctype != "x":
                continue
            target = definition_names.get(component.value or "")
            if target is not None:
                instance_counts[target] += 1

    blocks: dict[str | None, BlockStructure] = {}
    tunable: list[TunableEntry] = []
    net_terminals: dict[str, list[Terminal]] = {}

    for scope, components in scoped:
        facts = [_fact(scope, component) for component in components]
        for fact, component in zip(facts, components):
            for name in sorted(fact.params):
                tunable.append(TunableEntry(refdes=fact.refdes, param=name))
            if _is_numeric_value(component.value):
                tunable.append(TunableEntry(refdes=fact.refdes, param="value"))
            for terminal, net in zip(fact.terminals, fact.nodes):
                net_terminals.setdefault(_qualify(scope, net), []).append(terminal)

        ports = parsed.subckts[scope].ports if scope is not None else []
        blocks[scope] = BlockStructure(
            path=scope,
            ports=list(ports),
            components=facts,
            instance_count=instance_counts[scope] if scope is not None else 1,
        )

    return NetlistStructure(
        circuit_name=circuit_name, blocks=blocks, tunable=tunable, net_terminals=net_terminals
    )
```

`parse_spice_value`는 `netlist.py:495`에 이미 있다. 새로 만들지 말 것.

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_structure.py -q`
Expected: PASS (9 passed)

- [ ] **Step 5: 전체 테스트가 여전히 통과하는지 확인한다**

Run: `.venv/bin/python -m pytest -q`
Expected: 321 passed, 2 skipped

- [ ] **Step 6: 커밋**

```bash
git add src/analogcoder/structure.py tests/unit/test_structure.py
git commit -m "feat: derive flat per-scope netlist facts deterministically"
```

---

### Task 2: `signal_path.py` — 계층 연결

**Files:**
- Create: `src/analogcoder/signal_path.py`
- Test: `tests/unit/test_signal_path.py`

**Interfaces:**
- Consumes: `structure.NetlistStructure`, `structure.Terminal`
- Produces:
  - `InstanceEdge(instance_refdes: str, definition: str, port_nets: dict[str, str], mismatch: str | None)`
  - `SignalPaths(instances: list[InstanceEdge], net_blocks: dict[str, dict[str, str]])`
  - `build_signal_paths(structure: NetlistStructure) -> SignalPaths`

`net_blocks`의 키는 **최상위 스코프의 한정 넷 이름**이고, 값은 `{정의 이름:
"drive" | "sense"}`이다. 한 블록이 같은 넷에 구동 단자와 감지 단자를 모두
붙이면 `"drive"`가 이긴다.

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/unit/test_signal_path.py`:

```python
from analogcoder.signal_path import build_signal_paths
from analogcoder.structure import derive_structure

CHAIN = (
    "* t\n"
    ".subckt STAGE vin vout vss\n"
    "M1 vout vin vss vss NMOS W=10 L=1\n"
    ".ends STAGE\n"
    "Xs1 na nb 0 STAGE\n"
    "Xs2 nb nc 0 STAGE\n"
    ".end\n"
)


def test_an_instance_maps_definition_ports_to_parent_nets_by_position():
    paths = build_signal_paths(derive_structure(CHAIN, "demo"))
    edge = next(e for e in paths.instances if e.instance_refdes == "Xs1")

    assert edge.definition == "STAGE"
    assert edge.port_nets == {"vin": "na", "vout": "nb", "vss": "0"}
    assert edge.mismatch is None


def test_a_net_reports_the_definition_that_drives_it_and_the_one_that_senses_it():
    # STAGE는 M1의 드레인으로 vout을 구동하고 게이트로 vin을 감지한다.
    # nb는 Xs1의 출력이자 Xs2의 입력이므로 둘 다 붙는다.
    paths = build_signal_paths(derive_structure(CHAIN, "demo"))

    assert paths.net_blocks["nb"] == {"STAGE": "drive"}
    assert paths.net_blocks["na"] == {"STAGE": "sense"}


def test_drive_wins_when_one_definition_both_drives_and_senses_a_net():
    deck = (
        "* t\n"
        ".subckt SELF a vss\n"
        "M1 a a vss vss NMOS W=10 L=1\n"
        ".ends SELF\n"
        "Xs na 0 SELF\n"
        ".end\n"
    )

    paths = build_signal_paths(derive_structure(deck, "demo"))

    assert paths.net_blocks["na"] == {"SELF": "drive"}


def test_a_port_count_mismatch_is_reported_as_a_fact_not_silently_dropped():
    # 노드 수가 포트 수와 다른 것은 넷리스트 버그다. 감추면 사용자가
    # 시뮬레이션 실패의 원인을 영원히 못 찾는다.
    deck = (
        "* t\n"
        ".subckt STAGE vin vout vss\n"
        "M1 vout vin vss vss NMOS W=10 L=1\n"
        ".ends STAGE\n"
        "Xs1 na nb STAGE\n"
        ".end\n"
    )

    paths = build_signal_paths(derive_structure(deck, "demo"))
    edge = next(e for e in paths.instances if e.instance_refdes == "Xs1")

    assert edge.port_nets == {}
    assert "2" in edge.mismatch and "3" in edge.mismatch


def test_a_bulk_terminal_does_not_make_a_block_a_driver_of_ground():
    # bulk를 drive로 묶으면 모든 블록이 0을 구동하게 되어 초점이 무의미해진다.
    paths = build_signal_paths(derive_structure(CHAIN, "demo"))

    assert "STAGE" not in paths.net_blocks.get("0", {})
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_signal_path.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'analogcoder.signal_path'`

- [ ] **Step 3: 구현한다**

`src/analogcoder/signal_path.py`:

```python
from dataclasses import dataclass, field

from analogcoder.structure import NetlistStructure

_SIGNAL_ROLES = ("drive", "sense")


@dataclass
class InstanceEdge:
    instance_refdes: str
    definition: str
    port_nets: dict[str, str] = field(default_factory=dict)
    mismatch: str | None = None


@dataclass
class SignalPaths:
    instances: list[InstanceEdge]
    net_blocks: dict[str, dict[str, str]]


def _definition_of(structure: NetlistStructure, model: str | None) -> str | None:
    """인스턴스가 지목하는 정의 경로. 인스턴스는 정의를 이름으로 부르므로
    경로의 마지막 조각으로 찾는다."""
    if model is None:
        return None
    for path in structure.blocks:
        if path is not None and path.rpartition(".")[2] == model:
            return path
    return None


def build_signal_paths(structure: NetlistStructure) -> SignalPaths:
    instances: list[InstanceEdge] = []
    for path, block in structure.blocks.items():
        for fact in block.components:
            definition = _definition_of(structure, fact.model)
            if definition is None:
                continue
            ports = structure.blocks[definition].ports
            if len(ports) != len(fact.nodes):
                instances.append(
                    InstanceEdge(
                        instance_refdes=fact.refdes,
                        definition=definition,
                        mismatch=(
                            f"{fact.refdes} gives {len(fact.nodes)} nodes but "
                            f"{definition} declares {len(ports)} ports"
                        ),
                    )
                )
                continue
            instances.append(
                InstanceEdge(
                    instance_refdes=fact.refdes,
                    definition=definition,
                    port_nets=dict(zip(ports, fact.nodes)),
                )
            )

    # 정의별로 "이 포트를 통해 바깥으로 나가는 역할"을 먼저 모은다.
    port_roles: dict[str, dict[str, str]] = {}
    for path, block in structure.blocks.items():
        if path is None:
            continue
        roles: dict[str, str] = {}
        for fact in block.components:
            for terminal, net in zip(fact.terminals, fact.nodes):
                if terminal.role not in _SIGNAL_ROLES or net not in block.ports:
                    continue
                if roles.get(net) != "drive":
                    roles[net] = terminal.role
        port_roles[path] = roles

    # 최상위 인스턴스에서 시작해 포트를 통해 부모 넷으로 역할을 밀어 올린다.
    net_blocks: dict[str, dict[str, str]] = {}

    def record(net: str, definition: str, role: str) -> None:
        bucket = net_blocks.setdefault(net, {})
        if bucket.get(definition) != "drive":
            bucket[definition] = role

    edges_by_scope: dict[str | None, list[InstanceEdge]] = {}
    for edge in instances:
        scope = edge.instance_refdes.rpartition(".")[0] or None
        edges_by_scope.setdefault(scope, []).append(edge)

    def walk(scope: str | None, translate: dict[str, str] | None) -> None:
        for edge in edges_by_scope.get(scope, []):
            name = edge.definition.rpartition(".")[2]
            for port, local_net in edge.port_nets.items():
                outer = translate.get(local_net, None) if translate is not None else local_net
                role = port_roles.get(edge.definition, {}).get(port)
                if outer is None or role is None:
                    continue
                record(outer, name, role)
            # 한 단계 안으로 들어가며 좌표계를 부모 넷으로 바꾼다.
            inner = {}
            for port, local_net in edge.port_nets.items():
                outer = translate.get(local_net, None) if translate is not None else local_net
                if outer is not None:
                    inner[port] = outer
            walk(edge.definition, inner)

    walk(None, None)

    # 최상위에 직접 놓인 소자도 넷을 구동/감지한다.
    for fact in structure.blocks[None].components:
        for terminal, net in zip(fact.terminals, fact.nodes):
            if terminal.role in _SIGNAL_ROLES:
                record(net, fact.refdes, terminal.role)

    return SignalPaths(instances=instances, net_blocks=net_blocks)
```

최상위 소자는 정의가 없으므로 자기 refdes를 키로 기록한다. 초점 선정에서
최상위는 언제나 초점이므로 이 항목은 초점 계산에 영향을 주지 않고, 렌더링에서
"이 넷을 최상위의 무엇이 건드리는가"를 보여주는 데만 쓰인다.

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_signal_path.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: 실제 벤치마크로 손검증한다**

Run:
```bash
.venv/bin/python -c "
from analogcoder.structure import derive_structure
from analogcoder.signal_path import build_signal_paths
s = derive_structure(open('benchmarks/bandgap/netlist.cir').read(), 'bandgap')
p = build_signal_paths(s)
for net in ['vbg0', 'vbg1', 'vbgout']:
    print(net, p.net_blocks.get(net))
"
```
Expected: `vbg0`에 `BUF_P`가 `drive`로, `vbg1`에 `BUF_N`이 `drive`로 나온다.
다르면 구현이 틀린 것이니 넘어가지 말 것 — 스펙이 "`BUF_P`가 `vbg0`를 구동한다"를
`BUF_P`에서 실측했다고 기록하고 있다.

- [ ] **Step 6: 커밋**

```bash
git add src/analogcoder/signal_path.py tests/unit/test_signal_path.py
git commit -m "feat: map ports to nets across hierarchy and label net drivers"
```

---

### Task 3: `control_block.py` — measurement → 넷 매핑

**Files:**
- Create: `src/analogcoder/control_block.py`
- Test: `tests/unit/test_control_block.py`

**Interfaces:**
- Produces: `measurement_nets(control_block: str) -> dict[str, set[str]]`

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/unit/test_control_block.py`:

```python
from analogcoder.control_block import measurement_nets

BANDGAP_DC = """
.control
dc temp -40 125 1
meas dc vmax MAX v(vbgout)
meas dc vmin MIN v(vbgout)
meas dc vbg0_v FIND v(vbg0) AT=27
meas dc idd FIND i(Vdd) AT=27
let tc_ppm_per_c = (vmax-vmin)/(vbgout_v*165)*1e6
let iq_ua = -1e6*idd
.endc
"""


def test_a_meas_line_maps_its_name_to_the_nets_it_references():
    nets = measurement_nets(BANDGAP_DC)

    assert nets["vbg0_v"] == {"vbg0"}
    assert nets["vmax"] == {"vbgout"}


def test_a_current_reference_maps_to_the_source_it_names():
    nets = measurement_nets(BANDGAP_DC)

    assert nets["idd"] == {"Vdd"}


def test_a_let_expression_inherits_the_nets_of_the_measurements_it_references():
    nets = measurement_nets(BANDGAP_DC)

    # tc_ppm_per_c는 넷을 직접 언급하지 않는다. vmax/vmin을 통해서만 vbgout에 닿는다.
    assert nets["tc_ppm_per_c"] == {"vbgout"}
    assert nets["iq_ua"] == {"Vdd"}


def test_a_two_node_voltage_reference_yields_both_nets():
    nets = measurement_nets("meas ac gain_db MAX vdb(out,in)\n")

    assert nets["gain_db"] == {"out", "in"}


def test_an_unresolvable_name_yields_an_empty_set_rather_than_being_absent():
    # 빈 집합은 "이 measurement는 넷을 모른다"는 사실이고, 부재는 버그처럼 보인다.
    nets = measurement_nets("let mystery = undefined_thing * 2\n")

    assert nets["mystery"] == set()
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_control_block.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'analogcoder.control_block'`

- [ ] **Step 3: 구현한다**

`src/analogcoder/control_block.py`:

```python
import re

# v(a), v(a,b), vdb(a), i(Vdd) 같은 참조. 넷 이름과 소스 이름 모두 여기로 잡힌다.
_REFERENCE_RE = re.compile(r"\b[a-z]*[vi]\s*\(\s*([^)]*)\)", re.IGNORECASE)
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_MEAS_PREFIXES = ("meas", ".meas")


def _references(expression: str) -> set[str]:
    nets: set[str] = set()
    for inner in _REFERENCE_RE.findall(expression):
        for part in inner.split(","):
            name = part.strip()
            if name:
                nets.add(name)
    return nets


def measurement_nets(control_block: str) -> dict[str, set[str]]:
    """measurement 이름 → 그것이 관측하는 넷 이름 집합.

    criterion은 넷이 아니라 measurement 이름을 참조하므로, 초점 선정은 이
    매핑 없이는 성립하지 않는다. `meas` 줄은 `v(...)`/`i(...)` 참조를 직접
    읽고, `let` 줄은 표현식이 참조하는 다른 measurement 이름을 통해 한 단계 더
    따라간다."""
    direct: dict[str, set[str]] = {}
    derived: dict[str, set[str]] = {}

    for raw_line in control_block.splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if lowered.startswith(_MEAS_PREFIXES):
            tokens = line.split()
            # meas <analysis> <name> <func> <ref...>
            if len(tokens) < 3:
                continue
            direct[tokens[2]] = _references(line)
        elif lowered.startswith("let "):
            name, _, expression = line[4:].partition("=")
            derived[name.strip()] = set(_IDENTIFIER_RE.findall(expression))

    result: dict[str, set[str]] = dict(direct)

    # let은 다른 let을 참조할 수 있다. 이름 개수만큼 돌면 반드시 수렴하고,
    # 순환 참조가 있어도 늘어나지 않는 시점에 멈춘다.
    for _ in range(len(derived) + 1):
        changed = False
        for name, referenced in derived.items():
            nets: set[str] = set()
            for token in referenced:
                nets |= result.get(token, set())
            if result.get(name) != nets:
                result[name] = nets
                changed = True
        if not changed:
            break

    return result
```

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_control_block.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: 실제 스펙으로 손검증한다**

Run:
```bash
.venv/bin/python -c "
from analogcoder.spec import load_spec
from analogcoder.control_block import measurement_nets
spec = load_spec('benchmarks/bandgap/spec.yaml')
for tb in spec.testbenches:
    nets = measurement_nets(tb.control_block)
    for c in tb.criteria:
        print(tb.name, c.name, '->', c.measurement, '->', sorted(nets.get(c.measurement, [])))
"
```
Expected: 모든 criterion이 비어 있지 않은 넷 집합을 갖는다. 빈 것이 있으면
그 measurement의 `meas`/`let` 형태를 확인하고 파서를 고친다 — 빈 집합은 Task 4
에서 "전 블록 초점" 폴백으로 이어지므로 조용히 넘어가면 초점이 아무 일도 하지
않게 된다.

- [ ] **Step 6: 커밋**

```bash
git add src/analogcoder/control_block.py tests/unit/test_control_block.py
git commit -m "feat: resolve measurement names to the nets they observe"
```

---

### Task 4: `structure_view.py` — 초점 선정과 렌더링

**Files:**
- Create: `src/analogcoder/structure_view.py`
- Test: `tests/unit/test_structure_view.py`

**Interfaces:**
- Consumes: `structure.NetlistStructure`, `signal_path.SignalPaths`,
  `control_block.measurement_nets`
- Produces:
  - `select_focus(structure, paths, failing_nets: set[str], touched_refdes: set[str]) -> set[str]`
    — 반환값은 **정의 경로의 집합**. 최상위(`None`)는 언제나 초점이므로
    집합에 담지 않고 렌더러가 무조건 포함한다.
  - `render_structure(structure, paths, patterns: list, focus: set[str]) -> str`
  - `render_netlist(netlist_text: str, focus: set[str]) -> str`
  - `focus_misses(focus: set[str], changes: list[dict]) -> list[str]`

`patterns` 인자는 이 Task에서 빈 목록으로만 쓰인다. Task 7이 실제 매칭을
넘긴다. 빈 목록은 매칭이 없는 회로의 정당한 상태이므로 자리표시자가 아니다.

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/unit/test_structure_view.py`:

```python
from analogcoder.signal_path import build_signal_paths
from analogcoder.structure import derive_structure
from analogcoder.structure_view import (
    focus_misses,
    render_netlist,
    render_structure,
    select_focus,
)

CHAIN = (
    "* t\n"
    ".subckt DRIVER vin vout vss\n"
    "M1 vout vin vss vss NMOS W=10 L=1\n"
    ".ends DRIVER\n"
    ".subckt SPARE a b vss\n"
    "M9 b a vss vss NMOS W=10 L=1\n"
    ".ends SPARE\n"
    "Xd na out 0 DRIVER\n"
    "Xs p q 0 SPARE\n"
    "Vin na 0 DC 1\n"
    ".end\n"
)


def _built():
    s = derive_structure(CHAIN, "demo")
    return s, build_signal_paths(s)


def test_focus_seeds_from_the_blocks_that_touch_a_failing_net():
    s, paths = _built()

    assert select_focus(s, paths, {"out"}, set()) == {"DRIVER"}


def test_focus_walks_one_hop_back_to_whatever_drives_what_a_seed_senses():
    deck = CHAIN.replace("Xd na out 0 DRIVER", "Xd mid out 0 DRIVER").replace(
        "Xs p q 0 SPARE", "Xs na mid 0 SPARE"
    )
    s = derive_structure(deck, "demo")
    paths = build_signal_paths(s)

    # DRIVER는 mid를 감지하고 SPARE가 mid를 구동한다. 씨앗의 입력을 만드는
    # 블록을 못 보면 튜너가 원인 쪽을 건드릴 수 없다.
    assert select_focus(s, paths, {"out"}, set()) == {"DRIVER", "SPARE"}


def test_a_block_already_touched_this_run_stays_in_focus():
    s, paths = _built()

    assert select_focus(s, paths, {"out"}, {"SPARE.M9"}) == {"DRIVER", "SPARE"}


def test_no_seed_falls_back_to_every_block_rather_than_to_nothing():
    s, paths = _built()

    assert select_focus(s, paths, set(), set()) == {"DRIVER", "SPARE"}


def test_the_structure_view_lists_every_block_but_details_only_the_focused_ones():
    s, paths = _built()

    text = render_structure(s, paths, [], {"DRIVER"})

    assert "SPARE" in text          # 레벨 0으로는 반드시 보인다
    assert "DRIVER.M1.W" in text    # 초점 블록의 주소록
    assert "SPARE.M9.W" not in text


def test_the_structure_view_never_repeats_a_value():
    # 값의 단일 출처는 넷리스트 원문이다. 두 벌이 들어가면 E1이 겪은
    # "덱에 W가 두 번" 과 같은 모양의 불일치가 모델 쪽에서 재발한다.
    s, paths = _built()

    text = render_structure(s, paths, [], {"DRIVER"})

    assert "W=10" not in text


def test_the_netlist_view_keeps_every_header_and_folds_only_unfocused_bodies():
    text = render_netlist(CHAIN, {"DRIVER"})

    assert "M1 vout vin vss vss NMOS W=10 L=1" in text
    assert "M9" not in text
    assert ".subckt SPARE a b vss" in text
    assert "elided" in text
    assert "Xd na out 0 DRIVER" in text     # 최상위는 언제나 남는다
    assert "Vin na 0 DC 1" in text


def test_a_proposal_outside_focus_is_reported_so_a_wrong_focus_leaves_evidence():
    changes = [{"refdes": "SPARE.M9", "param": "W"}, {"refdes": "DRIVER.M1", "param": "W"}]

    assert focus_misses({"DRIVER"}, changes) == ["SPARE.M9"]
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_structure_view.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'analogcoder.structure_view'`

- [ ] **Step 3: 구현한다**

`src/analogcoder/structure_view.py`:

```python
from analogcoder.netlist import logical_lines, split_tokens
from analogcoder.signal_path import SignalPaths
from analogcoder.structure import NetlistStructure


def _definition_name(path: str) -> str:
    return path.rpartition(".")[2]


def select_focus(
    structure: NetlistStructure,
    paths: SignalPaths,
    failing_nets: set[str],
    touched_refdes: set[str],
) -> set[str]:
    """상세히 렌더링할 정의 경로의 집합.

    최상위 스코프는 반환값에 담지 않는다 - 테스트벤치의 자극원과 DUT
    인스턴스가 거기 있어 언제나 초점이고, 렌더러가 무조건 포함한다."""
    definitions = {path for path in structure.blocks if path is not None}
    by_name = {_definition_name(path): path for path in definitions}

    seeds: set[str] = set()
    for net in failing_nets:
        for name in paths.net_blocks.get(net, {}):
            if name in by_name:
                seeds.add(by_name[name])

    # 역방향 1홉: 씨앗이 감지하는 넷을 구동하는 블록을 더한다.
    sensed: set[str] = set()
    for net, blocks in paths.net_blocks.items():
        if any(by_name.get(name) in seeds and role == "sense" for name, role in blocks.items()):
            sensed.add(net)
    upstream = {
        by_name[name]
        for net in sensed
        for name, role in paths.net_blocks.get(net, {}).items()
        if role == "drive" and name in by_name
    }

    touched = {
        path
        for path in definitions
        for refdes in touched_refdes
        if refdes.startswith(f"{path}.")
    }

    focus = seeds | upstream | touched
    # 씨앗이 하나도 없으면 조용히 비는 대신 전부 보여준다.
    return focus or definitions


def render_structure(
    structure: NetlistStructure, paths: SignalPaths, patterns: list, focus: set[str]
) -> str:
    lines = [f"circuit: {structure.circuit_name}", "", "blocks:"]

    drives: dict[str, list[str]] = {}
    senses: dict[str, list[str]] = {}
    for net, blocks in sorted(paths.net_blocks.items()):
        for name, role in sorted(blocks.items()):
            (drives if role == "drive" else senses).setdefault(name, []).append(net)

    for path in sorted(p for p in structure.blocks if p is not None):
        block = structure.blocks[path]
        name = _definition_name(path)
        lines.append(
            f"  {path}  {block.instance_count} instance(s)  "
            f"{len(block.components)} comps  "
            f"drives {','.join(drives.get(name, [])) or '-'}  "
            f"senses {','.join(senses.get(name, [])) or '-'}"
        )

    for path in sorted(focus | {None}, key=lambda p: (p is not None, p or "")):
        block = structure.blocks.get(path)
        if block is None:
            continue
        label = path or "<top level>"
        lines += ["", f"{label}  ports: {' '.join(block.ports) or '-'}"]
        for fact in block.components:
            terminals = " ".join(
                f"{t.name}={net}{'(sense)' if t.role == 'sense' else ''}"
                for t, net in zip(fact.terminals, fact.nodes)
            )
            lines.append(
                f"  {fact.refdes} {fact.model or fact.ctype}  {terminals or ' '.join(fact.nodes)}"
            )
        matched = [p for p in patterns if getattr(p, "block", None) == path]
        if matched:
            lines.append("  patterns: " + "  ".join(f"{p.kind}({','.join(p.members)})" for p in matched))
        addresses = [
            f"{e.refdes}.{e.param}"
            for e in structure.tunable
            if (e.refdes.rpartition(".")[0] or None) == path
        ]
        if addresses:
            lines.append("  tunable: " + " ".join(addresses) + "   (값은 넷리스트 원문에서 읽을 것)")

    for edge in paths.instances:
        if edge.mismatch:
            lines.append(f"WARNING: {edge.mismatch}")

    return "\n".join(lines)


def render_netlist(netlist_text: str, focus: set[str]) -> str:
    """초점 블록은 본문 전문, 비초점 블록은 헤더만 남기고 접는다. 최상위 줄은
    전부 남긴다. 축약 단위는 정의이므로 중첩 정의는 바깥이 접히면 함께 접힌다 -
    접힌 본문 안에 헤더만 남기면 어디에 속하는지 알 수 없는 조각이 된다."""
    names = {_definition_name(path) for path in focus}
    out: list[str] = []
    stack: list[str] = []
    elided = 0

    for raw_line in netlist_text.splitlines():
        stripped = raw_line.strip()
        lowered = stripped.lower()

        if lowered.startswith((".subckt", ".macro")):
            name = split_tokens(stripped)[1]
            folding = bool(stack) or name not in names
            if not stack or name in names:
                out.append(raw_line)
            stack.append(name)
            if folding and len(stack) == 1:
                elided = 0
            continue

        if lowered.startswith((".ends", ".eom")):
            name = stack.pop() if stack else ""
            if not stack and name not in names:
                # `*` 주석으로 쓴다. 이 텍스트는 프롬프트 전용이고 절대
                # ngspice로 가지 않지만, SPICE로 읽어도 무해한 형태여야
                # 사람이 붙여 넣어 볼 때 오해가 없다.
                out.append(f"* ... ({elided} components elided)")
                out.append(raw_line)
                elided = 0
            elif not stack:
                out.append(raw_line)
            continue

        if stack and stack[0] not in names:
            if stripped and not stripped.startswith("*"):
                elided += 1
            continue

        out.append(raw_line)

    return "\n".join(out)


def focus_misses(focus: set[str], changes: list[dict]) -> list[str]:
    """초점 밖 블록을 지목한 제안의 refdes. 그런 일이 일어났다는 것은 초점
    규칙이 놓쳤다는 증거이므로 기록해 둔다 - 제안 자체는 정상 적용된다."""
    misses = []
    for change in changes:
        refdes = change["refdes"]
        scope = refdes.rpartition(".")[0]
        if scope and scope not in focus:
            misses.append(refdes)
    return misses
```

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_structure_view.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: bandgap에서 실제 크기를 확인한다**

Run:
```bash
.venv/bin/python -c "
from analogcoder.structure import derive_structure
from analogcoder.signal_path import build_signal_paths
from analogcoder.structure_view import select_focus, render_structure, render_netlist
t = open('benchmarks/bandgap/netlist.cir').read()
s = derive_structure(t, 'bandgap'); p = build_signal_paths(s)
f = select_focus(s, p, {'vbg0'}, set())
print('focus:', sorted(f))
print('structure view chars:', len(render_structure(s, p, [], f)))
print('netlist view chars:', len(render_netlist(t, f)), 'of', len(t))
"
```
Expected: 초점에 `BUF_P`가 들어가고, 넷리스트 뷰가 원문(8818자)보다 짧다.
초점이 전 블록으로 나오면 `select_focus`의 씨앗 계산이나 Task 2의
`net_blocks`가 틀린 것이다.

- [ ] **Step 6: 커밋**

```bash
git add src/analogcoder/structure_view.py tests/unit/test_structure_view.py
git commit -m "feat: focus the derived view and the netlist on the failing blocks"
```

---

### Task 5: `check_param_applicability` — 새 결정론적 게이트

**Files:**
- Modify: `src/analogcoder/netlist.py` (`check_refdes_resolution` 바로 뒤)
- Test: `tests/unit/test_param_applicability.py`

**Interfaces:**
- Produces: `check_param_applicability(text: str, changes: list[dict]) -> tuple[bool, str | None]`
  — `check_refdes_resolution`과 동일한 시그니처/반환 규약

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/unit/test_param_applicability.py`:

```python
from analogcoder.netlist import check_param_applicability

PDK = (
    "* t\n"
    ".option scale=1.0u\n"
    ".subckt A vdd vss\n"
    "X6 d g vss vss sky130_fd_pr__pfet_01v8 L=1 W=20\n"
    ".ends A\n"
    "Xq1 0 0 na 0 sky130_fd_pr__pnp_05v5_W3p40L3p40\n"
    "Xq8 0 0 ne8 0 sky130_fd_pr__pnp_05v5_W3p40L3p40 m=8\n"
    "Rf a b 10k\n"
    "Xa vdd 0 A\n"
    ".end\n"
)


def test_a_param_present_on_the_component_line_is_applicable():
    assert check_param_applicability(PDK, [{"refdes": "A.X6", "param": "W"}]) == (True, None)


def test_a_param_that_exists_nowhere_is_rejected_with_the_names_that_do_exist():
    # 재현된 결함: param="width"는 조용히 width=55를 덧붙이고 소자는 그대로다.
    ok, feedback = check_param_applicability(PDK, [{"refdes": "A.X6", "param": "width"}])

    assert ok is False
    assert "width" in feedback and "W" in feedback and "L" in feedback


def test_a_param_a_peer_instance_uses_is_applicable_even_when_absent_here():
    # Xq8의 m=8이 Xq1.m을 정당화한다. 이것을 막으면 bandgap의 이미터 면적비가
    # 프로젝트 전체에서 도달 불가능해진다.
    assert check_param_applicability(PDK, [{"refdes": "Xq1", "param": "m"}]) == (True, None)


def test_value_is_applicable_when_the_positional_token_is_numeric():
    assert check_param_applicability(PDK, [{"refdes": "Rf", "param": "value"}]) == (True, None)


def test_value_is_rejected_when_the_positional_token_is_a_model_name():
    # param="value"로 덮어쓰면 sky130_fd_pr__pfet_01v8이 숫자가 되어 덱이 깨진다.
    ok, feedback = check_param_applicability(PDK, [{"refdes": "A.X6", "param": "value"}])

    assert ok is False
    assert "sky130_fd_pr__pfet_01v8" in feedback


def test_value_is_rejected_for_a_subckt_instance():
    ok, _ = check_param_applicability(PDK, [{"refdes": "Xa", "param": "value"}])

    assert ok is False


def test_every_violation_is_reported_not_just_the_first():
    ok, feedback = check_param_applicability(
        PDK, [{"refdes": "A.X6", "param": "width"}, {"refdes": "Xa", "param": "value"}]
    )

    assert ok is False
    assert "width" in feedback and "Xa" in feedback
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_param_applicability.py -q`
Expected: FAIL — `ImportError: cannot import name 'check_param_applicability'`

- [ ] **Step 3: 구현한다**

`src/analogcoder/netlist.py`의 `check_refdes_resolution` 바로 뒤에 추가:

```python
def _numeric_or_none(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return parse_spice_value(raw)
    except ValueError:
        return None


def check_param_applicability(text: str, changes: list[dict]) -> tuple[bool, str | None]:
    """결정론적 사전 게이트: 제안된 param이 그 소자에 실제로 적용될 수 있는가.

    오케스트레이터의 튜닝 재시도 루프에서 check_refdes_resolution 직후,
    verify_pre 직전에 돈다 - 에어리어/refdes 게이트와 같은 자리이자 같은
    철학이다. 적용 불가능한 제안은 LLM 호출을 쓰지 않는다.

    잡는 결함은 실측된 것이다: param="width"인 제안이 refdes 게이트를 통과해
    `X6 ... L=1 W=20 width=55`를 만들고, 넷리스트는 바뀌었는데 소자는 그대로라
    시뮬레이션에 변화가 없고 verify_post가 롤백한다 - 아무도 볼 수 없는
    이유로 iteration 하나를 태운다.

    줄에 없는 param을 무조건 거부하지는 않는다. bandgap의 Xq1에는 m=이
    없지만 같은 모델의 Xq8이 m=8을 쓰고, m은 이 회로의 이미터 면적비를 정하는
    유일한 노브다. 동료 인스턴스가 쓰는 이름은 정당한 것으로 본다 - 하드코딩된
    PDK 표 없이 덱만 보고 판정하므로 정확하고, width 같은 헛소리는 여전히
    걸린다."""
    parsed = parse_netlist(text)
    everything = list(parsed.top_components) + [
        c for subckt in parsed.subckts.values() for c in subckt.components
    ]
    by_refdes: dict[str, Component] = {}
    for component in everything:
        by_refdes[component.refdes] = component
        if component.scope:
            by_refdes[f"{component.scope}.{component.refdes}"] = component

    # 같은 모델명(없으면 같은 ctype)을 쓰는 소자들이 실제로 쓰는 param 이름.
    peers: dict[str, set[str]] = {}
    for component in everything:
        key = component.value if component.value else component.ctype
        peers.setdefault(key, set()).update(component.params)

    violations: list[str] = []
    for change in changes:
        scoped_refdes = change["refdes"]
        param = change["param"]
        component = by_refdes.get(scoped_refdes)
        if component is None:
            # refdes 게이트가 앞서 걸렀어야 한다. 여기서는 판단하지 않는다.
            continue

        if param == "value":
            if _numeric_or_none(component.value) is None:
                violations.append(
                    f"{scoped_refdes!r}: param=\"value\" would overwrite the positional token "
                    f"{component.value!r}, which is not a number - it is this component's model "
                    f"or subckt name. Change a named parameter instead."
                )
            continue

        if param in component.params:
            continue

        key = component.value if component.value else component.ctype
        if param in peers.get(key, set()):
            continue

        available = sorted(component.params) or ["<none>"]
        peer_names = sorted(peers.get(key, set()) - set(component.params))
        violations.append(
            f"{scoped_refdes!r}: {param!r} is not a parameter of this component. It writes "
            f"{available}; other {key!r} instances in this netlist write {peer_names or ['<none>']}. "
            f"Adding an unknown name changes the netlist text without changing the device."
        )

    if violations:
        return False, "; ".join(violations)
    return True, None
```

`parse_spice_value`(`netlist.py:495`)와 `Component`는 둘 다 이미 이 파일에 있다.
새 import가 필요 없다.

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_param_applicability.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: 전체 테스트를 돌린다**

Run: `.venv/bin/python -m pytest -q`
Expected: 기존 테스트가 모두 통과. 이 Task는 아직 오케스트레이터에 배선되지
않았으므로 회귀가 있으면 안 된다.

- [ ] **Step 6: 커밋**

```bash
git add src/analogcoder/netlist.py tests/unit/test_param_applicability.py
git commit -m "feat: reject a tuning param that cannot apply to its component"
```

---

### Task 6: analyzer 제거와 오케스트레이터 배선

**Files:**
- Delete: `src/analogcoder/agents/analyzer.py`, `tests/unit/test_analyzer_agent.py`
- Modify: `src/analogcoder/orchestrator.py`, `src/analogcoder/cli.py`,
  `src/analogcoder/schemas.py`
- Test: `tests/unit/test_orchestrator.py`, `tests/unit/test_cli.py`,
  `tests/unit/test_schemas.py`

**Interfaces:**
- Consumes: Task 1–5의 모든 공개 함수
- Produces: `OrchestratorAgents`에서 `analyze` 필드가 사라진 형태.
  `tune`/`verify_pre`/`propose_topology`의 첫 인자는 dict가 아니라 렌더링된
  **문자열**이 된다.

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/unit/test_orchestrator.py`에 추가. 기존 `make_spec`을 먼저 고친다 —
`derive_structure`가 `circuit_name`을, 초점이 `control_block`과 `criteria`의
`measurement`를 필요로 한다.

```python
def make_spec(*testbench_names):
    testbenches = [
        SimpleNamespace(name=n, criteria=[], control_block=".control\n.endc\n")
        for n in testbench_names
    ]
    return SimpleNamespace(
        circuit_name="fake", testbenches=testbenches, canonical=testbenches[0]
    )
```

그리고 새 테스트:

```python
def test_the_orchestrator_no_longer_calls_an_analyzer_agent():
    # analyzer는 결정론적 파생으로 대체됐다. dataclass에 필드가 남아 있으면
    # cli.py가 조용히 예전 배선을 유지할 수 있다.
    import dataclasses

    assert "analyze" not in {f.name for f in dataclasses.fields(OrchestratorAgents)}


@pytest.mark.asyncio
async def test_the_tuner_receives_a_rendered_structure_not_an_llm_analysis(tmp_path):
    seen = {}
    judge_calls = {"count": 0}

    async def judge_fails_then_passes(measurements, spec):
        judge_calls["count"] += 1
        return FAIL_JUDGE if judge_calls["count"] == 1 else PASS_JUDGE

    async def capturing_tune(structure_view, judge_result, history, feedback, netlist_view):
        seen["structure"] = structure_view
        seen["netlist"] = netlist_view
        return FAKE_PROPOSAL

    agents = make_agents(tune=capturing_tune, judge=judge_fails_then_passes)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    assert isinstance(seen["structure"], str)
    assert "circuit: fake" in seen["structure"]
    assert "Rf" in seen["netlist"]


@pytest.mark.asyncio
async def test_an_inapplicable_param_is_rejected_before_verify_pre_is_called(tmp_path):
    verify_pre_calls = {"count": 0}

    async def counting_verify_pre(structure_view, judge_result, proposal, netlist_view):
        verify_pre_calls["count"] += 1
        return {"approved": True, "concerns": [], "feedback": ""}

    async def bad_tune(structure_view, judge_result, history, feedback, netlist_view):
        return {"proposed_changes": [{"refdes": "Rf", "param": "width",
                                      "old_value": "10k", "new_value": "15k"}]}

    agents = make_agents(
        tune=bad_tune,
        verify_pre=counting_verify_pre,
        judge=lambda m, s: _async(FAIL_JUDGE),
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    assert verify_pre_calls["count"] == 0
    events = [json.loads(line) for line in open(state.history_path)]
    assert any(e["step"] == "param_check" and e["approved"] is False for e in events)


@pytest.mark.asyncio
async def test_the_focus_decision_is_logged_so_an_elision_is_never_invisible(tmp_path):
    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE))
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": SUBCKT_NETLIST}, FAKE_SPEC, state, agents)

    events = [json.loads(line) for line in open(state.history_path)]
    focus_events = [e for e in events if e["step"] == "focus"]
    assert focus_events
    assert focus_events[0]["blocks"] == ["AMP"]
```

이 파일에는 `_async(value)` 헬퍼와 `make_agents(**overrides)`가 이미 있고,
순차 반환이 필요한 곳은 카운터를 든 인라인 클로저로 쓰는 것이 이 파일의
관례다. `json` import를 파일 상단에 추가한다.

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py -q`
Expected: FAIL — `analyze` 필드가 아직 존재하고, `param_check`/`focus` 이벤트가
없다.

- [ ] **Step 3: 구현한다**

`src/analogcoder/orchestrator.py`:

```python
from analogcoder.control_block import measurement_nets
from analogcoder.netlist import (
    apply_changes,
    apply_topology_swap,
    check_param_applicability,
    check_refdes_resolution,
    parse_netlist,
)
from analogcoder.signal_path import build_signal_paths
from analogcoder.structure import derive_structure
from analogcoder.structure_view import focus_misses, render_netlist, render_structure, select_focus
```

`OrchestratorAgents`에서 `analyze: Callable` 줄을 지운다.

`run_orchestration` 안에서 `analysis = await agents.analyze(...)` 와 그
`state.log_event("analysis", analysis)` 를 지우고, 대신 매 iteration 시작
부분(`netlist_texts = state.current_netlist_texts()` 바로 뒤)에 다음을 넣는다:

```python
            # 파생은 결정론적 파이썬이므로 매 iteration 다시 계산해도 비용이
            # 없다. analyzer가 LLM 호출이었을 때와 달리 캐시할 이유가 없다.
            structure = derive_structure(netlist_texts[canonical_name], spec.circuit_name)
            paths = build_signal_paths(structure)
```

그리고 `judge_result`가 나온 뒤, `agents.tune` 호출 앞에:

```python
            measurement_by_criterion = {
                c.name: c.measurement for tb in spec.testbenches for c in tb.criteria
            }
            nets_by_measurement: dict[str, set[str]] = {}
            for tb in spec.testbenches:
                nets_by_measurement.update(measurement_nets(tb.control_block))

            failing_nets: set[str] = set()
            for criterion in judge_result["criteria"]:
                if criterion["pass"]:
                    continue
                measurement = measurement_by_criterion.get(criterion["name"])
                failing_nets |= nets_by_measurement.get(measurement, set())

            touched_refdes = {
                change["refdes"]
                for entry in tuning_history
                for change in entry["proposal"]["proposed_changes"]
            }
            focus = select_focus(structure, paths, failing_nets, touched_refdes)
            structure_view = render_structure(structure, paths, [], focus)
            netlist_view = render_netlist(netlist_texts[canonical_name], focus)
            state.log_event(
                "focus",
                {
                    "outer_iter": outer_iter,
                    "blocks": sorted(focus),
                    "netlist_chars": len(netlist_view),
                    "netlist_chars_full": len(netlist_texts[canonical_name]),
                },
            )
```

`agents.tune`/`agents.verify_pre`/`agents.propose_topology` 호출의 `analysis`
인자를 `structure_view`로 바꾸고, `agents.tune`/`agents.verify_pre`의
넷리스트 인자를 `netlist_texts[canonical_name]`에서 `netlist_view`로 바꾼다.

`check_refdes_resolution` 블록 바로 뒤에 새 게이트를 넣는다. **원본 전문을
넘긴다** — 게이트는 초점과 무관하게 판정해야 한다:

```python
                param_ok, param_feedback = check_param_applicability(
                    netlist_texts[canonical_name], proposal["proposed_changes"]
                )
                state.log_event(
                    "param_check",
                    {"outer_iter": outer_iter, "retry": retry,
                     "approved": param_ok, "feedback": param_feedback},
                )
                if not param_ok:
                    rejection_feedback = param_feedback
                    continue

                misses = focus_misses(focus, proposal["proposed_changes"])
                if misses:
                    state.log_event(
                        "focus_miss",
                        {"outer_iter": outer_iter, "retry": retry, "refdes": misses},
                    )
```

`focus_miss`는 기록만 하고 흐름을 막지 않는다. 초점이 틀렸다는 증거이지
제안이 틀렸다는 증거가 아니다.

토폴로지 스왑 경로에서 `pre_swap_analysis`/`analysis = await agents.analyze(...)`
를 지운다. 스왑 후 넷리스트는 `state.push_netlist_version` 다음 iteration의
맨 위에서 다시 파생되므로 재계산 코드를 따로 넣을 필요가 없다. 다만 스왑
직후에도 `verify_post`까지 같은 iteration 안에서 진행하므로,
`propose_topology` 호출은 그 iteration의 `structure_view`를 그대로 쓴다.

`src/analogcoder/cli.py`:
- `from analogcoder.agents.analyzer import analyze_netlist` 삭제
- `AGENT_NAMES = ("simulator", "judge", "tuner", "verifier")`
- `analyze_fn` 정의와 `OrchestratorAgents(analyze=analyze_fn, ...)`의 해당 인자 삭제
- `tune_fn`/`verify_pre_fn`/`propose_topology_fn`의 첫 인자 이름을 `analysis`에서
  `structure_view`로 바꾼다 (동작은 그대로 통과시키기만 한다)

`src/analogcoder/schemas.py`: `ANALYZER_SCHEMA` 삭제.

`src/analogcoder/agents/tuner.py`·`verifier.py`의 프롬프트에서 `Circuit
analysis:` 라벨을 `Circuit structure (derived deterministically):`로 바꾸고,
튜너 시스템 프롬프트의 `Only propose changes to parameters listed in
tunable_params.` 문장을 `Only propose changes to parameters listed under
"tunable" in the structure above.`로 바꾼다. `verify_pre`의 param 관련 문단은
**남긴다** — 결정론적 게이트가 앞에 섰어도, refdes 게이트가 들어왔을 때
refdes 문단을 남긴 것과 같은 belt-and-braces 방침이다.

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py -q`
Expected: PASS

- [ ] **Step 5: analyzer 잔재를 지우고 전체를 돌린다**

```bash
git rm src/analogcoder/agents/analyzer.py tests/unit/test_analyzer_agent.py
grep -rn "analyzer\|ANALYZER" src tests | grep -v ".pyc"
```

남은 참조를 전부 지운 뒤:

Run: `.venv/bin/python -m pytest -q`
Expected: 전부 통과. analyzer 테스트가 빠졌으므로 총계는 줄어든다.

- [ ] **Step 6: 커밋**

```bash
git add -A
git commit -m "feat: replace the analyzer agent with deterministic derivation"
```

---

### Task 7: `patterns.py` — 네 가지 지역 매칭

**Files:**
- Create: `src/analogcoder/patterns.py`
- Modify: `src/analogcoder/orchestrator.py` (렌더러에 실제 매칭 전달)
- Test: `tests/unit/test_patterns.py`

**Interfaces:**
- Consumes: `structure.NetlistStructure`, `structure.ComponentFact`
- Produces:
  - `PatternMatch(kind: str, block: str | None, members: list[str], detail: str)`
  - `find_patterns(structure: NetlistStructure) -> list[PatternMatch]`
  - `kind`는 `"diff_pair"`, `"current_mirror"`, `"cascode"`,
    `"miller_compensation"` 중 하나

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/unit/test_patterns.py`:

```python
from analogcoder.patterns import find_patterns
from analogcoder.structure import derive_structure


def _kinds(deck):
    return {(m.kind, tuple(m.members)) for m in find_patterns(derive_structure(deck, "t"))}


def test_a_matched_differential_pair_is_reported():
    deck = (
        "* t\n"
        "M1 nx vinn tail vss NMOS W=48 L=1\n"
        "M2 ny vinp tail vss NMOS W=48 L=1\n"
        ".end\n"
    )

    assert ("diff_pair", ("M1", "M2")) in _kinds(deck)


def test_two_devices_sharing_a_source_but_not_matched_are_not_a_pair():
    # W가 다르면 차동쌍이 아니다. 침묵이 정답이고, 추측한 매칭은 사실보다 나쁘다.
    deck = (
        "* t\n"
        "M1 nx vinn tail vss NMOS W=48 L=1\n"
        "M2 ny vinp tail vss NMOS W=8 L=1\n"
        ".end\n"
    )

    assert not any(kind == "diff_pair" for kind, _ in _kinds(deck))


def test_two_devices_sharing_a_gate_with_one_diode_connected_are_a_mirror():
    deck = (
        "* t\n"
        "M1 nb nb vss vss NMOS W=10 L=1\n"
        "M2 out nb vss vss NMOS W=10 L=1\n"
        ".end\n"
    )

    assert ("current_mirror", ("M1", "M2")) in _kinds(deck)


def test_a_shared_gate_without_a_diode_connection_is_not_a_mirror():
    deck = (
        "* t\n"
        "M1 na nb vss vss NMOS W=10 L=1\n"
        "M2 out nb vss vss NMOS W=10 L=1\n"
        ".end\n"
    )

    assert not any(kind == "current_mirror" for kind, _ in _kinds(deck))


def test_a_device_stacked_on_another_drain_with_a_bias_gate_is_a_cascode():
    deck = (
        "* t\n"
        "M1 mid vin vss vss NMOS W=10 L=1\n"
        "M2 out ncas mid vss NMOS W=10 L=1\n"
        ".end\n"
    )

    assert ("cascode", ("M1", "M2")) in _kinds(deck)


def test_a_cap_between_a_gain_stage_input_gate_and_its_output_drain_is_miller():
    deck = (
        "* t\n"
        "M6 vout outA vss vss NMOS W=40 L=1\n"
        "Cc outA vout 3p\n"
        ".end\n"
    )

    assert ("miller_compensation", ("Cc", "M6")) in _kinds(deck)


def test_a_series_nulling_resistor_is_reported_with_the_miller_cap():
    deck = (
        "* t\n"
        "M6 vout outA vss vss NMOS W=40 L=1\n"
        "Cc outA nz 3p\n"
        "Rz nz vout 220k\n"
        ".end\n"
    )
    matches = [m for m in find_patterns(derive_structure(deck, "t")) if m.kind == "miller_compensation"]

    assert matches and "Rz" in matches[0].members


def test_a_decoupling_cap_to_ground_is_not_miller_compensation():
    deck = (
        "* t\n"
        "M6 vout outA vss vss NMOS W=40 L=1\n"
        "Cd vout 0 3p\n"
        ".end\n"
    )

    assert not any(kind == "miller_compensation" for kind, _ in _kinds(deck))


def test_matches_are_scoped_to_their_block():
    deck = (
        "* t\n"
        ".subckt AMP vss\n"
        "M1 nx vinn tail vss NMOS W=48 L=1\n"
        "M2 ny vinp tail vss NMOS W=48 L=1\n"
        ".ends AMP\n"
        ".end\n"
    )

    match = find_patterns(derive_structure(deck, "t"))[0]

    assert match.block == "AMP"
    assert match.members == ("AMP.M1", "AMP.M2")


def test_pattern_finding_is_deterministic():
    deck = (
        "* t\n"
        "M1 nb nb vss vss NMOS W=10 L=1\n"
        "M2 out nb vss vss NMOS W=10 L=1\n"
        ".end\n"
    )
    s = derive_structure(deck, "t")

    assert find_patterns(s) == find_patterns(s)
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_patterns.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'analogcoder.patterns'`

- [ ] **Step 3: 구현한다**

`src/analogcoder/patterns.py`:

```python
from dataclasses import dataclass
from itertools import combinations

from analogcoder.structure import BlockStructure, ComponentFact, NetlistStructure

_MOS_CLASSES = ("nfet", "pfet")


@dataclass(frozen=True)
class PatternMatch:
    kind: str
    block: str | None
    members: tuple[str, ...]
    detail: str


def _is_mos(fact: ComponentFact) -> bool:
    return len(fact.terminals) == 4 and [t.name for t in fact.terminals] == ["d", "g", "s", "b"]


def _nets(fact: ComponentFact) -> dict[str, str]:
    return {t.name: net for t, net in zip(fact.terminals, fact.nodes)}


def _same_kind(a: ComponentFact, b: ComponentFact) -> bool:
    """같은 소자 종류인가. 모델 이름이 있으면 그것으로, 없으면 ctype으로 본다.
    nfet과 pfet을 짝지으면 안 되므로 이 비교는 느슨해서는 안 된다."""
    return (a.model or a.ctype) == (b.model or b.ctype)


def _two_terminal_nets(fact: ComponentFact) -> tuple[str, str] | None:
    if len(fact.nodes) != 2:
        return None
    return fact.nodes[0], fact.nodes[1]


def _find_in_block(block: BlockStructure) -> list[PatternMatch]:
    matches: list[PatternMatch] = []
    mos = [f for f in block.components if _is_mos(f)]

    for a, b in combinations(mos, 2):
        na, nb = _nets(a), _nets(b)
        if not _same_kind(a, b):
            continue
        if (
            na["s"] == nb["s"]
            and na["g"] != nb["g"]
            and na["d"] != nb["d"]
            and a.params.get("W") == b.params.get("W")
            and a.params.get("L") == b.params.get("L")
        ):
            matches.append(PatternMatch(
                kind="diff_pair", block=block.path,
                members=tuple(sorted((a.refdes, b.refdes))),
                detail=f"common source {na['s']}, gates {na['g']}/{nb['g']}",
            ))
        if na["g"] == nb["g"] and na["s"] == nb["s"] and (
            na["g"] == na["d"] or nb["g"] == nb["d"]
        ):
            diode = a if na["g"] == na["d"] else b
            matches.append(PatternMatch(
                kind="current_mirror", block=block.path,
                members=tuple(sorted((a.refdes, b.refdes))),
                detail=f"shared gate {na['g']}, {diode.refdes} is diode-connected",
            ))

    for upper, lower in combinations(mos, 2):
        for top, bottom in ((upper, lower), (lower, upper)):
            nt, nbm = _nets(top), _nets(bottom)
            if not _same_kind(top, bottom):
                continue
            if nt["s"] == nbm["d"] and nt["g"] != nt["s"] and nt["g"] != nbm["g"]:
                matches.append(PatternMatch(
                    kind="cascode", block=block.path,
                    members=tuple(sorted((top.refdes, bottom.refdes))),
                    detail=f"{top.refdes} stacked on {bottom.refdes} at {nt['s']}, bias {nt['g']}",
                ))

    # 밀러 보상: 커패시터가 어떤 이득단의 입력 게이트와 출력 드레인을 잇는다.
    # 직렬 저항이 끼어 있으면 저항 너머까지 한 단계 따라간다.
    caps = [f for f in block.components if f.ctype == "C" or (f.device_class == "cap")]
    resistors = [f for f in block.components if f.ctype == "R" or (f.device_class == "res")]
    for cap in caps:
        endpoints = _two_terminal_nets(cap)
        if endpoints is None:
            continue
        # 커패시터의 두 끝은 대칭이다. 한쪽만 고정하면 Cc가 어느 방향으로
        # 적혔는지에 따라 매칭이 되기도 하고 안 되기도 한다.
        for near, other in (endpoints, tuple(reversed(endpoints))):
            for far_side, extra in _reachable(other, resistors):
                for device in mos:
                    nets = _nets(device)
                    if {near, far_side} != {nets["g"], nets["d"]}:
                        continue
                    members = tuple(sorted((cap.refdes, device.refdes) + tuple(extra)))
                    matches.append(PatternMatch(
                        kind="miller_compensation", block=block.path, members=members,
                        detail=f"{cap.refdes} bridges {nets['g']} and {nets['d']} of {device.refdes}",
                    ))

    return matches


def _reachable(net: str, resistors: list[ComponentFact]) -> list[tuple[str, tuple[str, ...]]]:
    """넷 자신과, 직렬 저항 하나를 건넌 넷들. 두 개 이상은 따라가지 않는다 -
    보상 회로가 아닌 저항 네트워크를 밀러로 오인하기 시작하는 지점이다."""
    out = [(net, ())]
    for resistor in resistors:
        pair = _two_terminal_nets(resistor)
        if pair is None:
            continue
        if pair[0] == net:
            out.append((pair[1], (resistor.refdes,)))
        elif pair[1] == net:
            out.append((pair[0], (resistor.refdes,)))
    return out


def find_patterns(structure: NetlistStructure) -> list[PatternMatch]:
    """네 가지 지역 서브그래프 매칭. **절대 추측하지 않는다** - 매칭되면
    사실이고, 매칭되지 않으면 침묵이다. LLM이 스키마를 만족시키려고
    {"a": "b"}를 채워 넣던 것의 정확한 반대편이며, 받아들이는 기준도 재현율이
    아니라 거짓 양성 0이다.

    세 파생 모듈 중 유일하게 틀릴 수 있는 부분이므로 따로 두었다.
    서브프로젝트 F(토폴로지 라이브러리 확장)가 자라날 자리이기도 하다."""
    matches: list[PatternMatch] = []
    for path in sorted(structure.blocks, key=lambda p: (p is not None, p or "")):
        matches += _find_in_block(structure.blocks[path])
    # 대칭 탐색은 같은 매칭을 두 번 낼 수 있다. PatternMatch가 frozen이라
    # dict.fromkeys로 순서를 지키며 중복만 걷어낼 수 있다.
    return sorted(dict.fromkeys(matches), key=lambda m: (m.kind, m.block or "", m.members))
```

`PatternMatch.members`는 튜플이다 — `structure_view.render_structure`의
`','.join(p.members)`는 튜플로도 그대로 동작한다.

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_patterns.py -q`
Expected: PASS (10 passed)

- [ ] **Step 5: 오케스트레이터에 배선한다**

`orchestrator.py`에서 `from analogcoder.patterns import find_patterns`를
추가하고, Task 6에서 빈 목록을 넘기던 자리를 바꾼다:

```python
            structure_view = render_structure(structure, paths, find_patterns(structure), focus)
```

Run: `.venv/bin/python -m pytest -q`
Expected: 전부 통과.

- [ ] **Step 6: 실제 벤치마크에서 거짓 양성을 눈으로 확인한다**

Run:
```bash
.venv/bin/python -c "
from analogcoder.structure import derive_structure
from analogcoder.patterns import find_patterns
for f in ['benchmarks/two_stage_opamp/netlist.cir','benchmarks/bandgap/netlist.cir']:
    print('===', f)
    for m in find_patterns(derive_structure(open(f).read(), 't')):
        print(' ', m.kind, m.block, m.members, '|', m.detail)
"
```
Expected: `two_stage_opamp`에서 차동쌍과 미러와 밀러 보상이 잡힌다. bandgap의
네 증폭기에서 폴디드 캐스코드가 잡힌다. **잡히지 않아야 할 것이 잡혔는지를
눈으로 확인하는 것이 이 단계의 목적이다** — 거짓 양성이 있으면 매칭 조건을
좁히고 좁힌 이유를 테스트로 남긴다. 재현율은 기준이 아니므로, 놓친 것은
그대로 두어도 된다.

- [ ] **Step 7: 커밋**

```bash
git add src/analogcoder/patterns.py tests/unit/test_patterns.py src/analogcoder/orchestrator.py
git commit -m "feat: match diff pairs, mirrors, cascodes and Miller compensation"
```

---

### Task 8: 골든 스냅샷, 결정론, 문서

**Files:**
- Create: `tests/unit/test_structure_golden.py`,
  `tests/fixtures/structure_golden/*.json`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: Task 1–7 전부

- [ ] **Step 1: 스냅샷 테스트를 작성한다**

`tests/unit/test_structure_golden.py`. `tests/unit/test_netlist_golden.py`의
구조를 그대로 따른다:

```python
import json
import os

import pytest

from analogcoder.patterns import find_patterns
from analogcoder.signal_path import build_signal_paths
from analogcoder.structure import derive_structure

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures", "structure_golden")

# test_netlist_golden.py와 같은 10개 덱. 목록이 줄어들면 커버리지가 조용히
# 사라지므로 개수도 함께 단언한다.
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


def _snapshot(text: str) -> dict:
    structure = derive_structure(text, "golden")
    paths = build_signal_paths(structure)
    return {
        "blocks": {
            (path or "<top>"): {
                "ports": block.ports,
                "instance_count": block.instance_count,
                "components": [
                    {
                        "refdes": f.refdes,
                        "ctype": f.ctype,
                        "device_class": f.device_class,
                        "model": f.model,
                        "nodes": f.nodes,
                        "terminals": [[t.name, t.role] for t in f.terminals],
                    }
                    for f in block.components
                ],
            }
            for path, block in sorted(structure.blocks.items(), key=lambda kv: kv[0] or "")
        },
        "tunable": sorted([e.refdes, e.param] for e in structure.tunable),
        "net_blocks": {net: dict(sorted(b.items())) for net, b in sorted(paths.net_blocks.items())},
        "mismatches": sorted(e.mismatch for e in paths.instances if e.mismatch),
        "patterns": sorted(
            [m.kind, m.block or "<top>", list(m.members)] for m in find_patterns(structure)
        ),
    }


def _golden_path(rel: str) -> str:
    return os.path.join(GOLDEN_DIR, rel.replace("/", "__") + ".json")


def test_the_golden_set_covers_every_benchmark_netlist():
    assert len(NETLISTS) == 10
    for rel in NETLISTS:
        assert os.path.exists(os.path.join(REPO, rel)), rel


@pytest.mark.parametrize("rel", NETLISTS)
def test_derived_structure_matches_the_golden_snapshot(rel):
    with open(os.path.join(REPO, rel)) as f:
        actual = _snapshot(f.read())
    with open(_golden_path(rel)) as f:
        expected = json.load(f)

    assert actual == expected, (
        f"{rel}의 파생 결과가 골든 스냅샷과 다르다. 의도한 변경이라면 스냅샷을 "
        f"다시 만들되, 무엇이 왜 바뀌었는지 커밋 메시지에 적을 것."
    )


@pytest.mark.parametrize("rel", NETLISTS)
def test_derivation_is_reproducible(rel):
    # analyzer는 같은 bandgap 넷리스트에 대해 component_roles를 93개, 26개,
    # 1개로 냈다. 이 테스트가 그것과 대비되는 지점이며, E2가 존재하는 이유다.
    with open(os.path.join(REPO, rel)) as f:
        text = f.read()

    assert _snapshot(text) == _snapshot(text)
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_structure_golden.py -q`
Expected: FAIL — 골든 파일이 없어 `FileNotFoundError`

- [ ] **Step 3: 골든 파일을 생성한다**

```bash
mkdir -p tests/fixtures/structure_golden
.venv/bin/python -c "
import json, os, sys
sys.path.insert(0, 'tests/unit')
from test_structure_golden import NETLISTS, REPO, _snapshot, _golden_path
for rel in NETLISTS:
    with open(os.path.join(REPO, rel)) as f:
        snap = _snapshot(f.read())
    with open(_golden_path(rel), 'w') as f:
        json.dump(snap, f, indent=2, sort_keys=True)
        f.write('\n')
print('wrote', len(NETLISTS))
"
```

- [ ] **Step 4: 생성된 스냅샷을 사람이 검토한다**

이 단계는 자동화할 수 없고, **건너뛰면 이 Task 전체가 무의미하다.**
`tests/fixtures/structure_golden/benchmarks__bandgap__netlist.cir.json`과
`...two_stage_opamp__netlist.cir.json`을 열어 다음을 확인한다:

1. `patterns`에 **잡히지 않아야 할 것이 없는가.** 거짓 양성 0이 받아들임
   기준이다. 있으면 매칭 조건을 좁히고 Task 7의 테스트에 이유를 남긴 뒤
   스냅샷을 다시 만든다.
2. `net_blocks`에서 `vbg0`을 `BUF_P`가, `vbg1`을 `BUF_N`이 구동하는가.
3. `mismatches`가 비어 있는가. 비어 있지 않으면 벤치마크 넷리스트에 실제
   버그가 있다는 뜻이므로 스냅샷을 고정하기 전에 그것부터 확인한다.
4. `tunable`에 모델명을 값으로 갖는 `("...", "value")` 항목이 없는가.

- [ ] **Step 5: 테스트를 돌려 통과를 확인한다**

Run: `.venv/bin/python -m pytest -q`
Expected: 전부 통과.

- [ ] **Step 6: `CLAUDE.md`를 갱신한다**

"Architecture" 절에 다음을 추가한다 (기존 항목들과 같은 밀도로):

```markdown
- `structure.py` / `signal_path.py` / `patterns.py` / `control_block.py` /
  `structure_view.py` — the deterministic replacement for what used to be an
  LLM `analyzer` agent. That agent contributed nothing measurable: a run
  passed in 4 iterations on an analysis that was `{"circuit_type": "test",
  "component_roles": {"a": "b"}, ...}`, and across runs on one bandgap
  netlist it produced 93, 26 and 1 component roles. The tuner succeeds by
  reading `netlist_text`, which it still receives. `structure.py` derives
  flat per-scope facts (inventory, device classes, the complete tunable
  `(refdes, param)` index, per-net terminal roles); `signal_path.py` maps
  ports to nets across hierarchy and labels each net's drivers and sensors
  by *definition* name, since a definition is what the tuner can address;
  `patterns.py` matches differential pairs, current mirrors, cascodes and
  Miller compensation. **Patterns never guess** - a match is a fact, a
  non-match is silence, and the acceptance bar is zero false positives, not
  recall. It is the only one of the three that can be wrong, which is why it
  is a separate module. See
  `docs/superpowers/specs/2026-07-27-netlist-structure-derivation-design.md`.
- **The prompt is focused; the gates never are.** `structure_view.py` picks
  the blocks reachable from the failing criteria's nets (via
  `control_block.py`, which resolves a measurement name to the nets its
  `meas`/`let` lines observe) and renders every block at one line, focused
  blocks in full, and the netlist itself with unfocused `.subckt` bodies
  folded away. `check_area_growth`, `check_refdes_resolution` and
  `check_param_applicability` always read the whole deck, so a wrong focus
  costs relevance, never correctness. A proposal naming a block outside
  focus is logged as `focus_miss` - that is the signal the focus rule missed
  something. Benchmark decks are small enough that this path barely fires;
  it exists for real production decks of hundreds of lines, where the raw
  netlist no longer fits a context-limited model.
- **A tuning `param` must be able to apply.** `check_param_applicability`
  (in `netlist.py`, run right after `check_refdes_resolution`) rejects
  `param="value"` when the positional token is a model or subckt name, and
  rejects a named parameter that appears neither on the component's own line
  nor on any same-model peer in the deck. The peer rule is what keeps
  `Xq1.m` reachable in `benchmarks/bandgap` - `Xq1` writes no `m=` but
  `Xq8` writes `m=8`, and `m` is the only knob that sets the emitter-area
  ratio. Without this gate a proposal like `param="width"` appends
  `width=55`, changes the netlist, does not change the device, and burns an
  iteration on a rollback nobody can explain.
```

그리고 "Known limitations" 절의 `param` 항목에 한 줄을 덧붙인다: 이제
`check_param_applicability`가 이 경우를 결정론적으로 막지만, `verify_pre`의
지시문도 belt-and-braces로 남아 있다.

- [ ] **Step 7: 커밋**

```bash
git add tests/unit/test_structure_golden.py tests/fixtures/structure_golden CLAUDE.md
git commit -m "test+docs: pin derived structure for all ten benchmark decks"
```

---

## 완료 기준

- [ ] `.venv/bin/python -m pytest -q` 전부 통과
- [ ] `grep -rn "analyzer\|ANALYZER" src tests`가 아무것도 내지 않음
- [ ] 10개 덱의 골든 스냅샷이 존재하고, 그중 bandgap과 two_stage_opamp를
      사람이 검토했음
- [ ] `patterns` 거짓 양성 0 (검토로 확인)
- [ ] `history.jsonl`에 `focus`, `param_check` 이벤트가 남고, 초점 밖 제안이
      나오면 `focus_miss`가 남음

## 이 계획이 다루지 않는 것

- **약한 모델(Ollama) 비교 실행.** 합의된 E2 성공 기준에 있으나 스펙이 범위
  밖으로 뒀다. 결정론적 검증이 끝난 뒤 별도로 수행한다.
- **기준선 재측정.** 원문 렌더링을 초점으로 통일했으므로 기록된 벤치마크
  수치(bandgap seed 1/1/4 iteration, `two_stage_opamp` 3 iteration)는 더 이상
  그대로의 기준선이 아니다. 머지 전 실런 여부는 그 시점에 판단한다.
