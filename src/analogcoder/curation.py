"""새 토폴로지 항목을 라이브러리에 넣을지 판정하는 큐레이션 게이트.

`docs/superpowers/plans/2026-07-28-topology-curation.md`의 "공통 인터페이스"가
정의하는 dataclass들과, 그 파이프라인의 1단(구조 검사)·2단(특성 재현)을 담는다.
1단·2단 자체는 **순수 함수**다 - `check_structure`/`reproduce_characteristics`
어디에도 LLM 호출이 없다.

**예외 하나: `author_and_verify_variant`(소스 C의 거부-재시도 루프)는 LLM을
호출한다.** CLAUDE.md의 확립된 관례 - `agents/*.py`는 시스템 프롬프트·스키마만
담고, 재시도/오케스트레이션은 게이트 옆의 결정론적 모듈에 둔다(파라미터 튜닝의
`orchestrator.py`, 최적화의 `optimizer.py` vs `agents/optimizer.py`의
순위-매기기 분리) - 를 따라 이 자리에 있다. LLM 호출 자체
(`agents.variant_author.author_variant` - 시스템 프롬프트와 스키마)는 여전히
`agents/` 아래에 있고, 이 함수는 그 결과를 이 모듈 자신의 게이트
(`check_structure`/`reproduce_characteristics`)에 실제로 통과시키는 재시도
루프이므로 게이트와 같은 자리가 맞다 - `description` 렌더링(다른 LLM 호출)만
`agents/curator.py`에 남아 있고, 그것은 어떤 게이트도 재시도하지 않으므로
이 예외에 해당하지 않는다.

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

from analogcoder.agents.backend import AgentBackend, AgentExecutionError
from analogcoder.agents.variant_author import author_variant
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
    declares_include,
    declares_scale,
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
_EQUALITY = ("==",)
# 이 모듈의 비교 규칙(`_is_better`/`_at_least_as_good`)이 실제로 판정할 수 있는
# 연산자 전체. `judge_tools._OPERATORS`가 스펙 언어로 허용하는 다섯 개와 같지만,
# 여기서 다시 적는 이유는 **판정 가능성**이 통과/불통과 판정과 다른 질문이기
# 때문이다 - 이 목록 밖의 연산자를 만나면 3단은 "지배가 없었다"(pass)가 아니라
# `inconclusive`로 끝나야 한다(아래 `_unjudgeable_operators`).
_COMPARABLE_OPERATORS = _BETTER_WHEN_LARGER + _BETTER_WHEN_SMALLER + _EQUALITY

# 3단(범위 밝힌 비교)의 파레토 판정에 쓰는 **상대** 허용오차.
#
# **이 값은 추측이 아니라 실측에서 나왔다.** 출하 슬롯 스펙
# `benchmarks/bandgap/spec_curate_slot.yaml`(4개 앰프가 바이어스 레일을 공유하는
# 8기준 단일 테스트벤치)에 Ahuja(지시 보상) 후보를 넣고 `TRIMAMP`의 30노브 x
# 5점 = 120회 시뮬레이션(2분 40초)을 실제로 돌려 얻은 숫자들이다:
#
#   잡음(솔버 정밀도 수준, 노브를 움직여도 변하지 않는다):
#     core_phase_margin  후보 66.08350 / 기존 66.08070 -> +0.0028 deg = 4.2e-5 상대
#     trim_loop_gain     후보 87.54500 / 기존 87.54490 -> +0.0001 dB  = 1.1e-6 상대
#   실제 설계 차이(같은 실행에서 같은 스윕이 낸 값):
#     trim_phase_margin  기존 81.13820 -> 89.42130     -> +8.3 deg    = 0.102 상대
#
# 허용오차가 없으면(엄격한 0 허용오차) 이 두 잡음이 양방향으로 거짓말을 한다.
# 지배를 막는 쪽으로는 `core_phase_margin`이 0.0011 deg 모자라 스윕 전 구간에서
# 지배를 봉쇄했고(그래서 이 하위 프로젝트 자신의 증명 사례인 Ahuja 후보가
# `dominating: None`으로 ADMIT됐다), 주장을 만드는 쪽으로는 같은 크기의 잡음이
# `addresses`에 `core_phase_margin`/`trim_loop_gain`을 올려 검증되지 않은 개선
# 주장을 튜너 프롬프트(`agents/tuner.py`)까지 실어 보냈다.
#
# 1e-3은 측정된 최대 잡음(4.2e-5)의 **약 24배 위**, 측정된 실제 개선(0.102)의
# **약 1/100 아래**다 - 양쪽으로 세 자리수 가까운 여유. 이 저장소는 가드 밴드에서
# 이미 같은 교훈을 값 주고 배웠다(추측한 비율 `g=0.2`는 bandgap에서 공집합
# 구간을 만들어 어떤 단계도 수락할 수 없게 만들었고, 실측한 0.0051이 그것을
# 고쳤다) - 그래서 여기서도 둥근 수를 고르지 않고 산술을 적는다.
COMPARISON_REL_TOLERANCE = 1e-3

# `author_and_verify_variant`(소스 C 거부-재시도 루프)의 재시도 상한.
# `orchestrator.MAX_TUNING_RETRIES`와 같은 값(3)이지만, 다른 서브시스템의
# 상수를 임포트해 결합을 만들지 않기 위해 이 모듈 자신의 상수로 복제한다.
MAX_VARIANT_AUTHOR_RETRIES = 3

# 3단의 지배 후보 중 "아무 노브도 바꾸지 않은" 지점 - 기존 본문 그 자체 - 에
# 붙는 라벨. 스윕 지점과 **구별 가능해야** 하므로(`knob`/`swept_value`가 둘 다
# `None`인 것만으로는 결측과 헷갈릴 수 있다) 명시적인 문자열을 둔다.
INCUMBENT_POINT_LABEL = "incumbent as shipped (no change)"


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
    않는다.

    **`.subckt` 헤더를 그대로 베끼는 것이 곧 그 포트들이 본문에 쓰인다는
    뜻은 아니다.** 그래서 소스 B와 **같은** 한 방향 검사
    (`reject_unreferenced_ports`)를 여기서도 돌린다 - 왜 세 출처 모두에
    필요한지는 그 함수의 docstring에 실측과 함께 있다.

    `assumes_scale`은 이 덱 **자신의 텍스트**에 있는 `.option scale=`에서만
    나온다. `parse_netlist`는 `.include`를 따라가지 않으므로, 스케일이 오직
    include 안에만 있는 덱은 여기서 조용히 1.0으로 기록될 수 있고 그것은
    1e6배 틀린 값이다(CLAUDE.md에 이미 기록된 함정 - 면적 게이트의 티어를
    모든 PDK 벤치마크에서 무력화한 바로 그 실수). 그래서 **추측하지 않고
    거부한다**: 덱이 `.option scale`을 자기 텍스트에 선언하지 않았는데
    `.include`를 갖고 있으면 `ValueError`다. include가 하나도 없으면 1.0은
    추측이 아니라 사실이다."""
    parsed = parse_netlist(deck_text)
    if block_path not in parsed.subckts:
        raise ValueError(f"subckt {block_path!r} not found in the deck")
    body = extract_subckt_body(deck_text, block_path)
    ports = list(parsed.subckts[block_path].ports)
    reject_unreferenced_ports(body, ports)
    if not declares_scale(deck_text) and declares_include(deck_text):
        raise ValueError(
            "this deck declares no `.option scale` of its own but does have `.include` "
            "line(s), so its geometry scale may live only inside an include - and "
            "parse_netlist never follows includes. Recording assumes_scale=1.0 here "
            "would be wrong by up to 1e6 (CLAUDE.md's `.option scale` trap). Declare "
            "`.option scale=` in the deck itself, or submit this body via --from-body "
            "with an explicit --assumes-scale"
        )
    return Candidate(
        topology_id=topology_id,
        subckt_body=body,
        ports=ports,
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


def reject_unreferenced_ports(body: str, ports: list[str]) -> None:
    """선언된 포트가 본문에서 실제로 참조되는가 - **세 출처 전부**가 통과해야
    하는 한 방향 검사. 위반이면 `ValueError`, 아니면 조용히 돌아온다.

    이 검사가 왜 세 출처 모두에 필요한가(리뷰가 실행으로 확인한 사실):
    소스 A는 `.subckt` 헤더를 그대로 베끼고 소스 C는 슬롯 블록의 포트를 그대로
    물려받으므로, 둘 다 **본문이 쓰지 않는 포트를 선언한 항목**을 만들 수 있다.
    소스 C의 옛 docstring은 "1단이 이미 포트 호환성을 판정하므로 여기서 다시
    볼 필요가 없다"고 적었는데 **그것은 거짓이었다**: 1단
    (`topology_match.compatible_swaps`)에 넘어가는 `topology.ports`가 바로 그
    블록 자신의 포트이므로 `set(topology.ports) <= set(block.ports)`는 항등식이
    되고, `leftover_ports`가 빈 리스트가 되어 부동 넷 검사가 **한 번도 돌지
    않는다.** 실측:

        source A ports: [... 'nbias']   (본문은 'nbias'를 한 번도 참조하지 않는다)
        source B 같은 본문: REJECTED - declared port(s) ['nbias'] are not referenced
        source C 같은 본문: ACCEPTED, provenance='authored'

        그렇게 나온 항목을 nbias 넷의 유일 참여자인 블록에 스왑하면
          ports = 본문이 실제로 쓰는 것  -> REJECTED ("it would float")
          ports = 소스 A/C가 선언한 것   -> 스왑 후보로 ADMITTED

    즉 부풀려진 `ports`는 그 항목이 라이브러리에 커밋된 **이후로 영원히**
    부동 넷 검사를 무력화한다 - 이 저장소가 여섯 번 출하한 "조용히 아무것도
    하지 않는 게이트"와 같은 모양이고, 이번에는 라이브러리 항목 하나가 그
    상태를 영구화한다. 소스 C의 일상적인 발동 조건: `--technique "remove the
    cascode"`는 `ncas`/`pcas`를 더 이상 참조하지 않는 본문을 내는데 항목은
    여전히 그 포트들을 선언한다.

    역방향(본문이 필요로 하는데 선언에 없는 포트)은 F1에서 확정된 대로
    구조적으로 판정 불가능하다 - 선언에 없는 포트는 내부 노드와 구별할 방법이
    없다. 그 방향은 2단의 시뮬레이션이 특성 재현 실패로 잡는다."""
    referenced = _ports_referenced_in_body(body, ports)
    unreferenced = [p for p in ports if p not in referenced]
    if unreferenced:
        raise ValueError(
            f"declared port(s) {unreferenced} are not referenced anywhere in the body - "
            "a port that is declared but never used cannot possibly connect to anything "
            "once this candidate is swapped into a block"
        )


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
    reject_unreferenced_ports(body, ports)
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
    설계가 아니라 국소 수정이므로, 스케일 가정은 애초에 바뀔 이유가 없다.

    **포트 집합은 다르다.** 이 함수의 옛 docstring은 소스 B와 달리 여기서
    포트 참조 검사를 하지 않는 근거로 "저술본은 반드시 1단을 거치고 그 단계가
    이미 포트 호환성을 판정한다"를 들었는데, **그것은 거짓이다** - 1단에
    넘어가는 `topology.ports`가 바로 그 블록 자신의 포트이므로 포트 규칙은
    항등식이 되고 부동 넷 검사는 한 번도 돌지 않는다(실측은
    `reject_unreferenced_ports`의 docstring). 그리고 물려받은 포트가 저술된
    본문에서 여전히 쓰인다는 보장은 없다: `--technique "remove the cascode"`
    하나면 `ncas`/`pcas`를 안 쓰는 본문이 나온다. 그래서 세 출처 전부와 같은
    검사를 여기서도 돌린다 - 이중 판정이 아니라, 그 자리에 검사가 아예 없었던
    것이다."""
    reject_unreferenced_ports(subckt_body, ports)
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


@dataclass
class VariantAuthorResult:
    """`author_and_verify_variant`(소스 C의 거부-재시도 루프)의 결과.

    `verdict`는 일부러 `CurationResult.verdict`와 같은 세 값을 문자열로 쓰지만
    ("PASS" 대신 "ADMIT"이 아니다) 이것은 최종 파이프라인 판정이 **아니다** -
    2.5단(코너, 저술본 전용)·3단(범위 밝힌 비교)이 아직 남아 있으므로,
    이 단계가 "PASS"를 내도 최종 판정은 여전히 다른 태스크(CLI)가 그 두 단계를
    마저 돌려야 정해진다. 그래서 성공 값은 `CurationResult`의 `"ADMIT"`과
    구별해 `"PASS"`로 쓴다 - 이 결과를 최종 판정으로 오해하는 호출자가 생기지
    않도록.

    `structure`/`reproduce`/`rationale`은 실제로 도달한 마지막 시도의 값이다 -
    도달하지 못한 단계(예: 구조 검사에서 매번 거부돼 2단에 한 번도 못 간 경우)
    는 `None`으로 남아, "이 단계가 통과했다"와 "이 단계를 아예 못 봤다"가
    같은 값으로 뭉개지지 않는다. **모든 반환 경로가 그렇다** - 재시도 상한을
    소진한 `"REJECT"`도 포함이다. 예전에는 그 경로만 셋을 하드코딩 `None`으로
    버려서, 3번의 LLM 호출·3번의 구조 검사·3번의 시뮬레이션된 2단 뒤에
    리포트의 `## Stages`가 비고 `curation.json`의 `"stages"`가 `[]`가 됐다."""

    verdict: str  # "PASS" | "REJECT" | "INCONCLUSIVE"
    reason: str | None
    attempts: int
    candidate: Candidate | None
    rationale: str | None
    structure: StageResult | None
    reproduce: StageResult | None
    addresses: list[str]


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


def _is_better(
    operator: str, candidate_value: float, baseline_value: float, tolerance: float = COMPARISON_REL_TOLERANCE
) -> bool:
    """`operator`의 방향으로 후보 값이 기존 값보다 **의미 있게** 나은가.

    "의미 있게"는 상대 허용오차 `tolerance`로 정의한다: 개선은 기준값의
    `tolerance * |baseline_value|`를 **넘어야** 세어진다. 그 안쪽은 동률이다.
    허용오차 없이 판정하면 SPICE 솔버 정밀도 수준의 차이(실측: 66.08 deg에서
    0.0028 deg)가 개선으로 세어져, 이 함수가 낳는 `addresses`가 **측정되지 않은
    주장**이 되고 그대로 튜너의 스왑 선택 프롬프트까지 간다 -
    `COMPARISON_REL_TOLERANCE`의 주석에 그 실측이 있다.

    `_at_least_as_good`과 **대칭**이다: 같은 폭의 띠 `[b - m, b + m]`
    (`m = tolerance * |b|`)를 두 함수가 공유하므로, 그 안의 값은 이쪽에서
    "낫지 않다"(False)이고 저쪽에서 "적어도 그만큼은 된다"(True)가 된다 -
    한 차이가 개선이면서 동시에 지배를 막는 일은 생기지 않는다.

    `==` 기준은 개선 방향이 정의되지 않으므로(맞다/틀리다이지 "더 낫다"가 없다)
    결코 개선으로 세지 않는다 - 추측하지 않는다. 그 밖의 알 수 없는 연산자도
    `False`(= 개선 아님)로 닫힌다: 이 함수의 실패 방향은 "모르면 주장하지
    않는다"이다. 반대로 `_at_least_as_good`은 같은 상황에서 조용히 `False`를
    내면 **열려서** 실패하므로(아무 지점도 지배하지 못해 게이트가 통과된다)
    거기서는 예외를 던지고, 3단이 그것을 `inconclusive`로 바꾼다."""
    band = tolerance * abs(baseline_value)
    if operator in _BETTER_WHEN_LARGER:
        return candidate_value > baseline_value + band
    if operator in _BETTER_WHEN_SMALLER:
        return candidate_value < baseline_value - band
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


def _stage_rejection_reason(stage_name: str, detail: dict) -> str:
    """`StageResult.detail`에서 다음 시도로 그대로 돌려줄 사유 문자열을 뽑는다.
    `check_structure`(1단)의 detail은 `reason`/`detail` 키를 쓰고
    (`compatible_swaps`가 낸 그대로), `reproduce_characteristics`(2단)의 실패
    detail은 `missing` 키를 쓴다 - 두 단계의 detail 모양이 다르므로 사유를
    뽑는 규칙도 단계별로 다르다. 어느 쪽이든 여기서 새 문장을 짓지 않는다:
    단계가 이미 낸 사실을 그대로 이어붙일 뿐이다(브리프 규칙 3의 "그대로")."""
    if stage_name == "structure":
        return f"stage 1 (structure) rejected: {detail.get('reason')}: {detail.get('detail')}"
    return f"stage 2 (reproduce) rejected: missing measurements: {detail.get('missing')}"


async def author_and_verify_variant(
    base_body: str,
    technique: str,
    ports: list[str],
    available_models: set[str],
    scale: float,
    topology_id: str,
    slot: Slot,
    netlist_texts: dict[str, str],
    sim_backend,
    backend: AgentBackend,
) -> VariantAuthorResult:
    """`agents.variant_author.author_variant`를 거부-재시도 루프로 감싸고,
    통과한 본문을 1단(`check_structure`)·2단(`reproduce_characteristics`)에
    실제로 통과시킨다. 2.5단(코너)·3단(범위 밝힌 비교)은 여기서 돌지 않는다 -
    그 둘은 저술 자체를 재시도할 이유가 없는, 이미 통과한 후보에 대한 별도
    판정이고(설계 문서 "2.5단"/"3단" 절), 이 함수가 그 후보를 다음 단계로
    넘기는 자리다.

    `VariantAuthorResult.verdict`는 `AgentExecutionError`가 즉시
    `"INCONCLUSIVE"`로 끝난다(재시도하지 않는다) - 백엔드가 죽었다면 다시
    불러도 대개 또 죽고, 남은 재시도 예산을 태우는 것은 "회로가 나쁘다"는
    사실을 하나도 더 밝히지 못한다. 2단이 시뮬레이터 예외로 `inconclusive`를
    낼 때도 같은 이유로 즉시 멈춘다 - 그것은 이 저술본이 나쁘다는 증거가
    아니라 시뮬레이터가 답하지 못했다는 사실이고, 다시 저술을 시켜도
    시뮬레이터가 죽는 이유는 바뀌지 않는다. 반대로 1단·2단이 각각 실패
    (`status == "fail"`)로 저술본을 거부하면 그 사유를 그대로 다음 시도의
    `rejection_feedback`으로 돌려주고, `MAX_VARIANT_AUTHOR_RETRIES`번까지
    다시 시도한다 - 그 상한을 다 쓰고도 통과하는 본문을 못 만들면 `"REJECT"`
    (재보지 못한 것이 아니라 재봤는데 실패했다), `reason`에 마지막 거부
    사유를 담는다."""
    rejection_feedback: str | None = None
    last_reason: str | None = None
    # 실제로 **도달한** 마지막 시도의 단계 결과들. 재시도 상한을 다 쓴 뒤의
    # 반환이 이것들을 버리면 - 예전 코드는 둘 다 하드코딩 `None`이었다 -
    # `curation_report.md`의 `## Stages`가 텅 비고 `curation.json`의 `"stages"`가
    # `[]`가 된다. LLM 호출 3회, 구조 검사 3회, 시뮬레이션된 2단 3회를 실제로
    # 돌고 나서다. 이 dataclass 자신의 docstring이 약속한 것("도달한 마지막
    # 시도의 StageResult")과 정면으로 어긋나고, 무엇보다 이 저장소가 반복해 온
    # "검사했고 문제없음"과 "검사가 사라짐"이 로그에서 구별되지 않는 모양
    # 그대로다.
    last_structure: StageResult | None = None
    last_reproduce: StageResult | None = None
    last_rationale: str | None = None

    for attempt in range(1, MAX_VARIANT_AUTHOR_RETRIES + 1):
        try:
            authored = await author_variant(
                base_body=base_body,
                technique=technique,
                ports=ports,
                available_models=available_models,
                scale=scale,
                rejection_feedback=rejection_feedback,
                backend=backend,
            )
        except AgentExecutionError as exc:
            return VariantAuthorResult(
                verdict="INCONCLUSIVE",
                reason=str(exc),
                attempts=attempt,
                candidate=None,
                rationale=last_rationale,
                structure=last_structure,
                reproduce=last_reproduce,
                addresses=[],
            )

        last_rationale = authored.get("rationale")

        try:
            candidate = candidate_from_technique(
                subckt_body=authored["subckt_body"],
                ports=ports,
                assumes_scale=scale,
                topology_id=topology_id,
            )
        except ValueError as exc:
            # 저술본이 물려받은 포트 중 하나를 더 이상 참조하지 않는다
            # (`reject_unreferenced_ports`). 이것은 "재보지 못했다"가 아니라
            # 이 시도의 본문이 나쁘다는 **판정된 사실**이므로, 1단·2단 거부와
            # 똑같이 사유를 그대로 피드백으로 돌려주고 다시 시도한다 -
            # 예외로 새어 나가면 CLI의 가드가 그것을 INCONCLUSIVE로 바꾸어
            # 남은 재시도 예산을 통째로 버린다.
            rejection_feedback = (
                f"the authored body no longer references every port it must keep: {exc}"
            )
            last_reason = rejection_feedback
            continue

        structure_result = check_structure(candidate, slot, netlist_texts)
        last_structure = structure_result
        if structure_result.status != "pass":
            rejection_feedback = _stage_rejection_reason("structure", structure_result.detail)
            last_reason = rejection_feedback
            continue

        reproduce_result, addresses = reproduce_characteristics(candidate, slot, netlist_texts, sim_backend)
        last_reproduce = reproduce_result
        if reproduce_result.status == "inconclusive":
            return VariantAuthorResult(
                verdict="INCONCLUSIVE",
                reason=reproduce_result.detail.get("error", "stage 2 (reproduce) was inconclusive"),
                attempts=attempt,
                candidate=None,
                rationale=authored.get("rationale"),
                structure=structure_result,
                reproduce=reproduce_result,
                addresses=[],
            )
        if reproduce_result.status != "pass":
            rejection_feedback = _stage_rejection_reason("reproduce", reproduce_result.detail)
            last_reason = rejection_feedback
            continue

        return VariantAuthorResult(
            verdict="PASS",
            reason=None,
            attempts=attempt,
            candidate=candidate,
            rationale=authored.get("rationale"),
            structure=structure_result,
            reproduce=reproduce_result,
            addresses=addresses,
        )

    return VariantAuthorResult(
        verdict="REJECT",
        reason=last_reason,
        attempts=MAX_VARIANT_AUTHOR_RETRIES,
        candidate=None,
        rationale=last_rationale,
        structure=last_structure,
        reproduce=last_reproduce,
        addresses=[],
    )


def verify_corners(
    candidate: Candidate,
    slot: Slot,
    netlist_texts: dict[str, str],
    sim_backend,
    addresses: list[str],
) -> StageResult:
    """2.5단: 저술본(`provenance == "authored"`)에만 붙는 코너 검증.

    **코너 검증은 (본문 x 슬롯)의 속성이지, SPICE 텍스트 자체의 속성이
    아니다.** 추출본이 원래 덱의 원래 자리에서 45코너를 통과했다는 사실이
    있어도, 그 본문을 뽑아 **다른** 슬롯에 제안하는 것은 이력과 무관한 새
    조합이고, 이 파이프라인은 임의의 `--from-deck` 대상이 실제로 코너
    스윕을 거쳤는지, 거쳤다 해도 그 검증이 이 슬롯으로 옮아오는지 알 방법이
    없다 - 그래서 추측하지 않는다. 파일 제출본은 애초에 제출자의 책임이라
    이 파이프라인이 검증할 근거 자체가 없다. 이 단계가 실제로 시뮬레이션을
    도는 대상은 **LLM이 지어낸**(즉 지금까지 누구도 검증한 적 없는) 본문
    뿐이다. `provenance`가 그 밖의 값이면 스윕 없이 `skipped`를 돌려주되
    `detail["why"]`에 사유를 적는다 - 건너뛴 것도 기록이라는 이 저장소의
    규칙대로, "이 출처는 대상이 아니다"와 "게이트가 조용히 사라졌다"를
    로그에서 구별할 수 있어야 한다. 그 문구는 "원 덱이 코너를 통과했다"는
    사실 주장을 하지 않는다 - 그런 주장은 이 파이프라인이 확인할 수 없는
    것이고, 그것을 확인한 것처럼 적으면 리포트의 `verified_at`(추출본은
    `"nominal"`)과 정면으로 모순된다.

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
                    f"provenance is {candidate.provenance!r}, not 'authored' - this "
                    "pipeline only measures corners for authored bodies, because an "
                    "authored body is the one nobody has ever verified. "
                    "Corner-verification is a property of (body x slot), not of the "
                    "SPICE text alone: this pipeline does not know, and does not "
                    "guess, whether an extracted or file-provenance candidate's source "
                    "was ever corner-swept, or whether that history would transfer to "
                    "THIS slot - so this skip makes no claim either way about the "
                    "source deck's own corner-verification history"
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
                "addresses": list(addresses),
                "addresses_compared": 0,
                "requirement_2_note": (
                    "requirement 2 (worst-corner comparison on the addressed criteria) was "
                    "not reached - requirement 1 already failed on a missing measurement"
                ),
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

    # 요구 2가 **몇 개를 비교했는지**를 항상 적는다. `addresses`가 비어 있으면
    # 이 루프는 한 바퀴도 돌지 않고 `worse`가 비어 `pass`가 되는데, 그 `pass`는
    # "코너에서 기존 본문보다 낫다"가 아니라 "요구 1만 통과했다"는 뜻이다.
    # 이것을 적지 않으면 산출물의 `verified_at="corners"`가 실제로 잰 것보다
    # 많은 것을 주장하게 된다(실측: 후보가 기존 본문보다 나쁜데 addresses가
    # 비어 `corners: pass, criteria: {}, worse: []`로 ADMIT됐다).
    if addresses:
        requirement_2_note = (
            f"requirement 2 compared {len(addresses)} addressed criterion(s) at their "
            f"worst corner: {list(addresses)}"
        )
    else:
        requirement_2_note = (
            "requirement 2 compared NOTHING: `addresses` is empty, so not a single "
            "criterion's worst-corner value was compared against the incumbent's. This "
            "stage's 'pass' therefore rests on requirement 1 (every criterion produced a "
            "measurement at every corner) alone - read verified_at='corners' as no more "
            "than that"
        )

    status = "fail" if worse else "pass"
    detail = {
        "addresses": addresses,
        "addresses_compared": len(addresses),
        "requirement_2_note": requirement_2_note,
        "criteria": per_address,
        "worse": worse,
        "candidate_worst_case_corners": candidate_sweep["worst_case_corners"],
        "baseline_worst_case_corners": baseline_sweep["worst_case_corners"],
        "scope_note": scope_note,
    }
    return StageResult(name="corners", status=status, detail=detail)


def _at_least_as_good(
    operator: str, value: float, reference: float, tolerance: float = COMPARISON_REL_TOLERANCE
) -> bool:
    """`_is_better`와 자매 함수이지만 다른 질문에 답한다: `_is_better`는
    "의미 있게 낫다"이고, 이 함수는 "적어도 그만큼은 된다"(동률도, 허용오차
    안의 근소한 열세도 그렇다)이다. 3단의 파레토 지배 규칙이 한국어로 "후보
    **이상**"이라고 적은 것이 정확히 이 관계다 - 동률이 지배로 세지 않으면,
    노브 하나로 후보와 정확히 같은 값을 내는 기존 본문이 있어도 그 항목을
    들여보내게 되는데, 그건 "값 튜닝으로 못 가는 곳에 간다"는 라이브러리의
    존재 이유를 어긴다.

    허용오차는 `_is_better`와 **같은 띠**를 쓴다(`m = tolerance * |reference|`).
    실측이 보여준 것은 이 방향의 손실이 더 비싸다는 것이다: 0 허용오차에서
    `core_phase_margin`이 0.0011 deg(1.7e-5 상대) 모자라 스윕 **전 구간**에서
    지배를 봉쇄했고, 그 결과 이 하위 프로젝트 자신의 증명 사례인 Ahuja 후보가
    `dominating: None`으로 ADMIT됐다.

    `==`는 **양방향 허용오차 안이면 "적어도 그만큼"이다.** `_is_better`가 `==`
    에서 `False`를 내는 것은 옳지만(개선 방향이 없다), 이 함수가 같은 이유로
    `False`를 내면 **열려서 실패한다** - `==` 기준이 하나라도 있으면 어떤
    지점도 결코 모든 기준에서 후보 이상이 될 수 없고, 3단은 구조적으로 아무도
    거부하지 못하는 상태가 되면서 그 사실을 어디에도 적지 않는다.

    판정할 수 없는 연산자는 조용히 `False`를 내지 않고 `ValueError`를 던진다 -
    "판정 못 했다"와 "지배가 없었다"는 다른 사실이고, 3단은 전자를
    `inconclusive`(연산자 이름과 함께)로 바꾼다. 정상 경로에서는
    `scoped_comparison`이 시작하자마자 `_unjudgeable_operators`로 걸러내므로
    이 예외에 도달하지 않는다."""
    band = tolerance * abs(reference)
    if operator in _BETTER_WHEN_LARGER:
        return value >= reference - band
    if operator in _BETTER_WHEN_SMALLER:
        return value <= reference + band
    if operator in _EQUALITY:
        return abs(value - reference) <= band
    raise ValueError(
        f"_at_least_as_good cannot judge operator {operator!r} - failing open here would "
        "silently make the Pareto rule unable to reject anything"
    )


def _unjudgeable_operators(criteria) -> list[dict]:
    """이 모듈의 비교 규칙이 판정할 수 없는 연산자를 쓰는 기준들. 3단이 첫
    줄에서 이것을 확인하고, 하나라도 있으면 시뮬레이션을 한 번도 쓰지 않고
    `inconclusive`로 끝낸다 - **연산자 이름과 함께**. 조용한 통과는 "아무도
    후보를 지배하지 못했다"(측정된 사실)와 "나는 이 기준을 판정할 수 없었다"
    (판정 불능)를 같은 값으로 뭉갠다."""
    return [
        {"criterion": c.name, "operator": c.operator}
        for c in criteria
        if c.operator not in _COMPARABLE_OPERATORS
    ]


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

    **두 끝의 근거가 다르다.** `M`(`allowed_multiplier`)은 에어리어 게이트가 이
    파라미터에 허용하는 **성장** 배수이므로, 위쪽 끝 `baseline*M`만 게이트가
    실제로 긋는 선이다. 아래쪽 끝 `baseline/M`은 이 단계가 **스스로 정한 대칭
    경계**이지 게이트의 경계가 아니다 - `area_limits.evaluate_area_growth`는
    `if group.ratio <= 1.0: continue`로 짧게 끊으므로 **축소는 전혀 제한하지
    않는다.** 그러므로 튜너는 `baseline/M`보다 더 작은 값도 합법적으로 제안할
    수 있고, 그런 지점은 여기서 **보지 않았다**. 이 비대칭은 판정을 후보에게
    유리한 쪽으로(= ADMIT 쪽으로) 기울이므로, 3단의 `detail`이 그 사실을
    문장으로 남긴다(`sweep_bounds_note`). 아래쪽을 무한정 넓히는 것은 다른
    설계 질문(무제한 축소 스윕)이라 여기서 하지 않는다 - 하지 않는다는 사실을
    적을 뿐이다.

    기준선 자체는 뺀다 - 2단이 이미 그 값을 쟀으므로 3단이 다시 시뮬레이션할
    이유가 없다. 다만 **그 값이 판정에서 빠지는 것은 아니다**: 3단은 2단이 낸
    기존 본문의 measurement를 `incumbent_measurements`로 받아 "아무것도 바꾸지
    않는" 지점을 지배 후보로 넣는다(`scoped_comparison` 참고). 여기서 빼는 것은
    **시뮬레이션**이지 **지점**이 아니다. `points`가 홀수면 로그 등간격의 가운뎃점이
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
    incumbent_measurements: dict,
    max_knobs: int | None,
    points: int,
    knob_names: list[tuple[str, str]] | None = None,
) -> StageResult:
    """3단: 범위 밝힌 비교와 파레토 거부.

    `knob_names`는 `max_knobs`와 나란한, **명명된** 좁히기다(Task 7이 남긴
    "max_knobs는 개수 절삭만 지원하고 이름 지정 선택은 없다"는 공백을 여기서
    채운다). `max_knobs`는 정렬 순서의 접두 N개를 자르므로 - 노브가 알파벳
    순으로 어디 있는지에 좌우된다 - "이 노브 하나가 후보를 이긴다"는 것을
    알고 있을 때 그 노브 하나만 정확히 스윕하고 싶다면 정렬 위치에 기대는
    것은 사실을 안다는 착각으로 우연히 맞아떨어지는 것이지 그 사실을 실제로
    쓰는 것이 아니다. `knob_names`가 주어지면 그 집합과 이 블록의 전체 노브
    인덱스의 **교집합**만 스윕한다(순서는 전체 인덱스의 정렬 순서를 그대로
    따른다 - 결정론). 교집합에서 빠진 나머지는 이유가 있는 좁히기이므로
    `max_knobs` 절삭과 같은 자리(`knobs_omitted`)에 이름이 남고,
    `detail["knob_names_requested"]`가 "이 실행은 특정 노브들로 명시적으로
    좁혀졌다"는 사실 자체를 - 몇 개가 잘렸는지가 아니라 무엇이 요청됐는지를
    - 별도로 기록한다(브리프 규칙 2: 좁혔다는 사실 자체가 리포트에 남아야
    한다). 요청됐지만 이 블록의 노브 인덱스에 없는 이름은 오탈자를 조용히
    삼키지 않도록 `knobs_unresolved`에 사유와 함께 남는다. `max_knobs`는
    named 선택 **다음에** 적용된다 - 두 좁히기가 같은 파이프라인에서 함께
    쓰일 수 있고, 순서는 "무엇을 볼지 정하고, 그중 몇 개까지 볼지 정한다"는
    자연스러운 순서다.

    기존 본문은 **그대로 둔 채**, 그 블록의 tunable 인덱스에서 노브
    **하나씩** `[baseline/M, baseline*M]`을 `points`점 로그 등간격으로 스윕한다
    (`M`은 에어리어 게이트가 그 파라미터에 허용하는 **성장** 배수 - 아래쪽
    끝이 왜 게이트의 경계가 아닌지는 `_sweep_values`의 docstring과
    `detail["sweep_bounds_note"]`에 있다). 한 스윕 지점의 변경은 정확히
    하나다 - 두 노브를 함께 움직이면 "이 노브 하나로 후보를 이긴다"는 주장이
    실제로는 "이 두 노브의 조합으로 이긴다"는 다른 주장이 되어, 이 게이트의
    정직성 근거가 깨진다.

    **지배 후보의 첫 번째는 "아무것도 바꾸지 않은" 지점, 즉 기존 본문 그
    자체다**(`incumbent_measurements` - 2단이 이미 잰 값이라 여기서 다시
    시뮬레이션하지 않는다). 그것이 존재하는 튜닝 중 **가장 싼 것**이고 어떤
    에어리어 허용 범위에도 자명하게 들어간다. 이 지점을 지배 후보에서 빼면,
    기존 본문보다 **모든 기준에서 더 나쁜** 후보가 "어떤 스윕 지점도 지배하지
    못했다"는 이유로 ADMIT된다 - 실측된 결함이었다(gain 후보 5.0 / 기존 10.0,
    스윕은 양 끝에서 1.5, `VERDICT: ADMIT`). 기록에서는 스윕 지점과 구별된다:
    `detail["incumbent_point"]`로 따로 남고, 지배했다면
    `dominating_point["point"] == "incumbent"`(`knob`/`swept_value`는 `None`)
    이다.

    거부 규칙은 파레토 지배다: 어떤 단일 지점(위 기존 본문 지점 또는 어떤
    스윕 지점)이 **모든 기준에서** 후보 이상이면(`_at_least_as_good`, 동률과
    허용오차 안의 근소차 포함) `REJECT`(`status="fail"`). 후보가 단 하나의
    기준에서라도 그 지점을 **의미 있게** 이기면 그 지점은 지배하지 못한다 -
    "모든 축에서 이겨야 입회"가 아니라 "하나의 축에서라도 후보가 앞서면
    생존"이다.

    **"이상"과 "앞선다"의 경계는 `COMPARISON_REL_TOLERANCE`(1e-3)의 상대
    허용오차다** - 그 상수의 주석에 이 값이 나온 실측이 있다. 허용오차가
    없으면 솔버 정밀도 수준의 차이가 양방향으로 거짓말을 한다(스윕 전 구간에서
    지배를 봉쇄하고, 동시에 근거 없는 `addresses`를 만든다). 적용된 값은
    `detail["tolerance"]`에 남는다.

    스펙의 어떤 기준이 이 비교 규칙으로 판정할 수 없는 연산자를 쓰면, 스윕을
    시작하지도 않고 `inconclusive`로 끝낸다 - `detail["unjudgeable_operators"]`
    에 기준 이름과 연산자를 적어서. "판정하지 못했다"를 "아무도 지배하지
    못했다"로 통과시키는 것이 이 게이트가 조용히 사라지는 방식이다.

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

    sweep_bounds_note = (
        "each knob was swept over [baseline/M, baseline*M], but the two ends do NOT "
        "have the same authority: M is the area gate's allowed GROWTH multiplier, so "
        "only baseline*M is the gate's own bound. area_limits.evaluate_area_growth "
        "short-circuits on `ratio <= 1.0`, i.e. the gate does not restrict shrinking "
        "at all - baseline/M is a SELF-IMPOSED symmetric bound of this stage. A tuner "
        "may legally set a parameter below baseline/M, and no such point was examined "
        "here; that omission narrows the sweep in the direction the gate does not "
        "restrict, which biases this stage toward ADMIT"
    )
    tolerance_note = (
        f"a difference within {COMPARISON_REL_TOLERANCE:g} relative to the value it is "
        "compared against counts as a tie in BOTH directions: it is not an improvement "
        "(so it cannot manufacture an `addresses` claim) and it does not block "
        "domination (so solver noise cannot rescue a candidate). See "
        "curation.COMPARISON_REL_TOLERANCE for the measured numbers this was set from"
    )

    unjudgeable = _unjudgeable_operators(criteria)
    if unjudgeable:
        return StageResult(
            name="comparison",
            status="inconclusive",
            detail={
                "topology_id": candidate.topology_id,
                "block_path": slot.block_path,
                "tolerance": COMPARISON_REL_TOLERANCE,
                "tolerance_note": tolerance_note,
                "sweep_bounds_note": sweep_bounds_note,
                "unjudgeable_operators": unjudgeable,
                "why": (
                    "this stage's Pareto rule cannot judge operator(s) "
                    f"{sorted({entry['operator'] for entry in unjudgeable})} used by criteria "
                    f"{[entry['criterion'] for entry in unjudgeable]}, so no sweep was run - "
                    "'could not judge' is a different fact from 'nothing dominated the "
                    "candidate', and reporting the latter would let the gate pass silently"
                ),
                "knobs_swept": [],
                "knobs_omitted": [],
                "knobs_unresolved": [],
                "excluded_points": [],
                "simulation_count": 0,
                "best_per_criterion": {},
                "dominating_point": None,
                "incumbent_point": None,
            },
        )

    canonical = slot.spec.canonical
    canonical_text = netlist_texts[canonical.name]

    all_knobs = _block_tunable_knobs(canonical_text, slot.spec.circuit_name, slot.block_path)

    knobs = all_knobs
    knobs_omitted: list[str] = []
    knobs_unresolved: list[dict] = []
    knob_names_requested: list[str] | None = None
    if knob_names is not None:
        wanted = set(knob_names)
        knobs = [k for k in all_knobs if k in wanted]
        knobs_omitted = [f"{refdes}.{param}" for refdes, param in all_knobs if (refdes, param) not in wanted]
        found = set(knobs)
        knobs_unresolved = [
            {
                "knob": f"{refdes}.{param}",
                "reason": "explicitly requested via knob_names but not present in this block's tunable index",
            }
            for refdes, param in knob_names
            if (refdes, param) not in found
        ]
        knob_names_requested = [f"{refdes}.{param}" for refdes, param in knob_names]

    if max_knobs is not None and len(knobs) > max_knobs:
        knobs_omitted = knobs_omitted + [f"{refdes}.{param}" for refdes, param in knobs[max_knobs:]]
        knobs = knobs[:max_knobs]

    baseline_components = index_baseline_components(canonical_text)

    candidate_eval = evaluate_criteria(candidate_measurements, criteria)
    candidate_by_name = {r["name"]: r for r in candidate_eval["criteria"]}
    # 후보 쪽 measurement 자체가 불완전하면(정상 경로에서는 2단이 이미 이를
    # 막는다) 어느 지점도 "지배"로 판정하지 않는다 - 방어적 조치일 뿐, 이
    # 저장소의 정상 파이프라인이 도달하는 경로는 아니다.
    candidate_incomplete = any(math.isnan(r["actual"]) for r in candidate_by_name.values())

    knobs_swept: list[dict] = []
    # knobs_unresolved may already carry entries from a knob_names request
    # that named a (refdes, param) absent from this block's tunable index -
    # this loop only ever appends to it, never resets it, so that fact
    # survives alongside whatever the sweep itself finds unresolved.
    excluded_points: list[dict] = []
    best_per_criterion: dict[str, dict] = {}
    dominating_point: dict | None = None
    simulation_count = 0

    def _record_best(by_name: dict, knob: str | None, swept_value: float | None, point_kind: str) -> None:
        for c in criteria:
            actual = by_name[c.name]["actual"]
            if math.isnan(actual):
                continue
            current = best_per_criterion.get(c.name)
            if current is None or _is_better(c.operator, actual, current["value"]):
                best_per_criterion[c.name] = {
                    "value": actual,
                    "knob": knob,
                    "swept_value": swept_value,
                    "point": point_kind,
                }

    def _dominates(by_name: dict) -> bool:
        return all(
            _at_least_as_good(c.operator, by_name[c.name]["actual"], candidate_by_name[c.name]["actual"])
            for c in criteria
        )

    # 지배 후보 0번: 아무것도 바꾸지 않은 지점 = 기존 본문 그 자체. 2단이 이미
    # 잰 값이므로 시뮬레이션을 쓰지 않는다(그래서 simulation_count에 더하지
    # 않는다). 스윕 지점과 구별되도록 knob/swept_value는 None이고 "point":
    # "incumbent" 라벨을 단다.
    incumbent_eval = evaluate_criteria(incumbent_measurements, criteria)
    incumbent_by_name = {r["name"]: r for r in incumbent_eval["criteria"]}
    incumbent_missing = sorted(name for name, r in incumbent_by_name.items() if math.isnan(r["actual"]))
    incumbent_point: dict = {
        "point": "incumbent",
        "label": INCUMBENT_POINT_LABEL,
        "knob": None,
        "swept_value": None,
        "measurements": dict(incumbent_measurements),
        "simulated_here": False,
        "source": (
            "stage 2 (reproduce)'s own baseline simulation - re-simulating the "
            "unchanged deck here would measure the same point twice"
        ),
        "excluded": bool(incumbent_missing),
        "excluded_reason": f"missing measurements: {incumbent_missing}" if incumbent_missing else None,
    }
    _record_best(incumbent_by_name, None, None, "incumbent")
    if incumbent_missing:
        excluded_points.append(
            {
                "point": "incumbent",
                "label": INCUMBENT_POINT_LABEL,
                "knob": None,
                "swept_value": None,
                "reason": f"missing measurements: {incumbent_missing}",
            }
        )
    elif not candidate_incomplete and _dominates(incumbent_by_name):
        dominating_point = {
            "point": "incumbent",
            "label": INCUMBENT_POINT_LABEL,
            "knob": None,
            "swept_value": None,
            "measurements": dict(incumbent_measurements),
        }

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
                    {
                        "point": "swept",
                        "knob": knob_name,
                        "swept_value": value,
                        "reason": f"simulation failed: {error}",
                    }
                )
                continue

            point_eval = evaluate_criteria(measurements, criteria)
            point_by_name = {r["name"]: r for r in point_eval["criteria"]}
            missing = sorted(name for name, r in point_by_name.items() if math.isnan(r["actual"]))

            _record_best(point_by_name, knob_name, value, "swept")

            if missing:
                excluded_points.append(
                    {
                        "point": "swept",
                        "knob": knob_name,
                        "swept_value": value,
                        "reason": f"missing measurements: {missing}",
                    }
                )
                continue

            if dominating_point is None and not candidate_incomplete and _dominates(point_by_name):
                dominating_point = {
                    "point": "swept",
                    "knob": knob_name,
                    "swept_value": value,
                    "measurements": dict(measurements),
                }

    status = "fail" if dominating_point is not None else "pass"
    detail = {
        "topology_id": candidate.topology_id,
        "block_path": slot.block_path,
        "tolerance": COMPARISON_REL_TOLERANCE,
        "tolerance_note": tolerance_note,
        "sweep_bounds_note": sweep_bounds_note,
        "incumbent_point": incumbent_point,
        "knob_names_requested": knob_names_requested,
        "knobs_swept": knobs_swept,
        "knobs_omitted": knobs_omitted,
        "knobs_unresolved": knobs_unresolved,
        "excluded_points": excluded_points,
        "simulation_count": simulation_count,
        "best_per_criterion": best_per_criterion,
        "dominating_point": dominating_point,
    }
    return StageResult(name="comparison", status=status, detail=detail)
