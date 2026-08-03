# 45코너 코너 축소 편익 측정 구현 계획

> **에이전트 작업자에게:** 필수 하위 스킬 — superpowers:subagent-driven-development 로 태스크 단위 실행한다.

**목표:** 사전 등록된 A/B 6런(축소 ON/OFF × 3)을 돌릴 슬롯과 하니스를 만들고, 재서 채택/기각/`void` 를 판정한다.

**설계 문서(잠김, 절대 수정 금지):** `docs/superpowers/specs/2026-08-03-reduction45-benefit-design.md` — **개정 1 포함.** 규칙을 여기에 다시 쓰지 말고 그 문서를 읽고 따른다.

**선행 확인:** `docs/superpowers/specs/2026-08-03-reduction45-precondition.md` — 비용 실측(반복당 60 시뮬, 스윕의 0.267×)과 슬롯 화면이 거기 있다.

**기술 스택:** Python, ngspice, 기존 `cli.py` 실행 경로, Claude 백엔드(LLM 튜너).

## Global Constraints

- **잠긴 사전 등록을 편집하지 않는다.** 값·격자·판정 규칙은 그 문서가 정한다.
- **`src/analogcoder/` 를 바꾸지 않는다.** 이 측정은 제품 코드에 손대지 않는다. 바꿔야 할 것 같으면 BLOCKED 로 보고한다.
- **슬롯 스펙은 `spec_seed_buf0_droop.yaml` 에서 `pvt_corners` 와 `corner_reduction:` 만 더한다.** 다른 값은 한 글자도 바꾸지 않는다.
- **드리프트 가드는 순서 구속:** 거부 0건을 확인한 **뒤에** 숫자를 올린다.
- 테스트는 노드 ID 단위 전경 실행. 전체 스위트는 태스크 5 에서 한 번만.
- 문서·주석은 한글.

## 파일 구조

| 파일 | 책임 |
|---|---|
| `benchmarks/bandgap/spec_seed_buf0_droop_45.yaml` (신규) | 슬롯 |
| `scripts/reduction45_ab.py` (신규) | 6런 실행. 팔 전환은 한 필드 계수 치환 |
| `scripts/reduction45_aggregate.py` (신규) | 잠긴 규칙을 코드로 옮겨 판정 계산 |
| `docs/superpowers/specs/2026-08-03-reduction45-benefit-results.md` (신규) | 결과 |
| `CLAUDE.md` | 규칙으로 기록 |

---

### Task 1: 슬롯 스펙 authoring 과 **45코너** 기준선 확인

**Files:**
- Create: `benchmarks/bandgap/spec_seed_buf0_droop_45.yaml`
- Modify: `tests/unit/test_control_block_gate.py` (드리프트 가드)

**Interfaces:**
- Produces: 슬롯 스펙 경로. 태스크 2·3 이 이 경로를 쓴다.

- [ ] **Step 1: 슬롯을 만든다**

`benchmarks/bandgap/spec_seed_buf0_droop.yaml` 를 복사하고 **두 블록만** 더한다. 위치는 `circuit_name:` **바로 앞**이며, `spec_corner_reduction_45.yaml` 의 해당 블록과 **바이트 단위로 같아야** 한다:

```yaml
pvt_corners:
  process: ["tt", "ss", "ff", "sf", "fs"]
  voltage: [1.62, 1.8, 1.98]
  temperature: [-40, 27, 125]
corner_reduction:
  enabled: true
  retry_budget: 2
  probe: true
```

머리말 주석에 이 파일이 무엇인지 적는다: 사전 등록 `2026-08-03-reduction45-benefit-design.md` 의 측정 슬롯이고, `spec_seed_buf0_droop.yaml` 에 45코너 격자와 축소 선언만 더한 것이며, **여기서 나온 숫자를 출하 스펙의 성능으로 인용하지 말 것.**

- [ ] **Step 2: 두 블록 외에 다른 차이가 없음을 확인한다**

Run: `diff benchmarks/bandgap/spec_seed_buf0_droop.yaml benchmarks/bandgap/spec_seed_buf0_droop_45.yaml`
Expected: 추가된 줄만 나온다. **삭제되거나 변경된 줄이 하나라도 있으면 실패다.**

- [ ] **Step 3: 스펙이 로드되고 기대한 모양인지 본다**

```python
from analogcoder.spec import load_spec
s = load_spec("benchmarks/bandgap/spec_seed_buf0_droop_45.yaml")
assert len(s.testbenches) == 5
assert len(s.all_criteria) == 22
assert len(s.pvt_corners.corners) == 45
assert s.corner_reduction is not None and s.corner_reduction.enabled
assert s.optimize is None      # 이 씨앗은 optimize 블록이 없다
```

- [ ] **Step 4: 45코너에서 기준선을 확인한다 — 명목이 아니다**

**이 단계가 이 태스크의 이유다.** 조합 스텝의 슬롯 C 는 명목에서만 확인했다가 죽었다.

`.include` 를 반드시 `netlist.resolve_includes` 로 절대화한 뒤 돌린다 — 안 하면 다섯 테스트벤치가 전부 `status=error` 로 나오고 그것을 "측정 없음" 으로 오독하게 된다(선행 확인 문서 §4).

```python
from analogcoder.spec import load_spec
from analogcoder.pvt import run_full_pvt_sweep
from analogcoder.simulators.ngspice import NgspiceBackend
s = load_spec("benchmarks/bandgap/spec_seed_buf0_droop_45.yaml")
texts = {tb.name: open(tb.netlist_path).read() for tb in s.testbenches}
sw = run_full_pvt_sweep(texts, s, NgspiceBackend())
fails = [c["name"] for c in sw["criteria"] if not c["pass"]]
print("실패한 기준:", fails)
print("vbg0_droop 최악:", [c for c in sw["criteria"] if c["name"] == "vbg0_droop"])
```

기대: `fails == ["vbg0_droop"]`, 최악값 **31.6032** 근처(선행 확인의 실측).

**다른 기준이 함께 실패하면 슬롯을 바꾸지 마라.** 사전 등록이 그것을 금지한다. 그 사실을 보고서에 적고 DONE_WITH_CONCERNS 로 보고한다 — 해석의 한계로 결과 문서에 들어간다.

- [ ] **Step 5: 드리프트 가드 — 순서를 지킨다**

먼저 거부가 0건인지 확인하고, **그 다음에** 숫자를 올린다. 새 스펙이 테스트벤치 5개를 들고 오므로 56 → 61 이다.

Run: `.venv/bin/python -m pytest tests/unit/test_control_block_gate.py -q`
독스트링의 이력에 `56 -> 61` 과 그 사유(이 측정의 슬롯)를 한 줄로 덧붙인다.

- [ ] **Step 6: 커밋**

---

### Task 2: 실행 하니스와 집계기

**Files:**
- Create: `scripts/reduction45_ab.py`, `scripts/reduction45_aggregate.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `benchmarks/bandgap/spec_seed_buf0_droop_45.yaml`
- Produces: `runs/reduction45/<arm>_<i>/` 실행 디렉터리, `runs/reduction45/invocations.jsonl`

- [ ] **Step 1: 팔 전환을 계수 치환으로 쓴다**

OFF 팔은 **같은 파일의 한 필드만** 다르다. 두 번째 스펙 파일을 커밋하지 않는다. 하니스가 파생 사본을 만들되 **치환이 정확히 1회**임을 확인한다. 사본은 넷리스트 상대 경로가 풀리도록 **`benchmarks/bandgap/` 안**에 둔다.

```python
import re
OFF_NAME = "benchmarks/bandgap/.reduction45_off.yaml"   # .gitignore 에 넣는다
PAT = re.compile(r"^(\s*enabled:\s*)true\s*$", re.MULTILINE)

def write_off_copy(src: str) -> str:
    text, n = PAT.subn(lambda m: m.group(1) + "false", open(src).read())
    if n != 1:
        raise SystemExit(f"enabled 치환이 {n}회다 - 1회여야 한다. 슬롯이 바뀌었다.")
    open(OFF_NAME, "w").write(text)
    return OFF_NAME
```

`.gitignore` 에 `benchmarks/bandgap/.reduction45_off.yaml` 을 더한다. 실행이 끝나면 `finally` 로 지운다.

- [ ] **Step 2: 6런을 돌린다 — 상한과 append**

팔마다 3회, 총 6런. 각 런은 `.venv/bin/analogcoder --spec <경로> --run-dir runs/reduction45/<arm>_<i>`.

- **상한 40분.** macOS 에 coreutils 의 `timeout` 이 **없다**(확인함). 자식을 백그라운드로 띄우고 경과를 재다가 `SIGTERM` → 5초 → `SIGKILL` 하는 감시견을 직접 쓴다. 죽였으면 `killed_by_cap: true` 로 기록한다.
- **런 하나가 끝날 때마다 결과를 디스크에 append 한다.** 중간에 죽어도 끝난 것은 남아야 한다.
- 실행 순서는 `off_1, on_1, off_2, on_2, off_3, on_3` — 한 팔을 몰아서 돌리면 환경 드리프트가 팔과 섞인다.

각 런에 대해 `runs/reduction45/invocations.jsonl` 한 줄:
`{arm, index, spec, exit, killed_by_cap, elapsed_s, run_dir}`

- [ ] **Step 3: 집계기 — 잠긴 규칙을 옮기기만 한다**

`scripts/reduction45_aggregate.py` 는 규칙을 새로 정하지 않는다. 각 함수의 독스트링에 잠긴 문서의 문장을 그대로 적는다.

읽을 것은 실행마다 `result.json` 과 `history.jsonl` 이고, 뽑을 값은:

| 이름 | 출처 | 왜 |
|---|---|---|
| `status`, `reason` | `result.json` | 실행의 판정 |
| `mid_pass_sweep_fail` | 아래 정의 | **1차 지표** |
| `loop_sims` | `history.jsonl` | 채택 규칙의 비 (개정 1) |
| `area_phase_sims` | `history.jsonl` | 부수 기록 (개정 1 로 합계에서 제외) |
| `reentry_count` | `corner_*` 이벤트 | 부수 |
| `iterations`, `wall_clock_s`, 캐시 적중/불발 | | 부수 |
| `area_optimization.corner_confirmed`, 착지 버전 | `result.json` | **개정 1 의 확인 사항** |

`mid_pass_sweep_fail` 의 정의는 사전 등록의 문장 그대로다: **중간 루프가 PASS 로 나온 뒤 최종 스윕이 실패했는가.** 구현할 때 "실행 전체가 FAIL 로 끝났다" 와 뭉개지 않는다 — 재진입이 있으면 실행은 결국 PASS 로 끝날 수 있고, 그래도 그 사건은 일어난 것이다.

**행이 탈락하는 경로(`no_result_json`, `killed_by_cap`)는 소리 없이 빠지면 안 된다.** 라벨을 붙여 출력하고, 판정에서는 **채택에 불리한 쪽**으로 작용하게 한다.

- [ ] **Step 4: 하니스 단위 시험 (ngspice·LLM 없이)**

`tests/unit/test_reduction45_ab.py` (신규). 가짜 `result.json`/`history.jsonl` 로 순수 함수를 시험한다:

- `enabled: true` 가 정확히 1회 치환되고, 0회나 2회면 죽는다
- `mid_pass_sweep_fail` 이 "실행이 FAIL 로 끝났다" 와 **다른 값을 내는** 경우가 있다
- 선행 조건 P 가 성립하지 않으면 판정이 `void` 다
- 채택 규칙이 정확성과 비용을 **둘 다** 요구한다(하나만 만족하면 기각)
- `killed_by_cap` 인 런이 채택을 막는다

**각 시험이 통과할 수밖에 없는 것이 아닌지 확인한다.** 최소 두 개는 구현을 망가뜨려 실제로 깨지는 것을 보이고 그 결과를 보고서에 적는다.

- [ ] **Step 5: 커밋**

---

### Task 3: 6런 실행

**Files:** 없음(산출물만)

- [ ] **Step 1:** 잠긴 사전 등록을 다시 읽는다. 규칙을 재진술하지 말고 따른다.
- [ ] **Step 2:** `scripts/reduction45_ab.py` 로 6런을 돌린다. 예산 ≈ 2시간.
- [ ] **Step 3:** 끝나면 `scripts/reduction45_aggregate.py` 로 판정을 계산하고 산출물을 `docs/superpowers/specs/data/` 로 옮긴다.
- [ ] **Step 4:** **선행 조건 P 를 먼저 확인한다.** 성립하지 않으면 거기서 멈추고 `void` 로 보고한다 — 채택 규칙을 적용하지 않는다.
- [ ] **Step 5:** 커밋

---

### Task 4: 결과 문서

**Files:**
- Create: `docs/superpowers/specs/2026-08-03-reduction45-benefit-results.md`

- [ ] **Step 1:** 사전 등록의 **어느 조항이 발화했는지**를 이름으로 적는다(`void` / 채택 / 기각).
- [ ] **Step 2:** 표의 모든 숫자가 실행 산출물에서 재현되는지 확인한다. 재현 방법도 적는다.
- [ ] **Step 3:** 개정 1 의 확인 사항을 실행마다 적는다 — 면적 단계가 코너 확인된 버전에 착지했는가.
- [ ] **Step 4:** 부수 관찰은 **판정에 쓰지 않는다**고 명시하고, 실제로 쓰지 않는다.
- [ ] **Step 5:** 이 측정이 답하지 못하는 것(덱 하나·격자 하나·씨앗 하나·k=3·검정 없음·탐침 분리 불가)을 적는다.
- [ ] **Step 6:** 커밋

---

### Task 5: `CLAUDE.md`

- [ ] **Step 1:** 판정을 규칙으로 적는다. `void` 면 **조건이 발생하지 않았음**을 적는다 — 채택·기각·`void` 는 서로 다른 사실이다.
- [ ] **Step 2:** 드리프트 가드. **순서 구속** — `pytest -m "not slow"` 를 전경에서 한 번 돌려 통과를 확인한 **뒤에** 숫자를 고친다. 현재 1573 / 2 / 9 / 98.66 s.
- [ ] **Step 3:** 커밋

---

## 이 계획이 다루지 않는 것

- **`coverage:` 체제.** 2026-07-30 에 자기 사전 등록 규칙으로 기각됐다.
- **`max_corners` 상한.** 선행 확인이 이 덱·격자에서 불필요함을 실측했다.
- **`two_stage_opamp` 의 세 번째 바이어스 해.** 별개의 결함이고 여기서 고치지 않는다.
- **두 번째 덱.** 슬롯이 하나인 것은 기록된 한계이지 이 계획이 고칠 것이 아니다.
