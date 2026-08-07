# 진입 시뮬레이션 선행 조건 구현 계획

> **에이전트 작업자에게:** 필수 하위 스킬 — superpowers:subagent-driven-development
> 로 태스크 단위 실행. 단계는 체크박스(`- [ ]`)로 추적한다.

**목표:** 한 점도 측정하지 못한 런이 튜너의 실패 사유를 달고 끝나는 것을 막는다 —
"시뮬레이터가 안 돌았다" 와 "튜너가 못 고쳤다" 를 `result.json` 에서 구별한다.

**배경(실측):** 2026-08-05 대안 정렬 A/B 의 OFF 팔 세 런은 PDK 서브모듈이 빈
워크트리에서 돌아 시뮬레이션 **0/5, 0/1, 0/3** 성공이었다. 세 런 모두 측정값
`{}` 를 상대로 튜닝을 반복했고, `off_1` 은 75분을 쓴 뒤
`tuning proposal repeatedly rejected` 로 끝났다. `orchestrator.py`·`cli.py`
어디에도 그 상황을 읽는 코드가 없다.
근거 문서: `docs/superpowers/specs/2026-08-05-alternatives-benefit-results.md`
(2026-08-07 정정 절).

**설계:** 그 런의 **첫** `simulate` 결과가 어느 테스트벤치에서도 측정값을 내지
못했으면 즉시 FAIL 로 끝낸다. 고유한 사유와 고유한 이벤트를 남긴다.

## Global Constraints

- **임계값이 아니라 선행 조건이다.** "연속 N회 실패" 같은 수를 도입하지 않는다.
  검사는 그 런의 **첫** 시뮬레이션에서 **한 번만** 한다.
- **조건은 `sim_result["measurements"]` 가 비었는가이지 `status` 문자열이
  아니다.** 이 저장소는 이름으로 의미를 알아보는 것을 금지한다. 비어 있다는 것은
  **판정이 받을 입력이 없다**는 뜻이고, 그것이 이 검사가 묻는 것 그대로다.
- 튜닝 **이후**의 시뮬레이션에는 적용하지 않는다. 거기서 측정값이 사라지는 것은
  튜닝이 덱을 깨뜨린 것이고, 이미 롤백이 옳은 답이다.
- 문서·주석·커밋 메시지는 한글.
- 새 사유 문자열은 기존 사유를 바꾸지 않는다. 기존 FAIL 경로는 그대로다.
- `pytest -m "not slow"` 가 통과해야 한다(현재 1669 passed, 2 skipped, 9 deselected).

---

### Task 1: 진입 시뮬레이션 선행 조건

**Files:**
- Modify: `src/analogcoder/orchestrator.py` (첫 `simulate` 호출 부근, 현재 418~422행)
- Test: `tests/unit/test_orchestrator.py`

**Interfaces:**
- Consumes: `agents.simulate(netlist_texts, spec)` 가 돌려주는 dict — `measurements`
  (병합된 이름→값), `by_testbench`, `status`, 테스트벤치별 `warnings`.
- Produces: `_final_result(...)` 가 돌려주는 결과의
  `failure_reason` 문자열, 그리고 `state.log_event("entry_simulation_empty", …)`
  이벤트. 후속 태스크가 둘 다 읽는다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/unit/test_orchestrator.py` 에 넣는다. 기존 파일의 가짜 에이전트 헬퍼를
그대로 쓴다(파일 안에서 이미 쓰는 패턴을 따를 것 — 새 헬퍼를 만들지 않는다).

```python
def test_a_run_whose_first_simulation_measures_nothing_ends_immediately():
    """측정값이 하나도 없으면 판정은 받을 입력이 없다. 그 런은 튜너의
    실패 사유가 아니라 **자기 사유**로 끝나야 한다 - 2026-08-05 A/B 의 OFF 팔
    세 런이 `{}` 를 상대로 75분을 쓰고 `tuning proposal repeatedly rejected`
    로 끝난 것이 이 테스트가 막는 것이다."""
    calls = {"simulate": 0, "tune": 0}

    async def simulate(netlist_texts, spec):
        calls["simulate"] += 1
        return {
            "status": "error",
            "measurements": {},
            "by_testbench": {"tb": {"status": "error", "measurements": {},
                                    "warnings": ["could not find include file foo.inc"]}},
        }

    async def tune(*args, **kwargs):
        calls["tune"] += 1
        raise AssertionError("측정값이 없는데 튜너가 불렸다")

    result = asyncio.run(run_orchestration(... simulate=simulate, tune=tune ...))

    assert result["status"] == "FAIL"
    assert "no measurements" in result["failure_reason"]
    # 원인이 사유에 실려야 한다. 이것이 없으면 읽는 사람이 history 를 파야 한다.
    assert "foo.inc" in result["failure_reason"]
    assert calls["simulate"] == 1      # 예산을 태우지 않는다
    assert calls["tune"] == 0
```

그리고 **게이트가 아무 일도 하지 않을 때**를 고정하는 테스트도 같이 쓴다:

```python
def test_a_run_whose_first_simulation_measures_something_is_untouched():
    """측정값이 하나라도 있으면 이 검사는 아무것도 하지 않는다. 부분 실패
    (테스트벤치 하나만 error)는 **다른 사실**이고 기존 NaN 처리가 맡는다."""
    # 테스트벤치 둘 중 하나만 error, 다른 하나는 측정값 있음 ->
    # 루프가 평소대로 돌고 failure_reason 에 "no measurements" 가 없어야 한다.
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py -k "measures_nothing or measures_something" -v`
Expected: 첫 번째 FAIL(`failure_reason` 없음 또는 튜너가 불림), 두 번째는 PASS 일 수 있다.

- [ ] **Step 3: 최소 구현**

`orchestrator.py` 의 첫 `simulate` 호출 직후(현재 418~419행 사이). `outer_iter`
값이 아니라 **이번 호출이 이 실행의 첫 시뮬레이션인가**로 판단한다 — 재개된
실행은 `outer_iter` 가 1이 아니지만, 그 실행도 자기 환경에서 측정할 수 있는지를
똑같이 확인해야 한다.

```python
            sim_result = await agents.simulate(netlist_texts, spec)
            state.log_event("simulation", {"outer_iter": outer_iter, **sim_result})

            # **이 실행의 첫 시뮬레이션이 아무것도 재지 못했으면 여기서 끝낸다.**
            # 조건은 `status` 문자열이 아니라 **판정이 받을 입력이 비었는가**다 -
            # 이름으로 의미를 알아보지 않는다는 규칙 그대로이고, 묻고 싶은 것
            # 자체가 "판정이 정의되는가"이기 때문이다.
            #
            # 임계값이 아니라 선행 조건이다. 튜닝 **이후**의 빈 측정은 튜닝이
            # 덱을 깨뜨린 것이고 이미 롤백이 옳은 답이므로 여기 오지 않는다.
            if first_simulation and not sim_result.get("measurements"):
                detail = _entry_simulation_detail(sim_result)
                state.log_event("entry_simulation_empty", {
                    "outer_iter": outer_iter,
                    "status": sim_result.get("status"),
                    "detail": detail,
                })
                return _final_result(
                    "FAIL", state, outer_iter, None,
                    failure_reason=(
                        "the entry simulation produced no measurements in any "
                        f"testbench: {detail}"
                    ),
                    topology_swaps=topology_swaps,
                    tuning_history=tuning_history,
                )
            first_simulation = False
```

`first_simulation = True` 는 루프 **바깥**에서 초기화한다.

`_entry_simulation_detail(sim_result)` 은 테스트벤치별 첫 `warnings` 항목을
`"<tb>: <warning>"` 로 모아 이어 붙인다. 경고가 없으면 `"no warning was reported"`
를 돌려준다 — **빈 문자열을 돌려주지 않는다.** "원인이 없다" 와 "원인이 기록되지
않았다" 는 다른 사실이다.

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py -v`
Expected: 전부 PASS.

- [ ] **Step 5: 회귀 확인**

Run: `.venv/bin/python -m pytest -q -m "not slow"`
Expected: 1671 passed(새 테스트 2개), 2 skipped, 9 deselected.
**개수를 올리기 전에 0 실패를 먼저 확인한다** — 이 저장소의 드리프트 가드 규약이다.

- [ ] **Step 6: 커밋**

```bash
git add src/analogcoder/orchestrator.py tests/unit/test_orchestrator.py
git commit -m "feat: 진입 시뮬레이션이 아무것도 재지 못하면 그 사유로 끝낸다"
```

---

### Task 2: 실제 조건에서 확인하고 기록한다

**Files:**
- Test: `tests/unit/test_orchestrator.py` (Task 1 의 테스트가 이미 있다 — 여기서는
  추가하지 않는다)
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-08-05-alternatives-benefit-results.md`

**Interfaces:**
- Consumes: Task 1 이 만든 `failure_reason` 문자열과 `entry_simulation_empty` 이벤트.

- [ ] **Step 1: 기록된 실패를 재생해 사유가 바뀌는지 본다**

`runs/alternatives_ab/off_1/history.jsonl` 의 첫 `simulation` 이벤트를 그대로
읽어(그 이벤트는 `status: "error"`, `measurements: {}`, `warnings` 에
`lod.spice` 문구를 담고 있다) 가짜 `simulate` 로 돌려주고, `run_orchestration`
이 새 사유로 끝나는지 확인한다. **새로 시뮬레이션하지 않는다** — 기록된
이벤트가 이미 그 조건이다.

확인할 것을 그대로 적는다:
- `result["failure_reason"]` 에 `"no measurements"` 와 `"lod.spice"` 가 있다
- `tuning_proposal` 이벤트가 **0건**이다(옛 실행은 `off_1` 에서 3건이었다)
- `iterations_used == 1`

- [ ] **Step 2: 돌려서 확인**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py -k lod -v`
Expected: PASS.

- [ ] **Step 3: CLAUDE.md 를 고친다**

대안 정렬 A/B 항목의 마지막 하위 불릿("A run whose simulator never succeeds
reports the tuner's failure reason, and nothing distinguishes the two")이 지금
**"Not fixed here"** 로 끝난다. 고쳐졌으므로 그 문장을 바꾼다. 적을 것:

- 조건은 `measurements` 가 비었는가이지 `status` 문자열이 아니다 — 이름으로
  의미를 알아보지 않는 규칙이 여기 어떻게 적용됐는지.
- 그 런의 **첫** 시뮬레이션에만 걸린다. 튜닝 이후의 빈 측정은 롤백이 답이다.
- 게이트가 아무 일도 하지 않을 때가 고정돼 있다(부분 실패 테스트).
- 실측: 옛 `off_1` 은 이 조건에서 3번 튜닝하고 75분을 썼다.

- [ ] **Step 4: A/B 결과 문서에 한 줄 더한다**

정정 절 끝에, 이 결함이 제품 쪽에서도 닫혔다는 것과 어느 커밋인지를 적는다.

- [ ] **Step 5: 회귀 확인 + 커밋**

Run: `.venv/bin/python -m pytest -q -m "not slow"`

```bash
git add -A
git commit -m "docs: 진입 시뮬레이션 선행 조건 — 기록된 실패로 확인"
```

---

## 최종 리뷰 후속 (2026-08-07)

구현 자체는 옳다고 확인됐다. 이 절은 **테스트가 고정하지 못한 것**, **틀린 문서
수치**, **새 게이트가 만든 다운스트림 사각지대**를 닫은 라운드의 기록이다.
각 항목은 고친 뒤 **돌연변이를 실제로 다시 넣어** 테스트가 FAIL 하는 것을
확인했다 — 아래 표의 "돌연변이" 열이 그 확인이다.

| # | 무엇이 고정되지 않았나 | 돌연변이 | 잡은 테스트 |
|---|---|---|---|
| I-1 | `entry_simulation_empty` 이벤트를 아무도 단언하지 않았다(부정 단언 하나뿐) | `state.log_event(...)` 줄 삭제 | `..._writes_an_entry_simulation_empty_event` 외 2건 |
| I-2/3 | 리터럴이 원본과 달라 "첫 경고" 규약이 고정되지 않았다 | `warnings[0]` → `warnings[-1]` | `..._detail_quotes_each_testbenchs_first_warning` |
| I-5 | 이벤트에 `attempt` 가 없어 재진입 발화가 구별되지 않았다 | `cli.py` 의 `attempt=attempt` 삭제 | `test_each_reentry_tells_run_orchestration_its_attempt_number` |
| M-3 | 재개된 실행에서 게이트가 걸리는 테스트가 없었다 | `first_simulation` → `outer_iter == 1` | `test_a_resumed_run_still_checks_its_own_first_simulation` |
| M-1 | `final_criteria == []` 에 출처 문장을 찍어 없던 판정을 주장했다 | 빈 목록 가드 삭제 | `..._says_nothing_was_judged_rather_than_naming_a_deck` |
| I-6 | 집계기가 진입 게이트 FAIL 을 "관측된 런"으로 셌다 | 라벨 분기 삭제 | `test_build_row_labels_an_entry_simulation_gate_fail_as_dropped` |

### 판단으로 정한 것

- **`attempt` 는 호출부에서 넘긴다.** `run_orchestration` 은 자기가 몇 번째
  시도인지 알 수 없다 — 재진입 루프가 `cli.py` 에 있고 그 함수는 루프 안에서
  매번 새로 불린다. 안에서 세면 항상 0 이다. `run_orchestration(..., attempt=0)`
  기본값으로 두어 기존 호출부(테스트 60여 곳)는 그대로 둔다.
- **재진입에서 게이트가 다시 걸리는 동작은 바꾸지 않았다.** 재진입은 새
  오케스트레이션의 출발점이고, 거기서 한 점도 못 재면 판정이 정의되지 않는다 —
  루프 *안*에서 튜닝이 덱을 깨뜨리는 것과는 다른 사실이다(그건 롤백이 맡는다).
  고친 것은 **문서의 "never re-armed" 문장**과 **이벤트에 실리는 `attempt`** 다.
- **집계기는 사유 문자열을 복사하지 않고 import 한다**
  (`orchestrator.ENTRY_SIMULATION_EMPTY_REASON`). 손으로 옮긴 사본은 사유
  문장이 한 글자 바뀌는 순간 조용히 안 맞게 되고, 그 침묵은 "진입 게이트 실패가
  없었다" 와 구별되지 않는다. 접두사로 비교한다 — `cli.py` 가 뒤에 최종 스윕
  사유를 덧붙일 수 있어 완전 일치로는 못 잡는다.
- **`dropped` 지 `void` 가 아니다.** 이 실행은 축소를 켰든 껐든 아무것도 재지
  못했으므로 어느 팔에 대해서도 증거가 아니고, `build_row` 가 이미 가진 라벨
  체계(`ok`/`dropped`)에 새 `drop_reason` 을 더하는 것이 이 저장소의 기존
  구분을 지키는 방식이다. 판정이 `void` 로 나가는 것은 `check_measurability`
  가 관측 0 건을 보고 정할 일이지 행 하나가 정할 일이 아니다.
- **M-2(재개가 환경을 다시 확인하지 않는다)는 기록만 했다.** `CLAUDE.md` 에
  한 문장. 재개 정책을 바꾸는 것은 별도 범위다.
- **기존 테스트 픽스처 하나를 고쳤다**:
  `test_the_provenance_names_the_area_phase_when_only_it_moved_the_deck` 이
  최상위 `final_criteria` 를 `[]` 로 둔 채 단계별 `final_criteria` 만 채우고
  있었는데, `cli.py:910-911` / `cli.py:945-946` 이 최상위 키를 **덮으므로**
  프로덕션에 없는 모양이다. 실제 도달 가능한 상태로 픽스처를 고쳤다.
