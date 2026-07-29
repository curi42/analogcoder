#!/usr/bin/env python3
"""반복 제안률 - D1 이전 커밋과 이후 커밋에서 **같은 방식으로** 계산된다.

정의: 같은 런 안에서, 이미 rolled_back 또는 rejected 로 끝난 (refdes, param)을
다시 제안한 변경 수 / 전체 제안 변경 수.

D1이 추가한 attempt_log 이벤트를 쓰지 않는다 - 쓰면 이전 커밋에서 잴 수 없고,
그러면 비교 자체가 성립하지 않는다.

**로그는 `analogcoder.history.read_events`로 읽는다.** 재개된 실행의
`history.jsonl`에는 크래시한 이터레이션의 부분 이벤트가 그대로 남아 있고,
재개 후 같은 이터레이션이 다시 돌아 같은 종류의 이벤트를 또 쓴다. 그것을
그대로 세면 버려진 시도의 제안을 실제 제안으로 센다 - D1 측정을 무효로 만든
것과 **같은 부류의 결함**이다(측정 대상이 아닌 것이 측정에 들어간다).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from analogcoder.history import discard_summary, read_events  # noqa: E402

GATES = ("area_check", "refdes_check", "param_check", "stimulus_check")


def measure(history_path: Path) -> dict:
    failed: set[tuple[str, str]] = set()
    pending: dict[tuple[int, int], list[tuple[str, str]]] = {}
    last_approved: tuple[int, int] | None = None
    proposals = repeats = iterations = 0

    for event in read_events(history_path):
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

        # 토폴로지 스왑의 verify_post는 retry가 없고 topology_swap=True를 단다.
        # 그것을 last_approved(마지막 **파라미터** tuning_proposal)에 접으면
        # 스왑 롤백이 무관한 파라미터 노브를 failed로 만든다 - 이 저장소가
        # 반복해서 값을 치른 "조용히 틀린 수" 모양이다. 측정한 세 런에는
        # 토폴로지 verify_post가 0건이라 공표된 수치는 바뀌지 않는다.
        elif (
            step == "verify_post"
            and not event.get("topology_swap")
            and event["recommendation"] == "rollback"
        ):
            failed.update(pending.get(last_approved, []))

        elif step == "judge":
            iterations = max(iterations, event.get("outer_iter", 0))

    summary = discard_summary(history_path)
    return {
        "proposals": proposals,
        "repeats": repeats,
        "rate": repeats / proposals if proposals else 0.0,
        "iterations": iterations,
        # **버린 것이 0이어도 항상 낸다.** 이 수가 안 보이면 "버려진 이벤트가
        # 없었다"와 "떨어뜨리는 계산이 사라졌다"가 같은 출력이 된다 - 이
        # 저장소가 아홉 번 값을 치른 바로 그 모양이다.
        "discarded_lines": summary["discarded"],
        "resumes": len(summary["ranges"]),
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
            f"rate={m['rate']:.3f} iterations={m['iterations']} "
            f"resumes={m['resumes']} discarded_lines={m['discarded_lines']}"
        )


if __name__ == "__main__":
    main(sys.argv[1:])
