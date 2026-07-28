# 토폴로지 큐레이션 (F2) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 라이브러리에 새 토폴로지를 넣는 경로를 만든다 — 본문을 추출/제출/저술로 얻고, 측정으로 입회 여부를 판정하며, 판정의 범위를 항상 함께 기록한다.

**Architecture:** 새 모듈 `curation.py`가 단계별 순수 함수로 게이트를 이루고, `cli_curate.py`가 그것을 순서대로 돌려 산출물 셋을 낸다. LLM은 둘뿐 — 변형 저술(소스 C)과 `description` 렌더링 — 이고 **둘 다 없어도 큐레이션은 산출물을 낸다**.

**Tech Stack:** Python 3, dataclasses, pytest, ngspice

**설계 문서:** `docs/superpowers/specs/2026-07-28-topology-curation-design.md` (커밋 `3519e4d`). 측정된 수치는 전부 거기 있다 — **다시 측정하지 말 것.**

## Global Constraints

- **테스트가 요구사항이고, 브리프의 코드 스케치는 제안일 뿐이다.** 스케치가 산문 규칙과 어긋나면 **산문이 이긴다**. 이 저장소의 지난 세 계획에서 브리프의 절반 이상이 결함 있는 스케치를 담았다.
- **새 테스트마다 "이 테스트는 어떤 변형을 잡는가"를 답할 것.** 답을 **실제로 변형을 적용해서** 확인한다. 지난 브랜치에서 무효 테스트 4건이 전부 이 방법으로만 잡혔다.
- **추측 금지.** 이름으로 의미를 알아보는 규칙은 금지. 판정은 파스된 사실에만 근거한다.
- **모든 단계는 통과했을 때도 기록한다.** 위반 시에만 기록하면 "검사했고 문제없음"과 "검사가 사라짐"이 구별되지 않는다. 이 저장소는 조용히 아무것도 안 하는 게이트를 **여섯 번** 출하했다.
- **판정은 셋이다: `ADMIT` / `REJECT` / `INCONCLUSIVE`.** "이 회로가 나쁘다"와 "재보지 못했다"는 다른 사실이다. 어떤 예외도 큐레이션을 트레이스백으로 끝내지 않는다.
- **게이트가 주장하지 않는 것을 결과에 적는다.** 3단은 한 번에 한 노브만 본다. "모든 파라미터 튜닝을 배제했다"는 문장은 어디에도 쓰지 않는다.
- 파이썬은 `.venv/bin/python`. 테스트는 `.venv/bin/python -m pytest`.
- 커밋 메시지는 한글, 마지막 줄에 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## 파일 구조

| 파일 | 책임 |
|---|---|
| `src/analogcoder/topologies.py` (수정) | `Topology`에 `provenance`/`verified_at` |
| `src/analogcoder/topology_match.py` (수정) | 포트 부분집합 완화 + 부동 넷 검사 |
| `src/analogcoder/curation.py` (신규) | 게이트 단계들. 순수 함수, LLM 없음 |
| `src/analogcoder/agents/curator.py` (신규) | `description` 렌더링 + 템플릿 폴백 |
| `src/analogcoder/agents/variant_author.py` (신규) | 소스 C 저술 |
| `src/analogcoder/cli_curate.py` (신규) | `analogcoder-curate` 진입점, 산출물 3종 |
| `benchmarks/bandgap/spec_curate_slot.yaml` (신규) | 코너 축을 가진 단일 테스트벤치 슬롯 스펙 |

## 공통 인터페이스 (모든 태스크가 이 이름을 쓴다)

```python
# curation.py
@dataclass(frozen=True)
class Candidate:
    topology_id: str
    subckt_body: str
    ports: list[str]
    assumes_scale: float
    provenance: str            # "extracted" | "file" | "authored"

@dataclass(frozen=True)
class Slot:
    spec: "TargetSpec"
    spec_dir: Path
    block_path: str

@dataclass
class StageResult:
    name: str                  # "structure" | "reproduce" | "corners" | "comparison"
    status: str                # "pass" | "fail" | "skipped" | "inconclusive"
    detail: dict               # 항상 채운다 - 통과했을 때도

@dataclass
class CurationResult:
    verdict: str               # "ADMIT" | "REJECT" | "INCONCLUSIVE"
    reason: str
    stages: list[StageResult]
    addresses: list[str]
    description: str
    description_source: str    # "agent" | "template"
```

**단계 순서는 고정이다:** structure → reproduce(후보와 기존 본문 둘 다 측정, 여기서 `addresses` 산출) → corners(저술본만) → comparison → description.

---

### Task 1: 라이브러리 표면과 포트 부분집합 완화

**Files:**
- Modify: `src/analogcoder/topologies.py`, `src/analogcoder/topology_match.py`
- Test: `tests/unit/test_topologies.py`, `tests/unit/test_topology_match.py`

**Interfaces:**
- Produces: `Topology(..., provenance: str, verified_at: str)`; `compatible_swaps`가 후보 ⊆ 블록을 허용

**규칙 (산문이 구속력을 갖는다):**

1. `Topology`에 `provenance: str`("extracted"|"file"|"authored")와 `verified_at: str`("nominal"|"corners"). 기존 네 항목은 `"extracted"` / `"corners"`.
2. 포트 규칙을 `set(topology.ports) == set(block.ports)`에서 **`set(topology.ports) <= set(block.ports)`**로 완화.
3. **남는 블록 포트마다 부동 넷 검사.** 그 블록의 **모든 인스턴스**에 대해, 남는 포트 위치에 붙은 넷이 **같은 스코프의 다른 소자에도 참조되어야** 한다. 하나라도 유일 참여자면 `ports` 사유로 거부한다(새 사유 코드를 만들지 말 것 — 포트 규칙의 일부다).
4. `signal_path.net_blocks`를 쓰지 말 것. 그것은 최상위 넷만 담는데 bandgap의 바이어스 넷은 `BANDGAP` 정의 안에 있다. 인스턴스 줄과 같은 스코프의 소자 목록만 보면 되는 **지역적** 질문이다.
5. 인스턴스가 하나도 없는 정의(어떤 덱에서도 인스턴스화되지 않는 블록)는 남는 포트가 있으면 판정 불가이므로 **거부**한다. 추측하지 않는다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/unit/test_topology_match.py`에:

```python
def test_a_candidate_whose_ports_are_a_subset_is_allowed():
    # 9포트 블록 + 5포트 후보, 남는 포트의 넷을 다른 소자도 만진다 -> 후보
    ...

def test_a_leftover_port_whose_net_has_no_other_user_is_rejected():
    # 남는 포트의 넷이 그 스코프에서 그 인스턴스만 만진다 -> ports 사유로 거부
    ...

def test_every_instance_must_pass_the_floating_check():
    # 같은 정의를 두 번 인스턴스화, 하나는 넷이 공유되고 하나는 아니다 -> 거부
    ...

def test_a_candidate_requiring_a_port_the_block_lacks_is_still_rejected():
    # 완화는 한 방향뿐이다
    ...

def test_a_definition_with_no_instance_and_leftover_ports_is_rejected():
    ...
```

`tests/unit/test_topologies.py`에:

```python
@pytest.mark.parametrize("topology_id", sorted(TOPOLOGY_LIBRARY))
def test_every_entry_declares_its_provenance_and_what_it_was_verified_at(topology_id):
    t = TOPOLOGY_LIBRARY[topology_id]
    assert t.provenance in {"extracted", "file", "authored"}
    assert t.verified_at in {"nominal", "corners"}
```

**그리고 완화가 오늘 아무것도 추가하지 않는다는 사실을 고정하는 테스트** (설계 문서의 실측 절):

```python
def test_the_port_subset_relaxation_admits_nothing_today():
    """이 완화는 규칙으로서 옳지만 오늘의 라이브러리/덱에서는 0쌍을 추가한다.
    5포트 항목 둘 다 sky130_fd_pr__cap_mim_m3_1 을 쓰고 bandgap 덱은 MOS 캡만
    쓰기 때문이다. 라이브러리나 덱이 바뀌어 참이 아니게 되면 이 테스트가 깨지고,
    그때 이 사실을 다시 적어야 한다."""
    text = Path("benchmarks/bandgap/netlist_loops.cir").read_text()
    _, rej = compatible_swaps({"loops": text}, TOPOLOGY_LIBRARY, set())
    five_port = {"miller_basic", "miller_nulling_resistor"}
    reasons = {r.topology_id: r.reason for r in rej if r.topology_id in five_port}
    assert set(reasons) == five_port
    assert set(reasons.values()) == {"models"}     # ports 가 아니라 models 로 걸린다
```

**잡는 변형:** 마지막 테스트는 완화를 되돌리는 변형(`==`로 복귀)을 잡는다 — 그러면 사유가 `models`가 아니라 `ports`가 된다. 부동 검사 테스트들은 그 검사를 통째로 지우는 변형을 잡는다.

- [ ] **Step 2: 실패 확인 → Step 3: 구현 → Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/unit/test_topology_match.py tests/unit/test_topologies.py -q`

- [ ] **Step 5: 회귀 + 커밋**

Run: `.venv/bin/python -m pytest -m "not slow" -q` (기준선 751 passed)

```bash
git add -A && git commit -m "feat: 항목이 출처와 검증 수준을 선언하고 포트 규칙을 부분집합으로 완화한다"
```

---

### Task 2: 큐레이션 골격 — 1단 구조 검사, 2단 특성 재현

**Files:**
- Create: `src/analogcoder/curation.py`
- Test: `tests/unit/test_curation.py`

**Interfaces:**
- Consumes: Task 1의 `compatible_swaps`, `spec.load_spec`, `judge_tools.evaluate_criteria`, `netlist.apply_topology_swap`, `simulators.base.SimulatorBackend`
- Produces: 위 "공통 인터페이스"의 전체 dataclass들, 그리고
  ```python
  def check_structure(candidate: Candidate, slot: Slot, netlist_texts: dict[str, str]) -> StageResult
  def reproduce_characteristics(candidate, slot, netlist_texts, sim_backend) -> tuple[StageResult, list[str]]
  ```
  두 번째의 반환 튜플 둘째 값이 측정된 `addresses`다.

**규칙:**

1. `check_structure`는 `compatible_swaps`를 슬롯의 넷리스트들에 대해 돌리고, **후보가 그 슬롯의 후보로 나오는지**만 본다. 안 나오면 `status="fail"`이고 `detail`에 해당 `SwapRejection`의 `reason`/`detail`을 **그대로** 넣는다. 새 사유 문자열을 만들지 말 것.
   - 후보는 아직 `TOPOLOGY_LIBRARY`에 없으므로 `{candidate.topology_id: <Topology from candidate>}` 한 항목짜리 임시 라이브러리로 호출한다.
2. `reproduce_characteristics`는 **두 번 시뮬레이션한다** — 후보를 스왑한 덱과 기존 본문 그대로의 덱. 둘 다 `evaluate_criteria`를 돌린다.
3. 요구는 **모든 기준을 통과하는 것이 아니다.** 항목은 한 블록만 바꾸므로 스펙 전체를 만족시킬 의무가 없다. 요구는 둘:
   - 후보 쪽에서 **모든 기준의 measurement가 나온다.** 빠진 이름이 있으면 `status="fail"`, `detail["missing"]`에 그 이름들.
   - 시뮬레이터 예외는 `status="inconclusive"` — 거부가 아니다.
4. `addresses`는 **측정에서 나온다**: 후보가 기존 본문보다 나은 기준의 이름들. "낫다"는 그 기준의 연산자 방향으로 판정한다(`>=`면 큰 쪽, `<=`면 작은 쪽). 동률은 개선이 아니다.
5. `detail`은 통과했을 때도 채운다: 후보/기존의 기준별 값, `addresses`, 시뮬 횟수.

- [ ] **Step 1: 실패하는 테스트를 쓴다** (가짜 `SimulatorBackend`로, ngspice 없이)

```python
def test_structure_failure_carries_the_swap_rejection_reason_verbatim(): ...
def test_a_missing_measurement_fails_reproduction(): ...
def test_a_simulator_exception_is_inconclusive_not_a_rejection(): ...
def test_addresses_are_measured_from_the_two_runs_not_declared(): ...
def test_a_criterion_that_ties_is_not_an_address(): ...
def test_the_operator_direction_decides_what_better_means():
    """`<=` 기준에서는 더 작은 값이 개선이다. 방향을 무시하는 구현은 여기서 걸린다."""
def test_reproduction_detail_is_populated_even_when_it_passes(): ...
```

**잡는 변형:** 여섯째는 개선 판정을 `>` 하나로 고정하는 변형을 잡는다(이 저장소의 두 기준 방향이 섞여 있으므로 실제 위험이다). 마지막은 조건부 로깅으로 되돌리는 변형을 잡는다.

- [ ] **Step 2~4: 실패 확인 → 구현 → 통과** — `.venv/bin/python -m pytest tests/unit/test_curation.py -q`
- [ ] **Step 5: 회귀 + 커밋**

```bash
git add -A && git commit -m "feat: 큐레이션 1단 구조 검사와 2단 특성 재현"
```

---

### Task 3: 3단 범위 밝힌 비교와 파레토 거부

**Files:**
- Modify: `src/analogcoder/curation.py`
- Test: `tests/unit/test_curation.py`

**Interfaces:**
- Consumes: `structure.derive_structure`(tunable 인덱스), `area_limits`(허용 배수), `netlist.apply_changes`
- Produces:
  ```python
  def scoped_comparison(candidate, slot, netlist_texts, sim_backend, candidate_measurements,
                        max_knobs: int | None, points: int) -> StageResult
  ```

**규칙:**

1. **기존 본문을 그대로 둔 채** 그 블록의 tunable 인덱스에서 **노브 하나씩** 스윕한다. 두 노브를 동시에 움직이지 않는다.
2. 각 노브의 범위는 **에어리어 게이트의 허용 배수 `M`** 안에서 `[baseline/M, baseline*M]`을 로그 등간격 `points`점. 기준선 자체는 넣지 않는다(2단에서 이미 쟀다). `m`/`nf` 같은 개수 노브는 **정수로 반올림하고 중복을 제거**한다 — 이 저장소는 비정수 `m` 제안을 거부하는 규칙을 이미 갖고 있다.
3. **거부 규칙은 파레토 지배다:** 어떤 단일 스윕 지점이 **모든 기준에서** 후보 이상이면 `REJECT`. 후보가 **하나의 기준이라도** 앞서면 통과.
4. 값을 못 낸 스윕 지점(시뮬 실패, measurement 누락)은 **지배 후보에서 제외**한다. 그런 지점이 후보를 지배한다고 볼 수 없다.
5. `detail`에 **비교 범위**를 반드시 담는다: 스윕한 노브 목록, 각 노브의 기준선/범위/점 수, 총 시뮬 횟수, 기준별 튜닝 최선값과 그 지점, 그리고 거부라면 **지배한 지점**.
6. `max_knobs`로 노브를 좁힐 수 있고, 좁혔다면 `detail["knobs_omitted"]`에 뺀 노브 이름을 넣는다. **조용한 절삭 금지** — 이 저장소의 규칙이다.

- [ ] **Step 1: 실패하는 테스트를 쓴다** (가짜 백엔드)

```python
def test_a_single_sweep_point_dominating_every_criterion_rejects(): ...
def test_a_candidate_winning_one_criterion_survives():
    """파레토다. 모든 기준에서 이겨야 하는 것이 아니다."""
def test_a_sweep_point_with_a_missing_measurement_cannot_dominate(): ...
def test_count_knobs_are_swept_at_integers_only(): ...
def test_the_scope_is_always_recorded_even_when_the_candidate_survives(): ...
def test_omitted_knobs_are_named_when_max_knobs_truncates(): ...
def test_only_one_knob_moves_per_sweep_point():
    """스윕 지점마다 기준선과 다른 파라미터가 정확히 하나여야 한다."""
```

**잡는 변형:** 둘째는 거부 규칙을 "모든 기준에서 앞서야 입회"로 뒤집는 변형을 잡는다. 셋째는 누락값을 무한대로 취급하는 변형을 잡는다. 마지막은 노브를 조합해 스윕하는 변형을 잡는다.

- [ ] **Step 2~4: 실패 확인 → 구현 → 통과**
- [ ] **Step 5: 회귀 + 커밋**

```bash
git add -A && git commit -m "feat: 3단 범위 밝힌 비교와 파레토 거부"
```

---

### Task 4: 2.5단 코너 검증과 출처별 요구 비대칭

**Files:**
- Modify: `src/analogcoder/curation.py`
- Test: `tests/unit/test_curation.py`

**Interfaces:**
- Consumes: `pvt.run_full_pvt_sweep` 또는 그 하위 도구
- Produces: `def verify_corners(candidate, slot, netlist_texts, sim_backend, addresses) -> StageResult`

**규칙:**

1. **저술본(`provenance == "authored"`)에만** 요구한다. 나머지 출처에서는 `status="skipped"`이고 `detail["why"]`에 이유를 적는다(건너뛴 것도 기록이다).
2. **스윕은 두 번뿐** — 후보 한 번, 기존 본문 한 번. **코너에서 노브를 스윕하지 않는다.**
3. 요구는 둘:
   - 모든 코너에서 모든 기준의 measurement가 나온다. 빠지면 `fail`.
   - `addresses`의 각 기준에서, **후보의 최악 코너 값이 기존 본문의 최악 코너 값보다 낫다.** 아니면 `fail`.
4. 슬롯 스펙이 `pvt_corners`를 선언하지 않으면 `status="inconclusive"` — **거부가 아니다.**
5. 이 단계는 3단의 비교를 코너로 확장하지 **않는다.** `detail`에 그 한계를 문자열로 적어 결과를 읽는 사람이 오해하지 않게 한다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_an_extracted_candidate_skips_corners_and_records_why(): ...
def test_an_authored_candidate_requires_corners(): ...
def test_an_authored_candidate_on_a_spec_without_corners_is_inconclusive_not_rejected():
    """'이 회로가 나쁘다'와 '재보지 못했다'는 다른 사실이다."""
def test_a_missing_measurement_at_any_corner_fails(): ...
def test_winning_at_nominal_but_losing_at_the_worst_corner_fails(): ...
def test_only_two_sweeps_are_run(): ...
```

**잡는 변형:** 첫 둘이 출처 비대칭을 지우는 변형을 잡는다 — **이 비대칭이 조용히 사라지는 것이 이 설계에서 가장 값비싼 회귀다.** 셋째는 `inconclusive`를 `fail`로 접는 변형을 잡는다. 다섯째는 코너 비교를 nominal 비교로 바꾸는 변형을 잡는다.

- [ ] **Step 2~4: 실패 확인 → 구현 → 통과**
- [ ] **Step 5: 회귀 + 커밋**

```bash
git add -A && git commit -m "feat: 저술본에만 요구되는 2.5단 코너 검증"
```

---

### Task 5: 후보 구성(소스 A/B)과 `description` 렌더링

**Files:**
- Modify: `src/analogcoder/curation.py`
- Create: `src/analogcoder/agents/curator.py`
- Test: `tests/unit/test_curation.py`, `tests/unit/test_curator_agent.py`

**Interfaces:**
- Produces:
  ```python
  def candidate_from_deck(deck_text: str, block_path: str, topology_id: str) -> Candidate
  def candidate_from_file(body: str, ports: list[str], assumes_scale: float, topology_id: str) -> Candidate
  async def render_description(facts: dict, backend: AgentBackend) -> tuple[str, str]  # (text, "agent"|"template")
  ```

**규칙:**

1. `candidate_from_deck`은 본문·`ports`·`assumes_scale`를 **파싱으로** 얻는다. `provenance="extracted"`.
2. `candidate_from_file`은 `ports`를 선언으로 받고 **선언된 모든 포트가 본문에서 참조되는지** 검증한다(F1에서 확정된 사실: 역방향은 구조적으로 판정 불가). `provenance="file"`.
3. `render_description`의 입력은 **측정된 사실만**이다: 개선/악화된 기준과 수치, `patterns.find_patterns`가 낸 구조 사실, 포트, 3단이 밝힌 비교 범위. 스키마는 `{"description": str}` 하나.
4. **LLM이 실패해도 큐레이션은 실패하지 않는다.** `AgentExecutionError`를 잡아 결정론적 템플릿으로 폴백하고 `"template"`을 반환한다. 산출물이 LLM 가용성에 걸리지 않아야 한다는 것은 최적화 단계에서 확정된 규율이다.
5. 에이전트에게 `addresses`를 쓰게 하지 **않는다.** 스키마에 그 필드를 두지 말 것.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_extracting_from_a_deck_reproduces_the_shipped_library_entry():
    """benchmarks/bandgap/netlist_loops.cir 의 BUF_P 를 추출하면
    TOPOLOGY_LIBRARY['folded_cascode_pmos_in_cs'] 와 본문·포트·스케일이 같다.
    F1의 라이브러리가 이 파이프라인으로 재생산 가능하다는 뜻이다."""

def test_a_file_candidate_with_an_unreferenced_declared_port_is_rejected(): ...
def test_the_description_prompt_contains_only_measured_facts(): ...
def test_an_agent_failure_falls_back_to_a_template(): ...
def test_the_description_schema_has_no_addresses_field(): ...
```

**잡는 변형:** 첫째는 추출을 손으로 적은 상수로 바꾸는 변형을 잡고, **F1의 항목이 실제로 재생산 가능한지를 증명한다.** 넷째는 `except`를 지우는 변형을 잡는다.

- [ ] **Step 2~4: 실패 확인 → 구현 → 통과**
- [ ] **Step 5: 회귀 + 커밋**

```bash
git add -A && git commit -m "feat: 소스 A/B 후보 구성과 description 렌더링"
```

---

### Task 6: 소스 C — 변형 저술 에이전트

**Files:**
- Create: `src/analogcoder/agents/variant_author.py`
- Modify: `src/analogcoder/curation.py`
- Test: `tests/unit/test_variant_author.py`

**Interfaces:**
- Produces:
  ```python
  async def author_variant(base_body: str, technique: str, ports: list[str],
                           available_models: set[str], scale: float,
                           rejection_feedback: str | None, backend: AgentBackend) -> dict
  ```
  스키마: `{"subckt_body": str, "rationale": str}`

**규칙:**

1. 프롬프트에 반드시 들어가는 것: 기존 본문(사이징된 채로), `technique` 문자열, 포트 목록, **그 덱이 인스턴스화하는 모델 이름 집합**, `.option scale`. 모델 집합이 빠지면 에이전트가 덱에 없는 소자를 쓰고 1단에서 튕긴다 — `miller_basic`이 `cap_mim`을 써서 bandgap 덱에서 죽는 것이 정확히 그 모양이다.
2. 프롬프트는 **국소 수정**을 요구한다. 백지 저술이 아니라 기존 사이징을 물려받는 수정임을 명시한다.
3. 거부-재시도 루프: 1단이나 2단이 거부하면 사유를 **그대로** 피드백으로 돌려 다시 시도한다. 상한은 기존 `MAX_TUNING_RETRIES`와 같은 값을 쓰되 이 모듈의 상수로 둔다.
4. 상한을 소진하면 `INCONCLUSIVE`가 아니라 **`REJECT`** 다 — 재보지 못한 것이 아니라 재봤는데 통과하는 본문을 못 만든 것이다. `reason`에 마지막 거부 사유를 담는다.
5. `AgentExecutionError`는 `INCONCLUSIVE`다(LLM이 죽은 것은 회로의 문제가 아니다).

- [ ] **Step 1: 실패하는 테스트를 쓴다** (가짜 백엔드)

```python
def test_the_prompt_carries_the_models_the_deck_actually_provides(): ...
def test_the_prompt_carries_the_base_body_and_scale(): ...
def test_a_structure_rejection_is_fed_back_verbatim_and_retried(): ...
def test_the_retry_limit_is_honoured(): ...
def test_exhausting_retries_is_a_rejection_not_inconclusive(): ...
def test_an_agent_execution_error_is_inconclusive(): ...
```

**잡는 변형:** 첫째는 모델 집합을 프롬프트에서 빼는 변형을 잡는다. 다섯째와 여섯째가 `REJECT`/`INCONCLUSIVE`를 서로 바꾸는 변형을 잡는다.

- [ ] **Step 2~4: 실패 확인 → 구현 → 통과**
- [ ] **Step 5: 회귀 + 커밋**

```bash
git add -A && git commit -m "feat: 기법 이름으로 기존 블록을 국소 수정하는 변형 저술"
```

---

### Task 7: CLI와 산출물, 코너 축 슬롯 스펙

**Files:**
- Create: `src/analogcoder/cli_curate.py`, `benchmarks/bandgap/spec_curate_slot.yaml`
- Modify: `pyproject.toml` (콘솔 스크립트 `analogcoder-curate`)
- Test: `tests/unit/test_cli_curate.py`

**규칙:**

1. 단계를 고정 순서로 돌리고 **어느 단계에서 무엇이 잘못됐든 리포트를 쓰고** `ADMIT`/`REJECT`/`INCONCLUSIVE`로 끝난다. 트레이스백으로 끝나지 않는다.
2. 산출물 셋: `curation_report.md`, `topology_candidate.py`(붙여 넣을 수 있는 `Topology(...)`), `curation.json`(원측정값). **`ADMIT`이 아니어도 셋 다 쓴다** — 왜 거부됐는지가 산출물이다.
3. `topology_candidate.py`는 `provenance`와 `verified_at`을 실제 통과한 것에 맞춰 채운다. **라이브러리를 수정하지 않는다.**
4. 리포트가 반드시 담는 것: 단계별 통과/실패와 `detail`, 3단의 **비교 범위**, 지배 지점(거부라면), 측정된 `addresses`, `description`의 출처(agent/template).
5. 다중 테스트벤치 슬롯이면 시작 시 **예상 시뮬 횟수와 시간**을 로그로 낸다.
6. `--technique`가 있으면 소스 C, `--from-deck`이면 A, `--from-body`면 B. 둘 이상 주어지면 오류.
7. `benchmarks/bandgap/spec_curate_slot.yaml`: `amp_loops` 테스트벤치 하나 + `spec_corner_reduction.yaml`의 **9코너 격자**. `optimize:`도 `corner_reduction:`도 넣지 않는다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_a_rejection_still_writes_all_three_artifacts(): ...
def test_the_report_records_the_comparison_scope(): ...
def test_the_candidate_snippet_carries_the_provenance_actually_verified(): ...
def test_the_library_module_is_not_modified(): ...
def test_two_source_flags_together_is_an_error(): ...
def test_an_unexpected_exception_still_produces_a_report_and_a_verdict(): ...
def test_the_slot_spec_loads_and_declares_nine_corners(): ...
```

**잡는 변형:** 첫째는 `ADMIT`일 때만 산출물을 쓰는 변형을 잡는다. 넷째는 라이브러리를 자동으로 고치는 변형을 잡는다.

- [ ] **Step 2~4: 실패 확인 → 구현 → 통과**
- [ ] **Step 5: 회귀 + 커밋**

```bash
git add -A && git commit -m "feat: analogcoder-curate 진입점과 산출물 셋"
```

---

### Task 8: 실 ngspice 종단 — Ahuja 재현 거부

**Files:**
- Create: `tests/unit/test_curation_ngspice.py`
- Test: 위 파일

**이 태스크가 F2의 증명이다.** 설계가 실제로 F1의 실수를 잡는지를 보인다.

**규칙:**

1. 지시 보상(Ahuja) 본문을 소스 B 후보로, `TRIMAMP`을 슬롯으로 넣으면 게이트가 **`REJECT`** 를 내고, 지배 지점으로 **`XRz.l` 변경**을 지목해야 한다.
2. 실행 시간을 위해 노브를 좁힌다(`max_knobs` 또는 명시 목록). **좁혔다는 사실이 테스트 이름과 리포트에 남아야 한다.** 설계 문서의 측정에 따르면 `XRz.l = 60`(다른 노브 고정) 하나로 지배가 성립한다.
3. `BUF_P` 추출이 `folded_cascode_pmos_in_cs`를 재생산하는 것도 여기서 실 시뮬로 확인한다.
4. 실행 시간을 재고 60초를 넘으면 `slow` 마커를 붙인다. 넘지 않으면 붙이지 않는다. **실측한 시간을 리포트에 적을 것.**

- [ ] **Step 1: 테스트를 쓴다**

```python
def test_indirect_compensation_is_rejected_because_a_single_knob_change_dominates_it():
    """F1에서 이 항목은 사람이 손으로 스윕해서 기각했다. 게이트가 같은 답을 내는가.
    노브를 XRz.l 하나로 좁혀 실행한다 - 좁혔다는 사실 자체가 3단의 '범위 밝힌'
    이라는 이름의 뜻이고, 리포트에 남는다."""

def test_extracting_buf_p_reproduces_the_shipped_library_entry_under_real_simulation():
    ...
```

**기대값은 설계 문서에 있다.** 지시 보상 후보 89.4°/5.45 MHz, `XRz.l=60` 지점 125.4°/24.8 MHz. **실제 값이 다르면 단언이 아니라 사실을 보고할 것.**

- [ ] **Step 2~3: 실행 → 통과 확인 → 시간 측정**
- [ ] **Step 4: 회귀 + 커밋**

```bash
git add -A && git commit -m "test: 큐레이션 게이트가 Ahuja 후보를 거부한다"
```

---

## 자체 점검

- **스펙 커버리지:** 소스 A/B → T5, 소스 C → T6, 1단 → T2, 2단 → T2, 2.5단 → T4, 3단 → T3, 4단(addresses) → T2, 5단(description) → T5, 포트 완화 → T1, 완화가 0쌍이라는 기록 → T1, `provenance`/`verified_at` → T1, 산출물/CLI → T7, 클래스 축 없음 → 스펙 문서에만(코드 없음, 의도적), 성공 기준 1·2 → T8. 누락 없음.
- **타입 일관성:** `Candidate`/`Slot`/`StageResult`/`CurationResult`는 T2가 정의하고 T3~T7이 소비한다. `provenance` 문자열 집합은 T1과 T2·T4가 같은 값을 쓴다.
- **알려진 위험:** T3의 노브 범위 계산이 `area_limits`의 티어 API에 의존하는데, 그 API가 "제안을 판정한다"는 모양이지 "허용 범위를 낸다"는 모양이 아닐 수 있다. 그렇다면 **티어 값을 얻는 작은 헬퍼를 `area_limits`에 추가**하되 기존 판정 경로의 동작은 바꾸지 말 것. 이 저장소는 에어리어 게이트를 여섯 번 조용히 무력화한 이력이 있다.
