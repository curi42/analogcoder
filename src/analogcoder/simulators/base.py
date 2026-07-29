from abc import ABC, abstractmethod
from dataclasses import dataclass, field


# 실패의 **종류** -> 그 종류를 캐시해도 되는가.
#
# 이 표가 있기 전에는 `cacheable=False` 가 `NgspiceBackend.run` 안의 예외 두
# 개에 하드코딩돼 있었고, 규칙("환경이 낸 결과만 False")은 주석으로만
# 존재했다. 그러면 **새 실패 축이 생길 때 그것이 조용히 캐시 가능 쪽에
# 합류한다** - 이 저장소가 `area_check`/`refdes_check` 가 같은 `feedback` 키를
# 쓴다는 이유로 이미 값을 치른 모양이다.
#
# 첫 후보는 HSPICE 의 **라이선스 거부**다. 정의상 timeout 과 같은 범주(환경이
# 낸 결과)인데 ngspice 에 그 축이 없어서 판단이 내려진 적이 없을 뿐이다.
# 여기에 넣는 것은 HSPICE 어댑터가 실제 문자열/종료 코드를 확인한 뒤의 일이며,
# **그때까지는 미분류 종류로 취급되어 캐시되지 않는다**(아래 fail-closed).
#
# 값의 의미: True = 결정론적이라 캐시해도 된다. False = 환경이 낸 결과다.
FAILURE_KINDS: dict[str, bool] = {
    "timeout": False,  # 벽시계에 달렸다
    "binary_missing": False,  # 환경 설정에 달렸다
    "convergence": True,  # 같은 덱 + 같은 옵션이면 같은 자리에서 실패한다
    "nonzero_exit": True,
    "no_measurements": True,
}


def is_cacheable(failure_kind: str | None) -> bool:
    """이 실패 종류의 결과를 내용 주소 캐시에 담아도 되는가.

    **미분류 종류는 닫는다(False).** 결정론적이라고 확인되지 않은 실패를
    캐시에 굳히면 한 번의 환경 요동이 실행 내내 같은 점을 실패로 못박는다.
    반대로 닫혔을 때의 대가는 재실행 한 번뿐이므로 비대칭이 분명하다.
    예외를 던지지 않는 이유: `run_orchestration` 의
    `except (AgentExecutionError, ValueError)` 가 그것을 **깨끗한 FAIL 로
    세탁**해서 파서/분류 버그가 "이 회로는 실패했다"는 정상 산출물이 된다."""
    if failure_kind is None:
        return True
    return FAILURE_KINDS.get(failure_kind, False)


@dataclass
class RawSimResult:
    status: str  # "success" | "convergence_failure" | "error"
    measurements: dict[str, float]
    raw_log: str
    warnings: list[str] = field(default_factory=list)
    # 이 결과를 내용 주소 캐시에 담아도 되는가. 기본은 True - 시뮬레이션은
    # 결정 요인의 순수 함수라는 것이 이 저장소의 전제다. False로 두는 것은
    # **환경이 낸 결과**뿐이다: timeout(벽시계에 달렸다)과 바이너리 부재.
    # 그런 결과를 캐시하면 한 번의 부하 급증이 실행 내내 같은 점을 실패로
    # 못박는다. simulators/cache.py의 docstring을 볼 것.
    #
    # **여전히 백엔드가 정한다.** `is_cacheable`는 그 판단의 근거를 한 자리에
    # 모아 둔 것이지 이 필드를 대신하지 않는다 - 대역(fake) 백엔드는
    # failure_kind 없이 cacheable만 쓴다.
    cacheable: bool = True
    # 실패의 종류. 성공이면 None. `status` 의 세 값(schemas.py 가 고정한다)은
    # **늘리지 않는다** - 이것은 그 옆에 붙는 순수 메타데이터다. status 가
    # "error" 하나로 접고 있는 것들(timeout / 바이너리 부재 / 비영 종료 /
    # 측정 0개)이 여기서 갈린다.
    failure_kind: str | None = None


class SimulatorBackend(ABC):
    @abstractmethod
    def run(self, netlist_path: str, testbench_config: dict) -> RawSimResult:
        ...

    def identity(self) -> str:
        """이 백엔드가 무엇으로 시뮬레이션하는가를 한 문자열로.

        캐시 키의 네 번째 결정 요인이다. 같은 덱·같은 control block이라도
        시뮬레이터가 다르면 다른 값이 나올 수 있으므로, 이것이 키에서 빠지면
        캐시는 다른 엔진의 측정값을 이 엔진의 값으로 돌려준다.

        기본 구현은 클래스 이름뿐이다 - 대역(fake) 백엔드에는 그것으로 충분하고,
        실제 엔진을 부르는 백엔드는 **반드시 재정의해서** 바이너리와 버전을
        실어야 한다(NgspiceBackend가 그렇게 한다)."""
        return f"{type(self).__module__}.{type(self).__qualname__}"
