"""45코너 코너 축소 A/B 하니스 - `docs/superpowers/specs/2026-08-03-reduction45-benefit-design.md`
가 정한 두 팔(OFF/ON)을 각 3회, `off_1, on_1, off_2, on_2, off_3, on_3` 순서로
돌린다. **판정은 여기서 하지 않는다** - `scripts/reduction45_aggregate.py`가
`result.json`/`history.jsonl`을 읽어 잠긴 규칙을 적용한다. 이 스크립트는
실행만 한다.

`.venv/bin/analogcoder`를 서브프로세스로 띄운다(같은 이유로 사전 등록도
"각 런은 `.venv/bin/analogcoder --spec <경로> --run-dir ...`"라고 적었다) -
LLM 에이전트를 인프로세스로 부르면 한 run의 크래시가 하니스 전체를 죽이고,
macOS에 coreutils의 `timeout`이 없어(이번 세션에서 `which timeout`/`which
gtimeout` 둘 다 없음을 확인했다) 상한을 코드로 직접 감시해야 하는데 그것도
자식 프로세스가 있어야 성립한다.

이 파일은 `src/analogcoder/`를 import하지 않는다 - 실행 하니스는 그 경계
밖에 있어야 한다(스펙을 읽어 팔 전환 정규식을 적용하는 것과 서브프로세스를
띄우는 것 둘 다 텍스트/OS 작업이지 라이브러리 API가 아니다).
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time

SLOT_SPEC = "benchmarks/bandgap/spec_seed_buf0_droop_45.yaml"
# 파생 사본. **`benchmarks/bandgap/` 안에 두는 이유**: 슬롯 스펙의 테스트벤치
# `netlist:` 경로가 스펙 파일 기준 상대 경로라, 다른 디렉터리에 사본을 쓰면
# `load_spec`이 넷리스트를 못 찾는다. `.gitignore`에 이 경로를 추가해 커밋되지
# 않게 한다.
OFF_COPY_PATH = "benchmarks/bandgap/.reduction45_off.yaml"

# `corner_reduction:` 블록의 `enabled: true` 한 줄만 바꾼다. `re.subn`으로
# 치환 횟수를 세는 이유는 CLAUDE.md의 규칙이다: "re.sub / str.replace는 침묵
# 속에서 실패한다. 이 저장소가 요구하는 재작성은 반드시 개수를 세고(re.subn)
# 그 결과를 기록해야 한다."
ENABLED_TRUE_RE = re.compile(r"^(\s*enabled:\s*)true\s*$", re.MULTILINE)

RUN_ROOT = "runs/reduction45"
INVOCATIONS_PATH = os.path.join(RUN_ROOT, "invocations.jsonl")

# 사전 등록: "상한: 실행 하나가 40분을 넘기면 timeout으로 기록하고 다음으로
# 간다."
CAP_SECONDS_DEFAULT = 40 * 60
# 브리프: "SIGTERM -> 5초 -> SIGKILL"
GRACE_SECONDS = 5.0
POLL_INTERVAL_S = 0.5

ANALOGCODER_BIN = os.path.join(".venv", "bin", "analogcoder")

# 실행 순서. off_1, on_1, off_2, on_2, off_3, on_3 - "한 팔을 몰아서 돌리면
# 환경 드리프트가 팔과 섞인다."
ARM_SEQUENCE = [(arm, i) for i in (1, 2, 3) for arm in ("off", "on")]


def write_off_copy(src_path: str, dst_path: str = OFF_COPY_PATH) -> str:
    """OFF 팔의 파생 사본을 쓴다. `enabled: true`가 정확히 1회 나타나야 한다.

    사전 등록/브리프의 규칙 그대로: "OFF 팔은 같은 파일의 한 필드만 다르다.
    두 번째 스펙 파일을 커밋하지 않는다. 하니스가 파생 사본을 만들되 치환이
    정확히 1회임을 확인한다." 그리고 "팔 전환은 정확히 1회 계수 치환이다 ...
    치환이 1회가 아니면 죽어라." - 슬롯 파일이 바뀌어 `enabled: true`가
    0회나 2회 이상 나타나면 이 함수는 조용히 아무 것도 안 하는 대신
    `SystemExit`으로 죽는다.
    """
    with open(src_path) as f:
        original = f.read()
    text, n = ENABLED_TRUE_RE.subn(lambda m: m.group(1) + "false", original)
    if n != 1:
        raise SystemExit(
            f"enabled 치환이 {n}회다 - 1회여야 한다. 슬롯이 바뀌었다 ({src_path})."
        )
    with open(dst_path, "w") as f:
        f.write(text)
    return dst_path


def run_with_cap(cmd: list[str], cap_s: float, *, cwd: str | None = None) -> dict:
    """자식을 백그라운드로 띄우고 경과를 재다가 상한을 넘기면 죽이는 감시견.

    브리프: "macOS 에 coreutils 의 timeout 이 없다(이번 세션에서 확인했다 -
    which timeout 도 gtimeout 도 없다). 상한 40분은 감시견을 직접 써야 한다:
    자식을 백그라운드로 띄우고 경과를 재다가 SIGTERM -> 5초 -> SIGKILL 한다."

    자식을 새 프로세스 그룹으로 띄운다(`start_new_session=True`) - `analogcoder`가
    내부에서 ngspice를 서브프로세스로 또 띄우므로, 자식 하나만 죽이면 손자
    프로세스가 남을 수 있다. 신호는 그룹 전체(`os.killpg`)로 보낸다.

    반환: `{"exit": int | None, "killed_by_cap": bool, "elapsed_s": float}`.
    `exit`은 강제 종료된 경우에도 `proc.wait()`가 돌려주는 값을 그대로
    싣는다(음수 = 신호로 죽음) - 죽였다는 사실 자체는 `killed_by_cap`이 말한다.
    """
    start = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=cwd, start_new_session=True)
    killed = False
    try:
        while True:
            ret = proc.poll()
            if ret is not None:
                break
            elapsed = time.monotonic() - start
            if elapsed >= cap_s:
                killed = True
                _terminate_then_kill(proc)
                break
            time.sleep(min(POLL_INTERVAL_S, max(cap_s - elapsed, 0.01)))
    finally:
        exit_code = proc.wait()
    elapsed_s = time.monotonic() - start
    return {"exit": exit_code, "killed_by_cap": killed, "elapsed_s": elapsed_s}


def _terminate_then_kill(proc: subprocess.Popen) -> None:
    """SIGTERM -> 최대 `GRACE_SECONDS` 대기 -> 여전히 살아있으면 SIGKILL.

    둘 다 프로세스 그룹 전체로 보낸다. `os.getpgid`도 `killpg`와 같은 방식으로
    감싼다 - `poll()` 직후의 좁은 경합 창에서 자식이 방금 끝났으면
    `ProcessLookupError`가 여기서도 날 수 있고, 감싸지 않으면 감시견 자신이
    죽어 남은 런이 통째로 실행되지 않는다(코드 리뷰 M1)."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + GRACE_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def append_jsonl(path: str, record: dict) -> None:
    """런 하나가 끝날 때마다 결과를 디스크에 append 한다.

    브리프: "런 하나가 끝날 때마다 결과를 디스크에 append 한다. 중간에 죽어도
    끝난 것은 남아야 한다." - 그래서 6런을 다 모아 한 번에 쓰지 않고, 매 런
    직후 한 줄씩 연다/쓴다/닫는다."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def run_one(
    arm: str,
    index: int,
    spec_path: str,
    *,
    run_root: str = RUN_ROOT,
    cap_s: float = CAP_SECONDS_DEFAULT,
    analogcoder_bin: str = ANALOGCODER_BIN,
) -> dict:
    """런 하나: `<run_root>/<arm>_<index>`에 `analogcoder`를 서브프로세스로
    띄우고 상한 안에서 감시한다. `invocations.jsonl` 한 줄의 모양을 그대로
    돌려준다: `{arm, index, spec, exit, killed_by_cap, elapsed_s, run_dir}`
    (브리프 Step 2)."""
    run_dir = os.path.join(run_root, f"{arm}_{index}")
    cmd = [analogcoder_bin, "--spec", spec_path, "--run-dir", run_dir]
    outcome = run_with_cap(cmd, cap_s)
    return {
        "arm": arm,
        "index": index,
        "spec": spec_path,
        "exit": outcome["exit"],
        "killed_by_cap": outcome["killed_by_cap"],
        "elapsed_s": outcome["elapsed_s"],
        "run_dir": run_dir,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--slot", default=SLOT_SPEC, help="ON 팔이 그대로 쓰는 슬롯 스펙")
    parser.add_argument("--run-root", default=RUN_ROOT)
    parser.add_argument("--invocations", default=INVOCATIONS_PATH)
    parser.add_argument(
        "--cap-s", type=float, default=CAP_SECONDS_DEFAULT,
        help="런 하나의 상한(초). 기본 40분 - 사전 등록값.",
    )
    parser.add_argument(
        "--analogcoder-bin", default=ANALOGCODER_BIN,
        help="서브프로세스로 띄울 analogcoder 바이너리",
    )
    parser.add_argument(
        "--off-copy", default=OFF_COPY_PATH,
        help="OFF 팔 파생 사본 경로(benchmarks/bandgap/ 안에 있어야 넷리스트 상대 경로가 풀린다)",
    )
    args = parser.parse_args(argv)

    off_spec_path = write_off_copy(args.slot, args.off_copy)
    try:
        for arm, index in ARM_SEQUENCE:
            spec_path = off_spec_path if arm == "off" else args.slot
            record = run_one(
                arm, index, spec_path,
                run_root=args.run_root,
                cap_s=args.cap_s,
                analogcoder_bin=args.analogcoder_bin,
            )
            append_jsonl(args.invocations, record)
            print(
                f"{arm}_{index}: exit={record['exit']} "
                f"killed_by_cap={record['killed_by_cap']} "
                f"elapsed_s={record['elapsed_s']:.1f}",
                file=sys.stderr,
            )
    finally:
        # 브리프: "실행이 끝나면 finally 로 지운다."
        if os.path.exists(args.off_copy):
            os.remove(args.off_copy)
    return 0


if __name__ == "__main__":
    sys.exit(main())
