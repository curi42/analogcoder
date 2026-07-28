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

from analogcoder.judge_tools import evaluate_criteria
from analogcoder.netlist import apply_topology_swap
from analogcoder.spec import TargetSpec, Testbench
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
