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


def test_a_lowercase_size_mismatch_is_not_a_diff_pair():
    # 실제 벤치마크가 한 덱 안에서 W=/L=(FET)과 w=/l=(sky130 res_high_po,
    # cap_mim_m3_1)를 섞어 쓴다. 대소문자 그대로 비교하면 두 소자 다
    # params.get("W")가 None을 돌려주고, None == None이 통과해 6배
    # 차이나는 두 소자를 "크기가 같다"고 잘못 판정한다.
    deck = (
        "* t\n"
        "M1 nx vinn tail vss NMOS w=48 l=1\n"
        "M2 ny vinp tail vss NMOS w=8 l=1\n"
        ".end\n"
    )

    assert not any(kind == "diff_pair" for kind, _ in _kinds(deck))


def test_a_lowercase_size_match_is_still_a_diff_pair():
    # 위 테스트의 반대쪽: 대소문자만 다를 뿐 실제로 크기가 같으면 여전히
    # 잡혀야 한다 - 대소문자 무시 비교가 지나치게 좁아지지 않았는지 확인한다.
    deck = (
        "* t\n"
        "M1 nx vinn tail vss NMOS w=48 l=1\n"
        "M2 ny vinp tail vss NMOS w=48 l=1\n"
        ".end\n"
    )

    assert ("diff_pair", ("M1", "M2")) in _kinds(deck)


def test_two_devices_with_no_declared_size_are_not_a_diff_pair():
    # 크기가 .model 카드에만 있어 params에 W/L이 아예 없으면 둘 다 None이다.
    # None == None을 "같다"로 세면 모르는 값끼리의 우연한 동등을 사실로
    # 둔갑시키는 것이다 - 침묵이 정답이다.
    deck = (
        "* t\n"
        "M1 nx vinn tail vss NMOS\n"
        "M2 ny vinp tail vss NMOS\n"
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


def test_opposite_polarity_devices_sharing_a_source_are_not_a_diff_pair():
    # _same_kind가 없으면 이 모양(같은 소스, 다른 게이트/드레인, 같은 W/L)이
    # nfet/pfet을 가리지 않고 통과한다 - 극성이 다른 두 소자는 절대 차동쌍이
    # 아니다.
    deck = (
        "* t\n"
        "M1 na vinn vss vss NMOS W=10 L=1\n"
        "M2 nb vinp vss vss PMOS W=10 L=1\n"
        ".end\n"
    )

    assert not any(kind == "diff_pair" for kind, _ in _kinds(deck))


def test_a_different_length_is_not_a_diff_pair():
    # W만 같고 L이 다르면 차동쌍이 아니다 - W 조건만 있고 L 조건이 빠지면
    # 이 모양을 놓치지 않고(=오판하고) 잡아버린다.
    deck = (
        "* t\n"
        "M1 nx vinn tail vss NMOS W=48 L=1\n"
        "M2 ny vinp tail vss NMOS W=48 L=2\n"
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


def test_a_diode_connected_device_does_not_mirror_a_shared_gate_partner_with_a_different_source():
    # 실제 two_stage_opamp 벤치마크에서 확인된 모양: Xn1(g=nbias,d=pbias,
    # s=vss)와 Xn2(diode, g=d=nbias, s=degn 소스 디제너레이션)가 게이트를
    # 공유하고 Xn2가 다이오드 연결이지만, 소스가 다르다(vss vs degn) - 소스
    # 공유 조건이 없으면 이 쌍이 미러로 오판된다. 소스 디제너레이션 저항을
    # 건너서까지 비교하지 않는 것은 의도된 침묵이다(추측하느니 놓치는 쪽).
    deck = (
        "* t\n"
        "M1 pbias nbias vss vss NMOS W=10 L=1\n"
        "M2 nbias nbias degn vss NMOS W=10 L=1\n"
        ".end\n"
    )

    assert not any(kind == "current_mirror" for kind, _ in _kinds(deck))


def test_a_mos_capacitor_does_not_mirror_a_diode_connected_device_on_its_gate():
    # 실제 bandgap 벤치마크의 BGR_CORE.Xcc/BUF_N.Xcl/BUF_P.Xcl과 같은 모양:
    # d==s==b를 한 넷에 묶어 MOS를 커패시터로 쓰는 sky130 관용구. 그 소자의
    # 게이트가 우연히 다이오드 노드에 앉으면(넷 이름 하나 차이), 도통
    # 방향이 없는 소자인데도 다이오드 연결 소자의 미러 출력처럼 잡힌다 -
    # 실제 벤치마크에서는 게이트가 다이오드 노드가 아니라서 피했을 뿐이다.
    deck = (
        "* t\n"
        "Mdio nbias nbias vss vss NMOS W=10 L=1\n"
        "Mcap vss nbias vss vss NMOS W=20 L=20\n"
        ".end\n"
    )

    assert not any(kind == "current_mirror" for kind, _ in _kinds(deck))


def test_a_device_stacked_on_another_drain_is_a_stacked_pair():
    deck = (
        "* t\n"
        "M1 mid vin vss vss NMOS W=10 L=1\n"
        "M2 out ncas mid vss NMOS W=10 L=1\n"
        ".end\n"
    )

    assert ("stacked_pair", ("M1", "M2")) in _kinds(deck)


def test_a_differential_pair_stacked_on_its_tail_current_source_is_not_a_stacked_pair():
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

    assert not any(kind == "stacked_pair" for kind, _ in _kinds(deck))
    # 차동쌍 자체는 여전히 잡혀야 한다 - 이 테스트가 캐스코드 조건을 지나치게
    # 좁혀서 diff_pair까지 함께 죽이지 않았는지 확인한다.
    assert ("diff_pair", ("M1", "M2")) in _kinds(deck)


def test_a_folded_cascode_fold_node_is_still_a_stacked_pair():
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

    assert ("stacked_pair", ("Mcas", "Mload")) in _kinds(deck)
    # 폴드 노드의 다른 쪽(입력 소자, 극성이 다름)은 캐스코드로 잡히면 안
    # 된다 - 이게 바로 _same_kind가 캐스코드 루프에서 하는 일이다. 이
    # 클래스를 지우면 실제 bandgap 넷리스트 4개 증폭기 전체에서 폴드당
    # 하나씩, 총 8개의 거짓 캐스코드가 나온다(리뷰어가 뮤테이션으로 확인).
    assert not any(
        kind == "stacked_pair" and "M1" in members for kind, members in _kinds(deck)
    )


def test_a_shared_gate_stack_is_not_a_stacked_pair():
    # top과 bottom이 게이트를 공유하면 직렬로 쌓인 게 아니라 병렬로 같은
    # 신호를 보는 것이다 - 진짜 캐스코드의 캐스코드 게이트는 스택 아래
    # 소자의 게이트와 다른 고정 바이어스다.
    deck = (
        "* t\n"
        "Mb nfold gshared vss vss NMOS W=10 L=1\n"
        "Mt out gshared nfold vss NMOS W=10 L=1\n"
        ".end\n"
    )

    assert not any(kind == "stacked_pair" for kind, _ in _kinds(deck))


def test_a_source_follower_over_a_current_sink_is_labelled_stacked_pair_not_cascode():
    # source follower(게이트로 신호가 들어와 소스로 나오는 소자) 위에 전류
    # 싱크가 얹힌 모양은 진짜 캐스코드와 지역 그래프 모양이 완전히 같다 -
    # 어느 넷이 "바이어스"고 어느 넷이 "신호"인지는 이름을 봐야 아는데,
    # 이름으로 판단하는 것은 이 모듈이 금지하는 추측이다. 예전에는 이 모양을
    # "cascode"로 부르고 그 오류를 문서화만 했지만, 거짓 양성 0이 기준이라면
    # 답은 "침묵 아니면 참인 이름"이지 "틀린 이름"이 아니다. stacked_pair는
    # 캐스코드에도, source follower에도, 파워 스위치에도 참이다 - 매처가
    # 추측하면 안 되는 명명 지식은 LLM이 얹으면 된다.
    deck = (
        "* t\n"
        "Mf vdd vin nout vss NMOS W=10 L=1\n"
        "Msink nout vbias vss vss NMOS W=10 L=1\n"
        ".end\n"
    )

    kinds = _kinds(deck)

    assert ("stacked_pair", ("Mf", "Msink")) in kinds
    assert not any(kind == "cascode" for kind, _ in kinds)


def test_a_power_gating_switch_is_labelled_stacked_pair_not_cascode():
    # 같은 이유의 다른 예: 파워 게이팅 하이사이드 스위치가 소자 하나를
    # 켜고 끄는 모양도 캐스코드와 그래프 모양이 같다.
    deck = (
        "* t\n"
        "Mdev nsw vin vss vss NMOS W=10 L=1\n"
        "Msw vdd en nsw vss NMOS W=10 L=1\n"
        ".end\n"
    )

    kinds = _kinds(deck)

    assert ("stacked_pair", ("Mdev", "Msw")) in kinds
    assert not any(kind == "cascode" for kind, _ in kinds)


def test_the_stacked_pair_detail_states_the_connection_not_an_interpretation():
    # detail이 게이트 넷을 "bias"라고 부르면 kind에서 걷어낸 바로 그 추측을
    # 한 칸 옆에서 다시 한다. 낼 수 있는 사실은 연결 관계뿐이다.
    deck = (
        "* t\n"
        "M1 mid vin vss vss NMOS W=10 L=1\n"
        "M2 out ncas mid vss NMOS W=10 L=1\n"
        ".end\n"
    )
    match = next(
        m for m in find_patterns(derive_structure(deck, "t")) if m.kind == "stacked_pair"
    )

    assert match.detail == "M2.s == M1.d at mid, M2.g on ncas"
    assert "bias" not in match.detail


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


def test_a_cap_bridging_nets_shared_by_two_devices_is_not_miller():
    # 커패시터의 두 끝({g,d})을 만족하는 소자가 둘 이상이면 어느 게 "그"
    # 이득단인지 추측하는 셈이다 - 캐스코드나 미러 출력 레그를 우연히
    # 가로지르는 커패시터가 통과하지 못하게 막는다. 이 조건이 두 벤치마크
    # 10개 덱 전부에서 실제 매칭을 하나도 지우지 않는다는 것을 Step 6
    # 재검증으로 확인했다(리포트 참고).
    deck = (
        "* t\n"
        "M1 vout outA vss vss NMOS W=10 L=1\n"
        "M2 vout outA tail vss NMOS W=10 L=1\n"
        "Cc outA vout 3p\n"
        ".end\n"
    )

    assert not any(kind == "miller_compensation" for kind, _ in _kinds(deck))


def test_a_three_terminal_pdk_resistor_still_carries_the_miller_nulling_hop():
    # sky130의 res_high_po는 포트 두 개 + 몸체/웰 넷까지 3노드다.
    # len(nodes) != 2로 두 단자를 판정하면 이런 소자는 언제나 걸러져서,
    # 실제 PDK 덱에서는 널링 저항 홉이 죽는다. structure.py가 res 클래스에
    # 매긴 2단자 표(terminals)로 판정하도록 고쳤다 - 앞의 두 노드가 실제
    # 신호 단자이고 몸체는 항상 마지막이다.
    deck = (
        "* t\n"
        "M6 vout outA vss vss NMOS W=40 L=1\n"
        "Cc outA nz 3p\n"
        "XRz nz vout 0 sky130_fd_pr__res_high_po w=1 l=40\n"
        ".end\n"
    )
    matches = [m for m in find_patterns(derive_structure(deck, "t")) if m.kind == "miller_compensation"]

    assert matches and "XRz" in matches[0].members


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
