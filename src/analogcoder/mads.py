"""신뢰영역 직접탐색(MADS) - `optimizer.py`의 좌표 하강 옆에 붙는 두 번째
탐색 전략.

로드맵 단계 3이 겨냥한 실측 약점은 셋이다: 탐색이 **노브 간 결합을 못 보고**,
스텝이 **고정 비율**(×0.9)이며, **코너에 눈이 멀어** 있다. 이 모듈은 앞의
둘을 친다. 셋째는 탐색기가 고칠 수 없다 - `spec_pvt.yaml`에 corner_reduction
블록이 없으므로 탐색이 보는 것은 nominal 한 점이고, 적응 스텝은 **어디에 점을
찍는가**를 바꿀 뿐 **무엇을 보는가**를 바꾸지 않는다.

`coordinate_descent`는 이 브랜치에서 **한 줄도 바뀌지 않는다.** 그것이 A/B의
기준선이고, 바꾸면 비교가 무의미해진다.


무엇이 MADS이고 무엇이 아닌가 (과장하지 않기 위해 적는다)
---------------------------------------------------------

구현된 것 - 여기까지는 Audet–Dennis(2006) MADS의 구조 그대로다.

- **적응 폴 반경** Δ: 성공하면 ×τ, **완결된** 폴이 실패하면 ÷τ.
- **메시** δ = Δ/ρ 로 폴 점이 격자 위에 놓이고, Δ가 줄면 δ/Δ도 **함께** 0으로
  간다. 이것이 MADS를 GPS와 가르는 정의적 성질이다(GPS는 δ/Δ가 상수).
- **이터레이션마다 바뀌는 방향 집합** - 하알톤 수열로 회전하는 정수 직교
  기저(OrthoMADS, Abramson et al. 2009)이고 **난수가 없다**.
- **극단 배리어**(extreme barrier): 실행불가능한 점은 그냥 버린다. 우리
  이음매가 정확히 그 모양이라 - `StepOutcome`은 accepted 불리언과 문장만
  준다 - MADS를 고른 가장 강한 이유다. 진행 배리어(2009)는 제약 위반량
  h(x)를 요구하는데 이음매에 그 수치가 **없다**. 기법의 한계가 아니라
  이음매의 사실이므로 그렇게 기록한다.
- **기회주의적 폴 + 동적 정렬**: 성공하면 남은 방향을 버리고, 다음 폴은 마지막
  성공 방향부터 시도한다.

구현되지 **않은** 것 - 그래서 "NOMAD와 같다"고 말하면 안 된다.

- OrthoMADS의 조정 하알톤(2^{t/2} 스케일링). 정규화된 방향 집합이 구면에서
  조밀해진다는 **밀도 증명**이 여기 걸려 있고, 그것 없이는 우리가 얻는 것은
  "선언된 원뿔 안에서의 정상점"이지 일반 Clarke 정상성이 아니다. 원뿔 안의
  보장은 폴 집합이 그 원뿔을 양생성하는 데서 나오고(아래), 그것은 단위
  테스트로 박혀 있다.
- 탐색(SEARCH) 단계. 폴(POLL)만 쓴다. HSPICE의 수반 민감도가 확보되면
  기울기 유도 SEARCH를 여기 꽂을 수 있고, MADS의 SEARCH/POLL 분리가 그
  확장을 이미 수용한다 - MADS를 고른 부수 이유 하나다.


상수 셋의 출처
--------------

- ``MADS_INITIAL_POLL_SIZE = |ln 0.9|`` — **실측이 아니라 현행에서 읽어 온
  값이다.** 이렇게 잡아야 MADS의 첫 폴 점이 좌표 하강의 첫 점(8.0 → 7.2)과
  **정확히** 같아진다. 두 팔이 다른 출발점에서 갈라지면 이긴 쪽이 알고리즘
  덕인지 출발점 덕인지 갈리지 않는다.
- ``MADS_STEP_FACTOR = 2.0`` (τ) — **MADS 문헌(Audet–Dennis 2006)의 표준값이고
  이 저장소에서 측정된 값이 아니다. 관례다.** 이 저장소가 값을 치른 세
  상수(가드밴드 비율, 큐레이션 허용오차, 면적 티어)와 **종류가 다르다**: 셋은
  전부 판정 임계값이라 틀리면 승인/거부가 조용히 뒤집힌다. τ는 **일정
  상수**이고 `accept_step`은 전략 밖에 있으므로(optimizer.py), τ가 나쁘면
  시뮬레이션을 더 쓸 뿐 판정이 뒤집히지 않는다. **이것은 논증이지 측정이
  아니다** - 그래서 사전 등록에 τ=2를 판정 팔로 고정했고, 결과를 본 뒤
  만지지 않는다.
- ``mesh_count`` (ρ = max(1, round(1/Δ))) — LT-MADS의 δ = min(Δ, Δ²)를 정수
  메시 칸수로 반올림한 것이다. 반올림하는 이유는 위 앵커다: ‖d‖_∞ = ρ 이고
  δ = Δ/ρ 이면 순수 좌표 방향의 반경이 **정확히** Δ가 된다.

**상한도 종료 허용오차도 새로 만들지 않는다.** 상한은 면적 티어와 시뮬레이션
실패가 이미 하고 있고(줄어드는 쪽에 바닥이 없는 것은 PDK 최소치수가 추측
금지이기 때문이다 - `_next_value` 참고), 종료는 예산 소진이 한다. 새 상수를
만들 때마다 근거를 대야 하는데, 이미 있는 규칙이 그 자리를 메우고 있다.


이 전략이 아무것도 안 하면 로그가 어떻게 보이는가
-------------------------------------------------

게이트에 적용하는 질문을 탐색기에도 적용한다. 세 가지 무력한 사태가 있고,
셋 다 `mads_poll` / `mads_summary` 이벤트에서 **서로 다르게** 보인다.

- 첫 폴에서 전부 거절 → `poll_complete: true`, `success: false`,
  `mesh: "contract"`가 반복되고 `mads_summary.expands == 0`.
- 예산이 n+1보다 적어 폴이 한 번도 완결되지 않음 → `poll_complete: false`,
  `mesh: "hold"`, `contracts == 0`. 완결되지 않은 폴에서 축소하는 것은 보지
  않은 방향을 실패로 단정하는 것이라 **하지 않는다.**
- 매 폴 첫 방향에서 성공 → `mesh`가 전부 `"expand"`, `contracts == 0`.

셋 다 "MADS가 졌다"가 아니라 **"MADS가 돈 적이 없다"**이므로,
`mads_summary.adaptive_step_exercised`(축소 ≥1 **그리고** 확대 ≥1)가 판정
자격 조건을 사후 재구성이 아니라 **결정되는 자리에서** 기록한다. 결합도
마찬가지다: 노브가 하나면 폴 집합이 방향 하나이므로 복합 이동이 원리적으로
존재하지 않고, `coupling_observable`이 그것을 그대로 적는다.

그리고 좌표 하강이 아무것도 못 한 실행과는 **이벤트 이름으로** 갈린다 -
좌표 하강은 `optimize_step`만 남기고 `mads_*`를 하나도 남기지 않는다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from analogcoder.optimizer import (
    SEARCH_STRATEGIES,
    STEP_RATIO,
    Knob,
    KnobState,
    ProposedStep,
    SearchRun,
    _format_value,
)

# Δ0. 유일하게 현행 코드에서 읽어 온 상수다 - 위 모듈 독스트링 참고.
MADS_INITIAL_POLL_SIZE = abs(math.log(STEP_RATIO))
# τ. 문헌 표준값이고 실측이 아니다 - 위 모듈 독스트링 참고.
MADS_STEP_FACTOR = 2.0


# ---------------------------------------------------------------------------
# 방향 생성 - 결정론적이고, 불변식이 tests/unit/test_mads_directions.py에 박혀
# 있다. 여기에 난수를 들이면 scripts/search_ab.py의 자기검사가 깨진다.
# ---------------------------------------------------------------------------


def _primes(count: int) -> list[int]:
    """앞에서부터 소수 count개. 하알톤 수열의 밑이다."""
    primes: list[int] = []
    candidate = 2
    while len(primes) < count:
        if all(candidate % p for p in primes):
            primes.append(candidate)
        candidate += 1
    return primes


def halton(index: int, base: int) -> float:
    """하알톤 수열(기수 역전). 난수가 아니라 저불일치 수열이다 - 결정론적인
    채로 방향이 매 이터레이션 달라지는 것이 MADS가 GPS와 다른 지점이다."""
    result = 0.0
    fraction = 1.0
    while index > 0:
        fraction /= base
        result += fraction * (index % base)
        index //= base
    return result


def halton_point(n: int, index: int) -> list[float]:
    """(0,1)^n 안의 하알톤 점 하나."""
    return [halton(index, base) for base in _primes(n)]


def householder_columns(q: list[int]) -> list[list[int]]:
    """H = ‖q‖²I − 2qqᵀ 의 열들.

    **어떤 정수 q에 대해서도** 정수 행렬이고 열이 서로 정확히 직교한다(각 열의
    노름² = ‖q‖⁴). 부동소수 정규직교화가 필요 없다는 뜻이고, 그래서 이 자리에는
    반올림으로 조용히 틀릴 여지가 없다 - 설계 문서가 "구현상 가장 미묘한 곳"으로
    지목한 부분을 정수 산술로 밀어낸 것이 이 함수다."""
    norm_sq = sum(value * value for value in q)
    size = len(q)
    return [
        [(norm_sq if row == col else 0) - 2 * q[row] * q[col] for row in range(size)]
        for col in range(size)
    ]


def minimal_positive_basis(columns: list[list[int]]) -> list[list[int]]:
    """최소 양기저 {h₁..hₙ, −Σhᵢ}. 2n개가 아니라 n+1개를 쓰는 이유는 예산이다 -
    실패하는 폴은 집합 전체를 재야 하고, 그 비용이 이 전략의 진짜 비용이다.

    합이 0이라는 것이 R^n 양생성의 구성적 증거다(앞 n개가 선형독립일 때).
    LP를 풀지 않고 단위 테스트로 확인할 수 있는 형태라서 이렇게 만든다."""
    size = len(columns[0])
    tail = [-sum(column[i] for column in columns) for i in range(size)]
    return [list(column) for column in columns] + [tail]


def scale_to_infinity_norm(vector, rho: int) -> tuple[int, ...]:
    """‖d‖_∞ = ρ 가 되도록 정수로 재는 방향.

    폴 점은 x + δ·d 이고 δ = Δ/ρ 이므로, 이 스케일이 있어야 반경이 **정확히**
    Δ가 된다 - 순수 좌표 방향에서 ×0.9가 나오는 근거가 이것 하나다."""
    longest = max((abs(value) for value in vector), default=0)
    if longest == 0:
        return tuple(0 for _ in vector)
    return tuple(int(round(rho * value / longest)) for value in vector)


def mesh_count(delta: float) -> int:
    """메시 칸수 ρ. δ = Δ/ρ 이므로 ρ가 클수록 격자가 곱다.

    ρ = max(1, round(1/Δ)) 는 LT-MADS의 δ = min(Δ, Δ²)를 정수로 반올림한
    것이다. Δ ≥ 1이면 ρ = 1, 즉 δ = Δ - 더 나눌 이유가 없는 구간이다."""
    return max(1, int(round(1.0 / delta))) if delta > 0 else 1


def composite_directions(n: int, index: int, rho: int) -> list[tuple[int, ...]]:
    """이 이터레이션의 복합 방향 n+1개. 하나가 여러 노브를 **동시에** 옮긴다 -
    그것이 결합을 보는 방식이고, `run.attempt`가 이미 목록을 받으므로 이음매
    변경이 필요 없다."""
    point = halton_point(n, index)
    raw = [2.0 * value - 1.0 for value in point]
    norm = math.sqrt(sum(value * value for value in raw))
    if norm == 0.0:
        # 하알톤 점이 정확히 중심에 떨어진 경우(n=1, index=1). 방향이 없으므로
        # 첫 축을 쓴다 - 추측이 아니라 퇴화 입력의 명시적 처리다.
        q = [1] + [0] * (n - 1)
    else:
        q = [int(round(rho * value / norm)) for value in raw]
        if not any(q):
            # ρ가 작고 n이 크면 전부 0으로 반올림될 수 있다. 가장 긴 성분만
            # 살린다 - q ≠ 0 이기만 하면 H는 여전히 정확히 직교한다.
            longest = max(range(n), key=lambda i: abs(raw[i]))
            q[longest] = 1 if raw[longest] > 0 else -1
    basis = minimal_positive_basis(householder_columns(q))
    return [scale_to_infinity_norm(vector, rho) for vector in basis]


def coordinate_directions(signs: tuple[int, ...], rho: int) -> list[tuple[int, ...]]:
    """실행가능 좌표 방향 n개. **폴 집합에 언제나 함께 들어간다.**

    설계 문서는 최소 양기저를 원뿔에 '스냅'하면 된다고 적었지만, 스냅은
    양생성을 보존하지 않는다 - 성분을 0으로 깎으면 원뿔의 경계 방향이 집합에서
    사라질 수 있고, 그러면 완결된 폴이 실패해도 "이 원뿔 안에 더 나은 메시
    이웃이 없다"고 말할 수 없다. 축소 결정이 근거를 잃는 것이고, 그것은 이
    저장소가 열 번 당한 "조용히 무력한 게이트"의 탐색기 판본이다.

    원뿔은 각 노브가 단측인 **상자**이므로 좌표 방향만으로 양생성이 구성상
    자명하다. 비용은 실패하는 폴의 방향 수가 n+1에서 최대 2n+1로 느는 것이고,
    사는 것은 "완결된 폴이 실패했다"는 문장의 의미다."""
    n = len(signs)
    return [
        tuple(signs[i] * rho if i == j else 0 for i in range(n)) for j in range(n)
    ]


def snap_to_cone(vector, signs: tuple[int, ...]) -> tuple[int, ...]:
    """선언된 방향과 부호가 어긋나는 성분을 0으로 깎는다.

    `Knob.direction`은 오늘도 단방향 구속이고(좌표 하강은 선언된 방향으로만
    간다), MADS도 **같은 상자**를 받는다. 양방향 폴을 허용하면 MADS가 이겨도
    그것이 적응 스텝 덕인지 더 넓은 실행가능 집합 덕인지 갈리지 않는다 -
    비교의 통제이지 알고리즘의 요구가 아니다."""
    return tuple(value if value * sign > 0 else 0 for value, sign in zip(vector, signs))


# ---------------------------------------------------------------------------
# 전략
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Move:
    """한 방향이 만들어 낸 후보. 값이 정해진 뒤의 모양이다."""

    steps: list[ProposedStep]
    floored: list[str]


def _candidate_value(
    state: KnobState, component: int, delta: float, rho: int
) -> float | None:
    """이 노브를 이 성분만큼 옮긴 값. 움직이지 않으면 None.

    기하 노브의 좌표는 **로그**다: 한 걸음이 곱셈 인자 exp(δ·d)가 되고,
    순수 좌표 방향(|d| = ρ)에서 정확히 exp(±Δ)가 된다.

    개수 노브(m/nf에 도달하는 것)는 값 자체가 좌표이고 granularity가 1이다.
    Δ0에서 ±1 - 좌표 하강과 같은 첫 걸음이다. 변위가 0으로 반올림되면 1로
    바닥을 둔다(granular MADS, Audet–Le Digabel–Tribes 2019). **그 바닥은
    추측한 허용오차가 아니라 격자의 구조적 사실이다**: 정수는 1보다 잘게
    나눌 수 없으므로, 바닥에 닿은 뒤 완결된 폴이 실패하면 그 좌표는 수렴한
    것이다.

    1 미만으로는 내려가지 않는다 - `_next_value`가 개수에 대해 이미 두고 있는
    경계와 같다. 기하 쪽에는 바닥이 **없다**: 최소 치수는 PDK 지식이고 이
    프로젝트는 그것을 추측하는 것을 금한다. 숫자를 지어내는 대신 시뮬레이션
    실패를 단계 거절로 받는다."""
    if component == 0:
        return None
    if not state.integer:
        return state.value * math.exp(delta * component / rho)
    raw = (delta / MADS_INITIAL_POLL_SIZE) * (component / rho)
    displacement = int(round(raw))
    if displacement == 0:
        displacement = 1 if raw > 0 else -1
    nxt = max(1.0, state.value + displacement)
    return None if nxt == state.value else nxt


def _floored(state: KnobState, component: int, delta: float, rho: int) -> bool:
    """이 걸음이 granularity 바닥(±1)에 눌렸는가. 이벤트에 남기려고 따로 잰다 -
    바닥에 닿은 정수 좌표는 더 이상 정제되지 않으므로, 그 사실이 로그에 없으면
    "축소했는데 아무 일도 없었다"의 원인을 읽을 수 없다."""
    if component == 0 or not state.integer:
        return False
    raw = (delta / MADS_INITIAL_POLL_SIZE) * (component / rho)
    return int(round(raw)) == 0


def _build_move(
    direction: tuple[int, ...],
    knobs: list[Knob],
    states: dict[Knob, KnobState],
    delta: float,
    rho: int,
) -> _Move | None:
    """방향 하나를 후보로 만든다. 어느 노브도 움직이지 않으면 None(무효 방향).

    무효 방향은 시뮬레이션도 예산도 쓰지 않는다 - 원뿔 스냅과 정수 바닥에서
    자연히 나오는 모양이라 이벤트에 수만 남긴다."""
    steps: list[ProposedStep] = []
    floored: list[str] = []
    for knob, component in zip(knobs, direction):
        state = states[knob]
        value = _candidate_value(state, component, delta, rho)
        if value is None:
            continue
        if _floored(state, component, delta, rho):
            floored.append(f"{knob.refdes}.{knob.param}")
        steps.append(ProposedStep(knob, state, value))
    return _Move(steps=steps, floored=floored) if steps else None


def _poll_set(
    knobs: list[Knob], iteration: int, rho: int, preferred: tuple[int, ...] | None
) -> tuple[list[tuple[int, ...]], int]:
    """이 폴에서 시도할 방향들과 무효 방향 수.

    순서가 규칙의 일부다: 지난 폴에서 성공한 방향 → 복합 방향 → 실행가능 좌표
    방향. 기회주의적 폴이므로 앞쪽에서 성공하면 뒤는 아예 재지 않는다. 복합을
    좌표보다 앞에 두는 것은 **결합을 먼저 본다**는 뜻이고, 좌표 방향을 뒤에
    두되 **반드시 넣는** 것은 축소 결정의 근거를 위해서다."""
    n = len(knobs)
    signs = tuple(-1 if knob.direction == "decrease" else 1 for knob in knobs)
    raw: list[tuple[int, ...]] = []
    if preferred is not None:
        raw.append(scale_to_infinity_norm(preferred, rho))
    raw.extend(composite_directions(n, iteration, rho))
    raw.extend(coordinate_directions(signs, rho))

    ordered: list[tuple[int, ...]] = []
    seen: set[tuple[int, ...]] = set()
    void = 0
    for vector in raw:
        snapped = snap_to_cone(vector, signs)
        if not any(snapped):
            void += 1
            continue
        if snapped in seen:
            # 같은 방향을 두 번 재면 시뮬레이션이 한 번 버려진다. 중복은
            # 스냅에서 흔히 생긴다(복합 방향이 좌표 방향으로 깎이는 경우).
            continue
        seen.add(snapped)
        ordered.append(snapped)
    return ordered, void


async def mads(run: SearchRun) -> None:
    """신뢰영역 직접탐색. 모듈 독스트링이 무엇이 MADS이고 무엇이 아닌지를 적는다.

    한 이터레이션 = 한 폴이다. 폴은 방향 집합을 순서대로 재다가 성공하면 즉시
    끝내고(기회주의적) 반경을 늘린다. 전부 실패하면 반경을 줄인다 - **단,
    집합을 끝까지 잰 경우에만.** 예산이 중간에 떨어져 완결하지 못한 폴에서
    축소하면 보지 않은 방향을 실패로 단정하는 것이 된다."""
    live = list(run.knobs)
    states: dict[Knob, KnobState] = {}
    delta = MADS_INITIAL_POLL_SIZE
    preferred: tuple[int, ...] | None = None
    preferred_for: list[Knob] = []

    iteration = 0
    tally = {"expand": 0, "contract": 0, "hold": 0}
    delta_seen = [delta]
    composite_evaluated = 0
    widest_poll = 0
    stopped = "budget"
    # 이미 거절된 **점**들. 같은 점을 다시 재는 것은 시뮬레이션을 버리는
    # 것이다 - SPICE는 결정론적이고 best_objective는 내려가기만 하므로, 한 번
    # 거절된 후보는 같은 자리에서 언제나 다시 거절된다. 정수 노브가
    # granularity 바닥(±1)에 닿은 뒤에는 반경을 줄여도 같은 점만 나오므로,
    # 이 억제가 없으면 예산이 같은 시뮬레이션으로 소진된다.
    rejected_points: set[tuple] = set()

    while True:
        iteration += 1

        # 게이트가 막은 노브는 **영구히** 버린다. 막은 것은 값이 아니라
        # 주소이므로 다른 값으로 다시 물어도 같은 자리에서 막히고, knob_state는
        # 물을 때마다 거절을 1건 세므로 이력이 같은 사실을 반복해서 적는다.
        surviving: list[Knob] = []
        for knob in live:
            state = run.knob_state(knob)
            if state is not None:
                states[knob] = state
                surviving.append(knob)
        live = surviving
        if not live:
            run.log_event(
                "mads_poll",
                _poll_event(iteration, live, delta, delta, "hold", 0, 0, True, False, 0, 0, 0, []),
            )
            stopped = "no_live_knobs"
            break

        rho = mesh_count(delta)
        if preferred_for != live:
            # 노브 구성이 바뀌면(막힌 노브가 빠지면) 지난 성공 방향의 성분이
            # 어느 노브의 것인지 말할 수 없다. 지어내지 않고 버린다.
            preferred = None
        directions, void = _poll_set(live, iteration, rho, preferred)
        widest_poll = max(widest_poll, len(live))

        evaluated = 0
        arity = 0
        repeated = 0
        floored: list[str] = []
        success = False
        complete = True
        for direction in directions:
            move = _build_move(direction, live, states, delta, rho)
            if move is None:
                void += 1
                continue
            signature = tuple(
                (step.knob.refdes, step.state.token, _format_value(step.value, step.state.integer))
                for step in move.steps
            )
            if signature in rejected_points:
                # 결과를 이미 아는 점이다. 재지 않지만 **폴은 완결된 것으로
                # 센다** - 그 방향의 결과가 실패라는 것을 알고 있으므로,
                # 축소 결정이 보지 않은 방향 위에 서지 않는다.
                repeated += 1
                continue
            if not run.spend_step(move.steps[0].knob):
                # 예산은 전역이다 - 다음 방향으로 넘어가도 달라지지 않는다.
                complete = False
                break
            evaluated += 1
            arity = max(arity, len(move.steps))
            if len(move.steps) > 1:
                composite_evaluated += 1
            for name in move.floored:
                if name not in floored:
                    floored.append(name)
            outcome = await run.attempt(move.steps)
            if outcome.accepted:
                success = True
                preferred = direction
                preferred_for = list(live)
                break
            rejected_points.add(signature)

        if not complete:
            mesh = "hold"
        elif success:
            mesh = "expand"
        elif evaluated == 0:
            # 잰 것이 하나도 없다 - 모든 방향이 무효(정수 노브가 전부 바닥에
            # 닿았거나 원뿔이 통째로 막혔다)이거나 이미 거절된 점을 되풀이한다.
            # 반경을 줄여도 같은 점만 나오므로 축소가 아니라 소진이다.
            mesh = "hold"
        else:
            mesh = "contract"

        before = delta
        if mesh == "expand":
            delta *= MADS_STEP_FACTOR
        elif mesh == "contract":
            delta /= MADS_STEP_FACTOR
        tally[mesh] += 1
        delta_seen.append(delta)

        run.log_event(
            "mads_poll",
            _poll_event(
                iteration, live, before, delta, mesh, len(directions), evaluated,
                complete, success, arity, void, repeated, floored,
            ),
        )

        if not complete:
            stopped = "budget"
            break
        if evaluated == 0:
            detail = (
                "every poll direction reproduces a candidate that was already rejected "
                "at this point"
                if repeated
                else "no poll direction moves it on the current mesh"
            )
            for knob in live:
                run.exhausted(
                    knob,
                    states[knob],
                    f"{knob.refdes}.{knob.param} in direction {knob.direction!r}: {detail}",
                )
            stopped = "all_knobs_exhausted"
            break

    run.log_event(
        "mads_summary",
        {
            "polls": iteration,
            "expands": tally["expand"],
            "contracts": tally["contract"],
            "holds": tally["hold"],
            "delta_initial": MADS_INITIAL_POLL_SIZE,
            "delta_final": delta,
            "delta_min": min(delta_seen),
            "delta_max": max(delta_seen),
            "step_factor": MADS_STEP_FACTOR,
            "composite_evaluated": composite_evaluated,
            "widest_poll_knobs": widest_poll,
            # 아래 둘이 판정 **자격** 조건이다. 사후에 history.jsonl에서
            # 재구성하지 않고 결정되는 자리에서 적는다 - 이 저장소가 거절
            # 사유 코드에 대해 이미 정한 규칙이다.
            "adaptive_step_exercised": tally["expand"] > 0 and tally["contract"] > 0,
            "coupling_observable": widest_poll >= 2,
            "stopped": stopped,
        },
    )


def _poll_event(
    iteration, knobs, before, after, mesh, poll_size, evaluated,
    complete, success, arity, void, repeated, floored,
) -> dict:
    return {
        "iteration": iteration,
        "knobs": [f"{knob.refdes}.{knob.param}" for knob in knobs],
        "n": len(knobs),
        "rho": mesh_count(before),
        "delta_before": before,
        "delta_after": after,
        # expand / contract / hold. hold는 "잴 수 없었다"이지 "실패했다"가
        # 아니다 - 완결되지 않은 폴에서 축소하면 보지 않은 방향을 실패로
        # 단정하게 된다.
        "mesh": mesh,
        "poll_size": poll_size,
        "evaluated": evaluated,
        "poll_complete": complete,
        "success": success,
        # 이 폴에서 한 후보가 동시에 옮긴 노브 수의 최대. 1이면 복합 이동이
        # 한 번도 일어나지 않은 것이고, 그 실행으로는 결합을 판정할 수 없다.
        "composite_arity": arity,
        "void_directions": void,
        # 이미 거절된 점을 되풀이해 재지 않은 방향 수. 시뮬레이션을 아낀
        # 것이지 보지 않은 것이 아니다 - 결과를 알고 건너뛴 것이라 폴은
        # 완결로 센다.
        "repeated_directions": repeated,
        "granularity_floored": floored,
    }


SEARCH_STRATEGIES["mads"] = mads
