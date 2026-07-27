import math

from analogcoder.spec import Criterion

_OPERATORS = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    "==": lambda a, b: a == b,
}


def evaluate_criteria(measurements: dict, criteria: list[Criterion]) -> dict:
    results = []
    overall_pass = True

    for c in criteria:
        actual = measurements.get(c.measurement)
        if actual is None:
            results.append({
                "name": c.name,
                "target": f"{c.operator}{c.threshold}",
                "actual": math.nan,
                "pass": False,
                "margin": math.nan,
            })
            overall_pass = False
            continue

        passed = _OPERATORS[c.operator](actual, c.threshold)
        margin = actual - c.threshold
        results.append({
            "name": c.name,
            "target": f"{c.operator}{c.threshold}",
            "actual": actual,
            "pass": passed,
            "margin": margin,
        })
        overall_pass = overall_pass and passed

    summary = "all criteria passed" if overall_pass else "one or more criteria failed"
    return {"overall_pass": overall_pass, "criteria": results, "summary": summary}


_LOWER_BOUND = (">=", ">")
_UPPER_BOUND = ("<=", "<")


def guard_band_violations(
    measurements: dict, criteria: list[Criterion], allowances: dict[str, float]
) -> list[str]:
    """여유분을 지키지 못한 기준의 설명 목록. 빈 목록이면 전부 지킨 것.

    최적화는 마진을 의도적으로 소비하므로 "통과했는가"만으로는 부족하다.
    임계값에 바짝 붙은 채로 멈추면 코너와 모델 변동에서 무너진다.

    allowances는 기준 이름 → 남겨야 할 **절대량**이다. 비율을 임계값에 곱하는
    형태였다면 음수 임계값에서 뒤집혔을 것이다 - `psr <= -10`에 `T·(1-0.2)`는
    `<= -8`이라 원래보다 느슨하다. 절대량을 빼고 더하는 형태는 부호와 무관하게
    항상 엄격해지는 방향이다.

    여유분이 없는 기준은 통과만 하면 된다. 각 criterion을 자기 임계값에 대해
    따로 판정한다 - 같은 measurement에 `>=`와 `<=`가 걸린 양쪽 창을 하나로
    뭉개면 한쪽이 사라지는데, pvt.py에서 그 모양의 결함이 두 번 있었다."""
    violations: list[str] = []

    for c in criteria:
        actual = measurements.get(c.measurement)
        if actual is None:
            violations.append(f"{c.name}: measurement {c.measurement!r} is missing")
            continue

        allowance = allowances.get(c.name, 0.0)
        if c.operator in _UPPER_BOUND:
            limit = c.threshold - allowance
            if actual > limit:
                violations.append(
                    f"{c.name}: {actual:g} exceeds the guarded limit {limit:g} "
                    f"(threshold {c.threshold:g}, allowance {allowance:g})"
                )
        elif c.operator in _LOWER_BOUND:
            limit = c.threshold + allowance
            if actual < limit:
                violations.append(
                    f"{c.name}: {actual:g} is below the guarded limit {limit:g} "
                    f"(threshold {c.threshold:g}, allowance {allowance:g})"
                )
        # "==" 에는 의미 있는 여유분이 없다 - 통과 여부는 evaluate_criteria가 본다.

    return violations


def corner_allowances(
    reference: dict, sweep: dict, criteria: list[Criterion]
) -> dict[str, float]:
    """기준별로 코너가 **기준점**에서 밀어내는 실측 거리.

    기준점은 탐색이 실제로 보는 측정값이다. 탐색이 nominal 한 점을 보면
    nominal이고, 축소 코너 집합의 최악값을 보면 그 최악값이다. 둘을 섞으면
    같은 간격을 두 번 세어 가드가 과도하게 조여진다 - 축소 집합은 이미
    최악에 가깝기 때문이다. `reference`가 무엇인지는 이 함수가 정하지
    않는다: 호출부가 탐색이 실제로 측정하는 값을 넘기는 한 이 함수는 그
    거리를 그대로 잰다.

    균일한 비율을 추측하는 대신, 이미 값을 치른 코너 스윕에서 읽는다. 코너에
    둔감한 기준은 여유를 더 쓸 수 있고 민감한 기준은 자동으로 보수적이 된다 -
    숫자 하나로는 못 하는 구분이다.

    스윕에 값이 없는 기준은 **넣지 않는다.** 0을 넣으면 "코너가 이 기준을
    전혀 안 움직인다"는 거짓 사실이 되고, 그건 이 저장소가 반복해서 당한
    조용한 무력화와 같은 모양이다."""
    by_name = {c.name: c for c in criteria}
    allowances: dict[str, float] = {}

    for entry in sweep.get("criteria", []):
        criterion = by_name.get(entry.get("name"))
        worst = entry.get("actual")
        if criterion is None or worst is None:
            continue
        reference_value = reference.get(criterion.measurement)
        if reference_value is None or math.isnan(worst) or math.isnan(reference_value):
            continue
        allowances[criterion.name] = abs(worst - reference_value)

    return allowances


def ratio_allowances(criteria: list[Criterion], guard_band: float) -> dict[str, float]:
    """코너를 잴 수 없는 스펙용 대체 여유분, `g·|T|`.

    `|T|`를 쓰므로 임계값의 부호와 무관하게 양수 절대량이 나오고, 그래서
    guard_band_violations 쪽이 부호 문제를 아예 만나지 않는다."""
    return {c.name: guard_band * abs(c.threshold) for c in criteria}
