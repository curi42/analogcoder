from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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
    cacheable: bool = True


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
