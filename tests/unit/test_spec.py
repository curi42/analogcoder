import textwrap
from analogcoder.spec import load_spec

SPEC_YAML = textwrap.dedent("""\
    circuit_name: inverting_amplifier
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

    spec = load_spec(str(spec_path))

    assert spec.circuit_name == "inverting_amplifier"
    assert spec.analyses == ["ac"]
    assert "meas ac gain_db" in spec.control_block
    assert len(spec.criteria) == 1
    c = spec.criteria[0]
    assert c.name == "closed_loop_gain"
    assert c.measurement == "gain_db"
    assert c.operator == ">="
    assert c.threshold == 19.5
    assert c.unit == "dB"
