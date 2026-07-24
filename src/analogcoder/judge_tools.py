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
