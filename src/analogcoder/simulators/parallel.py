"""코너 스윕의 워커 풀.

**코너 × 테스트벤치는 서로 독립이다.** 한 점의 시뮬레이션은 다른 점의 결과를
읽지 않고, 렌더링은 인자에서 텍스트를 만들 뿐이며, 각 호출은 이미 자기 임시
디렉터리를 판다(`pvt.run_full_pvt_sweep`, `corner_sim._run_point`). 그래서
병렬화는 순서를 바꾸는 것 말고는 아무것도 바꾸지 않는다.

## 왜 스레드인가

ngspice는 **별도 프로세스**다. `subprocess.run`이 기다리는 동안 GIL은 풀려
있으므로 스레드로 충분하고, 넘길 것이 없으니 pickle 문제도 없다. 멀티프로세싱은
시뮬레이터 백엔드(캐시를 들고 있다)를 프로세스 경계 너머로 옮겨야 하는데, 그
순간 캐시가 프로세스마다 갈라져 태스크 3의 다른 절반이 무의미해진다.

`CLAUDE.md`가 경고하는 동시 실행 파손은 **claude-agent-sdk 쪽**(LLM 경로)이고
ngspice와 무관하다. 여기서 병렬로 도는 것은 SPICE뿐이고, LLM 호출은 전부
호출부의 순차 경로에 남는다.

## 결과 병합은 순서 무관이어야 한다

`map_points`는 **키로 색인된 dict**를 돌려준다. 완료 순서로 리스트에 붙이면
같은 스윕이 실행마다 다른 코너에 다른 측정값을 붙일 수 있고, 그것은 캐시가
결정 요인을 빠뜨리는 것과 같은 종류의 사고다 - 재본 적 없는 사실이 생긴다.
호출부는 이 dict를 자기 순서(코너 선언 순서)로 다시 읽는다.

## 워커 수

기본은 `cpu_count - 1`이다 - 코어 하나는 비워 둔다. 환경 변수
`ANALOGCODER_SIM_WORKERS`로 덮어쓸 수 있고, **1이면 풀을 아예 만들지 않고
호출한 스레드에서 순차로 돈다.** A/B 하니스가 순차를 강제할 때 "워커 1개짜리
풀"이 아니라 진짜 순차 경로를 타게 하기 위한 것이다 - 병렬화가 값을 바꾸지
않는다는 주장의 대조군은 풀이 없는 쪽이어야 한다.
"""

import os
from concurrent.futures import ThreadPoolExecutor

ENV_WORKERS = "ANALOGCODER_SIM_WORKERS"


def default_workers() -> int:
    """설정된 워커 수. 환경 변수가 있으면 그것, 없으면 `cpu_count - 1`.

    환경 변수가 숫자로 안 읽히면 **조용히 기본값으로 돌아가지 않고** ValueError를
    낸다. 오타 하나로 병렬화가 통째로 꺼진 채 "왜 안 빨라지지"를 재는 것이,
    이 저장소가 조용한 게이트에 대해 아홉 번 적어 둔 바로 그 모양이다."""
    raw = os.environ.get(ENV_WORKERS)
    if raw is not None and raw.strip():
        value = int(raw)
        if value < 1:
            raise ValueError(f"{ENV_WORKERS} must be >= 1, got {value!r}")
        return value
    return max(1, (os.cpu_count() or 2) - 1)


def resolve_workers(max_workers: int | None) -> int:
    """명시 인자 > 환경 변수 > 기본값."""
    if max_workers is None:
        return default_workers()
    if max_workers < 1:
        raise ValueError(f"max_workers must be >= 1, got {max_workers!r}")
    return max_workers


def map_points(fn, items: list, max_workers: int | None = None) -> dict:
    """`items`의 각 `(key, payload)`에 대해 `fn(payload)`를 돌리고
    `{key: 결과}`를 돌려준다.

    예외는 삼키지 않는다 - 호출부(판정 경로)가 그것을 어떻게 다룰지 이미
    정해 두고 있고, 여기서 접으면 그 결정이 사라진다."""
    workers = resolve_workers(max_workers)
    if workers == 1 or len(items) <= 1:
        return {key: fn(payload) for key, payload in items}

    results: dict = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        futures = {pool.submit(fn, payload): key for key, payload in items}
        for future, key in futures.items():
            results[key] = future.result()
    return results
