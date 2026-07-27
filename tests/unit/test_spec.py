import textwrap

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
