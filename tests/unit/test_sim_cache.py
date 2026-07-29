"""내용 주소 시뮬레이션 캐시의 결정 요인 검사.

이 파일이 지키는 것은 속도가 아니라 **키에 결정 요인이 전부 들어갔는가**이다.
하나라도 빠지면 캐시는 다른 회로의 값을 이 회로의 측정값으로 돌려준다 - 이
저장소가 아홉 번 당한 조용한 게이트보다 나쁜 실패다(무력한 게이트는 통과시키기만
하지만, 틀린 캐시는 **없는 사실을 만든다**).

그래서 결정 요인마다 "그것만 바꾸면 미적중이 된다"를 하나씩 못박는다.
"""

import os
import threading

import pytest

from analogcoder.simulators.base import RawSimResult, SimulatorBackend
from analogcoder.simulators.cache import CachingSimulator, attach_log_event, simulation_key


class _CountingBackend(SimulatorBackend):
    """호출을 세고, 부를 때마다 **다른** 값을 낸다.

    같은 값을 내면 캐시가 적중했는지 재실행했는지 값으로는 구분할 수 없다.
    호출 번호를 측정값에 실어야 "이 값은 두 번째 실행에서 나왔다"가 보인다."""

    def __init__(self, ident="fake-sim-v1"):
        self.calls = 0
        self.ident = ident
        self.seen: list[tuple[str, dict]] = []

    def identity(self) -> str:
        return self.ident

    def run(self, netlist_path, testbench_config):
        self.calls += 1
        self.seen.append((netlist_path, dict(testbench_config)))
        return RawSimResult(
            status="success",
            measurements={"gain_db": float(self.calls)},
            raw_log=f"call {self.calls}",
            warnings=[],
        )


def _deck(tmp_path, text="* deck\nR1 a b 1k\n", name="deck.cir"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_the_same_deck_and_control_block_is_simulated_once(tmp_path):
    inner = _CountingBackend()
    cache = CachingSimulator(inner)
    path = _deck(tmp_path)

    first = cache.run(path, {"control_block": ".control\nop\n.endc"})
    second = cache.run(path, {"control_block": ".control\nop\n.endc"})

    assert inner.calls == 1
    assert first.measurements == second.measurements == {"gain_db": 1.0}
    assert cache.stats() == {"hits": 1, "misses": 1, "entries": 1}


def test_a_changed_deck_text_is_a_different_key(tmp_path):
    inner = _CountingBackend()
    cache = CachingSimulator(inner)
    path = _deck(tmp_path)

    cache.run(path, {"control_block": "cb"})
    # 같은 **경로**, 다른 내용. 경로로 키를 잡았다면 여기서 조용히 적중한다.
    with open(path, "w") as f:
        f.write("* deck\nR1 a b 2k\n")
    cache.run(path, {"control_block": "cb"})

    assert inner.calls == 2


def test_a_changed_control_block_is_a_different_key(tmp_path):
    inner = _CountingBackend()
    cache = CachingSimulator(inner)
    path = _deck(tmp_path)

    cache.run(path, {"control_block": "ac dec 10 1 1e9"})
    cache.run(path, {"control_block": "op"})

    assert inner.calls == 2


def test_any_extra_testbench_config_key_is_a_determinant(tmp_path):
    """오늘 `testbench_config`는 control_block 하나지만, 키가 하나 늘어나는 날
    그것이 조용히 캐시 밖에 남는 것이 이 모듈이 막으려는 실패 모양이다."""
    inner = _CountingBackend()
    cache = CachingSimulator(inner)
    path = _deck(tmp_path)

    cache.run(path, {"control_block": "op", "temperature": 27})
    cache.run(path, {"control_block": "op", "temperature": 125})

    assert inner.calls == 2


def test_a_different_simulator_identity_is_a_different_key(tmp_path):
    """같은 캐시를 공유하는 두 백엔드가 서로 다른 엔진일 수 있다. 식별자가
    키에서 빠지면 ngspice의 값이 HSPICE의 값으로 돌아간다."""
    path = _deck(tmp_path)
    key_a = simulation_key("* deck\n", str(tmp_path), {"control_block": "op"}, "ngspice|46")
    key_b = simulation_key("* deck\n", str(tmp_path), {"control_block": "op"}, "hspice|2021")
    assert key_a != key_b
    assert os.path.exists(path)


def test_the_corner_reaches_the_key_through_the_rendered_deck(tmp_path):
    """코너는 별도 인자로 오지 않는다 - `render_corner_netlist`가 include 경로,
    `.temp`, `Vdd`의 DC 값을 **텍스트 안으로** 쓴다. 그 셋 중 어느 하나만 달라도
    키가 갈라져야 한다."""
    base = '.include "/pdk/pdk_corner.inc"\n.temp 27\nVdd vdd 0 DC 1.8\n'
    ss = '.include "/pdk/pdk_corner_ss.inc"\n.temp 27\nVdd vdd 0 DC 1.8\n'
    hot = '.include "/pdk/pdk_corner.inc"\n.temp 125\nVdd vdd 0 DC 1.8\n'
    low = '.include "/pdk/pdk_corner.inc"\n.temp 27\nVdd vdd 0 DC 1.62\n'

    keys = {
        simulation_key(text, str(tmp_path), {"control_block": "op"}, "sim")
        for text in (base, ss, hot, low)
    }
    assert len(keys) == 4


def test_the_include_targets_contents_are_fingerprinted(tmp_path):
    """덱 텍스트에 **없는** 결정 요인은 include 대상 파일의 내용 하나뿐이다.
    PDK 파일이 바뀌면 같은 덱이 다른 값을 낸다."""
    inc = tmp_path / "pdk_corner.inc"
    inc.write_text("* models v1\n")
    text = f'.include "{inc}"\nR1 a b 1k\n'

    key_before = simulation_key(text, str(tmp_path), {"control_block": "op"}, "sim")
    # 크기와 mtime이 함께 바뀐다.
    inc.write_text("* models v2 - different length\n")
    key_after = simulation_key(text, str(tmp_path), {"control_block": "op"}, "sim")

    assert key_before != key_after


def test_a_relative_include_resolves_against_the_deck_directory(tmp_path):
    """상대 경로 include는 ngspice의 CWD(=덱이 놓인 디렉터리)에 대해 해석된다.
    그래서 같은 덱 텍스트라도 **다른 디렉터리**에 놓이면 다른 파일을 읽는다.
    디렉터리 이름 자체는 키에 넣지 않는다(코너 덱은 매번 새 임시 디렉터리에
    쓰이므로 그러면 캐시가 영원히 미적중이 된다) - 대신 해석된 include 지문이
    그 차이를 흡수한다."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "models.inc").write_text("* A models\n")
    (b / "models.inc").write_text("* B models are longer\n")
    text = '.include "models.inc"\nR1 a b 1k\n'

    assert simulation_key(text, str(a), {"control_block": "op"}, "sim") != simulation_key(
        text, str(b), {"control_block": "op"}, "sim"
    )


def test_the_same_deck_in_two_temp_dirs_still_hits(tmp_path):
    """코너 스윕은 매 점을 **새 임시 디렉터리**에 쓴다. 디렉터리를 키에 넣었다면
    이분 탐색이 되짚는 버전도, 롤백 직후의 재측정도 절대 적중하지 못한다 -
    캐시가 있으나 마나가 되는 정확한 조건이다."""
    inner = _CountingBackend()
    cache = CachingSimulator(inner)
    text = "* deck\nR1 a b 1k\n"
    d1 = tmp_path / "t1"
    d2 = tmp_path / "t2"
    d1.mkdir()
    d2.mkdir()

    cache.run(_deck(d1, text, "corner.cir"), {"control_block": "op"})
    cache.run(_deck(d2, text, "corner.cir"), {"control_block": "op"})

    assert inner.calls == 1
    assert cache.stats()["hits"] == 1


def test_hits_and_misses_are_logged_unconditionally(tmp_path):
    """캐시가 한 번도 안 맞는 상태와 캐시가 아예 안 붙은 상태가 로그에서 같아
    보이면 안 된다 - 게이트에 적용하던 규칙을 캐시에도 적용한다."""
    events = []
    cache = CachingSimulator(_CountingBackend(), log_event=lambda step, data: events.append((step, data)))
    path = _deck(tmp_path)

    cache.run(path, {"control_block": "op"})
    cache.run(path, {"control_block": "op"})

    assert [step for step, _ in events] == ["sim_cache", "sim_cache"]
    assert events[0][1]["hit"] is False
    assert events[1][1]["hit"] is True
    assert events[1][1]["hits"] == 1


def test_a_disabled_cache_still_says_so_in_the_log(tmp_path):
    events = []
    cache = CachingSimulator(
        _CountingBackend(), log_event=lambda step, data: events.append((step, data)), enabled=False
    )
    path = _deck(tmp_path)

    cache.run(path, {"control_block": "op"})
    cache.run(path, {"control_block": "op"})

    assert cache.stats() == {"hits": 0, "misses": 0, "entries": 0}
    assert all(data["enabled"] is False for _, data in events)


def test_a_timeout_is_not_cached(tmp_path):
    """timeout은 **환경이 낸 결과**라 순수 함수가 아니다. 캐시하면 한 번의
    부하 급증이 그 실행 내내 같은 점을 실패로 못박는다."""

    class _Flaky(SimulatorBackend):
        def __init__(self):
            self.calls = 0

        def run(self, netlist_path, testbench_config):
            self.calls += 1
            if self.calls == 1:
                return RawSimResult(
                    status="error", measurements={}, raw_log="timed out", warnings=[], cacheable=False
                )
            return RawSimResult(status="success", measurements={"g": 1.0}, raw_log="ok", warnings=[])

    inner = _Flaky()
    cache = CachingSimulator(inner)
    path = _deck(tmp_path)

    first = cache.run(path, {"control_block": "op"})
    second = cache.run(path, {"control_block": "op"})

    assert first.status == "error"
    assert second.status == "success"
    assert inner.calls == 2


def test_a_cached_result_is_copied_not_shared(tmp_path):
    """`RawSimResult`는 frozen이 아니고 measurements는 dict다. 같은 객체를 두 번
    내주면 한 소비자의 in-place 수정이 다음 적중의 값을 바꾼다 - 캐시가 값을
    지어내는 또 하나의 경로다."""
    cache = CachingSimulator(_CountingBackend())
    path = _deck(tmp_path)

    first = cache.run(path, {"control_block": "op"})
    first.measurements["gain_db"] = 999.0
    second = cache.run(path, {"control_block": "op"})

    assert second.measurements == {"gain_db": 1.0}


def test_an_unreadable_deck_bypasses_the_cache_rather_than_guessing_a_key(tmp_path):
    inner = _CountingBackend()
    events = []
    cache = CachingSimulator(inner, log_event=lambda step, data: events.append(data))

    cache.run(str(tmp_path / "missing.cir"), {"control_block": "op"})
    cache.run(str(tmp_path / "missing.cir"), {"control_block": "op"})

    assert inner.calls == 2
    assert all(data.get("unkeyable") == "netlist_unreadable" for data in events)


def test_the_cache_is_thread_safe(tmp_path):
    """코너 스윕이 이것을 여러 워커에서 동시에 부른다."""
    inner = _CountingBackend()
    cache = CachingSimulator(inner)
    paths = [_deck(tmp_path, f"* deck {i}\n", f"d{i}.cir") for i in range(12)]
    results = {}

    def work(i):
        for _ in range(5):
            results[i] = cache.run(paths[i], {"control_block": "op"}).measurements

    threads = [threading.Thread(target=work, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert inner.calls == 12
    stats = cache.stats()
    assert stats["entries"] == 12
    assert stats["hits"] + stats["misses"] == 60


def test_attach_log_event_does_not_overwrite_an_existing_sink(tmp_path):
    """`cli.py`가 이미 state.log_event를 붙였다면 corner_sim이 그것을 덮어쓰면
    안 된다 - 실행 하나의 history.jsonl이 진짜 싱크다."""
    first = []
    second = []
    cache = CachingSimulator(_CountingBackend(), log_event=lambda s, d: first.append(d))
    attach_log_event(cache, lambda s, d: second.append(d))
    cache.run(_deck(tmp_path), {"control_block": "op"})

    assert len(first) == 1
    assert second == []


def test_attach_log_event_is_a_noop_on_a_plain_backend():
    inner = _CountingBackend()
    attach_log_event(inner, lambda s, d: None)  # 던지지 않는 것이 계약이다


def test_the_default_identity_names_the_backend_class():
    assert _CountingBackend().ident == "fake-sim-v1"

    class _Plain(SimulatorBackend):
        def run(self, netlist_path, testbench_config):
            raise NotImplementedError

    assert _Plain().identity().endswith("_Plain")


@pytest.mark.parametrize("bad", ["", "  "])
def test_an_empty_worker_env_var_falls_back_to_the_default(monkeypatch, bad):
    from analogcoder.simulators.parallel import ENV_WORKERS, default_workers

    monkeypatch.setenv(ENV_WORKERS, bad)
    assert default_workers() >= 1
