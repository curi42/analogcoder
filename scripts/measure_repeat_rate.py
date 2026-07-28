#!/usr/bin/env python3
"""반복 제안률 - D1 이전 커밋과 이후 커밋에서 **같은 방식으로** 계산된다.

정의: 같은 런 안에서, 이미 rolled_back 또는 rejected 로 끝난 (refdes, param)을
다시 제안한 변경 수 / 전체 제안 변경 수.

D1이 추가한 attempt_log 이벤트를 쓰지 않는다 - 쓰면 이전 커밋에서 잴 수 없고,
그러면 비교 자체가 성립하지 않는다.
"""

import json
import sys
from pathlib import Path

GATES = ("area_check", "refdes_check", "param_check", "stimulus_check")


def measure(history_path: Path) -> dict:
    failed: set[tuple[str, str]] = set()
    pending: dict[tuple[int, int], list[tuple[str, str]]] = {}
    last_approved: tuple[int, int] | None = None
    proposals = repeats = iterations = 0

    for line in open(history_path):
        event = json.loads(line)
        step = event.get("step")

        if step == "tuning_proposal":
            key = (event["outer_iter"], event["retry"])
            knobs = [(c["refdes"], c["param"]) for c in event["proposed_changes"]]
            pending[key] = knobs
            last_approved = key
            for knob in knobs:
                proposals += 1
                if knob in failed:
                    repeats += 1

        elif step in GATES and not event["approved"]:
            failed.update(pending.get((event["outer_iter"], event["retry"]), []))

        elif step == "verify_pre" and not event["approved"]:
            failed.update(pending.get((event["outer_iter"], event["retry"]), []))

        elif step == "verify_post" and event["recommendation"] == "rollback":
            failed.update(pending.get(last_approved, []))

        elif step == "judge":
            iterations = max(iterations, event.get("outer_iter", 0))

    return {
        "proposals": proposals,
        "repeats": repeats,
        "rate": repeats / proposals if proposals else 0.0,
        "iterations": iterations,
    }


def main(run_dirs: list[str]) -> None:
    for run_dir in run_dirs:
        history = Path(run_dir) / "history.jsonl"
        if not history.exists():
            print(f"{run_dir}: history.jsonl 없음")
            continue
        m = measure(history)
        print(
            f"{run_dir}: proposals={m['proposals']} repeats={m['repeats']} "
            f"rate={m['rate']:.3f} iterations={m['iterations']}"
        )


if __name__ == "__main__":
    main(sys.argv[1:])
