# 토폴로지 스왑 적용 가능성 (하위 프로젝트 F1) — 설계

## 문제

토폴로지 스왑은 구현돼 있고 단위 테스트·실-ngspice 테스트·전체-브랜치 리뷰를
모두 통과했지만, **벤치마크 하나에서만 켜진다.** `orchestrator.py:66`:

```python
topology_swap_available = len(parse_netlist(initial_netlist_texts[canonical_name]).subckts) == 1
```

- `benchmarks/bandgap`은 `.subckt` 정의가 6개 → 이 값은 **항상 False**.
- 실제 생산 덱(다단 중첩(수십 블록))도 마찬가지로 항상 False.
- 켜지는 유일한 덱은 `benchmarks/two_stage_opamp`(정의 1개).
- 그리고 **꺼진 사실이 런 로그에 남지 않는다.** 이 저장소가 네 번 반복한
  "조용히 아무것도 안 하는 게이트"와 정확히 같은 모양이다
  (`.option scale`, include-only 래퍼 셀, 래퍼 인스턴스 파라미터,
  `area_before == 0` — 네 번 다 실행 로그로는 알아챌 수 없었다).

거기에 더해 **호환성 검사가 아예 없다.** `apply_topology_swap`은 `.subckt`
헤더와 `.ends`를 그대로 두고 본문만 갈아끼우는데, 라이브러리 본문은
`vinp vinn vout vdd vss` 5포트와 sky130 모델명을 **가정**할 뿐 선언하지
않는다. bandgap의 네 앰프는 9포트(`… nbias ncas pbias pcas`)이므로, 만약
스왑이 가능했다면 바이어스 포트 4개가 조용히 뜬 내부 노드가 된다 —
SPICE 문법상 합법이고, 오류 없이 잘못된 회로가 나온다.

그래서 "라이브러리 확장"(F2)은 지금 **소비자가 없는 상태**다. F1은 소비자를
만든다.

## 범위

**F1에 들어가는 것**

- 블록 지정 스왑: 다중 블록 덱에서 어느 `.subckt` 정의를 교체할지 지정.
  점 표기 경로(`OUTER.INNER`) 지원.
- 결정론적 호환성 판정 — 포트, 모델, 스케일. 세 규칙 모두 파스된 사실이며
  추측이 없다.
- 판정을 **게이트가 아니라 생성기**로 사용: 오케스트레이터가 호환 쌍만
  에이전트에 제시한다.
- 스왑이 왜 비가용인지, 어떤 쌍이 왜 기각됐는지 로깅.
- bandgap의 9포트 인터페이스에 맞는 라이브러리 항목 두 개와, 스왑으로만
  풀리는 시드 스펙 하나.

**F1에 들어가지 않는 것 (F2)**

- 수집(ingestion) 에이전트와 결정론적 입회 게이트.
- 회로 클래스 축(bandgap은 amp가 아니다).
- 라이브러리의 본격적 확장.
- 스왑 후 에어리어 게이트 기준선 재설정 — 아래 "에어리어 게이트" 참고.

## 측정 데이터

이 저장소의 규율대로 전부 ngspice 실측이며 손계산이 아니다. 모든 수치는
`benchmarks/bandgap/netlist_loops.cir`(직렬 전압 주입 루프 분해)에
`ac dec 40 1 1g`를 걸어 얻었다. 기준선(현재 출하 중인 덱):

| 루프 | 이득 | 위상여유 | UGBW |
|---|---|---|---|
| trim (TRIMAMP) | 87.54 dB | 81.15° | 4.813 MHz |
| buf1 (BUF_N) | 97.98 dB | 101.56° | 5.010 MHz |
| buf0 (BUF_P) | 100.16 dB | 104.39° | 3.121 MHz |

### 1. 캐스코드(Ahuja/indirect) 보상은 이 덱에서 **기각됐다**

브레인스토밍에서 채택한 항목은 `folded_cascode_indirect_comp` — Miller
`Xcc`+`XRz`를 걷어내고 보상 커패시터를 출력에서 캐스코드 소스 노드로
되돌리는(Saxena–Baker) 구성이었다. 실제로 돌려 보니 **기존 토폴로지가
파라미터 튜닝만으로 모든 축에서 더 낫다.**

지시 보상 (`Xcc`를 `vout` ↔ 캐스코드 소스 노드에, `Rz` 없음), TRIMAMP:

| Cc (L=W) | `ns`(NMOS 캐스코드 소스) | `ny`(PMOS 캐스코드 소스) |
|---|---|---|
| 5 | 21.4° / 40.3 MHz | −47.8° / 89.9 MHz |
| 10 | 62.0° / 16.7 MHz | 24.8° / 44.1 MHz |
| 15 | 80.2° / 8.97 MHz | 56.2° / 26.2 MHz |
| 20 | **89.4° / 5.45 MHz** | 72.4° / 17.2 MHz |
| 30 | 97.3° / 2.56 MHz | 87.4° / 8.96 MHz |
| 40 | 100.6° / 1.47 MHz | — |
| 60 | 103.4° / 0.653 MHz | — |

같은 회로의 Miller+Rz 2차원 스윕 (위상여유 / UGBW). `Rz`는
`res_high_po w=1`의 길이 `l`:

| Cc \ Rz.l | 5 | 15 (출하값) | 30 | 60 | 120 |
|---|---|---|---|---|---|
| 20 | 40.3° / 15.3 M | 55.4° / 15.1 M | 75.4° / 16.1 M | **99.7° / 27.0 M** | 42.1° / 67.8 M |
| 30 | 55.7° / 8.10 M | 72.7° / 7.97 M | 96.2° / 8.93 M | 118.5° / 24.9 M | 45.1° / 68.3 M |
| 40 | 63.4° / 4.90 M | 81.1° / 4.81 M | 106.8° / 5.58 M | 125.4° / 24.8 M | 46.1° / 68.5 M |
| 60 | 70.1° / 2.30 M | 88.3° / 2.25 M | 116.2° / 2.69 M | 129.9° / 25.1 M | 46.7° / 68.7 M |

지시 보상의 최선점은 `ns`/20 = **89.4° / 5.45 MHz**. Miller+Rz는 **같은 캡
면적(20×20)에서 99.7° / 27.0 MHz** 를 낸다 — 위상여유도 UGBW도 더 높다.
지시 보상의 어떤 점도 이 하나를 이기지 못한다.

원인은 기법이 아니라 출하 사이징이다. **TRIMAMP의 `XRz.l = 15`가 심하게
과소 설정돼 있다.** `l`을 60으로 키우면 위상여유가 81° → 125°, UGBW가
4.8 MHz → 24.8 MHz로 동시에 오른다. 즉 이 개선은 **토폴로지 스왑이 아니라
파라미터 튜닝이 도달하는 영역**이다. `l = 120`에서는 다시 무너지므로
(42–47°) 최적은 60 부근이며, 단조롭지 않다.

측정 신뢰성: `meas ac … WHEN tmag=0`이 여러 교차점 중 하나를 잘못 집었을
가능성을 배제하기 위해 `Cc20/Rz60`, `Cc20/Rz15`, `indirect ns20` 세 점의
전 대역 응답을 따로 출력해 **0 dB 교차가 각각 정확히 하나**임을 확인했고,
교차점 위상이 `meas` 값과 일치했다(100.6°/60.5°/89.3°). 세 경우 모두
1 GHz 부근에서 이득이 +6 dB로 되돌아오는 고주파 전방 경로가 보이지만,
교차점보다 훨씬 위이고 모든 변형에서 동일하므로 비교에 영향이 없다.

**이것을 기각 사유로 기록한다.** 라이브러리 항목의 존재 이유는 "값 튜닝으로
는 못 가는 곳에 간다"인데, 이 항목은 그 조건을 만족하지 못한다. 이는
`spec_topology_required.yaml`이 겪은 것과 같은 모양이다 — 그때도 "파라미터
튜닝으로 불가능"이라는 주장이 실제 런에서 무너졌다. 다만 방향이 반대다:
그때는 에이전트가 예상 밖 노브를 찾아냈고, 이번에는 **설계자(나)가 기존
노브의 범위를 과소평가**했다.

부수 소득 하나: `TRIMAMP.XRz.l`은 실제로 큰 개선 여지가 있는 노브다. 이건
F1의 산출물이 아니라 관측이며, 벤치마크 임계값을 건드리지 않는다(현재
`trim_phase_margin >= 70`을 81°로 이미 통과한다).

### 2. 상보 폴드는 **성립한다** — 스왑으로만 풀리는 실패

bandgap은 앰프 네 개가 같은 9포트 인터페이스를 쓰지만 **폴드 극성이 두
가지**다. `BUF_P`만 PMOS 입력쌍 상보 폴드이고, 이유가 덱 주석에 이미
적혀 있다: 그것이 버퍼하는 `vt05`가 **0.5 V**라 NMOS 입력쌍이 닿지 못한다.

`BUF_P` 슬롯에 NMOS 입력 폴드(`BUF_N` 본문)를 넣고 측정:

| 구성 | buf0 이득 | 위상여유 | UGBW | 테일 노드 |
|---|---|---|---|---|
| 출하(PMOS 입력 상보 폴드) | **100.16 dB** | 104.39° | 3.121 MHz | 1.6229 V |
| 시드(NMOS 입력 폴드) | 73.52 dB | 95.92° | 163.8 kHz | **10.1 mV** |
| 시드 + 입력쌍 W 20→40 | 78.88 dB | 100.07° | 285.0 kHz | 17.8 mV |
| 시드 + 입력쌍 W 20→80 | 83.45 dB | 105.09° | 447.9 kHz | 28.3 mV |
| 시드 + 입력쌍 W 20→150 | ngspice 중단 (`wmax` = 100 µm 빈 상한) | | | |

이득이 26.6 dB, UGBW가 19배 무너진다. 원인이 측정으로 확정된다:
`tail = Vcm − Vgs_n = 0.4999 − Vgs_n`이고, 테일 전류원 `Xt`에 **10.1 mV**만
남아 깊은 선형 영역에 들어간다. 이 저장소가 ERRAMP에서 이미 기록한 실패와
같은 노드·같은 모양이다("4 µA·W=16에서 테일이 35 mV, 루프가 레일에 걸림").

**그리고 이건 사이징 문제가 아니다.** 입력쌍을 넓히면 `Vgs_n`이 내려가므로
방향은 맞지만, `Vgs_n`에는 `Vth` 바닥이 있어 폭을 2배 할 때마다 테일이
7 mV씩만 오른다. W=150에서는 sky130의 100 µm 빈 상한에 먼저 부딪혀 런이
하드 에러로 중단된다. 4배(=에어리어 게이트의 상한이자 빈 상한 근처)까지
넓혀도 83.45 dB로, 스왑이 도달하는 100.16 dB에 16.7 dB 모자란다.

**정직한 단서:** 이 저장소는 "측정된 스윕은 무엇이 되는지가 아니라 무엇이
된다고 *알려졌는지*의 경계"라고 이미 두 번 기록했다(`BUF_P.X6.W`,
`Cc`+`M6.W`). 입력쌍 폭은 물리가 가리키는 노브이고 실제로 포화하지만,
유능한 에이전트가 다른 노브를 찾을 가능성을 배제한 주장은 하지 않는다.
시드 스펙의 헤더에 "**그** 노브"라고 쓰지 않는다.

**시드 국소성 확인:** 같은 시드 덱으로 DC 테스트벤치(`dc_tc`)를 돌려 나머지
8개 기준이 전부 유지됨을 확인했다 — `vbg0` 0.4999 → 0.5003 V, `vbgout`·
`vbg1`·TC 불변, `quiescent_current` 212.99 → 178.95 µA(굶은 폴드가 덜
먹는다). 즉 시드는 기존 `spec_seed_*` 변형들과 같은 성질을 갖는다: **정확히
한 기준만** 무너지고 그 해법이 **정확히 한 블록**에 있다.

## 아키텍처

### 판정은 게이트가 아니라 생성기다

지금은 라이브러리 전체를 에이전트에 보여주고 폐집합 멤버십만 확인한다.
다중 블록이 되면 후보가 `(블록, 토폴로지)` 쌍으로 늘어나고 대부분은 포트나
모델이 안 맞으므로, 전부 보여주면 재시도 예산만 태운다.

```python
@dataclass(frozen=True)
class SwapCandidate:
    block_path: str          # "BUF_P" 또는 "OUTER.INNER"
    topology_id: str

@dataclass(frozen=True)
class SwapRejection:
    block_path: str
    topology_id: str
    # "ports", "models", "scale", "missing_in_testbench",
    # "identical_body", "already_tried"
    reason: str
    detail: str

def compatible_swaps(
    netlist_texts: dict[str, str],
    library: dict[str, Topology],
    tried: set[tuple[str, str]],
) -> tuple[list[SwapCandidate], list[SwapRejection]]:
    ...
```

새 모듈 `src/analogcoder/topology_match.py`에 둔다. `topologies.py`는 데이터
(라이브러리)만, 판정 로직은 별도 — `patterns.py`가 `structure.py`와 분리된
것과 같은 이유로, 틀릴 수 있는 쪽을 격리한다.

- 오케스트레이터가 호환 쌍만 에이전트에 제시한다. 에이전트는 실패 기준을
  보고 **고르기만** 한다.
- 기각 쌍은 사유와 함께 `topology_candidates` 이벤트로 로깅된다. 호환 쌍이
  0개면 그 사실과 사유가 남는다 — 새 게이트는 규칙만이 아니라 **아무것도
  하지 않은 기록**과 함께 출하한다.
- 폐집합 멤버십 검사는 그대로 남는다(약한 모델이 없는 id를 낼 수 있다).
  다만 후보가 실제 적용 가능한 것뿐이라 재시도가 낭비되지 않는다.

### 가용성 판단이 바뀐다

`topology_swap_available`(`len(subckts) == 1`)은 **삭제**된다. 대신 매
iteration:

```python
candidates, rejections = compatible_swaps(netlist_texts, TOPOLOGY_LIBRARY, tried)
```

트리거는 그대로다 — `consecutive_rollbacks >= TOPOLOGY_SWITCH_THRESHOLD`(3).
스왑 시도는 `candidates`가 비어 있지 않을 때만 일어난다.

`tried_topologies`는 `set[str]` → `set[tuple[str, str]]`. `BUF_N`에 한 항목을
써 봤다는 것이 `BUF_P`에도 써 봤다는 뜻은 아니다.

## 호환성 규칙

세 규칙 전부 파스된 사실이다. 이름으로 알아보는 추측(`vdd`를 이름으로 전원
레일로 판정하는 것과 같은 종류)은 하나도 들어가지 않는다.

### 포트

라이브러리 항목이 `ports: list[str]`를 **선언**한다. 규칙은 양방향이다:

```
set(topology.ports) == set(target_subckt.ports)
```

- 한 방향만 보면 안 된다. "본문이 요구하는 포트가 대상에 다 있는가"만 보면
  9포트 대상에 5포트 본문을 끼우는 것이 통과하고, 바이어스 포트 4개가 조용히
  뜬 내부 노드가 된다. 반대로 "대상 포트가 본문에 다 쓰이는가"만 보면 그 반대
  경우가 통과한다.
- 순서는 보지 않는다. SPICE 본문은 포트를 **이름으로** 참조하므로 헤더의
  순서와 무관하다. 순서를 요구하는 것은 사실이 아닌 관습을 강요하는 것이다.
- 선언(`ports`)과 본문이 어긋날 수 있다. 라이브러리 테스트가 본문에서
  파생한 포트 참조 집합과 선언을 대조해 이 위험을 닫는다(아래 테스트 1).

### 모델

```
{본문이 쓰는 모델 이름} ⊆ {대상 덱 텍스트에 이미 등장하는 모델 이름}
```

`parse_netlist`는 `.include`를 따라가지 않는다(의도된 제약). 그래도 이
규칙은 판정 가능하다: **덱이 이미 그 모델을 인스턴스화하고 있다면, 그것을
제공하는 include가 존재한다**는 뜻이기 때문이다. 모델 파일을 읽지 않고도
참인 사실이다.

역은 요구하지 않는다. 대상 덱이 본문보다 많은 모델을 쓰는 것은 정상이다.

### 스케일

라이브러리 항목이 `assumes_scale: float`를 선언하고,
`netlist.netlist_scale(deck)`와 일치해야 한다. `W=8`이 8 µm인지 8 m인지가
여기서 갈린다 — 이 저장소가 이미 한 번 크게 당한 자리다.

### 모든 테스트벤치에서 판정한다

기존 게이트들은 canonical 덱만 읽는다. CLAUDE.md가 기록한 대로, 그래서
`run_orchestration`은 "canonical이 아닌 덱에서 `apply_changes`가 실패하는"
`ValueError` 경로를 따로 안고 있다.

토폴로지 스왑은 **모든 테스트벤치 덱에 적용**되므로 판정도 그렇게 한다:

- 후보는 **그 블록을 정의하는 모든 덱**에서 호환일 때만 호환이다.
- 어떤 덱이 그 블록을 정의하지 않으면 그 덱은 건너뛰고
  `missing_in_testbench` 기각 사유로 기록한다. 정의가 없는 곳에 스왑을
  적용할 수는 없고, 일부 테스트벤치만 바뀐 상태로 두는 것은
  `push_netlist_version`이 원자적이어야 한다는 `state.py`의 규약과 어긋난다.

이는 위 `ValueError` 구멍을 토폴로지 경로에 대해 닫는다.

## 스왑 적용

`netlist.apply_topology_swap(text, subckt_name, new_body)`가 점 표기 경로를
받도록 확장된다. 현재는 맨 이름으로 **첫 매치**를 잡는다 — 중첩 정의가 있는
덱에서 잘못된 블록을 교체할 수 있다. 확장 후:

- `"BUF_P"` → 최상위 정의 `BUF_P`.
- `"OUTER.INNER"` → `OUTER` 안의 `INNER` 정의. 경로는 **정확히** 일치해야
  하며, 부분 경로는 추측하지 않고 거부한다 —
  `check_refdes_resolution`의 규칙과 같다.
- 이름이 모호하면(같은 이름의 정의가 서로 다른 스코프에 둘) `ValueError`.

`.subckt` 헤더와 `.ends`는 그대로 두고 본문만 바꾸는 것은 유지된다.
스왑은 **대상 블록의 본문만** 지운다 — 다중 블록 덱에서 다른 블록에 쌓인
파라미터 튜닝은 보존된다. 이는 오늘 동작(정의가 하나뿐이라 전부 날아감)의
자연스러운 개선이며 별도 결정이 아니다.

## 라이브러리 표면

`Topology`에 필드 둘이 추가된다:

```python
@dataclass(frozen=True)
class Topology:
    id: str
    description: str
    subckt_body: str
    addresses: list[str]
    ports: list[str]          # 신규
    assumes_scale: float      # 신규
```

기존 두 항목(`miller_basic`, `miller_nulling_resistor`)은
`ports=["vinp","vinn","vout","vdd","vss"]`, `assumes_scale=1e-6`을 선언한다
(두 본문 모두 `.option scale=1.0u` 아래 µm 단위로 쓰였다).

**신규 항목 둘** — 둘 다 이미 출하 중인 bandgap 덱에서 45코너를 통과한
본문이므로, 새 회로 설계도 새 검증도 필요 없다:

| id | 본문 출처 | 설명 |
|---|---|---|
| `folded_cascode_nmos_in_cs` | `TRIMAMP` | NMOS 입력쌍 폴디드 캐스코드 + PMOS 공통소스 출력, Miller+널링 저항 |
| `folded_cascode_pmos_in_cs` | `BUF_P` | PMOS 입력쌍 **상보** 폴드 + NMOS 공통소스 출력. 입력 공통모드가 NMOS 쌍의 `Vgs` 바닥 아래일 때 쓴다 |

둘 다 `ports=["vinp","vinn","vout","vdd","vss","nbias","ncas","pbias","pcas"]`,
`assumes_scale=1e-6`.

`addresses`는 지금처럼 정보용이다(프롬프트에만 쓰이고 코드가 검사하지
않는다). 회로 클래스 축은 F2로 미룬다.

기각된 `folded_cascode_indirect_comp`는 라이브러리에 넣지 않는다. 기각
사유와 측정 데이터는 이 문서에 남는다.

## 에이전트 표면

`TOPOLOGY_SCHEMA`에 `block_path`가 추가되지만 **`required`에는 넣지
않는다**:

```python
TOPOLOGY_SCHEMA = {
    "type": "object",
    "properties": {
        "topology_id": {"type": "string", "pattern": "^[a-z_][a-z0-9_]*$"},
        "block_path": {"type": "string"},
        "reasoning": {"type": "string"},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": ["topology_id", "reasoning", "confidence"],
}
```

이유는 B에서 비싸게 배웠다: 필수 필드를 하나 늘리면 약한 모델이 그것을
빠뜨렸을 때 **모든 스펙이 하드 FAIL**한다. 대신 오케스트레이터가 해소한다:

- `block_path`가 있으면 그 값으로 후보를 찾는다.
- 없으면, 후보들의 `block_path`가 **하나로 결정될 때만** 그것을 쓴다.
- 결정되지 않으면 재시도 피드백으로 후보 목록을 돌려준다 —
  기존 거부-재시도 루프와 같은 모양, 같은 `MAX_TUNING_RETRIES` 상한.
- **`MAX_TUNING_RETRIES`를 소진해도 런을 끝내지 않는다.** 사유를
  (`topology_unavailable`, `reason: "proposal_unresolved"`) 남기고
  `consecutive_rollbacks`를 0으로 되돌린 뒤, 그 iteration은 **파라미터
  튜닝으로 흘러간다** — 후보가 0개일 때와 정확히 같은 경로다.

마지막 항목이 최종 리뷰에서 고쳐진 부분이며, 그 근거는 이 브랜치가 존재하는
이유인 바로 그 덱들이다. `block_path`는 `required`가 아니므로(위의 약한 모델
보호) 생략은 언제든 일어날 수 있는데, `bandgap/spec.yaml`은 후보 6개에
토폴로지 id당 블록 3개, `spec_seed_topology.yaml`은 후보 7개에 최대 4개다 —
즉 **생략은 이 덱들에서 항상 모호**하고, 옛 동작은 그때마다 3번의 재시도 뒤
런 전체를 FAIL로 끝내며 남은 outer iteration 6개와 아직 살아 있는 파라미터
튜닝 경로를 버렸다. 실측(에이전트만 스텁, ngspice는 실제): `block_path`가
있으면 iteration 4에서 PASS(`buf0_gain_db` 100.158), 빼면 같은 iteration
4에서 FAIL하고 덱이 `netlist_v0`(73.515)로 되돌아갔다. **에스컬레이션 시도의
실패가 에스컬레이션하지 않은 것보다 나빠서는 안 된다.**

이는 area 게이트가 이미 세운 선례와 같다(CLAUDE.md: "면적 기각만으로 재시도를
소진한 것은 즉시 런 실패가 아니라 파라미터 튜닝 롤백처럼 다룬다"). 파라미터
경로도 **LLM verifier가 거부했을 때**(`verify_pre_rejected_any`)에만 하드
FAIL하며, 결정론적 게이트 소진으로는 결코 그러지 않는다.
`consecutive_rollbacks` 리셋이 함께 필요한 이유는 비용이다: 리셋하지 않으면
카운터가 임계값 위에 머물러 이후 **모든** iteration이 다시 3번의 토폴로지 LLM
호출을 태운다(측정: 9회 → 21회). 스왑 iteration은 유지되든 롤백되든 카운터를
리셋한다는 기존 규칙과도 일치한다.

이는 "결정론적 계층이 해소하되 절대 추측하지 않는다"는 이 저장소의 규율
그대로다 — 생략된 `block_path`를 대신 골라 주는 것은 **하지 않는다**.

`TOPOLOGY_TUNER_SYSTEM_PROMPT`는 두 곳이 바뀐다. **게이트 규칙이 바뀌면
그것을 되풀이하는 프롬프트를 반드시 다시 읽는다** — `verify_pre`가 `Xq1.m`을
거부하도록 지시돼 있던 사고의 재발 방지다.

1. 이제 후보가 `(블록, 토폴로지)` 쌍이며, 목록에 있는 쌍만 고를 수 있다고
   명시한다.
2. "the amplifier's internal structure"라는 단수 표현을 고친다 — 덱에 앰프가
   넷일 수 있다.

프롬프트는 호환성 규칙 자체를 되풀이하지 **않는다**. 후보가 이미 걸러진
집합이므로 되풀이할 규칙이 없다. 규칙을 적어 두면 그것이 사실과 어긋날 때
덫이 된다는 것이 이 저장소의 기록이다.

## 로깅

`history.jsonl`에 나가는 것:

- `topology_candidates` — 매 스왑 판단 시점마다, 승인 여부와 무관하게
  **무조건** 기록한다. `{outer_iter, candidates: [...], rejections: [...]}`.
  기각 사유를 사유 코드와 함께 담는다. 위반이 있을 때만 로깅하면
  "검사했고 문제없음"과 "검사가 사라짐"이 로그에서 구별되지 않는다 —
  `optimize_guard_infeasible`에서 이미 세운 원칙이다.
- `topology_unavailable` — 스왑이 이 iteration에 일어나지 않았을 때,
  **사유 코드와 함께**. 이것이 없으면 F1은 다시 "조용히 꺼진 기능"이 된다.
  후보 0개는 하나의 관측이지만 사실은 여럿이고, 사유 코드가 없던 동안
  `.subckt`가 아예 없는 덱(`benchmarks/inverting_amp/spec.yaml`)과 진짜로
  소진된 라이브러리가 **바이트 단위로 같은** 두 줄을 냈다 — 그리고 그 두 줄은
  "검사가 사라짐"과도 구별되지 않았다. 사유 코드
  (`topology_match.unavailable_reason`):
  - `no_subckt_definitions` — 덱에 `.subckt` 정의가 없어 쌍을 열거할 수 없다.
  - `empty_library` — 라이브러리가 비었다.
  - `all_pairs_already_tried` — 모든 쌍을 이 런에서 이미 시도했다.
  - `all_pairs_rejected` — 호환성 규칙이 전부 기각했다.
  - `proposal_unresolved` — 후보는 있었으나 에이전트의 제안이
    `MAX_TUNING_RETRIES` 동안 하나의 쌍으로 해소되지 않았다(위 "에이전트
    표면" 참고). `detail`에 마지막 재시도 피드백이 실린다.

  소진을 **부재가 아니라 기록으로** 만들기 위해, `compatible_swaps`는
  `tried`에 든 쌍을 그냥 건너뛰지 않고 `already_tried` 사유의 기각으로
  남긴다.
- `topology_swap` — 기존 이벤트에 `block_path`가 추가된다.

## 산출물 (result.json / report.md)

스왑은 블록 본문을 **통째로** 갈아끼운다. 실측 실행에서 `BUF_P`의 16소자
본문이 극성도 사이징도 다른 본문으로 바뀌었는데 `result.json`에도
`report.md`에도 그 사실이 한 줄도 없었다 — 최적화 단계에서 이미 같은 값을
치른 모양이다(CLAUDE.md: "결과는 자기가 돌려주는 덱을 설명해야 한다").

- `result["topology_swaps"]` — **항상** 있는 키(스왑이 없으면 빈 목록).
  항목마다 `{outer_iter, block_path, topology_id, unconstrained_refdes,
  stale_baseline_refdes, outcome}`이며 `outcome`은 `"kept"` 또는
  `"rolled_back"`이다. refdes는 **개수**로 싣는다 — 전문은 이미
  `topology_swap` 이벤트에 있고, 여기서 필요한 것은 "면적 게이트가 이 런의
  나머지 구간에서 몇 개를 더 이상 묶지 못하는가"다.
- `report.md`의 `## Topology swaps` 섹션 — 스왑이 없었으면 **그리지 않는다**
  (최적화/코너 축소 섹션과 같은 규칙: 빈 섹션은 "시도했는데 아무것도 못
  했다"로 읽힌다).
- `verify_post`에 넘기는 `applied_changes`에도 `block_path`를 넣는다.
  유지/롤백 판정은 before/after judge 결과로 결정되므로 정확성 문제는 아니지만,
  verifier의 자유 서술 `feedback`이 `history.jsonl`에 남는 사람이 읽는
  기록이고, 앰프가 넷인 bandgap에서 "swapped folded_cascode_pmos_in_cs"는
  어느 블록인지 말하지 않는다.

## 에어리어 게이트

`baseline_components`는 `netlist_v0`에서 한 번만 계산되고 스왑 후 갱신되지
않는다. 기존 주석이 의도된 설계라고 명시하고 있으며(스왑이 들여온 새 소자는
원본에 비교 대상이 없다), **F1은 이를 바꾸지 않는다.**

바꾸는 것은 하나: `topology_swap` 이벤트에 **그 스왑으로 에어리어 제약이
사라진 refdes 목록**을 남긴다. 다중 블록 스왑이 가능해지면 이 경로가 훨씬
자주 열리므로, 규칙이 아니라 침묵을 기록한다는 원칙을 여기에도 적용한다.

## 에러 처리

새 경로는 없다. `compatible_swaps`는 순수 함수이고 LLM을 부르지 않는다.
`apply_topology_swap`의 `ValueError`는 `run_orchestration`의 기존
`except (AgentExecutionError, ValueError)`가 이미 받아 깨끗한 FAIL로
만든다 — 이 조합은 문서화된 것이며 제거하면 안 된다.

## 벤치마크

새 파일 둘:

- `benchmarks/bandgap/netlist_seed_topology.cir` — `netlist_loops.cir`에서
  `BUF_P`의 본문만 `BUF_N`의 NMOS 입력 폴드로 바꾼 것. 다른 모든 것은 동일.
  **`BUF_N`의 본문을 그대로 쓴다** — 라이브러리 항목
  `folded_cascode_nmos_in_cs`(출처 `TRIMAMP`)가 아니다. 둘은 `Xcl` 부하
  커패시터 유무가 다르고, 위 측정표의 73.52 dB / 10.1 mV는 `BUF_N` 본문으로
  잰 값이다. "라이브러리 항목으로 통일"하는 정리는 측정값을 무효로 만든다.
- `benchmarks/bandgap/spec_seed_topology.yaml` — 루프 테스트벤치 하나만
  선언하고, `buf0_loop_gain` 임계값을 **90 dB**로 올린다.

임계값 90 dB의 근거는 위 측정표다: 시드가 73.52 dB, 입력쌍을 소자 빈 상한
근처(W=80)까지 넓혀도 83.45 dB, 스왑이 100.16 dB. 90은 그 사이에 여유 있게
들어간다.

`optimize:` 블록도 `corner_reduction:` 블록도 넣지 않는다 —
`spec_corner_reduction.yaml`과 같은 이유로, 측정하려는 것 하나만 남기고
런타임을 예측 가능하게 유지한다.

## 테스트

1. `tests/unit/test_topologies.py` (확장) — 모든 항목에 대해, `subckt_body`를
   throwaway `.subckt`로 감싸 파싱했을 때 **본문이 참조하는 포트 집합이
   선언된 `ports`와 정확히 같은지** 확인한다. 이것이 선언과 본문이 어긋나는
   위험을 닫는 유일한 장치다. `assumes_scale`이 양수인지도 확인.
2. `tests/unit/test_topology_match.py` (신규) — `compatible_swaps`:
   - 9포트 대상에 5포트 항목은 `ports` 사유로 기각.
   - 5포트 대상에 9포트 항목도 `ports` 사유로 기각(양방향 확인).
   - 덱에 없는 모델을 쓰는 항목은 `models` 사유로 기각.
   - 스케일 불일치는 `scale` 사유로 기각.
   - 한 테스트벤치에만 없는 블록은 `missing_in_testbench` 사유로 기각되고
     **후보에 오르지 않는다**.
   - `tried`에 든 `(블록, 토폴로지)` 쌍은 후보에서 빠지지만, **같은 블록의
     다른 항목**과 **다른 블록의 같은 항목**은 남는다. 그리고 그 탈락은
     `already_tried` 사유의 기각으로 **기록된다** — 부재로 두면 소진이
     "판정이 사라짐"과 구별되지 않는다.
   - `unavailable_reason`이 후보 0개의 서로 다른 사실 넷을 구별한다.
3. `tests/unit/test_netlist.py` (확장) — `apply_topology_swap`의 점 표기
   경로: 중첩 정의를 정확히 지목, 부분 경로 거부, 이름 모호 시 `ValueError`,
   헤더/푸터 보존.
4. `tests/unit/test_orchestrator.py` (확장, 에이전트 모킹):
   - 다중 블록 덱에서 스왑이 **가용**하다(오늘은 불가능).
   - 호환 후보가 0개면 `topology_unavailable`이 **사유 코드와 함께**
     로깅되고 파라미터 모드가 계속된다 — `.subckt`가 없는 덱은
     `no_subckt_definitions`, 규칙이 전부 기각하면 `all_pairs_rejected`,
     라이브러리를 소진하면 `all_pairs_already_tried`로 서로 구별된다.
   - `block_path` 생략 + 후보 블록이 하나 → 해소된다.
   - `block_path` 생략 + 후보 블록이 둘 → 재시도 피드백, 그리고
     `MAX_TUNING_RETRIES`를 소진하면 **런이 끝나지 않고**
     `topology_unavailable`(`reason: "proposal_unresolved"`)가 남은 뒤 그
     iteration이 파라미터 튜닝으로 이어진다. 그 iteration에서 PASS까지 갈 수
     있어야 한다(실측 시나리오). `consecutive_rollbacks`가 0으로 리셋되므로
     이후 iteration이 매번 3번의 토폴로지 LLM 호출을 태우지 않는다(9회 vs
     21회로 못박는다).
   - 스왑이 **대상 블록만** 바꾸고 다른 블록의 튜닝 결과는 보존한다.
   - `topology_candidates`가 승인/기각 양쪽에서 기록된다.
   - 유지된 스왑과 되돌린 스왑이 `result["topology_swaps"]`에 결말과 면적
     개수까지 실리고, 스왑이 없던 실행은 빈 목록을 갖는다.
   - `verify_post`가 받는 `applied_changes`에 `block_path`가 들어 있다.
5. `tests/unit/test_topology_seed_ngspice.py` (신규, 실 ngspice) — 시드 덱을
   그대로 시뮬레이션하면 `buf0_gain_db < 90`이고,
   `apply_topology_swap`으로 `folded_cascode_pmos_in_cs`를 적용하면
   `>= 90`이 된다. LLM 없이, 결정론적으로. 이 테스트가 F1이 실제로 도는지를
   증명하는 자리다. 예상 실행 시간 10초 미만이므로 `slow` 마커를 붙이지
   않는다.
6. `tests/unit/test_report.py` (확장) — `## Topology swaps` 섹션이 블록 경로/
   토폴로지 id/iteration/결말/면적 개수를 적고, 스왑이 없던 실행에는 섹션이
   아예 없다.

전 구간에서 새 테스트마다 "이 테스트는 어떤 변형을 잡는가"를 묻는다 — 이
저장소에서 "통과하지만 아무것도 검증하지 않는 테스트"가 한 브랜치에서만
네 번 나왔고, 전부 **변형을 실제로 적용해서** 잡혔다.

## 비용

LLM 호출 수는 변하지 않는다. `compatible_swaps`는 파싱된 덱 위의 집합 연산
이며 시뮬레이션을 부르지 않는다. 새 실-ngspice 테스트 하나가 10초 미만
추가된다. `pytest -m "not slow"`의 현재 ~45초 예산 안에 든다.

## 성공 기준

1. `benchmarks/bandgap`에서 토폴로지 스왑이 **가용해진다** — 오늘은
   구조적으로 불가능하다.
2. 시드 덱에서 스왑이 `buf0_loop_gain`을 73.52 dB → 100.16 dB로 올리고,
   값 튜닝은 83.45 dB에서 포화한다(둘 다 측정됨).
3. 스왑이 비가용이거나 후보가 0개인 모든 경우에 그 사실과 사유가
   `history.jsonl`에 남는다.
4. 9포트 덱에 5포트 항목을 끼우는 것이 결정론적으로 차단된다.
5. 기존 `two_stage_opamp` 경로의 동작이 바뀌지 않는다(정의가 하나이므로
   후보가 정확히 그 블록 × 두 항목).
