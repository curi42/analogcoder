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

from analogcoder.area_limits import (
    index_baseline_components,
    is_count_param,
    is_neutral_param,
    tunable_range,
)
from analogcoder.judge_tools import evaluate_criteria
from analogcoder.netlist import (
    apply_changes,
    apply_topology_swap,
    extract_subckt_body,
    netlist_scale,
    parse_netlist,
)
from analogcoder.pvt import run_full_pvt_sweep
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


def candidate_from_deck(deck_text: str, block_path: str, topology_id: str) -> Candidate:
    """소스 A: 이미 존재하는(그리고 보통 이미 검증된) 덱에서 블록 하나를
    후보로 뽑는다. 본문·`ports`·`assumes_scale` **셋 다 파싱에서** 나온다 -
    선언되거나 손으로 옮겨 적는 값이 하나도 없다:

    - 본문은 `netlist.extract_subckt_body`가 자르는 원문 그대로의 물리 줄
      슬라이스다. `Component` 객체들을 다시 문자열로 조립하지 않는 이유는
      `apply_topology_swap`이 이미 찾는 것과 같은 구간을 그대로 잘라내야만
      공백·토큰 순서가 원문과 바이트 단위로 같다는 것이 보장되기 때문이다 -
      재조립은 "파싱으로 얻는다"는 규칙을 지키는 척하면서 실제로는 손으로
      옮겨 적은 것과 같은 위험(오탈자, 순서 변경)을 진다.
    - `ports`는 `.subckt` 헤더 줄이 선언한 순서 그대로다
      (`ParsedNetlist.subckts[block_path].ports`).
    - `assumes_scale`은 이 덱의 `.option scale=`(선언이 없으면 1.0)이다 -
      이 값이 없으면 나중 단계가 W/L을 미터로 잘못 읽는, 면적 게이트가 이미
      한 번 겪은 실수를 후보 하나가 그대로 물려받는다.

    `block_path`가 이 덱에 없으면(스코프까지 포함해 정확히 일치해야 한다 -
    `apply_topology_swap`과 같은 규칙) `ValueError`를 낸다 - 추측하지
    않는다."""
    parsed = parse_netlist(deck_text)
    if block_path not in parsed.subckts:
        raise ValueError(f"subckt {block_path!r} not found in the deck")
    body = extract_subckt_body(deck_text, block_path)
    return Candidate(
        topology_id=topology_id,
        subckt_body=body,
        ports=list(parsed.subckts[block_path].ports),
        assumes_scale=netlist_scale(deck_text),
        provenance="extracted",
    )


def _ports_referenced_in_body(body: str, ports: list[str]) -> set[str]:
    """`ports`를 헤더로 씌워 본문을 파싱하고, 그 스코프에서 실제로 참조되는
    넷 이름 집합을 돌려준다 - `topology_match._wrap_topology_body`와 같은
    요령이다(본문 자체는 포트 헤더가 없는 독립 SPICE 조각이라 그대로는
    파싱 대상이 아니다). 본문에 중첩된 `.subckt`가 있어도 그 정의 **내부**
    소자의 노드는 보지 않는다 - 후보 본문의 포트가 참조됐는지는 그 포트가
    이 스코프(TMP)에서 어떤 소자의 노드로 등장하는지의 질문이지, 중첩 정의
    내부에서 같은 이름의 로컬 포트가 쓰였는지의 질문이 아니다."""
    wrapped = f".subckt TMP {' '.join(ports)}\n{body}\n.ends TMP\n"
    parsed = parse_netlist(wrapped)
    referenced: set[str] = set()
    for component in parsed.subckts["TMP"].components:
        referenced.update(component.nodes)
    return referenced


def candidate_from_file(body: str, ports: list[str], assumes_scale: float, topology_id: str) -> Candidate:
    """소스 B: 어딘가의 완성된 SPICE 조각을 그대로 받아 후보로 삼는다.
    `ports`는(소스 A와 달리) 파싱이 아니라 제출자의 **선언**이므로, 구조적으로
    판정 가능한 한 방향만 검증한다: 선언된 모든 포트가 본문에서 실제로
    참조되는가. 역방향(본문이 필요로 하는데 선언에 없는 포트)은 F1에서 이미
    확정된 사실대로 구조적으로 판정 불가능하다 - 선언에 없는 포트는 내부
    노드와 구별할 방법이 없고, 그런 넷은 스왑 후 뜬 넷이 되어 `reproduce_
    characteristics`의 시뮬레이션이 특성 재현 실패로 잡아낸다(추측 대신
    다음 단계에 맡긴다).

    참조되지 않는 선언 포트가 하나라도 있으면 `ValueError`를 낸다 - 그
    포트는 이 후보를 어떤 블록에 스왑해도 결코 연결될 수 없는 인터페이스를
    약속하는 것이므로, 시뮬레이션까지 가서야 발견하게 두지 않는다."""
    referenced = _ports_referenced_in_body(body, ports)
    unreferenced = [p for p in ports if p not in referenced]
    if unreferenced:
        raise ValueError(
            f"declared port(s) {unreferenced} are not referenced anywhere in the body - "
            "a port that is declared but never used cannot possibly connect to anything "
            "once this candidate is swapped into a block"
        )
    return Candidate(
        topology_id=topology_id,
        subckt_body=body,
        ports=list(ports),
        assumes_scale=assumes_scale,
        provenance="file",
    )


def candidate_from_technique(subckt_body: str, ports: list[str], assumes_scale: float, topology_id: str) -> Candidate:
    """소스 C: `agents.variant_author.author_variant`가 슬롯의 기존 본문을
    기법 하나로 국소 수정해 낸 본문을 후보로 감싼다.

    `ports`/`assumes_scale`은(소스 A처럼) 파싱에서 나온 것이 아니라 슬롯의
    기존 블록에서 그대로 물려받은 값이다 - 에이전트에게 요구한 것이 백지
    설계가 아니라 국소 수정이므로, 포트 집합과 스케일 가정은 애초에 바뀔
    이유가 없다. 소스 B(`candidate_from_file`)와 달리 선언된 포트가 본문에서
    실제로 참조되는지 여기서 다시 검증하지 않는다 - 저술본은 반드시
    `check_structure`(1단)를 거치고, 그 단계가 이미 포트 호환성을 판정하므로
    같은 검사를 여기서 미리 하는 것은 이중 판정이지 새 방어가 아니다."""
    return Candidate(
        topology_id=topology_id,
        subckt_body=subckt_body,
        ports=list(ports),
        assumes_scale=assumes_scale,
        provenance="authored",
    )


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


def verify_corners(
    candidate: Candidate,
    slot: Slot,
    netlist_texts: dict[str, str],
    sim_backend,
    addresses: list[str],
) -> StageResult:
    """2.5단: 저술본(`provenance == "authored"`)에만 붙는 코너 검증.

    추출본은 이미 45코너 스윕을 통과한 덱에서 나오고, 파일 제출본은 제출자의
    책임이다 - 이 단계가 실제로 시뮬레이션을 도는 대상은 **LLM이 지어낸** 본문
    뿐이다. `provenance`가 그 밖의 값이면 스윕 없이 `skipped`를 돌려주되
    `detail["why"]`에 사유를 적는다 - 건너뛴 것도 기록이라는 이 저장소의
    규칙대로, "이 출처는 대상이 아니다"와 "게이트가 조용히 사라졌다"를
    로그에서 구별할 수 있어야 한다.

    슬롯의 스펙이 `pvt_corners`를 선언하지 않으면 `inconclusive`를 돌려준다 -
    `fail`이 아니다. 코너를 잴 방법이 없다는 것은 "이 회로가 코너에서
    나쁘다"는 사실이 아니라 "재 보지 못했다"는 사실이고, 이 둘을 접으면
    F1이 반복한 실수를 여기서도 반복하는 것이다.

    스윕은 정확히 둘 - 후보를 이 슬롯에 스왑한 덱 한 번, 기존 본문 그대로
    한 번. `run_full_pvt_sweep`가 이미 코너 x 테스트벤치 전체를 도므로,
    이 함수가 다시 코너마다 또는 노브마다 도는 일은 없다(3단의 "노브 하나씩
    스윕"과는 다른 축이다 - 여기서 노브는 전혀 스윕하지 않는다).

    요구는 둘이다:
    1. 모든 코너에서 모든 기준(주소 지정된 것만이 아니라 스펙의 전체
       기준)의 measurement가 나와야 한다. `run_full_pvt_sweep`은 어느
       코너에서든 한 기준의 measurement가 빠지면 그 기준 전체를 결측으로
       돌려주므로(withhold), 여기서는 그 결과의 `actual`이 NaN인지만 보면
       된다 - 후보 쪽 스윕과 기존 본문 쪽 스윕 양쪽 다.
    2. `addresses`에 오른 각 기준에서, 후보의 최악 코너 값이 기존 본문의
       최악 코너 값보다 **엄격히** 나아야 한다(`_is_better` - 2단이 이미
       쓰는 것과 같은 함수, 같은 "동률은 개선이 아니다" 규칙). 이것은
       nominal 비교가 아니라 각 스윕이 이미 계산해 낸 최악값끼리의
       비교다 - nominal에서는 이겨도 최악 코너에서 지면 `fail`이다.

    이 단계는 3단(`scoped_comparison`)의 범위 밝힌 비교를 코너로 확장하지
    **않는다** - 다른 토폴로지/노브 값을 코너에서 스윕하는 일은 없고, 오직
    이 후보와 이 기존 본문 각각의 최악 코너 값만 비교한다. 그 한계를
    `detail["scope_note"]`에 문자열로 적어, 결과를 읽는 사람이 "코너를 감안한
    파레토 비교"로 오해하지 않게 한다.

    시뮬레이터 예외는 2단과 같은 이유로 거부가 아니라 `inconclusive`다 - 이
    저장소의 규칙대로 어떤 예외도 큐레이션을 트레이스백으로 끝내지 않는다."""
    if candidate.provenance != "authored":
        return StageResult(
            name="corners",
            status="skipped",
            detail={
                "why": (
                    f"provenance is {candidate.provenance!r}, not 'authored' - corner "
                    "verification applies only to authored bodies (an extracted body "
                    "already comes from a deck that passed a full corner sweep; a file "
                    "body is the submitter's responsibility, not this pipeline's)"
                ),
            },
        )

    if slot.spec.pvt_corners is None:
        return StageResult(
            name="corners",
            status="inconclusive",
            detail={
                "why": (
                    "the slot's spec declares no pvt_corners, so worst-case corner "
                    "behaviour cannot be measured for this authored candidate - this is "
                    "not evidence the candidate is bad, only that it was never tried"
                ),
            },
        )

    testbenches = slot.spec.testbenches
    candidate_texts = {
        tb.name: apply_topology_swap(netlist_texts[tb.name], slot.block_path, candidate.subckt_body)
        for tb in testbenches
    }

    current_sweep = "candidate"
    try:
        candidate_sweep = run_full_pvt_sweep(candidate_texts, slot.spec, sim_backend)
        current_sweep = "baseline"
        baseline_sweep = run_full_pvt_sweep(netlist_texts, slot.spec, sim_backend)
    except Exception as exc:  # noqa: BLE001 - 시뮬레이터 예외는 거부가 아니라 inconclusive
        return StageResult(
            name="corners",
            status="inconclusive",
            detail={"error": str(exc), "failed_during": current_sweep},
        )

    candidate_by_name = {r["name"]: r for r in candidate_sweep["criteria"]}
    baseline_by_name = {r["name"]: r for r in baseline_sweep["criteria"]}

    scope_note = (
        "this stage compares only the candidate's and incumbent's own worst-corner "
        "values over the full corner grid (two sweeps total) - it does NOT extend "
        "stage 3's single-knob scoped comparison to corners; no other topology or "
        "parameter value was swept at any corner here"
    )

    missing_candidate = sorted(name for name, r in candidate_by_name.items() if math.isnan(r["actual"]))
    missing_baseline = sorted(name for name, r in baseline_by_name.items() if math.isnan(r["actual"]))
    missing = sorted(set(missing_candidate) | set(missing_baseline))
    if missing:
        return StageResult(
            name="corners",
            status="fail",
            detail={
                "missing": missing,
                "missing_candidate": missing_candidate,
                "missing_baseline": missing_baseline,
                "candidate_worst_case_corners": candidate_sweep["worst_case_corners"],
                "baseline_worst_case_corners": baseline_sweep["worst_case_corners"],
                "scope_note": scope_note,
            },
        )

    criteria_by_name = {c.name: c for c in slot.spec.all_criteria}
    per_address: dict = {}
    worse: list[str] = []
    for name in addresses:
        criterion = criteria_by_name.get(name)
        if criterion is None:
            # addresses is produced by reproduce_characteristics from this same
            # slot's criteria list (see its docstring), so every name in it is
            # necessarily a key in criteria_by_name - this is unreachable under
            # the current call graph, not a normal "not found" case. Raising
            # explicitly here matches check_structure's own should-be-
            # unreachable guard rather than surfacing a bare KeyError.
            raise RuntimeError(
                f"addresses contains {name!r}, which is not a criterion name in "
                f"slot.spec.all_criteria - this should be unreachable"
            )
        candidate_value = candidate_by_name[name]["actual"]
        baseline_value = baseline_by_name[name]["actual"]
        better = _is_better(criterion.operator, candidate_value, baseline_value)
        per_address[name] = {
            "operator": criterion.operator,
            "candidate_worst": candidate_value,
            "baseline_worst": baseline_value,
            "candidate_worst_corner": candidate_sweep["worst_case_corners"].get(name),
            "baseline_worst_corner": baseline_sweep["worst_case_corners"].get(name),
            "better": better,
        }
        if not better:
            worse.append(name)

    status = "fail" if worse else "pass"
    detail = {
        "addresses": addresses,
        "criteria": per_address,
        "worse": worse,
        "candidate_worst_case_corners": candidate_sweep["worst_case_corners"],
        "baseline_worst_case_corners": baseline_sweep["worst_case_corners"],
        "scope_note": scope_note,
    }
    return StageResult(name="corners", status=status, detail=detail)


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

    양끝(`low`/`high`)은 `math.exp(math.log(...))` 왕복을 거치지 않고 그대로
    쓴다 - 로그·지수를 왕복하면 `low`가 `499.99999999999983`처럼 부동소수점
    잡음을 얻어 `detail["range"]`(같은 `low`/`high`를 나눗셈/곱셈으로 직접
    계산)와 어긋난다. 중간 점들만 로그 등간격 보간이 필요하므로 `exp`를 쓴다.

    개수 노브(`m`/`nf`)는 반올림 후 정수로 중복 제거하고, **최소 1로
    죈다** - `m`은 병렬 소자의 개수이고 `m=0`은 튜닝이 아니라 소자를 지운
    것이다(에어리어 게이트의 허용 범위 밖의 변경이지, 그 범위 *안의* 값이
    아니다). 베이스라인이 1에 가깝고 배수가 크면(`m=1`, `M=2` → 범위
    [0.5, 2.0]) 반올림만으로는 0이 나올 수 있으므로 반올림 다음에 죈다.
    이 저장소는 비정수 `m` 제안을 이미 거부하는 규칙
    (`area_limits._integrality_violation`)을 갖고 있고, 스윕이 그 규칙이
    거부할 값(또는 그보다 더 나쁜, 소자가 사라진 값)을 애초에 만들어
    시뮬레이션을 낭비하지 않기 위해서다. 반올림이 두 로그 지점을 같은
    정수로 뭉갤 수 있으므로 중복 제거가 필요하다 - "노브 하나에 N점"을
    약속하지 않는다; 실제로 스윕한 값 목록을 그대로 `detail`에 담아 무엇을
    봤는지 숨기지 않는다."""
    low = baseline / allowed_multiplier
    high = baseline * allowed_multiplier
    if low <= 0 or high <= 0:
        return []

    log_low = math.log(low)
    log_high = math.log(high)
    step_count = max(points - 1, 1)
    raw = []
    for i in range(points):
        if i == 0:
            raw.append(low)
        elif i == points - 1:
            raw.append(high)
        else:
            raw.append(math.exp(log_low + (log_high - log_low) * i / step_count))
    if is_count:
        raw = [max(1.0, float(round(v))) for v in raw]

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
    반환하는 count는 실패한 시도까지 포함한다.

    `apply_changes` 호출도 `try` **안에** 있다 - 그 호출은 (스코프가 없는
    refdes가 두 컴포넌트에 걸쳐 있을 때) `ValueError`를 던질 수 있고, 이
    스윕 지점이 캐노니컬이 아닌 테스트벤치를 겨냥했을 때 그런 모호성이
    생길 수 있다. 이 단계의 규칙은 "값을 못 낸 지점은 결측 처리하고 나머지
    스윕은 계속한다"이므로, `apply_changes`의 예외만 못 잡게 두면 그 규칙
    자체가 이 한 곳에서 깨져 스윕 전체가, 나아가 3단 전체가 죽는다 -
    `run_orchestration`이 `AgentExecutionError`와 나란히 `ValueError`를
    잡는 것과 같은 이유다."""
    measurements: dict = {}
    count = 0
    for tb in testbenches:
        try:
            text = apply_changes(netlist_texts[tb.name], changes)
            with tempfile.TemporaryDirectory() as tmpdir:
                netlist_path = os.path.join(tmpdir, "sweep.cir")
                with open(netlist_path, "w") as f:
                    f.write(text)
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
            # "볼 것이 없다"(neutral - nf 같은 손가락 개수, 구조적으로 면적
            # 중립)와 "볼 수는 있는데 확정 못 했다"(unjudged)는 다른
            # 사실이다(CLAUDE.md의 bounded/neutral/blind/unjudged 4상태와
            # 같은 구별). 같은 사유 문자열로 뭉개면 둘 다 "게이트가 티어가
            # 없다고 했다"로 읽혀, nf처럼 의도적으로 무제약인 노브가 진짜
            # 판단 불가 노브와 구별되지 않는다.
            reason = (
                "area-neutral (finger count / nf) - nothing to sweep, not unresolved"
                if is_neutral_param(component, param)
                else "the area gate has no size tier for this parameter"
            )
            knobs_unresolved.append({"knob": knob_name, "reason": reason})
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
