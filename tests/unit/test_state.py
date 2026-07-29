import json
import math
import os

import pytest

from analogcoder.state import RunState


def test_push_netlist_version_writes_one_file_per_testbench(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain", "psr_plus"])

    v0_paths = state.push_netlist_version({"ac_loop_gain": "* ac v0\n.end\n", "psr_plus": "* psr v0\n.end\n"})
    v1_paths = state.push_netlist_version({"ac_loop_gain": "* ac v1\n.end\n", "psr_plus": "* psr v1\n.end\n"})

    assert os.path.basename(v0_paths["ac_loop_gain"]) == "netlist_v0_ac_loop_gain.cir"
    assert os.path.basename(v0_paths["psr_plus"]) == "netlist_v0_psr_plus.cir"
    assert os.path.basename(v1_paths["ac_loop_gain"]) == "netlist_v1_ac_loop_gain.cir"
    assert state.current_netlist_paths() == v1_paths
    with open(v1_paths["psr_plus"]) as f:
        assert f.read() == "* psr v1\n.end\n"


def test_current_netlist_texts_reads_back_latest_version(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain", "psr_plus"])
    state.push_netlist_version({"ac_loop_gain": "* ac v0\n.end\n", "psr_plus": "* psr v0\n.end\n"})
    state.push_netlist_version({"ac_loop_gain": "* ac v1\n.end\n", "psr_plus": "* psr v1\n.end\n"})

    texts = state.current_netlist_texts()

    assert texts == {"ac_loop_gain": "* ac v1\n.end\n", "psr_plus": "* psr v1\n.end\n"}


def test_rollback_restores_every_testbench_together(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain", "psr_plus"])
    v0_paths = state.push_netlist_version({"ac_loop_gain": "* ac v0\n.end\n", "psr_plus": "* psr v0\n.end\n"})
    state.push_netlist_version({"ac_loop_gain": "* ac v1\n.end\n", "psr_plus": "* psr v1\n.end\n"})

    restored_paths = state.rollback()

    assert restored_paths == v0_paths
    assert state.current_netlist_paths() == v0_paths


def test_rollback_raises_when_no_previous_version(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])
    state.push_netlist_version({"ac_loop_gain": "* v0\n.end\n"})

    with pytest.raises(ValueError):
        state.rollback()


def test_log_event_appends_jsonl(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])
    state.log_event("judge", {"overall_pass": False})
    state.log_event("judge", {"overall_pass": True})

    with open(state.history_path) as f:
        lines = [json.loads(line) for line in f]

    assert lines[0] == {"step": "judge", "overall_pass": False}
    assert lines[1] == {"step": "judge", "overall_pass": True}


def test_push_netlist_version_is_atomic_on_missing_key(tmp_path):
    """Verify that push_netlist_version raises ValueError and does not mutate state when texts is missing a key."""
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain", "psr_plus"])

    # First, push a version successfully.
    state.push_netlist_version({"ac_loop_gain": "* v0\n.end\n", "psr_plus": "* v0\n.end\n"})
    initial_versions = {"ac_loop_gain": list(state.netlist_versions["ac_loop_gain"]),
                       "psr_plus": list(state.netlist_versions["psr_plus"])}

    # Now attempt to push with a missing key.
    with pytest.raises(ValueError, match="texts keys.*do not match testbench_names"):
        state.push_netlist_version({"ac_loop_gain": "* v1\n.end\n"})

    # Verify state is unchanged: no partial mutation occurred.
    assert state.netlist_versions == initial_versions


# --- 감사 2.3: history.jsonl도 RFC 8259 JSON이 아니다 -------------------------
#
# `log_event`는 `json.dumps`를 `allow_nan` 없이 부르므로 bare `NaN`/`-Infinity`를
# 쓴다. 정상 경로에서 나온다: `judge_tools.evaluate_criteria`는 측정이 없는
# 기준에 `math.nan`을, `pvt.corner_severity`는 `-math.inf`를 낸다. 위험은 파싱
# 거부만이 아니다 - jq 1.7.1은 거부하지 않고 `-Infinity`를 `-1.797e308`로
# **조용히 바꿔 준다**. 어느 시뮬레이션에서도 관측된 적 없는 숫자다.


def _reject_non_rfc(token):
    raise AssertionError(f"non-RFC 8259 constant in history.jsonl: {token}")


def test_log_event_writes_valid_rfc_8259_json_for_a_nan_measurement(tmp_path):
    """**어떤 변형을 잡는가**: `log_event`에서 정규화나 `allow_nan=False` 중
    하나를 빼는 변형."""
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])
    state.log_event(
        "judge",
        {
            "outer_iter": 3,
            "overall_pass": False,
            "criteria": [
                {"name": "ugbw", "target": ">=1500000.0", "actual": math.nan, "pass": False, "margin": math.nan}
            ],
        },
    )
    state.log_event("pvt_final_sweep", {"per_corner": [{"severity": -math.inf}, {"severity": 0.5}]})

    text = open(state.history_path).read()
    for line in text.splitlines():
        json.loads(line, parse_constant=_reject_non_rfc)
    assert ": NaN" not in text and ": -Infinity" not in text and ": Infinity" not in text


def test_history_read_events_gives_back_the_float_it_was_handed(tmp_path):
    """산출물의 문자열 표지는 **전송 형식**이다. `history.read_events`는
    `history.jsonl`을 읽는 유일한 곳이고(모듈 독스트링), 그 소비자
    (`scripts/paired_tuner_probe.py`)는 `deltas_between`으로 판정값을 **뺀다**.
    표지를 그대로 넘기면 그 뺄셈이 TypeError가 되고, 지금 돌고 있는 D1
    재측정이 깨진다.

    **어떤 변형을 잡는가**: `_numbered`에서 복원을 빼는 변형.
    """
    from analogcoder.history import read_events

    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])
    state.log_event("judge", {"criteria": [{"name": "ugbw", "actual": math.nan}]})
    state.log_event("probe", {"severity": -math.inf, "label": "ss/1.62/125.0"})

    events = read_events(state.history_path)

    assert math.isnan(events[0]["criteria"][0]["actual"])
    assert events[1]["severity"] == -math.inf
    # 진짜 문자열은 건드리지 않는다.
    assert events[1]["label"] == "ss/1.62/125.0"


def test_a_none_in_an_event_stays_null_and_is_not_confused_with_a_nan(tmp_path):
    """`null`("그 필드에 값이 없다")과 NaN("쟀는데 값이 안 나왔다")은 다른
    사실이다. 산출물에서 둘이 같은 토큰이 되면 이 저장소가 반복해 온 실수를
    형식에서 다시 하는 것이다."""
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])
    state.log_event("corner_worst", {"measured": math.nan, "absent": None})

    raw = json.loads(open(state.history_path).read().strip(), parse_constant=_reject_non_rfc)
    assert raw["absent"] is None
    assert raw["measured"] == "NaN"
