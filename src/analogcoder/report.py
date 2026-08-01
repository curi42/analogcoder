import os

from analogcoder.corner_selection import raw_label
from analogcoder.json_io import dump as json_dump


def write_result_json(run_dir: str, result: dict) -> str:
    # `json_io.dump`는 비유한 float를 문자열 표지로 정규화한 뒤
    # `allow_nan=False`로 쓴다. 이 파일에는 실제로 NaN이 들어간다 -
    # `judge_tools.evaluate_criteria`가 측정이 없는 기준의 `actual`/`margin`에
    # `math.nan`을 싣고, 그것은 정상 경로다(실측: `runs/pvt_sonnet_1`의
    # `result.json`에 리터럴 `NaN` 8개, node의 `JSON.parse`가 파일 전체를
    # 거부).
    #
    # **정규화 없이 `allow_nan=False`만 켜면 안 된다.** 그러면 여기서 터지는
    # `ValueError`가 `cli.main()`의 다음 줄인 `write_report_md`까지 날린다 -
    # 최적화 단계가 크래시해서 산출물이 통째로 사라졌던 사건과 같은 모양이다.
    path = os.path.join(run_dir, "result.json")
    with open(path, "w") as f:
        json_dump(result, f, indent=2)
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
    attempts = reduction.get("attempts") or 0
    lines.append(f"**Re-entry attempts:** {attempts}")

    # M10(T19): 재진입에는 두 종류가 있고 지금까지 리포트는 **어느 쪽도
    # 그리지 않았다.** `grown`은 코너가 실제로 늘어난 attempt만 담고, 탐침
    # 승격 재진입 attempt는 `grown`에 빈 리스트를 신는다 - 그것만 보면
    # "무엇을 더했는지 기록이 없다"로 읽히지 "새 코너를 판정하러
    # 재진입했다"로 읽히지 않는다. `attempts > 0`이면 **항상** 그린다 - 재진입은
    # 했는데 무엇을 했는지 리포트에 없으면 이 저장소가 이미 치른 값이
    # 그대로 반복된다.
    if attempts:
        grown = reduction.get("grown") or []
        promotions = {p["attempt"]: p for p in (reduction.get("promotion_reentries") or [])}
        lines.append("")
        lines.append("**Corner set growth:**")
        for i in range(1, attempts + 1):
            promo = promotions.get(i)
            added = grown[i - 1] if i - 1 < len(grown) else None
            if promo is not None:
                pairs = ", ".join(
                    f"{name} at {corner}"
                    for name, corner in zip(
                        promo.get("criteria", []), promo.get("corners", [])
                    )
                )
                lines.append(
                    f"- attempt {i}: no corner added - re-entered to judge {pairs}, "
                    f"promoted by the probe after the judge last saw the set"
                )
            elif added:
                lines.append(f"- attempt {i}: added {', '.join(added)}")
            else:
                # `grown`에 이 attempt의 항목이 없거나 빈 리스트인데 승격
                # 재진입 기록도 없다 - 옛 result.json(이 필드가 생기기 전)이거나
                # 두 리스트가 어긋난 자리다. 지어내지 않고 그 사실 자체를 적는다.
                lines.append(f"- attempt {i}: no growth record available")
        # M3(T19 리뷰): 이 파일은 `"\n".join(lines)`로 쓰므로, 불릿 목록 바로
        # 뒤에 빈 줄이 없으면 다음 굵은 줄(`**Corner seed:**` 등)이 마크다운의
        # lazy continuation으로 마지막 불릿에 흡수된다 - 특히
        # `**Area-gate baselines:**`는 이 함수 독스트링이 "이 섹션이 존재하는
        # 유일한 이유"라고 적은 줄이라 흡수되면 안 된다.
        lines.append("")

    # **씨앗을 무엇으로 골랐는지.** history.jsonl의 `corner_seed`에만 있던 사실을
    # 여기로 끌어온다 - result.json/report.md만 보는 사람은 argmax와 ε-coverage
    # 중 무엇이 돌았는지 알 방법이 없었다. 없을 때(재개 회차에 다시 안 뽑았거나
    # 축소 자체가 꺼졌을 때)는 줄을 그리지 않는다 - 뽑지 않은 것을 뽑은 것처럼
    # 적지 않는다.
    seed = reduction.get("seed")
    if seed:
        line = f"**Corner seed:** {seed.get('mode')}"
        if seed.get("mode") == "coverage":
            dropped = seed.get("dropped") or []
            line += (
                f" (epsilon={seed.get('epsilon')}, tau={seed.get('tau')}, "
                f"dropped {len(dropped)} argmax corner(s))"
            )
        # **알고리즘 지표를 여기 싣는다.** `points_per_tb`는 seed_from_sweep이
        # 낸 값 그 자체(테스트벤치당 실제로 도는 점 수)이고, 두 모드 모두에서
        # 뜻이 같다 - 웨이브 수는 워커 수에 딸린 배포 사실이라 지표가 아니고,
        # 리포트만 보는 사람에게는 지금까지 이 숫자가 전혀 안 보였다.
        if seed.get("points_per_tb") is not None:
            line += f", points_per_tb={seed['points_per_tb']}"
        lines.append(line)
        # **목표 피복률에 못 미친 씨앗이 성공처럼 읽히면 안 된다.** coverage
        # 모드에서만 존재하는 값이라 `in seed`로 부재와 False를 구별한다 -
        # argmax 모드에는 애초에 "목표"라는 개념이 없다.
        if "reached_target" in seed:
            reached = seed["reached_target"]
            reached_line = f"**Coverage target reached:** reached_target={reached}"
            if not reached:
                reached_line += (
                    " - **the seed did NOT reach its declared coverage target** "
                    "(tau); the mid-loop corner set covers fewer criteria than "
                    "the spec's coverage block asked for"
                )
            lines.append(reached_line)

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
        # 코너 축소 재진입이 붙은 실행에서는 `outer_iter`가 시도마다 1부터
        # 다시 세므로, 시도 번호 없이는 attempt 0의 iteration 4와 attempt 1의
        # iteration 4가 같은 줄로 보인다.
        prefix = f"attempt {swap['attempt']}, " if "attempt" in swap else ""
        lines.append(
            f"- {prefix}iteration {swap.get('outer_iter')}: `{swap.get('block_path')}` <- "
            f"`{swap.get('topology_id')}` ({outcome})"
        )
        lines.append(
            f"  - area gate after this swap: {swap.get('unconstrained_refdes')} refdes "
            f"unconstrained, {swap.get('stale_baseline_refdes')} with a stale baseline"
        )
    return lines


def _resume_lines(resumed_from: dict | None) -> list[str]:
    """재개한 실행에만 그린다. 재개하지 않았으면 빈 목록 - 최적화/코너 축소
    섹션과 같은 규칙이다(돌지 않은 단계에 빈 섹션을 그리면 "돌았는데 아무것도
    못 했다"로 읽힌다).

    **result.json 쪽은 반대다**: `resumed_from`은 재개하지 않은 실행에도
    `null`로 항상 실린다. "재개 안 함"과 "필드가 사라짐"이 같은 모양이면 안
    되기 때문이고, `topology_swaps`가 항상 실리는 것과 같은 이유다.

    버려진 줄 수를 함께 적는 이유: 부분 런이 온전한 런처럼 측정 데이터에 들어간
    것이 D1 측정이 무효가 된 원인의 절반이었다. 재개 여부가 결과에서 안 보이면
    이 기능은 측정 장치를 고치는 게 아니라 망가뜨린다."""
    if not resumed_from:
        return []

    lines = [
        "",
        "## Resume",
        "",
        f"**Resumed at:** the `{resumed_from.get('boundary')}` boundary "
        f"(attempt {resumed_from.get('attempt')}, iteration {resumed_from.get('outer_iter')})",
        f"**Checkpoint:** `{resumed_from.get('checkpoint_path')}`",
    ]
    discarded = resumed_from.get("discarded_lines")
    # 빈 범위([40, 40])는 "버린 것이 없다"이지 "40-39번 줄"이 아니다.
    if discarded and discarded[1] > discarded[0]:
        lines.append(
            f"**Abandoned history lines:** {discarded[1] - discarded[0]} "
            f"(`history.jsonl` lines {discarded[0]}-{discarded[1] - 1}) - the log is not "
            f"truncated; `analogcoder.history.read_events` drops that range so a "
            f"half-finished iteration is not counted twice"
        )
    else:
        lines.append(
            "**Abandoned history lines:** 0 - the run died before writing any event "
            "past the checkpoint"
        )
    lines.append(f"**Resumes so far (this run dir):** {resumed_from.get('resume_count')}")
    return lines


def _pvt_lines(sweep: dict | None) -> list[str]:
    """**판정을 내리는** PVT 스윕. 키가 없으면 빈 목록(스윕이 안 돈 실행).

    이 섹션이 없던 리포트는 실제로 이렇게 나왔다 - `runs/pvt_sonnet_1`을
    렌더링하면 `**Status:** FAIL` 아래에 7개 기준이 **전부 `[PASS]`**이고
    `[FAIL]`이 0줄, `corner` 문자열이 0회다. 같은 `result.json`의 `pvt_sweep`은
    7개 전부 FAIL이고 `dc_gain`은 71.09 -> **3.14 dB**로 붕괴해 있었다.
    리포트만 읽는 사람에게 남는 것은 "기준은 다 통과했는데 왜 FAIL이지"다.
    "결과는 자기가 낸 덱을 설명해야 한다"의 네 번째 재발이자, 유일하게 실제
    산출물에서 관측된 재발이다.

    **통과한 스윕도 그린다.** 최적화/코너 축소/토폴로지 섹션은 "그 단계가 돌지
    않았다"를 침묵으로 표현하는데, 여기서 같은 규칙을 값(overall_pass)에까지
    밀면 "스윕이 통과했다"와 "스윕이 안 돌았다"가 같은 침묵이 된다. 그것이
    이 저장소가 게이트에 대해 아홉 번 치른 값이다. 침묵은 **키의 부재**에만
    대응한다.

    코너 좌표를 함께 적는 이유: 45개 중 어디가 깨졌는지가 이 스윕이 만드는
    유일한 추가 정보이고, 없으면 `result.json`을 열어야만 알 수 있다.
    """
    if not sweep:
        return []

    overall = "PASS" if sweep.get("overall_pass") else "FAIL"
    lines = [
        "",
        "## PVT sweep",
        "",
        f"**Overall:** {overall} - {sweep.get('summary')}",
    ]
    per_corner = sweep.get("per_corner")
    if per_corner is not None:
        lines.append(f"**Corners simulated:** {len(per_corner)}")
    lines += [
        "**Role:** this sweep is the run's veto - a failure here forces the run to FAIL, "
        "and it never turns a failing tuning loop into a PASS.",
        "",
    ]

    worst = sweep.get("worst_case_corners") or {}
    for c in sweep.get("criteria", []):
        mark = "PASS" if c["pass"] else "FAIL"
        entry = worst.get(c["name"])
        label = raw_label(entry)
        if label is None:
            # worst_case_measurements가 항목 자체를 안 만든 경우: 그
            # measurement가 **어느 코너에도** 나타나지 않았다.
            tail = " - the measurement appeared at no corner"
        elif entry.get("value") is None:
            # 이 항목은 argmax가 **아니다**. worst_case_measurements는 측정이
            # 빠진 코너가 하나라도 있으면 그 중 첫 번째를 값 없이 적는다
            # (`missing_corners[0]`). "여기가 최악이었다"로 적으면 데이터에
            # 없는 주장을 하는 것이다.
            tail = f" - no measurement at corner {label} (the first corner missing it)"
        else:
            tail = f" - worst at corner {label}"
        lines.append(
            f"- [{mark}] {c['name']}: target {c['target']}, actual {c['actual']} "
            f"(margin {c['margin']}){tail}"
        )
    return lines


def _final_criteria_provenance(result: dict) -> str:
    """"Final criteria" 표가 **어느 조건의, 누가 낸** 측정인지.

    후보가 셋이고 라벨이 없으면 이 표와 바로 아래 판정 스윕이 서로 다른 회로를
    설명하고 있다는 사실이 보이지 않는다. 실측 `runs/pvt_sonnet_1`에서 이 표는
    7개 전부 PASS(명목 한 점), 스윕은 7개 전부 FAIL이었다.

    두 축은 서로 독립이라 따로 읽는다.

    - **무엇을 쟀는가**: `corner_reduction.active`면 중간 루프가 본 값은 명목
      한 점이 아니라 **선택 집합의 최악값**이다(`corner_sim.build_corner_simulate`).
      아니면 코너를 통과시키지 않은 덱 한 점이다.
    - **누가 판정했는가**: 판정자는 두 경우 모두 `evaluate_criteria`다(LLM
      judge는 제거됐다). 다른 것은 **어느 덱을** 판정했는가다 - `cli.py`는
      최적화가 기준을 재고 왔으면 이 표를 **덮으므로**
      (`optimization["final_criteria"]`) 그때는 bisection이 착지한 버전이고,
      아니면 튜닝 루프가 돌려준 덱이다.
    """
    reduction = result.get("corner_reduction") or {}
    if reduction.get("active"):
        final_set = reduction.get("final_set") or []
        condition = (
            f"the worst value across the mid-loop's reduced corner set "
            f"({len(final_set)} corners"
            + (f": {', '.join(final_set)}" if final_set else "")
            + ")"
        )
    else:
        condition = "the deck as it is - one simulation point, no corner rendering"

    optimization = result.get("optimization") or {}
    if optimization.get("final_criteria"):
        judged_by = (
            "`evaluate_criteria`, on the netlist version the optimization phase landed on"
        )
    else:
        # 판정자는 이제 두 자리 모두 `evaluate_criteria`다(LLM judge 제거).
        # 그래도 두 문장을 합치지 않는다 - **어느 덱을** 판정했는지가 다르고,
        # 그것이 이 줄이 존재하는 이유다.
        judged_by = "`evaluate_criteria`, on the deck the tuning loop returned"

    return f"Measured on {condition}; judged by {judged_by}."


def _attempt_lines(summary: dict | None) -> list[str]:
    """이 실행의 제안이 **어떻게 끝났는지**. 키가 없으면 빈 목록(이 결과를
    만든 코드가 집계를 아예 안 쓴다는 뜻).

    **값이 전부 0이어도 그린다.** 침묵이 "안 돌았다"를 뜻한다는 규칙은 키의
    **부재**에만 걸리고 값에는 걸리지 않는다 - `_pvt_lines`가 통과했을 때도
    그리는 것과 같은 이유다. 여기서 0을 접으면 "거부가 한 건도 없었다"와
    "집계가 사라졌다"가 다시 같은 침묵이 된다.

    이 표가 없던 리포트는 실제로 이랬다: 모든 제안이 면적 게이트에 막혀 덱이
    한 번도 안 바뀐 실행과, 제안이 대부분 채택된 실행의 리포트가 구조적으로
    **동일**했다. 상태와 기준 표만 보고는 그 둘을 가를 수 없다.
    """
    if not summary:
        return []
    outcome = summary.get("by_outcome", {})
    reasons = summary.get("rejected_by_reason", {})
    lines = [
        "",
        "## Tuning attempts",
        "",
        f"**Component changes proposed:** {summary.get('changes', 0)}",
        "",
        "| outcome | count |",
        "| --- | --- |",
    ]
    lines += [f"| {name} | {count} |" for name, count in outcome.items()]
    if reasons:
        lines += [
            "",
            "**Rejections by gate** (a gate rejects the whole proposal, so every "
            "change in it is counted):",
            "",
            "| gate | count |",
            "| --- | --- |",
        ]
        lines += [f"| {name} | {count} |" for name, count in reasons.items()]
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
    pvt_lines = _pvt_lines(result.get("pvt_sweep"))
    lines += [
        "",
        "## Final criteria",
        "",
        f"*{_final_criteria_provenance(result)}*",
    ]
    if pvt_lines:
        # 없는 섹션을 가리키지 않는다 - 스윕이 안 돈 실행에서는 이 표가 곧
        # 실행이 가진 유일한 판정이다.
        lines.append(
            "*These are **not the verdict**: the run's status is decided by the PVT sweep below.*"
        )
    lines.append("")
    for c in result["final_criteria"]:
        mark = "PASS" if c["pass"] else "FAIL"
        lines.append(f"- [{mark}] {c['name']}: target {c['target']}, actual {c['actual']} (margin {c['margin']})")
    lines += pvt_lines
    lines += _resume_lines(result.get("resumed_from"))
    lines += _attempt_lines(result.get("attempt_summary"))
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
