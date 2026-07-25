# 2단 연산증폭기 복잡 벤치마크 설계

## 배경 및 목표

지금까지 유일한 벤치마크였던 `inverting_amp`는 이상적 op-amp(VCVS)로 만든 단일 기준(gain) 회로라, 여러 판정 기준이 서로 트레이드오프되는 상황에서 튜너/검증 에이전트가 실제로 잘 작동하는지 검증한 적이 없었다. 이 작업은 실제 트랜지스터 레벨 회로로 다기준 트레이드오프가 있는 벤치마크를 추가해서, 파라미터 튜닝 파이프라인이 "한 기준을 고치면 다른 기준이 나빠지는" 현실적인 상황에서도 동작하는지 검증 가능하게 만드는 것이 목표다.

### 범위

- `benchmarks/two_stage_opamp/`에 새 벤치마크(netlist.cir + spec.yaml) 추가
- 실제 ngspice로 시뮬레이션 가능한, 트랜지스터 레벨 2단 CMOS 연산증폭기
- 3개 판정 기준(DC gain, unity-gain bandwidth, phase margin)이 하나의 튜닝 파라미터(Miller 보상 커패시터 `Cc`)를 통해 서로 트레이드오프되도록 설계
- 처음부터 한 기준(phase margin)이 의도적으로 미달하도록 만들어, 튜닝 루프가 실제로 동작하는지 검증 가능하게 함

### 명시적으로 범위 밖

- 토폴로지 변경 튜닝 (별도 작업)
- 특정 파운드리 PDK 사용 (범용 ngspice level-1 모델 사용)
- 이 벤치마크를 사용하는 새로운 자동화 통합 테스트 추가 (기존 `--agent-backend` 플래그로 수동/탐색적 실행이 우선 목표이며, 통합 테스트 추가는 이 스펙 이후 별도로 결정)

## 회로 설계

PMOS 입력 차동쌍(M1/M2) + NMOS 전류미러 부하(M3/M4)로 구성된 1단, NMOS 공통소스(M6) + PMOS 전류원 부하(M7)로 구성된 2단, Miller 커패시터(`Cc`, outA-vout 사이)로 보상하는 고전적인 2단 CMOS 연산증폭기다. 바이어스는 단일 기준전류(`Iref`, PMOS 다이오드 연결 `M9`)에서 미러링해서 결정하며, 별도 PDK 없이 범용 `LEVEL=1` NMOS/PMOS 모델(`.model NMOSG`/`.model PMOSG`)만 사용한다. 1단 출력 노드(`outA`)에 기생 커패시터(`Ca`, 0.3pF)를, 출력 노드(`vout`)에 부하 커패시터(`Cload`, 2pF)를 추가해서 진짜 2-폴 시스템을 만들었다 — 이게 없으면 `Cc` 하나만 있는 사실상 1차 시스템이 되어 phase margin이 거의 180°에 가깝게 나와 트레이드오프 시나리오가 성립하지 않는다.

**테스트벤치 (AC 루프게인 측정법)**: 출력(`vout`)에서 반전 입력(`vinn`)으로 큰 인덕터(`Lfb`, 1MH)를 연결해서 DC에서는 루프를 닫아 바이어스를 자동으로 잡고(음성 피드백이 M6/M7 미스매치를 자동 보정), AC에서는 이 인덕터가 개방되어 루프가 끊긴다. `vinn`에 큰 커패시터(`Cin`, 1F)를 통해 AC 자극 신호를 주입한다. 이 구성에서 `vdb(vout)`/`vp(vout)`을 읽으면 루프게인의 크기/위상을 직접 얻는다 — 표준 SPICE 루프게인 측정 기법이다. `vp(vout)`은 DC에서 180°에 가깝게 시작해서(안정된 음성 피드백) 주파수가 올라갈수록 0°쪽으로 감소하므로, 루프게인이 0dB를 지나는 지점의 위상값이 곧 phase margin이다.

## 판정 기준 및 트레이드오프 (실제 ngspice로 검증)

`Cc=2p`(초기값) 실측: DC gain 87.0dB, UGBW 44.3MHz, phase margin 50.3°.

`Cc`를 스윕한 결과, phase margin과 UGBW가 뚜렷하게 반비례한다:

| Cc | phase margin | UGBW |
|---|---|---|
| 2p (초기값) | 50.3° | 44.3MHz |
| 3p | 56.4° | 31.0MHz |
| 4.1p | 60.1° | 23.1MHz |
| 6p | 63.4° | 16.0MHz |

기준을 DC gain ≥70dB, UGBW ≥20MHz, phase margin ≥60°로 설정하면: 초기값(Cc=2p)에서 gain과 UGBW는 여유 있게 통과하지만 phase margin만 미달(-9.7°)한다. `Cc`를 4.1~약 7p 사이로 늘려야 세 기준을 모두 만족하는 좁은 창이 생긴다 — 너무 적게 늘리면 phase margin이 여전히 부족하고, 너무 많이 늘리면 UGBW가 미달한다. `apply_changes(refdes="Cc", param="value", new_value="4.2p")`로 실제 적용 후 재시뮬레이션까지 확인: gain 87.0dB / UGBW 22.5MHz / phase margin 60.3° — 세 기준 모두 통과.

## 파일

- `benchmarks/two_stage_opamp/netlist.cir`: 회로 (`.subckt OPAMP2STAGE` + AC 루프게인 테스트벤치, `.control` 블록 없음 — 기존 관례대로 `spec.yaml`의 `control_block`에서 공급)
- `benchmarks/two_stage_opamp/spec.yaml`: `control_block`(AC 분석 + 3개 `.meas`)과 3개 `criteria` (dc_gain, unity_gain_bandwidth, phase_margin)

## 검증 방법

`NgspiceBackend.run()`을 통해 실측값이 수동 계산과 일치함을 확인했고, `evaluate_criteria()`로 초기 상태가 정확히 phase_margin 하나만 실패로 판정됨을 확인했다. `apply_changes()`로 `Cc`를 늘린 뒤 재시뮬레이션·재판정까지 실행해서 전체 기준 통과를 확인했다 — 이 세 단계 모두 실제 오케스트레이터가 사용하는 것과 동일한 함수(`NgspiceBackend`, `evaluate_criteria`, `apply_changes`)로 직접 검증했다.
