# 면적 게이트: 래퍼 셀 인스턴스 파라미터 (e2-followup-real-deck)

## 요약

`check_area_growth`가 래퍼 셀 덱의 사이징 노브에 대해 아무 제약도 걸지
않던 구멍을 닫았다. 인스턴스 줄의 파라미터가 **도달하는 본문 토큰**을 추적해
티어를 정하고, 총 폭이 `w × m`이므로 도달한 물리 소자별로 묶어 **곱**을 본다.

이 게이트가 조용히 무력했던 것은 이번이 세 번째다 (`.option scale` 미독해,
MiM 캡을 패럿으로 비교). 원인은 매번 다르고 결과는 매번 같다.

## 무엇을 바꿨나

### `src/analogcoder/netlist.py`
- `TracedTarget` 데이터클래스 추가: `(device, token, total_width)`.
  왜 이름이 아니라 토큰인지를 독스트링에 기록했다 — 인스턴스 파라미터 이름
  (`wn`, `ma1`)은 설계자의 명명 규칙이라 추측이지만, 그 값이 도달하는 본문
  토큰(`w`, `l`, `m`, `nf`)은 SPICE 표준 소자 문법이라 사실이다.
- `Component.traced_params: dict[str, list[TracedTarget]]` 필드 추가.

### `src/analogcoder/params.py`
- `free_names(raw)` — 표현식이 참조하는 파라미터 이름들 (`resolve_value`와
  같은 ast 화이트리스트).
- `_instance_env(...)` — **인스턴스 하나**에 대한 서브회로 내부 환경.
  우선순위: 본문 `.param` < `.subckt` 줄 기본값 < 인스턴스 오버라이드.
  오버라이드 값은 바깥 스코프에서 먼저 해소하고, 바깥에서 확정 못 하면
  기본값으로 되돌아가지 않고 가린다.
- `_trace(...)` — 본문에서 파라미터가 도달하는 소자/토큰 목록. 본문 소자가
  덱 안에 정의된 서브회로 인스턴스면 따라 들어간다(`_MAX_TRACE_DEPTH=8`).
  덱이 정의하지 않은 서브회로(PDK 프리미티브)는 잎.
- `annotate_traced_params(text, parsed, envs)` — 모든 인스턴스에 대해 채운다.

### `src/analogcoder/area_limits.py`
- `index_baseline_components`가 `annotate_traced_params`를 호출한다
  (`check_area_growth`는 넷리스트 원문이 아니라 이 표만 받으므로).
- 토큰별 취급 상수: `_GEOMETRY_TOKENS={w,l}`, `_COUNT_TOKENS={m}`,
  `_NEUTRAL_TOKENS={nf}`, `COUNT_ALLOWED_MULTIPLIER=2.0`.
- `_tier_baseline_value`: 트랜지스터의 티어 키가 폭이 아니라 **총 폭
  (`w × m`)**. 토큰 조회는 대소문자 무시(`W=30`과 `w=1`이 한 덱에 공존).
  `pnp_05v5`(ctype Q)는 이미 `m` 자체가 티어 키이므로 두 번 곱하지 않는다.
- `check_area_growth` 재구성: 변경별이 아니라 **도달한 물리 소자별 그룹**으로
  비율을 곱하고, 허용 배수는 관여한 파라미터들의 티어 중 가장 빡빡한 것.
  `nf`는 곱에서 제외. `m`/`nf`의 비정수 값은 즉시 거부.

## 티어/허용 배수 (합성 픽스처, 실측)

픽스처는 사용자 IP가 아니라 모양만 옮긴 것이다 (실물 넷리스트는 이 저장소에
넣지 않았다). 같은 정의를 서로 다른 값으로 두 번 인스턴스화한다:

```
xin1 ... WRAPCELL_A wn=2e-6  ln=3e-6 ma1=4 mb1=4 nf_n=1 geomod=1
xin2 ... WRAPCELL_A wn=20e-6 ln=3e-6 ma1=2 mb1=2 nf_n=1 geomod=1
```

| 변경 | 도달 토큰 | ctype | 허용 배수 |
|---|---|---|---|
| `xin1.wn` | `ma1.w`, `mb1.w` (소자 2개) | M | 3.0x (기하 티어, 총 폭 8.0um) |
| `xin1.ln` | `ma1.l`, `mb1.l` | M | 3.0x (기하 티어, 총 폭 8.0um) |
| `xin1.ma1` | `ma1.m` (소자 1개) | M | 2.0x (평평한 개수 티어) |
| `xin1.mb1` | `mb1.m` | M | 2.0x (평평한 개수 티어) |
| `xin1.nf_n` | `ma1.nf`, `mb1.nf` | M | 없음 — 면적 중립 (판단할 것이 없음) |
| `xin1.geomod` | `ma1.geomod`, `mb1.geomod` | M | 없음 — 크기가 아님 |
| `xin2.wn` | `ma1.w`, `mb1.w` | M | **2.0x** (기하 티어, 총 폭 40.0um) |
| `xin2.ma1` | `ma1.m` | M | 2.0x |

`xin1`과 `xin2`가 같은 정의인데 다른 티어를 받는 것이 인스턴스별 해소가
동작한다는 증거다 — `build_param_envs`는 `wn`/`ma1`을 (정당하게) 버린다
(`test_build_param_envs_drops_a_name_the_instances_disagree_on`).

### 6x 구멍

```
[{"refdes":"xin1","param":"wn","new_value":"6e-6"},    # 3x, 단독 허용
 {"refdes":"xin1","param":"ma1","new_value":"8"}]      # 2x, 단독 허용
-> (False, 'xin1 -> WRAPCELL_A.ma1: proposed change grows area by 6.00x,
            exceeding the 2.0x limit for its size tier')
```
형제 `mb1`은 `wn`의 3x만 받고 3.0x 티어라 통과한다 — 그룹이 소자 단위임을
보여준다.

## TDD 기록

RED (구현 전):
```
13 failed, 58 passed in 0.15s
  tests/unit/test_params.py       5 failed
  tests/unit/test_area_limits.py  8 failed
```
(`test_build_param_envs_drops_a_name_the_instances_disagree_on`은 기존 동작을
못박는 테스트라 처음부터 통과했다.)

GREEN (구현 후): `tests/unit/test_params.py tests/unit/test_area_limits.py`
71 passed.

새 테스트 19개:
- params: 본문 토큰 착지, 인스턴스별 총 폭, 중첩 인스턴스 추적, PDK
  프리미티브에서 잎으로 멈춤, 도달하지 않는 파라미터는 추적 없음,
  `build_param_envs`가 갈린 이름을 버림.
- area_limits: 폭 성장 차단/허용, 인스턴스별 티어, 개수 평평 2.0x, w×m 곱,
  각각 단독으로는 허용됨(곱 때문에 막혔음을 못박음), nf 중립·곱 제외,
  거부 피드백에 nf 제외 사실 명시, 비정수 개수 거부(증가/감소 양쪽),
  크기가 아닌 토큰 무제약, 추적 실패 시 막지 않음, PDK 래퍼의 scale 반영.

## 벤치마크 무변화 증명

두 가지로 확인했다.

1. **기존 테스트 전부 통과** — sky130 기하 티어, `BUF_P.Xcl` 20→50 (3.0x
   티어), `pnp_05v5.m` 평평 2.0x를 못박는 테스트들이 그대로 통과한다.
2. **구현 전/후 덤프 diff** — `benchmarks/*/*.cir` 10개 덱의 모든 컴포넌트에
   대해 `(_classify_ctype, _tier_baseline_value, allowed_multiplier_for,
   traced_params)`를 덤프해 `git stash`한 구버전과 비교: **완전히 동일**.
   벤치마크 덱에는 로컬 정의 서브회로에 파라미터를 넘기는 인스턴스가 하나도
   없어서(`grep`으로 확인) 추적 경로가 아예 발동하지 않고, `m=`을 쓰는 것은
   `pnp_05v5`뿐이라 총 폭 곱셈도 무영향이다. `nf=`는 벤치마크에 없다.

## 전체 스위트

```
470 passed, 2 skipped in 33.41s
```
(작업 전: 451 passed, 2 skipped.)

## 남는 것 / 판단

- 직접 주소지정 경로에도 같은 규칙을 적용했다: 소자를 직접 가리키는 변경에서
  `param`이 곧 그 소자의 토큰이므로 `m`은 2.0x로, `nf`는 중립으로, 정수 조건도
  똑같이 본다. 규칙을 두 벌로 나누는 것보다 낫다고 판단했고, 벤치마크에는
  영향이 없음을 위 diff로 확인했다.
- 추적은 `name=value` 토큰만 본다. 파라미터가 위치 토큰(`R1 a b rval`)에만
  도달하면 추적되지 않고 "판단 불가 → 막지 않음"으로 떨어진다. 요구된 표에
  `w/l/m/nf` 외는 "크기가 아님"이므로 의도적 범위다.
- `total_width`를 확정하지 못한 소자(예: `w`가 해소 불가)는 그 소자에 대해서만
  판단을 포기한다 — 나머지 도달 소자는 그대로 판정한다.
