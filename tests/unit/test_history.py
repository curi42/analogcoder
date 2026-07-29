import json

from analogcoder.history import (
    count_events,
    discard_summary,
    discarded_ranges,
    line_count,
    read_events,
)


def write_log(path, events):
    with open(path, "w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")
    return str(path)


def test_a_log_with_no_resume_event_keeps_every_line(tmp_path):
    path = write_log(
        tmp_path / "history.jsonl",
        [{"step": "simulation"}, {"step": "judge"}, {"step": "tuning_proposal"}],
    )

    assert [e["step"] for e in read_events(path)] == ["simulation", "judge", "tuning_proposal"]


def test_line_count_counts_physical_lines(tmp_path):
    path = write_log(tmp_path / "history.jsonl", [{"step": "a"}, {"step": "b"}])

    assert line_count(path) == 2


def test_line_count_of_a_missing_file_is_zero(tmp_path):
    assert line_count(str(tmp_path / "nope.jsonl")) == 0


def test_a_resume_event_drops_exactly_the_abandoned_range(tmp_path):
    # 0,1 = 살아남은 이터레이션. 2,3 = 크래시한 이터레이션의 부분 이벤트.
    # 4 = resume 이벤트 자신. 5 = 재개 후 다시 돈 같은 이터레이션.
    path = write_log(
        tmp_path / "history.jsonl",
        [
            {"step": "simulation", "tag": "keep-0"},
            {"step": "judge", "tag": "keep-1"},
            {"step": "tuning_proposal", "tag": "abandoned-2"},
            {"step": "area_check", "tag": "abandoned-3"},
            {"step": "resume", "discarded_lines": [2, 4]},
            {"step": "tuning_proposal", "tag": "keep-5"},
        ],
    )

    tags = [e.get("tag") for e in read_events(path)]

    assert tags == ["keep-0", "keep-1", None, "keep-5"]


def test_the_resume_event_itself_survives_its_own_range(tmp_path):
    path = write_log(
        tmp_path / "history.jsonl",
        [
            {"step": "tuning_proposal", "tag": "abandoned"},
            {"step": "resume", "discarded_lines": [0, 1]},
        ],
    )

    assert [e["step"] for e in read_events(path)] == ["resume"]


def test_a_log_resumed_twice_drops_both_ranges(tmp_path):
    path = write_log(
        tmp_path / "history.jsonl",
        [
            {"step": "judge", "tag": "keep-0"},
            {"step": "tuning_proposal", "tag": "abandoned-1"},
            {"step": "resume", "discarded_lines": [1, 2]},
            {"step": "judge", "tag": "keep-3"},
            {"step": "tuning_proposal", "tag": "abandoned-4"},
            {"step": "area_check", "tag": "abandoned-5"},
            {"step": "resume", "discarded_lines": [4, 6]},
            {"step": "judge", "tag": "keep-7"},
        ],
    )

    tags = [e.get("tag") for e in read_events(path)]

    assert tags == ["keep-0", None, "keep-3", None, "keep-7"]


def test_a_second_resume_swallowing_the_first_still_drops_everything_once(tmp_path):
    """재개 직후 새 체크포인트를 쓰기 전에 또 죽으면, 다음 재개가 들고 있는
    체크포인트는 **이전 것**이라 버릴 범위가 앞선 resume 이벤트까지 덮는다.
    그 이벤트가 떨어져 나가도 자기가 선언했던 범위는 새 범위 안에 들어 있다."""
    path = write_log(
        tmp_path / "history.jsonl",
        [
            {"step": "judge", "tag": "keep-0"},
            {"step": "tuning_proposal", "tag": "abandoned-1"},
            {"step": "resume", "discarded_lines": [1, 2]},
            {"step": "tuning_proposal", "tag": "abandoned-3"},
            {"step": "resume", "discarded_lines": [1, 4]},
            {"step": "judge", "tag": "keep-5"},
        ],
    )

    tags = [e.get("tag") for e in read_events(path)]

    assert tags == ["keep-0", None, "keep-5"]


def test_reading_without_dropping_returns_the_whole_log(tmp_path):
    path = write_log(
        tmp_path / "history.jsonl",
        [
            {"step": "tuning_proposal", "tag": "abandoned"},
            {"step": "resume", "discarded_lines": [0, 1]},
        ],
    )

    assert len(read_events(path, drop_discarded=False)) == 2


def test_blank_lines_are_skipped_without_shifting_the_indices(tmp_path):
    path = tmp_path / "history.jsonl"
    with open(path, "w") as f:
        f.write(json.dumps({"step": "judge", "tag": "keep-0"}) + "\n")
        f.write("\n")
        f.write(json.dumps({"step": "tuning_proposal", "tag": "abandoned-2"}) + "\n")
        f.write(json.dumps({"step": "resume", "discarded_lines": [2, 3]}) + "\n")

    tags = [e.get("tag") for e in read_events(str(path))]

    assert tags == ["keep-0", None]


def test_discarded_ranges_ignores_a_resume_event_with_no_range(tmp_path):
    assert discarded_ranges([{"step": "resume"}]) == []


def test_discard_summary_says_zero_when_nothing_was_discarded(tmp_path):
    """이 저장소의 규칙: 아무것도 하지 않은 검사가 사라진 검사와 같은 모양이면
    안 된다. 소비자는 항상 버린 줄 수를 찍을 수 있어야 한다."""
    path = write_log(tmp_path / "history.jsonl", [{"step": "judge"}, {"step": "simulation"}])

    summary = discard_summary(path)

    assert summary == {"total_lines": 2, "ranges": [], "discarded": 0, "kept": 2}


def test_discard_summary_counts_the_abandoned_lines(tmp_path):
    path = write_log(
        tmp_path / "history.jsonl",
        [
            {"step": "judge"},
            {"step": "tuning_proposal"},
            {"step": "area_check"},
            {"step": "resume", "discarded_lines": [1, 3]},
        ],
    )

    summary = discard_summary(path)

    assert summary == {"total_lines": 4, "ranges": [[1, 3]], "discarded": 2, "kept": 2}


def test_count_events_counts_only_the_given_range(tmp_path):
    path = write_log(
        tmp_path / "history.jsonl",
        [{"step": "a"}, {"step": "b"}, {"step": "c"}, {"step": "d"}],
    )

    assert count_events(path, 1, 3) == 2
    assert count_events(path, 0, 0) == 0
    assert count_events(path, 0, 99) == 4
