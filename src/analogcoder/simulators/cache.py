"""내용 주소 시뮬레이션 캐시.

**시뮬레이션은 순수 함수다.** SPICE가 결정론적이라는 것은 이 저장소가 이미
근거로 쓴 전제다 - 밴딧 계열을 기각한 사유가 "평가에 잡음이 없어 줄일 신뢰구간이
없다"였다. 그 전제가 그대로 캐시의 정당성이 된다: 같은 결정 요인이면 같은 값이
나오므로, 두 번째 호출은 재실행이 아니라 조회여도 된다.

**틀린 캐시는 이 저장소가 아홉 번 당한 조용한 게이트보다 나쁘다.** 무력한 게이트는
통과시키기만 하지만, 결정 요인이 하나 빠진 캐시는 **다른 회로의 값을 이 회로의
측정값으로 돌려준다** - 재본 적 없는 사실을 만들어 낸다. 그래서 이 모듈의 설계
중심은 속도가 아니라 **키에 결정 요인이 전부 들어갔는가**이다.

## 키에 들어가는 것

1. **덱 텍스트** - `netlist_path`의 파일 내용 그대로.
2. **testbench_config 전체** - control block만이 아니라 dict 전부를 정규화해
   해싱한다. 오늘 키는 `control_block` 하나뿐이지만, 키가 하나 늘어나는 날
   그것이 조용히 캐시 밖에 남는 것이 정확히 위에 적은 실패 모양이다.
3. **코너 정체성** - 코너는 별도 인자로 오지 않는다. `pvt.render_corner_netlist`가
   코너를 **덱 텍스트 안으로** 렌더링하기 때문이다(공정 include 경로, `.temp`
   줄, `Vdd`의 DC 값 - 셋 다 텍스트다). 그래서 (1)이 코너를 이미 포함한다.
   텍스트에 **없는** 부분이 하나 있다: include 대상 **파일의 내용**.
   `parse_netlist`가 include를 따라가지 않는다는 이 저장소의 경계와 같은 이유로
   여기서도 내용을 읽지 않고, 대신 최상위 `.include` 하나하나에 대해
   `(해석된 절대 경로, 크기, mtime_ns)`를 지문으로 넣는다. PDK 파일이 실행
   도중 바뀌면 지문이 바뀌고 캐시는 미적중이 된다.
   **상대 경로 include는 ngspice의 CWD(=덱이 놓인 디렉터리)에 대해 해석된다**는
   사실도 여기서 흡수된다 - 그래서 CWD 자체를 키에 넣지 않아도 된다. 넣으면
   안 되기도 한다: 코너 덱은 매번 새 임시 디렉터리에 쓰이므로, 디렉터리 이름을
   키에 넣는 순간 **캐시는 영원히 미적중**이 된다.
4. **시뮬레이터 식별자** - `SimulatorBackend.identity()`. ngspice는 해석된
   바이너리 절대 경로 + `ngspice -v`가 보고하는 버전 + timeout을 싣는다.
   timeout이 결정 요인인 이유는 그것이 `status="error"`를 만들 수 있기 때문이다.

## 캐시하지 않는 것

`RawSimResult.cacheable`이 False인 결과는 저장하지 않는다. timeout과 "바이너리를
못 찾음"이 그것이다 - 벽시계와 환경에 달린 결과라 **순수 함수가 아니다**. 이
둘을 캐시하면 한 번의 부하 급증이 그 실행 내내 같은 코너를 실패로 못박는다.

## 적중/미적중은 무조건 로그로 남긴다

게이트에 적용하는 규칙을 캐시에도 적용한다: **한 번도 안 맞는 캐시와 아예 안
붙은 캐시가 `history.jsonl`에서 같아 보이면 안 된다.** 그래서 로그는 조건부가
아니라 호출마다 나간다(`sim_cache` 이벤트), 적중이든 아니든.

## 범위는 실행 하나

프로세스 안 메모리에 두고 `CachingSimulator` 인스턴스의 수명 = 실행 하나다.
디스크에 남겨 실행 사이에 재사용하려면 "덱 정체성이 실행을 넘어서도 같다"는
논증이 필요한데, 그것은 PDK 파일과 ngspice 바이너리가 그 사이에 안 바뀐다는
주장이고 지문만으로는 부족하다(nested include는 지문에 없다). 범위 밖으로 둔다.
"""

import hashlib
import json
import os
import subprocess
import threading

# `netlist.py`의 비공개 이름을 그대로 쓴다. 복제하면 두 정규식이 갈라질 수 있고,
# 갈라진 쪽이 include 하나를 놓치면 그것이 곧 키에서 빠진 결정 요인이 된다.
from analogcoder.netlist import _INCLUDE_RE, _LIB_CALL_RE, _quoted_path
from analogcoder.simulators.base import RawSimResult, SimulatorBackend

# 지문이 훑는 깊이. **최상위 덱 한 겹뿐이다.**
#
# 독점 PDK 의 실제 모양은 `덱 -> corner_sig01.inc -> PROCESS.LIB` 두 단계이므로,
# 이 값이 1 인 한 **PDK 파일 자체는 캐시 키에 없다** - 코너 파일이 바뀌면
# 미적중이 되지만 PROCESS.LIB 만 바뀌면 적중한다.
#
# 한 겹 더 따라가지 않기로 한 근거 셋:
# 1. 깊이 2 의 대상을 *찾으려면* 깊이 1 의 파일을 **읽어야** 한다. 지문이 파일을
#    읽지 않는 것은 이 모듈의 설계 결정이고("PDK 모델 파일은 수십 MB이고,
#    시뮬레이션마다 읽으면 캐시가 아끼려던 것을 도로 쓴다"), 최상위 `.include`
#    가 그 수십 MB 파일을 **직접** 가리키는 덱이 실제로 있다. `simulation_key`
#    는 `run()` 마다 돈다.
# 2. 깊이 1 은 대상 환경체인이 정확히 2단이라는 **미확인 가정**이다. PROCESS.LIB
#    이 다시 무언가를 끌어오면 깊이 2 도 같은 자리에서 조용히 멈춘다. 확인
#    안 된 수를 고르는 것은 이 저장소가 금지하는 추측이다.
# 3. 벤치마크 20개 덱의 캐시 키가 바뀐다(`pdk_corner.inc` 가 sky130 모델
#    파일들을 다시 include 한다).
#
# **보이지 않는다는 사실은 조용하지 않다** - 이 값이 `sim_cache` 이벤트에
# 매번 실린다. 깊이를 늘리는 사람은 이 상수와 그것을 못박은 테스트
# (`test_the_company_corner_file_is_nested_and_the_fingerprint_stops_at_the_deck`)
# 를 함께 고쳐야 한다.
INCLUDE_FINGERPRINT_DEPTH = 1

_VERSION_CACHE: dict[str, str] = {}
_VERSION_LOCK = threading.Lock()


def ngspice_version(binary: str) -> str:
    """`ngspice -v`의 첫 줄. 프로세스당 바이너리 경로별로 한 번만 부른다.

    실패는 예외가 아니라 문자열로 남긴다 - 버전을 못 읽는 것 자체가 키의 일부다.
    못 읽는 상태에서 캐시가 "버전 미상"으로 통일되면 안 되므로, 오류 종류를
    그대로 싣는다."""
    with _VERSION_LOCK:
        if binary in _VERSION_CACHE:
            return _VERSION_CACHE[binary]
    try:
        proc = subprocess.run([binary, "-v"], capture_output=True, text=True, timeout=20)
        text = (proc.stdout + proc.stderr).strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        version = next((ln for ln in lines if "ngspice" in ln.lower()), lines[0] if lines else "")
    except (OSError, subprocess.SubprocessError) as exc:
        version = f"<unavailable: {type(exc).__name__}>"
    with _VERSION_LOCK:
        _VERSION_CACHE[binary] = version
    return version


def include_fingerprints(netlist_text: str, netlist_dir: str) -> list[list]:
    """덱이 선언한 최상위 `.include` 대상들의 지문.

    경로는 ngspice가 쓰는 것과 **같은 규칙**으로 해석한다 - 절대 경로는 그대로,
    상대 경로는 덱이 놓인 디렉터리(NgspiceBackend가 CWD로 넘기는 그 디렉터리)
    기준. 존재하지 않는 파일은 `None` 지문으로 남긴다: "없다"도 사실이고,
    없다가 생기는 것은 결과를 바꾸는 변화다.

    내용을 읽지 않는 이유는 `parse_netlist`가 include를 따라가지 않는 것과 같다 -
    PDK 모델 파일은 수십 MB이고, 시뮬레이션마다 읽으면 캐시가 아끼려던 것을
    도로 쓴다. `(크기, mtime_ns)`는 그 자리를 대신하는 값싼 대리다. 이것이
    실행을 넘는 캐시를 범위 밖으로 두는 이유이기도 하다.

    `.include`/`.inc` 와 **`.lib` 호출**을 둘 다 본다. `.lib` 이 빠져 있던
    동안에는 `.lib` 만 쓰는 덱의 지문이 `[]` 이었고, 그것은 "이 덱엔 include 가
    하나도 없다" 와 캐시 키에서 **글자 그대로 같았다** - PDK 파일이 실행 도중
    바뀌어도 옛 값이 계속 나온다. `.lib` **정의**(`.lib <섹션>` … `.endl`)는
    파일 참조가 아니므로 잡지 않는다(`netlist._LIB_CALL_RE` 주석 참조).

    깊이는 `INCLUDE_FINGERPRINT_DEPTH` - 최상위 덱 한 겹뿐이다."""
    fingerprints = []
    for path, _raw_was_relative in _referenced_paths(netlist_text, netlist_dir):
        try:
            stat = os.stat(path)
        except OSError:
            fingerprints.append([path, None, None])
        else:
            fingerprints.append([path, stat.st_size, stat.st_mtime_ns])
    fingerprints.sort()
    return fingerprints


def _referenced_paths(netlist_text: str, netlist_dir: str):
    """`(해석된 절대 경로, 원문이 상대 경로였는가)` 를 선언 순서대로.

    `.include` 와 `.lib` 호출을 한 자리에서 해석한다 - 지문과 계량이 같은
    목록을 봐야 "지문에 없다" 와 "계량에 없다" 가 어긋나지 않는다."""
    for regex in (_INCLUDE_RE, _LIB_CALL_RE):
        for match in regex.finditer(netlist_text):
            raw, _quote = _quoted_path(match, 2)
            relative = not os.path.isabs(raw)
            path = os.path.join(netlist_dir, raw) if relative else raw
            yield os.path.abspath(path), relative


def include_summary(netlist_text: str, netlist_dir: str) -> dict:
    """지문이 무엇을 봤는지의 계량. **캐시 키에는 들어가지 않는다.**

    키에 넣으면 같은 사실을 두 번 세는 것이고, 벤치마크 덱의 키가 계량을
    추가했다는 이유만으로 바뀐다.

    세 가지를 구별 가능하게 만든다:
    - `depth` - 어느 깊이까지 봤는가. 중첩된 코너 파일(생산 덱의 실제 모양)의
      내용이 키에 **없다**는 사실이 실행마다 관측된다.
    - `relative` - 상대 경로가 몇 개 들어왔는가. 사용자는 코너 경로를 절대
      경로로 고쳐 넣을 *수도* 있다고 했지 상대 경로가 안 온다고 보장하지
      않았다. 상대 경로가 조용히 지나가면 안 된다.
    - `unresolved` - 그 자리에 파일이 없어 `None` 지문이 된 것이 몇 개인가.
      지금까지 "지문을 못 잡음" 과 "이 덱엔 include 가 없다" 가 구별 불가였다.
    """
    counts = {
        "depth": INCLUDE_FINGERPRINT_DEPTH,
        "include": len(_INCLUDE_RE.findall(netlist_text)),
        "lib": len(_LIB_CALL_RE.findall(netlist_text)),
        "relative": 0,
        "unresolved": 0,
    }
    for path, relative in _referenced_paths(netlist_text, netlist_dir):
        counts["relative"] += int(relative)
        if not os.path.exists(path):
            counts["unresolved"] += 1
    return counts


def simulation_key(
    netlist_text: str, netlist_dir: str, testbench_config: dict, simulator_identity: str
) -> str:
    """네 결정 요인의 내용 주소. 모듈 docstring이 각 항목의 근거를 적는다.

    `sort_keys=True`와 `default=repr`을 함께 쓴다: 전자는 dict 순서가 키를
    바꾸지 못하게 하고, 후자는 JSON으로 못 옮기는 값이 들어와도 **조용히 빠지지
    않게** 한다(직렬화 실패로 죽는 대신 repr이 키에 들어간다)."""
    payload = {
        "netlist": netlist_text,
        "testbench_config": testbench_config,
        "includes": include_fingerprints(netlist_text, netlist_dir),
        "simulator": simulator_identity,
    }
    blob = json.dumps(payload, sort_keys=True, default=repr).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class CachingSimulator(SimulatorBackend):
    """`SimulatorBackend` 하나를 감싸 같은 입력의 재실행을 조회로 바꾼다.

    **결정론적 층에만 붙인다.** 감싸는 대상은 `SimulatorBackend.run`이지
    `cli.py`의 `simulate_fn`이 아니다 - 후자는 LLM이 끼어 control block이
    수렴해 가는 경로라 입력이 닫힌 집합이 아니다. 다만 시뮬레이터 에이전트가
    **도구로** 부르는 것은 결국 이 `run`이고, control block이 키에 들어가므로
    그 경로도 안전하게 캐시된다: 에이전트가 control block을 바꾸면 키가 바뀐다.

    스레드 안전하다. 코너 스윕이 이것을 여러 워커에서 동시에 부른다."""

    def __init__(self, inner: SimulatorBackend, log_event=None, enabled: bool = True):
        self.inner = inner
        self.log_event = log_event
        self.enabled = enabled
        self._entries: dict[str, RawSimResult] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def identity(self) -> str:
        return self.inner.identity()

    def stats(self) -> dict:
        with self._lock:
            return {"hits": self.hits, "misses": self.misses, "entries": len(self._entries)}

    def _emit(self, data: dict) -> None:
        """로그는 락 **안에서** 나간다. `RunState.log_event`는 파일을 열어 한 줄을
        덧붙이는데, 워커 여럿이 동시에 하면 줄이 섞일 수 있다. 여기서 직렬화하면
        이 모듈이 만드는 동시성에 대해서는 그 문제가 없어진다."""
        if self.log_event is None:
            return
        with self._lock:
            self.log_event("sim_cache", data)

    def run(self, netlist_path: str, testbench_config: dict) -> RawSimResult:
        if not self.enabled:
            self._emit({"hit": False, "enabled": False})
            return self.inner.run(netlist_path, testbench_config)

        netlist_dir = os.path.dirname(os.path.abspath(netlist_path))
        try:
            with open(netlist_path) as f:
                netlist_text = f.read()
        except OSError:
            # 덱을 못 읽으면 키를 만들 수 없다. 추측해서 키를 짓는 대신 캐시를
            # 통째로 비켜 간다 - 안쪽 백엔드가 같은 실패를 자기 방식으로 보고한다.
            self._emit({"hit": False, "unkeyable": "netlist_unreadable"})
            return self.inner.run(netlist_path, testbench_config)

        key = simulation_key(netlist_text, netlist_dir, testbench_config, self.identity())
        # 계량은 **매번** 실린다. 조건부로 내면 "include 가 없는 덱" 과
        # "계량이 사라졌다" 가 로그에서 같아진다.
        includes = include_summary(netlist_text, netlist_dir)

        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self.hits += 1
                stats = {"hits": self.hits, "misses": self.misses, "entries": len(self._entries)}
            else:
                self.misses += 1
                stats = None

        if cached is not None:
            # **복사해서 돌려준다.** RawSimResult는 frozen이 아니고 measurements는
            # dict다. 같은 객체를 두 번 내주면 한 소비자의 in-place 수정이 다음
            # 적중의 값을 바꾼다 - 캐시가 값을 지어내는 또 하나의 경로다.
            if self.log_event is not None:
                with self._lock:
                    self.log_event(
                        "sim_cache",
                        {"hit": True, "key": key[:16], "includes": includes, **stats},
                    )
            return RawSimResult(
                status=cached.status,
                measurements=dict(cached.measurements),
                raw_log=cached.raw_log,
                warnings=list(cached.warnings),
                cacheable=cached.cacheable,
                failure_kind=cached.failure_kind,
            )

        result = self.inner.run(netlist_path, testbench_config)
        if result.cacheable:
            with self._lock:
                self._entries[key] = result
        with self._lock:
            stats = {"hits": self.hits, "misses": self.misses, "entries": len(self._entries)}
            if self.log_event is not None:
                self.log_event(
                    "sim_cache",
                    {
                        "hit": False,
                        "key": key[:16],
                        "stored": result.cacheable,
                        "failure_kind": result.failure_kind,
                        "includes": includes,
                        **stats,
                    },
                )
        return RawSimResult(
            status=result.status,
            measurements=dict(result.measurements),
            raw_log=result.raw_log,
            warnings=list(result.warnings),
            cacheable=result.cacheable,
            failure_kind=result.failure_kind,
        )


def attach_log_event(sim_backend, log_event) -> None:
    """백엔드가 캐시라면 로그 싱크를 붙인다. 아니면 아무 일도 하지 않는다.

    `build_corner_simulate`와 `run_full_pvt_sweep`은 `log_event`를 이미 들고
    있으면서 백엔드는 주입받는다. 캐시를 만든 쪽(`cli.py`)이 싱크를 못 붙였을
    때 적중/미적중이 `history.jsonl`에서 통째로 사라지는 것을 막는 자리다."""
    if isinstance(sim_backend, CachingSimulator) and sim_backend.log_event is None:
        sim_backend.log_event = log_event
