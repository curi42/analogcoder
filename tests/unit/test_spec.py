import textwrap

import pytest

from analogcoder.spec import load_spec

SPEC_YAML = textwrap.dedent("""\
    circuit_name: inverting_amplifier
    testbenches:
      - name: ac_loop_gain
        netlist: netlist.cir
        analyses: ["ac"]
        control_block: |
          .control
          ac dec 10 1 1meg
          meas ac gain_db find vdb(vout) at=1k
          .endc
        criteria:
          - name: closed_loop_gain
            measurement: gain_db
            operator: ">="
            threshold: 19.5
            unit: dB
    """)


def test_load_spec(tmp_path):
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(SPEC_YAML)
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")

    spec = load_spec(str(spec_path))

    assert spec.circuit_name == "inverting_amplifier"
    assert len(spec.testbenches) == 1
    tb = spec.testbenches[0]
    assert tb.name == "ac_loop_gain"
    assert tb.netlist_path == str(tmp_path / "netlist.cir")
    assert tb.analyses == ["ac"]
    assert "meas ac gain_db" in tb.control_block
    assert len(tb.criteria) == 1
    c = tb.criteria[0]
    assert c.name == "closed_loop_gain"
    assert c.measurement == "gain_db"
    assert c.operator == ">="
    assert c.threshold == 19.5
    assert c.unit == "dB"


def test_canonical_returns_first_testbench(tmp_path):
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(SPEC_YAML)
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")

    spec = load_spec(str(spec_path))

    assert spec.canonical is spec.testbenches[0]
    assert spec.canonical.name == "ac_loop_gain"


def test_all_criteria_flattens_across_testbenches(tmp_path):
    multi_yaml = textwrap.dedent("""\
        circuit_name: two_stage_opamp
        testbenches:
          - name: ac_loop_gain
            netlist: a.cir
            analyses: ["ac"]
            control_block: ".control\\n.endc\\n"
            criteria:
              - name: dc_gain
                measurement: gain_db
                operator: ">="
                threshold: 70.0
                unit: dB
          - name: psr_plus
            netlist: b.cir
            analyses: ["ac"]
            control_block: ".control\\n.endc\\n"
            criteria:
              - name: psr_plus
                measurement: psr_plus_db
                operator: "<="
                threshold: -10.0
                unit: dB
        """)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(multi_yaml)

    spec = load_spec(str(spec_path))

    assert [c.name for c in spec.all_criteria] == ["dc_gain", "psr_plus"]


def test_netlist_path_resolved_relative_to_spec_directory(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    spec_path = nested / "spec.yaml"
    spec_path.write_text(SPEC_YAML)

    spec = load_spec(str(spec_path))

    assert spec.testbenches[0].netlist_path == str(nested / "netlist.cir")


def test_topology_required_spec_has_stricter_phase_margin_threshold():
    spec = load_spec("benchmarks/two_stage_opamp/spec_topology_required.yaml")
    baseline = load_spec("benchmarks/two_stage_opamp/spec.yaml")

    phase_margin = next(c for c in spec.canonical.criteria if c.name == "phase_margin")
    baseline_phase_margin = next(c for c in baseline.canonical.criteria if c.name == "phase_margin")

    assert phase_margin.threshold == 62.0
    assert phase_margin.threshold > baseline_phase_margin.threshold
    # On sky130 (see docs/superpowers/specs/2026-07-26-sky130-pdk-migration-design.md),
    # dc_gain and unity_gain_bandwidth are also raised, not just phase_margin -
    # every criterion in the harder spec is at least as strict as the baseline.
    baseline_by_name = {c.name: c.threshold for c in baseline.canonical.criteria}
    for c in spec.canonical.criteria:
        assert c.threshold >= baseline_by_name[c.name]


def test_load_spec_without_pvt_corners_defaults_to_none(tmp_path):
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(SPEC_YAML)
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")

    spec = load_spec(str(spec_path))

    assert spec.pvt_corners is None


def test_pvt_spec_declares_full_45_corner_sweep():
    spec = load_spec("benchmarks/two_stage_opamp/spec_pvt.yaml")

    assert spec.pvt_corners is not None
    assert spec.pvt_corners.process == ["tt", "ss", "ff", "sf", "fs"]
    assert spec.pvt_corners.voltage == [1.62, 1.8, 1.98]
    assert spec.pvt_corners.temperature == [-40.0, 27.0, 125.0]


def test_pvt_spec_reuses_baseline_spec_testbenches_and_thresholds():
    spec = load_spec("benchmarks/two_stage_opamp/spec_pvt.yaml")
    baseline = load_spec("benchmarks/two_stage_opamp/spec.yaml")

    assert [tb.name for tb in spec.testbenches] == [tb.name for tb in baseline.testbenches]
    assert [tb.netlist_path for tb in spec.testbenches] == [tb.netlist_path for tb in baseline.testbenches]
    for tb, baseline_tb in zip(spec.testbenches, baseline.testbenches):
        assert tb.control_block == baseline_tb.control_block
        assert {c.name: (c.operator, c.threshold) for c in tb.criteria} == {
            c.name: (c.operator, c.threshold) for c in baseline_tb.criteria
        }


def test_a_spec_without_an_optimize_block_has_none(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(
        "circuit_name: demo\ntestbenches:\n  - name: tb\n    netlist: n.cir\n"
        "    analyses: ['ac']\n    control_block: '.control\\n.endc\\n'\n"
        "    criteria: []\n"
    )
    assert load_spec(str(path)).optimize is None


def test_an_optimize_block_is_loaded_with_its_three_fields(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(
        "circuit_name: demo\n"
        "optimize:\n  objective: iq_ua\n  area_budget: 1.10\n  guard_band: 0.2\n"
        "testbenches:\n  - name: tb\n    netlist: n.cir\n"
        "    analyses: ['ac']\n    control_block: '.control\\n.endc\\n'\n"
        "    criteria: []\n"
    )
    opt = load_spec(str(path)).optimize

    assert opt.objective == "iq_ua"
    assert opt.area_budget == 1.10
    assert opt.guard_band == 0.2


def test_a_spec_can_declare_corner_reduction(tmp_path):
    path = tmp_path / "spec.yaml"
    path.write_text("""
circuit_name: t
corner_reduction:
  enabled: true
  retry_budget: 3
  probe: false
testbenches:
  - name: tb
    netlist: n.cir
    analyses: [ac]
    control_block: ".ac dec 10 1 1G"
    criteria:
      - name: gain
        measurement: g
        operator: ">="
        threshold: 40
""")
    spec = load_spec(str(path))
    assert spec.corner_reduction.enabled is True
    assert spec.corner_reduction.retry_budget == 3
    assert spec.corner_reduction.probe is False


def test_corner_reduction_defaults_are_on_with_a_budget_of_two(tmp_path):
    # 블록만 있고 필드가 없으면 기본값. 기본을 끄는 쪽으로 두면 스펙에 블록을
    # 적어 두고도 아무 일이 안 일어난다 - 이 저장소가 반복해서 당한 모양이다.
    path = tmp_path / "spec.yaml"
    path.write_text("""
circuit_name: t
corner_reduction: {}
testbenches:
  - name: tb
    netlist: n.cir
    analyses: [ac]
    control_block: ".ac dec 10 1 1G"
    criteria:
      - name: gain
        measurement: g
        operator: ">="
        threshold: 40
""")
    spec = load_spec(str(path))
    assert spec.corner_reduction.enabled is True
    assert spec.corner_reduction.retry_budget == 2
    assert spec.corner_reduction.probe is True


def test_a_spec_without_the_block_has_no_corner_reduction(tmp_path):
    path = tmp_path / "spec.yaml"
    path.write_text("""
circuit_name: t
testbenches:
  - name: tb
    netlist: n.cir
    analyses: [ac]
    control_block: ".ac dec 10 1 1G"
    criteria:
      - name: gain
        measurement: g
        operator: ">="
        threshold: 40
""")
    assert load_spec(str(path)).corner_reduction is None


def test_corner_reduction_explicit_enabled_false_produces_false(tmp_path):
    # Catches: the loader ignoring `enabled: false` (defaulting it to True, or
    # dropping the field). It does NOT catch the bool("false") coercion bug -
    # the comment here used to claim it did, and that was wrong: PyYAML parses
    # an unquoted `false` into a Python bool before get_bool ever sees it, so
    # this spec passes under the buggy loader too. The coercion bug is caught
    # by test_corner_reduction_raises_on_quoted_false_string below, which is
    # where a string actually reaches the loader.
    path = tmp_path / "spec.yaml"
    path.write_text("""
circuit_name: t
corner_reduction:
  enabled: false
testbenches:
  - name: tb
    netlist: n.cir
    analyses: [ac]
    control_block: ".ac dec 10 1 1G"
    criteria:
      - name: gain
        measurement: g
        operator: ">="
        threshold: 40
""")
    spec = load_spec(str(path))
    assert spec.corner_reduction.enabled is False


def test_corner_reduction_explicit_probe_false_produces_false(tmp_path):
    # Catches: the loader ignoring `probe: false`. As with the `enabled` test
    # above, it does NOT catch the bool("false") coercion - PyYAML has already
    # produced a Python bool by then. Legitimate coverage, corrected claim.
    path = tmp_path / "spec.yaml"
    path.write_text("""
circuit_name: t
corner_reduction:
  probe: false
testbenches:
  - name: tb
    netlist: n.cir
    analyses: [ac]
    control_block: ".ac dec 10 1 1G"
    criteria:
      - name: gain
        measurement: g
        operator: ">="
        threshold: 40
""")
    spec = load_spec(str(path))
    assert spec.corner_reduction.probe is False


def test_corner_reduction_raises_on_quoted_false_string(tmp_path):
    # Catches: bool("false") silently coercing to True instead of raising.
    # Authors coming from other config formats may write "false" as a quoted string.
    # int() and float() fail loud on bad input; bool fields must too.
    import pytest

    path = tmp_path / "spec.yaml"
    path.write_text("""
circuit_name: t
corner_reduction:
  enabled: "false"
testbenches:
  - name: tb
    netlist: n.cir
    analyses: [ac]
    control_block: ".ac dec 10 1 1G"
    criteria:
      - name: gain
        measurement: g
        operator: ">="
        threshold: 40
""")
    with pytest.raises(ValueError, match=r"corner_reduction\.enabled must be a boolean"):
        load_spec(str(path))


def test_corner_reduction_raises_on_integer_value_for_boolean(tmp_path):
    # Catches: bool(1) silently coercing to True, or bool(0) to False.
    # Some YAML authors might write 1/0 instead of true/false.
    import pytest

    path = tmp_path / "spec.yaml"
    path.write_text("""
circuit_name: t
corner_reduction:
  probe: 1
testbenches:
  - name: tb
    netlist: n.cir
    analyses: [ac]
    control_block: ".ac dec 10 1 1G"
    criteria:
      - name: gain
        measurement: g
        operator: ">="
        threshold: 40
""")
    with pytest.raises(ValueError, match=r"corner_reduction\.probe must be a boolean"):
        load_spec(str(path))


def _corner_reduction_spec(tmp_path, block: str):
    path = tmp_path / "spec.yaml"
    path.write_text(f"""
circuit_name: t
corner_reduction:
{block}
testbenches:
  - name: tb
    netlist: n.cir
    analyses: [ac]
    control_block: ".ac dec 10 1 1G"
    criteria:
      - name: gain
        measurement: g
        operator: ">="
        threshold: 40
""")
    return str(path)


def test_a_negative_retry_budget_is_rejected(tmp_path):
    # 음수 예산은 조용히 0처럼 동작한다 - 재진입을 켰다고 믿는 스펙에서
    # 아무 일도 일어나지 않는다. get_bool을 같은 함수 안에서 시끄럽게 만든
    # 것과 같은 이유로 여기서도 시끄럽게 실패한다.
    import pytest

    with pytest.raises(ValueError, match="retry_budget"):
        load_spec(_corner_reduction_spec(tmp_path, "  retry_budget: -1"))


def test_a_zero_retry_budget_is_allowed(tmp_path):
    # 0은 "재진입하지 않는다"는 유효한 선언이다. 범위 검사를 `<= 0`으로
    # 두는 변형이 이것을 막는다.
    assert load_spec(_corner_reduction_spec(tmp_path, "  retry_budget: 0")).corner_reduction.retry_budget == 0


def test_a_non_integer_retry_budget_is_rejected(tmp_path):
    # int("two")는 이미 시끄럽게 실패한다 - 그 동작을 못박아 둔다.
    import pytest

    with pytest.raises(ValueError):
        load_spec(_corner_reduction_spec(tmp_path, '  retry_budget: "two"'))


# --- §3.5: 테스트벤치 사이의 이름 충돌 ---------------------------------------
#
# 판정 경로 두 곳이 이름으로 색인된 슬롯 하나에 두 값을 쓴다:
# pvt.combined_worst_corners(criterion 이름)와 cli.simulate_fn의
# merged_measurements(measurement 이름). 둘 다 last-wins라 앞선 값이
# 조용히 사라진다. orchestrator.py는 초점 경로에서 정확히 이 이유로 이미
# 합집합 병합으로 고쳐졌다 - 판정 경로는 덮어쓰기로 남았으므로, 계약을
# 어기는 스펙을 로더가 거부한다.

def _two_testbench_spec(tmp_path, first_criterion: str, second_criterion: str) -> str:
    path = tmp_path / "spec.yaml"
    (tmp_path / "n1.cir").write_text("* n1\n.end\n")
    (tmp_path / "n2.cir").write_text("* n2\n.end\n")
    def block(name: str, netlist: str, criteria: str) -> str:
        return (
            f"  - name: {name}\n"
            f"    netlist: {netlist}\n"
            "    analyses: [ac]\n"
            '    control_block: ".ac dec 10 1 1G"\n'
            "    criteria:\n" + textwrap.indent(criteria, "      ")
        )

    path.write_text(
        "circuit_name: two_tb\ntestbenches:\n"
        + block("tb1", "n1.cir", first_criterion)
        + block("tb2", "n2.cir", second_criterion)
    )
    return str(path)


_GAIN_MIN = """\
- name: gain_min
  measurement: gain_db
  operator: ">="
  threshold: 60
"""
_GAIN_MAX = """\
- name: gain_max
  measurement: gain_db
  operator: "<="
  threshold: 80
"""
_PM = """\
- name: pm
  measurement: phase_margin
  operator: ">="
  threshold: 60
"""


def test_two_testbenches_sharing_a_criterion_name_are_rejected(tmp_path):
    # pvt.combined_worst_corners는 criterion 이름으로 색인된 dict를 update로
    # 채운다. 같은 이름이 둘이면 뒤에 온 테스트벤치의 최악 코너가 앞의 것을
    # 덮고, 앞 테스트벤치가 위반하는 사실이 overall_pass에서 사라진다.
    import pytest

    same_name = _GAIN_MIN.replace("gain_db", "phase_margin")
    with pytest.raises(ValueError, match="gain_min"):
        load_spec(_two_testbench_spec(tmp_path, _GAIN_MIN, same_name))


def test_two_testbenches_producing_the_same_measurement_name_are_rejected(tmp_path):
    # cli.simulate_fn이 테스트벤치별 측정값을 merged_measurements.update로
    # 합친다. 두 테스트벤치가 같은 measurement 이름을 내면 앞의 값이 버려지고
    # judge는 두 기준을 한 회로의 값으로 판정한다.
    import pytest

    with pytest.raises(ValueError, match="gain_db"):
        load_spec(_two_testbench_spec(tmp_path, _GAIN_MIN, _PM.replace("phase_margin", "gain_db")))


def test_a_two_sided_window_inside_one_testbench_is_still_allowed(tmp_path):
    # 한 테스트벤치 안에서 두 기준이 같은 measurement를 나눠 쓰는 것은
    # 정상이고 출하 스펙이 실제로 그렇게 쓴다(vbgout_min/vbgout_max).
    # 위 규칙을 테스트벤치 경계가 아니라 measurement 전역에 걸면 이것이
    # 깨진다.
    spec = load_spec(_two_testbench_spec(tmp_path, _GAIN_MIN + _GAIN_MAX, _PM))

    assert [c.name for c in spec.all_criteria] == ["gain_min", "gain_max", "pm"]


# --- §3.7: pvt_corners 축의 모양 --------------------------------------------

def _pvt_spec(tmp_path, pvt_block: str) -> str:
    path = tmp_path / "spec.yaml"
    (tmp_path / "n.cir").write_text("* n\n.end\n")
    path.write_text(textwrap.dedent("""\
        circuit_name: c
        testbenches:
          - name: tb
            netlist: n.cir
            analyses: [ac]
            control_block: ".ac dec 10 1 1G"
            criteria:
              - name: gain
                measurement: g
                operator: ">="
                threshold: 40
        pvt_corners:
        """) + textwrap.indent(textwrap.dedent(pvt_block), "  "))
    return str(path)


def test_a_bare_string_process_axis_is_rejected(tmp_path):
    # 대괄호를 빠뜨린 `process: tt`는 문자열이 문자 단위로 순회되어
    # CornerPoint(process='t')를 만들고, 렌더러가 존재하지 않는
    # pdk_corner_t.inc를 include해 45코너 전부가 NaN·FAIL이 된다.
    # 사람이 보는 표층 신호는 "회로가 모든 코너에서 망가졌다"인데 원인은
    # 대괄호 두 개다.
    import pytest

    with pytest.raises(ValueError, match="process"):
        load_spec(_pvt_spec(tmp_path, """\
            process: tt
            voltage: [1.8]
            temperature: [27]
            """))


def test_an_empty_axis_is_rejected(tmp_path):
    # 빈 축은 itertools.product를 0점으로 만든다 - pvt_corners를 선언한
    # 스펙이 코너를 하나도 안 도는데 아무 말도 없다.
    import pytest

    with pytest.raises(ValueError, match="voltage"):
        load_spec(_pvt_spec(tmp_path, """\
            process: [tt]
            voltage: []
            temperature: [27]
            """))


def test_a_non_numeric_voltage_is_rejected_by_name(tmp_path):
    # float("1.8v")의 ValueError는 이미 시끄럽지만 어느 축인지 안 말한다.
    import pytest

    with pytest.raises(ValueError, match="voltage"):
        load_spec(_pvt_spec(tmp_path, """\
            process: [tt]
            voltage: ["1.8v"]
            temperature: [27]
            """))


def test_a_non_string_process_entry_is_rejected(tmp_path):
    # YAML의 `process: [tt, 1.8]`은 코너 include 이름을 만들 수 없다.
    import pytest

    with pytest.raises(ValueError, match="process"):
        load_spec(_pvt_spec(tmp_path, """\
            process: [tt, 1.8]
            voltage: [1.8]
            temperature: [27]
            """))


def test_the_axis_check_does_not_constrain_process_label_content(tmp_path):
    # V2: 검사는 모양만 본다. process 라벨의 내용을 알려진 집합
    # (tt/ss/ff)으로 제한하면 대상 환경의 라벨 기반 코너 선언을
    # 구조적으로 막게 된다 - 이 저장소가 "이름으로 레일을 알아보기"를
    # 금지한 것과 같은 이유다.
    spec = load_spec(_pvt_spec(tmp_path, """\
        process: [worst_speed, worst_power]
        voltage: [1.8]
        temperature: [27]
        """))

    assert spec.pvt_corners.process == ["worst_speed", "worst_power"]


# --------------------------- 코너를 **열거로** 선언한다

# 대상 흐름에서 사인오프가 요구하는 것은 데카르트 곱 전체가 아니라 **사람이
# 고른 서명 코너 N개**이고, 그 선택은 analogcoder 밖의 코드가 한다(사용자 확인,
# 2026-07-29). 그러면 이 저장소가 받아야 하는 것은 곱이 아니라 목록이다.
#
# **목록은 곱을 표현할 수 있지만(전개해서 나열) 곱은 임의의 목록을 표현할 수
# 없다.** 부분 격자 - 금지 조합이 빠진 집합 - 는 어떤 축 선언으로도 못 만든다.
# 그래서 열거형이 두 세계의 정확한 공통 표현이고, 내부 표현을 그쪽으로 통일한다.
# 축 선언은 로더에서 그 자리에 전개되는 **설탕**으로 남는다.
#
# 그래서 이후에 "곱으로 되돌리는 최적화"를 하면 안 된다 - 표현력이 줄어든다.


def _spec_yaml(tmp_path, pvt_block):
    deck = tmp_path / "n.cir"
    deck.write_text("* t\n.end\n")
    path = tmp_path / "s.yaml"
    path.write_text(
        "circuit_name: c\n"
        "testbenches:\n"
        "  - name: tb\n"
        f"    netlist: {deck.name}\n"
        "    analyses: ['ac']\n"
        "    control_block: \".control\\nop\\n.endc\\n\"\n"
        "    criteria:\n"
        "      - name: gain\n"
        "        measurement: gain\n"
        "        operator: '>='\n"
        "        threshold: 1.0\n"
        f"{pvt_block}"
    )
    return str(path)


def test_an_axis_declaration_is_expanded_into_an_enumeration(tmp_path):
    path = _spec_yaml(
        tmp_path,
        "pvt_corners:\n  process: [tt, ss]\n  voltage: [1.62, 1.8]\n  temperature: [27]\n",
    )

    corners = load_spec(path).pvt_corners.corners

    assert [(c.process, c.voltage, c.temperature) for c in corners] == [
        ("tt", 1.62, 27.0),
        ("tt", 1.8, 27.0),
        ("ss", 1.62, 27.0),
        ("ss", 1.8, 27.0),
    ]


def test_an_explicit_corner_list_is_taken_as_declared(tmp_path):
    """사인오프 집합은 사람이 고른 것이므로 곱이 아니다. 이 셋은 어떤 축
    선언으로도 만들 수 없다 - (tt,1.8) 과 (ss,1.62) 를 함께 담으면서
    (tt,1.62) 와 (ss,1.8) 은 빼는 곱이 없다."""
    path = _spec_yaml(
        tmp_path,
        "pvt_corners:\n"
        "  corners:\n"
        "    - {process: tt, voltage: 1.8, temperature: 27}\n"
        "    - {process: ss, voltage: 1.62, temperature: 125}\n"
        "    - {process: ff, voltage: 1.98, temperature: -40}\n",
    )

    corners = load_spec(path).pvt_corners.corners

    assert [(c.process, c.voltage, c.temperature) for c in corners] == [
        ("tt", 1.8, 27.0),
        ("ss", 1.62, 125.0),
        ("ff", 1.98, -40.0),
    ]


def test_declaring_both_shapes_is_refused(tmp_path):
    """둘 다 있으면 어느 쪽이 이기는지 추측해야 한다. 이 저장소는 그런 자리에서
    조용히 한쪽을 고르지 않는다."""
    path = _spec_yaml(
        tmp_path,
        "pvt_corners:\n"
        "  process: [tt]\n  voltage: [1.8]\n  temperature: [27]\n"
        "  corners:\n    - {process: ss, voltage: 1.62, temperature: 125}\n",
    )

    with pytest.raises(ValueError, match="corners"):
        load_spec(path)


def test_an_empty_corner_list_is_refused(tmp_path):
    """빈 목록은 "코너 없음"이 아니라 선언 실수다 - 코너가 없다는 뜻이라면
    `pvt_corners` 블록 자체를 안 쓰면 된다(그 경우가 이미 있고 다르게 로그된다)."""
    path = _spec_yaml(tmp_path, "pvt_corners:\n  corners: []\n")

    with pytest.raises(ValueError, match="corners"):
        load_spec(path)


def test_a_duplicated_corner_in_the_list_is_refused(tmp_path):
    """`CornerSet`이 중복을 불변식으로 거부하므로 여기서 통과시키면 나중에
    진단이 ValueError로 바뀐다. 선언 자리에서 거부하는 편이 사유가 분명하다."""
    path = _spec_yaml(
        tmp_path,
        "pvt_corners:\n"
        "  corners:\n"
        "    - {process: tt, voltage: 1.8, temperature: 27}\n"
        "    - {process: tt, voltage: 1.8, temperature: 27}\n",
    )

    with pytest.raises(ValueError, match="duplicate"):
        load_spec(path)


def test_a_corner_entry_missing_a_coordinate_is_refused(tmp_path):
    """오늘의 렌더러는 세 좌표를 전부 읽는다. 빠진 것을 기본값으로 채우면
    N개 코너가 조용히 같은 조건을 돌 수 있다."""
    path = _spec_yaml(
        tmp_path,
        "pvt_corners:\n  corners:\n    - {process: tt, voltage: 1.8}\n",
    )

    with pytest.raises(ValueError, match="temperature"):
        load_spec(path)


def test_a_corner_reduction_without_a_coverage_block_declares_no_coverage(tmp_path):
    """블록이 없으면 `None`이다. 기본값 객체를 넣으면 '선언하지 않았다'와
    '기본값으로 선언했다'가 구별되지 않고, 이 설계의 전제(블록이 없으면 오늘
    동작과 바이트 동일)가 코드에서 보이지 않게 된다."""
    from analogcoder.spec import _load_corner_reduction

    cr = _load_corner_reduction({"corner_reduction": {"enabled": True}})

    assert cr is not None
    assert cr.coverage is None


def test_a_coverage_block_carries_epsilon_and_tau(tmp_path):
    from analogcoder.spec import _load_corner_reduction

    cr = _load_corner_reduction(
        {"corner_reduction": {"enabled": True, "coverage": {"epsilon": 0.03, "tau": 1.0}}}
    )

    assert cr.coverage.epsilon == 0.03
    assert cr.coverage.tau == 1.0


def test_a_coverage_block_needs_both_epsilon_and_tau():
    """기본값을 주지 않는다. epsilon 은 이 덱에서 **유도**해야 하는 값이고,
    코드가 하나 골라 두면 그 숫자가 근거 없이 생산 덱까지 따라간다."""
    from analogcoder.spec import _load_corner_reduction

    for block in ({"epsilon": 0.03}, {"tau": 1.0}, {}):
        with pytest.raises(ValueError, match="epsilon|tau"):
            _load_corner_reduction({"corner_reduction": {"coverage": block}})


@pytest.mark.parametrize("epsilon", [-0.1, 1.5])
def test_an_epsilon_outside_zero_to_one_is_refused(epsilon):
    """음수는 뜻이 없고, 1.0 초과는 '최악값의 100% 이상 떨어져도 덮는다'는
    뜻이라 사실상 전 코너가 전 기준을 덮는다 - 씨앗이 1개로 붕괴하면서
    로그는 정상으로 읽힌다."""
    from analogcoder.spec import _load_corner_reduction

    with pytest.raises(ValueError, match="epsilon"):
        _load_corner_reduction(
            {"corner_reduction": {"coverage": {"epsilon": epsilon, "tau": 1.0}}}
        )


@pytest.mark.parametrize("tau", [0.0, -0.5, 1.5])
def test_a_tau_outside_zero_exclusive_to_one_is_refused(tau):
    from analogcoder.spec import _load_corner_reduction

    with pytest.raises(ValueError, match="tau"):
        _load_corner_reduction(
            {"corner_reduction": {"coverage": {"epsilon": 0.03, "tau": tau}}}
        )


# --- 비교 연산자 검증 - 세 소비자가 서로 다르게 읽는 것은 로드에서 거절한다 --


@pytest.mark.parametrize("operator", ["==", ">==", "=>", "!=", "≥", ""])
def test_an_operator_the_repo_does_not_implement_consistently_is_refused(operator):
    """`spec.py`는 오늘까지 연산자를 **전혀** 검증하지 않았다 - 어떤 스펙도
    임의의 문자열을 넣을 수 있었고, 오타는 판정 시점의 `KeyError`로,
    `==`는 조용한 거짓 주장으로 나타났다.

    `==`가 왜 거절되는가: `judge_tools.relative_slack`과
    `baseline_ratio_allowances`는 그것을 상한(`<=`)으로 읽고
    `guard_band_violations`는 아예 건너뛴다. `vref == 1.2`가 1.0을 재면
    `relative_slack`은 **+0.167**, 즉 실패 중인 기준에 양수 여유를
    돌려준다. 셋 중 하나를 조용히 고르는 대신 거절한다."""
    from analogcoder.spec import _load_criteria

    with pytest.raises(ValueError, match="operator"):
        _load_criteria([
            {"name": "ref", "measurement": "vref_v", "operator": operator, "threshold": 1.2},
        ])


@pytest.mark.parametrize("operator", [">=", ">", "<=", "<"])
def test_the_four_implemented_operators_load(operator):
    """거절이 너무 넓지 않다는 쪽도 고정한다 - 이 넷은 세 소비자가 모두
    같은 뜻으로 구현한다. 출하된 벤치마크 14개 스펙의 기준 210개가 전부
    `>=` 아니면 `<=`이므로, 이 게이트는 오늘 아무것도 막지 않는다."""
    from analogcoder.spec import _load_criteria

    criteria = _load_criteria([
        {"name": "ref", "measurement": "vref_v", "operator": operator, "threshold": 1.2},
    ])

    assert criteria[0].operator == operator


def test_allowed_operators_is_exactly_what_judge_tools_classifies():
    """**`ALLOWED_OPERATORS`가 옳은 것은 이 불변식 덕분이고, 지금까지 그것을
    붙잡는 것이 아무것도 없었다.**

    `spec.py`의 허용 집합과 `judge_tools`의 방향 분류(`_LOWER_BOUND` ∪
    `_UPPER_BOUND`)가 **같아야** 한다. 한쪽에만 연산자를 추가하면 `==`에서
    막 닫은 결함이 한 줄 편집으로 되살아난다 - 다른 파일에서, 오류 없이:

    - `relative_slack`은 `if operator in _LOWER_BOUND: ... else: ...`라
      분류되지 않은 연산자를 **상한으로 읽는다**.
    - `baseline_ratio_allowances`도 같은 삼항으로 상한으로 읽고, 그 기준에
      여유분을 준다.
    - `guard_band_violations`는 `if _UPPER_BOUND / elif _LOWER_BOUND`라
      그 기준을 **통째로 건너뛴다** - 여유분은 나왔는데 적용되지 않고,
      `_unguarded`는 이름이 allowances에 있으므로 "방비됨"이라고 보고한다.

    셋 다 조용하다. `"!="`, `"~="`, 유니코드 `"≥"` 중 무엇을 넣어도 같다.

    두 번째 단언은 **부분집합**이지 동등이 아니다 - `judge_tools._OPERATORS`는
    `==`를 **일부러** 들고 있고(`evaluate_criteria`는 그것을 판정할 수 있다),
    `ALLOWED_OPERATORS`가 그것을 거절하는 이유는 그 아래 세 소비자가
    갈리기 때문이다. 비교표에 없는 연산자를 허용하면 판정 시점의
    `KeyError`인데, 그쪽은 **시끄럽다** - 위의 조용한 분기보다 위험이 낮아
    같은 테스트에 넣되 방향만 고정한다."""
    from analogcoder import judge_tools
    from analogcoder.spec import ALLOWED_OPERATORS

    classified = set(judge_tools._LOWER_BOUND) | set(judge_tools._UPPER_BOUND)

    assert set(ALLOWED_OPERATORS) == classified, (
        f"spec.ALLOWED_OPERATORS {sorted(ALLOWED_OPERATORS)} and judge_tools' "
        f"direction classification {sorted(classified)} disagree - an operator in "
        f"only one of them is read as an upper bound by relative_slack and "
        f"baseline_ratio_allowances and skipped entirely by guard_band_violations, "
        f"silently"
    )
    # 허용된 것은 전부 판정 가능해야 한다(역은 아니다 - '=='가 그 예다).
    assert set(ALLOWED_OPERATORS) <= set(judge_tools._OPERATORS)
