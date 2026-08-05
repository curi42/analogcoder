"""`verify_post` 를 대체할 후보 규칙들과, 기록된 결정으로 그것을 재생하는 코드.

**이 파일은 커밋된다.** 2차 측정(2026-08-02)의 재생 코드는 세션 스크래치패드에만
있었고 사라졌다 - 그래서 3차는 재구현부터 해야 했고, 재구현이 옛 결과를
재현하는지가 3차의 선행 조건 하나가 됐다. 같은 일이 다시 나지 않게 한다.

사전 등록: `docs/superpowers/specs/2026-08-05-verify-post-per-criterion-rules-design.md`.

**옛 47건 표본으로 P 규칙을 채점하는 것은 이 파일이 거부한다.** 그것이 2차가
"맞추기" 라고 부른 것이고, 사전 등록이 그 금지를 코드로 강제하라고 적었다.
"""

import json
import math
import os
import re

from analogcoder.curation import COMPARISON_REL_TOLERANCE
from analogcoder.judge_tools import violation_sum
from analogcoder.spec import Criterion

TOL = COMPARISON_REL_TOLERANCE

# 2차(2026-08-02)가 채점한 표본. **P 규칙을 여기 돌리지 않는다** - 이 목록은
# 재현 확인(선행 조건 R)에만 쓴다.
DESIGN_SAMPLE_RUNS = frozenset({
    "pvt_sonnet_1", "pvt_verification_1", "pvt_verification_2",
    "area_aware_validation_1", "psr_and_settling_validation_1",
    "topology_swap_claude_validation",
    "bg_buf0", "bg_buf0b", "bg_buf0c", "bg_tc", "bg_trim_pm",
    "cc20_argmax", "cc20_coverage", "perturb_argmax", "perturb_coverage",
    "vp_bgtop_1", "vp_bgtop_2", "vp_buf0_1", "vp_buf0_2",
    "vp_tc_1", "vp_tc_2", "vp_trimpm_1", "vp_trimpm_2",
    "vp_tso_1", "vp_tso_2", "vp_tsotop_1", "vp_tsotop_2",
})

# `target` 은 옛 런에서 `">=60.0 dB"`, 새 런에서 `">=60.0"` 이다 - LLM judge 를
# 제거하면서 단위가 빠졌다. 둘 다 읽어야 두 표본을 같은 코드로 잰다.
_TARGET = re.compile(r"^\s*(>=|<=|>|<|==)\s*(-?[0-9.eE+-]+)")


def _criterion(entry: dict) -> Criterion | None:
    m = _TARGET.match(str(entry.get("target", "")))
    if not m:
        return None
    try:
        threshold = float(m.group(2))
    except ValueError:
        return None
    return Criterion(name=entry["name"], measurement=entry["name"],
                     operator=m.group(1), threshold=threshold)


def _slack(entry: dict) -> float | None:
    """상대 여유. 통과 방향이 양수다. `judge_tools.relative_slack` 과 같은 식을
    쓰되 이벤트 dict 에서 연산자·임계값을 파싱해 온다."""
    criterion = _criterion(entry)
    actual = entry.get("actual")
    if criterion is None or actual is None:
        return None
    if isinstance(actual, float) and math.isnan(actual):
        return None
    scale = max(abs(criterion.threshold), abs(actual))
    if scale == 0.0:
        return 0.0
    if criterion.operator in (">=", ">"):
        return (actual - criterion.threshold) / scale
    return (criterion.threshold - actual) / scale


def _paired(before: list[dict], after: list[dict]) -> list[tuple[dict, dict]]:
    """이름으로 짝지은 기준들. 한쪽에만 있는 이름은 **버리고 센다** -
    없는 것을 0 으로 읽으면 "재지 않았다" 가 "변화 없음" 이 된다."""
    b = {e["name"]: e for e in before}
    return [(b[e["name"]], e) for e in after if e["name"] in b]


def _deltas(pairs) -> list[tuple[str, float | None, float | None]]:
    return [(pb["name"], _slack(pb), _slack(pa)) for pb, pa in pairs]


def _meaningful(before_slack: float, after_slack: float) -> int:
    """-1 = 의미있게 나쁨, +1 = 의미있게 좋음, 0 = 잡음 안.

    띠는 `TOL * max(1, |before|)` 다. `max(1, ...)` 는 여유가 0 근처인 기준에서
    띠가 0 으로 붕괴하는 것을 막는다 - 그러면 부동소수 잡음이 전부 '의미있게'가
    된다. 새 상수가 아니라 0 나눗셈을 피하는 것과 같은 종류의 방어다."""
    band = TOL * max(1.0, abs(before_slack))
    delta = after_slack - before_slack
    if delta < -band:
        return -1
    if delta > band:
        return +1
    return 0


# --- 2차의 세 규칙 (재구현) ---------------------------------------------------

def rule_D(before: list[dict], after: list[dict]) -> str:
    """pass 플립만 본다. `regressed_between` 과 같은 판정이다."""
    flipped = any(pb.get("pass") and not pa.get("pass") for pb, pa in _paired(before, after))
    return "rollback" if flipped else "keep"


def rule_G(before: list[dict], after: list[dict]) -> str:
    """정규화 위반량 + pass 플립."""
    if rule_D(before, after) == "rollback":
        return "rollback"
    pairs = _paired(before, after)
    criteria, b_meas, a_meas = [], {}, {}
    for pb, pa in pairs:
        criterion = _criterion(pb)
        if criterion is None:
            continue
        criteria.append(criterion)
        b_meas[criterion.measurement] = pb.get("actual")
        a_meas[criterion.measurement] = pa.get("actual")
    result = violation_sum(criteria, b_meas, a_meas)
    # **동률(improvement == 0)은 keep 이다.** 이 규약은 2차의 구현이 사라져
    # 복원한 것이고, 복원의 근거는 발표된 수치다: `>` 로 두면 G=42 가 나오고
    # `>=` 로 두면 41 이 나온다. 그리고 갈리는 단 한 건이 2차가 (a)
    # "결정론적으로 불가능" 으로 분류한 `pvt_verification_1` it=1 인데, 그
    # 건의 위반량 변화가 정확히 4.0000 -> 4.0000 이다. 즉 그 건이 G 의
    # 불일치로 세어졌다는 사실 자체가 동률을 keep 으로 읽었다는 증거다.
    # **추론된 규약이지 복원된 코드가 아니라는 것을 여기 적어 둔다.**
    return "keep" if result.improvement >= 0 else "rollback"


def rule_I(before: list[dict], after: list[dict]) -> str:
    """총 상대 여유가 늘었는가."""
    total_b = total_a = 0.0
    for _name, sb, sa in _deltas(_paired(before, after)):
        if sb is None or sa is None:
            continue
        total_b += sb
        total_a += sa
    # 동률은 keep. G 와 같은 규약이고 같은 근거다(`>` 면 I=40, `>=` 면 39).
    return "keep" if total_a >= total_b else "rollback"


# --- 3차의 기준별 규칙 계열 ---------------------------------------------------

def _classified(before: list[dict], after: list[dict]):
    """(이름, 방향, 적용전 통과, 적용후 통과) 목록."""
    out = []
    for pb, pa in _paired(before, after):
        sb, sa = _slack(pb), _slack(pa)
        if sb is None or sa is None:
            continue
        out.append((pb["name"], _meaningful(sb, sa), bool(pb.get("pass")), bool(pa.get("pass"))))
    return out


def rule_P1(before: list[dict], after: list[dict]) -> str:
    """기준 벡터 파레토: 나빠진 것 0개, 좋아진 것 1개 이상."""
    rows = _classified(before, after)
    if any(d < 0 for _n, d, _b, _a in rows):
        return "rollback"
    return "keep" if any(d > 0 for _n, d, _b, _a in rows) else "rollback"


def rule_P2(before: list[dict], after: list[dict]) -> str:
    """실패 기준 우선: 통과→실패 뒤집힘 0건, 실패하던 기준 중 나빠진 것 0개,
    그중 좋아진 것 1개 이상. **통과 기준의 여유 손실은 허용한다** - P1 과
    갈리는 지점이 정확히 그것이다."""
    rows = _classified(before, after)
    if any(b and not a for _n, _d, b, a in rows):
        return "rollback"
    failing = [(d) for _n, d, b, _a in rows if not b]
    if any(d < 0 for d in failing):
        return "rollback"
    return "keep" if any(d > 0 for d in failing) else "rollback"


def rule_P3(before: list[dict], after: list[dict]) -> str:
    """P1 이고, 그 개선이 **적용 전 실패하던** 기준에서 1개 이상 나온다."""
    if rule_P1(before, after) == "rollback":
        return "rollback"
    rows = _classified(before, after)
    return "keep" if any(d > 0 and not b for _n, d, b, _a in rows) else "rollback"


LEGACY_RULES = {"D": rule_D, "G": rule_G, "I": rule_I}
PER_CRITERION_RULES = {"P1": rule_P1, "P2": rule_P2, "P3": rule_P3}


# --- 표본 읽기 ----------------------------------------------------------------

def decisions(history_path: str) -> list[dict]:
    """한 런의 `verify_post` 결정들을 (적용 전 판정, 적용 후 판정)과 짝지어 낸다.

    짝은 **이벤트 순서**로 맞춘다: 각 `verify_post` 바로 앞의 `judge` 가 적용
    후이고, 그 앞의 `judge` 가 적용 전이다.

    `post_tuning` 플래그로 짝짓지 **않는다.** 토폴로지 교체 경로
    (`orchestrator.py` 의 스왑 분기)는 그 플래그 없이 시뮬레이션과 판정을
    하므로, 플래그를 요구하면 그 경로의 결정이 통째로 빠진다 - 실측으로
    `pvt_sonnet_1` iter 10 과 `vp_tsotop_1` iter 4 가 그렇게 사라졌고, 그
    둘이 2차의 47건과 이 재구현의 45건 차이 전부였다. 이벤트 순서는 두 경로
    모두에서 같은 인과 순서다.

    앞에 judge 가 둘 미만이면 그 결정은 **버리고 센다** - 지어내지 않는다."""
    events = []
    with open(history_path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    out = []
    judges: list[dict] = []
    for e in events:
        step = e.get("step")
        if step == "judge":
            judges.append(e)
            continue
        if step != "verify_post":
            continue
        if len(judges) < 2:
            out.append({"outer_iter": e.get("outer_iter"), "unpaired": True})
            continue
        out.append({
            "outer_iter": e.get("outer_iter"),
            "unpaired": False,
            "recommendation": e.get("recommendation"),
            "before": judges[-2]["criteria"],
            "after": judges[-1]["criteria"],
        })
    return out


def score(run_dirs: list[str], rules: dict, *, allow_design_sample: bool) -> dict:
    """규칙별 일치 수를 센다.

    **P 규칙을 옛 설계 표본에 돌리는 것은 거부한다.** 사전 등록이 그 금지를
    코드로 강제하라고 적었고, 그것이 이 인자의 유일한 이유다."""
    if not allow_design_sample:
        offenders = [d for d in run_dirs if os.path.basename(d.rstrip("/")) in DESIGN_SAMPLE_RUNS]
        if offenders:
            raise SystemExit(
                "사전 등록 위반: 옛 47건 설계 표본에 이 규칙을 채점하려 한다 - "
                f"{[os.path.basename(o) for o in offenders]}. "
                "그 표본은 재현 확인(선행 조건 R)에만 쓴다."
            )
    agree = {name: 0 for name in rules}
    total = unpaired = 0
    rows = []
    for run_dir in run_dirs:
        path = os.path.join(run_dir, "history.jsonl")
        if not os.path.exists(path):
            continue
        for d in decisions(path):
            if d["unpaired"]:
                unpaired += 1
                continue
            total += 1
            row = {"run": os.path.basename(run_dir.rstrip("/")),
                   "outer_iter": d["outer_iter"], "llm": d["recommendation"]}
            for name, fn in rules.items():
                verdict = fn(d["before"], d["after"])
                row[name] = verdict
                if verdict == d["recommendation"]:
                    agree[name] += 1
            rows.append(row)
    return {"total": total, "unpaired": unpaired, "agree": agree, "rows": rows}
