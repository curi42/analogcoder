"""`analogcoder-curate` 진입점.

`curation.py`가 낸 네 단계(구조 -> 재현 -> 코너(저술본만) -> 범위 밝힌 비교)를
고정 순서로 돌리고, 통과했으면 `agents.curator.render_description`으로
`description`을 렌더링한 뒤, 산출물 셋(`curation_report.md`,
`topology_candidate.py`, `curation.json`)을 쓴다. `docs/superpowers/specs/
2026-07-28-topology-curation-design.md`의 "CLI"/"에러 처리" 절이 이 모듈의
계약이다.

**이 실행은 라이브러리를 바꾸지 않는다.** `topologies.py`/`TOPOLOGY_LIBRARY`를
읽지도 쓰지도 않는다 - 산출물 `topology_candidate.py`는 사람이 검토하고 손으로
붙여 넣을 스니펫일 뿐이다(설계 문서 "왜 사람이 커밋하는가").

**이 실행은 절대 트레이스백으로 끝나지 않는다.** `run_curation`은
`run_optimization`의 가드와 같은 모양 - 파이프라인 전체(`_curate`)를
`try/except Exception`으로 감싸고, 무엇이 터지든 지금까지 쌓인 부분 결과
(`_RunContext`)로 `INCONCLUSIVE`를 만들어 돌려준다. `REJECT`는 오직 게이트
단계 하나가 `status == "fail"`을 낼 때만 나온다 - "이 회로가 나쁘다"(측정된
사실)와 "재보지 못했다"(예외/inconclusive)를 섞지 않는다는 이 저장소의 규칙
그대로다."""

import argparse
import asyncio
import json
import logging
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from analogcoder.agents.backend import AgentBackend
from analogcoder.agents.backends.claude_sdk import DEFAULT_CLAUDE_MODEL, ClaudeSDKBackend
from analogcoder.agents.backends.openai_compatible import OpenAICompatibleBackend
from analogcoder.agents.curator import render_description
from analogcoder.curation import (
    Candidate,
    Slot,
    StageResult,
    author_and_verify_variant,
    candidate_from_deck,
    candidate_from_file,
    check_structure,
    reproduce_characteristics,
    scoped_comparison,
    verify_corners,
)
from analogcoder.netlist import (
    all_model_names,
    extract_subckt_body,
    netlist_scale,
    parse_netlist,
    resolve_includes,
)
from analogcoder.simulators.base import SimulatorBackend
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import TargetSpec, load_spec
from analogcoder.structure import derive_structure

logger = logging.getLogger(__name__)

# 3단(범위 밝힌 비교)의 실측 비용: bandgap 루프 테스트벤치에서 AC 스윕 하나짜리
# 시뮬이 0.93초(설계 문서 "비용" 절). 다중 테스트벤치 슬롯의 예상 시뮬 횟수/시간을
# 실행 시작 시 로그로 내는 데만 쓰는 대략치이지, 어떤 게이트의 판정에도 쓰이지
# 않는다.
_SEC_PER_SIMULATION = 0.93

# 3단의 기본값.
#
# **`--max-knobs`의 기본값은 없다(절삭 없음).** 예전 기본값 8은 "보수적으로
# 잡는다"는 규칙을 비용에만 적용하고 이 게이트의 존재 이유 - 정직한 비교 -
# 에는 적용하지 않은 것이었다. 실측: 출하된 Ahuja 실행에서 지배 노브
# `TRIMAMP.XRz.l`은 30개 노브를 알파벳 정렬했을 때 정확히 **9번째**(인덱스 8)
# 라 `--max-knobs 8`에 잘려 나갔고, 같은 실행이 16회 시뮬 뒤 **ADMIT**으로
# 끝났다. 잘린 이름이 `knobs_omitted`에 남으므로 조용하지는 않았지만,
# **판정을 결정하는 노브에 닿을 수 없는 기본값**은 그 사실만으로 나쁜
# 기본값이다 - 게다가 어느 노브가 결정적인지는 실행 전에 알 수 없으므로
# "알면 `--knobs`로 좁혀라"는 답이 되지 못한다.
#
# 비용은 감당 가능하다: 큐레이션은 오프라인이고 후보당 한 번 돈다. 단일
# 테스트벤치 슬롯에서 30노브 x 5점 = 150회 ≈ **2.5분**(측정된 0.93초/시뮬).
# `--max-knobs`는 그대로 남아 있되 **옵트인 속도 상한**이 된다.
DEFAULT_MAX_KNOBS = None

# `--points`의 기본값이 3이면 실제로 도는 지점은 **양 끝 두 개뿐**이다:
# 로그 등간격 홀수 격자의 가운뎃점은 기하평균 `sqrt((b/M)*(b*M)) == b`, 즉
# 기준선 그 자체이고 `_sweep_values`가 그것을 뺀다(2단이 이미 쟀고, 3단은
# 그 값을 무변경 지점으로 공짜로 갖는다). 그래서 `_sweep_values(10, 3, 3,
# False) == [3.33, 30.0]`으로 `points=2`와 완전히 같았다 - 기본 실행이 범위의
# **내부를 한 번도 보지 않았다**.
#
# 5로 올린다. 홀수를 유지하는 이유는 격자가 기준선을 중심으로 대칭이기
# 때문이고(지수 -1, -1/2, 0, +1/2, +1), 가운뎃점이 빠지므로 실제 시뮬은
# 노브당 4회다: `b/M`, `b/sqrt(M)`, `b*sqrt(M)`, `b*M` - 양 끝과 내부를 모두
# 본다. Ahuja 증명이 실제로 쓴 값이기도 하다.
DEFAULT_POINTS = 5


# --- 인자 파싱 --------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="analogcoder-curate")

    # 소스 A - 이미 검증된 덱의 블록에서 추출.
    parser.add_argument("--from-deck", default=None, help="source A: path to a deck to extract a block from")
    parser.add_argument("--from-block", default=None, help="source A: block path within --from-deck")

    # 소스 B - 완성된 SPICE 조각 파일.
    parser.add_argument("--from-body", default=None, help="source B: path to a file containing a SPICE body")
    parser.add_argument("--ports", default=None, help="source B: space-separated port list, e.g. 'vinp vinn vout vdd vss'")
    parser.add_argument("--assumes-scale", type=float, default=None, help="source B: the body's assumed .option scale")

    # 소스 C - 기법 이름으로 슬롯의 기존 본문을 국소 수정.
    parser.add_argument("--technique", default=None, help="source C: technique name/description for the variant author agent")

    parser.add_argument("--slot-spec", required=True, help="spec.yaml declaring the slot this candidate competes in")
    parser.add_argument("--slot-block", required=True, help="block path within the slot's netlists this candidate would replace")
    parser.add_argument("--id", dest="topology_id", required=True, help="topology_id for the candidate")
    parser.add_argument("--out-dir", required=True, help="directory to write the three artifacts into")

    parser.add_argument(
        "--max-knobs",
        type=int,
        default=DEFAULT_MAX_KNOBS,
        help=(
            "stage 3: OPT-IN speed cap on how many of the block's own knobs are swept "
            "(alphabetical prefix). Off by default - by default every knob of the block "
            "is swept, because a count-based cut is decided by alphabetical order and "
            "the deciding knob is not knowable before the run"
        ),
    )
    parser.add_argument("--points", type=int, default=DEFAULT_POINTS, help="stage 3: how many log-spaced points per knob")
    parser.add_argument(
        "--knobs",
        default=None,
        help=(
            "stage 3: comma-separated 'refdes.param' list to restrict the sweep to "
            "(e.g. 'TRIMAMP.XRz.l,TRIMAMP.Xcc.W') - a named narrowing alongside "
            "--max-knobs's count-based one (see scoped_comparison's docstring); "
            "applied before --max-knobs's cap"
        ),
    )

    parser.add_argument("--simulator", choices=["ngspice"], default="ngspice")
    parser.add_argument("--agent-backend", choices=["claude", "openai-compatible"], default="claude")
    parser.add_argument("--llm-base-url", default=None)
    parser.add_argument("--llm-model", default=None)
    parser.add_argument("--claude-model", default=DEFAULT_CLAUDE_MODEL)

    return parser


def _build_agent_backend(args) -> AgentBackend:
    """`cli.py`의 `_build_agent_backend`와 같은 규칙. 이 파이프라인은 에이전트
    호출이 둘뿐이고(소스 C의 저술, description 렌더링) 둘 다 같은 백엔드 하나면
    충분하므로 - `cli.py`처럼 에이전트별로 다른 모델을 얹는 `--agent-model`은
    두지 않는다(YAGNI - 그 기능이 필요해지면 그때 추가한다)."""
    if args.agent_backend == "claude":
        return ClaudeSDKBackend(model=args.claude_model)
    if not args.llm_base_url or not args.llm_model:
        raise ValueError("--llm-base-url and --llm-model are required when --agent-backend=openai-compatible")
    return OpenAICompatibleBackend(base_url=args.llm_base_url, api_key_env="LOCAL_LLM_API_KEY", model=args.llm_model)


def _validate_source_flags(args) -> str:
    """`--from-deck`/`--from-body`/`--technique` 중 정확히 하나가 주어졌는지
    확인하고, 그것을 소스 문자열("deck"/"body"/"technique")로 돌려준다.

    이 확인은 `_curate` 안, 파이프라인의 첫 줄에서 돈다 - `main()`이 따로
    걸러내지 않는다. 그래서 여기서 던지는 `ValueError`는 다른 모든 파이프라인
    예외와 똑같이 `run_curation`의 가드를 거쳐 `INCONCLUSIVE` + 산출물 셋으로
    끝난다("어느 단계에서 무엇이 잘못됐든" 이 잘못까지 포함한다) - 별도의
    크래시 경로를 두지 않는다."""
    given = [name for name, value in (("from_deck", args.from_deck), ("from_body", args.from_body), ("technique", args.technique)) if value]
    if len(given) != 1:
        raise ValueError(
            f"exactly one of --from-deck, --from-body, --technique is required; got {given or 'none'}"
        )
    source = given[0]
    if source == "from_deck":
        if not args.from_block:
            raise ValueError("--from-block is required together with --from-deck")
        return "deck"
    if source == "from_body":
        missing = [n for n, v in (("--ports", args.ports), ("--assumes-scale", args.assumes_scale)) if v is None]
        if missing:
            raise ValueError(f"{', '.join(missing)} required together with --from-body")
        return "body"
    return "technique"


def _read_text(path: str) -> str:
    with open(path) as f:
        return f.read()


def _parse_knob_names(raw: str | None) -> list[tuple[str, str]] | None:
    """`--knobs` 인자를 `scoped_comparison`의 `knob_names` 모양(list[tuple[refdes,
    param]])으로 판다. `rsplit(".", 1)`인 이유는 refdes 자체가 스코프 경로라
    점을 품을 수 있어서다(`OUTER.INNER.XRz`) - 마지막 점만 param과의 경계이고,
    그 앞은 전부 refdes다. `None`이면(플래그 생략) `None`을 그대로 돌려 이
    좁히기가 아예 요청되지 않았다는 사실을 보존한다 - 빈 리스트는 다른 사실
    ("스윕할 노브가 0개로 좁혀졌다")이다."""
    if raw is None:
        return None
    result: list[tuple[str, str]] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if "." not in token:
            raise ValueError(f"--knobs entry {token!r} is not in 'refdes.param' form")
        refdes, param = token.rsplit(".", 1)
        result.append((refdes, param))
    return result


# --- 다중 테스트벤치 슬롯 비용 추정(로그 전용) -------------------------------


def _block_knob_count(netlist_text: str, circuit_name: str, block_path: str) -> int:
    """이 블록 스코프 안의 (refdes, param) 노브 개수 - `curation._block_tunable_
    knobs`와 같은 필터(스코프 접두 일치)를 쓰지만, 이것은 어떤 게이트의 판정도
    아니고 실행 시작 시 로그 한 줄을 위한 추정치일 뿐이다. 실제 3단
    (`scoped_comparison`)은 이 값을 참조하지 않고 스스로 다시 계산한다 - 이중
    판정이 아니라 로그와 게이트가 서로 다른 이유로 같은 사실을 각자 구한다."""
    structure = derive_structure(netlist_text, circuit_name)
    prefix = f"{block_path}."
    return len(
        {
            (entry.refdes, entry.param)
            for entry in structure.tunable
            if entry.refdes == block_path or entry.refdes.startswith(prefix)
        }
    )


def estimate_curation_cost(
    spec: TargetSpec,
    netlist_texts: dict[str, str],
    block_path: str,
    max_knobs: int | None,
    points: int,
    knob_names: list[tuple[str, str]] | None = None,
) -> dict:
    """다중 테스트벤치 슬롯의 예상 시뮬 횟수/시간. 2단(재현)은 후보/기존 각
    한 번씩 x 테스트벤치 수, 3단(비교)은 스윕 노브 수 x points x 테스트벤치
    수(설계 문서 "비용" 절의 곱셈 그대로). 2.5단(코너)은 저술본에만 붙고 이
    시점에는 소스가 무엇이 될지 이미 알려져 있지만(호출부가 안다), 이 함수는
    소스에 무관하게 같은 추정을 내도록 코너 비용을 아예 포함하지 않는다 -
    코너 스윕 자체의 비용은 설계 문서에 이미 별도로 측정돼 있고("18초/90초"),
    이 로그가 노리는 위험은 오직 3단의 테스트벤치 곱셈이다.

    `knob_names`(명명된 좁히기, `scoped_comparison`과 같은 뜻)가 주어지면
    `swept_knob_count`(스윕될 노브 수, 비용 곱셈에 실제로 쓰이는 값)의
    상한을 이 블록의 전체 노브 개수가 아니라 **요청된 이름과 전체 노브
    인덱스의 교집합 크기**로 잡는다 - `scoped_comparison` 자신이 적용하는
    것과 같은 교집합 규칙이다. `knob_count`(이 블록에 존재하는 전체 노브
    수)는 그대로 둔다 - `knob_names`는 "무엇을 볼지"를 좁힐 뿐 "무엇이
    있는지"를 바꾸지 않으므로, 둘을 같이 덮어쓰면 "전체 30개 중 1개로
    좁혔다"는 사실 자체가 로그에서 사라진다. 이 함수는 로그 전용 추정치이지
    게이트가 아니므로, 요청된 이름이 실제로 이 블록에 없어
    `knobs_unresolved`로 빠지는 경우까지 추정에 반영할 필요는 없다 - 그
    경우 실제 스윕은 이 추정보다 더 적게 돌 뿐이고, 이 로그가 막으려는 것은
    과소 추정이 아니라 과대 추정이다. `knob_names`가 없으면(기본값 `None`)
    이전과 동일하게 `max_knobs`만으로 추정한다 - 이 인자를 생략한 모든 기존
    호출은 동작이 그대로다."""
    canonical_text = netlist_texts[spec.canonical.name]
    knob_count = _block_knob_count(canonical_text, spec.circuit_name, block_path)
    swept_upper_bound = knob_count
    if knob_names is not None:
        # Deliberately re-derives the block's own knob set here rather than
        # importing curation._block_tunable_knobs - same reasoning as
        # _block_knob_count above (this log and the stage 3 gate derive the
        # same fact independently, for different reasons).
        structure = derive_structure(canonical_text, spec.circuit_name)
        prefix = f"{block_path}."
        all_knobs = {
            (entry.refdes, entry.param)
            for entry in structure.tunable
            if entry.refdes == block_path or entry.refdes.startswith(prefix)
        }
        swept_upper_bound = len(all_knobs & set(knob_names))
    swept_knob_count = swept_upper_bound if max_knobs is None else min(swept_upper_bound, max_knobs)
    testbench_count = len(spec.testbenches)
    stage2_simulations = 2 * testbench_count
    stage3_simulations = swept_knob_count * points * testbench_count
    total_simulations = stage2_simulations + stage3_simulations
    return {
        "testbench_count": testbench_count,
        "knob_count": knob_count,
        "swept_knob_count": swept_knob_count,
        "points": points,
        "stage2_simulations": stage2_simulations,
        "stage3_simulations": stage3_simulations,
        "total_simulations": total_simulations,
        "estimated_seconds": round(total_simulations * _SEC_PER_SIMULATION, 1),
    }


def _log_expected_cost(
    spec: TargetSpec,
    netlist_texts: dict[str, str],
    block_path: str,
    max_knobs: int | None,
    points: int,
    knob_names: list[tuple[str, str]] | None = None,
) -> None:
    cost = estimate_curation_cost(spec, netlist_texts, block_path, max_knobs, points, knob_names)
    logger.info(
        "multi-testbench slot (%d testbenches): expected ~%d simulations (~%.1fs) for stages 2+3 alone - %s",
        cost["testbench_count"],
        cost["total_simulations"],
        cost["estimated_seconds"],
        cost,
    )


# --- 파이프라인 --------------------------------------------------------------


@dataclass
class _RunContext:
    """`_curate`가 진행하며 채우는 부분 결과. `run_curation`의 예외 가드가
    (무엇이 어디서 터지든) 이 시점까지 쌓인 사실만으로 `INCONCLUSIVE`를 만들
    수 있어야 하므로, 새 지역 변수를 쌓는 대신 이 객체 하나를 계속 갱신한다."""

    source: str | None = None
    slot: Slot | None = None
    candidate: Candidate | None = None
    addresses: list[str] = field(default_factory=list)
    stages: list[StageResult] = field(default_factory=list)
    # 소스 C 에이전트가 낸 설명. 어떤 게이트도 읽지 않지만(그래서 스키마에서
    # optional이다) 버리지도 않는다 - 산출물까지 실어 사람이 읽게 한다.
    rationale: str | None = None


def _stage_fail_reason(name: str, stage: StageResult) -> str:
    """게이트 단계 하나가 `status == "fail"`일 때 리포트/최종 사유에 실을
    문장. `curation._stage_rejection_reason`과 같은 규칙(단계가 이미 낸 사실을
    새 문장 없이 이어붙인다)이지만, 그것은 1·2단만 다루는 재시도 루프 전용이라
    2.5·3단까지 포함하는 이 CLI 전용 버전이 따로 필요하다."""
    if name == "structure":
        return f"stage 1 (structure) rejected: {stage.detail.get('reason')}: {stage.detail.get('detail')}"
    if name == "reproduce":
        return f"stage 2 (reproduce) rejected: missing measurements: {stage.detail.get('missing')}"
    if name == "corners":
        # `verify_corners`에는 실패 분기가 **둘**이다. 이 함수는 두 번째(요구 2,
        # 최악 코너 비교)만 알고 있어서, 첫 번째(요구 1, 어느 코너에서 어떤
        # 기준의 measurement가 빠짐)로 거부된 실행이 `detail`에 `missing=['gain']`
        # 을 담은 채 사유 문자열로는 "worse at worst-case corner on: None"을
        # 냈다 - 재보지도 않은 최악 코너 회귀를 **단언**하고, 기준 이름은 하나도
        # 대지 않으며, 빠진 값이 **기존 본문 쪽**이었을 가능성을 감춘다. 그
        # 문자열은 리포트의 헤드라인 `**Reason:**`이자 `curation.json`의
        # `reason`이다. 한 줄 위 `reproduce` 분기는 이미 `missing`을 옳게 읽으므로
        # 이것은 누락이었다.
        missing = stage.detail.get("missing")
        if missing:
            return (
                "stage 2.5 (corners) rejected: requirement 1 (every criterion produces a "
                f"measurement at every corner) failed - missing: {missing} "
                f"(candidate side: {stage.detail.get('missing_candidate')}, "
                f"incumbent side: {stage.detail.get('missing_baseline')})"
            )
        return f"stage 2.5 (corners) rejected: worse at worst-case corner on: {stage.detail.get('worse')}"
    if name == "comparison":
        return f"stage 3 (comparison) rejected: dominated by {stage.detail.get('dominating_point')}"
    raise RuntimeError(f"unknown stage name {name!r} - this should be unreachable")


def _reproduce_measurements(stages: list[StageResult]) -> tuple[dict, dict]:
    """이미 통과한 2단(`reproduce`) StageResult에서 후보 쪽과 **기존 본문 쪽**
    measurement를 함께 되찾는다 - 3단(`scoped_comparison`)이 요구하는
    `candidate_measurements`/`incumbent_measurements` 두 인자다. 2단을 다시
    시뮬레이션하지 않는다.

    기존 본문 쪽을 함께 넘기는 것이 3단의 정확성 요건이다: 그것이 "아무것도
    바꾸지 않는" 지점, 즉 존재하는 튜닝 중 가장 싼 것이고, 그 지점을 지배
    후보에서 빼면 기존 본문보다 모든 기준에서 더 나쁜 후보가 ADMIT된다
    (`scoped_comparison`의 docstring에 실측이 있다). 2단은 이미 두 덱을 다
    시뮬레이션했으므로 여기서 새로 드는 비용은 없다."""
    for stage in stages:
        if stage.name == "reproduce":
            return stage.detail["candidate_measurements"], stage.detail["baseline_measurements"]
    raise RuntimeError("no 'reproduce' stage recorded before scoped_comparison - this should be unreachable")


def _verified_at(stages: list[StageResult]) -> str:
    """`Topology.verified_at`에 실을 값 - 이 실행이 실제로 코너를 통과시켰으면
    (2.5단이 `status == "pass"`) `"corners"`, 아니면 `"nominal"`.

    추출본/파일 제출본은 2.5단이 `"skipped"`로 끝나므로(코너 검증은 저술본
    전용) 이 실행 자체는 코너를 하나도 재지 않았고, 그래서 `"nominal"`이다 -
    그 덱의 원 출처가 코너를 통과했을 수도 있다는 것과, **이 파이프라인이**
    그것을 확인했다는 것은 다른 사실이다. 저술본이 ADMIT까지 온다는 것은
    2.5단이 실제로 `"pass"`를 냈다는 뜻이므로 `"corners"`다."""
    for stage in stages:
        if stage.name == "corners" and stage.status == "pass":
            return "corners"
    return "nominal"


def _finalize(ctx: _RunContext, verdict: str, reason: str, description: str = "", description_source: str = "not_reached") -> dict:
    return {
        "verdict": verdict,
        "reason": reason,
        "source": ctx.source,
        "block_path": ctx.slot.block_path if ctx.slot is not None else None,
        "verified_at": _verified_at(ctx.stages),
        "stages": list(ctx.stages),
        "addresses": list(ctx.addresses),
        "rationale": ctx.rationale,
        "candidate": ctx.candidate,
        "description": description,
        "description_source": description_source,
    }


def _comparison_scope_text(detail: dict) -> str:
    """3단 detail을 사람이 읽는 한 줄로 - description 렌더링 프롬프트에 실을
    `comparison_scope` 사실(측정된 것만, LLM의 주장이 아니다).

    `knob_names_requested`가 있으면(명명된 좁히기, `max_knobs`와 나란한
    선택지 - `scoped_comparison`의 docstring 참고) 그 사실 자체를 앞에
    적는다 - "몇 개를 스윕했는가"만으로는 "이 실행이 특정 노브로 의도적으로
    좁혀졌다"는 사실이 사라진다.

    **판정은 `is not None`이지 truthiness가 아니다.** `--knobs ""`는 `[]`로
    파싱되고(그 구별은 `_parse_knob_names`의 docstring이 일부러 보존한다),
    `if requested:`는 그 빈 리스트를 "요청 없음"과 똑같이 버려서 리포트가
    "0 knob(s) swept (none)"만 말하고 좁히기가 요청됐다는 사실은 사라졌다.
    `--max-knobs 0`/`--points 0`도 같은 모양이고 파서는 둘 다 받아들이므로,
    셋 다 명시적으로 적는다."""
    knobs = detail.get("knobs_swept", [])
    knob_names = [k["knob"] for k in knobs]
    narrowings: list[str] = []

    requested = detail.get("knob_names_requested")
    if requested is not None:
        narrowings.append(f"--knobs restricted the sweep to {requested or 'NOTHING (an empty list was requested)'}")

    max_knobs = detail.get("max_knobs_requested")
    if max_knobs is not None:
        narrowings.append(f"--max-knobs capped the sweep at {max_knobs} knob(s)")

    prefix = f"scope narrowed: {'; '.join(narrowings)} - " if narrowings else ""
    # `points`는 좁히기가 아니라 언제나 참인 범위 사실이므로 접두가 아니라
    # 본문에 적는다 - 다만 0이면 어떤 노브도 실제로는 시뮬레이션되지 않으므로
    # 그것은 명시적으로 말한다.
    points = detail.get("points_requested")
    points_text = f", {points} point(s) requested per knob" if points is not None else ""
    if points == 0:
        points_text += " (zero - no sweep point was simulated for any knob)"
    return (
        f"{prefix}{len(knobs)} knob(s) swept ({', '.join(knob_names) if knob_names else 'none'})"
        f"{points_text}, {detail.get('simulation_count', 0)} simulation(s) total"
    )


async def _curate(args, sim_backend: SimulatorBackend, agent_backend: AgentBackend, ctx: _RunContext) -> dict:
    ctx.source = _validate_source_flags(args)
    knob_names = _parse_knob_names(getattr(args, "knobs", None))

    spec = load_spec(args.slot_spec)
    netlist_texts: dict[str, str] = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            netlist_texts[tb.name] = resolve_includes(f.read(), os.path.dirname(tb.netlist_path))
    ctx.slot = Slot(spec=spec, block_path=args.slot_block)

    # 브리프 규칙 5: 다중 테스트벤치 슬롯이면 시작 시 예상 시뮬 횟수/시간을
    # 로그로 낸다. 판정에는 아무 영향도 주지 않는다 - 순수한 기록이다.
    if len(spec.testbenches) > 1:
        _log_expected_cost(spec, netlist_texts, ctx.slot.block_path, args.max_knobs, args.points, knob_names)

    if ctx.source == "technique":
        base_text = netlist_texts[spec.canonical.name]
        parsed = parse_netlist(base_text)
        if ctx.slot.block_path not in parsed.subckts:
            raise ValueError(f"subckt {ctx.slot.block_path!r} not found in the slot's canonical netlist")
        subckt = parsed.subckts[ctx.slot.block_path]
        base_body = extract_subckt_body(base_text, ctx.slot.block_path)

        variant = await author_and_verify_variant(
            base_body=base_body,
            technique=args.technique,
            ports=list(subckt.ports),
            available_models=all_model_names(parsed),
            scale=netlist_scale(base_text),
            topology_id=args.topology_id,
            slot=ctx.slot,
            netlist_texts=netlist_texts,
            sim_backend=sim_backend,
            backend=agent_backend,
        )
        ctx.rationale = variant.rationale
        if variant.structure is not None:
            ctx.stages.append(variant.structure)
        if variant.reproduce is not None:
            ctx.stages.append(variant.reproduce)
        if variant.verdict != "PASS":
            # `VariantAuthorResult.verdict`는 "PASS"/"REJECT"/"INCONCLUSIVE" -
            # 마지막 둘은 이 파이프라인의 최종 판정 문자열과 그대로 같다.
            return _finalize(ctx, verdict=variant.verdict, reason=variant.reason or "source C's author/retry loop did not produce a passing candidate")
        ctx.candidate = variant.candidate
        ctx.addresses = variant.addresses
    else:
        if ctx.source == "deck":
            candidate = candidate_from_deck(_read_text(args.from_deck), args.from_block, args.topology_id)
        else:
            candidate = candidate_from_file(_read_text(args.from_body), args.ports.split(), args.assumes_scale, args.topology_id)
        ctx.candidate = candidate

        structure_result = check_structure(candidate, ctx.slot, netlist_texts)
        ctx.stages.append(structure_result)
        if structure_result.status != "pass":
            return _finalize(ctx, verdict="REJECT", reason=_stage_fail_reason("structure", structure_result))

        reproduce_result, addresses = reproduce_characteristics(candidate, ctx.slot, netlist_texts, sim_backend)
        ctx.stages.append(reproduce_result)
        if reproduce_result.status == "inconclusive":
            return _finalize(
                ctx,
                verdict="INCONCLUSIVE",
                reason=reproduce_result.detail.get("error", "stage 2 (reproduce) was inconclusive"),
            )
        if reproduce_result.status != "pass":
            return _finalize(ctx, verdict="REJECT", reason=_stage_fail_reason("reproduce", reproduce_result))
        ctx.addresses = addresses

    corners_result = verify_corners(ctx.candidate, ctx.slot, netlist_texts, sim_backend, ctx.addresses)
    ctx.stages.append(corners_result)
    if corners_result.status == "inconclusive":
        return _finalize(
            ctx,
            verdict="INCONCLUSIVE",
            reason=corners_result.detail.get("why", "stage 2.5 (corners) was inconclusive"),
        )
    if corners_result.status == "fail":
        return _finalize(ctx, verdict="REJECT", reason=_stage_fail_reason("corners", corners_result))

    candidate_measurements, incumbent_measurements = _reproduce_measurements(ctx.stages)
    comparison_result = scoped_comparison(
        ctx.candidate,
        ctx.slot,
        netlist_texts,
        sim_backend,
        candidate_measurements,
        incumbent_measurements,
        args.max_knobs,
        args.points,
        knob_names=knob_names,
    )
    ctx.stages.append(comparison_result)
    if comparison_result.status == "fail":
        return _finalize(ctx, verdict="REJECT", reason=_stage_fail_reason("comparison", comparison_result))
    if comparison_result.status == "inconclusive":
        # "판정하지 못했다"는 "아무도 후보를 지배하지 못했다"가 아니다 - 3단이
        # 자기 비교 규칙으로 판정할 수 없는 연산자를 만나면 그대로 INCONCLUSIVE로
        # 끝나야 하고, ADMIT으로 넘어가서는 안 된다.
        return _finalize(
            ctx,
            verdict="INCONCLUSIVE",
            reason=comparison_result.detail.get("why", "stage 3 (comparison) was inconclusive"),
        )

    facts = {
        "topology_id": ctx.candidate.topology_id,
        "block_path": ctx.slot.block_path,
        "ports": ctx.candidate.ports,
        "addresses": ctx.addresses,
        "comparison_scope": _comparison_scope_text(comparison_result.detail),
    }
    description, description_source = await render_description(facts, agent_backend)

    return _finalize(ctx, verdict="ADMIT", reason="all stages passed", description=description, description_source=description_source)


async def run_curation(args, sim_backend: SimulatorBackend | None = None, agent_backend: AgentBackend | None = None) -> dict:
    """`_curate`를 가드로 감싼다 - `run_optimization`과 같은 이유(모듈
    docstring). 무엇이 터지든 `INCONCLUSIVE` + 지금까지 쌓인 `_RunContext`로
    끝나고, 이 함수 자체는 절대 예외를 내지 않는다."""
    sim_backend = sim_backend if sim_backend is not None else NgspiceBackend()
    agent_backend = agent_backend if agent_backend is not None else _build_agent_backend(args)

    ctx = _RunContext()
    try:
        return await _curate(args, sim_backend, agent_backend, ctx)
    except Exception as exc:  # noqa: BLE001 - 이 파이프라인은 절대 트레이스백으로 끝나지 않는다
        return _finalize(ctx, verdict="INCONCLUSIVE", reason=f"unexpected error: {type(exc).__name__}: {exc}")


# --- 산출물 -------------------------------------------------------------------


# 비유한 float를 JSON으로 내보낼 때 쓰는 문자열 표지들. `json.dump`의 기본
# 동작은 `NaN`/`Infinity`라는 **bare 토큰**을 쓰는 것인데, 그것은 RFC 8259가
# 아니어서 엄격한 파서는 파일 전체를 거부한다(실측: `jq`와 JS `JSON.parse`
# 둘 다 거부). 이 파일에는 실제로 NaN이 들어간다 - `per_criterion[...]
# ["baseline"]`은 기존 본문이 후보가 낸 measurement를 못 낸 경우 `math.nan`
# 이고, 그것은 정상 경로다.
#
# `null`로 바꾸지 않는다: 이 산출물에서 `null`은 이미 "그 필드가 없다"를
# 뜻하고(예: `dominating_point: null`), NaN은 "쟀는데 값이 안 나왔다"는 다른
# 사실이다. 둘을 같은 토큰으로 접으면 이 저장소가 반복해 온 실수를 산출물
# 형식에서 다시 하는 것이다. 문자열 표지는 유효한 JSON이면서 그 구별을
# 보존한다.
_NON_FINITE_JSON = {"nan": "NaN", "inf": "Infinity", "-inf": "-Infinity"}


def _json_safe(value):
    """`json.dump(..., allow_nan=False)`가 던지는 대신, 비유한 float를 문자열
    표지로 바꿔 **유효한** RFC 8259 JSON을 낸다. dict/list를 재귀적으로 훑고
    그 밖의 값은 그대로 둔다."""
    if isinstance(value, float):
        if math.isnan(value):
            return _NON_FINITE_JSON["nan"]
        if math.isinf(value):
            return _NON_FINITE_JSON["inf" if value > 0 else "-inf"]
        return value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def write_curation_json(out_dir: str, result: dict) -> str:
    os.makedirs(out_dir, exist_ok=True)
    payload = {
        "verdict": result["verdict"],
        "reason": result["reason"],
        "source": result["source"],
        "block_path": result["block_path"],
        "verified_at": result["verified_at"],
        "addresses": result["addresses"],
        "description": result["description"],
        "description_source": result["description_source"],
        # 소스 C 에이전트가 낸 `rationale`. 판정에는 쓰이지 않지만(스키마에서
        # optional로 내린 것도 그 때문이다) 있으면 사람이 읽을 값이므로
        # 버리지 않고 여기 남긴다 - 그 전에는 `VariantAuthorResult` 필드
        # 하나로 존재하다 그대로 사라졌다.
        "rationale": result.get("rationale"),
        "candidate": asdict(result["candidate"]) if result["candidate"] is not None else None,
        "stages": [asdict(stage) for stage in result["stages"]],
    }
    path = os.path.join(out_dir, "curation.json")
    with open(path, "w") as f:
        # allow_nan=False로 못 박아 두면, 나중에 누가 _json_safe를 우회하는
        # 경로를 추가했을 때 조용히 비표준 JSON이 나가는 대신 여기서 터진다.
        json.dump(_json_safe(payload), f, indent=2, allow_nan=False)
    return path


def write_topology_candidate_py(out_dir: str, result: dict) -> str:
    """`topologies.py`에 그대로 붙여 넣을 수 있는 `Topology(...)` 스니펫.
    `ADMIT`이 아니어도 쓴다 - 후보가 무엇이었는지, 왜 거부/미확정됐는지가
    산출물이다. `provenance`/`verified_at`은 이 실행이 실제로 통과시킨 것에서만
    나온다(브리프 규칙 3) - 후보 자체가 없으면(예: 소스 C가 에이전트 오류로
    한 번도 저술본을 못 냈을 때) 스니펫 대신 그 사실을 적은 주석만 쓴다."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "topology_candidate.py")
    candidate = result["candidate"]

    if candidate is None:
        text = (
            f"# Curation verdict: {result['verdict']}\n"
            f"# No candidate body was ever constructed - reason: {result['reason']}\n"
        )
        with open(path, "w") as f:
            f.write(text)
        return path

    entry_lines = [
        "CANDIDATE = Topology(",
        f"    id={candidate.topology_id!r},",
        f"    description={result['description']!r},",
        f"    subckt_body={candidate.subckt_body!r},",
        f"    addresses={result['addresses']!r},",
        f"    ports={candidate.ports!r},",
        f"    assumes_scale={candidate.assumes_scale!r},",
        f"    provenance={candidate.provenance!r},",
        f"    verified_at={result['verified_at']!r},",
        ")",
    ]

    if result["verdict"] == "ADMIT":
        text = (
            "from analogcoder.topologies import Topology\n\n"
            f"# Curation verdict: ADMIT ({result['reason']})\n"
            + "\n".join(entry_lines)
            + "\n"
        )
    else:
        # **입회하지 못한 스니펫은 실수로 붙여 넣을 수 없어야 한다.** 예전에는
        # REJECT/INCONCLUSIVE도 문법적으로 멀쩡하고 import 가능한
        # `Topology(...)`를 그대로 냈고, 그것이 입회본과 다르다는 표시는 2번째
        # 줄의 `#` 주석 하나뿐이었다 - 라이브러리의 가치가 "사람이 측정 증거를
        # 보고 커밋했다"인데, 그 사람이 주석 한 줄을 놓치면 거부된 본문이
        # 라이브러리에 들어간다.
        #
        # 그래서 비-ADMIT은 항목 전체를 **주석 처리해** 낸다: 붙여 넣어도
        # 아무 일도 일어나지 않고, 되살리려면 한 줄씩 주석을 벗겨야 하므로
        # 실수로는 불가능하다. 사실 자체(무엇이 후보였는지, 왜 거부/미확정
        # 됐는지)는 하나도 잃지 않는다 - 그것이 이 산출물의 존재 이유다.
        text = (
            f"# Curation verdict: {result['verdict']}\n"
            f"# Reason: {result['reason']}\n"
            "#\n"
            "# THIS ENTRY WAS NOT ADMITTED. The snippet below is commented out on\n"
            "# purpose so that pasting it into topologies.py does nothing. Only a\n"
            "# curation run that ends in ADMIT emits pasteable code. If you believe\n"
            "# this verdict is wrong, fix what it measured and re-run the gate -\n"
            "# do not uncomment this.\n"
            "#\n"
            "# from analogcoder.topologies import Topology\n"
            + "\n".join(f"# {line}" for line in entry_lines)
            + "\n"
        )

    with open(path, "w") as f:
        f.write(text)
    return path


def _stage_section(stage: StageResult) -> list[str]:
    lines = [f"### Stage: {stage.name} - {stage.status}", "", "```json", json.dumps(stage.detail, indent=2, default=str), "```", ""]
    if stage.name == "comparison":
        lines.append(f"**Comparison scope:** {_comparison_scope_text(stage.detail)}")
        dominating = stage.detail.get("dominating_point")
        lines.append(
            f"**Dominating point:** {dominating if dominating is not None else 'none - candidate survives the incumbent-as-shipped point and every single-knob sweep point'}"
        )
        if stage.detail.get("tolerance") is not None:
            lines.append(f"**Relative tolerance applied:** {stage.detail['tolerance']:g} - {stage.detail.get('tolerance_note', '')}")
        if stage.detail.get("sweep_bounds_note"):
            lines.append(f"**Sweep bounds:** {stage.detail['sweep_bounds_note']}")
        lines.append("")
    if stage.name == "corners":
        # 요구 2가 무엇을 비교했는지(또는 아무것도 비교하지 않았는지)는 리포트에
        # 반드시 도달해야 한다 - `verified_at="corners"`를 잰 것보다 크게 읽지
        # 않도록. 이 단계가 건너뛰어졌거나(추출본/파일본) 코너가 없어
        # inconclusive면 그 detail에는 이 키가 없고, 그때는 줄을 더하지 않는다.
        note = stage.detail.get("requirement_2_note")
        if note:
            lines.append(f"**Requirement 2 (worst-corner comparison):** {note}")
            lines.append("")
    return lines


def _verified_at_caveat(result: dict) -> list[str]:
    """`verified_at == "corners"`인데 2.5단의 요구 2가 **아무 기준도 비교하지
    않은** 경우, 그 사실을 판정 바로 옆에 적는다. `addresses`가 비면 요구 2의
    루프가 한 바퀴도 돌지 않고 `pass`가 나오므로(실측: `corners: pass,
    criteria: {}, worse: []`로 ADMIT), 태그만 보면 코너에서 기존 본문을 이겼다는
    뜻으로 읽힌다. 단계 detail에도 같은 사실이 있지만(`requirement_2_note`),
    이 저장소의 규칙대로 리포트를 읽는 사람이 JSON 덤프를 파고들어야만 알 수
    있게 두지 않는다."""
    for stage in result["stages"]:
        if stage.name == "corners" and stage.status == "pass" and stage.detail.get("addresses_compared") == 0:
            return [
                (
                    "> **`verified_at=\"corners\"` here means requirement 1 only.** Stage 2.5's "
                    "requirement 2 (the candidate beats the incumbent at its worst corner) "
                    "compared **zero** criteria, because this run measured no improvement to "
                    "address. Every criterion did produce a measurement at every corner "
                    "(requirement 1) - nothing more was verified at corners."
                ),
                "",
            ]
    return []


def write_curation_report_md(out_dir: str, result: dict) -> str:
    os.makedirs(out_dir, exist_ok=True)
    lines = [
        "# Curation Report",
        "",
        f"**Verdict:** {result['verdict']}",
        f"**Reason:** {result['reason']}",
        f"**Source:** {result['source']}",
        f"**Block:** {result['block_path']}",
        f"**Verified at:** {result['verified_at']}",
        (
            "*(This reflects only what THIS curation run measured for THIS candidate "
            "in THIS slot - it makes no claim about the source deck's own "
            "corner-verification history, and does not imply that history transfers "
            "to a different slot. A human committing this snippet may upgrade "
            "`verified_at` to `\"corners\"` only after independently confirming a "
            "corner sweep for this exact candidate-in-slot pairing - the way the "
            "four shipped library entries earned their tag.)*"
        ),
        "",
    ]
    lines += _verified_at_caveat(result)
    lines += [
        "## Stages",
        "",
    ]
    for stage in result["stages"]:
        lines += _stage_section(stage)

    lines += [
        "## Addresses (measured improvement over the incumbent)",
        "",
        f"{result['addresses'] if result['addresses'] else 'none'}",
        "",
        "## Variant author rationale (source C only)",
        "",
        (result["rationale"] or "(none - this source does not produce one, or the agent omitted it)"),
        "",
        "## Description",
        "",
        f"**Source:** {result['description_source']}",
        "",
        result["description"] or "(not reached - the run ended before stage 5)",
        "",
    ]

    path = os.path.join(out_dir, "curation_report.md")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def write_curation_artifacts(out_dir: str, result: dict) -> None:
    write_curation_json(out_dir, result)
    write_topology_candidate_py(out_dir, result)
    write_curation_report_md(out_dir, result)


# --- 진입점 -------------------------------------------------------------------


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    result = asyncio.run(run_curation(args))
    write_curation_artifacts(args.out_dir, result)
    print(f"Verdict: {result['verdict']}")
    sys.exit(0 if result["verdict"] == "ADMIT" else 1)


if __name__ == "__main__":
    main()
