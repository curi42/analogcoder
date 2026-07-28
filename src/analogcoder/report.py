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


def _corner_reduction_lines(reduction: dict | None) -> list[str]:
    """코너 축소 단계를 설명하는 섹션. 키 자체가 없으면 빈 목록.

    이 섹션이 있어야 하는 이유는 **`area_baselines`** 한 줄이다. 재진입할
    때마다 orchestrator가 면적 게이트의 기준선을 자기가 받은 덱에서 다시
    잡으므로, 한 소자가 원래 덱에 대해 허용받는 성장은 `tier^area_baselines`가
    된다 - 기본값(재시도 2회, 1.5x 티어)에서 3.375배다. PASS로 끝난 실행에서
    그 사실은 result.json을 열지 않으면 **어디에도 보이지 않았다**. 이
    저장소에서 면적 게이트가 조용히 안 걸린 것이 네 번이고 네 번 다 실행
    로그에 안 보였다는 것이 이 줄의 존재 이유다.

    실패 사유(path_disagreement/reentry_skipped)는 **있을 때만** 적는다.
    없는 것을 "없음"이라고 적으면 흔한 경우가 소음이 된다."""
    if not reduction:
        return []

    lines = [
        "",
        "## Corner reduction",
        "",
        f"**Active:** {reduction.get('active')}",
    ]
    if reduction.get("reason"):
        lines.append(f"**Inactive because:** {reduction['reason']}")

    final_set = reduction.get("final_set") or []
    lines.append(
        f"**Mid-loop corner set:** {len(final_set)} corners"
        + (f" ({', '.join(final_set)})" if final_set else "")
    )
    lines.append(f"**Re-entry attempts:** {reduction.get('attempts')}")

    baselines = reduction.get("area_baselines")
    line = f"**Area-gate baselines:** {baselines}"
    if isinstance(baselines, int) and baselines > 1:
        line += (
            f" - the growth limit was re-anchored on each re-entry, so a component's "
            f"allowed growth against the deck this run started from is tier^{baselines}"
        )
    lines.append(line)

    disagreement = reduction.get("path_disagreement")
    if disagreement:
        pairs = ", ".join(
            f"{name} at {corner}"
            for name, corner in zip(
                disagreement.get("criteria", []), disagreement.get("corners", [])
            )
        )
        lines.append(f"**Path disagreement:** {pairs}")

    skipped = reduction.get("reentry_skipped")
    if skipped:
        lines.append(
            f"**Re-entry skipped:** the tuning loop returned "
            f"{skipped.get('orchestration_status')} "
            f"({skipped.get('orchestration_failure_reason')}), so no converged deck "
            f"was available to carry forward"
        )

    return lines


def _topology_lines(swaps: list | None) -> list[str]:
    """토폴로지 스왑을 설명하는 섹션. 스왑이 없었으면 빈 목록.

    스왑은 블록 본문을 통째로 갈아끼운다 - 실측 실행에서 `BUF_P`의 16소자
    본문이 극성도 사이징도 다른 본문으로 바뀌었는데 `result.json`도
    `report.md`도 그 사실을 한 줄도 말하지 않았다. 최적화 단계에서 이미 같은
    값을 치렀다("결과는 자기가 돌려주는 덱을 설명해야 한다").

    `unconstrained`/`stale` 개수를 함께 적는 이유는 그것이 **면적 게이트가 이
    실행의 나머지 구간에서 무엇을 더 이상 묶지 못하는지**이기 때문이다.
    기준선은 `netlist_v0`에서 한 번만 잡히고 스왑 후 갱신되지 않는다(의도된
    설계) - 그 침묵을 리포트에 적는다.

    돌지 않은 실행에 빈 섹션을 그리지 않는 것은 최적화/코너 축소 섹션과 같은
    규칙이다."""
    if not swaps:
        return []

    lines = ["", "## Topology swaps", ""]
    for swap in swaps:
        outcome = swap.get("outcome") or "unknown"
        lines.append(
            f"- iteration {swap.get('outer_iter')}: `{swap.get('block_path')}` <- "
            f"`{swap.get('topology_id')}` ({outcome})"
        )
        lines.append(
            f"  - area gate after this swap: {swap.get('unconstrained_refdes')} refdes "
            f"unconstrained, {swap.get('stale_baseline_refdes')} with a stale baseline"
        )
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
    lines += _topology_lines(result.get("topology_swaps"))
    lines += _optimization_lines(result.get("optimization"))
    lines += _corner_reduction_lines(result.get("corner_reduction"))
    if result.get("failure_reason"):
        lines.append("")
        lines.append(f"**Failure reason:** {result['failure_reason']}")
    text = "\n".join(lines) + "\n"
    path = os.path.join(run_dir, "report.md")
    with open(path, "w") as f:
        f.write(text)
    return path
