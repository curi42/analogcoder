"""대안 정렬 A/B 하니스.

사전 등록: `docs/superpowers/specs/2026-08-05-alternatives-benefit-design.md`
(2026-08-05 실행 0회 시점에 잠김). 이 파일은 그 문서가 정한 것을 실행만 한다 -
슬롯도 상한도 팔의 정의도 여기서 정하지 않는다.

`reduction45_ab.py`와 다른 점 하나: **두 팔이 서로 다른 코드다.** 그래서 스펙
파일을 파생시키는 대신 **두 워크트리의 서로 다른 `analogcoder` 바이너리**를
부른다.

**각 워크트리는 자기 `.venv`를 가져야 한다.** 메인 `.venv/bin/analogcoder`는
메인 `src`를 가리키는 editable 설치이므로, 워크트리를 cwd로만 바꿔 그것을
부르면 **두 팔이 조용히 같은 코드를 돈다.** 이 하니스는 실행 전에 두 바이너리가
서로 다른 `orchestrator.py`를 임포트하는지 확인하고, 같으면 시작하지 않는다.
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reduction45_ab import (  # noqa: E402  - 감시견을 그대로 재사용한다
    CAP_SECONDS_DEFAULT,
    append_jsonl,
    run_with_cap,
)

SPEC = "benchmarks/bandgap/spec_seed_buf0_droop.yaml"
SIM_WORKERS = "5"
RUNS_DIR = "runs/alternatives_ab"
INVOCATIONS = os.path.join(RUNS_DIR, "invocations.jsonl")


def _binary(root: str) -> str:
    return os.path.join(root, ".venv", "bin", "analogcoder")


def _imported_orchestrator(root: str) -> str:
    """그 워크트리의 파이썬이 실제로 임포트하는 `orchestrator.py`의 경로."""
    out = subprocess.run(
        [os.path.join(root, ".venv", "bin", "python"), "-c",
         "import analogcoder.orchestrator as o; print(o.__file__)"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def verify_arms(off_root: str, on_root: str) -> dict:
    """두 팔이 **서로 다른 코드**를 도는지 확인한다. 이것이 없으면 A/B가
    아무것도 비교하지 않으면서 6시간을 쓴다."""
    off_file = _imported_orchestrator(off_root)
    on_file = _imported_orchestrator(on_root)
    if off_file == on_file:
        raise SystemExit(
            f"두 팔이 같은 orchestrator.py 를 임포트한다: {off_file}\n"
            "워크트리마다 자기 .venv 에 editable 설치가 되어 있어야 한다."
        )
    if not os.path.abspath(off_file).startswith(os.path.abspath(off_root)):
        raise SystemExit(f"OFF 팔이 자기 워크트리 밖을 임포트한다: {off_file}")
    if not os.path.abspath(on_file).startswith(os.path.abspath(on_root)):
        raise SystemExit(f"ON 팔이 자기 워크트리 밖을 임포트한다: {on_file}")

    # 사전 등록이 요구하는 확인: 두 팔이 **같은 회로**를 본다.
    diff = subprocess.run(
        ["git", "-C", on_root, "diff", "--stat",
         _rev(off_root), "HEAD", "--", "benchmarks/", "src/analogcoder/topologies.py"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if diff:
        raise SystemExit(
            "두 팔의 회로/토폴로지가 다르다 - 같은 회로를 비교하지 못한다:\n" + diff
        )

    # ON 팔에만 있어야 하는 것들. 없으면 처치 팔이 처치가 아니다.
    probe = subprocess.run(
        [os.path.join(on_root, ".venv", "bin", "python"), "-c",
         "import analogcoder.cli as c, analogcoder.orchestrator as o;"
         "print(hasattr(c,'screen_simulate'), hasattr(o,'_alternatives_event'))"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if probe != "True True":
        raise SystemExit(f"ON 팔에 처치가 없다: {probe}")
    return {"off_orchestrator": off_file, "on_orchestrator": on_file}


def _rev(root: str) -> str:
    return subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def _env() -> dict:
    env = dict(os.environ)
    env["ANALOGCODER_SIM_WORKERS"] = SIM_WORKERS
    return env


def run_wave(index: int, off_root: str, on_root: str, cap_s: float) -> list[dict]:
    """한 파동: 두 팔을 **동시에** 띄우고 둘 다 끝나면 돌아온다.

    동시에 띄우는 이유는 두 팔이 같은 순간의 같은 기계를 보게 하기 위해서다 -
    직렬로 교대하면 환경 드리프트가 팔과 섞인다."""
    import threading

    env = _env()
    results: dict[str, dict] = {}

    def _one(arm: str, root: str) -> None:
        run_dir = os.path.join(RUNS_DIR, f"{arm}_{index}")
        cmd = [_binary(root), "--spec", SPEC, "--run-dir", os.path.abspath(run_dir)]
        os.makedirs(run_dir, exist_ok=True)
        results[arm] = run_with_cap(
            cmd, cap_s, cwd=root, env=env,
            stdout_path=os.path.join(RUNS_DIR, f"{arm}_{index}.stdout"),
            stderr_path=os.path.join(RUNS_DIR, f"{arm}_{index}.stderr"),
        )
        results[arm].update({"arm": arm, "index": index, "run_dir": run_dir,
                             "spec": SPEC, "root": root})

    threads = [threading.Thread(target=_one, args=(a, r))
               for a, r in (("off", off_root), ("on", on_root))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    out = []
    for arm in ("off", "on"):
        append_jsonl(INVOCATIONS, results[arm])
        out.append(results[arm])
        print(f"wave {index} {arm}: exit={results[arm]['exit']} "
              f"{results[arm]['elapsed_s'] / 60:.1f}min "
              f"cap={results[arm]['killed_by_cap']}", flush=True)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--off-root", required=True)
    ap.add_argument("--on-root", default=os.getcwd())
    ap.add_argument("--cap-s", type=float, default=CAP_SECONDS_DEFAULT)
    ap.add_argument("--waves", default="1,2,3")
    args = ap.parse_args(argv)

    os.makedirs(RUNS_DIR, exist_ok=True)
    identity = verify_arms(args.off_root, args.on_root)
    print("팔 확인 통과:", json.dumps(identity, ensure_ascii=False), flush=True)

    for wave in [int(w) for w in args.waves.split(",") if w.strip()]:
        run_wave(wave, args.off_root, args.on_root, args.cap_s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
