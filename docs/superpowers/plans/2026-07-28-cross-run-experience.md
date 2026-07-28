# 런 간 경험 (D) 구현 계획

> **상태: 실행하지 말 것. 보류(D3). 2026-07-28.**
> 이 계획의 설계 문서가 보류되었다 — 이유는
> `docs/superpowers/specs/2026-07-28-cross-run-experience-design.md`의 머리말.
> 요약하면 런 내 히스토리가 이미 튜너에게 가고 있고, 이 계획은 같은 런이 이미
> 버리는 필드를 런 사이로 나르는 인프라를 짓는다.
> **지금 실행할 계획:** `docs/superpowers/plans/2026-07-28-tuning-attempt-record.md` (D1).
> 아래 태스크 중 회로 지문과 패턴 키에 대한 것은 D3에서 다시 쓴다.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 런이 시도와 결과를 기계 판독 가능한 형태로 남기고, 같은 회로·같은 기준을 다시 만난 런이 그것을 사실로 회상하게 만든다.

**Architecture:** 두 개의 새 순수 모듈(`circuit_fingerprint.py`, `experience.py`)과 배선 둘(기록: 오케스트레이터, 소비: 튜너 프롬프트). LLM은 새로 추가되지 않는다. 경험 계층의 어떤 실패도 런을 실패시키지 않는다.

**Tech Stack:** Python 3, dataclasses, pytest, ngspice(종단 태스크만)

**설계 문서:** `docs/superpowers/specs/2026-07-28-cross-run-experience-design.md` (커밋 `191dc22`). 코퍼스 실측은 전부 거기 있다 — **다시 세지 말 것.**

## Global Constraints

- **테스트가 요구사항이고, 브리프의 코드 스케치는 제안일 뿐이다.** 스케치가 산문 규칙과 어긋나면 **산문이 이긴다**. 지난 세 계획에서 브리프의 절반 이상이 결함 있는 스케치를 담았다.
- **새 테스트마다 "이 테스트는 어떤 변형을 잡는가"를 답하고, 변형을 실제로 적용해 확인한다.** 지난 두 브랜치에서 무효 테스트가 열 건 넘게 이 방법으로만 잡혔다.
- **경험 계층은 런을 실패시키지 않는다.** 디스크·권한·깨진 줄·지문 파생 실패 — 전부 런이 계속되고 사실이 `history.jsonl`에 남는다.
- **조회 사실은 일치 0건이어도 기록한다.** 조건부 로깅은 "조회했고 0건"과 "조회가 사라졌다"를 구별 불가능하게 만든다. 이 저장소는 조용히 무력한 게이트를 **아홉 번** 출하했다.
- **조용한 절삭 금지.** 프롬프트에 넣는 항목을 잘랐으면 잘린 수를 기록한다.
- **추측 금지.** 회로 사이의 유사도, 과거 런의 넷리스트 소급 추정 — 전부 금지.
- **LLM이 쓴 산문(`reasoning`)은 저장하지 않는다.**
- 파이썬은 `.venv/bin/python`. 테스트는 `.venv/bin/python -m pytest`.
- 커밋 메시지는 한글, 마지막 줄에 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- 기준선: `pytest -m "not slow"` → 904 passed, 2 skipped, 6 deselected.

## 계획 오류 정정 #1 (스펙보다 이것이 맞다)

설계 문서는 *"회상은 `(circuit, criterion, refdes, param)`이 정확히 같을 때만
일어난다"*고 쓰지만, **같은 문서의 프롬프트 예시는 한 기준에 대해 서로 다른 세
노브를 보여 준다.** 예시가 맞고 문장이 틀렸다 — 프롬프트를 만드는 시점에 튜너는
아직 노브를 고르지 않았으므로 `refdes`/`param`으로 조회할 수가 없다.

**회상 키는 `(circuit, criterion)`이다.** 둘 다 정확 일치여야 한다.
`refdes`/`param`은 **조회 키가 아니라 보여 주는 내용**이다. Task 3이 이 문장에
맞춰 스펙을 고친다.

## 파일 구조

| 파일 | 책임 |
|---|---|
| `src/analogcoder/circuit_fingerprint.py` (신규) | 값 불변 회로 지문. 순수 함수 |
| `src/analogcoder/experience.py` (신규) | `Attempt`, 저장 위치 판정, append-only 읽기/쓰기, 회상 |
| `src/analogcoder/orchestrator.py` (수정) | 시도를 항목으로 기록 |
| `src/analogcoder/agents/tuner.py` (수정) | 회상된 사실을 프롬프트에 |
| `src/analogcoder/cli.py` (수정) | `--experience-dir`, 저장 위치 로깅 |
| `scripts/experience_digest.py` (신규) | `DIGEST.md` 생성 |

## 공통 인터페이스 (모든 태스크가 이 이름을 쓴다)

```python
# circuit_fingerprint.py
@dataclass(frozen=True)
class CircuitFingerprint:
    digest: str            # 안정 해시
    summary: dict          # 사람이 읽을 재료 (blocks, ports, classes, patterns)

def fingerprint(netlist_text: str) -> CircuitFingerprint

# experience.py
@dataclass(frozen=True)
class Attempt:
    circuit: str; criterion: str; target: str; actual_before: float | None
    refdes: str; param: str; old_value: str; new_value: str
    gate: str | None; outcome: str          # "kept"|"rolled_back"|"gate_rejected"
    deltas: dict[str, float]; regressed: list[str]
    spec: str; run: str

@dataclass(frozen=True)
class StoreLocation:
    path: Path; committed: bool; reason: str

def resolve_store(spec_path: Path, repo_root: Path, override: Path | None) -> StoreLocation
def append_attempts(store: StoreLocation, attempts: list[Attempt]) -> None
def load_attempts(store: StoreLocation) -> tuple[list[Attempt], int]   # (항목들, 깨진 줄 수)
def recall(attempts: list[Attempt], circuit: str, criterion: str,
           limit: int) -> tuple[list[Attempt], int]                    # (보여줄 것, 잘린 수)
```

---

### Task 1: 회로 지문

**Files:** Create `src/analogcoder/circuit_fingerprint.py`; Test `tests/unit/test_circuit_fingerprint.py`

**규칙 (산문이 구속력을 갖는다):**

1. `structure.derive_structure`와 `patterns.find_patterns`에서만 파생한다. 넷리스트 텍스트를 직접 파싱하지 않는다.
2. **값은 넣지 않는다.** `W`/`L`/`m`/`nf`, 위치 값, `.param` — 전부 제외. 넣으면 튜닝 한 번에 지문이 바뀌어 회상이 영원히 빗나간다.
3. 넣는 것: 블록 경로 집합, 블록별 포트 목록, 블록별 `device_class`(없으면 `ctype`) 카운트, 패턴 종류별 개수. 최상위 스코프도 하나의 블록으로 센다.
4. `digest`는 **순서에 안정적**이어야 한다 — 집합/딕셔너리를 정렬해서 직렬화한 뒤 해싱한다. 같은 덱을 두 번 넣으면 같은 값이 나와야 한다.
5. `summary`는 사람이 읽을 수 있는 형태로 같은 재료를 담는다.
6. 파생이 실패하면 예외를 삼키지 말고 올린다 — 삼키는 것은 호출자(Task 4)의 책임이다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_the_fingerprint_is_invariant_to_value_tuning():
    """지문에 값이 들어가면 튜닝 한 번에 회상이 끊긴다. 이 테스트가 그 변형을 잡는다."""
    base = Path("benchmarks/two_stage_opamp/netlist.cir").read_text()
    tuned = apply_changes(base, [{"refdes": "OPAMP2STAGE.X1", "param": "W", "new_value": "99"}])
    assert base != tuned
    assert fingerprint(base).digest == fingerprint(tuned).digest

def test_a_topology_swap_changes_the_fingerprint():
    """스왑 후에는 다른 회로다. 그것이 옳다."""

def test_two_different_benchmarks_have_different_fingerprints(): ...
def test_the_digest_is_stable_across_calls_and_dict_ordering(): ...
def test_the_summary_names_every_block_the_structure_has(): ...
```

**잡는 변형:** 첫째는 지문에 `params`를 넣는 변형을 잡는다. 넷째는 `sorted(...)`를 빼는 변형을 잡는다(파이썬 딕셔너리 순서가 우연히 같아 통과할 수 있으므로, **키 순서를 실제로 뒤집은 입력**으로 확인할 것).

- [ ] **Step 2~4: 실패 확인 → 구현 → 통과** — `.venv/bin/python -m pytest tests/unit/test_circuit_fingerprint.py -q`
- [ ] **Step 5: 실제 벤치마크 지문을 리포트에 적는다** (두 덱의 `summary`)
- [ ] **Step 6: 회귀 + 커밋** — `git commit -m "feat: 값에 불변인 결정론적 회로 지문"`

---

### Task 2: 경험 저장소

**Files:** Create `src/analogcoder/experience.py`; Test `tests/unit/test_experience.py`

**Interfaces:** `Attempt`, `StoreLocation`, `resolve_store`, `append_attempts`, `load_attempts`

**규칙:**

1. `attempts.jsonl`은 **append-only**. 기존 줄을 다시 쓰지 않는다.
2. **깨진 줄 하나가 파일을 무효화하지 않는다.** 줄 단위로 파싱하고 실패한 줄은 **세어서** 반환한다. 조용히 건너뛰지 않는다.
3. **저장 위치 판정 (IP 경계, 이 태스크의 핵심):**
   - `override`가 주어지면 그것을 쓰고 `committed=False`, `reason="explicit override"`.
   - 아니면 `spec_path`를 절대화해 `repo_root` **안에** 있는지 본다. 안이면 `repo_root/experience`, `committed=True`. 밖이면 git이 무시하는 로컬 경로, `committed=False`, `reason`에 스펙이 저장소 밖이라는 사실.
   - **경로 포함은 파스된 사실로 판정한다** — `Path.resolve()` 후 `is_relative_to`. 이름을 보고 판단하지 않는다. 심볼릭 링크로 우회되지 않도록 반드시 `resolve()` 먼저.
4. 로컬 경로는 저장소 밖에 둔다(예: 사용자 캐시 디렉터리). 저장소 안에 두고 `.gitignore`에 의존하면 `.gitignore`가 지워지는 날 IP가 커밋된다.
5. `append_attempts`는 디렉터리가 없으면 만든다. 실패는 **올린다**(삼키는 것은 호출자 책임).

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_a_spec_inside_the_repo_goes_to_the_committed_store(): ...

def test_a_spec_outside_the_repo_never_goes_to_the_committed_store(tmp_path):
    """이 하위 프로젝트에서 조용히 사라지면 가장 비싼 회귀다.
    공개 저장소를 체크아웃해 생산 덱을 돌리면 래퍼 셀 이름이 항목에 들어간다."""
    loc = resolve_store(tmp_path / "company" / "spec.yaml", REPO_ROOT, None)
    assert loc.committed is False
    assert REPO_ROOT not in loc.path.resolve().parents and loc.path.resolve() != REPO_ROOT
    assert "outside" in loc.reason.lower() or loc.reason

def test_a_symlink_into_the_repo_does_not_defeat_the_boundary(tmp_path): ...
def test_an_explicit_override_is_never_treated_as_committed(): ...
def test_a_corrupt_line_is_counted_not_silently_skipped(): ...
def test_appending_does_not_rewrite_existing_lines(): ...
def test_reasoning_is_not_a_field_of_attempt():
    assert "reasoning" not in {f.name for f in dataclasses.fields(Attempt)}
```

**잡는 변형:** 둘째·셋째가 경계 검사를 지우거나 `resolve()`를 빼는 변형을 잡는다. 다섯째는 깨진 줄을 `continue`로 삼키는 변형을 잡는다.

- [ ] **Step 2~4: 실패 확인 → 구현 → 통과**
- [ ] **Step 5: 회귀 + 커밋** — `git commit -m "feat: 경험 저장소와 IP 경계 판정"`

---

### Task 3: 회상

**Files:** Modify `src/analogcoder/experience.py`, `docs/superpowers/specs/2026-07-28-cross-run-experience-design.md`; Test `tests/unit/test_experience_recall.py`

**규칙:**

1. **회상 키는 `(circuit, criterion)`이고 둘 다 정확 일치다.** 위 "계획 오류 정정 #1" 참고 — 설계 문서의 그 문장을 이 태스크가 고친다.
2. `refdes`/`param`은 조회 키가 아니라 **보여 주는 내용**이다.
3. 느슨한 유사도 없음. 부분 문자열, 접두사, 정규화 — 전부 금지.
4. `limit`를 넘으면 자르고 **잘린 수를 반환한다.**
5. 정렬은 결정론적이어야 한다. 같은 입력에 같은 순서.
6. 같은 `(refdes, param, old, new)`가 여러 번 나오면 **중복 제거하지 않는다** — "두 번 시도했고 두 번 다 롤백됐다"는 한 번과 다른 사실이다. 다만 표시 순서는 결정론적이어야 한다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_a_different_criterion_recalls_nothing(): ...
def test_a_different_circuit_recalls_nothing(): ...
def test_all_knobs_for_the_matching_criterion_are_recalled():
    """조회 키가 (circuit, criterion)이라는 것. refdes로 좁히는 변형을 잡는다."""
def test_no_fuzzy_matching_on_criterion_names():
    """'phase_margin'과 'trim_phase_margin'은 서로를 회상하지 않는다."""
def test_truncation_reports_how_many_were_dropped(): ...
def test_the_order_is_deterministic(): ...
def test_repeated_identical_attempts_are_not_deduplicated(): ...
```

- [ ] **Step 2~4: 실패 확인 → 구현 → 통과**
- [ ] **Step 5: 설계 문서의 회상 키 문장을 고친다** (정정 #1)
- [ ] **Step 6: 커밋** — `git commit -m "feat: 정확 일치 회상과 스펙의 회상 키 정정"`

---

### Task 4: 기록 배선

**Files:** Modify `src/analogcoder/orchestrator.py`, `src/analogcoder/cli.py`; Test `tests/unit/test_orchestrator.py`, `tests/unit/test_cli.py`

**규칙:**

1. 항목이 만들어지는 자리는 셋이다:
   - **게이트 거부** — `area_check`/`refdes_check`/`param_check`/`stimulus_check` 중 하나가 막았을 때. `outcome="gate_rejected"`, `gate`에 사유 코드. 시뮬레이션 없이 얻은 진짜 정보다.
   - **적용 후 유지** — `verify_post`가 `keep`. `outcome="kept"`.
   - **적용 후 롤백** — `verify_post`가 `rollback`. `outcome="rolled_back"`, `regressed`에 `verify_post`의 `regressed_criteria`.
2. `criterion`은 **그 이터레이션에서 실패 중이던 기준**이다. 여러 개면 **각각에 대해 항목을 만든다** — 다음 런이 기준별로 조회하기 때문이다.
3. `deltas`는 적용 전후 `judge_result`의 기준별 `actual` 차이. 게이트 거부는 적용이 없었으므로 빈 dict.
4. `actual_before`는 적용 전 그 기준의 `actual`.
5. **경험 계층의 어떤 실패도 런을 실패시키지 않는다.** 기록 전체를 가드로 감싸고, 실패하면 `experience_unavailable`을 사유와 함께 로깅하고 계속한다.
6. `experience_recorded` 이벤트: 항목 수, 저장 위치, `committed` 여부, 그리고 로컬로 갔다면 그 이유.
7. `cli.py`에 `--experience-dir`. 기본은 `resolve_store`가 정한다.

- [ ] **Step 1: 실패하는 테스트를 쓴다** (에이전트 모킹)

```python
async def test_a_gate_rejection_becomes_an_attempt_with_the_gate_code(): ...
async def test_a_kept_proposal_becomes_an_attempt_with_deltas(): ...
async def test_a_rolled_back_proposal_records_the_regressed_criteria(): ...
async def test_two_failing_criteria_produce_an_attempt_each(): ...
async def test_an_experience_write_failure_does_not_fail_the_run():
    """가드를 지우는 변형을 잡는다. 런은 PASS로 끝나고 experience_unavailable이 남는다."""
async def test_experience_recorded_logs_where_it_went_and_whether_committed(): ...
```

- [ ] **Step 2~4: 실패 확인 → 구현 → 통과**
- [ ] **Step 5: 회귀 + 커밋** — `git commit -m "feat: 시도를 경험 항목으로 기록한다"`

---

### Task 5: 소비 배선

**Files:** Modify `src/analogcoder/agents/tuner.py`, `src/analogcoder/orchestrator.py`; Test `tests/unit/test_tuner_agent.py`, `tests/unit/test_orchestrator.py`

**규칙:**

1. 실패 중인 기준마다 회상하고, 결과를 튜너 프롬프트에 **사실 목록**으로 넣는다.
2. **프롬프트는 지시가 아니라 사실로 제시한다.** "이걸 하지 마라"가 아니라 "전에 이렇게 했더니 이렇게 됐다". 튜너는 여전히 현재 넷리스트를 보고 스스로 판단한다 — 과거의 롤백이 지금도 롤백이라는 보장은 없다(그 사이 회로의 다른 곳이 바뀌었을 수 있다).
3. **일치가 없으면 그 절을 아예 그리지 않는다.** 빈 절은 모델에게 노이즈다.
4. **`experience_recall` 이벤트는 무조건 기록한다** — 일치 0건이어도. 조회 키, 일치 수, 프롬프트에 넣은 수, 잘린 수.
5. 상한은 상수로 두고, 잘랐으면 프롬프트에도 잘렸다는 사실을 적는다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
async def test_recalled_attempts_appear_in_the_tuner_prompt_as_facts(): ...
async def test_no_match_draws_no_section_at_all(): ...
async def test_the_prompt_does_not_instruct_only_reports():
    """'do not', 'must not', 'avoid' 같은 지시어가 회상 절에 없다."""
async def test_experience_recall_is_logged_even_with_zero_matches():
    """조건부 로깅으로 되돌리는 변형을 잡는다."""
async def test_truncation_is_visible_in_both_the_log_and_the_prompt(): ...
```

- [ ] **Step 2~4: 실패 확인 → 구현 → 통과**
- [ ] **Step 5: 회귀 + 커밋** — `git commit -m "feat: 회상된 사실을 튜너 프롬프트에 넣는다"`

---

### Task 6: `DIGEST.md` 생성

**Files:** Create `scripts/experience_digest.py`; Test `tests/unit/test_experience_digest.py`

**규칙:**

1. `attempts.jsonl`과 지문 요약에서 **생성**한다. 손으로 고치지 않는다.
2. 헤더에 **재생성 명령**과 "이 파일은 생성물이다"를 적는다.
3. 재생성은 손으로 고친 내용을 **덮어쓴다.** 그것이 "마크다운은 파생물"의 뜻이다.
4. 담는 것: 회로별 항목 수, 기준별 시도와 결과 분포(kept/rolled_back/gate_rejected), 가장 많이 건드린 노브. **집계만** — 해석이나 권고는 넣지 않는다.
5. 깨진 줄이 있었으면 그 수를 적는다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_the_digest_is_generated_from_the_store_not_authored(): ...
def test_regenerating_overwrites_hand_edits(): ...
def test_the_header_carries_the_regeneration_command(): ...
def test_corrupt_line_count_reaches_the_digest(): ...
def test_the_digest_contains_no_recommendation_language():
    """집계만이다. 'should', 'recommend', '권장' 같은 말이 없다."""
```

- [ ] **Step 2~4: 실패 확인 → 구현 → 통과**
- [ ] **Step 5: 커밋** — `git commit -m "feat: 경험 저장소에서 DIGEST.md를 생성한다"`

---

### Task 7: 코퍼스 생성과 종단 증명

**Files:** Create `tests/unit/test_experience_end_to_end.py`; 그리고 실제 런 산출물

**이 태스크가 D의 증명이다.** D는 "데이터가 없다"는 사실 위에 서 있으므로 데이터를 만드는 것까지가 범위다.

**규칙:**

1. **종단 테스트는 LLM 없이 결정론적으로** 만든다: 같은 시드 스펙을 두 번 `run_orchestration`에 태우되 에이전트는 고정 응답 스텁을 쓰고, **두 번째 실행이 첫 번째의 항목을 회상하는지**를 확인한다. 실 시뮬레이터는 써도 되고 안 써도 된다 — 시간을 재서 판단하고 60초를 넘으면 `slow` 마커.
2. **실제 코퍼스 생성 실행**은 별도로, LLM을 써서 돌린다:
   - `benchmarks/bandgap`의 `spec_seed_trim_pm.yaml`, `spec_seed_tc.yaml`, `spec_seed_buf0_droop.yaml`
   - `benchmarks/two_stage_opamp/spec.yaml`
   - 각 스펙을 **두 번씩**
3. **측정해서 리포트에 적을 것:** 생성된 항목 수, 회상이 발동한 횟수, 회상이 실제로 다른 행동을 낳았는지. **개선을 주장하지 않는다.** 회상이 아무것도 바꾸지 않았다면 그것도 결과이고, 그 사실이 다음 단계(느슨한 매칭·순위가 필요한지)의 근거가 된다.
4. 생성된 `experience/`를 커밋한다. 생산 덱은 쓰지 않는다(전부 벤치마크).
5. 런이 실패하거나 SDK가 죽으면(이 저장소는 `aclose()` 간헐 오류를 기록해 뒀다) **그 사실을 적고 남은 것으로 보고한다.** 억지로 채우지 않는다.

- [ ] **Step 1: 결정론적 종단 테스트를 쓴다 → 통과 확인 → 시간 측정**
- [ ] **Step 2: 실제 코퍼스 생성 실행** (LLM). 각 런의 결과와 `experience_recall` 이벤트를 수집한다.
- [ ] **Step 3: 측정 결과를 리포트에 적는다** — 위 규칙 3의 세 가지
- [ ] **Step 4: `DIGEST.md` 생성 후 커밋**
- [ ] **Step 5: 회귀 + 커밋** — `git commit -m "test: 경험 회상 종단 증명과 첫 코퍼스"`

---

## 자체 점검

- **스펙 커버리지:** 경험의 단위 → T2. 지문 → T1. 정확 일치 회상 → T3. 소비 → T5. 저장 위치와 IP → T2. 산출물(마크다운 파생) → T6. 코퍼스 생성 → T7. 마이그레이션 안 함 → 코드 없음(스펙 문서에만, 의도적). 에러 처리 → T4 규칙 5. 로깅 → T4·T5. 성공 기준 1~6 → T7·T4·T5·T2·T6·T7.
- **정정 하나를 계획에 반영했다**(정정 #1): 회상 키는 `(circuit, criterion)`이다. 설계 문서의 문장이 자기 예시와 어긋났고 T3가 고친다.
- **타입 일관성:** `Attempt`/`StoreLocation`은 T2가 정의하고 T3~T7이 소비한다. `CircuitFingerprint.digest`가 `Attempt.circuit`에 들어가는 값이다.
- **알려진 위험:** T4는 오케스트레이터의 튜닝 재시도 루프 안쪽에 손을 댄다. 그 루프는 F1에서 토폴로지 경로가 추가되며 이미 한 번 복잡해졌고, 게이트 사유 코드가 이벤트마다 다른 키에 들어 있다(`area_check`는 `feedback`, `refdes_check`도 `feedback`). **사유 코드를 이벤트에서 다시 파싱하지 말고** 게이트 함수의 반환값에서 직접 받을 것.
