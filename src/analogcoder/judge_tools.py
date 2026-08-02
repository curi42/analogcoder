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


def relative_slack(criterion: Criterion, actual: float | None) -> float | None:
    """임계값 대비 남은 상대 여유. 부호는 통과 방향이 양수다.

    `scripts/area_guard_measurement.py`가 처음 정의했던 것을 그대로 옮긴
    것 - 스크립트와 `optimizer.py`(Task 3의 `tightest_slack`) 둘 다 이
    함수 하나를 쓴다. 두 곳에 같은 식을 두면 갈라진다는 것이
    `compose.py`가 `netlist.py`의 include 규칙을 손으로 베껴 겪은 일이다.

    스케일은 `max(|threshold|, |actual|)`다 - 임계값이 0인 기준에서
    0으로 나누지 않기 위해서다. `scale == 0.0`은 폴백이 **아니다**: 그
    값은 임계값과 실측이 **둘 다** 0일 때만 나오고, 그때 여유는 정확히
    0이다(기준이 자기 임계값 위에 정확히 서 있다) - 나눗셈을 피하려는
    임의의 대체값이 아니라 실제로 옳은 답이다.

    `actual`이 NaN이면 이 함수는 그 NaN을 그대로(또는 `scale` 계산의
    인자 순서에 따라 뒤섞인 값으로) 돌려준다 - 이 함수 자체는 NaN을
    막지 않는다. 최솟값 경쟁(`optimizer._tightest_slack`)에 넣기 전에
    NaN을 걸러내는 것은 그 호출부의 책임이다: `max()`가 NaN 비교에서
    인자 순서에 따라 다른 값을 돌려주므로, 이 함수 안에서 막으면 그
    사실이 안 보이게 된다."""
    if actual is None:
        return None
    scale = max(abs(criterion.threshold), abs(actual))
    if scale == 0.0:
        return 0.0
    if criterion.operator in _LOWER_BOUND:
        return (actual - criterion.threshold) / scale
    return (criterion.threshold - actual) / scale


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


def baseline_ratio_allowances(
    baseline_measurements: dict, criteria: list[Criterion], r: float
) -> tuple[dict[str, float], list[str]]:
    """기준선 여유의 `r` 배를 남기라는 여유분, 그리고 **적용할 수 없는 기준들**.

    `ratio_allowances`가 `g·|T|`(임계값에 비례)인 것과 달리 이쪽은
    `r·|기준선 - T|`(회로가 실제로 갖고 있던 여유에 비례)다. 고를 상수가
    비율 하나뿐이고 기준의 단위·부호·크기와 무관한 것이 이 규칙을 후보에
    넣은 이유다.

    셋을 제외하고 그 이름을 **돌려준다**(조용히 빼지 않는다): 측정값이
    없는 기준(거리를 잴 수 없다), 이미 실패 중인 기준(음수에 r을 곱하면
    하한이 위로 올라가 규칙이 뒤집힌다), 임계값에 정확히 붙은 기준(여유 0에
    무엇을 곱해도 0이라 규칙이 침묵한다). 앞 둘은 `overall_pass`가 이미
    판정하므로 이 규칙이 할 일이 없다. `guard_band_violations`는 이름이
    없는 기준의 여유분을 0.0으로 읽으므로, 제외된 이름을 별도로 돌려주지
    않으면 "적용 안 함"과 "여유 0"이 구분되지 않는다."""
    allowances: dict[str, float] = {}
    excluded: list[str] = []

    for c in criteria:
        actual = baseline_measurements.get(c.measurement)
        if actual is None:
            excluded.append(c.name)
            continue

        slack = (actual - c.threshold) if c.operator in _LOWER_BOUND else (c.threshold - actual)
        if slack <= 0.0 or slack != slack:  # NaN 도 여기서 걸린다 - 측정 실패를 여유로 읽지 않는다
            excluded.append(c.name)
            continue

        allowances[c.name] = r * slack

    return allowances, excluded
