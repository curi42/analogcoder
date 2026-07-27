import json
import os


def write_result_json(run_dir: str, result: dict) -> str:
    path = os.path.join(run_dir, "result.json")
    with open(path, "w") as f:
        json.dump(result, f, indent=2)
    return path


def _optimization_lines(optimization: dict | None) -> list[str]:
    """최적화 단계를 설명하는 섹션. 그 단계가 돌지 않았으면 빈 목록.

    돌지 않은 실행에 빈 섹션을 그리면 "돌았는데 아무것도 못 했다"로 읽힌다 -
    그 둘은 다른 사실이다.

    "Final criteria"만 있던 리포트는 최적화가 넷리스트를 바꿔도 그 사실을 한
    줄도 말하지 않았다. 이 단계에는 FAIL 결말이 없으므로 실행은 여전히 PASS로
    끝나고, 그래서 리포트가 말하지 않으면 최적화가 통째로 죽은 것을 아무도
    모른다 - failure를 함께 적는 이유다."""
    if not optimization:
        return []

    status = optimization.get("status")
    lines = ["", "## Optimization", "", f"**Status:** {status}"]

    if status == "SKIPPED":
        lines.append("The spec declares no `optimize:` block.")
        return lines

    before = optimization.get("objective_before")
    after = optimization.get("objective_after")
    lines += [
        f"**Objective:** {before} -> {after}",
        f"**Area:** {optimization.get('area_before')} -> {optimization.get('area_after')}",
        f"**Steps:** {optimization.get('steps_accepted')} accepted, "
        f"{optimization.get('steps_rejected')} rejected",
        f"**Corner confirmed:** {optimization.get('corner_confirmed')}",
    ]

    if optimization.get("corner_failure"):
        lines.append(f"**Corner sweep could not run:** {optimization['corner_failure']}")
    if optimization.get("failure"):
        # 최적화가 터져서 접힌 경우. 실행은 PASS인데 이 단계는 아무것도 하지
        # 않았다 - 리포트가 유일한 안내판이다.
        lines.append(f"**Optimization could not run:** {optimization['failure']}")
    if optimization.get("guard_infeasible"):
        lines.append(
            "**Guard band infeasible at the baseline** (no step could ever be accepted): "
            + "; ".join(optimization["guard_infeasible"])
        )
    coverage = optimization.get("area_coverage") or {}
    if coverage.get("reason"):
        lines.append(f"**Area budget:** {coverage['reason']}")

    return lines


def write_report_md(run_dir: str, result: dict) -> str:
    lines = [
        "# Run Report",
        "",
        f"**Status:** {result['status']}",
        f"**Iterations used:** {result['iterations_used']}",
        "**Final netlists:**",
    ]
    for name, path in result["final_netlist_paths"].items():
        lines.append(f"- {name}: `{path}`")
    lines += [
        "",
        "## Final criteria",
        "",
    ]
    for c in result["final_criteria"]:
        mark = "PASS" if c["pass"] else "FAIL"
        lines.append(f"- [{mark}] {c['name']}: target {c['target']}, actual {c['actual']} (margin {c['margin']})")
    lines += _optimization_lines(result.get("optimization"))
    if result.get("failure_reason"):
        lines.append("")
        lines.append(f"**Failure reason:** {result['failure_reason']}")
    text = "\n".join(lines) + "\n"
    path = os.path.join(run_dir, "report.md")
    with open(path, "w") as f:
        f.write(text)
    return path
