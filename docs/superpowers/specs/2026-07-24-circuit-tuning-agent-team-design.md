# 회로 검증-튜닝 멀티 에이전트 시스템 설계

## 배경 및 목표

회로 netlist를 시뮬레이션으로 검증하고, 판정 기준(spec)을 통과하지 못하면 netlist를 수정해서 재검증하는 과정을 판정 통과할 때까지 자동으로 반복하는 시스템을 만든다. 이 과정을 5개의 전문 에이전트(넷리스트 분석, 시뮬레이션, 판정, 튜닝, 검증)와 이를 조율하는 오케스트레이터로 구성한다.

### MVP 범위

- 회로: 소자 수가 적은 간단한 벤치마크 회로(예: 인버팅 증폭기)로 시작
- 튜닝 범위: **소자 파라미터 값만 수정** (저항/커패시터 값, W/L 등). 토폴로지(소자 추가/제거/재배선)는 고정
- 시뮬레이터: ngspice (로컬에 설치되어 있음, `ngspice-46`)
- 스택: Python + Claude Agent SDK (`claude-agent-sdk-python`)
- 인터페이스: CLI 전용
- 판정 기준(spec): 사전 정의된 벤치마크 세트 + 사용자 커스텀 spec 둘 다 지원 (YAML)
- 오케스트레이션 제어: 결정론적(하드코딩된 규칙) 루프. 오케스트레이터 자체는 LLM이 아님

### 명시적으로 MVP 범위 밖 (향후 확장)

- 토폴로지 변경 튜닝 (Phase B)
- 런(run)을 넘어선 영구 경험 학습 (history 로그를 활용한 사례 기반 참고/RAG)
- 다기준 트레이드오프가 복잡한 벤치마크 (예: 2단 연산증폭기)
- CLI 외 라이브러리/서비스 형태 제공
- HSPICE 등 다른 시뮬레이터 백엔드 (현재는 ngspice만 구현하되, 구조적으로 교체 가능하게 설계)

## 아키텍처 원칙

오케스트레이터는 **순수 Python 코드(결정론적, 하드코딩 규칙 기반)**이고, 5개 전문 에이전트는 각각 독립적인 Claude Agent SDK `query()` 호출이다.

```
┌─────────────────────────────────────────────────────┐
│  Orchestrator (deterministic Python, 하드코딩 규칙)      │
│  - 루프 제어, 재시도 횟수, 롤백, 최종 리포트 생성           │
└─────────────────────────────────────────────────────┘
       │            │            │            │         │
       ▼            ▼            ▼            ▼         ▼
  [분석 에이전트] [시뮬레이션 에이전트] [판정 에이전트] [튜닝 에이전트] [검증 에이전트]
```

각 에이전트는 고유한 system prompt, 역할에 맞게 제한된 custom tool, `output_format` JSON 스키마로 강제된 구조화 출력을 가진다. 오케스트레이터는 자유 텍스트를 파싱하지 않고 항상 정해진 JSON 필드로만 에이전트 결과를 받는다. 이는 흐름 제어 로직을 완전히 예측 가능하고 디버깅 가능하게 만들기 위함이다.

## 에이전트별 역할 및 입출력 스펙

### ① 넷리스트 분석 에이전트 (최초 1회만 실행, 결과 캐싱)

- 입력: 초기 netlist 원문
- 도구: `parse_netlist` — SPICE 문법을 컴포넌트 리스트로 파싱하는 결정론적 도구. 시뮬레이터 방언에 무관한 표준 SPICE 구조(`.subckt`/`.ends` 계층 포함)를 다룬다.
- 출력: `{circuit_type, stages: [{name, role, components}], component_roles: {refdes: role_설명}, tunable_params: [{refdes, param, role_in_circuit}]}`
- 역할: 이게 어떤 회로고 각 소자가 어떤 기능을 하는지 구조적으로 파악. 파라미터 튜닝만 하는 MVP에서는 토폴로지가 바뀌지 않으므로 **한 번만 실행하고 이후 모든 반복에서 재사용**한다.

### ② 시뮬레이션 에이전트

- 입력: 현재 netlist, target spec(필요한 측정 항목)
- 도구: `SimulatorBackend` 어댑터를 통한 시뮬레이션 실행 (아래 "시뮬레이터 어댑터 구조" 참고)
- 출력: `{measurements: {name: value_with_unit}, status: "success"|"convergence_failure"|"error", warnings: []}`
- 역할: spec에 맞는 테스트벤치/measure 지시문 구성, 시뮬레이션 실행, 수렴 실패 시 솔버 옵션 조정 후 재시도(회로값이 아닌 시뮬레이션 설정만 조정), 결과를 정제된 JSON으로 구조화

### ③ 판정 에이전트

- 입력: measurements, target spec의 pass/fail 기준
- 도구: `evaluate_criteria` — 수치 비교를 결정론적으로 계산 (LLM의 산술 오류 방지)
- 출력: `{overall_pass: bool, criteria: [{name, target, actual, pass, margin}], summary}`

### ④ 튜닝 에이전트

- 입력: 넷리스트 분석 결과(캐시), 판정 결과(어떤 기준이 왜 실패했는지), 같은 run 내 과거 시도 이력(같은 실수 반복 방지), 직전 검증 에이전트의 회귀 피드백(있다면)
- 출력: `{proposed_changes: [{refdes, param, old_value, new_value, reasoning}], overall_reasoning, confidence}`
- 사전검토에서 반려된 경우, 반려 사유를 프롬프트에 포함해 재제안한다.

### ⑤ 검증 에이전트 (사전검토 / 사후검증 2가지 모드)

같은 시스템 프롬프트(페르소나)를 공유하되, 호출 시점에 따라 다른 태스크 프롬프트/출력 스키마를 사용한다.

- **사전검토**: 튜닝 에이전트의 제안이 넷리스트 분석·판정 결과에 비춰 타당한지 적용 전에 검토
  - 입력: 넷리스트 분석, 판정 결과, 튜닝 제안
  - 출력: `{approved: bool, concerns: [], feedback}`
- **사후검증**: 적용 후 재시뮬레이션·재판정 결과를 보고 실제로 개선됐는지, 다른 기준이 회귀(regression)했는지 확인
  - 입력: 이전/이후 판정 결과, 실제 적용된 변경사항
  - 출력: `{improved: bool, regressed_criteria: [], recommendation: "keep"|"rollback", feedback}`

## 오케스트레이션 루프 & 재시도/롤백 정책

**상태 관리**: netlist 버전 스택(롤백용), 반복 이력(각 시도의 판정/제안/검증 결과 전체 로그), 캐시된 회로 분석 결과

**하드코딩 상수** (설정으로 조정 가능): `MAX_OUTER_ITERATIONS=10`, `MAX_TUNING_RETRIES=3` (사전검토 반려 시 재제안 횟수)

```
1. 넷리스트 분석 (1회, 캐싱)
2. for outer_iter in 1..MAX_OUTER_ITERATIONS:
   a. 시뮬레이션 → 판정
   b. 판정 통과 → SUCCESS 종료
   c. 판정 실패 →
        for retry in 1..MAX_TUNING_RETRIES:
          튜닝 제안 → 검증(사전검토)
          승인 → break
          반려 → 반려 사유를 튜닝 에이전트에게 피드백으로 전달, 재시도
        모든 재시도 소진해도 반려 → FAILURE 종료 (사유: 튜닝 합의 실패)
   d. 승인된 변경 적용 (오케스트레이터가 직접 netlist 파일 수정, 이전 버전은 스택에 push)
   e. 재시뮬레이션 → 재판정 → 검증(사후검증)
   f. 사후검증이 "rollback" 권고 → 이전 netlist 버전으로 복원, 이번 시도를 실패 이력에 기록
      (이번 outer_iter는 카운트 소모로 간주)
   g. 사후검증이 "keep" 권고 → 새 netlist를 현재 상태로 확정, 루프 계속
3. MAX_OUTER_ITERATIONS 소진 → FAILURE 종료
4. 종료 시 최종 리포트 생성: 성공/실패 여부, 최종 netlist, 전체 반복 이력
```

**확정된 정책**:
- 사전검토 재시도(`MAX_TUNING_RETRIES`)를 모두 소진해도 승인이 안 나면 그 자리에서 전체 실행을 실패로 종료한다 (마지막 제안을 강행 적용하지 않는다).
- 사후검증이 rollback을 권고한 시도도 `outer_iter` 카운트를 소모한 것으로 간주한다.

## 데이터/파일 구조 및 산출물

**입력**
- `netlist.cir`: 사용자가 제공하는 초기 SPICE netlist
- `spec.yaml`: target spec — 판정 기준(gain, bandwidth, phase margin, power 등 threshold), 필요한 시뮬레이션 타입(AC/DC/transient). 벤치마크 세트에도 동일 포맷 사용 (`benchmarks/inverting_amp.yaml` 등으로 사전 정의)

**실행 중 상태**
- `runs/<run_id>/netlist_v0.cir, netlist_v1.cir, ...`: 매 적용마다 새 버전 파일로 저장 (덮어쓰지 않음 → 롤백은 이전 버전 파일 참조)
- `runs/<run_id>/history.jsonl`: 매 스텝(분석/시뮬레이션/판정/튜닝제안/사전검토/사후검증)의 에이전트 입출력을 한 줄씩 append. 디버깅 용도이자, 향후 영구 학습 기능을 추가할 때 학습 데이터 소스로 재사용 가능하도록 미리 남겨둔다.

**출력**
- `runs/<run_id>/result.json`: `{status: "PASS"|"FAIL", final_netlist_path, iterations_used, final_criteria, failure_reason?}`
- `runs/<run_id>/report.md`: 사람이 읽기 좋은 요약 (시도 이력, 최종 변경사항)
- CLI 종료 코드: 성공 0 / 실패 1 (CI 파이프라인 연동 고려)

## 시뮬레이터 어댑터 구조 (ngspice → HSPICE 교체 대비)

현재는 내부망 밖 환경이라 HSPICE를 쓸 수 없어 ngspice로 시작하지만, 추후 HSPICE로 교체할 수 있어야 한다.

**SPICE 계열 시뮬레이터 간 호환성**: `.subckt`/`.ends` 계층 구조는 SPICE 공통 표준이며 ngspice와 HSPICE 모두 동일하게 지원한다. 따라서 넷리스트 분석 에이전트의 `parse_netlist` 도구는 시뮬레이터 무관하게 재사용 가능하다. 실제로 갈리는 지점은 `.measure` 문법 세부 옵션, `.option`/`.lib` corner 지정 방식, PDK `.model` 카드 호환성, Monte Carlo/`.alter`/`.data` 같은 시뮬레이터 전용 확장 기능이다.

**설계**: 시뮬레이션 관련 로직을 `SimulatorBackend` 추상 인터페이스로 감싼다.
- `SimulatorBackend` (추상): `run(netlist, testbench_config) -> RawSimResult` 공통 인터페이스만 정의
- `NgspiceBackend` (MVP 구현체): ngspice CLI 배치 실행
- `HspiceBackend` (향후 구현): 동일 인터페이스로 HSPICE 호출

오케스트레이터가 설정(`--simulator ngspice|hspice`)으로 어떤 백엔드를 쓸지 결정한다. 시뮬레이션 에이전트는 어떤 백엔드가 붙어있는지 몰라도 되고, 판정/튜닝/검증 에이전트는 `measurements` JSON만 받으므로 시뮬레이터 교체에 전혀 영향받지 않는다.

## 향후 확장 로드맵

1. **Phase B — 토폴로지 튜닝**: 소자 추가/제거/재배선까지 튜닝 에이전트 권한 확장. 이 경우 넷리스트 분석을 매 반복마다 재실행해야 할 수 있음
2. **영구 경험 학습**: `history.jsonl`을 여러 run에 걸쳐 누적하고, 튜닝 에이전트가 제안 전에 유사 사례를 조회(RAG)하도록 확장
3. **HSPICE 백엔드**: `HspiceBackend` 구현
4. **복잡한 벤치마크 확장**: 2단 연산증폭기 등 다기준 트레이드오프가 있는 회로
