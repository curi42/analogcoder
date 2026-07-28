"""새 토폴로지 항목을 라이브러리에 넣을지 판정하는 큐레이션 게이트.

`docs/superpowers/plans/2026-07-28-topology-curation.md`의 "공통 인터페이스"가
정의하는 dataclass들과, 그 파이프라인의 1단(구조 검사)·2단(특성 재현)을 담는다.
단계는 전부 **순수 함수**다 - LLM은 이 모듈 어디에도 없다(소스 C 저술과
`description` 렌더링만 LLM을 쓰고, 그것은 다른 태스크가 별도 모듈에 둔다).

이 파일이 서 있는 전제: "시뮬레이션되고 선언한 수치를 재현한다"는 라이브러리
입회의 **필요조건이지 충분조건이 아니다.** F1에서 텍스트북대로 옳은
cascode-compensation 후보가 기각된 것은 그 후보가 나빠서가 아니라, 기존
토폴로지의 노브 하나를 튜닝하는 것이 모든 축에서 그 후보를 이겼기 때문이다 -
그 판정(3단, 다른 태스크)은 이 모듈이 낳는 `addresses`(측정된 개선 기준
집합)를 입력으로 삼는다. 그래서 `addresses`는 여기서 **선언이 아니라 측정**
으로 나와야 한다: 후보가 실제로 이 시뮬레이션에서 기존 본문보다 나은 기준만
담는다.

판정은 셋이다 - `pass`/`fail`/`inconclusive` (각 단계 수준. 파이프라인
전체의 최종 판정 `ADMIT`/`REJECT`/`INCONCLUSIVE`는 이 dataclass들을 소비하는
더 상위 태스크가 낸다). "이 회로가 나쁘다"(fail)와 "재보지 못했다"
(inconclusive)는 다른 사실이고, 그 구별을 흐리는 것이 F1이 반복해 온
실수다.
"""

import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from analogcoder.area_limits import index_baseline_components, is_count_param, tunable_range
from analogcoder.judge_tools import evaluate_criteria
from analogcoder.netlist import apply_changes, apply_topology_swap
from analogcoder.spec import TargetSpec, Testbench
from analogcoder.structure import derive_structure
from analogcoder.topologies import Topology
from analogcoder.topology_match import SwapCandidate, compatible_swaps

_BETTER_WHEN_LARGER = (">=", ">")
_BETTER_WHEN_SMALLER = ("<=", "<")


@dataclass(frozen=True)
class Candidate:
    """입회 심사를 받는 토폴로지 본문 하나. `TOPOLOGY_LIBRARY`의 `Topology`와
    거의 같은 모양이지만, 아직 라이브러리에 없으므로 `description`/
    `verified_at`(둘 다 다른 단계가 검증한 뒤에야 정해진다)이 빠져 있다."""

    topology_id: str
    subckt_body: str
    ports: list[str]
    assumes_scale: float
    provenance: str  # "extracted" | "file" | "authored"


@dataclass(frozen=True)
class Slot:
    """후보가 겨루는 대상 - 어느 스펙의 어느 블록인지."""

    spec: TargetSpec
    spec_dir: Path
    block_path: str


@dataclass
class StageResult:
    name: str  # "structure" | "reproduce" | "corners" | "comparison"
    status: str  # "pass" | "fail" | "skipped" | "inconclusive"
    detail: dict  # 항상 채운다 - 통과했을 때도. "검사했고 문제없음"과
    # "검사가 사라짐"을 로그에서 구별하기 위해서다.


@dataclass
class CurationResult:
    verdict: str  # "ADMIT" | "REJECT" | "INCONCLUSIVE"
    reason: str
    stages: list[StageResult]
    addresses: list[str]
    description: str
    description_source: str  # "agent" | "template"


def _candidate_as_topology(candidate: Candidate) -> Topology:
    """`compatible_swaps`가 요구하는 `Topology` 모양으로 감싼다.

    `description`/`addresses`/`verified_at`은 `compatible_swaps`의 세 판정
    규칙(포트/모델/스케일) 중 어느 것도 읽지 않는 필드라 값 자체는 판정에
    영향을 주지 않는다 - 다만 dataclass가 frozen이라 값을 채워야 생성된다.
    실제 값은 이 단계가 아니라 큐레이션 파이프라인의 뒷단(구성/검증 단계)이
    정한다."""
    return Topology(
        id=candidate.topology_id,
        description="",
        subckt_body=candidate.subckt_body,
        addresses=[],
        ports=candidate.ports,
        assumes_scale=candidate.assumes_scale,
        provenance=candidate.provenance,
        verified_at="nominal",
    )


def check_structure(candidate: Candidate, slot: Slot, netlist_texts: dict[str, str]) -> StageResult:
    """1단: 후보가 이 슬롯의 스왑 후보로 나오는가만 본다.

    `compatible_swaps`를 후보 하나짜리 임시 라이브러리로 돌린다 - 후보는 아직
    `TOPOLOGY_LIBRARY`에 없으므로 실제 라이브러리를 건드리지 않는다. 실패
    사유는 `compatible_swaps`가 이미 낸 `SwapRejection.reason`/`.detail`을
    그대로 옮긴다 - 이 함수는 새 사유 문자열을 만들지 않는다."""
    library = {candidate.topology_id: _candidate_as_topology(candidate)}
    candidates, rejections = compatible_swaps(netlist_texts, library, set())

    wanted = SwapCandidate(block_path=slot.block_path, topology_id=candidate.topology_id)
    if wanted in candidates:
        return StageResult(
            name="structure",
            status="pass",
            detail={
                "block_path": slot.block_path,
                "topology_id": candidate.topology_id,
                "testbenches": sorted(netlist_texts),
            },
        )

    matching = [r for r in rejections if r.block_path == slot.block_path and r.topology_id == candidate.topology_id]
    # compatible_swaps는 라이브러리의 모든 (block_path, topology_id) 쌍에
    # 대해, 후보가 되지 못하면 반드시 적어도 하나의 SwapRejection을 남긴다
    # (각 테스트벤치 루프에서 실패마다 append한다) - 그래서 matching이 비어
    # 있는 경우는 이 함수의 버그이지 정상 동작이 아니다. `assert`가 아니라
    # 명시적으로 raise하는 이유는 `assert`가 `python -O` 아래서 통째로
    # 사라지기 때문이다 - 이 불변식은 최적화 플래그와 무관하게 지켜야 한다.
    if not matching:
        raise RuntimeError(
            f"compatible_swaps produced neither a candidate nor a rejection for "
            f"({slot.block_path!r}, {candidate.topology_id!r}) - this should be unreachable"
        )
    # 같은 (block, topology) 쌍이 테스트벤치마다 다른 사유로 거부될 수 있다
    # (예: tb1은 scale 불일치, tb2는 필요한 모델이 없음). rejections 리스트는
    # compatible_swaps가 `for tb in sorted(netlist_texts)` 순서로 append한
    # 것이므로, matching[0]은 "임의의 하나"가 아니라 **정렬된 테스트벤치
    # 이름 순으로 가장 먼저 실패한 테스트벤치의 사유**다 - compatible_swaps
    # 자신이 이미 확립한 순서를 그대로 따르는 것이지 이 함수가 새로 순서를
    # 매기는 것이 아니다. 전체 사유 목록은 detail["rejections"]에 남아 있어
    # 손실되지 않는다 (test_multiple_matching_rejections_reports_the_first_
    # testbench_in_sorted_order로 고정).
    first = matching[0]
    return StageResult(
        name="structure",
        status="fail",
        detail={
            "block_path": slot.block_path,
            "topology_id": candidate.topology_id,
            "reason": first.reason,
            "detail": first.detail,
            "rejections": [{"reason": r.reason, "detail": r.detail} for r in matching],
        },
    )


def _simulate_deck(netlist_texts: dict[str, str], testbenches: list[Testbench], sim_backend) -> tuple[dict, int]:
    """슬롯의 모든 테스트벤치에 대해 한 덱(후보 스왑본 또는 기존 본문 그대로)을
    시뮬레이션하고 measurement를 병합한다. `pvt.run_full_pvt_sweep`/
    `cli.py`의 `simulate_fn`과 같은 패턴 - 텍스트를 임시 파일에 써서
    `sim_backend.run(path, {"control_block": ...})`을 부른다. 여러
    테스트벤치가 같은 measurement 이름을 낸다면 나중 테스트벤치가 이긴다
    (기존 `simulate_fn`과 같은 규칙).

    `sim_backend.run`이 던지는 예외는 여기서 잡지 않는다 - 호출부
    (`reproduce_characteristics`)가 "시뮬레이터 예외는 거부가 아니라
    inconclusive"라는 규칙에 따라 잡는다."""
    measurements: dict = {}
    count = 0
    for tb in testbenches:
        text = netlist_texts[tb.name]
        with tempfile.TemporaryDirectory() as tmpdir:
            netlist_path = os.path.join(tmpdir, "candidate.cir")
            with open(netlist_path, "w") as f:
                f.write(text)
            result = sim_backend.run(netlist_path, {"control_block": tb.control_block})
        measurements.update(result.measurements)
        count += 1
    return measurements, count


def _is_better(operator: str, candidate_value: float, baseline_value: float) -> bool:
    """`operator`의 방향으로 후보 값이 기존 값보다 나은가. 동률은 나은 것이
    아니다. `==` 기준은 개선 방향이 정의되지 않으므로(맞다/틀리다이지
    "더 낫다"가 없다) 결코 개선으로 세지 않는다 - 추측하지 않는다."""
    if operator in _BETTER_WHEN_LARGER:
        return candidate_value > baseline_value
    if operator in _BETTER_WHEN_SMALLER:
        return candidate_value < baseline_value
    return False


def reproduce_characteristics(
    candidate: Candidate, slot: Slot, netlist_texts: dict[str, str], sim_backend
) -> tuple[StageResult, list[str]]:
    """2단: 후보를 스왑한 덱과 기존 본문 그대로의 덱을 각각 한 번씩
    시뮬레이션한다 (테스트벤치가 여럿이면 그 안에서 테스트벤치별로 돈다 -
    "두 번"은 덱의 개수를 센 것이지 시뮬레이션 호출 횟수가 아니다).

    요구는 스펙 전체 통과가 아니다 - 항목은 한 블록만 바꾸므로 스펙 전체를
    만족할 의무가 없다. 요구는 둘뿐이다: 후보 쪽에서 모든 기준의 measurement가
    나올 것, 그리고 시뮬레이터가 예외를 던지면 그것은 거부가 아니라
    inconclusive로 남을 것. 두 덱 모두 `judge_tools.evaluate_criteria`를
    거친다 - measurement 존재 여부와 기준별 실제값을 이 함수의 판정 하나로
    통일하기 위해서다. 손수 만든 "키가 dict에 있는가" 검사는 값이 리터럴
    `None`으로 존재하는 경우를 놓친다(evaluate_criteria는 그 경우도 actual을
    NaN으로 채워 결측으로 잡는다).

    반환하는 `addresses`(둘째 값)는 측정에서 나온다 - 후보가 기존 본문보다
    나은 기준의 이름들이고, "낫다"는 그 기준의 연산자 방향으로 판정한다."""
    testbenches = slot.spec.testbenches
    criteria = slot.spec.all_criteria

    candidate_texts = {tb.name: apply_topology_swap(netlist_texts[tb.name], slot.block_path, candidate.subckt_body) for tb in testbenches}

    simulations_attempted = 0
    try:
        candidate_measurements, candidate_sim_count = _simulate_deck(candidate_texts, testbenches, sim_backend)
        simulations_attempted += candidate_sim_count
        baseline_measurements, baseline_sim_count = _simulate_deck(netlist_texts, testbenches, sim_backend)
        simulations_attempted += baseline_sim_count
    except Exception as exc:  # noqa: BLE001 - 시뮬레이터 예외는 거부가 아니라 inconclusive
        return (
            StageResult(
                name="reproduce",
                status="inconclusive",
                detail={
                    "error": str(exc),
                    "simulations_attempted": simulations_attempted,
                },
            ),
            [],
        )

    # evaluate_criteria가 "measurement가 나왔는가"의 유일한 판정처다 - 키가
    # 아예 없을 때뿐 아니라, 키는 있지만 값이 리터럴 None일 때도
    # (measurements.get(...)이 None을 돌려주면) actual을 math.nan으로
    # 채운다. 시뮬레이터가 "임계값 교차를 못 찾았다"를 키 생략이 아니라
    # None 값으로 보고하는 모양은 이 저장소가 이미 겪은 사실이다(45코너 중
    # 14곳에서 값 없는 settling time). 그래서 여기서 `c.measurement not in
    # candidate_measurements`처럼 키 존재만 보는 손수 만든 대체 검사를 쓰지
    # 않는다 - 그 검사는 키가 있고 값이 None인 경우를 놓친다.
    candidate_eval = evaluate_criteria(candidate_measurements, criteria)
    baseline_eval = evaluate_criteria(baseline_measurements, criteria)
    candidate_by_name = {r["name"]: r for r in candidate_eval["criteria"]}
    baseline_by_name = {r["name"]: r for r in baseline_eval["criteria"]}

    missing = sorted(name for name, r in candidate_by_name.items() if math.isnan(r["actual"]))
    if missing:
        return (
            StageResult(
                name="reproduce",
                status="fail",
                detail={
                    "missing": missing,
                    "candidate_measurements": candidate_measurements,
                    "baseline_measurements": baseline_measurements,
                    "simulation_count": simulations_attempted,
                },
            ),
            [],
        )

    per_criterion: dict = {}
    addresses: list[str] = []
    for c in criteria:
        candidate_value = candidate_by_name[c.name]["actual"]
        baseline_value = baseline_by_name[c.name]["actual"]
        better = not math.isnan(baseline_value) and _is_better(c.operator, candidate_value, baseline_value)
        per_criterion[c.name] = {
            "measurement": c.measurement,
            "operator": c.operator,
            "candidate": candidate_value,
            "baseline": baseline_value,
            "better": better,
        }
        if better:
            addresses.append(c.name)

    detail = {
        "missing": [],
        "criteria": per_criterion,
        "addresses": addresses,
        "candidate_measurements": candidate_measurements,
        "baseline_measurements": baseline_measurements,
        "simulation_count": simulations_attempted,
    }
    return StageResult(name="reproduce", status="pass", detail=detail), addresses


def _at_least_as_good(operator: str, value: float, reference: float) -> bool:
    """`_is_better`와 자매 함수이지만 다른 질문에 답한다: `_is_better`는
    "엄격히 낫다"(동률은 아니다)이고, 이 함수는 "적어도 그만큼 낫다"(동률도
    그렇다)이다. 3단의 파레토 지배 규칙이 한국어로 "후보 **이상**"이라고
    적은 것이 정확히 이 관계다 - 동률이 지배로 세지 않으면, 노브 하나로
    후보와 정확히 같은 값을 내는 기존 본문이 있어도 그 항목을 들여보내게
    되는데, 그건 "값 튜닝으로 못 가는 곳에 간다"는 라이브러리의 존재
    이유를 어긴다."""
    if operator in _BETTER_WHEN_LARGER:
        return value >= reference
    if operator in _BETTER_WHEN_SMALLER:
        return value <= reference
    return False


def _block_tunable_knobs(netlist_text: str, circuit_name: str, block_path: str) -> list[tuple[str, str]]:
    """그 블록 스코프 **안에** 있는 (refdes, param) 쌍만, 정렬된 결정론적
    순서로. `structure.derive_structure`의 tunable 인덱스는 넷리스트 전체를
    돌려주므로, `TunableEntry.refdes`가 스코프-한정 경로라는 사실
    (`structure.py`가 이미 확립함, 예: "BLOCK.R1")을 이용해 접두로 거른다.
    정렬은 `max_knobs` 절삭과 스윕 순서 둘 다를 결정론적으로 만든다 - 그래야
    "몇 번째 노브가 잘렸는가"를 테스트가 안정적으로 확인할 수 있다."""
    structure = derive_structure(netlist_text, circuit_name)
    prefix = f"{block_path}."
    knobs = {
        (entry.refdes, entry.param)
        for entry in structure.tunable
        if entry.refdes == block_path or entry.refdes.startswith(prefix)
    }
    return sorted(knobs)


def _sweep_values(baseline: float, allowed_multiplier: float, points: int, is_count: bool) -> list[float]:
    """`[baseline/M, baseline*M]`을 로그 등간격 `points`점으로 나눈 값들.

    기준선 자체는 뺀다 - 2단이 이미 그 값을 쟀으므로 3단이 다시 시뮬레이션할
    이유가 없다(브리프 규칙 2). `points`가 홀수면 로그 등간격의 가운뎃점이
    기하평균 `sqrt((baseline/M)*(baseline*M)) == baseline`과 정확히 같아지므로,
    끝점만이 아니라 매 값을 기준선과 비교해 걸러낸다.

    개수 노브(`m`/`nf`)는 반올림 후 정수로 중복 제거한다 - 이 저장소는
    비정수 `m` 제안을 이미 거부하는 규칙(`area_limits._integrality_violation`)을
    갖고 있고, 스윕이 그 규칙이 거부할 값을 애초에 만들어 시뮬레이션을
    낭비하지 않기 위해서다. 반올림이 두 로그 지점을 같은 정수로 뭉갤 수
    있으므로 중복 제거가 필요하다 - "노브 하나에 N점"을 약속하지 않는다;
    실제로 스윕한 값 목록을 그대로 `detail`에 담아 무엇을 봤는지 숨기지
    않는다."""
    low = baseline / allowed_multiplier
    high = baseline * allowed_multiplier
    if low <= 0 or high <= 0:
        return []

    log_low = math.log(low)
    log_high = math.log(high)
    step_count = max(points - 1, 1)
    raw = [math.exp(log_low + (log_high - log_low) * i / step_count) for i in range(points)]
    if is_count:
        raw = [float(round(v)) for v in raw]

    values: list[float] = []
    seen: set = set()
    for v in raw:
        if is_count:
            if v == round(baseline):
                continue
            key = v
        else:
            if math.isclose(v, baseline, rel_tol=1e-9):
                continue
            key = round(v, 12)
        if key in seen:
            continue
        seen.add(key)
        values.append(v)
    return values


def _simulate_point(
    netlist_texts: dict[str, str], testbenches: list[Testbench], sim_backend, changes: list[dict]
) -> tuple[dict, int, str | None]:
    """`changes`(**하나의** 변경)를 기존 본문에 적용한 덱을 슬롯의 모든
    테스트벤치에 대해 시뮬레이션한다. `_simulate_deck`(2단이 쓰는 것)과 갈라진
    이유는 예외 처리 단위가 다르기 때문이다: 2단은 시뮬레이터 예외를 단계
    전체의 inconclusive로 다루지만, 3단의 규칙(브리프 4)은 "시뮬 실패"를
    "이 스윕 지점 하나"의 결측과 같은 것으로 다룬다 - 나머지 스윕은 계속
    돈다. 그러려면 예외를 테스트벤치 루프 **안에서** 잡아야 하므로
    `_simulate_deck`을 재사용할 수 없다(그쪽은 예외 도중의 시도 횟수를
    돌려주지 않는다).

    실패 시 그때까지 모인 부분 measurement는 버린다(빈 dict를 돌려준다) -
    한 테스트벤치가 죽었는데 다른 테스트벤치의 값만으로 이 지점을 판정하면
    "이 지점의 완전한 값"이라는 착각을 준다. 실패도 "시도했다"로 세므로
    반환하는 count는 실패한 시도까지 포함한다."""
    measurements: dict = {}
    count = 0
    for tb in testbenches:
        text = apply_changes(netlist_texts[tb.name], changes)
        with tempfile.TemporaryDirectory() as tmpdir:
            netlist_path = os.path.join(tmpdir, "sweep.cir")
            with open(netlist_path, "w") as f:
                f.write(text)
            try:
                result = sim_backend.run(netlist_path, {"control_block": tb.control_block})
            except Exception as exc:  # noqa: BLE001 - 이 지점만 결측 처리, 단계 전체는 아니다
                count += 1
                return {}, count, str(exc)
        measurements.update(result.measurements)
        count += 1
    return measurements, count, None


def scoped_comparison(
    candidate: Candidate,
    slot: Slot,
    netlist_texts: dict[str, str],
    sim_backend,
    candidate_measurements: dict,
    max_knobs: int | None,
    points: int,
) -> StageResult:
    """3단: 범위 밝힌 비교와 파레토 거부.

    기존 본문은 **그대로 둔 채**, 그 블록의 tunable 인덱스에서 노브
    **하나씩** 에어리어 게이트가 그 파라미터에 허용하는 배수 `M` 안에서
    `[baseline/M, baseline*M]`을 `points`점 로그 등간격으로 스윕한다. 한
    스윕 지점의 변경은 정확히 하나다 - 두 노브를 함께 움직이면 "이 노브
    하나로 후보를 이긴다"는 주장이 실제로는 "이 두 노브의 조합으로 이긴다"는
    다른 주장이 되어, 이 게이트의 정직성 근거가 깨진다.

    거부 규칙은 파레토 지배다: 어떤 단일 스윕 지점이 **모든 기준에서**
    후보 이상이면(`_at_least_as_good`, 동률 포함) `REJECT`(`status="fail"`).
    후보가 단 하나의 기준에서라도 그 지점을 이기면 그 지점은 지배하지
    못한다 - "모든 축에서 이겨야 입회"가 아니라 "하나의 축에서라도 후보가
    앞서면 생존"이다.

    값을 못 낸 지점(시뮬 예외, 또는 evaluate_criteria가 결측으로 잡는 값 -
    키 부재든 리터럴 None이든)은 지배 후보에서 **제외**한다 - 결측을 무한히
    좋다/나쁘다로 취급하지 않는다. 그런 지점이 다른 기준에서 잰 값은 기준별
    "튜닝 최선값" 기록에는 여전히 반영한다(그 값 자체는 실제로 측정됐으므로).

    `detail`은 통과/거부와 무관하게 항상 비교 범위를 담는다: 스윕한 노브
    목록과 각각의 베이스라인/범위/점 수, 총 시뮬레이션 횟수, 기준별 튜닝
    최선값과 그 지점, 그리고(거부라면) 지배한 지점. `max_knobs`로 좁혔다면
    뺀 노브 이름이 `knobs_omitted`에, 에어리어 게이트가 이 파라미터에
    범위를 지을 수 없었다면(베이스라인 해소 불가, 티어 없음) 그 이름과
    사유가 `knobs_unresolved`에 남는다 - 이 저장소의 규칙대로, 노브를 뺐다면
    조용히 빼지 않는다."""
    testbenches = slot.spec.testbenches
    criteria = slot.spec.all_criteria

    canonical = slot.spec.canonical
    canonical_text = netlist_texts[canonical.name]

    all_knobs = _block_tunable_knobs(canonical_text, slot.spec.circuit_name, slot.block_path)

    knobs = all_knobs
    knobs_omitted: list[str] = []
    if max_knobs is not None and len(all_knobs) > max_knobs:
        knobs = all_knobs[:max_knobs]
        knobs_omitted = [f"{refdes}.{param}" for refdes, param in all_knobs[max_knobs:]]

    baseline_components = index_baseline_components(canonical_text)

    candidate_eval = evaluate_criteria(candidate_measurements, criteria)
    candidate_by_name = {r["name"]: r for r in candidate_eval["criteria"]}
    # 후보 쪽 measurement 자체가 불완전하면(정상 경로에서는 2단이 이미 이를
    # 막는다) 어느 지점도 "지배"로 판정하지 않는다 - 방어적 조치일 뿐, 이
    # 저장소의 정상 파이프라인이 도달하는 경로는 아니다.
    candidate_incomplete = any(math.isnan(r["actual"]) for r in candidate_by_name.values())

    knobs_swept: list[dict] = []
    knobs_unresolved: list[dict] = []
    excluded_points: list[dict] = []
    best_per_criterion: dict[str, dict] = {}
    dominating_point: dict | None = None
    simulation_count = 0

    for refdes, param in knobs:
        knob_name = f"{refdes}.{param}"
        component = baseline_components.get(refdes)
        if component is None:
            knobs_unresolved.append(
                {"knob": knob_name, "reason": "refdes not found in the canonical netlist's baseline index"}
            )
            continue

        baseline_value, allowed = tunable_range(component, param)
        if baseline_value is None:
            knobs_unresolved.append({"knob": knob_name, "reason": "baseline value could not be resolved"})
            continue
        if allowed is None:
            knobs_unresolved.append(
                {"knob": knob_name, "reason": "the area gate has no size tier for this parameter"}
            )
            continue
        if baseline_value <= 0:
            knobs_unresolved.append(
                {"knob": knob_name, "reason": f"baseline value {baseline_value!r} is not positive"}
            )
            continue

        is_count = is_count_param(component, param)
        values = _sweep_values(baseline_value, allowed, points, is_count)
        knobs_swept.append(
            {
                "knob": knob_name,
                "baseline": baseline_value,
                "allowed_multiplier": allowed,
                "range": [baseline_value / allowed, baseline_value * allowed],
                "swept_values": list(values),
            }
        )

        for value in values:
            new_value = str(int(round(value))) if is_count else repr(value)
            changes = [{"refdes": refdes, "param": param, "new_value": new_value}]
            measurements, count, error = _simulate_point(netlist_texts, testbenches, sim_backend, changes)
            simulation_count += count

            if error is not None:
                excluded_points.append(
                    {"knob": knob_name, "swept_value": value, "reason": f"simulation failed: {error}"}
                )
                continue

            point_eval = evaluate_criteria(measurements, criteria)
            point_by_name = {r["name"]: r for r in point_eval["criteria"]}
            missing = sorted(name for name, r in point_by_name.items() if math.isnan(r["actual"]))

            for c in criteria:
                actual = point_by_name[c.name]["actual"]
                if math.isnan(actual):
                    continue
                current = best_per_criterion.get(c.name)
                if current is None or _is_better(c.operator, actual, current["value"]):
                    best_per_criterion[c.name] = {"value": actual, "knob": knob_name, "swept_value": value}

            if missing:
                excluded_points.append({"knob": knob_name, "swept_value": value, "reason": f"missing measurements: {missing}"})
                continue

            if dominating_point is None and not candidate_incomplete:
                dominates = all(
                    _at_least_as_good(c.operator, point_by_name[c.name]["actual"], candidate_by_name[c.name]["actual"])
                    for c in criteria
                )
                if dominates:
                    dominating_point = {"knob": knob_name, "swept_value": value, "measurements": dict(measurements)}

    status = "fail" if dominating_point is not None else "pass"
    detail = {
        "topology_id": candidate.topology_id,
        "block_path": slot.block_path,
        "knobs_swept": knobs_swept,
        "knobs_omitted": knobs_omitted,
        "knobs_unresolved": knobs_unresolved,
        "excluded_points": excluded_points,
        "simulation_count": simulation_count,
        "best_per_criterion": best_per_criterion,
        "dominating_point": dominating_point,
    }
    return StageResult(name="comparison", status=status, detail=detail)
