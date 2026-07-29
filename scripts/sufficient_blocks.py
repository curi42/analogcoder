#!/usr/bin/env python3
"""태스크 6(노브 순위의 그래프 사전확률)의 **정답표**를 실측으로 만든다 - "충분
블록 집합" (T20).

LLM 없음. 전부 ngspice 시뮬레이션과 셈이다.

## 왜 이렇게 재는가

`scripts/aggregation_discriminates.py`가 2026-07-30에 확인한 것은 세 집계 규칙
(min-distance/min-rank/min+sum)이 다중 블록 실패 3건 x 거리 정의 3종 9칸
전부에서 서로 다른 블록 순위를 낸다는 사실뿐이다. 규칙은 판별 가능하지만,
**어느 순위가 옳은지 말해 줄 정답표가 없다.** 지금 있는 것은 "교란한 자리"뿐이고,
이 저장소는 그것이 "고쳐야 할 자리"와 다르다는 것을 이미 실측했다
(`cc_trim_20`: `TRIMAMP.Xcc.W` 40->20을 교란했지만 실제로 PASS시킨 것은
`TRIMAMP.XRz.l` 15->30, 다른 소자였다). 단일 블록 케이스에서는 "다른 소자,
같은 블록"이라 넘어갔지만, 다중 블록 케이스에서는 넘어갈 수 없다 - 체인이
처지면 하류 블록의 criterion이 실패하고, 그것을 상류에서 고칠 수도 하류에서
고칠 수도 있다. 어느 쪽이 실제로 되는지는 재야 안다.

기존 정답표 다섯 케이스(`tests/fixtures/task6_ground_truth.json`)가 이미 이
방식으로 만들어졌다 - `correct_knobs`가 `kind`(verified_sufficient_alone /
verified_partial_alone__sufficient_in_combination / verified_insufficient_alone)로
갈리는 **실측된 값**이다. 이 스크립트는 다중 블록 3건(`tail_both_3`,
`tail_both_2`, `tail_trim_3` - `docs/superpowers/specs/2026-07-30-multi-block-failure-cases.json`)
에 같은 규격을 준다.

## 절차 (요약 - 각 단계의 근거는 함수 docstring에)

1. `scripts/multi_block_failures.py`가 하는 것과 똑같이 교란 덱을 만든다
   (`perturbations.PERTURBATIONS[shape]`을 `netlist.apply_changes`로 다섯
   테스트벤치 덱 전부에 적용). 새로 짜지 않고 그 스크립트의 덱 구성 로직을
   `build_perturbed_texts`로 뽑아 재사용한다.
2. `pvt.run_full_pvt_sweep`로 9코너 스윕을 돌려 실패 criterion 목록이
   `docs/superpowers/specs/2026-07-30-multi-block-failure-cases.json`의
   `failing`과 일치하는지 확인한다. 불일치면 그 케이스를 여기서 멈추고 보고한다
   - 재현되지 않는 전제 위에 측정을 쌓는 것이 이 저장소가 무효 측정을 만든
   방식이다(D1의 첫 측정).
3. 정본 덱(`spec.canonical`)의 tunable 인덱스 전체(167개 (refdes,param))에
   대해, 각 노브의 **교란 덱에서의 현재 값**에 x0.5/x2/x4를 후보로 준다.
   `area_limits.tunable_range`를 재사용해 현재 값을 읽는다 - 이 함수는
   `evaluate_area_growth`가 소자를 찾는 바로 그 경로(추적된 인스턴스 파라미터
   vs 직접 토큰)를 거울처럼 따르므로, 여기서 새로 파싱 규칙을 만들지 않는다.
   반환하는 두 번째 값(에어리어 게이트가 허용하는 배수)은 버린다 - 정답표는
   "물리적으로 고칠 수 있는가"를 묻고, 게이트는 "제안을 허용할 것인가"를 묻는
   다른 질문이다(면적 게이트는 이 스크립트 어디에서도 적용하지 않는다).
   정수 노브(m/nf, `area_limits.is_count_param`)는 반올림하고 1 미만은 버린다.
   현재 값을 해소할 수 없는 노브는 건너뛰고 사유와 함께 센다.
4. **1단계 선별(nominal)**: 실패 criterion이 사는 테스트벤치들만(2개 -
   settling + amp_loops, 세 케이스 전부 동일) 렌더링 없는 덱으로 돌려, 원래
   실패하던 criterion 전부가 통과하는지 `judge_tools.evaluate_criteria`로
   본다. 비교식을 손으로 세우지 않는다 - `curation._simulate_point`를 그대로
   재사용한다(3단 큐레이션이 이미 "변경 하나 -> 여러 테스트벤치 -> 부분 실패는
   그 지점만 결측 처리"를 검증된 방식으로 하고 있다). `simulators.parallel` +
   `simulators.cache.CachingSimulator`로 병렬/캐시.
5. **2단계 확인(9코너 격자)**: 1단계를 통과한 후보만 `run_full_pvt_sweep`로
   다섯 테스트벤치 전부·9코너 전부를 돌려 확인한다. `overall_pass`가 참인
   것만 충분하다고 센다 - 1단계는 선별일 뿐 판정이 아니다(코너에서 무너지는
   것은 이 저장소가 최적화 단계에서 이미 실측한 바로 그 실패 모양이다).
6. 충분한 노브의 스코프(`SUBCKT.refdes`의 앞부분)를 모아 블록 집합으로 접는다.

## 한계 (하한 방향 - 데이터의 `limits` 키에도 반복)

- **단일 노브만 훑는다.** 이 저장소는 단일 노브 스윕이 놓친 조합에서 답을
  찾은 적이 두 번 있다(`benchmarks/bandgap`의 `Xcc`+`M6.W`,
  `TRIMAMP.XRz.l`+`Xcc.W` 없이 다른 조합). 그래서 이 스크립트가 내는 "충분
  블록 집합"은 진짜 충분 블록 집합의 **하한**이다.
- **값 격자가 세 점(x0.5/x2/x4)이다.** 이것도 하한 방향이다.
- **하한이 판정에 주는 방향**: 순위 지표(다음 태스크)는 "가장 앞선 *알려진*
  충분 블록의 순위"를 잴 것이므로, 실제로는 충분하지만 여기서 안 잡힌 블록이
  더 앞에 서 있으면 지표는 순위를 **실제보다 나쁘게** 평가한다 - 채택에
  불리한 방향이라, 이 하한을 지금 쓸 수 있는 이유다.

사용법::

    .venv/bin/python scripts/sufficient_blocks.py

산출물: `docs/superpowers/specs/2026-07-30-task6-sufficient-blocks.json`
(및 같은 이름의 `.md` 요약). 환경 변수 `SUFFICIENT_BLOCKS_OUT`으로 JSON 경로를
바꿀 수 있다(테스트/스모크용).
"""

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
sys.path.insert(0, _HERE)

from analogcoder.area_limits import index_baseline_components, is_count_param, tunable_range
from analogcoder.curation import _simulate_point
from analogcoder.judge_tools import evaluate_criteria
from analogcoder.netlist import apply_changes, resolve_includes
from analogcoder.pvt import run_full_pvt_sweep
from analogcoder.simulators.cache import CachingSimulator
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.simulators.parallel import map_points, resolve_workers
from analogcoder.spec import load_spec
from analogcoder.structure import derive_structure
from perturbations import PERTURBATIONS

SPEC_PATH = "benchmarks/bandgap/spec_corner_reduction.yaml"
MULTI_BLOCK_CASES_PATH = "docs/superpowers/specs/2026-07-30-multi-block-failure-cases.json"
MULTIPLIERS = (0.5, 2.0, 4.0)
CASES = ["tail_both_3", "tail_both_2", "tail_trim_3"]


def build_perturbed_texts(spec, spec_dir: str, shape: str) -> dict[str, str]:
    """`scripts/multi_block_failures.py`의 덱 구성과 바이트 동일한 절차 - 두
    스크립트가 각자 이 로직을 들면 "정답표가 착수조건을 확인한 바로 그 덱
    상태를 쟀는가"를 나중에 아무도 확인할 수 없다(`compose.py`가 include 규칙을
    두 번 베껴 갈라진 것과 같은 실패 모양)."""
    perturb = PERTURBATIONS[shape]
    texts = {}
    for tb in spec.testbenches:
        text = resolve_includes(open(tb.netlist_path).read(), spec_dir)
        if perturb:
            text = apply_changes(text, perturb)
        texts[tb.name] = text
    return texts


def failing_criteria(sweep: dict, criteria: list) -> list[str]:
    """판정 스윕이 실패시킨 criterion 이름. `multi_block_failures.py`의
    `failing_criteria`와 같은 정의 - `evaluate_criteria`가 이미 낸 `pass`
    플래그를 그대로 읽는다. 여기서 다시 비교식을 세우면 두 정의가 갈라진다."""
    by_name = {e["name"]: e for e in sweep.get("criteria", [])}
    return [c.name for c in criteria if not by_name.get(c.name, {}).get("pass", True)]


def candidate_values(baseline: float, is_count: bool) -> list[float]:
    """`baseline`(교란 덱에서의 현재 값)에 x0.5/x2/x4. 정수 노브는 반올림하고
    1 미만은 버리며(음수/영은 애초에 baseline<=0에서 걸러진다), 반올림 후
    같은 정수로 뭉치면 한 번만 센다 - 중복 시뮬레이션은 낭비다."""
    raw = [baseline * m for m in MULTIPLIERS]
    if not is_count:
        return raw
    out: list[float] = []
    seen: set[int] = set()
    for v in raw:
        r = round(v)
        if r < 1 or r in seen:
            continue
        seen.add(r)
        out.append(float(r))
    return out


def format_value(value: float, is_count: bool) -> str:
    return str(int(round(value))) if is_count else repr(value)


def build_candidates(canonical_text: str, circuit_name: str) -> tuple[list[dict], list[dict]]:
    """정본 덱의 tunable 인덱스 전체를 후보로 편다.

    `tunable_range`를 그대로 쓰고 그 두 번째 반환값(에어리어 게이트 허용
    배수)은 버린다 - 브리프가 요구하는 것은 "이 값을 바꿀 수 있는가"(첫 번째
    반환값)이지 "게이트가 허용하는가"가 아니다."""
    structure = derive_structure(canonical_text, circuit_name)
    components = index_baseline_components(canonical_text)

    candidates: list[dict] = []
    skipped: list[dict] = []
    for entry in structure.tunable:
        refdes, param = entry.refdes, entry.param
        component = components.get(refdes)
        if component is None:
            skipped.append({"refdes": refdes, "param": param, "reason": "component_not_indexed"})
            continue
        baseline_value, _allowed_ignored = tunable_range(component, param)
        if baseline_value is None:
            skipped.append({"refdes": refdes, "param": param, "reason": "baseline_unresolvable"})
            continue
        if baseline_value <= 0:
            skipped.append(
                {"refdes": refdes, "param": param, "reason": f"baseline_not_positive({baseline_value!r})"}
            )
            continue
        count = is_count_param(component, param)
        values = candidate_values(baseline_value, count)
        if not values:
            skipped.append(
                {"refdes": refdes, "param": param, "reason": "all_candidates_rounded_below_1(count_param)"}
            )
            continue
        for value in values:
            candidates.append(
                {
                    "refdes": refdes,
                    "param": param,
                    "baseline": baseline_value,
                    "value": value,
                    "is_count": count,
                }
            )
    return candidates, skipped


def analyse_case(shape: str, expected_failing: list[str], spec, spec_dir: str, backend, workers) -> dict:
    stats = {"sim_count": 0}
    texts = build_perturbed_texts(spec, spec_dir, shape)

    # --- 2. 재현 확인 -----------------------------------------------------
    sweep0 = run_full_pvt_sweep(texts, spec, backend, max_workers=workers)
    stats["sim_count"] += len(spec.testbenches) * 9  # 9코너 x 5테스트벤치
    reproduced_failing = failing_criteria(sweep0, spec.all_criteria)
    reproduced = sorted(reproduced_failing) == sorted(expected_failing)

    case_record = {
        "shape": shape,
        "expected_failing": expected_failing,
        "reproduced_failing": reproduced_failing,
        "reproduced": reproduced,
    }
    if not reproduced:
        case_record["stopped"] = "reproduction_mismatch"
        return case_record, stats

    failing_set = set(reproduced_failing)
    relevant_tbs = [tb for tb in spec.testbenches if any(c.name in failing_set for c in tb.criteria)]
    restricted_criteria = [c for c in spec.all_criteria if c.name in failing_set]
    case_record["relevant_testbenches"] = [tb.name for tb in relevant_tbs]

    # --- 3. 후보 -----------------------------------------------------------
    canonical_text = texts[spec.canonical.name]
    candidates, skipped = build_candidates(canonical_text, spec.circuit_name)
    case_record["n_candidates"] = len(candidates)
    case_record["n_skipped"] = len(skipped)
    case_record["skipped_knobs"] = skipped

    # --- 4. 1단계 선별(nominal, 실패 테스트벤치만) ---------------------------
    def run_stage1(change):
        measurements, count, error = _simulate_point(texts, relevant_tbs, backend, change)
        return measurements, count, error

    items = []
    for index, cand in enumerate(candidates):
        change = [
            {"refdes": cand["refdes"], "param": cand["param"], "new_value": format_value(cand["value"], cand["is_count"])}
        ]
        items.append((index, change))

    results1 = map_points(run_stage1, items, workers)
    stage1_records = []
    stage1_pass = []
    for index, cand in enumerate(candidates):
        measurements, count, error = results1[index]
        stats["sim_count"] += count
        rec = dict(cand)
        if error is not None:
            rec["stage1"] = "sim_error"
            rec["stage1_error"] = error
        else:
            evaluation = evaluate_criteria(measurements, restricted_criteria)
            rec["stage1"] = "pass" if evaluation["overall_pass"] else "fail"
            rec["stage1_measurements"] = {
                c.measurement: measurements.get(c.measurement) for c in restricted_criteria
            }
            if evaluation["overall_pass"]:
                stage1_pass.append(rec)
        stage1_records.append(rec)

    case_record["n_stage1_pass"] = len(stage1_pass)
    print(f"  [{shape}] 1단계 통과 {len(stage1_pass)} / 후보 {len(candidates)}"
          f" — 2단계 시작 (후보당 {len(spec.testbenches) * 9} 시뮬)", flush=True)

    # --- 5. 2단계 확인(9코너 전체) ------------------------------------------
    #
    # **블록 단위로 확인하고, 블록이 확정되면 그 블록은 그만둔다.**
    # 이 측정이 답해야 하는 질문은 "어느 **블록**에 고치는 노브가 있는가"이고
    # (로드맵: 정답표는 블록 단위로만 신뢰할 수 있다 - 같은 블록 안 다른 소자로
    # 고친 실측이 있다), 노브를 전부 열거하는 것이 아니다. 1단계 통과 후보를
    # 전부 확인하면 후보당 45 시뮬이므로 상한이 없다 - 실측: 수백 개가 통과해
    # 케이스 하나가 시간 단위로 갔다.
    #
    # 그래서 블록마다 라운드로빈으로 하나씩 확인하고, 하나가 확정되면 그 블록의
    # 남은 후보는 건너뛴다. 건너뛴 수를 **세어서 기록한다** - 조용한 절단은
    # "전부 봤다"로 읽힌다.
    #
    # 블록 상태는 셋이고 그 구분이 이 기록의 값이다:
    #   confirmed  하나가 코너까지 통과했다 -> 충분 블록
    #   exhausted  후보를 전부 시도했고 아무것도 통과하지 않았다
    #   capped     시도 상한에 걸렸다 -> **모른다** (없다는 뜻이 아니다)
    STAGE2_ATTEMPTS_PER_BLOCK = 12

    by_block: dict[str, list] = {}
    for rec in stage1_pass:
        by_block.setdefault(rec["refdes"].split(".")[0], []).append(rec)

    sufficient = []
    block_state = {}
    for block, recs in sorted(by_block.items()):
        tried = 0
        state = "exhausted"
        for rec in recs:
            if tried >= STAGE2_ATTEMPTS_PER_BLOCK:
                state = "capped"
                break
            tried += 1
            change = [
                {"refdes": rec["refdes"], "param": rec["param"],
                 "new_value": format_value(rec["value"], rec["is_count"])}
            ]
            cand_texts = {tb.name: apply_changes(texts[tb.name], change) for tb in spec.testbenches}
            sweep = run_full_pvt_sweep(cand_texts, spec, backend, max_workers=workers)
            stats["sim_count"] += len(spec.testbenches) * 9
            rec["stage2_overall_pass"] = sweep["overall_pass"]
            rec["stage2_still_failing"] = [e["name"] for e in sweep["criteria"] if not e["pass"]]
            if sweep["overall_pass"]:
                sufficient.append(rec)
                state = "confirmed"
                break
        block_state[block] = {
            "state": state,
            "stage1_candidates": len(recs),
            "stage2_attempts": tried,
            "skipped_after_confirm": len(recs) - tried if state == "confirmed" else 0,
            "unattempted_at_cap": len(recs) - tried if state == "capped" else 0,
        }
        print(f"    {block:<12} {state:<10} 시도 {tried}/{len(recs)}", flush=True)

    case_record["n_stage2_pass"] = len(sufficient)
    case_record["stage2_attempts_per_block_cap"] = STAGE2_ATTEMPTS_PER_BLOCK
    case_record["block_state"] = block_state

    sufficient_out = []
    for rec in sufficient:
        sufficient_out.append(
            {
                "refdes": rec["refdes"],
                "param": rec["param"],
                "before": repr(rec["baseline"]),
                "after": format_value(rec["value"], rec["is_count"]),
                "is_count": rec["is_count"],
                "evidence": {
                    "stage1_measurements": rec["stage1_measurements"],
                    "stage2_overall_pass": rec["stage2_overall_pass"],
                },
            }
        )
    case_record["sufficient_knobs"] = sufficient_out

    blocks = sorted({rec["refdes"].split(".")[0] for rec in sufficient})
    case_record["sufficient_blocks"] = blocks

    perturbed_refdes = {c["refdes"].split(".")[0] for c in PERTURBATIONS[shape]}
    case_record["perturbed_blocks"] = sorted(perturbed_refdes)
    case_record["perturbed_blocks_match_sufficient_blocks"] = perturbed_refdes.issubset(set(blocks))

    return case_record, stats


def main():
    t0 = time.time()
    spec = load_spec(SPEC_PATH)
    spec_dir = os.path.dirname(os.path.abspath(SPEC_PATH))
    with open(MULTI_BLOCK_CASES_PATH) as f:
        multi_block_cases = json.load(f)
    expected_by_shape = {row["shape"]: row["failing"] for row in multi_block_cases["rows"]}

    backend = CachingSimulator(NgspiceBackend())
    workers = resolve_workers(None)

    out = {
        "spec": SPEC_PATH,
        "multi_block_cases_source": MULTI_BLOCK_CASES_PATH,
        "multipliers": list(MULTIPLIERS),
        "workers": workers,
        "area_gate_applied": False,
        "cases": [],
        "limits": {
            "single_knob_only": (
                "각 후보는 노브 하나만 움직인다. 이 저장소는 단일 노브 스윕이 "
                "놓친 조합에서 답을 찾은 적이 두 번 있다(CLAUDE.md - Cc+M6.W, "
                "TRIMAMP.XRz.l+Xcc.W). 그래서 이 파일의 충분 블록 집합은 하한이다."
            ),
            "three_point_grid": "값 격자가 x0.5/x2/x4 세 점이다. 이것도 하한 방향이다.",
            "stage2_capped_per_block": (
                "2단계는 블록마다 최대 12개 후보만 확인하고, 하나가 확정되면 그 블록의 "
                "나머지는 건너뛴다. 이 측정이 답하는 질문이 블록 단위이기 때문이다. "
                "block_state 의 state 를 반드시 함께 읽어라 - 'capped' 는 '그 블록에 "
                "충분한 노브가 없다'가 아니라 **모른다**는 뜻이고, 'exhausted' 만이 "
                "후보를 전부 시도했다는 뜻이다."
            ),
            "direction_of_bound": (
                "순위 지표는 '가장 앞선 *알려진* 충분 블록의 순위'를 잰다. 실제로는 "
                "충분하지만 여기서 못 잡은 블록이 더 앞에 있으면 지표는 순위를 "
                "실제보다 나쁘게 평가한다 - 채택에 불리한 방향(안전한 방향)이라 "
                "이 하한을 지금 쓸 수 있다."
            ),
        },
    }

    total_sim_count = 0
    for shape in CASES:
        expected_failing = expected_by_shape[shape]
        case_record, stats = analyse_case(shape, expected_failing, spec, spec_dir, backend, workers)
        total_sim_count += stats["sim_count"]
        out["cases"].append(case_record)
        print(
            f"[{shape}] reproduced={case_record.get('reproduced')} "
            f"stage1={case_record.get('n_stage1_pass')} "
            f"stage2={case_record.get('n_stage2_pass')} "
            f"sims_so_far={total_sim_count} elapsed={time.time()-t0:.1f}s"
        )

    out["total_simulation_count"] = total_sim_count
    out["cache_stats"] = backend.stats()
    out["wall_clock_seconds"] = time.time() - t0

    dest = os.environ.get(
        "SUFFICIENT_BLOCKS_OUT", "docs/superpowers/specs/2026-07-30-task6-sufficient-blocks.json"
    )
    with open(dest, "w") as f:
        json.dump(out, f, indent=2, default=repr)
    print(f"\nwrote {dest}")
    print(f"total sims: {total_sim_count}, wall clock: {out['wall_clock_seconds']:.1f}s, cache: {out['cache_stats']}")


if __name__ == "__main__":
    main()
