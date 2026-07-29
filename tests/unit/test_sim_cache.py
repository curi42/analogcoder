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


# ------------------------------------------------------- `.lib` 과 지문 깊이

# 코너 지정 파일 **형식**의 합성 재현. 이름은 전부 합성이고 그 이유 둘
# (독점 PDK 유래 문자열은 저장소에 넣지 않는다 / 축 정체성을 이름에서 읽지
# 않는다)은 `test_netlist_dialect.py` 의 같은 픽스처 주석에 있다.
_CORNER_INC_BODY = (
    "*Axis A\n"
    ".lib '{lib_dir}/LIB_A.LIB' SEC_A1\n"
    "\n"
    "*Axis B\n"
    ".lib '{lib_dir}/LIB_B.LIB' SEC_B1\n"
    "\n"
    "*Axis C\n"
    ".lib '{lib_dir}/LIB_C.LIB' SEC_C1\n"
)


def _corner_libs(tmp_path):
    lib_dir = tmp_path / "corner_libs"
    lib_dir.mkdir()
    for name in ("LIB_A.LIB", "LIB_B.LIB", "LIB_C.LIB"):
        (lib_dir / name).write_text(f"* {name}\n.lib TT\n.model nch nmos\n.endl TT\n")
    return lib_dir


def test_a_lib_call_lands_in_the_cache_fingerprint(tmp_path):
    """`.lib` 이 지문에서 빠지면 PDK 가 **캐시 키에서 통째로 사라진다** -
    지문 `[]` 과 "이 덱엔 include 가 하나도 없다" 가 글자 그대로 같아진다.
    이 모듈의 docstring 이 "결정 요인이 빠진 캐시는 조용한 게이트보다 나쁘다"
    고 적은 자리가 정확히 이것이다."""
    from analogcoder.simulators.cache import include_fingerprints

    lib_dir = _corner_libs(tmp_path)
    text = _CORNER_INC_BODY.format(lib_dir=lib_dir)

    fingerprints = include_fingerprints(text, str(tmp_path))

    paths = [fp[0] for fp in fingerprints]
    assert paths == sorted(str(lib_dir / n) for n in ("LIB_A.LIB", "LIB_C.LIB", "LIB_B.LIB"))
    assert all(fp[1] is not None and fp[2] is not None for fp in fingerprints)


def test_changing_a_lib_target_changes_the_cache_key(tmp_path):
    from analogcoder.simulators.cache import simulation_key

    lib_dir = _corner_libs(tmp_path)
    text = _CORNER_INC_BODY.format(lib_dir=lib_dir)
    config = {"control_block": "op"}

    before = simulation_key(text, str(tmp_path), config, "sim-v1")
    (lib_dir / "LIB_A.LIB").write_text("* LIB_A.LIB - 다른 내용\n.lib TT\n.endl TT\n")
    after = simulation_key(text, str(tmp_path), config, "sim-v1")

    assert before != after


def test_a_lib_definition_does_not_land_in_the_fingerprint(tmp_path):
    from analogcoder.simulators.cache import include_fingerprints

    text = "* t\n.lib SEC_A1\n.model nch nmos level=54\n.endl SEC_A1\n.end\n"

    assert include_fingerprints(text, str(tmp_path)) == []


def test_the_corner_chain_is_three_deep_and_the_fingerprint_stops_at_the_deck(tmp_path):
    """**이 계층이 어디까지 보는지를 숫자로 못박는 테스트다.**

    확인된 구조는 **최소 3단**이다:

        덱 (깊이 0)
          -> 코너 지정 파일        (깊이 1)  - `.lib` 호출 세 줄, 축마다 하나
             -> 축 라이브러리 섹션  (깊이 2)  - 그 안에 다시 약 10줄의 참조
                -> 모델/스큐 파일   (깊이 3)

    그래서 **코너 하나가 닿는 파일이 10개를 넘는데 지문에는 1개만 들어간다.**
    코너 지정 파일이 바뀌면 미적중이지만, 축 라이브러리나 모델 파일만 바뀌면
    **적중한다.**

    **깊이를 1에서 2로 늘려도 이 구멍은 안 닫힌다** - 3단의 약 10개 파일이
    여전히 빠지고, 그러면 "지문이 PDK를 본다"는 인상만 생긴다. 그것이 이
    저장소가 아홉 번 값을 치른 모양이다. 나머지 근거 셋:
    (1) 깊이 2 의 대상을 찾으려면 깊이 1 의 파일을 **읽어야** 하는데 최상위
    include 가 수십 MB 모델 파일을 직접 가리키는 덱이 있고 `simulation_key`
    는 `run()` 마다 돈다, (2) 어느 깊이를 고르든 그 수는 체인이 거기서 끝난다는
    미확인 가정이다, (3) 벤치마크 20개 덱의 캐시 키가 바뀐다.

    보이지 않는다는 사실 자체는 조용하지 않다 - `sim_cache` 이벤트가
    `INCLUDE_FINGERPRINT_DEPTH` 를 매번 싣는다(아래 테스트).

    **중첩을 따라가게 만드는 사람은 이 테스트를 갱신해야 한다.**"""
    from analogcoder.netlist import resolve_includes
    from analogcoder.simulators.cache import include_fingerprints, include_summary

    # 깊이 3: 모델/스큐 파일들
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    models = [model_dir / f"MODEL_{i}.inc" for i in range(10)]
    for m in models:
        m.write_text("* model\n")

    # 깊이 2: 축 라이브러리. 섹션 하나가 다시 10줄을 담고, 그 경로는 이미
    # 절대 경로다(확인된 사실). 공급 축은 소스 줄이 아니라 `.param` 여러
    # 개를, 온도 축은 `.param` + `.temp <이름>` 을 쓴다 - 우리 계층은 그
    # 내용을 보지 않으므로 인식에는 영향이 없다.
    lib_dir = tmp_path / "corner_libs"
    lib_dir.mkdir()
    (lib_dir / "LIB_A.LIB").write_text(
        ".lib SEC_A1\n" + "".join(f".inc '{m}'\n" for m in models) + ".endl SEC_A1\n"
    )
    (lib_dir / "LIB_B.LIB").write_text(
        ".lib SEC_B1\n.param vd1=1.8\n.param vd2=1.2\n.param vd3=3.3\n.endl SEC_B1\n"
    )
    (lib_dir / "LIB_C.LIB").write_text(".lib SEC_C1\n.param tnom_c=25\n.temp tnom_c\n.endl SEC_C1\n")

    # 깊이 1: 코너 지정 파일
    corner_dir = tmp_path / "corners"
    corner_dir.mkdir()
    corner_inc = corner_dir / "corner_case_1.inc"
    corner_inc.write_text(_CORNER_INC_BODY.format(lib_dir=lib_dir))

    deck = "* deck\n.include '{}'\nR1 a b 1k\n.end\n".format(corner_inc)

    fingerprints = include_fingerprints(deck, str(tmp_path))

    # 깊이 1 만: 코너 지정 파일 하나.
    assert [fp[0] for fp in fingerprints] == [str(corner_inc)]
    assert include_summary(deck, str(tmp_path))["depth"] == 1
    # 깊이 2 도 깊이 3 도 지문에 없다.
    assert not any("LIB_" in fp[0] or "MODEL_" in fp[0] for fp in fingerprints)
    # 덱 텍스트에도 안 나타난다 - 중간 파일을 열지조차 않는다.
    assert "LIB_A" not in resolve_includes(deck, str(tmp_path))

    # 정규식 자체는 각 단계를 전부 본다. 막힌 것은 **순회**이지 인식이 아니다.
    assert len(include_fingerprints(corner_inc.read_text(), str(corner_dir))) == 3
    assert len(include_fingerprints((lib_dir / "LIB_A.LIB").read_text(), str(lib_dir))) == 10

    # 이 코너 하나가 닿는 파일을 손으로 세어 지문이 덮는 비율을 못박는다.
    reachable = {str(corner_inc)}
    reachable |= {fp[0] for fp in include_fingerprints(corner_inc.read_text(), str(corner_dir))}
    reachable |= {
        fp[0] for fp in include_fingerprints((lib_dir / "LIB_A.LIB").read_text(), str(lib_dir))
    }
    assert len(reachable) == 14  # 코너 1 + 축 3 + 모델 10
    assert len(fingerprints) == 1  # 그중 지문에 든 것
    # 깊이를 2로 늘리면 4개가 되고 10개가 여전히 빠진다 - 구멍이 닫히지 않는다.


def test_a_relative_corner_include_is_anchored_and_a_missing_one_is_counted(tmp_path):
    """상대 경로가 실제로 들어왔을 때 **조용히 통과하지 않는다.**

    사용자는 코너 경로를 절대 경로로 고쳐 넣을 **수도** 있다고 했지, 상대
    경로가 영원히 안 온다고 보장하지 않았다. 상대 경로는 덱이 놓인
    디렉터리에 앵커되고(ngspice 가 쓰는 것과 같은 규칙), 그 자리에 파일이
    없으면 `None` 지문 + `unresolved` 계량으로 남는다."""
    from analogcoder.simulators.cache import include_fingerprints, include_summary

    lib_dir = _corner_libs(tmp_path)
    deck_dir = tmp_path / "tb"
    deck_dir.mkdir()
    text = "* t\n.lib '../corner_libs/LIB_A.LIB' SEC_A1\n.lib 'nowhere/X.LIB' TT\n.end\n"

    fingerprints = include_fingerprints(text, str(deck_dir))
    summary = include_summary(text, str(deck_dir))

    resolved = [fp for fp in fingerprints if fp[1] is not None]
    assert [fp[0] for fp in resolved] == [str(lib_dir / "LIB_A.LIB")]
    assert summary["relative"] == 2
    assert summary["unresolved"] == 1
    assert summary["lib"] == 2


def test_the_sim_cache_event_says_how_deep_the_fingerprint_looks(tmp_path):
    """계량은 **매번** 나간다. 조건부로 내면 "include 가 없다" 와 "계량이
    사라졌다" 가 로그에서 같아진다 - `optimize_guard_infeasible` 과
    `attempt_log` 가 이미 정한 규칙이다."""
    from analogcoder.simulators.cache import INCLUDE_FINGERPRINT_DEPTH

    events = []
    cache = CachingSimulator(_CountingBackend(), log_event=lambda step, data: events.append(data))

    cache.run(_deck(tmp_path), {"control_block": "op"})
    cache.run(_deck(tmp_path), {"control_block": "op"})

    assert len(events) == 2  # 미적중 하나, 적중 하나
    for data in events:
        assert data["includes"] == {
            "depth": INCLUDE_FINGERPRINT_DEPTH,
            "include": 0,
            "lib": 0,
            "relative": 0,
            "unresolved": 0,
        }


def test_a_cache_hit_carries_the_failure_kind_back(tmp_path):
    """복사본을 돌려주는 자리에서 필드 하나가 빠지면 그것은 캐시가 사실을
    **잃는** 경로다 - `measurements` 를 복사하는 것과 같은 이유로 고정한다."""

    class _Failing(SimulatorBackend):
        def __init__(self):
            self.calls = 0

        def identity(self):
            return "fail-sim"

        def run(self, netlist_path, testbench_config):
            self.calls += 1
            return RawSimResult(
                status="convergence_failure",
                measurements={},
                raw_log="no convergence",
                failure_kind="convergence",
            )

    inner = _Failing()
    cache = CachingSimulator(inner)
    path = _deck(tmp_path)

    first = cache.run(path, {"control_block": "op"})
    second = cache.run(path, {"control_block": "op"})

    assert inner.calls == 1  # convergence 는 캐시 가능하다
    assert first.failure_kind == second.failure_kind == "convergence"


def test_an_environmental_failure_is_not_stored_and_the_event_says_why(tmp_path):
    class _Env(SimulatorBackend):
        def __init__(self):
            self.calls = 0

        def identity(self):
            return "env-sim"

        def run(self, netlist_path, testbench_config):
            self.calls += 1
            return RawSimResult(
                status="error",
                measurements={},
                raw_log="timed out",
                cacheable=False,
                failure_kind="timeout",
            )

    inner = _Env()
    events = []
    cache = CachingSimulator(inner, log_event=lambda step, data: events.append(data))
    path = _deck(tmp_path)

    cache.run(path, {"control_block": "op"})
    cache.run(path, {"control_block": "op"})

    assert inner.calls == 2
    assert [data["stored"] for data in events] == [False, False]
    assert [data["failure_kind"] for data in events] == ["timeout", "timeout"]
