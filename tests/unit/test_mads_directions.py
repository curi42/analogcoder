"""MADS 방향 생성 - 결정론적이고, 불변식이 단위 테스트로 박혀 있어야 한다.

이 파일이 지키는 것은 하나다: **폴 집합이 실행가능 원뿔을 양생성한다.**
그것이 없으면 "완결된 폴이 실패했으니 반경을 줄인다"가 근거 없는 추론이 된다 -
보지 않은 방향을 실패로 단정하는 것이다. 이 저장소가 열 번 당한 "조용히
무력한 게이트"의 탐색기 판본이 정확히 그 모양이라, 방향 생성은 시뮬레이터
없이 도는 테스트로 못박는다.

두 번째로 지키는 것은 **결정론**이다. LT-MADS는 난수 하삼각 기저를 쓰는데,
그러면 scripts/search_ab.py의 `--assert-identical` 자기검사가 깨지고 "LLM을
빼면 분산이 0"이라는 하니스의 존재 이유가 사라진다.
"""

import math
from fractions import Fraction

import pytest

from analogcoder.mads import (
    MADS_INITIAL_POLL_SIZE,
    composite_directions,
    coordinate_directions,
    halton,
    halton_point,
    householder_columns,
    mesh_count,
    minimal_positive_basis,
    scale_to_infinity_norm,
    snap_to_cone,
)


def _determinant(rows: list[list[int]]) -> Fraction:
    """정확한 유리수 가우스 소거. float로 재면 0인지 아닌지가 판정이 아니라
    반올림이 된다 - 특이 행렬을 통과시키면 폴 집합이 양생성을 잃는다."""
    matrix = [[Fraction(value) for value in row] for row in rows]
    size = len(matrix)
    det = Fraction(1)
    for col in range(size):
        pivot = next((r for r in range(col, size) if matrix[r][col] != 0), None)
        if pivot is None:
            return Fraction(0)
        if pivot != col:
            matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
            det = -det
        det *= matrix[col][col]
        inverse = Fraction(1) / matrix[col][col]
        for r in range(col + 1, size):
            factor = matrix[r][col] * inverse
            if factor:
                matrix[r] = [a - factor * b for a, b in zip(matrix[r], matrix[col])]
    return det


# --- 상수의 출처 -----------------------------------------------------------


def test_the_initial_poll_size_is_read_from_the_incumbent_not_guessed():
    """Δ0 = |ln 0.9|. 이 값만은 문헌이 아니라 **현행 좌표 하강에서 읽어 온
    것**이고, 그래야 MADS의 첫 폴 점이 좌표 하강의 첫 점과 정확히 같아진다.
    같지 않으면 A/B의 두 팔이 다른 출발점에서 갈라져, 이긴 쪽이 알고리즘
    덕인지 출발점 덕인지 구별되지 않는다."""
    from analogcoder.optimizer import STEP_RATIO, _next_value

    assert MADS_INITIAL_POLL_SIZE == abs(math.log(STEP_RATIO))
    # 순수 좌표 방향 한 걸음 = exp(-Δ0)배 = 정확히 ×0.9.
    assert 8.0 * math.exp(-MADS_INITIAL_POLL_SIZE) == _next_value(8.0, False, "decrease") == 7.2


def test_the_mesh_is_finer_than_the_poll_radius_and_the_ratio_goes_to_zero():
    """MADS를 GPS와 가르는 성질이다: 메시 δ는 폴 반경 Δ보다 작고, Δ가 줄면
    δ/Δ가 **함께** 0으로 간다. 여기서는 δ = Δ/ρ, ρ = max(1, round(1/Δ)) -
    LT-MADS의 δ = min(Δ, Δ²)를 정수 메시 칸수로 반올림한 것이고, 그렇게
    반올림해야 폴 반경이 **정확히** Δ가 되어 위 테스트의 앵커가 유지된다."""
    assert mesh_count(MADS_INITIAL_POLL_SIZE) == 9
    ratios = []
    delta = MADS_INITIAL_POLL_SIZE
    for _ in range(4):
        rho = mesh_count(delta)
        ratios.append(1 / rho)
        delta /= 2.0
    assert ratios == sorted(ratios, reverse=True)  # δ/Δ가 단조 감소한다
    assert ratios[-1] < ratios[0] / 4
    # Δ가 1보다 크면 더 나눌 이유가 없다 - min(Δ, Δ²)가 Δ가 되는 구간이다.
    assert mesh_count(2.0) == 1
    assert mesh_count(1e9) == 1


# --- 결정론 ---------------------------------------------------------------


def test_direction_generation_has_no_randomness_at_all():
    """같은 인덱스는 언제나 같은 방향을 낸다. 하니스의 자기검사가 여기에
    걸려 있다."""
    for index in (1, 2, 7, 31):
        assert composite_directions(3, index, 9) == composite_directions(3, index, 9)
    assert halton(11, 2) == halton(11, 2)


def test_halton_is_the_textbook_radical_inverse():
    """손으로 검산할 수 있는 값이어야 한다 - 여기가 틀리면 방향이 조용히
    한쪽으로 쏠린다."""
    assert halton(1, 2) == 0.5
    assert halton(2, 2) == 0.25
    assert halton(3, 2) == 0.75
    assert halton(1, 3) == pytest.approx(1 / 3)
    assert halton(4, 3) == pytest.approx(4 / 9)
    assert halton(0, 2) == 0.0
    point = halton_point(3, 1)
    assert point == pytest.approx([0.5, 1 / 3, 0.2])  # 밑은 앞의 세 소수


def test_successive_polls_do_not_reuse_the_same_directions():
    """방향 집합이 이터레이션마다 **바뀌는 것**이 MADS가 GPS와 다른 두 번째
    지점이다. 고정 유한 집합이면 좌표 하강의 결합 눈멂이 형태만 바꿔 남는다."""
    seen = [composite_directions(3, index, 9) for index in range(1, 6)]
    assert len({tuple(dirs) for dirs in seen}) == len(seen)


# --- 하우스홀더 기저의 불변식 ----------------------------------------------


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_the_householder_columns_are_exactly_orthogonal_integers(n):
    """H = ‖q‖²I − 2qqᵀ 는 **어떤 정수 q에 대해서도** 정수 행렬이고 열이 서로
    정확히 직교한다. 부동소수 정규직교화가 필요 없다는 뜻이고, 그래서 이
    부분에는 반올림 오차로 조용히 틀릴 자리가 없다."""
    q = [i + 1 for i in range(n)]
    columns = householder_columns(q)
    assert len(columns) == n
    for column in columns:
        assert all(isinstance(value, int) for value in column)
        assert sum(value * value for value in column) == sum(x * x for x in q) ** 2
    for i in range(n):
        for j in range(i + 1, n):
            assert sum(a * b for a, b in zip(columns[i], columns[j])) == 0


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_the_minimal_positive_basis_has_n_plus_one_vectors_that_sum_to_zero(n):
    """최소 양기저 {h₁..hₙ, −Σhᵢ}. 합이 0이라는 것이 양생성의 구성적 증거다 -
    LP를 풀지 않고도 확인할 수 있는 형태라서 이렇게 만든다."""
    columns = householder_columns([i + 1 for i in range(n)])
    basis = minimal_positive_basis(columns)
    assert len(basis) == n + 1
    assert [sum(vector[i] for vector in basis) for i in range(n)] == [0] * n
    # 그리고 앞의 n개가 선형독립이어야 한다(그래야 합이 0인 것이 R^n의
    # 양생성을 뜻한다). 정수 산술로 정확히 잰다.
    assert _determinant([list(row) for row in zip(*basis[:n])]) != 0


def test_scaling_puts_the_longest_component_exactly_on_the_mesh_radius():
    """폴 점은 x + δ·d 이고 δ = Δ/ρ 이므로, ‖d‖_∞ = ρ 여야 반경이 정확히
    Δ가 된다. 순수 좌표 방향에서 ×0.9가 나오는 근거가 이 한 줄이다."""
    assert scale_to_infinity_norm((3, -1, 0), 9) == (9, -3, 0)
    assert max(abs(v) for v in scale_to_infinity_norm((7, -13, 2), 5)) == 5
    assert scale_to_infinity_norm((0, 0), 9) == (0, 0)


# --- 단측 원뿔 스냅과 좌표 방향 --------------------------------------------


def test_a_component_that_disagrees_with_the_declared_direction_is_snapped_to_zero():
    """`Knob.direction`은 오늘도 단방향 구속이고, MADS도 같은 상자를 받는다.
    양방향 폴을 허용하면 MADS가 이겨도 그것이 적응 스텝 덕인지 **더 넓은
    실행가능 집합** 덕인지 갈리지 않는다."""
    # signs: -1 = decrease만 허용, +1 = increase만 허용
    assert snap_to_cone((5, -3), (-1, -1)) == (0, -3)
    assert snap_to_cone((5, -3), (+1, +1)) == (5, 0)
    assert snap_to_cone((5, -3), (-1, +1)) == (0, 0)  # 통째로 무효인 방향


def test_the_coordinate_directions_alone_positively_span_the_feasible_cone():
    """**설계 문서가 놓쳤던 지점이고, 이 파일의 존재 이유다.**

    설계는 최소 양기저 n+1개를 원뿔에 '스냅'하면 된다고 적었지만, 스냅은
    양생성을 보존하지 않는다 - 성분을 0으로 깎으면 원뿔의 경계 방향이 집합에서
    사라질 수 있고, 그러면 완결된 폴이 실패해도 "이 원뿔 안에 더 나은 메시
    이웃이 없다"고 말할 수 없다. 그래서 폴 집합에 **실행가능 좌표 방향
    n개**를 언제나 함께 넣는다. 원뿔은 상자(각 노브가 단측)라 좌표 방향만으로
    양생성이 구성상 자명하다.

    비용은 실패하는 폴에서 방향이 n+1개가 아니라 최대 2n+1개가 되는 것이고,
    사는 것은 축소 결정의 근거다."""
    signs = (-1, +1, -1)
    coords = coordinate_directions(signs, 9)
    assert coords == [(-9, 0, 0), (0, 9, 0), (0, 0, -9)]
    # 원뿔의 임의의 방향이 이들의 음이 아닌 결합으로 나온다 - 상자 원뿔이므로
    # 성분별로 자명하다. 반대로 원뿔 밖은 나오지 않는다.
    for vector in coords:
        assert all(v * s >= 0 for v, s in zip(vector, signs))


def test_a_one_knob_poll_set_is_exactly_the_declared_direction():
    """n=1이면 폴 집합이 방향 하나다. 그래서 노브 하나짜리 A/B로는 결합을
    판정할 수 없다 - 부정이 아니라 **무효**다(설계 문서 G1)."""
    signs = (-1,)
    composites = [snap_to_cone(d, signs) for d in composite_directions(1, 1, 9)]
    usable = {d for d in composites if any(d)}
    assert usable == {(-9,)}
    assert coordinate_directions(signs, 9) == [(-9,)]


def test_a_composite_direction_really_moves_more_than_one_knob():
    """결합을 보는 방식은 이것 하나다: 폴 점 하나가 여러 노브를 **동시에**
    옮긴다. run.attempt가 ProposedStep의 목록을 받으므로 이음매 변경이 없다."""
    signs = (-1, -1, -1)
    moved = [
        sum(1 for v in snap_to_cone(d, signs) if v)
        for d in composite_directions(3, 1, 9)
    ]
    assert max(moved) >= 2
