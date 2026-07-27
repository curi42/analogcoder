from analogcoder.control_block import measurement_nets

BANDGAP_DC = """
.control
dc temp -40 125 1
meas dc vmax MAX v(vbgout)
meas dc vmin MIN v(vbgout)
meas dc vbg0_v FIND v(vbg0) AT=27
meas dc idd FIND i(Vdd) AT=27
let tc_ppm_per_c = (vmax-vmin)/(vbgout_v*165)*1e6
let iq_ua = -1e6*idd
.endc
"""


def test_a_meas_line_maps_its_name_to_the_nets_it_references():
    nets = measurement_nets(BANDGAP_DC)

    assert nets["vbg0_v"] == {"vbg0"}
    assert nets["vmax"] == {"vbgout"}


def test_a_current_reference_maps_to_the_source_it_names():
    nets = measurement_nets(BANDGAP_DC)

    assert nets["idd"] == {"Vdd"}


def test_a_let_expression_inherits_the_nets_of_the_measurements_it_references():
    nets = measurement_nets(BANDGAP_DC)

    # tc_ppm_per_c는 넷을 직접 언급하지 않는다. vmax/vmin을 통해서만 vbgout에 닿는다.
    assert nets["tc_ppm_per_c"] == {"vbgout"}
    assert nets["iq_ua"] == {"Vdd"}


def test_a_two_node_voltage_reference_yields_both_nets():
    nets = measurement_nets("meas ac gain_db MAX vdb(out,in)\n")

    assert nets["gain_db"] == {"out", "in"}


def test_an_unresolvable_name_yields_an_empty_set_rather_than_being_absent():
    # 빈 집합은 "이 measurement는 넷을 모른다"는 사실이고, 부재는 버그처럼 보인다.
    nets = measurement_nets("let mystery = undefined_thing * 2\n")

    assert nets["mystery"] == set()


# bandgap의 amp_loops 테스트벤치처럼, 같은 이름(tmag/tph)의 let이 alter/ac
# 블록마다 다시 정의되고 그 직후의 meas가 v()/i()가 아니라 그 중간 변수를
# 참조하는 실제 형태. 등장 순서를 따라가지 않으면 core_gain_db가 마지막
# 재정의(buf0)의 넷을 가리키는 조용한 오류가 난다.
REPEATED_LET_CONTROL_BLOCK = """
.control
let tmag = vdb(ampout)-vdb(mpgate)
let tph  = vp(ampout)-vp(mpgate)
meas ac core_gain_db FIND tmag AT=1
meas ac core_pm_deg  FIND tph WHEN tmag=0
let tmag = vdb(vbg0)-vdb(b0_i)
let tph  = vp(vbg0)-vp(b0_i)
meas ac buf0_gain_db FIND tmag AT=1
meas ac buf0_pm_deg  FIND tph WHEN tmag=0
.endc
"""


def test_a_meas_referencing_a_reassigned_let_uses_the_value_at_that_point():
    nets = measurement_nets(REPEATED_LET_CONTROL_BLOCK)

    assert nets["core_gain_db"] == {"ampout", "mpgate"}
    assert nets["core_pm_deg"] == {"ampout", "mpgate"}
    assert nets["buf0_gain_db"] == {"vbg0", "b0_i"}
    assert nets["buf0_pm_deg"] == {"vbg0", "b0_i"}
