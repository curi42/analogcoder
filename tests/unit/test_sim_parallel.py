"""워커 풀의 계약: 순서 무관 병합, 예외 전파, 워커 수 해석.

병렬화의 위험은 속도가 아니라 **순서**다. 완료 순서로 결과를 붙이면 같은 스윕이
실행마다 다른 코너에 다른 측정값을 붙일 수 있고, 그것은 캐시가 결정 요인을
빠뜨리는 것과 같은 종류의 사고다 - 재본 적 없는 사실이 생긴다.
"""

import random
import threading
import time

import pytest

from analogcoder.simulators.parallel import (
    ENV_WORKERS,
    default_workers,
    map_points,
    resolve_workers,
)


def test_results_are_keyed_by_identity_not_by_completion_order():
    # 일부러 역순으로 끝나게 만든다: 먼저 제출된 것이 가장 오래 걸린다.
    items = [((i,), i) for i in range(8)]

    def fn(i):
        time.sleep((8 - i) * 0.01)
        return i * 10

    results = map_points(fn, items, max_workers=8)
    assert results == {(i,): i * 10 for i in range(8)}


def test_the_sequential_path_and_the_parallel_path_agree():
    items = [((i,), i) for i in range(20)]
    fn = lambda i: i * i  # noqa: E731

    assert map_points(fn, items, max_workers=1) == map_points(fn, items, max_workers=6)


def test_one_worker_never_starts_a_pool():
    """A/B의 대조군은 "워커 1개짜리 풀"이 아니라 풀이 없는 경로여야 한다."""
    caller = threading.current_thread().ident
    seen = []
    map_points(lambda x: seen.append(threading.current_thread().ident), [((i,), i) for i in range(4)], 1)
    assert set(seen) == {caller}


def test_an_exception_is_not_swallowed():
    def fn(i):
        if i == 3:
            raise RuntimeError("boom")
        return i

    with pytest.raises(RuntimeError, match="boom"):
        map_points(fn, [((i,), i) for i in range(6)], max_workers=4)


def test_the_points_actually_run_concurrently():
    """병렬화가 조용히 순차로 접히면 벽시계는 그대로인데 테스트는 통과한다 -
    이 저장소가 게이트에 대해 아홉 번 적어 둔 모양이다. 동시 재실행 수를 직접
    센다."""
    lock = threading.Lock()
    live = {"now": 0, "peak": 0}

    def fn(_):
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        time.sleep(0.05)
        with lock:
            live["now"] -= 1

    map_points(fn, [((i,), i) for i in range(8)], max_workers=4)
    assert live["peak"] > 1


def test_the_worker_count_resolution_order(monkeypatch):
    monkeypatch.setenv(ENV_WORKERS, "3")
    assert default_workers() == 3
    # 명시 인자가 환경 변수를 이긴다.
    assert resolve_workers(5) == 5
    assert resolve_workers(None) == 3
    monkeypatch.delenv(ENV_WORKERS)
    assert default_workers() == max(1, (__import__("os").cpu_count() or 2) - 1)


def test_a_nonsense_worker_env_var_raises_rather_than_silently_defaulting(monkeypatch):
    monkeypatch.setenv(ENV_WORKERS, "eight")
    with pytest.raises(ValueError):
        default_workers()
    monkeypatch.setenv(ENV_WORKERS, "0")
    with pytest.raises(ValueError):
        default_workers()


def test_an_empty_item_list_is_an_empty_dict():
    assert map_points(lambda x: x, [], max_workers=4) == {}


def test_a_shuffled_submission_order_does_not_change_the_mapping():
    items = [((i,), i) for i in range(30)]
    shuffled = list(items)
    random.Random(0).shuffle(shuffled)
    assert map_points(lambda i: i + 1, items, 7) == map_points(lambda i: i + 1, shuffled, 7)
