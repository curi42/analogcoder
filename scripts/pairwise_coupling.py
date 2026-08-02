#!/usr/bin/env python3
"""two_stage_opamp의 tunable 노브 33개, 528쌍 전수에 대해 결합(coupling)이 있는지 잰다.

**T17. 로드맵 단계 3(신뢰영역 DFO)의 부정 결과가 남긴 열린 선행 조건을 채우는
측정이다.** 그 판정 문장은 좁게 읽어야 한다고 이미 적혀 있다 - 겨냥한 약점 셋
중 둘(결합, 혼합정수)이 **원리적으로 발화하지 못했다**: 노브 순위에 노브가
하나뿐이라 복합 이동이 존재할 수 없었다. 그래서 "탐색이 병목이 아니다"로 읽으면
안 되고, 단계 4(제약 BO)의 선행 조건은 열려 있다. `scripts/knob_coupling_scan.py`
(T8)가 한 쌍(`Xcc` x `X6.W`)을 재서 **거의 직교**하다는 것을 실측했다(위상 여유
축 변화폭 비 22.5x) - 그것으로 판별력 있는 벤치마크 설계의 전제가 반증됐지만,
한 쌍으로는 "이 회로에 결합이 없다"고 말할 수 없다. 그래서 전수로 잰다.

## 왜 바이스테이블 처리를 하는가 - 이것을 빼면 측정이 통째로 무효다

`benchmarks/two_stage_opamp/netlist.cir`의 자기 바이어스 체인은 **안정한 DC 해를
둘** 갖는다(`docs/superpowers/specs/2026-07-30-two-stage-opamp-bistable-bias.md`).
두 상태의 `ugbw_hz`는 13배 다르고, 어느 쪽으로 가는지는 소자 크기에 혼돈적으로
의존한다(T8의 스캔에서 `X6.W` = 6, 27 두 열이 정확히 그 이상이었다). 격자의 한
칸이 상태를 뒤집으면 그 칸의 값은 "두 노브의 상호작용"이 아니라 **다른 회로**의
값이고, 이 스크립트의 상호작용 대비 공식은 그 차이를 곧바로 "결합"으로 읽는다 -
즉 처리하지 않으면 거짓 결합 쌍을 만들어낸다.

완화책은 **측정용 덱에만** `.nodeset v(xdut.nbias)=0.5399 v(xdut.pbias)=0.9129`를
넣는 것이다(`benchmarks/`의 원본은 건드리지 않는다 - 그것은 사람에게 넘긴 설계
판단이다). `.nodeset`은 뉴턴 반복의 **초기 추정**일 뿐이므로 최종 해는 여전히
진짜 DC 해다 - 답을 강제하는 것이 아니라 어느 분지로 갈지를 유도한다. 이 완화책은
설계 문서 4절에서 열 개 폭에 걸쳐 검증됐다(이상했던 값 전부가 매끄러운 추세로
돌아오고 정상이던 값은 바이트 동일).

**그리고 모든 크기에서 듣는다는 보장은 없으므로, 매 격자점에서 확인한다.**
`v(xdut.degn)`을 다른 측정값과 **같은 컨트롤 블록**에서(별도 실행이 아니라 -
별도로 재면 그 실행이 같은 상태에 있었다는 보장이 없다) 뽑아, 상태 A(~0.0119V)
에서 벗어난 점(`> 0.03V` - 두 상태 0.0119/0.0626의 중간이므로 유도된 값)을 세어
산출물에 싣는다. 0이 아니면 그 측정은 **무효**이고 그렇게 적는다.

## 이 스크립트를 만들며 되풀이하지 않은 함정 셋 (전부 `scripts/dc_solution_uniqueness.py`가 먼저 밟았다)

1. **`.end` 뒤에 컨트롤 블록/`.nodeset`을 붙이면 ngspice가 통째로 무시한다.**
   `_insert_before_end`가 마지막 `.end` 줄을 찾아 그 **앞에** 삽입한다.
2. **`print v(a) v(b) v(c)`는 이름 하나가 틀리면 줄 전체를 버린다.** 여기서는
   프로브가 `v(xdut.degn)` 하나뿐이라 여러 이름을 한 줄에 낼 위험 자체가 없지만,
   같은 이유로 `print` 한 줄에 하나만 낸다.
3. **`.nodeset` 줄을 출력에서 되읽으면 내가 넣은 값을 측정값으로 보고한다.**
   `_extract_degn`은 raw_log에서 `.nodeset`으로 시작하는 줄을 걸러낸 뒤에만
   정규식을 돌린다.

## 지표 - 사전 등록(`docs/superpowers/specs/2026-07-29-theory-adoption-roadmap.md`
「T17 사전 등록」)에서 그대로 옮긴다. 바꾸지 않는다.

측정값 `m`, 노브 쌍 `(A, B)`에 대해:

1. **결합으로만 도달 가능한 영역**: `beats = #{(a,b): m(a,b) > max(단일축 A 최대,
   단일축 B 최대)}` - A만 격자 전체를 훑고 B는 출하값에 고정한 행의 최댓값과,
   B만 훑고 A는 출하값에 고정한 열의 최댓값, 그 **둘 다**를 넘는 격자점 수.
2. **상호작용 대비**: `I = |m(a1,b1) - m(a1,b0) - m(a0,b1) + m(a0,b0)|`,
   `I_rel = I / max(|m(a1,b0)-m(a0,b0)|, |m(a0,b1)-m(a0,b0)|)`.

   **`a0`/`a1`을 어디서 뽑는가 - 사전 등록의 "격자의 양 끝에서"를 문자 그대로
   읽는다.** 표준 2수준(2-level) 상호작용 대비는 교과서적으로 실험 범위의
   **낮은 끝과 높은 끝** 둘로 정의되고(가운데 점은 곡률 확인용이지 상호작용
   추정에 쓰지 않는다), 사전 등록 문장도 "격자의 양 끝"이라고 명시한다. 그래서
   `a0` = 그 노브의 격자에서 가장 낮은 값(보통 x0.5, 정수 노브가 반올림으로
   그 값을 잃으면 남은 것 중 가장 낮은 값 - 그 경우 출하값 자체), `a1` = 가장
   높은 값(x2)이다. 출하값(x1)은 `beats`에는 쓰이지만 이 대비 공식에는 등장하지
   않는다 - 사전 등록의 "기준값 a0/b0는 출하 덱의 값"이라는 문장은 `beats`
   공식의 표기이고, 이 공식은 "격자의 양 끝에서"라는 별도 지시를 받는다. (정수
   노브가 낮은 끝을 잃어 `a0`가 출하값과 같아지는 경우, `beats`와 `I`가 같은
   기준점을 공유하게 되는 것은 우연이 아니라 그 경우 격자의 낮은 끝이 곧
   출하값이기 때문이다.)

**임계값은 유도하지 않고 이미 실측된 상수를 쓴다.** `curation.COMPARISON_REL_TOLERANCE`
(=1e-3)를 **import**해서 쓴다 - 새 비율을 고르는 것은 ε=0.03이 반증된 것과 같은
부류의 실수라고 사전 등록이 명시한다.

**결합 쌍(쌍, 측정값) = `beats > 0` 그리고 `I_rel > COMPARISON_REL_TOLERANCE`.**

## 절차 - 2단계

**1단계 선별, 3x3.** 528쌍 x 3x3 격자(각 노브 x0.5/x1/x2) x 측정값 3개. 캐시
(`simulators.cache.CachingSimulator`)와 병렬(`simulators.parallel`)을 쓴다.
격자점은 쌍 사이에 크게 겹친다 - "출하값에 고정" 쪽 절반은 파트너 노브가
무엇이든 **바이트 동일한 덱 텍스트**이므로, 이 스크립트는 시뮬레이션을 보내기
전에 파이썬 레벨에서 텍스트로 중복을 제거한다(캐시가 병렬 워커 사이의 경합으로
같은 점을 여러 번 미스 처리하는 것을 막기 위해서이기도 하다 - 캐시 자체는
안전망으로 계속 붙인다).

**2단계 확인, 7x7.** 1단계에서 결합으로 나온 쌍만. 1단계의 `I_rel`은 양 끝
두 점으로만 재므로 국소 계단 하나에 민감하다 - 7x7 격자 **내부의 모든 2x2
부분격자**(임의의 두 행 x 임의의 두 열, C(7,2)^2 = 441개 조합)에 대해 같은
공식으로 `I_rel`을 재고, **중앙값**이 임계를 넘어야 확정이다(최댓값을 쓰면
계단 하나가 통과시킨다).

**정수 노브(`m`/`nf`/`mf`)**는 반올림하고 1 미만은 버린다(그 노브의 그 격자점은
없는 것으로 취급 - 어느 축이 좁아지는지는 산출물의 `dropped_scales`에 남는다).
값이 파싱 안 되는 노브는 건너뛰고 세어서 보고한다.

**비용 상한.** 총 시뮬레이션이 `MAX_TOTAL_SIMS`(8000)를 넘거나 벽시계가
`MAX_WALL_SECONDS`(40분)를 넘으면 멈추고 그 사실을 산출물에 적는다. 격자를
3x3/7x7 아래로 줄이지 않는다(그러면 두 지표 중 하나를 계산할 수 없다) - 대신
노브를 줄이되 어느 노브를 왜 뺐는지 명시해야 한다(`--max-knobs 8`이
`TRIMAMP.XRz.l`을 가린 전례가 이 저장소에 있다. 이 스크립트는 기본으로 33개
전체를 쓴다).

사용:

    .venv/bin/python scripts/pairwise_coupling.py
    .venv/bin/python scripts/pairwise_coupling.py --out /path/to/out.json
"""

import argparse
import itertools
import json
import os
import re
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))

from analogcoder.area_limits import index_baseline_components  # noqa: E402
from analogcoder.area_ranking import rank_by_area_gain  # noqa: E402
from analogcoder.curation import COMPARISON_REL_TOLERANCE  # noqa: E402
from analogcoder.netlist import apply_changes, parse_netlist, parse_spice_value, resolve_includes  # noqa: E402
from analogcoder.optimizer import _area_change, _area_knob_state  # noqa: E402
from analogcoder.simulators.cache import CachingSimulator  # noqa: E402
from analogcoder.simulators.ngspice import NgspiceBackend  # noqa: E402
from analogcoder.simulators.parallel import default_workers, map_points  # noqa: E402
from analogcoder.spec import load_spec  # noqa: E402
from analogcoder.structure import derive_structure  # noqa: E402

REPO = os.path.dirname(_HERE)
SPEC_PATH = os.path.join(REPO, "benchmarks", "two_stage_opamp", "spec_pvt.yaml")
CIRCUIT_NAME = "two_stage_opamp"
TESTBENCH_NAME = "ac_loop_gain"

MEASUREMENTS = ("gain_db", "ugbw_hz", "phase_margin_deg")

# 바이스테이블 완화. §"왜 이 처리를 하는가" 참조. 측정용 덱에만 넣는다.
NODESET_LINE = ".nodeset v(xdut.nbias)=0.5399 v(xdut.pbias)=0.9129"
DEGN_PROBE = "v(xdut.degn)"
# 상태 A(~0.0119V)/B(~0.0626V)의 중간. 브리프가 지정한 유도값.
DEGN_STATE_A_THRESHOLD = 0.03

SCALES_STAGE1 = (0.5, 1.0, 2.0)
# 0.5/1.0/2.0을 부분집합으로 포함한다 - 확정 후보의 1단계 시뮬레이션이 2단계
# 캐시에서 그대로 재사용된다(같은 프로세스에서 캐시 인스턴스를 공유하는 한).
SCALES_STAGE2 = (0.3, 0.5, 0.7, 1.0, 1.4, 2.0, 3.0)

# CLAUDE.md: "m multiplies area, nf does not" - 인스턴스 배수 계열 파라미터는
# 정수다. 이 덱의 tunable 인덱스에는 mf(MiM 캡 finger 수)만 해당한다.
INTEGER_PARAMS = {"m", "nf", "mf"}

# 바이어스 생성기 그 자체인 소자들(netlist.cir): Xp3/Xp4/Xn1/Xn2가 nbias/pbias를
# 만드는 베타-멀티플라이어이고 Rdeg/Rstart가 그 축퇴/시동 소자다. 이 소자들을
# 직접 건드린 격자점의 degn 이탈은 **기대된 물리일 수 있다는 가설**이지 확인된
# 사실이 아니다 - `DEGN_STATE_A_THRESHOLD`가 상태 A/B의 중간값이라, "소자를
# 바꿔 동작점이 옮겨갔다"와 "솔버가 상태 B에 착지했다"를 지금 데이터로는 가를
# 수 없다. 그래서 이 집합은 **진단(바이어스 관련/무관 분해)에만** 쓰고, 유효성
# 판정(`bistability_validity`)에는 쓰지 않는다 - 판정은 사전 등록 문장 그대로
# "이탈 점을 가진 쌍은 무효"를 예외 없이 적용한다.
BIAS_GENERATOR_REFDES = {
    "OPAMP2STAGE.Xp3", "OPAMP2STAGE.Xp4", "OPAMP2STAGE.Xn1",
    "OPAMP2STAGE.Xn2", "OPAMP2STAGE.Rdeg", "OPAMP2STAGE.Rstart",
}

MAX_TOTAL_SIMS = 8000
MAX_WALL_SECONDS = 40 * 60


@dataclass(frozen=True)
class DeckProfile:
    """어느 덱을 어떻게 재는가. **기본 프로파일은 T17이 실제로 돌린 설정 그대로다.**

    이 데이터클래스가 생긴 이유는 T18b(밴드갭 결합 선행 조건 측정)가 같은
    지표 공식을 써야 하기 때문이다 - 공식을 복사하면 두 결과가 비교 불가능해지고,
    이 저장소는 그 실패(`compose.py`가 `netlist.py`의 include 규칙을 손으로
    베껴 양방향으로 갈라진 것)를 이미 치렀다. 그래서 `_interaction`/`_beats`/
    `_pair_grid`/`_levels_for`/`SCALES_*`/임계값은 **한 벌만** 존재하고,
    덱마다 다른 것(스펙 경로, 측정값 이름, 바이스테이블 완화책, 노브 출처)만
    여기로 뺐다.

    `nodeset_line`/`degn_probe`가 `None`인 프로파일에서는 **바이스테이블 확인이
    돌지 않는다**. "확인했고 깨끗했다"와 "확인하지 않았다"는 다른 사실이므로
    산출물의 `bistability_validity.checked`가 그것을 명시한다."""

    key: str
    spec_path: str
    circuit_name: str
    # None이면 spec.canonical(= testbenches[0])을 쓴다.
    testbench_name: str | None
    measurements: tuple[str, ...]
    # 바이스테이블 완화책. None이면 덱에 아무것도 주입하지 않는다.
    nodeset_line: str | None
    degn_probe: str | None
    degn_state_a_threshold: float | None
    bias_generator_refdes: frozenset
    # "tunable_index": 덱의 tunable 인덱스 전체(T17).
    # "area_ranking": optimizer.run_area_optimization이 실제로 쓰는 면적 이득
    #                 순위(T18b) - 그 단계가 건드리는 노브만 재는 것이 질문이다.
    knob_source: str
    top_knobs: int | None
    # 헤드라인 사례(있으면 산출물에 별도 확인 칸을 만든다).
    headline_pair: tuple[str, str] | None
    headline_measurement: str | None
    # 노브 **순위**를 매길 덱의 테스트벤치. knob_source="area_ranking"에서
    # None이면 spec.canonical을 쓴다(면적 단계가 읽는 덱).
    # 면적 단계는 `spec.canonical`의 덱 하나에서 순위를 매기므로(optimizer.py의
    # `start_text = netlist_texts[canonical_name]`), 다른 테스트벤치에서 재더라도
    # **순위는 canonical에서** 나와야 "면적 단계가 만지는 노브"라는 주장이 선다.
    rank_testbench_name: str | None = None


TWO_STAGE_PROFILE = DeckProfile(
    key="two_stage_opamp",
    spec_path=SPEC_PATH,
    circuit_name=CIRCUIT_NAME,
    testbench_name=TESTBENCH_NAME,
    measurements=MEASUREMENTS,
    nodeset_line=NODESET_LINE,
    degn_probe=DEGN_PROBE,
    degn_state_a_threshold=DEGN_STATE_A_THRESHOLD,
    bias_generator_refdes=frozenset(BIAS_GENERATOR_REFDES),
    knob_source="tunable_index",
    top_knobs=None,
    headline_pair=("OPAMP2STAGE.X5.L", "OPAMP2STAGE.X7.L"),
    headline_measurement="ugbw_hz",
)

# T18b. 밴드갭에는 바이스테이블 완화책이 **없다** - 이 덱이 그 오염을 갖지
# 않는다는 것은 `scripts/dc_solution_uniqueness.py`가 이미 쟀다(바이어스 체인
# 초기 추정을 다섯 방향으로, 소자 크기 네 가지, 테스트벤치 덱 네 개에 걸쳐
# 밀었고 여섯 프로브가 매번 동일했다). 그래도 **이 실행에서 확인한 것은
# 아니므로**, 산출물은 "확인 안 함"을 명시한다.
BANDGAP_PROFILE = DeckProfile(
    key="bandgap",
    spec_path=os.path.join(REPO, "benchmarks", "bandgap", "spec.yaml"),
    circuit_name="bandgap",
    testbench_name=None,  # spec.canonical
    measurements=("vbgout_v", "vbg0_v", "vbg1_v", "iq_ua", "tc_ppm_per_c"),
    nodeset_line=None,
    degn_probe=None,
    degn_state_a_threshold=None,
    bias_generator_refdes=frozenset(),
    knob_source="area_ranking",
    top_knobs=12,
    headline_pair=None,
    headline_measurement=None,
)

# T18b 후속. `dc_tc`(canonical)에서는 면적 순위 상위 12개 중 **10개가 정확히
# 무효과**다 - Xcc는 보상 커패시터고 dc_tc는 DC 온도 스윕이라 DC 전류를 흘리지
# 않는다(측정값 다섯 개가 바이트 동일하다는 것을 단축 프로브로 확인했다).
# 그래서 그 열 개에 대한 "결합 없음"은 직교성의 증거가 아니라 **계기가 그
# 노브를 못 본 것**이다. 결합은 (덱 x 테스트벤치 x 측정값)의 성질이므로, 그
# 노브를 볼 수 있는 테스트벤치에서 따로 잰다. 순위는 여전히 canonical 덱에서
# 나온다 - 면적 단계가 순위를 매기는 덱이 그것이기 때문이다.
BANDGAP_LOOPS_PROFILE = replace(
    BANDGAP_PROFILE,
    key="bandgap_loops",
    testbench_name="amp_loops",
    rank_testbench_name=None,  # = spec.canonical, 아래에서 그렇게 해석된다
    measurements=(
        "core_gain_db", "core_pm_deg", "trim_gain_db", "trim_pm_deg",
        "buf1_gain_db", "buf1_pm_deg", "buf0_gain_db", "buf0_pm_deg",
    ),
)

PROFILES = {p.key: p for p in (TWO_STAGE_PROFILE, BANDGAP_PROFILE, BANDGAP_LOOPS_PROFILE)}

DEFAULT_OUT_JSON = os.path.join(
    REPO, "docs", "superpowers", "specs", "2026-07-30-knob-coupling-scan.json"
)
OUT_JSON_BY_DECK = {
    "two_stage_opamp": DEFAULT_OUT_JSON,
    "bandgap": os.path.join(
        REPO, "docs", "superpowers", "specs", "2026-08-02-bandgap-coupling-precondition.json"
    ),
    "bandgap_loops": os.path.join(
        REPO, "docs", "superpowers", "specs",
        "2026-08-02-bandgap-coupling-precondition-amp-loops.json"
    ),
}
_SCRATCH = "/private/tmp/claude-501/-Users-sunbeom-orca-projects-analogcoder/e49ad585-6ed0-4f42-8349-b144ea9e75bd/scratchpad"
DEFAULT_LOG = os.path.join(
    _SCRATCH if os.path.isdir(_SCRATCH) else tempfile.gettempdir(),
    "pairwise_coupling_progress.log",
)

_START = time.monotonic()
_LOG_PATH = DEFAULT_LOG


def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')} +{time.monotonic() - _START:6.1f}s] {msg}"
    print(line, flush=True)
    try:
        with open(_LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


@dataclass
class Knob:
    refdes: str
    param: str
    baseline_raw: str
    baseline: float
    is_integer: bool

    @property
    def label(self) -> str:
        return f"{self.refdes}.{self.param}"


def _find_component(parsed, scoped_refdes: str):
    from analogcoder.netlist import split_scoped_refdes

    scope, refdes = split_scoped_refdes(scoped_refdes)
    if scope is None:
        candidates = [c for c in parsed.top_components if c.refdes == refdes]
    else:
        subckt = parsed.subckts.get(scope)
        candidates = [c for c in subckt.components if c.refdes == refdes] if subckt else []
    return candidates[0] if len(candidates) == 1 else None


def build_knobs(
    base_text: str, profile: "DeckProfile" = None
) -> tuple[list[Knob], list[str]]:
    """덱의 tunable 인덱스 전체를 Knob으로. 값이 안 파싱되면 건너뛰고 이름을 모은다."""
    profile = profile or TWO_STAGE_PROFILE
    structure = derive_structure(base_text, profile.circuit_name)
    parsed = parse_netlist(base_text)
    knobs, skipped = [], []
    for entry in structure.tunable:
        comp = _find_component(parsed, entry.refdes)
        raw = None
        if comp is not None:
            raw = comp.value if entry.param == "value" else comp.params.get(entry.param)
        baseline = None
        if raw is not None:
            try:
                baseline = parse_spice_value(raw)
            except ValueError:
                baseline = None
        if comp is None or raw is None or baseline is None:
            skipped.append(f"{entry.refdes}.{entry.param}")
            continue
        knobs.append(
            Knob(
                refdes=entry.refdes,
                param=entry.param,
                baseline_raw=raw,
                baseline=baseline,
                is_integer=entry.param.lower() in INTEGER_PARAMS,
            )
        )
    return knobs, skipped


def build_knobs_from_area_ranking(
    rank_text: str, profile: "DeckProfile"
) -> tuple[list[Knob], list[str], dict]:
    """면적 단계가 **실제로 건드리는** 노브를, 그 단계가 쓰는 코드로 순위 매긴다.

    `optimizer.run_area_optimization`의 준비 구간을 그대로 재현한다:
    `derive_structure` → `_area_knob_state`(주소 지정 게이트 + 덱 철자 토큰) →
    `rank_by_area_gain(text, candidates, _area_change)`. 순위 공식이나 스텝
    규칙을 여기 복제하지 않는다 - 복제하면 이 측정이 "면적 단계가 만지는
    노브"가 아니라 "이 스크립트가 만진다고 생각하는 노브"를 재게 된다.

    `rank_text`는 `run_area_optimization`이 읽는 것과 같은 **덱 파일 원문**이다
    (`state.current_netlist_texts()`가 파일을 그대로 읽는다). include 절대경로화는
    시뮬레이션용 텍스트에만 필요하고, 순위에는 영향이 없다 - `resolve_includes`는
    `.include` 경로만 다시 쓸 뿐 본문을 인라인하지 않는다."""
    parsed = parse_netlist(rank_text)
    structure = derive_structure(rank_text, profile.circuit_name)
    baseline_components = index_baseline_components(rank_text)
    candidates = []
    for entry in structure.tunable:
        knob_state = _area_knob_state(
            rank_text, baseline_components, entry.refdes, entry.param
        )
        if knob_state is None:
            continue
        candidates.append(
            (entry.refdes, knob_state.token, knob_state.value, knob_state.integer)
        )
    ranking = rank_by_area_gain(rank_text, candidates, _area_change)

    integer_by_label = {f"{r}.{p}": integer for r, p, _v, integer in candidates}
    knobs, skipped = [], []
    selected = ranking.entries
    if profile.top_knobs is not None:
        selected = selected[: profile.top_knobs]
    for entry in selected:
        label = f"{entry.refdes}.{entry.param}"
        comp = _find_component(parsed, entry.refdes)
        raw = None
        if comp is not None:
            raw = comp.value if entry.param == "value" else comp.params.get(entry.param)
        baseline = None
        if raw is not None:
            try:
                baseline = parse_spice_value(raw)
            except ValueError:
                baseline = None
        if comp is None or raw is None or baseline is None:
            skipped.append(label)
            continue
        knobs.append(
            Knob(
                refdes=entry.refdes,
                param=entry.param,
                baseline_raw=raw,
                baseline=baseline,
                is_integer=integer_by_label.get(label, entry.param.lower() in INTEGER_PARAMS),
            )
        )
    record = {
        "source": "optimizer.run_area_optimization 준비 구간 재현 "
                  "(derive_structure → _area_knob_state → rank_by_area_gain(_area_change))",
        "n_tunable": len(structure.tunable),
        "n_candidates": len(candidates),
        "ranked_all": [
            {"refdes": e.refdes, "param": e.param, "gain": e.gain} for e in ranking.entries
        ],
        "zero_gain": ranking.zero_gain,
        "unknown": ranking.unknown,
        "top_knobs_requested": profile.top_knobs,
        "top_knobs_used": [f"{e.refdes}.{e.param}" for e in selected],
    }
    return knobs, skipped, record


def _levels_for(knob: Knob, scales: tuple[float, ...]) -> tuple[list[float], list[float]]:
    """`scales`를 오름차순으로 적용해 (레벨 목록, 버려진 scale 목록)을 낸다.

    정수 노브는 반올림하고 1 미만은 버린다. `scales`가 항상 오름차순이고 1.0을
    포함하므로 결과 레벨 목록도 오름차순이고, 기준값(scale=1.0)은 정수 노브라도
    절대 버려지지 않는다(원래 값이 이미 1 이상의 정수라서)."""
    levels, dropped = [], []
    for s in scales:
        v = knob.baseline * s
        if knob.is_integer:
            v = float(round(v))
            if v < 1:
                dropped.append(s)
                continue
        if v not in levels:
            levels.append(v)
    return levels, dropped


def _fmt_value(value: float, is_integer: bool) -> str:
    return str(int(value)) if is_integer else repr(value)


def _insert_before_end(text: str, line: str) -> str:
    lines = text.rstrip("\n").splitlines()
    end_at = next(
        (i for i in range(len(lines) - 1, -1, -1) if lines[i].strip().lower() == ".end"),
        None,
    )
    if end_at is None:
        raise RuntimeError("deck has no .end line - refusing to guess insertion point")
    return "\n".join(lines[:end_at] + [line] + lines[end_at:]) + "\n"


def build_control_block(base_control_block: str, profile: "DeckProfile" = None) -> str:
    """degn 프로브를 다른 측정값과 **같은** 컨트롤 블록에 넣는다(별도 실행 금지).

    `op`을 `ac` 분석보다 먼저 돌리고 그 직후에 print해야 print가 op의 동작점을
    읽는다 - ac 다음에 두면 현재 plot이 ac로 바뀌어 다른 것을 읽는다.

    **프로파일에 프로브가 없으면 컨트롤 블록을 손대지 않고 그대로 돌려준다.**
    이것이 중요한 이유: ngspice는 `print` 한 줄에 이름이 하나라도 틀리면
    **줄 전체를 버린다**(`CLAUDE.md`가 종료코드 0짜리 조용한 실패 다섯 중
    하나로 기록한 것). 밴드갭 덱에는 `xdut.degn`이 없으므로 이 주입을 그대로
    가져가면 측정이 멀쩡해 보이면서 틀린다."""
    profile = profile or TWO_STAGE_PROFILE
    if profile.degn_probe is None:
        return base_control_block
    lines = base_control_block.splitlines()
    idx = next(i for i, ln in enumerate(lines) if ln.strip().lower() == ".control")
    injected = ["op", f"print {profile.degn_probe}"]
    return "\n".join(lines[: idx + 1] + injected + lines[idx + 1 :]) + "\n"


_DEGN_RE = re.compile(re.escape(DEGN_PROBE) + r"\s*=\s*([-+0-9.eE]+)")


def _extract_degn(raw_log: str, probe: str | None = DEGN_PROBE) -> float | None:
    """raw_log에서 degn 값을 뽑는다. `.nodeset` 줄은 걸러낸다 - 안 그러면 내가
    넣은 초기 추정값을 측정값으로 잘못 읽을 수 있다(이 저장소가 이미 밟은 함정).

    `probe`가 None이면 이 덱에는 프로브가 없다 - **읽지 않았다**는 뜻의 None을
    낸다(0.0이 아니다)."""
    if probe is None:
        return None
    filtered = "\n".join(
        ln for ln in raw_log.splitlines() if not ln.strip().lower().startswith(".nodeset")
    )
    pattern = _DEGN_RE if probe == DEGN_PROBE else re.compile(
        re.escape(probe) + r"\s*=\s*([-+0-9.eE]+)"
    )
    m = pattern.search(filtered)
    return float(m.group(1)) if m else None


def _grid_text(base_text: str, knob_a: Knob, val_a: float, is_base_a: bool,
                knob_b: Knob, val_b: float, is_base_b: bool,
                profile: "DeckProfile" = None) -> str:
    changes = []
    if not is_base_a:
        changes.append({"refdes": knob_a.refdes, "param": knob_a.param,
                         "new_value": _fmt_value(val_a, knob_a.is_integer)})
    if not is_base_b:
        changes.append({"refdes": knob_b.refdes, "param": knob_b.param,
                         "new_value": _fmt_value(val_b, knob_b.is_integer)})
    profile = profile or TWO_STAGE_PROFILE
    modified = apply_changes(base_text, changes) if changes else base_text
    # 프로파일에 완화책이 없으면 덱에 아무것도 넣지 않는다. `.nodeset`이
    # 이름 짓는 노드(`xdut.nbias`/`xdut.pbias`)는 밴드갭 덱에 존재하지 않는다.
    if profile.nodeset_line is None:
        return modified
    return _insert_before_end(modified, profile.nodeset_line)


def _simulate_text(text: str, control_block: str, backend, profile: "DeckProfile" = None) -> dict:
    profile = profile or TWO_STAGE_PROFILE
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "deck.cir")
        with open(path, "w") as f:
            f.write(text)
        result = backend.run(path, {"control_block": control_block})
    return {
        "status": result.status,
        "measurements": dict(result.measurements),
        "degn": _extract_degn(result.raw_log, profile.degn_probe),
    }


@dataclass
class PairGrid:
    """한 쌍(A,B)에 대한 한 스테이지의 격자 결과."""

    levels_a: list[float]
    levels_b: list[float]
    base_idx_a: int
    base_idx_b: int
    values: dict  # (ia, ib) -> {measurement: float|None, "degn": float|None}


def _collect_texts_for_pairs(
    knobs: list[Knob], pairs: list[tuple[int, int]], scales: tuple[float, ...], base_text: str,
    profile: "DeckProfile" = None,
) -> tuple[dict, dict]:
    """`pairs`(knobs 인덱스 쌍)의 전체 격자에 필요한 유일한 덱 텍스트 집합과,
    각 노브의 (레벨, 기준 인덱스, 버려진 scale)을 낸다. 텍스트로 중복을
    제거하므로 "출하값 고정" 축은 파트너가 몇 개든 한 번만 계산된다."""
    profile = profile or TWO_STAGE_PROFILE
    level_cache: dict[int, tuple[list[float], int, list[float]]] = {}

    def levels_of(i: int):
        if i not in level_cache:
            levels, dropped = _levels_for(knobs[i], scales)
            base_idx = levels.index(knobs[i].baseline)
            level_cache[i] = (levels, base_idx, dropped)
        return level_cache[i]

    unique_texts: set[str] = set()
    for i, j in pairs:
        la, ba, _ = levels_of(i)
        lb, bb, _ = levels_of(j)
        for ia, va in enumerate(la):
            for ib, vb in enumerate(lb):
                text = _grid_text(base_text, knobs[i], va, ia == ba, knobs[j], vb, ib == bb,
                                  profile)
                unique_texts.add(text)
    return unique_texts, level_cache


def _run_texts(unique_texts: set[str], control_block: str, backend, max_workers=None,
               profile: "DeckProfile" = None) -> dict:
    profile = profile or TWO_STAGE_PROFILE
    items = [(t, t) for t in unique_texts]
    return map_points(
        lambda t: _simulate_text(t, control_block, backend, profile), items, max_workers
    )


def _pair_grid(knobs, i, j, level_cache, base_text, results_by_text,
               profile: "DeckProfile" = None) -> PairGrid:
    profile = profile or TWO_STAGE_PROFILE
    la, ba, _ = level_cache[i]
    lb, bb, _ = level_cache[j]
    values = {}
    for ia, va in enumerate(la):
        for ib, vb in enumerate(lb):
            text = _grid_text(base_text, knobs[i], va, ia == ba, knobs[j], vb, ib == bb, profile)
            r = results_by_text.get(text)
            entry = {"degn": None}
            for name in profile.measurements:
                entry[name] = r["measurements"].get(name) if r else None
            if r is not None:
                entry["degn"] = r["degn"]
            values[(ia, ib)] = entry
    return PairGrid(levels_a=la, levels_b=lb, base_idx_a=ba, base_idx_b=bb, values=values)


def _beats(grid: PairGrid, name: str) -> tuple[int, float | None]:
    ba, bb = grid.base_idx_a, grid.base_idx_b
    axis_a = [grid.values[(ia, bb)][name] for ia in range(len(grid.levels_a))]
    axis_b = [grid.values[(ba, ib)][name] for ib in range(len(grid.levels_b))]
    axis_a = [v for v in axis_a if v is not None]
    axis_b = [v for v in axis_b if v is not None]
    if not axis_a or not axis_b:
        return 0, None
    single_max = max(max(axis_a), max(axis_b))
    count = sum(
        1
        for (ia, ib), entry in grid.values.items()
        if entry[name] is not None and entry[name] > single_max
    )
    return count, single_max


def _interaction(grid: PairGrid, name: str, ia0: int, ia1: int, ib0: int, ib1: int):
    """§"a0/a1을 어디서 뽑는가" - 호출자가 두 레벨 인덱스 쌍을 넘긴다. 1단계는
    (낮은 끝, 높은 끝), 2단계 확인은 7x7 안의 임의의 두 레벨 쌍."""
    v00 = grid.values[(ia0, ib0)][name]
    v10 = grid.values[(ia1, ib0)][name]
    v01 = grid.values[(ia0, ib1)][name]
    v11 = grid.values[(ia1, ib1)][name]
    if None in (v00, v10, v01, v11):
        return None, None
    I = abs(v11 - v10 - v01 + v00)
    denom = max(abs(v10 - v00), abs(v01 - v00))
    if denom == 0:
        return I, (0.0 if I == 0 else None)
    return I, I / denom


def _degn_deviations(grid: PairGrid, profile: "DeckProfile" = None) -> int:
    """이탈 점 수. 프로파일에 임계값이 없으면 **확인 자체를 하지 않은 것**이라
    0을 낸다 - 그리고 그 0을 "깨끗함"으로 읽으면 안 되기 때문에 산출물은
    `bistability_validity.checked=False`로 별도로 말한다."""
    profile = profile or TWO_STAGE_PROFILE
    if profile.degn_state_a_threshold is None:
        return 0
    return sum(
        1
        for entry in grid.values.values()
        if entry["degn"] is not None and entry["degn"] > profile.degn_state_a_threshold
    )


def _touches_bias_generator(knob_a: Knob, knob_b: Knob, profile: "DeckProfile" = None) -> bool:
    profile = profile or TWO_STAGE_PROFILE
    return (
        knob_a.refdes in profile.bias_generator_refdes
        or knob_b.refdes in profile.bias_generator_refdes
    )


def stage1(knobs, base_text, control_block, backend, log, max_workers=None,
           profile: "DeckProfile" = None):
    profile = profile or TWO_STAGE_PROFILE
    n = len(knobs)
    pairs = list(itertools.combinations(range(n), 2))
    log(f"1단계: 노브 {n}개, 쌍 {len(pairs)}개, 스케일 {SCALES_STAGE1}")

    unique_texts, level_cache = _collect_texts_for_pairs(
        knobs, pairs, SCALES_STAGE1, base_text, profile
    )
    log(f"1단계: 유일 덱 텍스트 {len(unique_texts)}개 "
        f"(전수 격자점이었다면 최대 {len(pairs) * 9}개)")

    if len(unique_texts) > MAX_TOTAL_SIMS:
        log(f"!! 유일 시뮬레이션 {len(unique_texts)}개가 상한 {MAX_TOTAL_SIMS}를 넘는다 - 중단")
        return None

    t0 = time.monotonic()
    results_by_text = _run_texts(unique_texts, control_block, backend, max_workers, profile)
    elapsed = time.monotonic() - t0
    log(f"1단계: 시뮬레이션 {len(unique_texts)}개 완료, {elapsed:.1f}s "
        f"(캐시: {backend.stats()})")

    if time.monotonic() - _START > MAX_WALL_SECONDS:
        log(f"!! 벽시계 상한 {MAX_WALL_SECONDS}s 초과 - 1단계 이후 중단")

    # 이탈 카운트는 바이어스 생성기 관련/무관으로 **분해해서 보고만** 한다 -
    # 그 분해를 "무관이면 무효, 관련이면 정상 물리"로 읽어 유효성 판정에 쓰지
    # 않는다(그것이 이번 수정 라운드가 되돌린 사후 규칙 완화다). 사전 등록의
    # 문자 그대로의 규칙은 "이탈 점을 가진 쌍은 무효, 0인 쌍은 유효"이고,
    # `void`가 그 규칙을 그대로 구현한다.
    pair_results = {}
    degn_dev_total = 0
    degn_dev_bias = 0
    degn_dev_non_bias = 0
    for i, j in pairs:
        grid = _pair_grid(knobs, i, j, level_cache, base_text, results_by_text, profile)
        dev = _degn_deviations(grid, profile)
        degn_dev_total += dev
        touches_bias = _touches_bias_generator(knobs[i], knobs[j], profile)
        if touches_bias:
            degn_dev_bias += dev
        else:
            degn_dev_non_bias += dev
        per_measurement = {}
        for name in profile.measurements:
            beats, single_max = _beats(grid, name)
            ia0, ia1 = 0, len(grid.levels_a) - 1
            ib0, ib1 = 0, len(grid.levels_b) - 1
            I, I_rel = _interaction(grid, name, ia0, ia1, ib0, ib1)
            coupled = bool(beats > 0 and I_rel is not None and I_rel > COMPARISON_REL_TOLERANCE)
            per_measurement[name] = {
                "beats": beats,
                "single_axis_max": single_max,
                "I": I,
                "I_rel": I_rel,
                "coupled": coupled,
            }
        pair_results[(knobs[i].label, knobs[j].label)] = {
            "measurements": per_measurement,
            "degn_deviations": dev,
            "touches_bias_generator": touches_bias,
            "void": dev > 0,
        }

    log(f"1단계: degn 이탈 총 {degn_dev_total}개 (바이어스 생성기 노브 관련 "
        f"{degn_dev_bias}개 / 무관 {degn_dev_non_bias}개)")

    return {
        "pairs": pair_results,
        "unique_sims": len(unique_texts),
        "unique_texts": unique_texts,
        "wall_seconds": elapsed,
        "cache_stats": backend.stats(),
        "degn_deviations": degn_dev_total,
        "degn_deviations_bias_generator": degn_dev_bias,
        "degn_deviations_non_bias": degn_dev_non_bias,
        "level_cache": level_cache,
    }


def stage2(knobs, candidate_pairs, base_text, control_block, backend, log, max_workers=None,
           already_computed: set | None = None, sims_budget_remaining: int | None = None,
           profile: "DeckProfile" = None):
    """1단계에서 결합으로 나온 (쌍,측정값)의 쌍들만, 7x7로 확인한다.

    `already_computed`(1단계가 실제로 돌린 덱 텍스트 집합)와 `sims_budget_remaining`은
    예산 판단에만 쓴다 - SCALES_STAGE2가 SCALES_STAGE1의 부분집합(0.5/1.0/2.0)을
    포함하므로 후보 쌍의 9개 격자점은 1단계에서 이미 계산됐고, 여기서 필요한
    **새** 시뮬레이션은 그 차집합뿐이다. `backend`(같은 프로세스의 같은
    CachingSimulator 인스턴스)가 그 재사용을 실제로 수행한다 - 여기서는 예산을
    넘는지 판단하기 위해 미리 세어 볼 뿐이다."""
    profile = profile or TWO_STAGE_PROFILE
    idx_by_label = {k.label: n for n, k in enumerate(knobs)}
    pair_indices = sorted({
        tuple(sorted((idx_by_label[a], idx_by_label[b]))) for a, b in candidate_pairs
    })
    log(f"2단계: 확인 대상 쌍 {len(pair_indices)}개, 스케일 {SCALES_STAGE2}")

    unique_texts, level_cache = _collect_texts_for_pairs(
        knobs, pair_indices, SCALES_STAGE2, base_text, profile
    )
    already_computed = already_computed or set()
    new_texts = unique_texts - already_computed
    log(f"2단계: 유일 덱 텍스트 {len(unique_texts)}개 (1단계와 겹쳐 재사용 가능한 것 "
        f"{len(unique_texts & already_computed)}개, 신규 {len(new_texts)}개)")

    if sims_budget_remaining is not None and len(new_texts) > sims_budget_remaining:
        log(f"!! 2단계 신규 시뮬레이션 {len(new_texts)}개가 남은 예산 "
            f"{sims_budget_remaining}개를 넘는다 - 2단계 생략(부정 결과로 보고)")
        return {
            "skipped": True,
            "reason": f"신규 시뮬레이션 {len(new_texts)}개 필요, 남은 예산 {sims_budget_remaining}개",
            "n_candidate_pairs": len(pair_indices),
            "confirmations": {},
            "pair_degn": {},
            "unique_sims": 0,
            "wall_seconds": 0.0,
            "cache_stats": backend.stats(),
            "degn_deviations": 0,
            "degn_deviations_bias_generator": 0,
            "degn_deviations_non_bias": 0,
        }

    t0 = time.monotonic()
    results_by_text = _run_texts(unique_texts, control_block, backend, max_workers, profile)
    elapsed = time.monotonic() - t0
    stats = backend.stats()
    log(f"2단계: 시뮬레이션 배치 완료, {elapsed:.1f}s (누적 캐시: {stats})")

    # Critical 1 수정: 2단계는 판정 단계이고 격자도 1단계보다 넓다(x0.3..x3 대
    # x0.5..x2) - 극단적인 크기라 상태가 뒤집힐 확률이 1단계보다 낮지 않고
    # 오히려 높은 쪽이다. 1단계와 같은 방식(쌍마다 degn 이탈 카운트 + 바이어스
    # 생성기 관련/무관 분해)으로 여기서도 반드시 확인한다 - 재시뮬레이션이
    # 아니라 이미 돌린 결과(results_by_text)를 다시 읽을 뿐이라 사실상 무료다.
    confirmations = {}
    pair_degn: dict[tuple[str, str], dict] = {}
    degn_dev_total = 0
    degn_dev_bias = 0
    degn_dev_non_bias = 0
    for i, j in pair_indices:
        grid = _pair_grid(knobs, i, j, level_cache, base_text, results_by_text, profile)
        dev = _degn_deviations(grid, profile)
        touches_bias = _touches_bias_generator(knobs[i], knobs[j], profile)
        degn_dev_total += dev
        if touches_bias:
            degn_dev_bias += dev
        else:
            degn_dev_non_bias += dev
        pair_degn[(knobs[i].label, knobs[j].label)] = {
            "degn_deviations": dev,
            "touches_bias_generator": touches_bias,
            "void": dev > 0,
        }
        na, nb = len(grid.levels_a), len(grid.levels_b)
        for name in profile.measurements:
            rels = []
            for ia0, ia1 in itertools.combinations(range(na), 2):
                for ib0, ib1 in itertools.combinations(range(nb), 2):
                    _I, I_rel = _interaction(grid, name, ia0, ia1, ib0, ib1)
                    if I_rel is not None:
                        rels.append(I_rel)
            if not rels:
                confirmations[(knobs[i].label, knobs[j].label, name)] = {
                    "n_subgrids": 0, "median_I_rel": None, "max_I_rel": None, "confirmed": False,
                }
                continue
            median = statistics.median(rels)
            confirmations[(knobs[i].label, knobs[j].label, name)] = {
                "n_subgrids": len(rels),
                "median_I_rel": median,
                "max_I_rel": max(rels),
                "confirmed": median > COMPARISON_REL_TOLERANCE,
            }

    log(f"2단계: degn 이탈 총 {degn_dev_total}개 (바이어스 생성기 노브 관련 "
        f"{degn_dev_bias}개 / 무관 {degn_dev_non_bias}개)")

    return {
        "skipped": False,
        "confirmations": confirmations,
        "pair_degn": pair_degn,
        "n_candidate_pairs": len(pair_indices),
        "unique_sims": len(unique_texts),
        "new_sims": len(new_texts),
        "wall_seconds": elapsed,
        "cache_stats": stats,
        "degn_deviations": degn_dev_total,
        "degn_deviations_bias_generator": degn_dev_bias,
        "degn_deviations_non_bias": degn_dev_non_bias,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None)
    parser.add_argument("--max-workers", type=int, default=None)
    parser.add_argument(
        "--deck", choices=sorted(PROFILES), default="two_stage_opamp",
        help=(
            "어느 덱을 잴 것인가. 기본값 two_stage_opamp는 T17이 실제로 돌린 설정 "
            "그대로다(플래그를 하나도 주지 않으면 T17의 재현 실행이 된다). "
            "bandgap은 T18b - 면적 단계가 실제로 만지는 노브(면적 이득 순위 상위 "
            "12개)만 재고, 바이스테이블 완화책은 붙지 않는다."
        ),
    )
    parser.add_argument(
        "--top-knobs", type=int, default=None,
        help="면적 순위 상위 N개만 쓴다(knob_source=area_ranking인 덱에서만). "
             "기본값은 프로파일이 정한다.",
    )
    parser.add_argument(
        "--stage2-max-pairs", type=int, default=None,
        help=(
            "1단계 결합 후보가 예산 안에서 2단계를 전부 돌리기엔 너무 많을 때, "
            "1단계 최댓값 I_rel 기준 상위 N쌍만 2단계로 확인한다(알파벳/등록 순서가 "
            "아니다 - `--max-knobs 8`이 결정적 노브를 가린 전례가 있다). 나머지는 "
            "'미확인'으로 명시하고 그 개수를 산출물에 남긴다. 기본은 제한 없음 - "
            "이 플래그는 실측된 예산 초과에 대응해 명시적으로만 켠다."
        ),
    )
    args = parser.parse_args()

    profile = PROFILES[args.deck]
    if args.top_knobs is not None:
        profile = replace(profile, top_knobs=args.top_knobs)
    out_path = args.out or OUT_JSON_BY_DECK[profile.key]

    spec = load_spec(profile.spec_path)
    if profile.testbench_name is None:
        tb = spec.canonical
    else:
        tb = next(t for t in spec.testbenches if t.name == profile.testbench_name)
    # 순위를 매기는 덱과 재는 덱은 다를 수 있다. 면적 단계는 **canonical
    # 덱 하나**에서 순위를 매기므로(optimizer.py: `start_text =
    # netlist_texts[canonical_name]`), 다른 테스트벤치에서 재더라도 "면적
    # 단계가 만지는 노브"라는 주장을 지키려면 순위는 canonical에서 나와야 한다.
    if profile.knob_source == "area_ranking":
        rank_tb = (
            spec.canonical if profile.rank_testbench_name is None
            else next(t for t in spec.testbenches if t.name == profile.rank_testbench_name)
        )
    else:
        rank_tb = tb
    # 순위는 `run_area_optimization`이 읽는 것과 같은 **파일 원문**에서 매기고,
    # 시뮬레이션은 include를 절대경로화한 텍스트로 돈다. resolve_includes는
    # 본문을 인라인하지 않고 경로만 다시 쓰므로 tunable 인덱스는 둘이 같다.
    rank_text = open(rank_tb.netlist_path).read()
    base_text = resolve_includes(
        open(tb.netlist_path).read(), os.path.dirname(tb.netlist_path)
    )
    control_block = build_control_block(tb.control_block, profile)

    knob_ranking_record = None
    if profile.knob_source == "area_ranking":
        knobs, skipped, knob_ranking_record = build_knobs_from_area_ranking(rank_text, profile)
        _log(f"면적 이득 순위: tunable {knob_ranking_record['n_tunable']}개 중 "
             f"후보 {knob_ranking_record['n_candidates']}개, 순위 "
             f"{len(knob_ranking_record['ranked_all'])}개, 상위 {len(knobs)}개 사용")
        _log(f"상위 노브: {[k.label for k in knobs]}")
    else:
        knobs, skipped = build_knobs(base_text, profile)
        _log(f"tunable 인덱스: {len(knobs) + len(skipped)}개, 건너뜀 {len(skipped)}개 {skipped}")
    _log(f"덱: {profile.key}, 테스트벤치: {tb.name}, 측정값: {list(profile.measurements)}")
    _log(f"워커 수: {args.max_workers or default_workers()}")

    backend = CachingSimulator(NgspiceBackend())

    s1 = stage1(knobs, base_text, control_block, backend, _log, args.max_workers, profile)
    if s1 is None:
        _log("1단계가 상한 초과로 중단됐다 - 결과 없이 종료")
        return 1

    candidate_pairs = [
        (a, b) for (a, b), rec in s1["pairs"].items()
        if any(v["coupled"] for v in rec["measurements"].values())
    ]
    _log(f"1단계 결합 후보 쌍: {len(candidate_pairs)}개")

    untested_candidates: list = []
    if args.stage2_max_pairs is not None and len(candidate_pairs) > args.stage2_max_pairs:
        # 알파벳/등록 순서가 아니라 1단계 최댓값 I_rel로 순위를 매긴다 - 결정적
        # 쌍을 가리는 것이 이 저장소가 이미 --max-knobs 8에서 치른 실수다.
        def _max_i_rel(pair):
            a, b = pair
            return max(v["I_rel"] or 0.0 for v in s1["pairs"][(a, b)]["measurements"].values())

        ranked = sorted(candidate_pairs, key=_max_i_rel, reverse=True)
        selected = ranked[: args.stage2_max_pairs]
        untested_candidates = ranked[args.stage2_max_pairs :]
        _log(f"2단계 대상이 예산 안에서 처리 가능한 수를 넘어 "
             f"1단계 최댓값 I_rel 상위 {len(selected)}쌍만 확인하고 "
             f"나머지 {len(untested_candidates)}쌍은 미확인으로 남긴다")
        candidate_pairs = selected

    s2 = None
    if candidate_pairs:
        if s1["unique_sims"] > MAX_TOTAL_SIMS:
            _log("!! 이미 1단계에서 상한을 넘었다 - 2단계 생략")
        else:
            budget_remaining = MAX_TOTAL_SIMS - s1["unique_sims"]
            s2 = stage2(
                knobs, candidate_pairs, base_text, control_block, backend, _log, args.max_workers,
                already_computed=s1["unique_texts"], sims_budget_remaining=budget_remaining,
                profile=profile,
            )

    s2_new_sims = 0 if s2 is None else s2.get("new_sims", s2.get("unique_sims", 0))
    total_sims = s1["unique_sims"] + s2_new_sims
    total_wall = time.monotonic() - _START
    budget_exceeded = total_sims > MAX_TOTAL_SIMS or total_wall > MAX_WALL_SECONDS or (
        s2 is not None and s2.get("skipped")
    )

    confirmed_pairs = []
    if s2:
        by_pair = {}
        for (a, b, name), rec in s2["confirmations"].items():
            by_pair.setdefault((a, b), {})[name] = rec
        for (a, b), meas in by_pair.items():
            if any(v["confirmed"] for v in meas.values()):
                confirmed_pairs.append([a, b])

    # Critical 2 수정: 사전 등록 규칙을 문자 그대로 적용한다 - 이탈 점을 가진
    # 쌍은 무효, 0인 쌍은 유효. "바이어스 생성기를 직접 건드렸으니 예상된
    # 물리"라는 예외는 결과를 본 뒤 만든 하위 규칙이라 여기서 쓰지 않는다
    # (limits에 가설로만 남긴다). 유효성은 1단계 자신의 격자 **그리고**(테스트된
    # 경우) 2단계 자신의 격자를 모두 본다 - 2단계 격자가 더 넓어(x0.3..x3)
    # 1단계에서 안 보이던 이탈이 거기서 새로 나올 수 있다.
    s2_pair_degn = s2["pair_degn"] if s2 else {}

    def _pair_void(a: str, b: str) -> bool:
        void1 = s1["pairs"][(a, b)]["void"]
        void2 = s2_pair_degn.get((a, b), {}).get("void", False)
        return bool(void1 or void2)

    valid_confirmed_pairs = [[a, b] for a, b in confirmed_pairs if not _pair_void(a, b)]
    void_confirmed_pairs = [[a, b] for a, b in confirmed_pairs if _pair_void(a, b)]

    headline_check = None
    if profile.headline_pair is not None:
        HEADLINE_PAIR = profile.headline_pair
        HEADLINE_MEASUREMENT = profile.headline_measurement
        headline_in_s1 = HEADLINE_PAIR in s1["pairs"]
        headline_void = _pair_void(*HEADLINE_PAIR) if headline_in_s1 else None
        headline_confirmed = list(HEADLINE_PAIR) in confirmed_pairs
        headline_check = {
            "pair": list(HEADLINE_PAIR),
            "measurement": HEADLINE_MEASUREMENT,
            "confirmed": headline_confirmed,
            "void": headline_void,
            "valid_and_confirmed": bool(headline_confirmed and headline_void is False),
        }
        _log(f"헤드라인 사례 {HEADLINE_PAIR} 확인={headline_confirmed} 무효={headline_void}")

    out = {
        "deck": profile.key,
        "spec": os.path.relpath(profile.spec_path, REPO),
        "testbench": tb.name,
        "rank_testbench": rank_tb.name,
        "measurements": list(profile.measurements),
        "tolerance": COMPARISON_REL_TOLERANCE,
        "knobs": {
            "total_tunable": len(knobs) + len(skipped),
            "used": [k.label for k in knobs],
            "skipped_unparseable": skipped,
        },
        "nodeset": profile.nodeset_line,
        "degn_state_a_threshold": profile.degn_state_a_threshold,
        "bias_generator_refdes": sorted(profile.bias_generator_refdes),
        "knob_source": profile.knob_source,
        "area_gain_ranking": knob_ranking_record,
        "stage1": {
            "scales": list(SCALES_STAGE1),
            "n_pairs": len(s1["pairs"]),
            "unique_sims": s1["unique_sims"],
            "wall_seconds": s1["wall_seconds"],
            "cache_stats": s1["cache_stats"],
            "degn_deviation_points": s1["degn_deviations"],
            "degn_deviation_points_bias_generator_pairs": s1["degn_deviations_bias_generator"],
            "degn_deviation_points_non_bias_pairs": s1["degn_deviations_non_bias"],
            "pairs": {
                f"{a}|{b}": rec for (a, b), rec in s1["pairs"].items()
            },
            "coupled_candidates": [[a, b] for a, b in candidate_pairs],
        },
        "stage2": None if s2 is None else {
            "scales": list(SCALES_STAGE2),
            "skipped": s2.get("skipped", False),
            "skip_reason": s2.get("reason"),
            "n_candidate_pairs": s2["n_candidate_pairs"],
            "unique_sims": s2["unique_sims"],
            "new_sims": s2.get("new_sims", s2["unique_sims"]),
            "wall_seconds": s2["wall_seconds"],
            "cache_stats": s2["cache_stats"],
            "degn_deviation_points": s2.get("degn_deviations", 0),
            "degn_deviation_points_bias_generator_pairs": s2.get("degn_deviations_bias_generator", 0),
            "degn_deviation_points_non_bias_pairs": s2.get("degn_deviations_non_bias", 0),
            "pair_degn": {
                f"{a}|{b}": rec for (a, b), rec in s2_pair_degn.items()
            },
            "confirmations": {
                f"{a}|{b}|{name}": rec for (a, b, name), rec in s2["confirmations"].items()
            },
            "confirmed_pairs": confirmed_pairs,
            "untested_candidates": [[a, b] for a, b in untested_candidates],
            "untested_candidates_reason": (
                None if not untested_candidates else
                f"1단계 최댓값 I_rel 상위 {len(candidate_pairs)}쌍만 예산 안에서 확인했다 - "
                f"나머지 {len(untested_candidates)}쌍은 2단계를 거치지 않았으므로 결합 여부가 "
                f"미정이다(결합 없음으로 읽으면 안 된다)."
            ),
        },
        # Critical 2 수정: 사전 등록의 문자 그대로의 규칙("이탈 점을 가진 쌍은
        # 무효, 0인 쌍은 유효")으로 판정한 결과. 바이어스 생성기 관련/무관
        # 분해는 stage1/stage2의 degn_deviation_points_* 필드에 **진단으로만**
        # 남아 있고, 여기서는 쓰지 않는다.
        "bistability_validity": {
            # **이 실행에서 바이스테이블 확인이 돌았는가.** "확인했고 깨끗했다"와
            # "확인하지 않았다"는 다른 사실이고, 이 필드가 그 둘을 가른다.
            # False일 때 degn_deviation_points가 0인 것은 "이탈이 없었다"가
            # 아니라 "재지 않았다"는 뜻이다.
            "checked": profile.degn_probe is not None,
            "not_checked_reason": None if profile.degn_probe is not None else (
                f"덱 '{profile.key}'에는 바이스테이블 완화책도 상태 프로브도 붙지 않았다. "
                "이 덱이 그 오염을 갖지 않는다는 근거는 별도 측정에 있다"
                "(scripts/dc_solution_uniqueness.py: 바이어스 체인 초기 추정을 다섯 "
                "방향으로, 소자 크기 네 가지, 테스트벤치 덱 네 개에 걸쳐 밀었고 여섯 "
                "프로브가 매번 동일했다). **그러나 이번 실행에서 확인한 것은 아니다.** "
                "degn_deviation_points=0은 '이탈 없음'이 아니라 '재지 않음'이다."
            ),
            "rule": "이탈 점(v(xdut.degn) > degn_state_a_threshold)을 하나라도 가진 "
                    "쌍은 무효, 0인 쌍은 유효 - 사전 등록 문장을 그대로 적용한다.",
            "confirmed_pairs_total": len(confirmed_pairs),
            "confirmed_pairs_valid": valid_confirmed_pairs,
            "confirmed_pairs_valid_count": len(valid_confirmed_pairs),
            "confirmed_pairs_void": void_confirmed_pairs,
            "confirmed_pairs_void_count": len(void_confirmed_pairs),
            "headline_check": headline_check,
        },
        "totals": {
            "total_sims": total_sims,
            "total_wall_seconds": total_wall,
            "budget_exceeded": budget_exceeded,
            "stage2_untested_candidate_count": len(untested_candidates),
        },
        "limits": ([
            f"1단계 결합 후보가 {len(candidate_pairs) + len(untested_candidates)}쌍이었고, 이 "
            f"전부의 2단계(7x7)를 예산({MAX_TOTAL_SIMS} 시뮬) 안에서 돌릴 수 없다는 것을 실측으로 "
            f"확인했다(신규 시뮬레이션 소요가 예산을 크게 넘었다). 그래서 1단계 최댓값 I_rel 상위 "
            f"{len(candidate_pairs)}쌍만 2단계로 확인했다 - 알파벳/등록 순서가 아니라 I_rel 순위로 "
            f"골랐다. 나머지 {len(untested_candidates)}쌍은 stage2.untested_candidates에 있고 결합 "
            f"여부가 미정이다 - 결합 없음으로 읽지 말 것."
        ] if untested_candidates else []) + [
            "1단계의 I_rel은 노브 하나당 격자의 양 끝(x0.5, x2) 두 점으로만 재므로 "
            "국소 계단(예: T16이 발견한 바이스테이블 전환처럼 좁은 폭에서만 나는 이상)에 "
            "민감하다 - 그래서 2단계(7x7, 모든 2x2 부분격자의 중앙값)가 있다.",
            "nominal(tt/1.8/27, 렌더링 없는 덱) 한 점에서만 쟀다 - 코너 확인은 결합 "
            "후보가 정해진 뒤 별도로 한다.",
            "정수 노브(mf)는 반올림 후 1 미만인 scale이 버려지므로 그 축의 '낮은 끝'이 "
            "출하값 자체가 되는 경우가 있다 - knobs.dropped_scales_stage1로 남는다.",
            "결합은 (덱 x 테스트벤치 x 측정값)의 성질이다. 여기서 잰 것은 "
            f"'{tb.name}' 테스트벤치 하나이므로, 이 결과는 같은 덱의 다른 "
            "테스트벤치를 대변하지 않는다.",
        ] + ([] if profile.degn_probe is not None else [
            f"**이 실행에서 바이스테이블 확인은 돌지 않았다**(덱 '{profile.key}'에 프로브도 "
            "완화책도 붙지 않았다). degn_deviation_points=0은 '이탈 없음'이 아니라 "
            "'재지 않음'이다 - bistability_validity.checked 참조. 이 덱이 오염되지 "
            "않았다는 근거는 scripts/dc_solution_uniqueness.py의 별도 측정이고, "
            "그 측정도 다섯 방향 탐침이지 유일성의 증명은 아니다.",
        ]) + ([
            ".nodeset 완화책이 모든 격자점에서 상태 A를 유지한다는 보장은 없다 - 그래서 "
            "매 점에서(1단계·2단계 모두) degn을 확인한다. 사전 등록의 문자 그대로의 "
            "규칙은 '이탈 점을 가진 쌍은 무효, 0인 쌍은 유효'이고(`bistability_validity`), "
            "바이어스 생성기 노브를 직접 건드린 이탈만 예외로 두지 않는다 - 그런 예외는 "
            "결과를 본 뒤 만든 하위 규칙이라 여기서 쓰지 않는다.",
            "가설로만 남긴다(확인 안 됨): 바이어스 생성기 소자(Xp3/Xp4/Xn1/Xn2/Rdeg/"
            "Rstart) 자신을 건드린 격자점의 degn 이탈은 '회로 자체가 달라져 새 동작점에 "
            "선 것'일 수 있고, 그렇다면 T16이 발견한 '무관한 소자 크기 때문에 두 해 중 "
            "하나로 혼돈적으로 튄다'는 오염과는 다른 현상이다. 그러나 "
            "`degn_state_a_threshold`(0.03V)는 상태 A(0.0119V)와 B(0.0626V)의 "
            "중간값이라, 지금 갖고 있는 데이터로는 '소자를 바꿔 바이어스점이 이동했다'와 "
            "'솔버가 상태 B에 착지했다'를 구별할 수 없다 - 이 가설을 확인하려면 그 쌍의 "
            "동작점이 상태 B와 일치하는지(모든 바이어스 노드 비교) 또는 매끄러운 추세를 "
            "따르는지 별도로 확인해야 한다.",
        ] if profile.degn_probe is not None else []) + [
            "1단계는 스크리닝이지 판정이 아니다 - 528쌍 중 다수가 결합으로 나오는 것은 "
            "L/W가 분리된 tunable 노브인 이 덱에서는 놀랍지 않다(같은 트랜지스터의 L과 "
            "W는 W/L 비를 통해 물리적으로 결합돼 있다). 사전 등록이 이 경우를 "
            "예상했다 - 1단계 결과는 보고하되 확정하지 않고, stage2.confirmed_pairs만 "
            "'결합이 실재한다'는 주장의 근거로 쓴다.",
        ],
    }
    # level_cache/dropped 요약을 knobs 밑에 붙인다(직렬화 가능한 형태로).
    dropped_summary = {}
    for idx, knob in enumerate(knobs):
        _levels, dropped = _levels_for(knob, SCALES_STAGE1)
        if dropped:
            dropped_summary[knob.label] = dropped
    out["knobs"]["dropped_scales_stage1"] = dropped_summary

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, sort_keys=True)
    _log(f"결과를 {out_path}에 썼다")
    _log(f"결합 후보(1단계) {len(candidate_pairs)}개, 확정(2단계) {len(confirmed_pairs)}개, "
         f"그중 유효(무이탈) {len(valid_confirmed_pairs)}개 / 무효(이탈 있음) "
         f"{len(void_confirmed_pairs)}개")
    _log(f"1단계 상태 이탈 점 {s1['degn_deviations']}개(바이어스 생성기 관련 "
         f"{s1['degn_deviations_bias_generator']} / 무관 {s1['degn_deviations_non_bias']})")
    _log(f"2단계 상태 이탈 점 {s2.get('degn_deviations', 0) if s2 else 0}개(바이어스 생성기 관련 "
         f"{s2.get('degn_deviations_bias_generator', 0) if s2 else 0} / 무관 "
         f"{s2.get('degn_deviations_non_bias', 0) if s2 else 0})")
    _log(f"총 시뮬 {total_sims}개, 총 벽시계 {total_wall:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
