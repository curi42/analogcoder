"""실험 설계·판정을 위한 순수 통계 함수. 외부 의존성 없음(표준 라이브러리만).

이 모듈이 존재하는 이유는 D1이 무효로 측정됐기 때문이다 - 표본 수(n=3)를
느낌으로 정했고, 판정 규칙을 수치를 본 뒤에 골랐고, 기준선 실행이 지표를
구조적으로 0.000 외에는 낼 수 없는 상태였다. 세 결함 모두 코드가 아니라
실험 설계의 결함이었고, 이 모듈은 그 세 자리를 각각 막는다:

- 표본 수를 사전에 계산한다(``required_discordant_pairs``/``required_pairs``) -
  느낌으로 정하지 않는다.
- 순차검정(``SPRT``)으로 결론이 조기에 확정되면 멈춘다 - LLM 호출과
  시뮬레이션을 아낀다.
- 지표가 발화할 조건이 있었는지 실행 전에 확인한다(``informative``) - 없으면
  평균에 넣지 않는다.

`docs/superpowers/specs/2026-07-29-theory-adoption-roadmap.md`의 단계 0.
"""

import math
from statistics import NormalDist


def mcnemar_exact(b: int, c: int) -> float:
    """짝지은 이항 결과(전-후, A안-B안)의 양측 정확 McNemar 검정.

    ``b``/``c``는 불일치 쌍의 수 - 한쪽만 맞고 다른 쪽만 틀린 두 경우의 카운트다
    (일치 쌍은 검정에 정보를 주지 않으므로 아예 들어오지 않는다). H0 아래서
    불일치 쌍은 Binomial(b+c, 0.5)를 따르므로, 두 관측된 방향 중 작은 쪽의
    누적확률을 두 배 해 양측 p값을 얻는다.

    **정규/카이제곱 근사가 아니라 ``math.comb``로 정확한 이항 꼬리를 계산한다
    - 여기서 다루는 표본은 작고(D1처럼 n이 한 자리), 근사가 정확히 틀리는
    영역이 바로 거기다.** 연속성 보정 카이제곱(McNemar의 표준 형태)조차
    쓰지 않는다 - `test_mcnemar_exact_differs_from_the_chi_square_approximation_on_small_n`가
    두 값이 실제로 다름을 고정한다.

    ``n = b + c == 0``(불일치 쌍이 하나도 없음)은 "증거 없음"이지 나눗셈
    오류가 아니다 - 1.0을 반환한다.
    """
    if b < 0 or c < 0:
        raise ValueError(f"b, c must be non-negative discordant counts: b={b}, c={c}")
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    # P(X <= k), X ~ Binomial(n, 0.5). 대칭 이항이므로 분모는 2**n 하나로 충분.
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    p = 2 * tail
    # b==c일 때 k==n/2이고 P(X<=n/2)가 0.5를 넘으므로 2배 하면 1.0을 넘는다 -
    # p값은 확률이므로 여기서 클램프한다. 이것이 유일하게 clamp가 발동하는
    # 경우다(그 밖에는 2*tail이 항상 1.0 이하).
    return min(p, 1.0)


def required_discordant_pairs(p1: float, alpha: float = 0.05, power: float = 0.8) -> int:
    """McNemar 검정으로 ``p1``의 효과크기를 잡는 데 필요한 **불일치 쌍**의 수.

    ``p1``은 불일치 쌍이 한쪽 팔을 편드는 참 비율(H0은 p=0.5). 표준
    two-proportion McNemar 검정력 공식:

        n_d = (z_{alpha/2}·0.5 + z_power·sqrt(p1·(1-p1)))^2 / (p1-0.5)^2

    분모가 효과크기(p1-0.5)의 제곱이므로, ``p1``이 0.5에 가까울수록(효과가
    작을수록) 더 많은 쌍이 필요하다 - 이 단조성이 태스크 목적 자체다.

    ``p1 == 0.5``는 효과크기 0을 요청한 것이고, 그 경우 n_d는 발산한다.
    거대하지만 유한한 정수를 반환하면 "이 실험은 실행 불가능하다"는 사실이
    "그냥 표본이 많이 필요하다"로 조용히 둔갑하므로, 여기서 명시적으로
    거부한다.
    """
    if p1 == 0.5:
        raise ValueError(
            "p1 == 0.5 is a zero effect size against H0 (p=0.5); "
            "required_discordant_pairs would need infinitely many pairs, "
            "so this is rejected rather than returning a huge finite int."
        )
    z_alpha2 = NormalDist().inv_cdf(1 - alpha / 2)
    z_power = NormalDist().inv_cdf(power)
    n_d = (z_alpha2 * 0.5 + z_power * math.sqrt(p1 * (1 - p1))) ** 2 / (p1 - 0.5) ** 2
    return math.ceil(n_d)


def required_pairs(
    p1: float, discordance_rate: float, alpha: float = 0.05, power: float = 0.8
) -> int:
    """검정력을 낼 만큼의 불일치 쌍을 실제로 관측하는 데 필요한 **전체** 쌍 수.

    ``required_discordant_pairs``는 "불일치 쌍이 몇 개 필요한가"만 답한다.
    실행 대 실행 비교에서 두 팔이 실제로 갈리는 쌍은 전체의 일부
    (``discordance_rate``)뿐이므로, 전체 쌍 수는 그 비율로 나눠야 한다.

    **이것이 D1의 n=3 실패를 막았을 함수다.** D1의 첫 비교는 표본 수를
    느낌으로 정했고, 기준선 실행은 불일치를 낼 조건(선행 실패 이벤트) 자체가
    0건이었다 - 그 실행을 몇 번을 더 돌려도 ``discordance_rate``가 0이면
    이 함수는 무한을 요구해 그 사실을 실행 전에 드러냈을 것이다.

    ``discordance_rate``는 (0, 1] 구간이어야 한다. 0 이하는 "결코 갈리지
    않는다"는 뜻이라 전체 쌍 수가 무한이 되고, 1을 넘는 값은 확률이 아니라
    입력 오류다.
    """
    if not (0 < discordance_rate <= 1):
        raise ValueError(
            f"discordance_rate must be in (0, 1], got {discordance_rate!r}"
        )
    n_d = required_discordant_pairs(p1, alpha=alpha, power=power)
    return math.ceil(n_d / discordance_rate)


class SPRT:
    """Wald의 순차확률비검정(Sequential Probability Ratio Test), 베르누이 모수용.

    H0: p = p0  대  H1: p = p1 를 관측을 하나씩 넣으며 검정한다. 매
    관측(``success``/``failure``)마다 로그가능도비에 다음을 더한다:

        성공: log(p1/p0)
        실패: log((1-p1)/(1-p0))

    누적값이 상한 A = log((1-beta)/alpha)를 넘으면 H1을 채택하고, 하한
    B = log(beta/(1-alpha))를 밑돌면 H0을 채택한다. 그 사이는 계속 관측한다
    ("continue"). 이것이 SPRT의 핵심 이득이다 - 결론이 일찍 갈리면 그 자리에서
    멈춰 LLM 호출과 시뮬레이션을 아낀다(고정 표본 크기 검정은 항상 끝까지
    돈다).

    **``max_observations``가 없으면 이 검정은 원리상 끝나지 않을 수 있다** -
    p가 정확히 (p0+p1)/2 근처라면 로그가능도비가 경계를 향해 표류하지 않고
    맴돌 수 있다. 측정 하니스에 무한 루프는 용납되지 않으므로, 상한에 도달하면
    ``"inconclusive"``를 낸다 - 이것도 정당한 검정 결과다("이 예산으로는
    갈리지 않았다"), 억지로 H0/H1 중 하나를 고르는 것보다 정직하다.
    """

    VERDICTS = ("continue", "H1", "H0", "inconclusive")

    def __init__(
        self,
        p0: float,
        p1: float,
        alpha: float = 0.05,
        beta: float = 0.2,
        max_observations: int | None = None,
    ) -> None:
        if not (0 < p0 < 1) or not (0 < p1 < 1):
            raise ValueError(f"p0, p1 must be in (0, 1): p0={p0}, p1={p1}")
        if p0 == p1:
            raise ValueError(
                f"p0 == p1 == {p0}: H0 and H1 are indistinguishable, "
                "no evidence can ever separate them"
            )
        self.p0 = p0
        self.p1 = p1
        self.alpha = alpha
        self.beta = beta
        self.max_observations = max_observations

        self._success_increment = math.log(p1 / p0)
        self._failure_increment = math.log((1 - p1) / (1 - p0))
        self._upper = math.log((1 - beta) / alpha)  # A: 여기 넘으면 H1
        self._lower = math.log(beta / (1 - alpha))  # B: 여기 밑돌면 H0

        self.llr = 0.0
        self.n_observations = 0
        self._verdict = "continue"

    @property
    def verdict(self) -> str:
        return self._verdict

    def update(self, success: bool) -> str:
        """관측 하나를 반영하고 현재 판정을 반환한다.

        이미 결정된 뒤(``"H1"``/``"H0"``/``"inconclusive"``)에 더 넣으면
        누적값을 더 움직이지 않고 그 판정을 그대로 돌려준다 - 결정된 검정은
        멈춘 검정이라는 SPRT의 전제를 코드로도 지킨다.
        """
        if self._verdict != "continue":
            return self._verdict

        self.llr += self._success_increment if success else self._failure_increment
        self.n_observations += 1

        if self.llr >= self._upper:
            self._verdict = "H1"
        elif self.llr <= self._lower:
            self._verdict = "H0"
        elif (
            self.max_observations is not None
            and self.n_observations >= self.max_observations
        ):
            self._verdict = "inconclusive"

        return self._verdict


def informative(condition_count: int, *, what: str) -> tuple[bool, str]:
    """지표가 이 실행에서 다른 답을 낼 수 있었는지 확인한다. D1 가드의 일반형.

    어떤 지표가 선행 조건(예: 게이트 거부, 롤백, 실패 이벤트)이 최소 한 번
    일어나야만 0이 아닌 값을 낼 수 있는 구조라면, 그 조건이 이 실행에서 0건인
    경우 그 지표는 **발화 불가능**이다 - 값이 0.000으로 나왔다고 "효과 없음"을
    뜻하지 않는다. 산술적으로 그 값 말고는 낼 수가 없었을 뿐이다. 그런 실행을
    평균에 그냥 넣으면 조용히 평균을 오염시킨다.

    **측정으로 실제 발생한 사례:** ``runs/measure/before/two_stage-1``은
    제안 이벤트 4건을 냈지만 게이트 거부 0건, verify_pre 거부 0건, 롤백
    0건이었다 - 반복 제안률(직전 제안과 같은 (refdes, param) 재제안 비율)은
    이 조건들이 최소 1건 있어야만 0이 아닌 값을 낼 수 있는 지표인데, 세
    선행 조건이 전부 0이었으므로 그 실행에서는 0.000 외의 값을 낼 수
    **없었다**. 그런데도 이 실행이 D1 전-후 비교의 "before" 표본으로 그대로
    쓰였고, 그것이 첫 D1 측정이 무효였던 세 결함 중 하나다.

    ``condition_count``는 호출부가 이미 센 선행 조건의 발생 횟수 - 이 함수는
    그 자체를 세지 않는다(무엇을 셀지는 지표마다 다르고, 이 함수가 알 수
    없는 지식이다).
    """
    if condition_count == 0:
        reason = (
            f"이 실행에는 {what}이(가) 0건이므로 이 지표는 0.000 외의 값을 "
            "낼 수 없었다"
        )
        return False, reason
    return True, ""
