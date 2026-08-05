"""대안 정렬 A/B 의 **선행 확인** — 슬롯이 다른 답을 낼 수 있는가.

"통과 대안이 2개 이상" 분기는 이 측정의 판정을 좌우한다(발화 0이면 튜너
단계의 면적 정렬을 되돌린다는 것이 사전 등록된 결론이다). 그 분기는 **서로
다른 두 변경이 각각 덱을 통과시킬 수 있을 때만** 발화한다. 정답 노브가 하나인
슬롯에서는 구조적으로 0이고, 그러면 "발화 0"이 기법의 성질이 아니라 슬롯의
성질이 된다 - `spec_seed_*` 를 "오직 `BUF_P.Xcl` 만 고친다"고 기록해 둔 것이
정확히 그 상태다.

그래서 슬롯을 **고르는 시점에** 확인한다: 단일 노브를 쓸어 각각 7 기준을
전부 통과시키는 노브가 몇 개인가. LLM 이 필요 없는 결정론 측정이다.

읽는 법을 미리 적어 둔다:

- **통과시키는 노브가 0개면 그 슬롯은 어느 팔도 PASS 하지 못한다.** 면적
  단계는 루프가 PASS 한 뒤에만 돌므로(`cli.py`) 착지 면적 지표가 통째로
  무효가 된다.
- **1개면 "통과 대안 >= 2" 분기가 구조적으로 발화할 수 없다.** 그 슬롯으로
  측정하면 발화 0 이 나오는데, 그것은 기법에 대한 사실이 아니다.
- **2개 이상이어야 이 측정이 다른 답을 낼 수 있다.**

단일 노브 스윕이 놓치는 것도 적어 둔다: 이 저장소는 **한 노브 스윕이 시도하지
않은 조합에서 승리한 사례를 두 번** 찾았다. 그러므로 "0개"는 "이 슬롯은
해결 불가"가 아니라 "단일 노브로는 안 된다"이다.
"""

import json
import pathlib
import sys

from analogcoder.cli import screen_simulate
from analogcoder.judge_tools import evaluate_criteria
from analogcoder.netlist import apply_changes, resolve_includes
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.simulators.parallel import map_points
from analogcoder.spec import load_spec
from analogcoder.structure import derive_structure

# 기하는 배수로, 개수는 가감으로 쓴다 - `optimizer._next_value` 와 같은 종류의
# 움직임이되 훨씬 넓게 본다. 선행 확인은 탐색이 아니라 **가능성**을 묻는다.
FACTORS = (0.25, 0.5, 0.75, 1.5, 2.0, 3.0, 5.0)


def main(spec_path: str) -> None:
    spec = load_spec(spec_path)
    base_dir = str(pathlib.Path(spec.canonical.netlist_path).parent.resolve())
    texts = {
        tb.name: resolve_includes(pathlib.Path(tb.netlist_path).read_text(), base_dir)
        for tb in spec.testbenches
    }
    backend = NgspiceBackend()

    baseline = screen_simulate(texts, spec, backend)
    base_verdict = evaluate_criteria(baseline["measurements"], spec.all_criteria)
    print(f"기준선 통과: {base_verdict['overall_pass']}")
    if base_verdict["overall_pass"]:
        raise SystemExit(
            "기준선이 이미 통과한다 - 튜닝할 것이 없으므로 이 슬롯은 대안 정렬을 잴 수 없다"
        )
    failing = [c["name"] for c in base_verdict["criteria"] if not c["pass"]]
    print(f"실패 기준: {failing}")

    structure = derive_structure(texts[spec.canonical.name], spec.circuit_name)
    knobs = list(structure.tunable)
    print(f"튜닝 가능한 노브: {len(knobs)}")

    # (노브, 배수) 점을 전부 만든다. 값을 못 읽는 노브는 **건너뛰되 센다** -
    # 0으로 두면 "시도했는데 안 됐다"와 구별되지 않는다.
    points, unreadable = [], []
    for entry in knobs:
        current = _current_value(texts[spec.canonical.name], entry)
        if current is None:
            unreadable.append(f"{entry.refdes}.{entry.param}")
            continue
        for factor in FACTORS:
            new = current * factor
            if entry.param in ("m", "nf"):
                new = round(new)
                if new < 1:
                    continue
            points.append(((entry.refdes, entry.param, factor), (entry, current, new)))

    print(f"값을 못 읽어 건너뛴 노브: {len(unreadable)}")
    print(f"시뮬레이션할 점: {len(points)}")

    def _run(payload):
        entry, old, new = payload
        change = [{
            "refdes": entry.refdes, "param": entry.param,
            "old_value": _fmt(old), "new_value": _fmt(new),
        }]
        try:
            moved = {n: apply_changes(t, change) for n, t in texts.items()}
        except ValueError as exc:
            return {"error": f"apply: {exc}"}
        try:
            sim = screen_simulate(moved, spec, backend)
        except Exception as exc:  # noqa: BLE001 - 한 점의 실패가 스윕을 끝내면 안 된다
            return {"error": f"sim: {type(exc).__name__}"}
        verdict = evaluate_criteria(sim["measurements"], spec.all_criteria)
        return {
            "status": sim["status"],
            "overall_pass": verdict["overall_pass"],
            "new_value": _fmt(new),
        }

    results = map_points(_run, points, None)

    passing_knobs: dict[str, list] = {}
    errors = 0
    for key, _payload in points:
        row = results[key]
        if row.get("error"):
            errors += 1
            continue
        if row["overall_pass"]:
            passing_knobs.setdefault(f"{key[0]}.{key[1]}", []).append(row["new_value"])

    print(f"\n오류로 못 잰 점: {errors}")
    print(f"**단독으로 통과시키는 노브: {len(passing_knobs)}개**")
    for name, values in sorted(passing_knobs.items()):
        print(f"  {name}: {values}")

    verdict = (
        "eligible" if len(passing_knobs) >= 2
        else "single_knob" if len(passing_knobs) == 1
        else "no_single_knob_solution"
    )
    print(f"\n선행 확인 판정: {verdict}")
    out = pathlib.Path("runs/alternatives_slot_precondition.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "spec": spec_path,
        "failing_criteria": failing,
        "knobs_total": len(knobs),
        "knobs_unreadable": unreadable,
        "points": len(points),
        "errors": errors,
        "passing_knobs": passing_knobs,
        "verdict": verdict,
    }, ensure_ascii=False, indent=1))
    print(f"-> {out}")


def _current_value(text: str, entry):
    from analogcoder.area_limits import index_baseline_components

    component = index_baseline_components(text).get(entry.refdes)
    if component is None:
        return None
    raw = component.value if entry.param == "value" else component.params.get(entry.param)
    if raw is None:
        return None
    from analogcoder.netlist import parse_spice_value

    try:
        return parse_spice_value(str(raw))
    except (ValueError, TypeError):
        return None


def _fmt(value: float) -> str:
    return f"{value:.6g}"


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "benchmarks/two_stage_opamp/spec.yaml")
