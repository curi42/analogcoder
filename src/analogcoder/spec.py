import os
from dataclasses import dataclass, field

import yaml


def axis_corner_id(process: str, voltage: float, temperature: float) -> str:
    """축 좌표에서 코너의 정체성 문자열을 만든다. **생성자는 여기 하나뿐이다.**

    로더, `CornerPoint.__post_init__`, 그리고 산출물 dict를 다시 코너로 읽는
    `corner_selection._as_point`가 전부 이 함수를 부른다. 세 곳이 각자 같은
    f-string을 쓰면 어긋나는 날 `_as_point`가 어떤 경로에서 온 코너는 받고
    어떤 경로에서 온 코너는 거부한다 - 재진입/체크포인트 재개처럼 드문
    경로에서만 터져 로그에서 원인이 안 보인다.

    **형식은 오늘의 `corner_selection.label`과 바이트 동일하다.** 그것이 이
    변경의 회귀 기준값이다."""
    return f"{process}/{voltage}/{temperature}"


@dataclass(frozen=True)
class CornerPoint:
    """스윕할 한 점. **불변이며 필드로 비교·해시된다** - `CornerSet`의 집합
    연산 전부가 그 위에 서 있다.

    **정체성은 `corner_id`이고 좌표는 선택이다.** 대상 흐름에서 사인오프가
    요구하는 것은 데카르트 곱이 아니라 사람이 고른 서명 코너 N개이고, 그
    선택은 이 저장소 밖의 코드가 한다. 코너 파일은 불투명하다 - 안을
    들여다보고 축을 해석하는 것은 이 저장소가 금지한 추측(파일명·이름에서
    뜻을 읽기)이다. 그래서 좌표를 필수로 두면 그런 코너를 아예 표현할 수
    없다. 반대로 좌표를 **지우면** 벤치마크 경로의 렌더링(`pdk_corner_ss.inc`
    선택, `.temp` 주입, 전압 치환)이 되돌릴 수 없이 사라진다. 둘 다 산다.

    `process`/`voltage`/`temperature`는 **축 선언으로 만들어졌을 때만 채워지는
    원본 기록**이다. 라벨로 선언된 코너에서는 비어 있고, 열거에서 축을
    역산해 채우지 않는다.

    `payload`는 이 코너를 **실현하는** 파일의 절대경로다. 조합 덱에서 코너
    슬롯에 채워지는 것이 그 파일이고, 이 저장소는 그 **내용을 절대 읽지
    않는다** - 경로는 선언된 입력이고 내용은 불투명하다.

    **`factors`(축 분해)는 일부러 넣지 않았다.** 라벨 전용 단계에서는 항상
    비어 있을 수밖에 없고 - 채울 코드 경로가 존재할 수 없다 - 그러면 "축이
    없는 코너다"와 "축을 채우는 코드가 없다"가 같은 값이 된다. 조용히 무력한
    게이트를 자료형에 적용한 모양이다. 선택 필드라 필요해지는 날 0곳 수정으로
    들어온다.

    여기(스펙 모듈)에 사는 이유: 코너는 스펙이 **선언**하는 것이고, `pvt.py`는
    그것을 렌더링하고 도는 쪽이다. 방향도 그렇게만 성립한다 - `pvt.py`가
    `spec.py`를 import하므로 반대는 순환이다. `pvt`가 이 이름을 그대로
    재수출하므로 `from analogcoder.pvt import CornerPoint`는 계속 동작한다.

    **여기에 NaN을 절대 넣지 마라.** `NaN != NaN`이므로 그런 값은 자기
    자신과도 같지 않고, 그러면 `point not in cs.corners`가 이미 있는 코너에
    대해 참이 된다 - `grown_with`가 다시 추가하고 중복 검사가 진단을
    `ValueError`로 바꾼다. 오늘 이것을 만드는 코드는 없다(좌표는 스펙에서
    온다). 코너를 **측정**에서 유도하는 첫 사람을 위한 규칙이다.

    **라벨에도 같은 부류의 함정이 방향만 바꿔 재등장한다.** `corner_sig01`,
    `Corner1001`, `" corner_sig01"`은 서로 다른 세 코너다(set 크기 3). 필드 기반
    중복 검사는 그것을 중복으로 보지 않으므로, 방어선은 **선언 자리**의
    중복 거부(`_explicit_corners`)이고 반드시 유지해야 한다. 정규화(strip,
    대소문자)는 코드가 추측하면 안 된다."""

    corner_id: str | None = None
    process: str | None = None
    voltage: float | None = None
    temperature: float | None = None
    payload: str | None = None

    def __post_init__(self) -> None:
        """정체성이 없으면 좌표에서 **유도**한다. 둘 다 없으면 거부한다.

        유도를 여기 두는 이유는 오늘의 모든 생성부(`CornerPoint(process=...)`)가
        그대로 동작해야 하기 때문이고, 거부하는 이유는 정체성 없는 코너는
        가리킬 이름이 없기 때문이다."""
        if self.corner_id is not None:
            return
        if self.process is None or self.voltage is None or self.temperature is None:
            raise ValueError(
                f"a CornerPoint needs either a corner_id or all three coordinates, "
                f"got {self!r}"
            )
        object.__setattr__(
            self, "corner_id", axis_corner_id(self.process, self.voltage, self.temperature)
        )


@dataclass
class Criterion:
    name: str
    measurement: str
    operator: str
    threshold: float
    unit: str | None = None


@dataclass
class PVTCorners:
    """스윕할 코너의 **열거**. 축 선언은 로더에서 여기로 전개되는 설탕이다.

    **왜 곱이 아니라 목록인가.** 대상 흐름에서 사인오프가 요구하는 것은
    데카르트 곱 전체가 아니라 **사람이 고른 서명 코너 N개**이고, 그 선택은
    이 저장소 밖의 코드가 한다(2026-07-29 확인). 그런 부분 격자 - 금지 조합이
    빠진 집합 - 는 **어떤 축 선언으로도 만들 수 없다.** 목록은 곱을 표현할 수
    있지만(전개해서 나열) 곱은 임의의 목록을 표현할 수 없으므로, 열거형이 두
    세계의 정확한 공통 표현이다.

    그래서 **나중에 "곱으로 되돌리는 최적화"를 하면 안 된다** - 표현력이
    줄어들고, 곱으로 코너를 생성하면 존재하지 않는 조합을 가리키게 된다.

    `process`/`voltage`/`temperature`는 축 선언으로 만들어진 경우에만 채워지는
    **원본 기록**이다. 명시 목록으로 선언되면 비어 있다 - 그 경우 축이라는
    것이 존재하지 않기 때문이고, 없는 축을 열거에서 역산해 채우는 것은 이
    저장소가 금지한 추측이다(파일명에서 축 정체성을 읽는 것과 같은 부류).
    코너를 소비하는 코드는 `corners`만 읽는다.
    """

    corners: list[CornerPoint] | None = None
    process: list[str] = field(default_factory=list)
    voltage: list[float] = field(default_factory=list)
    temperature: list[float] = field(default_factory=list)
    nominal: str | None = None
    """조합형 테스트벤치에서 "임계값이 정해진 그 덱"이 어느 코너인지.

    단일 파일 경로에는 이런 것이 없고 있어서도 안 된다 - 거기서 nominal은
    렌더링을 거치지 않은 **덱 그 자체**이고, `tt/27`은 실제 코너일 뿐
    nominal이 아니다. 조합 모델에는 그 "덱 그 자체"가 존재하지 않는다:
    코너가 입력이므로 코너를 고르기 전에는 덱이 없다. 그래서 **사람이
    선언**해야 한다 - 이름이나 순서에서 알아내면 그것이 금지된 추측이다."""

    def __post_init__(self):
        """축만 주고 만들면 **여기서 한 번** 전개된다 - 곱은 생성 시점의
        설탕이고, 그 뒤로는 아무도 곱을 다시 만들지 않는다.

        전개를 생성자에 두는 이유는 `corners`가 유일한 진실이 되게 하기
        위해서다. 소비자가 축을 읽어 곱을 다시 만들 수 있으면 명시 목록으로
        선언된 부분 격자에서 그 경로가 조용히 틀린 코너 집합을 만든다."""
        if self.corners is None:
            self.corners = [
                CornerPoint(process=p, voltage=v, temperature=t)
                for p in self.process
                for v in self.voltage
                for t in self.temperature
            ]


@dataclass
class OptimizeSpec:
    """스펙에 여유가 있을 때 무엇을 어디까지 줄일지. 선언이 없으면
    최적화 단계 자체를 돌리지 않는다 - 조용히 안 도는 것과 명시적으로
    안 도는 것은 다르다."""

    objective: str
    area_budget: float
    guard_band: float


@dataclass(frozen=True)
class CoverageConfig:
    """ε-근접 피복으로 코너 씨앗을 고를 때의 두 값.

    **둘 다 스펙이 선언해야 하고 기본값이 없다.** `epsilon`은 이 덱에서
    유도되는 값이지 상수가 아니다 - 코드가 하나 골라 두면 근거 없는 숫자가
    다른 덱까지 따라간다. 이 저장소가 가드 밴드와
    `COMPARISON_REL_TOLERANCE`를 정한 방식과 같은 규율이다.

    `tau`는 목표 피복률이고 예산 k는 여기서 **유도**된다. 정수 상한
    (`max_corners`)을 두지 않는 이유는 사람이 고르는 것이 숫자가 아니라
    "기준의 몇 %를 보겠는가"라는 뜻이 있는 값이어야 하기 때문이다."""

    epsilon: float
    tau: float


@dataclass
class CornerReduction:
    """중간 반복의 코너 축소 설정.

    enabled=False면 오늘 동작(nominal 한 점)이 그대로다. pvt_corners가 선언되지
    않은 스펙에서는 축소할 것이 없으므로 이 블록이 있어도 아무 일도 하지
    않으며, 그 사실은 cli가 로그로 남긴다."""

    enabled: bool = True
    retry_budget: int = 2
    probe: bool = True
    coverage: CoverageConfig | None = None
    """`None`이면 오늘의 argmax 합집합 - 씨앗이 바이트 동일하다. 기본값
    객체를 넣지 않는 것은 '선언하지 않았다'와 '기본값으로 선언했다'를
    구별하기 위해서다."""


@dataclass(frozen=True)
class FragmentRef:
    """조합 덱을 이루는 조각 하나의 **선언**.

    `kind`는 `"file"`(절대경로) 또는 `"corner_slot"`(코너가 채워지는 자리).
    `tunable`은 이 조각이 튜너가 고치고 버전으로 남는 조각이라는 뜻이고,
    조합형 테스트벤치에 정확히 하나 있어야 한다 - 그것이
    `Testbench.netlist_path`가 되므로 `RunState`·체크포인트·`resolve_includes`
    소비자가 전부 오늘 그대로 동작한다."""

    kind: str
    path: str | None = None
    tunable: bool = False


@dataclass
class Testbench:
    name: str
    netlist_path: str
    analyses: list[str]
    control_block: str
    criteria: list[Criterion]
    fragments: tuple[FragmentRef, ...] | None = None
    """`None`이면 오늘의 단일 파일 테스트벤치 - 그 경로는 한 글자도 바뀌지
    않는다. 조합형이면 조각 선언 목록이고, 덱은 시뮬레이션 **직전에**
    `compose.deck_for`가 만든다."""

    @property
    def corner_slot_index(self) -> int | None:
        if self.fragments is None:
            return None
        for index, ref in enumerate(self.fragments):
            if ref.kind == "corner_slot":
                return index
        return None


@dataclass
class TargetSpec:
    circuit_name: str
    testbenches: list[Testbench]
    pvt_corners: PVTCorners | None = None
    optimize: OptimizeSpec | None = None
    corner_reduction: CornerReduction | None = None

    @property
    def canonical(self) -> Testbench:
        return self.testbenches[0]

    @property
    def all_criteria(self) -> list[Criterion]:
        return [c for tb in self.testbenches for c in tb.criteria]

    def nominal_corner(self) -> CornerPoint | None:
        """조합형 테스트벤치의 "임계값이 정해진 그 덱"에 해당하는 코너.

        선언이 없으면 `None`이고, 그때 조합 경로는 nominal 덱을 만들 수 없다고
        **시끄럽게** 실패한다 - 아무 코너나 골라 nominal이라고 부르는 것이
        이 저장소가 금지한 추측이다."""
        if self.pvt_corners is None or self.pvt_corners.nominal is None:
            return None
        for corner in self.pvt_corners.corners:
            if corner.corner_id == self.pvt_corners.nominal:
                return corner
        return None


# 스펙이 선언할 수 있는 비교 연산자. **이 저장소의 모든 소비자가 같은 뜻으로
# 구현한 것만** 들어 있다.
#
# `==`는 일부러 빠져 있다. `evaluate_criteria`는 그것을 판정할 수 있지만
# (`_OPERATORS`에 있다) 그 아래의 세 소비자가 서로 다르게 읽는다:
# `judge_tools.relative_slack`과 `baseline_ratio_allowances`는 상한(`<=`)
# 분기로 떨어지고, `guard_band_violations`는 `==`를 아예 건너뛴다. 결과가
# 조용한 거짓 주장이다 - `vref == 1.2`가 1.0을 재면 `relative_slack`이
# **+0.167**(실패 중인 기준에 양수 여유)을 돌려주므로 `_tightest_slack`이
# 과소 보고하고, `baseline_ratio_allowances`는 여유분을 주는데
# `guard_band_violations`는 그 여유분을 적용하지 않아 리포트가 "0 (every
# criterion is corner- or ratio-guarded)"라고 쓰면서 실제로는 가드가 0이다.
#
# 세 모듈이 서로 다른 답을 갖고 있으므로 **셋 중 하나를 조용히 고르는 대신
# 거절한다** - `render_corner_report`가 다시 쓸 수 없는 공급 라인을 반쯤
# 처리하는 대신 예외를 던지는 것과 같은 모양이다. 오타난 연산자(`>==`,
# `=>`)도 같은 문에서 걸린다: 오늘은 그런 것이 `evaluate_criteria`의
# `_OPERATORS[...]`에서 `KeyError`로, 즉 스펙 로드가 아니라 **판정 시점에**
# 터진다.
#
# 되살리려면: `==`를 판정하는 방법을 세 소비자 모두에서 정의하고(허용 오차를
# 무엇으로 할지가 그 결정의 핵심이다), 그 다음 여기에 넣는다. 오늘 출하된
# 스펙 중 `==`를 쓰는 것은 없다(벤치마크 14개 스펙의 210개 기준 전부가
# `>=` 아니면 `<=`다).
ALLOWED_OPERATORS = (">=", ">", "<=", "<")


def _load_criteria(raw_criteria: list[dict]) -> list[Criterion]:
    criteria = []
    for c in raw_criteria:
        operator = c["operator"]
        if operator not in ALLOWED_OPERATORS:
            raise ValueError(
                f"criterion {c['name']!r} declares operator {operator!r}, which this "
                f"repository does not implement consistently. Allowed: "
                f"{list(ALLOWED_OPERATORS)}. (An '==' criterion is read three "
                f"different ways by relative_slack, baseline_ratio_allowances and "
                f"guard_band_violations, so it is refused rather than silently "
                f"resolved to one of them.)"
            )
        criteria.append(
            Criterion(
                name=c["name"],
                measurement=c["measurement"],
                operator=operator,
                threshold=float(c["threshold"]),
                unit=c.get("unit"),
            )
        )
    return criteria


def _axis(raw_pvt: dict, key: str) -> list:
    """축 하나를 리스트로 꺼낸다. 검사하는 것은 **모양뿐**이고 내용은 보지
    않는다.

    `process: [tt, ss, ff]`를 대괄호 없이 `process: tt`로 적으면 YAML이
    문자열을 주고, 그 문자열은 순회 가능하므로 아무 예외 없이 문자 단위로
    풀려 CornerPoint(process='t') 넷이 만들어진다. 그 코너는
    존재하지 않는 pdk_corner_t.inc를 include하므로 45코너 전부가 NaN·FAIL이
    되고, 사람이 보는 표층 신호는 "회로가 모든 코너에서 망가졌다"가 된다 -
    원인은 대괄호 두 개다. 빈 리스트도 같은 부류로 거부한다: 축 하나가
    비면 itertools.product가 0점을 내므로, pvt_corners를 선언한 스펙이
    코너를 하나도 안 돌면서 아무 말도 하지 않는다. 같은 파일의
    _load_corner_reduction이 이미 세워 둔 규율("조용히 0으로 동작하느니
    크게 터진다")을 이 함수에도 적용한다.

    **내용은 검사하지 않는다.** process 라벨을 알려진 집합(tt/ss/ff)으로
    제한하면 라벨로 코너를 선언하는 흐름(대상 환경에서 코너가 좌표가
    아니라 이름으로 오는 경우)을 로더가 구조적으로 막게 된다 - 이 저장소가
    "이름으로 레일을 알아보기"를 금지하는 것과 같은 이유로, 모양은 사실이고
    내용은 추측이다."""
    if key not in raw_pvt:
        raise ValueError(f"pvt_corners.{key} is required")
    value = raw_pvt[key]
    if isinstance(value, str) or not isinstance(value, list):
        raise ValueError(
            f"pvt_corners.{key} must be a list, not {type(value).__name__}: {value!r} "
            f"(a bare scalar is iterated element-by-element and yields nonsense corners)"
        )
    if not value:
        raise ValueError(
            f"pvt_corners.{key} must not be empty: an empty axis makes the corner grid "
            f"zero points while the spec still declares pvt_corners"
        )
    return value


def _load_pvt_corners(raw: dict, spec_dir: str) -> PVTCorners | None:
    raw_pvt = raw.get("pvt_corners")
    if raw_pvt is None:
        return None
    if not isinstance(raw_pvt, dict):
        raise ValueError(
            f"pvt_corners must be a mapping of process/voltage/temperature axes, not "
            f"{type(raw_pvt).__name__}: {raw_pvt!r}"
        )

    if "corners" in raw_pvt:
        # 축 선언과 명시 목록이 함께 있으면 어느 쪽이 이기는지 **추측해야**
        # 한다. 이 저장소는 그런 자리에서 조용히 한쪽을 고르지 않는다.
        axes_present = [k for k in ("process", "voltage", "temperature") if k in raw_pvt]
        if axes_present:
            raise ValueError(
                f"pvt_corners declares both an explicit 'corners' list and the axis "
                f"key(s) {axes_present} - declare one or the other, never both"
            )
        return _explicit_corners(raw_pvt["corners"], raw_pvt.get("nominal"), spec_dir)

    if "nominal" in raw_pvt:
        # 축 선언 경로에서 nominal은 **덱 그 자체**이고 코너가 아니다. 여기서
        # 코너 하나를 nominal이라 부르게 두면 `tt/27`이 nominal로 둔갑하는,
        # `corner_selection.NOMINAL`이 존재하는 바로 그 이유가 무너진다.
        raise ValueError(
            "pvt_corners.nominal only applies to a composed testbench's corner slot, "
            "where the deck does not exist until a corner is chosen. On the axis-declared "
            "path nominal is the unrendered deck itself, not any corner"
        )

    process = _axis(raw_pvt, "process")
    for entry in process:
        # 코너 이름은 include 파일 이름의 조각이 되므로 문자열이어야 한다.
        # `process: [tt, 1.8]`(들여쓰기 실수로 전압이 섞인 경우)은 float를
        # 문자열로 포매팅해 pdk_corner_1.8.inc를 만든다.
        if not isinstance(entry, str):
            raise ValueError(
                f"pvt_corners.process entries must be strings, not "
                f"{type(entry).__name__}: {entry!r}"
            )

    def numbers(key: str) -> list[float]:
        out = []
        for entry in _axis(raw_pvt, key):
            try:
                out.append(float(entry))
            except (TypeError, ValueError):
                # float()의 기본 메시지는 어느 축인지 말하지 않는다.
                raise ValueError(
                    f"pvt_corners.{key} entries must be numbers, not {entry!r}"
                ) from None
        return out

    # 전개는 PVTCorners.__post_init__ 이 한다 - 곱을 만드는 자리를 하나로 둔다.
    return PVTCorners(
        process=process,
        voltage=numbers("voltage"),
        temperature=numbers("temperature"),
    )


_AXIS_KEYS = ("process", "voltage", "temperature")


def _label_corner(index: int, entry: dict, spec_dir: str) -> CornerPoint:
    """라벨로 선언된 서명 코너 하나. `id` + `include`.

    **`include`가 가리키는 파일의 내용은 읽지 않는다.** 코너 파일은
    불투명하고, 안을 들여다보고 축을 해석하는 것은 파일명에서 뜻을 읽는 것과
    같은 부류의 추측이다. 조합 덱의 코너 슬롯에 들어가는 것은 이 절대경로를
    가리키는 `.include` 한 줄뿐이다."""
    mixed = [key for key in _AXIS_KEYS if key in entry]
    if mixed:
        raise ValueError(
            f"pvt_corners.corners[{index}] declares both an id and the axis key(s) "
            f"{mixed}: a corner is either a label whose file realises it, or a point in "
            f"an axis grid - which one wins would have to be guessed"
        )
    if not isinstance(entry["id"], str) or not entry["id"]:
        raise ValueError(f"pvt_corners.corners[{index}].id must be a non-empty string")
    include = entry.get("include")
    if not isinstance(include, str) or not include:
        raise ValueError(
            f"pvt_corners.corners[{index}] has no 'include': a label with no file behind "
            f"it cannot be realised, and a corner slot filled with nothing would run some "
            f"other corner's deck under this corner's name"
        )
    return CornerPoint(
        corner_id=entry["id"], payload=os.path.join(spec_dir, include)
    )


def _explicit_corners(raw_corners, nominal, spec_dir: str) -> PVTCorners:
    """사람이 고른 서명 코너 목록. 축 선언으로는 표현할 수 없는 부분 격자다.

    `process`/`voltage`/`temperature`는 **비워 둔다**. 열거에서 축을 역산해
    채우면 선언되지 않은 조합이 축 목록에 나타나고, 그것을 읽는 다음 사람이
    "이 스펙은 곱을 돈다"고 읽는다. 없는 사실을 만들지 않는다.
    """
    if not isinstance(raw_corners, list):
        raise ValueError(
            f"pvt_corners.corners must be a list of corner mappings, not "
            f"{type(raw_corners).__name__}: {raw_corners!r}"
        )
    if not raw_corners:
        # 빈 목록은 "코너 없음"이 아니라 선언 실수다 - 코너를 안 돌 생각이면
        # `pvt_corners` 블록을 안 쓰면 되고, 그 경우는 이미 따로 로그된다.
        raise ValueError("pvt_corners.corners is empty - omit the pvt_corners block instead")

    points: list[CornerPoint] = []
    for index, entry in enumerate(raw_corners):
        if not isinstance(entry, dict):
            raise ValueError(
                f"pvt_corners.corners[{index}] must be a mapping with process/voltage/"
                f"temperature, not {type(entry).__name__}: {entry!r}"
            )
        if "id" in entry:
            points.append(_label_corner(index, entry, spec_dir))
            continue
        for key in ("process", "voltage", "temperature"):
            if key not in entry:
                # 빠진 좌표를 기본값으로 채우면 N개 코너가 조용히 같은 조건을
                # 돌면서 코너별 값으로 보고된다.
                raise ValueError(f"pvt_corners.corners[{index}] has no '{key}': {entry!r}")
        if not isinstance(entry["process"], str):
            raise ValueError(
                f"pvt_corners.corners[{index}].process must be a string, not "
                f"{type(entry['process']).__name__}: {entry['process']!r}"
            )
        numbers = {}
        for key in ("voltage", "temperature"):
            try:
                numbers[key] = float(entry[key])
            except (TypeError, ValueError):
                raise ValueError(
                    f"pvt_corners.corners[{index}].{key} must be a number, not {entry[key]!r}"
                ) from None
        points.append(
            CornerPoint(
                process=entry["process"],
                voltage=numbers["voltage"],
                temperature=numbers["temperature"],
            )
        )

    # `CornerSet`이 중복을 불변식으로 거부하므로, 통과시키면 나중에 진단이
    # `ValueError`로 바뀌어 사유가 흐려진다. 선언 자리에서 거부한다.
    # **정체성으로 본다.** 라벨 모델에서 `corner_sig01`/`Corner1001`/`" corner_sig01"`
    # 은 필드 기반 중복 검사가 구별하지 못하는 서로 다른 세 코너이고, 정규화
    # (strip, 대소문자)는 코드가 추측하면 안 된다. 그래서 선언 자리가 방어선이다.
    seen: set[str] = set()
    for point in points:
        if point.corner_id in seen:
            raise ValueError(
                f"pvt_corners.corners has a duplicate corner: {point.corner_id}"
            )
        seen.add(point.corner_id)

    if nominal is not None and not any(p.corner_id == nominal for p in points):
        raise ValueError(
            f"pvt_corners.nominal is {nominal!r}, which names no declared corner: "
            f"{[p.corner_id for p in points]}"
        )
    return PVTCorners(corners=points, nominal=nominal)


def _load_optimize(raw: dict) -> OptimizeSpec | None:
    raw_opt = raw.get("optimize")
    if raw_opt is None:
        return None
    return OptimizeSpec(
        objective=raw_opt["objective"],
        area_budget=float(raw_opt["area_budget"]),
        guard_band=float(raw_opt["guard_band"]),
    )


def _load_corner_reduction(raw: dict) -> CornerReduction | None:
    block = raw.get("corner_reduction")
    if block is None:
        return None

    # Helper to validate and extract booleans — fail loud like int/float do.
    # bool("false") returns True (non-empty string), silently inverting explicit false.
    def get_bool(key: str, default: bool) -> bool:
        value = block.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(
                f"corner_reduction.{key} must be a boolean, not {type(value).__name__}: {value!r}"
            )
        return value

    # A negative budget silently behaves as 0 - the re-entry loop compares
    # `attempt >= retry_budget` and 0 >= -1 on the first pass - so a spec that
    # believes it enabled re-entry gets none, with nothing said. Same reason
    # get_bool above fails loud rather than coercing.
    retry_budget = int(block.get("retry_budget", 2))
    if retry_budget < 0:
        raise ValueError(
            f"corner_reduction.retry_budget must be >= 0, not {retry_budget}: a "
            f"negative budget silently behaves as 0 (no re-entry at all)"
        )

    raw_coverage = block.get("coverage")
    coverage = None
    if raw_coverage is not None:
        if not isinstance(raw_coverage, dict):
            raise ValueError(
                f"corner_reduction.coverage must be a mapping with 'epsilon' and 'tau', "
                f"not {type(raw_coverage).__name__}: {raw_coverage!r}"
            )
        for key in ("epsilon", "tau"):
            if key not in raw_coverage:
                raise ValueError(
                    f"corner_reduction.coverage.{key} is required - it is derived from "
                    f"this deck's own measurements, so a code-side default would carry "
                    f"an ungrounded number to every other deck"
                )
            if isinstance(raw_coverage[key], bool) or not isinstance(
                raw_coverage[key], (int, float)
            ):
                raise ValueError(
                    f"corner_reduction.coverage.{key} must be a number, not "
                    f"{type(raw_coverage[key]).__name__}: {raw_coverage[key]!r}"
                )
        epsilon = float(raw_coverage["epsilon"])
        tau = float(raw_coverage["tau"])
        if not 0.0 <= epsilon <= 1.0:
            # 이 메시지는 조건과 어긋나면 안 된다: epsilon==1.0은 이 조건에서
            # **허용된다**(0.0 <= epsilon <= 1.0의 등호). 예전 문구("above 1.0
            # every corner covers every criterion")는 1.0 자체가 안전한 것처럼
            # 읽히는데, 1.0에서 이미 허용오차가 |worst| 그 자체와 같아져
            # (0에서 2*worst까지가 덮인다) 대부분의 판별력을 잃는다 - "1.0
            # 초과부터 위험하다"는 주장은 방어할 수 없다. 조건은 그대로 두고
            # 문구만 실제 경계(허용오차가 커질수록 판별력이 줄고, 범위 밖은
            # 아예 거부한다)를 말하도록 고친다.
            raise ValueError(
                f"corner_reduction.coverage.epsilon must be in [0, 1], got {epsilon}: "
                f"a negative tolerance has no meaning, and epsilon scales the tolerance "
                f"band as a fraction of the worst-case magnitude - already at 1.0 the band "
                f"spans back to zero, so values outside [0, 1] only widen it further and "
                f"stop the seed from discriminating near-worst corners from unrelated ones"
            )
        if not 0.0 < tau <= 1.0:
            raise ValueError(
                f"corner_reduction.coverage.tau must be in (0, 1], got {tau}: "
                f"tau is the fraction of criteria the seed must cover, and 0 covers none"
            )
        coverage = CoverageConfig(epsilon=epsilon, tau=tau)

    return CornerReduction(
        enabled=get_bool("enabled", True),
        retry_budget=retry_budget,
        probe=get_bool("probe", True),
        coverage=coverage,
    )


def _reject_name_collisions(testbenches: list[Testbench]) -> None:
    """이름으로 색인된 슬롯 하나에 두 값이 들어가는 스펙을 거부한다.

    판정 경로에 last-wins 병합이 두 군데 있다:

      * `pvt.run_full_pvt_sweep`의 `combined_worst_corners`는 **criterion
        이름**으로 색인된 dict를 update로 채운다. 같은 이름을 두 테스트벤치가
        쓰면 뒤에 온 쪽의 최악 코너가 앞의 것을 덮어써, 앞 테스트벤치가
        실제로 위반하는데도 overall_pass가 True로 나온다.
      * `cli.simulate_fn`의 `merged_measurements`는 **measurement 이름**으로
        색인된 dict를 update로 채운다. 두 테스트벤치가 같은 이름의 측정값을
        내면 앞의 값이 버려지고, judge는 두 회로의 기준을 한 회로의 값으로
        판정한다.

    orchestrator.py는 초점(relevance 전용) 경로에서 정확히 이 이유로 이미
    합집합 병합으로 고쳐졌다 - 판정 경로는 덮어쓰기로 남아 있다. 소비자
    두 곳이 다른 에이전트 소유라 여기서 계약 위반 입력을 로더가 거부한다:
    `_load_corner_reduction`의 음수 예산과 같은 규율이다.

    **measurement 이름을 한 테스트벤치 *안에서* 나눠 쓰는 것은 정상이고
    막지 않는다.** 두 기준이 한 측정값의 양쪽을 보는 two-sided window
    (`vbgout_min`/`vbgout_max`)가 출하 스펙에 실제로 있고, 그 충돌은
    corner_sim이 "위반하는 쪽을 이긴 값으로 고른다"로 이미 해소한다.
    여기서 거부하는 것은 **테스트벤치 경계를 넘는** 공유뿐이다."""
    seen_criteria: dict[str, str] = {}
    seen_measurements: dict[str, str] = {}
    for tb in testbenches:
        for criterion in tb.criteria:
            owner = seen_criteria.get(criterion.name)
            if owner is not None:
                raise ValueError(
                    f"criterion name {criterion.name!r} is declared by both testbench "
                    f"{owner!r} and {tb.name!r}: the PVT sweep indexes worst corners by "
                    f"criterion name, so one verdict would silently overwrite the other"
                )
            seen_criteria[criterion.name] = tb.name
        for criterion in tb.criteria:
            owner = seen_measurements.setdefault(criterion.measurement, tb.name)
            if owner != tb.name:
                raise ValueError(
                    f"measurement name {criterion.measurement!r} is produced by both "
                    f"testbench {owner!r} and {tb.name!r}: per-testbench measurements are "
                    f"merged into one name-keyed dict, so one value would be dropped"
                )


def _load_fragments(tb: dict, spec_dir: str) -> tuple[tuple[FragmentRef, ...], str]:
    """조합형 테스트벤치의 조각 선언과, 그중 **버전으로 남는** 조각의 경로.

    분석 3이 정한 버전 관리 경계가 여기 있다: 조각만 버전으로 남기고 조합은
    시뮬레이션 직전에 한다. 튜너가 고치는 조각을 `netlist_path`로 돌려주므로
    `RunState`·`checkpoint`·`resolve_includes`의 소비자가 전부 오늘 그대로
    동작한다."""
    raw_fragments = tb["compose"]
    if not isinstance(raw_fragments, list) or not raw_fragments:
        raise ValueError(
            f"testbench {tb['name']!r}: compose must be a non-empty list of fragments"
        )

    refs: list[FragmentRef] = []
    for index, entry in enumerate(raw_fragments):
        if not isinstance(entry, dict):
            raise ValueError(
                f"testbench {tb['name']!r}: compose[{index}] must be a mapping "
                f"({{file: ...}} or {{corner_slot: true}}), not {entry!r}"
            )
        if entry.get("corner_slot"):
            if "file" in entry:
                raise ValueError(
                    f"testbench {tb['name']!r}: compose[{index}] is both a file and the "
                    f"corner_slot"
                )
            refs.append(FragmentRef(kind="corner_slot"))
            continue
        if "file" not in entry:
            raise ValueError(
                f"testbench {tb['name']!r}: compose[{index}] has neither 'file' nor "
                f"'corner_slot': {entry!r}"
            )
        refs.append(
            FragmentRef(
                kind="file",
                path=os.path.join(spec_dir, entry["file"]),
                tunable=bool(entry.get("tunable", False)),
            )
        )

    tunable = [ref for ref in refs if ref.tunable]
    if len(tunable) != 1:
        raise ValueError(
            f"testbench {tb['name']!r}: exactly one composed fragment must be marked "
            f"tunable (found {len(tunable)}) - that fragment is the one the tuner edits "
            f"and the one the run versions, so with none there is nothing to tune and "
            f"with two the version stack would have to guess which"
        )
    slots = [ref for ref in refs if ref.kind == "corner_slot"]
    if len(slots) > 1:
        raise ValueError(
            f"testbench {tb['name']!r}: more than one corner_slot ({len(slots)}) - the "
            f"same corner file pulled in twice is a duplicate-directive collision, which "
            f"ngspice reports (if at all) only as a redefinition warning nobody reads"
        )
    return tuple(refs), tunable[0].path


def _load_testbench(tb: dict, spec_dir: str) -> Testbench:
    has_compose = "compose" in tb
    has_netlist = "netlist" in tb
    if has_compose and has_netlist:
        # 어느 쪽이 이기는지 **추측해야** 한다. 축 선언과 명시 코너 목록이
        # 함께 있을 때와 같은 규율.
        raise ValueError(
            f"testbench {tb['name']!r} declares both 'netlist' and 'compose' - declare "
            f"one or the other, never both"
        )
    if not has_compose and not has_netlist:
        raise ValueError(f"testbench {tb['name']!r} declares neither 'netlist' nor 'compose'")

    fragments = None
    if has_compose:
        fragments, netlist_path = _load_fragments(tb, spec_dir)
    else:
        netlist_path = os.path.join(spec_dir, tb["netlist"])

    return Testbench(
        name=tb["name"],
        netlist_path=netlist_path,
        analyses=tb["analyses"],
        control_block=tb["control_block"],
        criteria=_load_criteria(tb["criteria"]),
        fragments=fragments,
    )


def _reject_unrealisable_corners(testbenches: list[Testbench], pvt: PVTCorners | None) -> None:
    """조합형 테스트벤치와 선언된 코너가 서로 실현 가능한지.

    전부 같은 실패 모양을 막는다: **N개 코너가 전부 같은 조건을 돌면서
    코너별 값으로 보고되는 것.** `netlist_startup.cir`의 45코너가 실은 15조건이던
    사고와 같은 계열이고, 그때 아무 로그도 다르지 않았다.

    **판정 단위는 스펙이 아니라 (코너, 테스트벤치) 짝이다.** 두 실현 경로가
    있고 각각이 코너에게 요구하는 것이 다르다 - 재작성 경로는 좌표를, 조합
    경로는 payload 를 요구한다. 한 스펙이 두 종류의 테스트벤치를 함께 선언할 수
    있으므로, 스펙 단위로 "조합형이 하나라도 있는가"를 물으면 남은 짝이 검사
    없이 통과한다."""
    composed = [tb for tb in testbenches if tb.fragments is not None]
    single_file = [tb for tb in testbenches if tb.fragments is None]

    # 라벨 코너 -> 재작성 경로. 아래 `missing` 검사(좌표 코너 -> 슬롯)의 거울짝이고,
    # 오래 한쪽만 있었다. 좌표가 없는 코너가 `render_corner_report`에 닿으면 셋을
    # 전부 `None`으로 쓴다 - `pdk_corner_None.inc`, `.temp None`, `DC None` - 그리고
    # `states`는 셋 다 `applied`로 적힌다. 재작성이 일어났음을 증명해야 할 기록이
    # 돌지 못하는 덱에 대해 성공을 증명하는 것이고, 그 줄들이 아예 없는 덱이면 셋 다
    # `absent`가 되면서 모든 코너가 같은 덱을 돈다.
    if pvt is not None and single_file:
        coordinateless = [c.corner_id for c in pvt.corners if c.process is None]
        if coordinateless:
            raise ValueError(
                f"corner(s) {coordinateless} carry no coordinates while testbench(es) "
                f"{[tb.name for tb in single_file]} are single-file: the rewrite path has "
                f"nothing to substitute and would write None into the process include, the "
                f"'.temp' and the supply line while reporting all three as applied. A corner "
                f"declared by label can only be realised by a composed testbench that names "
                f"it in a corner_slot"
            )

    if not composed:
        return
    slotted = [tb for tb in composed if tb.corner_slot_index is not None]

    if pvt is not None:
        without = [tb.name for tb in composed if tb.corner_slot_index is None]
        if without:
            raise ValueError(
                f"composed testbench(es) {without} have no corner_slot while the spec "
                f"declares pvt_corners: every corner would run the same deck and be "
                f"reported under its own name"
            )

    if not slotted:
        return
    if pvt is None:
        raise ValueError(
            f"composed testbench(es) {[tb.name for tb in slotted]} declare a corner_slot "
            f"but the spec declares no pvt_corners: there is nothing to fill the slot with"
        )
    missing = [c.corner_id for c in pvt.corners if c.payload is None]
    if missing:
        raise ValueError(
            f"corner(s) {missing} carry no payload to fill a corner_slot with. A corner "
            f"reaching a composed deck has to name the file that realises it"
        )
    if pvt.nominal is None:
        raise ValueError(
            f"composed testbench(es) {[tb.name for tb in slotted]} declare a corner_slot, "
            f"so pvt_corners.nominal must name the corner the thresholds were set at. "
            f"There is no unrendered deck in the composed model - the deck does not exist "
            f"until a corner is chosen - and picking one by name or position is a guess"
        )


def refuse_composed_testbenches(spec: "TargetSpec", *, consumer: str, detail: str) -> None:
    """조합형 테스트벤치를 **경로**로 소비하는 진입점에서의 거부.

    `Testbench.netlist_path`는 조합형에서 버전 관리되는 **tunable 조각**을
    가리킨다 - 자극도 코너도 없는, 회로가 아닌 파일이다. 그것을 조용히 도는
    쪽이 훨씬 나쁘다는 근거는 실측이다: 조각 뷰에서 `check_stimulus_untouched`가
    자극 변경을 approved=True로 통과시키고(게이트가 **열린 채** 실패한다),
    `signal_path`가 `AMP drives vdd`라는 거짓 구조 주장을 되살리며,
    `.option scale`이 다른 조각에 실려 있으면 같은 제안의 면적 판정이 뒤집힌다.

    **거부가 한 자리에만 적혀 있으면 다음 진입점이 조용히 그 경계를 넘는다.**
    실제로 그랬다 - `cli._run`에는 있고 `cli_curate`에는 없었다. 같은 규칙을
    두 곳이 각자 적는 것은 `compose._include_paths`가 `netlist.py`의 규칙을
    손으로 복제했다가 두 방향으로 갈라진 것과 같은 모양이므로, 문장은 하나다."""
    composed = [tb.name for tb in spec.testbenches if tb.fragments is not None]
    if not composed:
        return
    raise ValueError(
        f"composed testbench(es) {composed} are not wired into {consumer} yet: {detail} "
        f"For a composed testbench Testbench.netlist_path is the versioned tunable "
        f"fragment - a file with no stimulus and no corner, which is not a runnable deck. "
        f"The corner sweep path does compose (see pvt.deck_for_corner), so this refusal "
        f"is the boundary, not the feature's limit."
    )


def load_spec(path: str) -> TargetSpec:
    with open(path) as f:
        raw = yaml.safe_load(f)

    spec_dir = os.path.dirname(os.path.abspath(path))
    testbenches = [_load_testbench(tb, spec_dir) for tb in raw["testbenches"]]
    _reject_name_collisions(testbenches)
    pvt_corners = _load_pvt_corners(raw, spec_dir)
    _reject_unrealisable_corners(testbenches, pvt_corners)

    return TargetSpec(circuit_name=raw["circuit_name"], testbenches=testbenches, pvt_corners=pvt_corners, optimize=_load_optimize(raw), corner_reduction=_load_corner_reduction(raw))
