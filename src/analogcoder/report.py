import json
import os


def write_result_json(run_dir: str, result: dict) -> str:
    path = os.path.join(run_dir, "result.json")
    with open(path, "w") as f:
        json.dump(result, f, indent=2)
    return path


def write_report_md(run_dir: str, result: dict) -> str:
    lines = [
        "# Run Report",
        "",
        f"**Status:** {result['status']}",
        f"**Iterations used:** {result['iterations_used']}",
        f"**Final netlist:** `{result['final_netlist_path']}`",
        "",
        "## Final criteria",
        "",
    ]
    for c in result["final_criteria"]:
        mark = "PASS" if c["pass"] else "FAIL"
        lines.append(f"- [{mark}] {c['name']}: target {c['target']}, actual {c['actual']} (margin {c['margin']})")
    if result.get("failure_reason"):
        lines.append("")
        lines.append(f"**Failure reason:** {result['failure_reason']}")
    text = "\n".join(lines) + "\n"
    path = os.path.join(run_dir, "report.md")
    with open(path, "w") as f:
        f.write(text)
    return path
