#!/usr/bin/env python3
"""실험 1 - 튜너 호출 단위 페어드 비교 (D1 재측정 설계, 2026-07-29).

기록된 런의 `history.jsonl`을 재생해 각 `tuning_proposal` 시점의 튜너 입력을
그대로 복원하고, 그 시점에서 튜너를 두 팔로 호출한다.

- **A (D1 off)** — 그 시점의 `tuning_history`를 통째로 비운다. D1 이전
  오케스트레이터가 하던 것과 같다: `attempts_view`가 비고 `touched_refdes`도
  빈다. 후자를 빼먹으면 초점이 그대로 남아 **프롬프트 문단만** 재는 것이 되고,
  그것은 출하된 개입이 아니다.
- **B (D1 on)** — 복원한 실제 `tuning_history`.

나머지 인자(judge 결과, 거부 피드백, 넷리스트, 스펙)는 두 팔이 문자 그대로
같다. 시뮬레이터는 한 번도 돌지 않는다.

재생이 틀리면 실험 전체가 틀리므로, LLM 호출을 한 번도 쓰기 전에 런이 남긴
`attempt_log` 이벤트(`total`/`by_outcome`/`rendered`/`dropped`)와 대조한다.
하나라도 어긋나면 멈춘다.

사용법:
    python scripts/paired_tuner_probe.py \
        --spec benchmarks/two_stage_opamp/spec.yaml \
        --run runs/measure/after/two_stage-1 --run runs/measure/after/two_stage-2 \
        --out runs/paired_probe/results.json --repeats 5
    # --dry-run 을 주면 재생/검증/시점 선정까지만 하고 LLM 호출은 하지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from analogcoder.agents.backend import AgentExecutionError  # noqa: E402
from analogcoder.agents.backends.claude_sdk import ClaudeSDKBackend  # noqa: E402
from analogcoder.agents.tuner import propose_tuning  # noqa: E402
from analogcoder.attempt_log import (  # noqa: E402
    ATTEMPT_RENDER_LIMIT,
    Attempt,
    deltas_between,
    regressed_between,
    render_attempts,
)
from analogcoder.control_block import measurement_nets  # noqa: E402
from analogcoder.netlist import apply_changes  # noqa: E402
from analogcoder.patterns import find_patterns  # noqa: E402
from analogcoder.signal_path import build_signal_paths  # noqa: E402
from analogcoder.spec import load_spec  # noqa: E402
from analogcoder.structure import derive_structure  # noqa: E402
from analogcoder.structure_view import render_netlist, render_structure, select_focus  # noqa: E402

# 게이트 이벤트 -> Attempt.reason. orchestrator._record_rejected 가 쓰는 코드와
# 같은 문자열이어야 한다 - 재생본이 렌더링까지 같아야 하기 때문이다.
GATE_REASONS = {
    "area_check": "area",
    "refdes_check": "refdes",
    "param_check": "param",
    "stimulus_check": "stimulus",
}
# state.log_event 가 judge 결과에 덧붙이는 키들. 튜너에게 넘어간 judge_result 를
# 되찾으려면 이것만 걷어내면 된다.
JUDGE_META = ("step", "outer_iter", "post_tuning", "post_topology_swap")

FAILED_OUTCOMES = ("rejected", "rolled_back")


# ---------------------------------------------------------------- 값 비교


def normalize_value(raw) -> tuple[str, object]:
    """값 비교 키. 양쪽 다 float 로 파싱되면 수치로, 아니면 공백만 턴 문자열로
    비교한다. 태그를 붙여 두면 ("num", 14.0) 과 ("str", "14u") 가 절대 같아지지
    않는다 - 한쪽만 파싱되는 경우를 조용히 같다고 읽지 않기 위해서다."""
    text = str(raw).strip()
    try:
        return ("num", float(text))
    except (TypeError, ValueError):
        return ("str", text)


def triple_of(refdes: str, param: str, new_value) -> tuple:
    return (refdes, param, normalize_value(new_value))


# ---------------------------------------------------------------- 재생


@dataclass
class Timepoint:
    run: str
    outer_iter: int
    retry: int
    judge_result: dict
    rejection_feedback: str | None
    netlist_texts: dict[str, str]
    history: list[Attempt]
    logged_attempt_log: dict
    actual_changes: list[dict]

    @property
    def key(self) -> str:
        return f"{self.run}#{self.outer_iter}.{self.retry}"

    @property
    def failed_knobs(self) -> set[tuple[str, str]]:
        return {
            (a.refdes, a.param) for a in self.history if a.outcome in FAILED_OUTCOMES
        }

    @property
    def failed_triples(self) -> set[tuple]:
        return {
            triple_of(a.refdes, a.param, a.new_value)
            for a in self.history
            if a.outcome in FAILED_OUTCOMES
        }


def _strip_judge(event: dict) -> dict:
    return {k: v for k, v in event.items() if k not in JUDGE_META}


def _outcome_counts(attempts: list[Attempt]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for a in attempts:
        counts[a.outcome] = counts.get(a.outcome, 0) + 1
    return counts


def _record_rejected(
    history: list[Attempt], outer_iter: int, retry: int, proposal: dict, reason: str, detail
) -> None:
    """orchestrator._record_rejected 와 같은 규칙 - 게이트는 제안 전체를
    거부하므로 모든 변경이 같은 사유로 한 항목씩 들어간다."""
    for change in proposal["proposed_changes"]:
        history.append(
            Attempt(
                outer_iter=outer_iter,
                retry=retry,
                refdes=change["refdes"],
                param=change["param"],
                old_value=change["old_value"],
                new_value=change["new_value"],
                outcome="rejected",
                reason=reason,
                detail=detail,
            )
        )


def read_events(history_path: Path) -> list[dict]:
    return [json.loads(line) for line in open(history_path) if line.strip()]


def replay(events: list[dict], initial_texts: dict[str, str], run_label: str = "") -> tuple[list[Timepoint], dict[str, str]]:
    """orchestrator.run_orchestration 의 상태 갱신을 이벤트에서 되짚는다.

    돌려주는 것: 모든 `tuning_proposal` 시점(필터 전)과 마지막 넷리스트.
    """
    history: list[Attempt] = []
    stack: list[dict[str, str]] = [dict(initial_texts)]
    timepoints: list[Timepoint] = []

    pending_log: dict | None = None
    pending_proposal: dict | None = None
    approved: dict | None = None
    prev_judge: dict | None = None
    post_judge: dict | None = None
    rejection_feedback = None

    for event in events:
        step = event.get("step")

        if step in ("topology_proposal", "topology_swap"):
            # 이 실험이 재는 대상이 아니고, 재생 규칙도 다르다. 조용히 무시하면
            # 넷리스트 재구성이 어긋난 채로 진행되므로 큰 소리로 멈춘다.
            raise ValueError("topology events are not supported by this replay")

        if step == "judge":
            if event.get("post_tuning") or event.get("post_topology_swap"):
                post_judge = _strip_judge(event)
            else:
                prev_judge = _strip_judge(event)
                # 새 outer iteration - 오케스트레이터도 재시도 루프 직전에
                # rejection_feedback 을 None 으로 초기화한다.
                rejection_feedback = None

        elif step == "attempt_log":
            pending_log = event

        elif step == "tuning_proposal":
            timepoints.append(
                Timepoint(
                    run=run_label,
                    outer_iter=event["outer_iter"],
                    retry=event["retry"],
                    judge_result=prev_judge,
                    rejection_feedback=rejection_feedback,
                    netlist_texts=dict(stack[-1]),
                    history=list(history),
                    logged_attempt_log=pending_log,
                    actual_changes=event["proposed_changes"],
                )
            )
            pending_proposal = event

        elif step in GATE_REASONS and not event["approved"]:
            rejection_feedback = event["feedback"]
            _record_rejected(
                history,
                event["outer_iter"],
                event["retry"],
                pending_proposal,
                GATE_REASONS[step],
                event["feedback"],
            )

        elif step == "verify_pre":
            if event["approved"]:
                approved = pending_proposal
            else:
                rejection_feedback = event["feedback"]
                _record_rejected(
                    history,
                    event["outer_iter"],
                    event["retry"],
                    pending_proposal,
                    "verify_pre",
                    event["feedback"],
                )

        elif step == "verify_post" and not event.get("topology_swap"):
            outcome = "rolled_back" if event["recommendation"] == "rollback" else "kept"
            # 델타/회귀는 verify_post 의 주장이 아니라 judge 가 낸 숫자에서 온다
            # (attempt_log.regressed_between 의 docstring 참고).
            deltas = deltas_between(prev_judge, post_judge)
            regressed = regressed_between(prev_judge, post_judge)
            for change in approved["proposed_changes"]:
                history.append(
                    Attempt(
                        outer_iter=approved["outer_iter"],
                        retry=approved["retry"],
                        refdes=change["refdes"],
                        param=change["param"],
                        old_value=change["old_value"],
                        new_value=change["new_value"],
                        outcome=outcome,
                        deltas=deltas,
                        regressed=regressed,
                    )
                )
            # push 후 rollback 은 스택에 대해 no-op 이므로 kept 일 때만 민다.
            if outcome == "kept":
                stack.append(
                    {
                        name: apply_changes(text, approved["proposed_changes"])
                        for name, text in stack[-1].items()
                    }
                )
            approved = None

    return timepoints, stack[-1]


def verify_replay(timepoints: list[Timepoint]) -> list[str]:
    """복원한 히스토리를 런이 실제로 남긴 `attempt_log` 와 대조한다.

    렌더 문자열 자체는 로그에 없으므로 그 문자열을 결정하는 네 수치를 전부
    맞춰 본다: 총 항목 수, 결과별 개수, 렌더된 수, 잘린 수.
    """
    problems: list[str] = []
    for tp in timepoints:
        logged = tp.logged_attempt_log
        if logged is None:
            problems.append(f"{tp.key}: attempt_log 이벤트가 없다")
            continue
        if logged["outer_iter"] != tp.outer_iter or logged["retry"] != tp.retry:
            problems.append(
                f"{tp.key}: attempt_log 가 {logged['outer_iter']}.{logged['retry']} 을 가리킨다"
            )
        rendered = len(tp.history[-ATTEMPT_RENDER_LIMIT:])
        checks = {
            "total": (len(tp.history), logged["total"]),
            "by_outcome": (_outcome_counts(tp.history), logged["by_outcome"]),
            "rendered": (rendered, logged["rendered"]),
            "dropped": (len(tp.history) - rendered, logged["dropped"]),
        }
        for field_name, (mine, theirs) in checks.items():
            if mine != theirs:
                problems.append(f"{tp.key}: {field_name} 재생={mine} 로그={theirs}")
    return problems


def select_timepoints(timepoints: list[Timepoint]) -> list[Timepoint]:
    """`tuning_history` 가 비어 있지 않고 `failed` 집합도 비어 있지 않은 시점.

    두 조건 모두 필요하다 - 히스토리가 있어도 전부 kept 면 `R_exact` 의 분자가
    구조적으로 0 이고, 그것이 첫 측정을 무효로 만든 바로 그 모양이다.
    """
    return [tp for tp in timepoints if tp.history and tp.failed_knobs]


# ---------------------------------------------------------------- 채점


def score_changes(changes: list[dict], failed_knobs: set, failed_triples: set) -> dict:
    exact = []
    knob = []
    for change in changes:
        key = (change["refdes"], change["param"])
        triple = triple_of(change["refdes"], change["param"], change["new_value"])
        if triple in failed_triples:
            exact.append(change)
        elif key in failed_knobs:
            knob.append(change)
    return {
        "any_exact": bool(exact),
        "any_knob": bool(knob),
        "exact_changes": [
            {"refdes": c["refdes"], "param": c["param"], "new_value": c["new_value"]} for c in exact
        ],
        "knob_changes": [
            {"refdes": c["refdes"], "param": c["param"], "new_value": c["new_value"]} for c in knob
        ],
    }


# ---------------------------------------------------------------- 통계


def mcnemar_exact(b: int, c: int) -> float:
    """불일치 쌍 (b, c) 에 대한 정확 이항 양측 p 값 (p=0.5).

    정규 근사를 쓰지 않는다 - 불일치 쌍이 한 자릿수일 수 있고, 그때 근사는
    틀린다. scipy 는 이 저장소의 의존성이 아니므로 직접 센다.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2**n)
    return min(1.0, 2 * tail)


# ---------------------------------------------------------------- 실행


@dataclass
class ArmInputs:
    structure_view: str
    netlist_view: str
    attempts_view: str
    focus: list[str]


def build_arm_inputs(tp: Timepoint, spec, use_history: bool) -> ArmInputs:
    """orchestrator.py:229-243 / :476 의 구성을 그대로 되짚는다."""
    canonical = spec.canonical.name
    text = tp.netlist_texts[canonical]
    structure = derive_structure(text, spec.circuit_name)
    paths = build_signal_paths(structure)

    measurement_by_criterion = {
        c.name: c.measurement for tb in spec.testbenches for c in tb.criteria
    }
    nets_by_measurement: dict[str, set[str]] = {}
    for tb in spec.testbenches:
        for name, nets in measurement_nets(tb.control_block).items():
            nets_by_measurement.setdefault(name, set()).update(nets)

    failing_nets: set[str] = set()
    for criterion in tp.judge_result["criteria"]:
        if criterion["pass"]:
            continue
        measurement = measurement_by_criterion.get(criterion["name"])
        failing_nets |= nets_by_measurement.get(measurement, set())

    history = tp.history if use_history else []
    touched_refdes = {a.refdes for a in history}
    focus = select_focus(structure, paths, failing_nets, touched_refdes, text)
    return ArmInputs(
        structure_view=render_structure(structure, paths, find_patterns(structure), focus),
        netlist_view=render_netlist(text, focus),
        attempts_view=render_attempts(history),
        focus=sorted(focus),
    )


async def run_sweep(points: list[Timepoint], spec, repeats: int, out_path: Path) -> dict:
    backend = ClaudeSDKBackend()
    records: list[dict] = []
    failures: list[dict] = []

    total_calls = len(points) * repeats * 2
    done = 0
    started = time.time()

    for tp in points:
        arms = {arm: build_arm_inputs(tp, spec, arm == "B") for arm in ("A", "B")}
        failed_knobs = tp.failed_knobs
        failed_triples = tp.failed_triples
        for repeat in range(repeats):
            # 팔을 반복 안에서 번갈아 부른다 - 쌍이 시간상 붙어 있어야 어떤
            # 표류가 있더라도 페어드 비교가 그것을 흡수한다.
            for arm in ("A", "B"):
                inputs = arms[arm]
                record = {
                    "run": tp.run,
                    "outer_iter": tp.outer_iter,
                    "retry": tp.retry,
                    "arm": arm,
                    "repeat": repeat,
                    "focus": inputs.focus,
                    "attempts_view_chars": len(inputs.attempts_view),
                    "netlist_view_chars": len(inputs.netlist_view),
                }
                try:
                    # 절대 동시에 부르지 않는다: claude-agent-sdk 는 동시
                    # 사용에서 깨지고 두 호출이 함께 실패한다.
                    proposal = await propose_tuning(
                        inputs.structure_view,
                        tp.judge_result,
                        inputs.attempts_view,
                        tp.rejection_feedback,
                        inputs.netlist_view,
                        backend,
                    )
                except Exception as exc:  # noqa: BLE001
                    # 떨어진 호출은 결측이지 0 이 아니다. 스윕을 멈추지 않고
                    # null 로 남긴 뒤 따로 센다. AgentExecutionError 가 주된
                    # 경우지만 claude-agent-sdk 의 전송 예외가 그대로 올라올
                    # 수도 있어 넓게 잡는다 - 측정 하나가 스윕 전체를 죽이는
                    # 것보다 결측 하나가 낫다.
                    record["agent_execution_error"] = isinstance(exc, AgentExecutionError)
                    record["error"] = f"{type(exc).__name__}: {exc}"
                    record["result"] = None
                    failures.append(record)
                else:
                    changes = proposal.get("proposed_changes", [])
                    record["result"] = {
                        "proposed_changes": [
                            {
                                "refdes": c.get("refdes"),
                                "param": c.get("param"),
                                "old_value": c.get("old_value"),
                                "new_value": c.get("new_value"),
                            }
                            for c in changes
                        ],
                        **score_changes(changes, failed_knobs, failed_triples),
                    }
                records.append(record)
                done += 1
                elapsed = time.time() - started
                print(
                    f"  [{done}/{total_calls}] {tp.key} arm={arm} r={repeat} "
                    f"{'ERR' if record.get('error') else ('EXACT' if record['result']['any_exact'] else ('knob' if record['result']['any_knob'] else 'new'))}"
                    f"  ({elapsed:.0f}s)",
                    flush=True,
                )
                # 중간에 죽어도 데이터를 잃지 않는다.
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(json.dumps({"records": records}, indent=2))

    return {"records": records, "failures": failures}


# ---------------------------------------------------------------- 분석


def analyse(points: list[Timepoint], records: list[dict], repeats: int) -> dict:
    by_key: dict[tuple, dict] = {}
    for rec in records:
        by_key[(rec["run"], rec["outer_iter"], rec["retry"], rec["arm"], rec["repeat"])] = rec

    pairs = []
    for tp in points:
        for repeat in range(repeats):
            a = by_key.get((tp.run, tp.outer_iter, tp.retry, "A", repeat))
            b = by_key.get((tp.run, tp.outer_iter, tp.retry, "B", repeat))
            if not a or not b or a["result"] is None or b["result"] is None:
                continue
            pairs.append(
                {
                    "point": tp.key,
                    "repeat": repeat,
                    "a_exact": a["result"]["any_exact"],
                    "b_exact": b["result"]["any_exact"],
                    "a_knob": a["result"]["any_knob"],
                    "b_knob": b["result"]["any_knob"],
                }
            )

    table = {"both": 0, "a_only": 0, "b_only": 0, "neither": 0}
    for p in pairs:
        if p["a_exact"] and p["b_exact"]:
            table["both"] += 1
        elif p["a_exact"]:
            table["a_only"] += 1
        elif p["b_exact"]:
            table["b_only"] += 1
        else:
            table["neither"] += 1
    p_value = mcnemar_exact(table["a_only"], table["b_only"])

    n = len(pairs)
    a_rate = sum(p["a_exact"] for p in pairs) / n if n else 0.0
    b_rate = sum(p["b_exact"] for p in pairs) / n if n else 0.0

    per_point = []
    for tp in points:
        mine = [p for p in pairs if p["point"] == tp.key]
        if not mine:
            continue
        per_point.append(
            {
                "point": tp.key,
                "pairs": len(mine),
                "a_exact_rate": sum(p["a_exact"] for p in mine) / len(mine),
                "b_exact_rate": sum(p["b_exact"] for p in mine) / len(mine),
                "a_knob_rate": sum(p["a_knob"] for p in mine) / len(mine),
                "b_knob_rate": sum(p["b_knob"] for p in mine) / len(mine),
            }
        )

    knob_table = {"both": 0, "a_only": 0, "b_only": 0, "neither": 0}
    for p in pairs:
        if p["a_knob"] and p["b_knob"]:
            knob_table["both"] += 1
        elif p["a_knob"]:
            knob_table["a_only"] += 1
        elif p["b_knob"]:
            knob_table["b_only"] += 1
        else:
            knob_table["neither"] += 1

    if p_value >= 0.05:
        verdict = "효과 없음"
    elif b_rate < a_rate:
        verdict = "D1 효과 있음"
    else:
        verdict = "역효과"

    return {
        "pairs": n,
        "r_exact": {
            "a_rate": a_rate,
            "b_rate": b_rate,
            "table": table,
            "mcnemar_exact_p": p_value,
        },
        "r_knob": {
            "a_rate": sum(p["a_knob"] for p in pairs) / n if n else 0.0,
            "b_rate": sum(p["b_knob"] for p in pairs) / n if n else 0.0,
            "table": knob_table,
            "mcnemar_exact_p": mcnemar_exact(knob_table["a_only"], knob_table["b_only"]),
            "note": "맥락 전용 - 판정 입력이 아니다",
        },
        "per_point": per_point,
        "verdict": verdict,
    }


# ---------------------------------------------------------------- main


def load_run(run_dir: Path, spec, label: str) -> tuple[list[Timepoint], dict[str, str]]:
    names = [tb.name for tb in spec.testbenches]
    initial = {}
    for name in names:
        initial[name] = (run_dir / f"netlist_v0_{name}.cir").read_text()
    events = read_events(run_dir / "history.jsonl")
    return replay(events, initial, run_label=label)


def check_final_deck(run_dir: Path, final_texts: dict[str, str]) -> list[str]:
    """재구성한 마지막 덱이 런이 실제로 돌려준 덱과 같은지. apply_changes 재생
    전체를 한 번에 검증하는 종단 확인이다."""
    result_path = run_dir / "result.json"
    if not result_path.exists():
        return [f"{run_dir}: result.json 없음 - 최종 덱 대조 생략"]
    result = json.loads(result_path.read_text())
    problems = []
    for name, path in result.get("final_netlist_paths", {}).items():
        on_disk = Path(path)
        if not on_disk.is_absolute():
            on_disk = Path.cwd() / path
        if not on_disk.exists():
            problems.append(f"{run_dir}: {path} 없음")
            continue
        if on_disk.read_text() != final_texts.get(name):
            problems.append(f"{run_dir}: 최종 덱 불일치 ({name})")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--run", action="append", required=True)
    parser.add_argument("--out", default="runs/paired_probe/results.json")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    spec = load_spec(args.spec)

    all_points: list[Timepoint] = []
    all_timepoints: list[Timepoint] = []
    problems: list[str] = []
    for run in args.run:
        run_dir = Path(run)
        timepoints, final_texts = load_run(run_dir, spec, label=run_dir.name)
        problems += verify_replay(timepoints)
        problems += check_final_deck(run_dir, final_texts)
        all_timepoints += timepoints
        all_points += select_timepoints(timepoints)

    print(f"제안 시점 {len(all_timepoints)}개 -> 선정 {len(all_points)}개")
    for tp in all_points:
        print(
            f"  {tp.key}  history={len(tp.history)} failed_knobs={len(tp.failed_knobs)} "
            f"failed_triples={len(tp.failed_triples)}"
        )
    if problems:
        print("\n재생 검증 실패 - 실험을 돌리지 않는다:")
        for p in problems:
            print("  " + p)
        return 1
    print("재생 검증 통과 (attempt_log 전 시점 일치, 최종 덱 일치)")

    if args.dry_run:
        return 0

    out_path = Path(args.out)
    sweep = asyncio.run(run_sweep(all_points, spec, args.repeats, out_path))
    summary = analyse(all_points, sweep["records"], args.repeats)
    summary["timepoints"] = len(all_points)
    summary["repeats"] = args.repeats
    summary["calls_attempted"] = len(sweep["records"])
    summary["calls_failed"] = len(sweep["failures"])
    summary["failed_calls"] = [
        {k: v for k, v in f.items() if k in ("run", "outer_iter", "retry", "arm", "repeat", "error")}
        for f in sweep["failures"]
    ]
    out_path.write_text(json.dumps({"summary": summary, "records": sweep["records"]}, indent=2))
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
