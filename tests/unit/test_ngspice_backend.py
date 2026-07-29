import os

from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec

BENCHMARK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "inverting_amp")


def test_ngspice_backend_runs_inverting_amp_benchmark():
    netlist_path = os.path.join(BENCHMARK_DIR, "netlist.cir")
    spec = load_spec(os.path.join(BENCHMARK_DIR, "spec.yaml"))

    backend = NgspiceBackend()
    result = backend.run(netlist_path, {"control_block": spec.canonical.control_block})

    assert result.status == "success"
    assert "gain_db" in result.measurements
    assert 19.0 <= result.measurements["gain_db"] <= 21.0


def test_ngspice_backend_reports_error_on_bad_netlist(tmp_path):
    bad_netlist = tmp_path / "bad.cir"
    bad_netlist.write_text("Rin in vminus\n.end\n")  # missing value token

    backend = NgspiceBackend()
    result = backend.run(str(bad_netlist), {"control_block": ".control\nac dec 10 1 1meg\n.endc"})

    assert result.status == "error"


def test_ngspice_backend_reports_error_on_missing_binary():
    backend = NgspiceBackend(ngspice_bin="/nonexistent/ngspice-binary-xyz")
    result = backend.run(
        os.path.join(BENCHMARK_DIR, "netlist.cir"),
        {"control_block": ".control\nac dec 10 1 1meg\n.endc"},
    )
    assert result.status == "error"
    assert "not found" in result.raw_log.lower()


def test_ngspice_backend_reports_error_on_timeout():
    backend = NgspiceBackend(timeout=0.001)
    result = backend.run(
        os.path.join(BENCHMARK_DIR, "netlist.cir"),
        {"control_block": ".control\nac dec 10 1 1meg\n.endc"},
    )
    assert result.status == "error"
    assert "timed out" in result.raw_log.lower()


def test_ngspice_backend_resolves_relative_includes_against_netlist_directory(tmp_path):
    # NgspiceBackend copies the netlist into its own private temp directory
    # before invoking ngspice. A relative .include path in the netlist must
    # still resolve against the ORIGINAL netlist's directory (where a
    # sibling .inc file actually lives), not the process's CWD (pytest's
    # CWD here is the repo root, unrelated to tmp_path) and not the
    # backend's private temp copy location.
    included = tmp_path / "shared.inc"
    included.write_text("* shared include\n.param unused_param=1\n")

    netlist = tmp_path / "netlist.cir"
    netlist.write_text(
        "* test\n"
        '.include "shared.inc"\n'
        "R1 in 0 1k\n"
        "V1 in 0 DC 1\n"
        ".end\n"
    )

    backend = NgspiceBackend()
    result = backend.run(str(netlist), {"control_block": ".control\nac dec 10 1 1meg\nmeas ac gain_db find v(in) at=1k\n.endc"})

    assert result.status == "success"
    assert "could not find include file" not in result.raw_log.lower()


# --------------------------------------------- 실패의 종류와 캐시 가능성

def test_the_cacheability_rule_is_declared_in_one_place_and_fails_closed():
    """`cacheable` 판정이 예외 두 개에 하드코딩돼 있었다.

    그 근거(`base.py`: "False 로 두는 것은 **환경이 낸 결과**뿐이다")는 규칙인데
    규칙이 적힌 자리가 없어서, **새 실패 축이 생기면 조용히 캐시 가능 쪽에
    합류한다.** HSPICE 의 라이선스 거부가 정확히 그 축이다 - 정의상 환경성인데
    ngspice 에 그 축이 없어서 판단이 내려진 적이 없을 뿐이다.

    그래서 미분류 종류는 **캐시하지 않는 쪽**으로 닫는다: 결정론적이라고
    확인되지 않은 실패를 캐시에 굳히는 것이 이 저장소가 피하려는 실패이고,
    반대 방향의 대가는 재실행 한 번뿐이다."""
    from analogcoder.simulators.base import FAILURE_KINDS, is_cacheable

    assert is_cacheable(None) is True  # 성공은 순수 함수의 결과다
    assert is_cacheable("timeout") is False
    assert is_cacheable("binary_missing") is False
    assert is_cacheable("convergence") is True
    assert is_cacheable("nonzero_exit") is True
    assert is_cacheable("no_measurements") is True
    # 분류표에 없는 종류는 캐시하지 않는다.
    assert is_cacheable("license") is False
    assert "license" not in FAILURE_KINDS


def test_a_raw_result_defaults_to_no_failure_kind():
    from analogcoder.simulators.base import RawSimResult

    assert RawSimResult(status="success", measurements={}, raw_log="").failure_kind is None


def test_ngspice_names_the_failure_kind_without_moving_status_or_cacheable(tmp_path):
    """**`(status, cacheable)` 쌍은 오늘과 한 글자도 다르지 않다.**
    `failure_kind` 는 순수 메타데이터이고, 그것이 이 변경의 안전성이다."""
    bad = tmp_path / "bad.cir"
    bad.write_text("Rin in vminus\n.end\n")
    control = {"control_block": ".control\nac dec 10 1 1meg\n.endc"}
    good = os.path.join(BENCHMARK_DIR, "netlist.cir")
    good_control = {"control_block": load_spec(os.path.join(BENCHMARK_DIR, "spec.yaml")).canonical.control_block}

    cases = [
        (NgspiceBackend(ngspice_bin="/nonexistent/ngspice-xyz").run(good, control),
         "error", False, "binary_missing"),
        (NgspiceBackend(timeout=0.001).run(good, control), "error", False, "timeout"),
        (NgspiceBackend().run(str(bad), control), "error", True, None),
        (NgspiceBackend().run(good, good_control), "success", True, None),
    ]

    for result, status, cacheable, kind in cases:
        assert result.status == status
        assert result.cacheable is cacheable
        if kind is not None:
            assert result.failure_kind == kind
    # 나쁜 덱은 "측정을 하나도 못 읽음" 이고 그것이 timeout 과 같은
    # `status="error"` 로 접혀 있었다. 이제 종류로 갈린다.
    assert cases[2][0].failure_kind in ("nonzero_exit", "no_measurements")
