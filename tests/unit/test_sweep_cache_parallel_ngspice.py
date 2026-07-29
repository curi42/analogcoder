"""사전 등록된 판정 규칙을 **진짜 ngspice**로 잰다.

로드맵 단계 0 태스크 3의 규칙은 돌리기 전에 고정됐다:

- **채택:** 캐시를 끈 실행과 켠 실행의 측정값이 동일하고, 병렬 스윕과 순차
  스윕의 결과가 동일하며, 벽시계가 줄어들 것.
- **불채택:** 어느 하나라도 값이 달라질 것. 속도는 정확성과 교환하지 않는다.

그래서 이 파일은 **결과 dict 전체**를 비교한다 - 키 집합도, 몇 개 골라낸 값도
아니다. 이 저장소는 부분 비교에 물린 적이 있다: 키 집합만 같으면 통과하는 단언은
서로 다른 두 회로의 측정값을 같다고 말한다.

`ngspice`를 PATH에서 가정한다(이 저장소의 관례, 스킵 게이트 없음).
비용: 스윕 한 번이 4코너 × 4테스트벤치 = 16 시뮬레이션이고, 전체 ~21초다.
"""

import os

import pytest

from analogcoder.corner_selection import NOMINAL, CornerSet
from analogcoder.corner_sim import CornerState, build_corner_simulate
from analogcoder.netlist import resolve_includes
from analogcoder.pvt import CornerPoint, run_full_pvt_sweep
from analogcoder.simulators.cache import CachingSimulator
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import PVTCorners, load_spec
from analogcoder.state import RunState

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OPAMP_SPEC = os.path.join(REPO, "benchmarks", "two_stage_opamp", "spec_pvt.yaml")
BANDGAP_SPEC = os.path.join(REPO, "benchmarks", "bandgap", "spec_pvt.yaml")

FS = CornerPoint(process="fs", voltage=1.98, temperature=125.0)
SS = CornerPoint(process="ss", voltage=1.62, temperature=-40.0)


def _opamp_spec():
    spec = load_spec(OPAMP_SPEC)
    # **격자를 공정 × 전압으로 잡는 것은 의도적이다.** 공정만 흔들면 코너마다
    # include **경로**가 달라지고, 그 경로는 캐시 키의 include 지문에도 들어간다 -
    # 그러면 덱 텍스트를 키에서 통째로 빼는 뮤테이션을 이 스윕이 못 잡는다
    # (실제로 재봤다: 지문만으로 통과했다). tt/1.62와 tt/1.98은 include가 같고
    # **덱 텍스트의 Vdd 줄만** 다르므로, 그 구멍을 스윕 수준에서 닫는다.
    # 비용은 테스트벤치 4개 × 4코너 = 16 시뮬레이션으로 몇 초다.
    spec.pvt_corners = PVTCorners(process=["tt", "ss"], voltage=[1.62, 1.98], temperature=[27])
    return spec


def _texts(spec):
    texts = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            texts[tb.name] = resolve_includes(f.read(), os.path.dirname(tb.netlist_path))
    return texts


@pytest.fixture(scope="module")
def uncached_sequential():
    """대조군: 캐시 없음, 워커 1개. 이 브랜치 이전의 코드 경로 그대로다."""
    spec = _opamp_spec()
    return run_full_pvt_sweep(_texts(spec), spec, NgspiceBackend(), max_workers=1)


def test_a_cached_sweep_measures_exactly_what_an_uncached_sweep_measured(uncached_sequential):
    """판정 규칙 1. 캐시를 켠 실행과 끈 실행의 **결과 dict 전체**가 같아야 한다.

    스윕을 두 번 돌린다: 첫 번째는 전부 미적중(캐시가 채워진다), 두 번째는
    전부 적중(시뮬레이션이 한 번도 안 돈다). 셋이 전부 같아야 한다 - 그래야
    "캐시가 돌려준 값은 재본 값"이라는 주장이 선다."""
    spec = _opamp_spec()
    cache = CachingSimulator(NgspiceBackend())
    texts = _texts(spec)

    filling = run_full_pvt_sweep(texts, spec, cache, max_workers=1)
    stats_after_fill = cache.stats()
    from_cache = run_full_pvt_sweep(texts, spec, cache, max_workers=1)

    assert filling == uncached_sequential
    assert from_cache == uncached_sequential

    # 캐시가 실제로 붙었는지를 값이 아니라 **회계**로 확인한다. 이것이 없으면
    # 캐시를 통째로 지워도 이 테스트는 통과한다 - 이 저장소가 아홉 번 당한
    # "조용히 아무것도 안 하는 장치"의 정확한 모양이다.
    assert stats_after_fill == {"hits": 0, "misses": 16, "entries": 16}
    assert cache.stats() == {"hits": 16, "misses": 16, "entries": 16}


def test_a_parallel_sweep_measures_exactly_what_a_sequential_sweep_measured(uncached_sequential):
    """판정 규칙 2. 병렬 스윕과 순차 스윕의 결과가 같아야 한다.

    완료 순서로 결과를 붙이는 구현이라면 여기서 `per_corner`의 측정값이 코너
    사이에서 섞이거나, `worst_case_corners`가 다른 코너를 지목한다."""
    spec = _opamp_spec()
    parallel = run_full_pvt_sweep(_texts(spec), spec, NgspiceBackend(), max_workers=8)

    assert parallel == uncached_sequential


def test_the_cache_and_the_pool_together_still_measure_the_same_thing(uncached_sequential):
    """둘을 함께 켰을 때가 실제 실행 구성이다. 캐시가 스레드 경계에서 값을
    섞으면 여기서 걸린다."""
    spec = _opamp_spec()
    cache = CachingSimulator(NgspiceBackend())
    texts = _texts(spec)

    first = run_full_pvt_sweep(texts, spec, cache, max_workers=8)
    second = run_full_pvt_sweep(texts, spec, cache, max_workers=8)

    assert first == uncached_sequential
    assert second == uncached_sequential
    assert cache.stats()["hits"] == 16


def test_a_dropped_determinant_would_be_caught_here():
    """**뮤테이션 가드.** 코너를 키에서 빼면 45개 코너가 전부 첫 코너의 값이
    된다 - 캐시가 '없는 사실'을 만드는 정확한 모양이다.

    여기서는 그 상황을 직접 재현한다: 코너 정체성을 무시하는(덱 텍스트를 키에서
    뺀) 캐시를 끼우고, 스윕 결과가 대조군과 **달라지는지** 본다. 달라지지
    않는다면 위의 두 테스트는 결정 요인 누락을 잡지 못한다는 뜻이므로, 그
    사실을 여기서 먼저 안다."""
    spec = _opamp_spec()

    class _CornerBlindCache(CachingSimulator):
        def run(self, netlist_path, testbench_config):
            # 덱 텍스트(= 코너가 렌더링돼 들어가는 자리)를 키에서 뺀다.
            key = testbench_config["control_block"]
            with self._lock:
                cached = self._entries.get(key)
            if cached is not None:
                return cached
            result = self.inner.run(netlist_path, testbench_config)
            with self._lock:
                self._entries[key] = result
            return result

    blind = run_full_pvt_sweep(_texts(spec), spec, _CornerBlindCache(NgspiceBackend()), max_workers=1)
    good = run_full_pvt_sweep(_texts(spec), spec, CachingSimulator(NgspiceBackend()), max_workers=1)

    # 코너를 잃은 캐시는 네 코너를 하나로 접는다: 모든 코너의 측정값이 같아진다.
    blind_sets = [entry["measurements"] for entry in blind["per_corner"]]
    assert blind_sets[0] == blind_sets[1] == blind_sets[2] == blind_sets[3]
    # 온전한 캐시는 그러지 않는다 - 코너마다 다른 회로다.
    good_sets = [entry["measurements"] for entry in good["per_corner"]]
    assert good_sets[0] != good_sets[1]
    assert blind != good


class _CountingNgspice(NgspiceBackend):
    """실제로 ngspice 프로세스를 몇 번 띄웠는지 센다.

    **이 카운트가 벽시계보다 강한 증거다.** 벽시계는 머신 부하에 흔들리지만
    "몇 번 안 돌렸는가"는 결정론적이고, 캐시의 효과를 정확히 그 값으로
    설명한다. 병렬화 쪽에서는 같은 카운트가 "병렬이 일을 더 하거나 덜 하지
    않는다"를 말한다."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.invocations = 0
        self._count_lock = __import__("threading").Lock()

    def run(self, netlist_path, testbench_config):
        with self._count_lock:
            self.invocations += 1
        return super().run(netlist_path, testbench_config)


def test_the_cache_avoids_a_deterministic_number_of_ngspice_invocations():
    """부하와 무관한 대리 지표. 스윕 한 번은 4코너 × 4테스트벤치 = 16회다.
    캐시가 채워진 뒤의 두 번째 스윕은 **0회**여야 한다 - 16회 회피."""
    spec = _opamp_spec()
    inner = _CountingNgspice()
    cache = CachingSimulator(inner)
    texts = _texts(spec)

    run_full_pvt_sweep(texts, spec, cache, max_workers=1)
    after_fill = inner.invocations
    run_full_pvt_sweep(texts, spec, cache, max_workers=1)
    after_reuse = inner.invocations

    assert after_fill == 16
    assert after_reuse == 16, "두 번째 스윕은 ngspice를 한 번도 띄우지 않아야 한다"
    assert cache.stats() == {"hits": 16, "misses": 16, "entries": 16}


def test_the_parallel_sweep_runs_exactly_as_many_simulations_as_the_sequential_one():
    """병렬화가 일을 더 하거나(중복 제출) 덜 하는(점을 빠뜨리는) 변형을
    카운트로 잡는다. 벽시계와 달리 이 값은 머신 부하에 흔들리지 않는다."""
    spec = _opamp_spec()
    sequential = _CountingNgspice()
    parallel = _CountingNgspice()

    run_full_pvt_sweep(_texts(spec), spec, sequential, max_workers=1)
    run_full_pvt_sweep(_texts(spec), spec, parallel, max_workers=8)

    assert sequential.invocations == parallel.invocations == 16


@pytest.mark.asyncio
async def test_the_corner_aware_simulate_agrees_between_sequential_and_parallel(tmp_path):
    """축소 코너 경로도 같은 규칙을 받는다. corner_sim은 테스트벤치 **안쪽**을
    병렬화하고 탐침을 같은 배치에 넣으므로, 순차와 값이 갈라질 자리가 pvt보다
    하나 더 많다."""
    async def _run(workers, run_dir):
        spec = load_spec(BANDGAP_SPEC)
        spec.testbenches = spec.testbenches[:1]
        tb = spec.canonical
        with open(tb.netlist_path) as f:
            text = resolve_includes(f.read(), os.path.dirname(tb.netlist_path))
        state = RunState(run_dir=run_dir, testbench_names=[tb.name])
        state.push_netlist_version({tb.name: text})

        async def agent(netlist_path, control_block):
            return {"status": "success", "measurements": {}, "warnings": [],
                    "control_block": control_block}

        corner_state = CornerState(CornerSet(corners=(NOMINAL, FS), probe_order=(SS,)))
        events: list = []
        simulate = build_corner_simulate(
            agent, NgspiceBackend(), state, corner_state,
            lambda step, data: events.append((step, data)), max_workers=workers,
        )
        return await simulate(state.current_netlist_texts(), spec), events, corner_state

    sequential, seq_events, seq_state = await _run(1, str(tmp_path / "seq"))
    parallel, par_events, par_state = await _run(8, str(tmp_path / "par"))

    assert parallel["measurements"] == sequential["measurements"]
    assert parallel["corner_worst"] == sequential["corner_worst"]
    assert parallel["status"] == sequential["status"]
    # 탐침도 같은 배치에서 돌았으므로 판정과 승격까지 같아야 한다.
    assert parallel["probe"] == sequential["probe"]
    assert par_state.corner_set == seq_state.corner_set
    assert [step for step, _ in par_events] == [step for step, _ in seq_events]
