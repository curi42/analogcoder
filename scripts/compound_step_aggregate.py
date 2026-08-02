"""9런 산출물을 읽어 사전 등록의 판정 규칙을 그대로 적용한다.

규칙은 여기서 다시 정하지 않는다 - `2026-08-02-compound-step-search-design.md`
가 정한 것을 코드로 옮긴 것뿐이고, 옮긴 문장을 각 함수의 독스트링에 적어 둔다.

이 스크립트는 판정을 **계산**만 한다. 결과를 보고 규칙을 고치는 일은 없다.
"""

import json
import pathlib
import sys

OUT = pathlib.Path("runs/search_ab/compound_step")
SLOTS = {
    "A": "benchmarks/bandgap/spec_pvt.yaml",
    "B": "benchmarks/bandgap/spec_corner_reduction.yaml",
    "C": "benchmarks/two_stage_opamp/spec_search_slot.yaml",
}
# 사전 등록: "런 하나가 15분을 넘기면 timeout 으로 기록하고 다음으로 간다."
RUN_CAP_S = 900.0
# 사전 등록: "적어도 한 슬롯에서 면적 절감률을 1.0 %p 이상 더 낸다."
EFFECT_PP = 1.0


def reduction_pct(rec):
    """확인된 면적으로 절감률을 낸다.

    코너 경로이므로 착지 덱은 확인 스윕이 통과시킨 버전이다 -
    `objective_confirmed` 가 그 덱의 면적이고, `area_after` 와 같아야 한다.
    둘이 다르면 그것 자체가 사실이므로 그대로 드러낸다.
    """
    before = rec["area_before"]
    after = rec["area_after"]
    confirmed = rec.get("objective_confirmed")
    return {
        "area_before": before,
        "area_after": after,
        "objective_confirmed": confirmed,
        "after_equals_confirmed": (confirmed is None or abs(after - confirmed) <= 0),
        "reduction_pct": (before - after) / before * 100.0 if before else None,
    }


def load():
    rows = []
    for inv in (OUT / "invocations.jsonl").read_text().splitlines():
        meta = json.loads(inv)
        cmp_path = OUT / meta["name"] / "comparison.json"
        if not cmp_path.exists():
            rows.append({**meta, "row_status": "no_comparison_json"})
            continue
        d = json.loads(cmp_path.read_text())
        elig = d.get("eligibility_check")
        for side in ("a", "b"):
            blob = d.get(side)
            if not isinstance(blob, dict) or "record" not in blob:
                rows.append({**meta, "side": side, "row_status": "no_record",
                             "eligibility": elig})
                continue
            rec = blob["record"]
            wall = blob.get("meta", {}).get("wall_clock_s")
            rows.append({
                "invocation": meta["name"],
                "slot": meta["slot"],
                "side": side,
                "strategy": d["strategies"][0 if side == "a" else 1],
                "partners": 0 if side == "a" else meta["partners"],
                "row_status": "ok",
                "wall_clock_s": wall,
                # 사전 등록의 상한은 시간에만 의존하므로 사후 라벨로 적용해도
                # 편향이 없다. 죽여서 데이터를 버리는 대신 라벨을 붙인다.
                "over_run_cap": (wall is not None and wall > RUN_CAP_S),
                # 기록의 실제 키다. `steps_accepted_nominal` 과 `steps_survived`
                # 는 다른 사실이다 - 앞의 것은 탐색이 받아들인 수, 뒤의 것은
                # 확인 스윕(과 실패 시 이분 탐색)을 지나 살아남은 수다.
                "steps_accepted_nominal": rec.get("steps_accepted_nominal"),
                "steps_survived": rec.get("steps_survived"),
                "steps_rejected": rec.get("steps_rejected"),
                "record_status": rec.get("status"),
                "step_budget": rec.get("step_budget"),
                "compound_steps_accepted": rec.get("compound_steps_accepted"),
                "corner_confirmed": rec.get("corner_confirmed"),
                "corner_failure": rec.get("corner_failure"),
                "guard_infeasible": rec.get("guard_infeasible"),
                "failure": rec.get("failure"),
                "outcome": rec.get("outcome"),
                "simulations": rec.get("simulations"),
                "final_deck_sha256": rec.get("final_deck_sha256"),
                "eligibility": elig,
                **reduction_pct(rec),
            })
    return rows


def control_determinism(rows):
    """대조군은 슬롯당 두 번 돈다. 면적 단계는 결정론적이므로 같아야 한다.

    같지 않으면 결정론 전제가 깨진 것이고, 그때는 실행 하나로 판정한다는
    사전 등록의 근거 자체가 사라진다. 그래서 판정 전에 먼저 본다.
    """
    out = {}
    for slot in SLOTS:
        ctl = [r for r in rows if r.get("slot") == slot and r.get("side") == "a"
               and r.get("row_status") == "ok"]
        if len(ctl) < 2:
            out[slot] = {"checked": False,
                         "reason": f"대조군 기록 {len(ctl)}건 - 대조할 짝이 없다"}
            continue
        decks = [json.dumps(c["final_deck_sha256"], sort_keys=True) for c in ctl]
        reds = [c["reduction_pct"] for c in ctl]
        out[slot] = {
            "checked": True,
            "decks_identical": len(set(decks)) == 1,
            "reductions": reds,
            "reductions_identical": len(set(reds)) == 1,
        }
    return out


def verdict(rows, contaminated_slots=()):
    """사전 등록의 판정 규칙 그대로.

    선행 조건: 어떤 슬롯에서도 조합 스텝이 한 번도 수락되지 않으면 그 슬롯은
    `void`. 세 슬롯 전부 `void` 면 측정 전체가 `void` 이고 채택도 기각도 하지
    않는다.

    채택 := 어떤 partners > 0 이, 모든 슬롯에서 partners = 0 보다 나쁘지 않고,
    적어도 한 슬롯에서 절감률을 1.0 %p 이상 더 낸다.
    `timeout` 은 "모든 슬롯에서 나쁘지 않다" 를 만족시키지 못한다.
    """
    slots = {}
    for slot in SLOTS:
        srows = [r for r in rows if r.get("slot") == slot and r.get("row_status") == "ok"]
        if not srows:
            slots[slot] = {"state": "no_data"}
            continue
        elig = srows[0].get("eligibility") or {}
        if elig.get("verdict") != "eligible":
            slots[slot] = {"state": elig.get("verdict") or "unknown",
                           "reason": elig.get("reason")}
            continue
        if slot in contaminated_slots:
            slots[slot] = {"state": "contaminated"}
            continue
        treat = [r for r in srows if r["partners"] > 0]
        if treat and all(r["compound_steps_accepted"] == 0 for r in treat):
            slots[slot] = {"state": "void",
                           "reason": "조합 스텝이 한 번도 수락되지 않았다"}
            continue
        slots[slot] = {"state": "usable"}

    usable = [s for s, v in slots.items() if v["state"] == "usable"]
    if not usable:
        return {"slots": slots, "verdict": "void",
                "reason": "쓸 수 있는 슬롯이 없다 - 채택도 기각도 하지 않는다"}

    ctl = {}
    for slot in usable:
        c = [r for r in rows if r["slot"] == slot and r["partners"] == 0
             and r["row_status"] == "ok"]
        ctl[slot] = c[0]["reduction_pct"] if c else None

    cand = {}
    for p in (1, 3):
        per_slot = {}
        for slot in usable:
            m = [r for r in rows if r["slot"] == slot and r["partners"] == p
                 and r["row_status"] == "ok"]
            per_slot[slot] = m[0] if m else None
        cand[p] = per_slot

    results = {}
    for p, per_slot in cand.items():
        missing = [s for s, r in per_slot.items() if r is None]
        timed_out = [s for s, r in per_slot.items() if r is not None and r["over_run_cap"]]
        deltas = {s: (r["reduction_pct"] - ctl[s]) if r is not None else None
                  for s, r in per_slot.items()}
        not_worse = (not missing and not timed_out
                     and all(d is not None and d >= 0.0 for d in deltas.values()))
        big_enough = any(d is not None and d >= EFFECT_PP for d in deltas.values())
        results[p] = {
            "per_slot_reduction": {s: (r["reduction_pct"] if r else None)
                                   for s, r in per_slot.items()},
            "control_reduction": ctl,
            "delta_pp": deltas,
            "missing_slots": missing,
            "timed_out_slots": timed_out,
            "not_worse_everywhere": not_worse,
            "at_least_one_slot_ge_1pp": big_enough,
            "adopt": bool(not_worse and big_enough),
        }

    winners = [p for p, v in results.items() if v["adopt"]]
    if not winners:
        final = {"verdict": "REJECT",
                 "reason": "어떤 partners>0 도 '모든 슬롯에서 나쁘지 않고 한 슬롯에서 1.0%p 이상' 을 만족하지 못했다"}
    else:
        # 동률이면 모든 슬롯 절감률의 최솟값이 큰 것, 그래도 동률이면 작은 partners.
        def key(p):
            vals = [v for v in results[p]["per_slot_reduction"].values() if v is not None]
            return (min(vals), -p)
        best = max(winners, key=key)
        final = {"verdict": "ADOPT", "partners": best}
    single_deck = len({("bandgap" if SLOTS[s].startswith("benchmarks/bandgap") else "two_stage_opamp")
                       for s in usable}) == 1
    return {"slots": slots, "candidates": results, **final,
            "single_deck": single_deck}


if __name__ == "__main__":
    rows = load()
    contaminated = tuple(sys.argv[1:])
    out = {
        "rows": rows,
        "control_determinism": control_determinism(rows),
        "verdict": verdict(rows, contaminated),
        "contaminated_slots_supplied": list(contaminated),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
