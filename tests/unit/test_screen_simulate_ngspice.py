"""대안 선별용 시뮬레이션 경로를 **실제 ngspice**로 확인한다.

이 경로는 `simulate_fn`과 두 가지가 다르고, 둘 다 조용히 틀릴 수 있는
종류다: 시뮬레이터 에이전트를 거치지 않고, `state.current_netlist_paths()`가
아니라 **인자로 받은 텍스트**를 잰다. 두 번째가 위험한 이유는 텍스트를
원래 디렉터리 밖의 임시 디렉터리에서 돌리기 때문이다 - 상대 `.include`가
그 순간 조용히 풀리지 않으면 측정이 통째로 사라지고, 이 저장소는 그 함정에
이미 여러 번 걸렸다(`resolve_includes`가 그래서 있다).
"""

import os

from analogcoder.cli import screen_simulate
from analogcoder.netlist import apply_changes, resolve_includes
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec

BENCHMARK_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "benchmarks", "two_stage_opamp"
)


def _texts(spec):
    """`cli._run`이 오케스트레이터에 넘기기 전에 하는 것과 같다 - 최상위
    상대 include를 절대 경로로 바꾼다."""
    base = os.path.abspath(BENCHMARK_DIR)
    return {
        tb.name: resolve_includes(open(tb.netlist_path).read(), base)
        for tb in spec.testbenches
    }


def test_the_screening_path_measures_the_text_it_is_given_not_the_deck_on_disk():
    spec = load_spec(os.path.join(BENCHMARK_DIR, "spec.yaml"))
    texts = _texts(spec)

    baseline = screen_simulate(texts, spec, NgspiceBackend())
    assert baseline["status"] == "success"
    # 네 테스트벤치의 측정이 전부 합쳐져 온다.
    assert set(baseline["by_testbench"]) == {t.name for t in spec.testbenches}
    assert baseline["measurements"]["gain_db"] > 60.0

    # Miller 캡을 키우면 위상여유가 오르고 UGBW가 준다 - 이 덱의 선언된
    # 트레이드오프다. 디스크의 파일은 하나도 안 바뀌었는데 측정이 움직이는
    # 것이 "인자로 받은 텍스트를 잰다"의 증거다.
    changes = [
        {"refdes": "OPAMP2STAGE.Xcc", "param": "w", "old_value": "12.05", "new_value": "20"},
        {"refdes": "OPAMP2STAGE.Xcc", "param": "l", "old_value": "12.05", "new_value": "20"},
    ]
    bumped = {name: apply_changes(text, changes) for name, text in texts.items()}
    after = screen_simulate(bumped, spec, NgspiceBackend())

    assert after["status"] == "success"
    assert after["measurements"]["phase_margin_deg"] > baseline["measurements"]["phase_margin_deg"]
    assert after["measurements"]["ugbw_hz"] < baseline["measurements"]["ugbw_hz"]

    # 디스크는 그대로다.
    assert open(spec.canonical.netlist_path).read().count("w=12.05 l=12.05") == 1


def test_the_screening_path_reports_a_failing_testbench_rather_than_swallowing_it():
    """status를 합치지 않으면 수렴하지 못한 테스트벤치의 측정값으로 후보를
    고르게 된다. 실제로 수렴 실패가 낸 값이 개선으로 수락된 전례가 있다."""
    spec = load_spec(os.path.join(BENCHMARK_DIR, "spec.yaml"))
    texts = _texts(spec)
    # include를 깨뜨린다 - ngspice가 모델을 못 찾아 그 테스트벤치가 실패한다.
    broken = dict(texts)
    name = spec.canonical.name
    broken[name] = texts[name].replace(".include", ".include /nonexistent/")

    result = screen_simulate(broken, spec, NgspiceBackend())
    assert result["status"] != "success"
