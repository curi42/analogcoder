"""노브별 **절대 면적 이득**을 계산해 정렬한다.

이 모듈에 LLM이 없는 것이 설계의 핵심이다. 전류 단계는 "어떤 노브가 전류를
줄이는가"를 계산할 방법이 없어 LLM 순위가 필요하지만, **면적은 계산된다** -
노브를 한 스텝 줄인 덱의 총 면적을 재면 끝이고 시뮬레이션도 필요 없다.

부수 효과 둘이 공짜로 따라온다. `nf`는 면적을 곱하지 않으므로 이득이 0이 되어
스스로 빠지고, 튜너가 키운 소자는 가장 크므로 이득도 가장 커 자동으로 앞에
온다. 둘 다 별도 규칙을 적지 않는다 - 적으면 그 규칙이 언젠가 틀린다."""

from dataclasses import dataclass
from typing import Callable

from analogcoder.area import DEFAULT_AREA_MODEL, AreaModel
from analogcoder.netlist import apply_changes, check_refdes_resolution


@dataclass(frozen=True)
class GainEntry:
    """한 스텝 줄였을 때의 **절대** 면적 감소량. gain은 언제나 > 0 이다."""

    refdes: str
    param: str
    gain: float


@dataclass(frozen=True)
class Ranking:
    """정렬 결과와 **빠진 것들**.

    빠진 것을 두 리스트로 나누는 이유: 이득 0은 "줄여도 면적이 안 준다"는
    사실이고, unknown은 "잴 수 없었다"는 사실이다. 합치면 탐색에서 조용히
    사라진 노브가 몇 개인지 아무도 모른다."""

    entries: list[GainEntry]
    zero_gain: list[str]
    unknown: list[str]


def rank_by_area_gain(
    netlist_text: str,
    candidates: list[tuple[str, str, float, bool]],
    make_change: Callable[[str, str, float, bool], dict | None],
    area_model: AreaModel = DEFAULT_AREA_MODEL,
) -> Ranking:
    """`candidates`는 `(refdes, param, current_value, integer)`.

    `make_change`를 **주입받는다**. 스텝 규칙(기하 x0.9, 개수 -1)과 값 서식을
    여기 복제하면 탐색이 실제로 밟는 스텝과 순위가 가정한 스텝이 갈라지고,
    그러면 순위가 일어나지 않을 이득을 기준으로 정렬한다."""
    base = area_model(netlist_text)
    entries: list[GainEntry] = []
    zero_gain: list[str] = []
    unknown: list[str] = []

    for refdes, param, current, integer in candidates:
        label = f"{refdes}.{param}"
        change = make_change(refdes, param, current, integer)
        if change is None:
            # 더 줄일 수 없는 노브. 이득 0이 아니라 잴 수 없는 것이다.
            unknown.append(label)
            continue
        # refdes 해석을 먼저 확인한다. 결정론적 함수로 해석 불가를 판단하고,
        # 불가면 apply_changes를 부르지 않는다.
        resolved, reason = check_refdes_resolution(netlist_text, [change])
        if not resolved:
            unknown.append(label)
            continue
        try:
            moved_text = apply_changes(netlist_text, [change])
        except ValueError:
            # 모호한 refdes. apply_changes가 해석 오류를 일으켰다는 것은
            # check_refdes_resolution이 놓친 경우인데, 두 번째 방어선이다.
            unknown.append(label)
            continue
        try:
            moved = area_model(moved_text)
        except Exception:
            unknown.append(label)
            continue
        if moved.counted != base.counted:
            # 해소되는 소자 집합이 달라졌다 - 두 총합의 차는 이 노브의
            # 이득이 아니라 커버리지 변화다. 그것을 이득이라 부르지 않는다.
            unknown.append(label)
            continue
        gain = base.area - moved.area
        if gain <= 0.0:
            zero_gain.append(label)
            continue
        entries.append(GainEntry(refdes=refdes, param=param, gain=gain))

    # 동률에서 이름으로 갈라 놓는 것은 순서를 결정론적으로 만들기 위해서다 -
    # 순서가 실행마다 달라지면 두 실행의 차이가 탐색 때문인지 정렬 때문인지
    # 구별할 수 없다.
    entries.sort(key=lambda e: (-e.gain, e.refdes, e.param))
    return Ranking(entries=entries, zero_gain=zero_gain, unknown=unknown)
