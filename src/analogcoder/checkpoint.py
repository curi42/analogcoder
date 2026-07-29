"""중단된 실행을 **측정 가능한 것**으로 바꾸는 체크포인트.

편의 기능이 아니다. 실측 근거 두 개:

- `after/two_stage-2` 실행이 iteration 3에서 agent execution error로 죽어
  `iterations_used: 2`로 끝났다. 1348초가 통째로 버려졌고, D1 측정에서 그 부분
  실행이 온전한 실행과 나란히 평균에 들어갈 뻔했다.
- 온전한 실행 비용: `two_stage_opamp` 10 iteration = 6161 s(~103분), bandgap
  코너 앵커 최적화 = 1790 s. 이 규모에서 중단은 재현 비용이 시간 단위다.

**재개는 경계에서만 한다.** 세 경계뿐이고, 이터레이션 중간 재개는 범위 밖이다 -
LLM 호출을 리플레이해야 하고 그건 새 정합성 문제를 만든다. 경계 재개의 최악은
이터레이션 하나를 다시 도는 것이고, 그건 받아들일 수 있는 값이다.

1. `orchestrator.run_orchestration`의 outer iteration 시작
2. `cli`의 코너 축소 재진입 attempt 시작
3. 메인 루프 종료 -> 최적화 단계 진입

**최적화 단계는 자체 버전 스택과 이분 탐색을 갖고 있으므로 재개 시 처음부터
다시 돈다** - 경계 3에서 재개하면 메인 루프만 건너뛴다. 이 결정은 의도적이며,
"최적화 도중 재개"를 나중에 넣으려는 시도는 그 단계의 버전 스택/이분 탐색
불변식을 통째로 다시 논증해야 한다.

**파생 가능한 것은 담지 않는다**: `baseline_components`(netlist_v0에서 다시
만든다), `structure`/`paths`(넷리스트에서), `measurement_by_criterion` /
`nets_by_measurement`(spec에서). 담으면 두 소스가 어긋날 자리가 생기고, 이
저장소는 그런 어긋남으로 이미 여러 번 값을 치렀다.
"""

import hashlib
import json
import os
from dataclasses import dataclass, field, replace

from analogcoder.attempt_log import Attempt
from analogcoder.corner_selection import NOMINAL, CornerSet, _as_point
from analogcoder.json_io import restore_non_finite
from analogcoder.json_io import dump as json_dump
from analogcoder.pvt import CornerPoint, corner_fields
from analogcoder.state import RunState

# 1 -> 2: `Checkpoint`에 `corner_seed`가 늘었다(T2). 올리면 `rejection_reason`이
# 옛 버전이 쓴 체크포인트를 **거부한다** - 의도된 것이다: 옛 체크포인트는
# `corner_seed`를 아예 모르므로, 조용히 읽어 주면 재개된 실행의 seed가 다시
# null이 되어 이 수정이 고치려는 바로 그 침묵이 재개된 실행마다 재현된다.
# 진행 중이던 실행은 처음부터 다시 돌아야 한다 - "재개는 최적화이지 정확성이
# 아니다"라는 이 파일의 기존 규칙 그대로다.
#
# 2 -> 3: `Checkpoint`에 `last_judged_corners`가 늘었다(C1, 전체-브랜치 리뷰).
# **정확히 같은 이유로 올린다.** `corner_sim.CornerState.last_judged_corners`는
# 판정자가 실제로 본 코너 집합의 승격 이전 스냅샷이고, cli.py의 재진입 분기가
# 그것으로 (a) 경로 불일치 / (b) 탐침 승격 재진입 / (c) 판단 근거 없음을 가른다.
# 옛 체크포인트는 이 필드를 몰라서 조용히 읽으면 재개된 실행은 항상
# `judged=None`으로 시작하고, 재개 경계가 BOUNDARY_OPTIMIZATION이면
# run_orchestration 전체가 건너뛰어져 스냅샷이 다시 찍힐 기회조차 없다 -
# T10이 고친 거짓 `corner_path_disagreement` FAIL이 재개 실행에서만 되살아나는
# 자리다. 조용히 읽어 주는 대신 재개를 거부해 처음부터 다시 돈다.
#
# 3 -> 4: `Checkpoint`에 `promotion_reentries`가 늘었다(T19, M10). 같은 이유를
# 세 번째로 반복한다: cli.py는 탐침 승격 재진입마다 `grown_labels`에 빈
# 리스트를 밀어 `len(grown) == attempts` 불변식을 지키는데, 그 사실 자체
# ("이 attempt는 성장이 아니라 승격 재진입이었다")는 `grown_labels`에서
# 되읽을 수 없다 - 빈 리스트는 "아무 근거 없이 재진입했다"로도 읽힌다.
# 옛 체크포인트는 이 필드를 몰라서 조용히 읽으면 재개된 실행의
# `result["corner_reduction"]["promotion_reentries"]`가 재개 이전 attempt의
# 승격 재진입 기록을 잃는다 - T2(`corner_seed`)와 C1(`last_judged_corners`)이
# 정확히 같은 모양으로 두 번 값을 치른 자리이므로, 세 번째로 조용히 읽어
# 주는 대신 재개를 거부해 처음부터 다시 돈다.
CHECKPOINT_SCHEMA_VERSION = 4
CHECKPOINT_FILENAME = "checkpoint.json"

BOUNDARY_OUTER_ITERATION = "outer_iteration"
BOUNDARY_ATTEMPT = "attempt"
BOUNDARY_OPTIMIZATION = "optimization"
BOUNDARIES = (BOUNDARY_OUTER_ITERATION, BOUNDARY_ATTEMPT, BOUNDARY_OPTIMIZATION)

# 체크포인트에 적힌 시뮬레이터와 지금 재개하려는 시뮬레이터의 관계. 네 값이
# 서로 다른 **사실**이고, 그래서 이름을 갖는다 - `match` 와 `unrecorded` 가
# 로그에서 같아 보이면 "엔진이 같아서 통과" 와 "검사가 인자 없이 불려서 통과"
# 를 사후에 구별할 수 없다.
SIMULATOR_MATCH = "match"
SIMULATOR_MISMATCH = "mismatch"
SIMULATOR_UNRECORDED = "unrecorded"  # 체크포인트를 쓴 쪽이 안 남겼다
SIMULATOR_UNSUPPLIED = "unsupplied"  # 재개하는 쪽이 안 넘겼다


class CheckpointRejected(Exception):
    """재개를 거부한다 - 크래시가 아니라 무엇이 왜 어긋났는지 말하는 오류.

    추측하지 않는다. 스펙이 바뀌었는데 중간부터 이으면 두 회로/두 기준의
    측정이 한 결과에 섞인다 - 이 저장소가 `push_netlist_version`을 원자적으로
    만든 것과 정확히 같은 이유다.
    """


@dataclass
class LoopProgress:
    """`run_orchestration`이 outer iteration 경계에서 반송하는 상태 **전부**.

    `entry_netlist_paths`가 여기 있는 이유: 면적 게이트의 기준선은 이 호출이
    **받은** 덱에서 잡힌다(`index_baseline_components(initial_netlist_texts)`).
    재진입한 attempt는 원본이 아니라 직전 attempt가 끝낸 덱을 받으므로,
    재개할 때 원본 파일을 다시 읽으면 기준선이 조용히 달라진다.
    """

    outer_iter: int
    entry_netlist_paths: dict[str, str]
    tried_topologies: set[tuple[str, str]] = field(default_factory=set)
    consecutive_rollbacks: int = 0
    tuning_history: list[Attempt] = field(default_factory=list)
    topology_swaps: list[dict] = field(default_factory=list)
    judge_result: dict = field(default_factory=dict)


@dataclass
class Checkpoint:
    boundary: str
    spec_path: str
    spec_sha256: str
    netlist_sha256: dict[str, str]
    testbench_names: list[str]
    netlist_versions: dict[str, list[str]]
    # 이 시점의 history.jsonl 줄 수. 재개할 때 이 값부터 파일 끝까지가
    # **버려진 이터레이션의 부분 이벤트**이며, resume 이벤트가 그 범위를
    # 선언한다. 로그는 자르지 않는다 - 증거를 파괴하는 것은 답이 아니다.
    history_lines: int
    attempt: int = 0
    all_topology_swaps: list[dict] = field(default_factory=list)
    corner_set: CornerSet | None = None
    # 시도별로 코너 집합에 더해진 코너 이름들. `corner_set`에서 파생되지
    # **않는다** - 집합은 합쳐진 결과만 들고 있고, 어느 시도가 무엇을 더했는지는
    # 거기서 되읽을 수 없다. 재개한 실행의 result가 이것을 잃으면 리포트가
    # "성장 없음"이라고 말하면서 집합은 자라 있다.
    grown_labels: list[list[str]] = field(default_factory=list)
    # 탐침 승격 재진입의 **기록**(M10, T19) - `grown_labels`와 나란히 쌓이지만
    # 다른 사실이다. `grown_labels`의 해당 attempt 항목은 항상 빈 리스트([])다
    # (그 attempt는 코너를 하나도 더하지 않았다) - 그래서 그 사실만으로는
    # "성장할 것이 없어 재진입 안 함"과 "성장할 것은 없지만 새 코너를 판정하러
    # 재진입함"을 구별할 수 없다. 각 항목은
    # `{"attempt": int, "criteria": [...], "corners": [...]}`이고 `attempt`는
    # `corner_set_grown` 이벤트가 쓰는 것과 같은(증가 후) 번호다. **무조건**
    # 담는다(승격 재진입이 없었던 실행은 빈 리스트) - 조건부로 담으면 "승격
    # 재진입이 없었다"와 "체크포인트가 이 필드를 잊었다"가 같아진다.
    promotion_reentries: list[dict] = field(default_factory=list)
    # `corner_selection.seed_from_sweep`의 **기록**(두 번째 반환값) - "어떤 방식
    # (argmax/coverage)으로 씨앗을 뽑았는가"다. `corner_set`에서 파생되지
    # **않는다** - 집합은 결과 코너들만 들고 있고, 어느 모드가 그것을 골랐는지,
    # points_per_tb가 몇이었는지는 거기서 되읽을 수 없다. cli.py는 이것을
    # 무조건 `corner_seed` 이벤트로 로깅하고 result.json에 싣는데(argmax를
    # 골랐다"와 "기록이 사라졌다"를 구별하기 위해서), 재개된 실행이 이것을
    # 잃으면 그 구별이 정확히 되돌아간다 - 재개는 씨앗을 다시 뽑지 않으므로
    # (진입 스윕을 재사용할 뿐 다시 뽑을 스윕이 없다), 체크포인트가 이것을
    # 담지 않으면 재개된 실행에서 seed는 영원히 None이다.
    corner_seed: dict | None = None
    # `corner_sim.CornerState.last_judged_corners`의 **스냅샷**(라벨 문자열의
    # frozenset) - 판정자가 마지막으로 실제 본 코너 집합, 승격 이전. `corner_set`
    # 에서 파생되지 **않는다**: 집합은 지금 코너들만 담고, 그중 어느 것이 아직
    # 한 번도 판정된 적 없이 탐침 승격으로 들어왔는지는 거기서 되읽을 수 없다.
    # cli.py의 재진입 분기는 이 구별로 (a) 경로 불일치(재시도 안 함)와 (b) 탐침
    # 승격 재진입(재시도함)을 가른다 - 잃으면 (b)가 죽어 전부 (a)로 접히고,
    # T10이 고친 거짓 FAIL이 되돌아온다(C1, 전체-브랜치 리뷰가 종단으로 재현).
    last_judged_corners: "frozenset[str] | None" = None
    progress: LoopProgress | None = None
    orchestration_result: dict | None = None
    # `SimulatorBackend.identity()` - 이 실행이 무엇으로 시뮬레이션했는가.
    #
    # 바로 아래 층은 이것을 이미 결정 요인으로 쓴다: `cache.simulation_key` 가
    # `identity()` 를 **네 번째** 축으로 넣고, 그 근거가 "시뮬레이터가 다르면
    # 다른 값이 나올 수 있으므로 키에서 빠지면 캐시가 다른 엔진의 측정값을 이
    # 엔진의 값으로 돌려준다" 이다. 위층에는 그것이 없었는데, 재개는 진입
    # 코너 스윕(45코너 286 s)을 **다시 돌지 않고 재사용하고** 그 값이
    # `corner_allowances`(최적화 가드밴드)와 `seed_from_sweep`(코너 축소
    # 시드)으로 흘러간다. 한 층에서 지켜지는 결정 요인이 바로 위 층에서
    # 빠져 있었다.
    #
    # None 은 "기록되지 않음" 이지 "엔진이 없음" 이 아니다 - 두 상태의 차이가
    # `simulator_identity_state` 로 이름을 갖는다.
    simulator_identity: str | None = None
    schema_version: int = CHECKPOINT_SCHEMA_VERSION


# ---------------------------------------------------------------- 해시


def file_digest(path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def spec_fingerprint(spec_path, spec) -> tuple[str, dict[str, str], list[str]]:
    """스펙 파일 해시, 테스트벤치별 **원본 파일** 해시, 테스트벤치 이름.

    해시는 `resolve_includes`를 거치기 **전** 파일 바이트에서 잡는다. 거친
    뒤 텍스트는 `.include`가 절대경로로 바뀌어 있어서 cwd가 다른 자리에서
    재개하면 같은 덱인데도 달라진다 - 그것은 회로가 바뀐 것이 아니므로
    거부 사유가 되어서는 안 된다.
    """
    return (
        file_digest(spec_path),
        {tb.name: file_digest(tb.netlist_path) for tb in spec.testbenches},
        [tb.name for tb in spec.testbenches],
    )


def build_checkpoint(
    *,
    boundary: str,
    spec_path: str,
    spec,
    netlist_versions: dict[str, list[str]],
    history_lines: int,
    attempt: int = 0,
    all_topology_swaps: list[dict] | None = None,
    corner_set: CornerSet | None = None,
    grown_labels: list[list[str]] | None = None,
    promotion_reentries: list[dict] | None = None,
    corner_seed: dict | None = None,
    last_judged_corners: "frozenset[str] | None" = None,
    progress: LoopProgress | None = None,
    orchestration_result: dict | None = None,
    simulator_identity: str | None = None,
) -> Checkpoint:
    spec_sha, netlist_sha, names = spec_fingerprint(spec_path, spec)
    return Checkpoint(
        boundary=boundary,
        spec_path=os.path.abspath(spec_path),
        spec_sha256=spec_sha,
        netlist_sha256=netlist_sha,
        testbench_names=list(names),
        netlist_versions={name: list(paths) for name, paths in netlist_versions.items()},
        history_lines=history_lines,
        attempt=attempt,
        all_topology_swaps=[dict(s) for s in (all_topology_swaps or [])],
        corner_set=corner_set,
        grown_labels=[list(g) for g in (grown_labels or [])],
        promotion_reentries=[dict(p) for p in (promotion_reentries or [])],
        corner_seed=dict(corner_seed) if corner_seed is not None else None,
        last_judged_corners=(
            frozenset(last_judged_corners) if last_judged_corners is not None else None
        ),
        progress=progress,
        orchestration_result=orchestration_result,
        simulator_identity=simulator_identity,
    )


# ---------------------------------------------------------------- 직렬화


def _attempt_payload(attempt: Attempt) -> dict:
    return {
        "outer_iter": attempt.outer_iter,
        "retry": attempt.retry,
        "refdes": attempt.refdes,
        "param": attempt.param,
        "old_value": attempt.old_value,
        "new_value": attempt.new_value,
        "outcome": attempt.outcome,
        "reason": attempt.reason,
        "detail": attempt.detail,
        # deltas는 쌍의 튜플이다(frozen 안의 dict은 여전히 바뀌므로). JSON에는
        # 리스트의 리스트로 나가고 되돌아올 때 다시 튜플이 된다 - Attempt가
        # frozen이라 값 동등성으로 비교되며, 리스트로 되살리면 같은 시도가
        # 같지 않게 된다.
        "deltas": [[name, value] for name, value in attempt.deltas],
        "regressed": list(attempt.regressed),
    }


def _attempt_from_payload(raw: dict) -> Attempt:
    return Attempt(
        outer_iter=raw["outer_iter"],
        retry=raw["retry"],
        refdes=raw["refdes"],
        param=raw["param"],
        old_value=raw["old_value"],
        new_value=raw["new_value"],
        outcome=raw["outcome"],
        reason=raw.get("reason"),
        detail=raw.get("detail"),
        deltas=tuple((name, value) for name, value in raw.get("deltas", [])),
        regressed=tuple(raw.get("regressed", [])),
    )


def _corner_payload(point: CornerPoint | None):
    """`pvt._corner_fields`와 **같은 모양**을 쓴다 - 코너 dict를 만드는 자리가
    셋으로 갈라져 있던 것이 이 함수였다."""
    if point is NOMINAL:
        return None
    return corner_fields(point)


def _corner_from_payload(raw) -> CornerPoint | None:
    """체크포인트에 적힌 코너를 되읽는다. **모양을 모르면 인덱싱하지 않는다.**

    예전 코드는 `raw["process"]`로 바로 인덱싱해서, 라벨로 선언된 코너가 담긴
    체크포인트를 재개하면 `KeyError: 'process'`를 냈다 - 그것은
    `CheckpointRejected`가 아니라서 `cli.py`의 잡에 걸리지 않고 트레이스백이
    되며, `result.json`도 `report.md`도 안 나온다. `run_optimization` 가드가
    이미 값을 치른 모양 그대로다.

    **재개는 최적화이지 정확성이 아니다.** 알 수 없는 모양이면 체크포인트를
    버리고 처음부터 도는 것이 옳고, 그것이 `CheckpointRejected`의 계약이다."""
    if raw is None:
        return NOMINAL
    if not isinstance(raw, dict):
        raise CheckpointRejected(f"체크포인트의 코너 항목이 매핑이 아니다: {raw!r}")
    try:
        return _as_point(raw)
    except (ValueError, KeyError, TypeError) as exc:
        raise CheckpointRejected(
            f"체크포인트의 코너 항목을 읽을 수 없다: {raw!r} ({exc})"
        ) from None


def _corner_set_payload(corner_set: CornerSet | None):
    if corner_set is None:
        return None
    return {
        "corners": [_corner_payload(c) for c in corner_set.corners],
        "probe_order": [_corner_payload(c) for c in corner_set.probe_order],
        "probe_index": corner_set.probe_index,
    }


def _corner_set_from_payload(raw) -> CornerSet | None:
    if raw is None:
        return None
    # **생성자를 거친다.** CornerSet.__post_init__의 네 불변식이 이 하위
    # 프로젝트 전체가 딛고 선 기반이고, 그 docstring이 예상한 뒷문이 정확히
    # "재개된 실행을 위한 역직렬화"다. probe_order에 이미 corners 안에 있는
    # 코너가 남으면 next_probe가 그것을 또 골라 이 프로젝트가 막으려는
    # 낭비된 시뮬레이션을 만든다.
    return CornerSet(
        corners=tuple(_corner_from_payload(c) for c in raw["corners"]),
        probe_order=tuple(_corner_from_payload(c) for c in raw["probe_order"]),
        probe_index=raw.get("probe_index", 0),
    )


def _last_judged_payload(judged: "frozenset[str] | None"):
    """정렬된 리스트로 - JSON에는 frozenset이 없고, 정렬은 같은 집합이 같은
    바이트를 내게 하기 위해서다(다른 `corner_seed`/`grown_labels` 직렬화와 같은
    이유)."""
    if judged is None:
        return None
    return sorted(judged)


def _last_judged_from_payload(raw) -> "frozenset[str] | None":
    if raw is None:
        return None
    return frozenset(raw)


def _progress_payload(progress: LoopProgress | None):
    if progress is None:
        return None
    return {
        "outer_iter": progress.outer_iter,
        "entry_netlist_paths": dict(progress.entry_netlist_paths),
        # set도 tuple도 JSON에 없다 - 리스트의 리스트로 왕복시킨다. 정렬해서
        # 내보내는 것은 같은 상태가 같은 바이트를 내게 하기 위해서다.
        "tried_topologies": sorted([list(pair) for pair in progress.tried_topologies]),
        "consecutive_rollbacks": progress.consecutive_rollbacks,
        "tuning_history": [_attempt_payload(a) for a in progress.tuning_history],
        "topology_swaps": [dict(s) for s in progress.topology_swaps],
        "judge_result": progress.judge_result,
    }


def _progress_from_payload(raw) -> LoopProgress | None:
    if raw is None:
        return None
    return LoopProgress(
        outer_iter=raw["outer_iter"],
        entry_netlist_paths=dict(raw["entry_netlist_paths"]),
        tried_topologies={(pair[0], pair[1]) for pair in raw.get("tried_topologies", [])},
        consecutive_rollbacks=raw.get("consecutive_rollbacks", 0),
        tuning_history=[_attempt_from_payload(a) for a in raw.get("tuning_history", [])],
        topology_swaps=[dict(s) for s in raw.get("topology_swaps", [])],
        judge_result=raw.get("judge_result") or {},
    )


def to_payload(checkpoint: Checkpoint) -> dict:
    return {
        "schema_version": checkpoint.schema_version,
        "boundary": checkpoint.boundary,
        "spec_path": checkpoint.spec_path,
        "spec_sha256": checkpoint.spec_sha256,
        "netlist_sha256": dict(checkpoint.netlist_sha256),
        "testbench_names": list(checkpoint.testbench_names),
        "netlist_versions": {n: list(p) for n, p in checkpoint.netlist_versions.items()},
        "history_lines": checkpoint.history_lines,
        "attempt": checkpoint.attempt,
        "all_topology_swaps": [dict(s) for s in checkpoint.all_topology_swaps],
        "corner_set": _corner_set_payload(checkpoint.corner_set),
        "grown_labels": [list(g) for g in checkpoint.grown_labels],
        # **무조건 나간다** - grown_labels와 같은 규칙(T19, M10). 조건부로 쓰면
        # "승격 재진입이 없었다"(빈 리스트)와 "체크포인트가 이 필드를 잊었다"
        # (필드 부재)가 같아진다.
        "promotion_reentries": [dict(p) for p in checkpoint.promotion_reentries],
        # **무조건 나간다** - simulator_identity와 같은 규칙. 조건부로 쓰면
        # `null`("이번 회차에 씨앗을 안 뽑았다")과 "필드가 통째로 없다"가 같아져,
        # "축소가 꺼져 있었다"와 "체크포인트가 이 필드를 잊었다"를 사후에
        # 구별할 수 없다.
        "corner_seed": dict(checkpoint.corner_seed) if checkpoint.corner_seed is not None else None,
        # **무조건 나간다** - corner_seed와 같은 규칙. 조건부로 쓰면 `null`
        # ("이번 회차에 아직 아무것도 판정하지 않았다")과 필드의 부재("체크포인트가
        # 이 필드를 모른다")가 같아진다.
        "last_judged_corners": _last_judged_payload(checkpoint.last_judged_corners),
        "progress": _progress_payload(checkpoint.progress),
        "orchestration_result": checkpoint.orchestration_result,
        # **무조건 나간다.** 조건부로 쓰면 `null` 과 "필드가 통째로 없다" 가
        # 같아지고, 그러면 "기록 안 함" 과 "검사가 사라졌다" 를 구별할 수 없다.
        "simulator_identity": checkpoint.simulator_identity,
    }


def from_payload(payload: dict) -> Checkpoint:
    return Checkpoint(
        boundary=payload["boundary"],
        spec_path=payload["spec_path"],
        spec_sha256=payload["spec_sha256"],
        netlist_sha256=dict(payload.get("netlist_sha256", {})),
        testbench_names=list(payload["testbench_names"]),
        netlist_versions={n: list(p) for n, p in payload["netlist_versions"].items()},
        history_lines=payload["history_lines"],
        attempt=payload.get("attempt", 0),
        all_topology_swaps=[dict(s) for s in payload.get("all_topology_swaps", [])],
        corner_set=_corner_set_from_payload(payload.get("corner_set")),
        grown_labels=[list(g) for g in payload.get("grown_labels", [])],
        promotion_reentries=[dict(p) for p in payload.get("promotion_reentries", [])],
        corner_seed=(
            dict(payload["corner_seed"]) if payload.get("corner_seed") is not None else None
        ),
        last_judged_corners=_last_judged_from_payload(payload.get("last_judged_corners")),
        progress=_progress_from_payload(payload.get("progress")),
        orchestration_result=payload.get("orchestration_result"),
        simulator_identity=payload.get("simulator_identity"),
        schema_version=payload.get("schema_version", CHECKPOINT_SCHEMA_VERSION),
    )


# ---------------------------------------------------------------- 원자적 쓰기


def checkpoint_path(run_dir) -> str:
    return os.path.join(run_dir, CHECKPOINT_FILENAME)


def write_checkpoint(run_dir, checkpoint: Checkpoint) -> str:
    """임시 파일에 쓰고 `os.replace`로 원자 교체한다 - **협상 불가**.

    이 기능의 판정 규칙이 정확히 "임의 지점에서 강제 종료"다. 파일을 직접
    덮어쓰면 찢어진 JSON이 남을 수 있고, 그러면 재개가 크래시하거나 - 더
    나쁘게 - 부분적으로 읽힌다. 파일은 하나를 덮어쓴다(이력이 아니라 현재
    상태이므로 누적할 이유가 없다).

    실측: 이 함수를 루프에서 돌리며 서로 다른 10개 시점에 `SIGKILL`을 보냈고
    (0.15 s ~ 1.17 s), 10번 모두 `checkpoint.json`이 온전한 JSON으로 읽혔다.
    그중 6번은 `checkpoint.json.tmp`가 남아 있었다 - open 과 replace 사이에서
    죽은 것이며, **그 파일은 아무도 읽지 않고** 다음 쓰기가 `"w"`로 잘라
    덮어쓴다. 남은 `.tmp`를 보고 "체크포인트가 찢어졌다"고 읽지 마라.
    """
    path = checkpoint_path(run_dir)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            # `json_io.dump` - 정규화 먼저, `allow_nan=False` 는 그 뒤의 못.
            # `judge_result` 와 `orchestration_result["final_criteria"]` 는
            # `evaluate_criteria` 의 출력이고, 측정이 없는 기준의 `actual`/
            # `margin` 에 `math.nan` 이 실린다 - 예외가 아니라 정상 경로다.
            json_dump(to_payload(checkpoint), f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # 찢어진 임시 파일을 남기지 않는다. 실패해도 `path`는 손대지 않았으므로
        # 직전 체크포인트는 그대로다.
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return path


def read_payload(run_dir) -> dict | None:
    """읽기 경계에서 표지를 되돌린다 - `history.read_events` 와 같은 자리.

    **체크포인트는 `result.json` 과 다르다.** `result.json` 은 사람과 외부
    소비자가 읽고 끝이지만 체크포인트는 **다시 도는 런에 들어간다**:
    `judge_result` 는 재개한 루프가 그대로 쓰는 값이고
    `attempt_log.deltas_between` 은 judge 값을 뺀다. 표지 문자열을 그대로
    올려 보내면 그 뺄셈이 `TypeError` 이므로, 쓰기만 고치는 것은 결함을
    옮기는 것이지 없애는 것이 아니다.
    """
    path = checkpoint_path(run_dir)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return restore_non_finite(json.load(f))


# ---------------------------------------------------------------- 재개 거부


def simulator_identity_state(payload: dict | None, simulator_identity: str | None) -> str:
    """체크포인트의 시뮬레이터와 지금 재개하려는 시뮬레이터의 관계.

    **거부 여부와 따로 이름을 갖는 이유**: 거부는 `mismatch` 일 때만 일어나고
    나머지 셋은 전부 "통과" 인데, 그 셋이 같은 사실이 아니다. 특히
    `unrecorded`/`unsupplied` 는 **검사가 아무 일도 하지 않은 상태**이고, 그것이
    `match` 와 로그에서 같아 보이면 이 저장소가 아홉 번 값을 치른 모양이 그대로
    재현된다. 호출자는 이 값을 실행 로그에 남겨야 한다.
    """
    recorded = (payload or {}).get("simulator_identity")
    if recorded is None:
        return SIMULATOR_UNRECORDED
    if simulator_identity is None:
        return SIMULATOR_UNSUPPLIED
    return SIMULATOR_MATCH if recorded == simulator_identity else SIMULATOR_MISMATCH


def rejection_reason(
    payload: dict | None, run_dir, spec_path, spec, simulator_identity: str | None = None
) -> str | None:
    """재개하면 안 되는 이유. 없으면 None.

    추측하지 않는다 - 하나라도 어긋나면 재개하지 않고 무엇이 왜 어긋났는지
    말한다.

    `simulator_identity` 는 `SimulatorBackend.identity()` 다. 넘기지 않으면
    그 축은 판정되지 않으며(`SIMULATOR_UNSUPPLIED`), **거부하지 않는다** -
    거부하면 이 인자를 아직 안 넘기는 호출부의 체크포인트가 전부 재개 불가가
    되어 이 기능이 막으려는 것(버려진 실행 시간)을 스스로 만든다. 판정되지
    않았다는 사실은 `simulator_identity_state` 로 호출자가 읽어 로그에 남긴다.
    """
    if payload is None:
        return (
            f"{checkpoint_path(run_dir)} 가 없다 - 이 run-dir 은 체크포인트를 남긴 "
            f"실행의 것이 아니다 (체크포인트는 outer iteration 경계에서 처음 쓰인다)"
        )

    version = payload.get("schema_version")
    if version != CHECKPOINT_SCHEMA_VERSION:
        return (
            f"checkpoint schema version {version!r} != {CHECKPOINT_SCHEMA_VERSION} - "
            f"이 체크포인트는 다른 버전의 코드가 썼다"
        )

    boundary = payload.get("boundary")
    if boundary not in BOUNDARIES:
        return f"unknown checkpoint boundary {boundary!r}; expected one of {list(BOUNDARIES)}"

    spec_sha, netlist_sha, names = spec_fingerprint(spec_path, spec)

    if payload.get("spec_sha256") != spec_sha:
        return (
            f"spec 파일 내용이 체크포인트에 기록된 것과 다르다 ({spec_path}): "
            f"{payload.get('spec_sha256')} -> {spec_sha}. 스펙이 바뀐 채로 중간부터 "
            f"이으면 두 기준의 측정이 한 결과에 섞인다"
        )

    if list(payload.get("testbench_names", [])) != names:
        return (
            f"testbench_names 가 지금 스펙과 다르다: "
            f"{payload.get('testbench_names')} -> {names}"
        )

    if dict(payload.get("netlist_sha256", {})) != netlist_sha:
        changed = sorted(
            name
            for name, digest in netlist_sha.items()
            if payload.get("netlist_sha256", {}).get(name) != digest
        )
        return (
            f"netlist 파일 내용이 체크포인트에 기록된 것과 다르다 ({', '.join(changed)}) - "
            f"바뀐 회로에 중간부터 이으면 두 회로의 측정이 한 결과에 섞인다"
        )

    if simulator_identity_state(payload, simulator_identity) == SIMULATOR_MISMATCH:
        return (
            f"시뮬레이터가 체크포인트에 기록된 것과 다르다: "
            f"{payload.get('simulator_identity')!r} -> {simulator_identity!r}. "
            f"재개는 진입 코너 스윕을 다시 돌지 않고 재사용하므로, 이어 붙이면 "
            f"두 엔진의 측정이 한 결과에 섞인다"
        )

    missing = []
    for paths in payload.get("netlist_versions", {}).values():
        missing += [p for p in paths if not os.path.exists(p)]
    progress = payload.get("progress") or {}
    missing += [
        p for p in (progress.get("entry_netlist_paths") or {}).values() if not os.path.exists(p)
    ]
    if missing:
        return (
            f"체크포인트가 가리키는 넷리스트 버전 파일이 디스크에 없다: "
            f"{', '.join(sorted(set(missing)))}"
        )

    return None


def load_checkpoint(run_dir, spec_path, spec, simulator_identity: str | None = None) -> Checkpoint:
    payload = read_payload(run_dir)
    reason = rejection_reason(payload, run_dir, spec_path, spec, simulator_identity)
    if reason is not None:
        raise CheckpointRejected(reason)
    return from_payload(payload)


def restore_state(state: RunState, checkpoint: Checkpoint) -> RunState:
    """버전 스택을 되돌려 놓는다. **디스크를 훑지 않는다** - 체크포인트가 적은
    목록 그대로여야 버전 번호가 중단 없이 돈 실행과 같아진다.

    복사해서 넣는 이유: state가 이어서 append하는데 그것이 체크포인트 객체의
    리스트를 함께 바꾸면, 같은 실행 안에서 다시 쓰는 체크포인트가 이미 지나간
    상태를 담게 된다.
    """
    state.netlist_versions = {
        name: list(paths) for name, paths in checkpoint.netlist_versions.items()
    }
    return state


def snapshot_progress(progress: LoopProgress) -> LoopProgress:
    """체크포인트에 담을 **스냅샷**. 루프는 계속 같은 객체를 바꿔 나가므로
    얕게 넘기면 나중 변화가 이미 쓴 체크포인트에 소급된다 - 특히
    `topology_swaps`의 레코드는 `outcome`이 나중에 채워진다."""
    return replace(
        progress,
        entry_netlist_paths=dict(progress.entry_netlist_paths),
        tried_topologies=set(progress.tried_topologies),
        tuning_history=list(progress.tuning_history),
        topology_swaps=[dict(s) for s in progress.topology_swaps],
        judge_result=dict(progress.judge_result or {}),
    )
