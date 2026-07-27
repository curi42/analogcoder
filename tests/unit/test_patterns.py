from analogcoder.patterns import find_patterns
from analogcoder.structure import derive_structure


def _kinds(deck):
    return {(m.kind, tuple(m.members)) for m in find_patterns(derive_structure(deck, "t"))}


def test_a_matched_differential_pair_is_reported():
    deck = (
        "* t\n"
        "M1 nx vinn tail vss NMOS W=48 L=1\n"
        "M2 ny vinp tail vss NMOS W=48 L=1\n"
        ".end\n"
    )

    assert ("diff_pair", ("M1", "M2")) in _kinds(deck)


def test_two_devices_sharing_a_source_but_not_matched_are_not_a_pair():
    # W가 다르면 차동쌍이 아니다. 침묵이 정답이고, 추측한 매칭은 사실보다 나쁘다.
    deck = (
        "* t\n"
        "M1 nx vinn tail vss NMOS W=48 L=1\n"
        "M2 ny vinp tail vss NMOS W=8 L=1\n"
        ".end\n"
    )

    assert not any(kind == "diff_pair" for kind, _ in _kinds(deck))


def test_two_devices_sharing_a_widely_used_rail_are_not_a_diff_pair():
    # 실제 벤치마크(bandgap의 startup 체인)에서 발견된 거짓 양성: 서로 무관한
    # 두 소자가 우연히 같은 W/L을 쓰면서 공통 소스가 "tail 노드"가 아니라
    # 수십 개 소자가 매달린 전원 레일(vss)이면, 그건 차동쌍이 아니라 흔한
    # 우연의 일치다. 세 번째 소자가 같은 레일을 더 써서 팬아웃을 3으로
    # 만든다 - 진짜 차동쌍의 tail은 정확히 2개 소자만 소스로 문다.
    deck = (
        "* t\n"
        "M1 na vinn vss vss NMOS W=10 L=1\n"
        "M2 nb vinp vss vss NMOS W=10 L=1\n"
        "M3 nc vbias vss vss NMOS W=10 L=1\n"
        ".end\n"
    )

    assert not any(kind == "diff_pair" for kind, _ in _kinds(deck))


def test_two_devices_sharing_a_gate_with_one_diode_connected_are_a_mirror():
    deck = (
        "* t\n"
        "M1 nb nb vss vss NMOS W=10 L=1\n"
        "M2 out nb vss vss NMOS W=10 L=1\n"
        ".end\n"
    )

    assert ("current_mirror", ("M1", "M2")) in _kinds(deck)


def test_a_shared_gate_without_a_diode_connection_is_not_a_mirror():
    deck = (
        "* t\n"
        "M1 na nb vss vss NMOS W=10 L=1\n"
        "M2 out nb vss vss NMOS W=10 L=1\n"
        ".end\n"
    )

    assert not any(kind == "current_mirror" for kind, _ in _kinds(deck))


def test_a_device_stacked_on_another_drain_with_a_bias_gate_is_a_cascode():
    deck = (
        "* t\n"
        "M1 mid vin vss vss NMOS W=10 L=1\n"
        "M2 out ncas mid vss NMOS W=10 L=1\n"
        ".end\n"
    )

    assert ("cascode", ("M1", "M2")) in _kinds(deck)


def test_a_differential_pair_stacked_on_its_tail_current_source_is_not_a_cascode():
    # 실제 벤치마크(two_stage_opamp/bandgap 둘 다)에서 발견된 거짓 양성: 차동쌍의
    # 두 소자가 tail 전류원의 드레인을 공통 소스로 문다. s(top)==d(bottom)라는
    # 조건만 보면 캐스코드처럼 보이지만, 진짜 캐스코드는 그 노드를 소스로 무는
    # 소자가 정확히 하나다(직렬 스택) - tail은 둘(차동쌍의 두 다리)이 문다.
    # 그 차이가 "차동쌍이 tail 위에 있다"와 "한 소자가 다른 소자 위에 쌓였다"를
    # 가른다.
    deck = (
        "* t\n"
        "Mbias tail vbias vss vss NMOS W=20 L=1\n"
        "M1 n1 vinn tail vss NMOS W=10 L=1\n"
        "M2 n2 vinp tail vss NMOS W=10 L=1\n"
        ".end\n"
    )

    assert not any(kind == "cascode" for kind, _ in _kinds(deck))
    # 차동쌍 자체는 여전히 잡혀야 한다 - 이 테스트가 캐스코드 조건을 지나치게
    # 좁혀서 diff_pair까지 함께 죽이지 않았는지 확인한다.
    assert ("diff_pair", ("M1", "M2")) in _kinds(deck)


def test_a_folded_cascode_fold_node_is_still_a_cascode():
    # 실제 bandgap 벤치마크로 확인된 규칙: 폴디드 캐스코드의 폴드 노드는
    # 드레인을 정확히 둘(입력 소자 + 접는 소자, 서로 다른 극성) 무는 게
    # 구조상 정상이다. 캐스코드 판정에서 bottom의 드레인 팬아웃까지
    # ==1로 요구하면 이 정상적인 경우가 통째로 사라진다 - 그래서 그
    # 조건은 top의 소스 팬아웃에만 건다.
    deck = (
        "* t\n"
        "M1 fold vinn tail vss NMOS W=48 L=1\n"  # 입력 소자, 드레인=fold
        "Mload fold pbias vdd vdd PMOS W=8 L=2\n"  # 접는 소자, 드레인도 fold
        "Mcas out ncas fold vdd PMOS W=8 L=1\n"  # 캐스코드, 소스=fold, Mload와 같은 극성
        ".end\n"
    )

    assert ("cascode", ("Mcas", "Mload")) in _kinds(deck)


def test_a_cap_between_a_gain_stage_input_gate_and_its_output_drain_is_miller():
    deck = (
        "* t\n"
        "M6 vout outA vss vss NMOS W=40 L=1\n"
        "Cc outA vout 3p\n"
        ".end\n"
    )

    assert ("miller_compensation", ("Cc", "M6")) in _kinds(deck)


def test_a_series_nulling_resistor_is_reported_with_the_miller_cap():
    deck = (
        "* t\n"
        "M6 vout outA vss vss NMOS W=40 L=1\n"
        "Cc outA nz 3p\n"
        "Rz nz vout 220k\n"
        ".end\n"
    )
    matches = [m for m in find_patterns(derive_structure(deck, "t")) if m.kind == "miller_compensation"]

    assert matches and "Rz" in matches[0].members


def test_a_decoupling_cap_to_ground_is_not_miller_compensation():
    deck = (
        "* t\n"
        "M6 vout outA vss vss NMOS W=40 L=1\n"
        "Cd vout 0 3p\n"
        ".end\n"
    )

    assert not any(kind == "miller_compensation" for kind, _ in _kinds(deck))


def test_matches_are_scoped_to_their_block():
    deck = (
        "* t\n"
        ".subckt AMP vss\n"
        "M1 nx vinn tail vss NMOS W=48 L=1\n"
        "M2 ny vinp tail vss NMOS W=48 L=1\n"
        ".ends AMP\n"
        ".end\n"
    )

    match = find_patterns(derive_structure(deck, "t"))[0]

    assert match.block == "AMP"
    assert match.members == ("AMP.M1", "AMP.M2")


def test_pattern_finding_is_deterministic():
    deck = (
        "* t\n"
        "M1 nb nb vss vss NMOS W=10 L=1\n"
        "M2 out nb vss vss NMOS W=10 L=1\n"
        ".end\n"
    )
    s = derive_structure(deck, "t")

    assert find_patterns(s) == find_patterns(s)
