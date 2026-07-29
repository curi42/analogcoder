from analogcoder.topologies import Topology
from analogcoder.topology_match import SwapCandidate, compatible_swaps, unavailable_reason

FIVE_PORT = Topology(
    id="five_port", description="d", addresses=[],
    ports=["vinp", "vinn", "vout", "vdd", "vss"], assumes_scale=1e-6,
    provenance="authored", verified_at="nominal",
    subckt_body="M1 vout vinp vdd vss NMOSG W=2 L=1\nM2 vout vinn vss vss NMOSG W=2 L=1\n",
)
NINE_PORT = Topology(
    id="nine_port", description="d", addresses=[],
    ports=["vinp", "vinn", "vout", "vdd", "vss", "nbias", "ncas", "pbias", "pcas"],
    assumes_scale=1e-6,
    provenance="authored", verified_at="nominal",
    subckt_body=(
        "M1 vout vinp vdd vss NMOSG W=2 L=1\n"
        "M2 vout vinn vss vss NMOSG W=2 L=1\n"
        "M3 nbias ncas pbias pcas NMOSG W=2 L=1\n"
    ),
)

DECK_9 = """* t
.option scale=1.0u
.subckt AMP vinp vinn vout vdd vss nbias ncas pbias pcas
M1 vout vinp vdd vss NMOSG W=2 L=1
.ends AMP
.end
"""
DECK_5 = """* t
.option scale=1.0u
.subckt AMP vinp vinn vout vdd vss
M1 vout vinp vdd vss NMOSG W=2 L=1
.ends AMP
.end
"""

LIB = {"five_port": FIVE_PORT, "nine_port": NINE_PORT}

# 9포트 블록을 두 번 인스턴스화하고, 남는 네 포트(nbias/ncas/pbias/pcas)의 넷을
# 두 인스턴스가 공유한다 - bandgap의 실제 바이어스 체인과 같은 모양. 부동 넷
# 검사가 통과해야 하는 경우다.
DECK_9_SHARED_BIAS = """* t
.option scale=1.0u
.subckt AMP vinp vinn vout vdd vss nbias ncas pbias pcas
M1 vout vinp vdd vss NMOSG W=2 L=1
.ends AMP
Xu1 a1 b1 c1 vdd vss nbias ncas pbias pcas AMP
Xu2 a2 b2 c2 vdd vss nbias ncas pbias pcas AMP
.end
"""

# 인스턴스가 정확히 하나 있지만, 남는 포트의 넷(nbias/ncas/pbias/pcas)을 그
# 스코프의 다른 어떤 소자도 참조하지 않는다 - 유일 참여자.
DECK_9_LONE_INSTANCE = """* t
.option scale=1.0u
.subckt AMP vinp vinn vout vdd vss nbias ncas pbias pcas
M1 vout vinp vdd vss NMOSG W=2 L=1
.ends AMP
Xu1 a1 b1 c1 vdd vss nbias ncas pbias pcas AMP
.end
"""

# 인스턴스(Xu1)의 노드가 5개뿐인데(위치 인자 부족 - 잘못된 SPICE) AMP는
# 포트를 9개 선언한다. 남는 포트(nbias 등) 위치가 인스턴스의 노드 목록
# 범위를 벗어난다 - _leftover_ports_float_reason의 방어 분기.
DECK_9_SHORT_INSTANCE = """* t
.option scale=1.0u
.subckt AMP vinp vinn vout vdd vss nbias ncas pbias pcas
M1 vout vinp vdd vss NMOSG W=2 L=1
.ends AMP
Xu1 a1 b1 c1 vdd vss AMP
.end
"""

# 같은 정의(AMP)를 세 번 인스턴스화한다: Xu1a/Xu1b는 남는 포트의 넷
# (nbias/ncas/pbias/pcas)을 서로 공유해 통과하지만, Xu2는 별도의 넷
# (nbias2/...)을 혼자 쓴다 - 유일 참여자라 실패. "모든 인스턴스가 통과해야
# 한다"는 규칙은 인스턴스 하나라도 실패하면 전체가 거부됨을 요구한다.
DECK_9_ONE_INSTANCE_FLOATS = """* t
.option scale=1.0u
.subckt AMP vinp vinn vout vdd vss nbias ncas pbias pcas
M1 vout vinp vdd vss NMOSG W=2 L=1
.ends AMP
Xu1a a1 b1 c1 vdd vss nbias ncas pbias pcas AMP
Xu1b a2 b2 c2 vdd vss nbias ncas pbias pcas AMP
Xu2  a3 b3 c3 vdd vss nbias2 ncas2 pbias2 pcas2 AMP
.end
"""


def _reasons(rejections, topology_id):
    return {r.reason for r in rejections if r.topology_id == topology_id}


def test_a_five_port_topology_is_rejected_for_a_nine_port_block():
    """**거부되는 사유가 포트 규칙 자체는 아니다.** `bc53d9e` 이후 포트 규칙은
    `topo_ports <= block_ports`(부분집합)이므로 5포트 본문은 9포트 블록의 포트
    검사를 통과한다. 여기서 거부되는 실제 이유는 `DECK_9`가 `AMP`를 어디서도
    인스턴스화하지 않아 남는 네 포트가 뜰지를 판정할 근거가 없기 때문이다
    (실측 detail: "block 'AMP' has no instance anywhere in this testbench, so
    whether leftover port(s) ['nbias','ncas','pbias','pcas'] would float cannot
    be judged"). 같은 사실을 정면으로 다루는 것이
    `test_a_definition_with_no_instance_and_leftover_ports_is_rejected`이고,
    포트 부분집합이 실제로 통과하는 모양은
    `test_a_candidate_whose_ports_are_a_subset_is_allowed`가 고정한다."""
    cands, rej = compatible_swaps({"tb": DECK_9}, LIB, set())
    assert cands == [SwapCandidate(block_path="AMP", topology_id="nine_port")]
    assert _reasons(rej, "five_port") == {"ports"}


def test_a_nine_port_topology_is_rejected_for_a_five_port_block():
    """포트 규칙이 **유지하는 한 방향**(`topo_ports <= block_ports`)을 고정한다.

    이 독스트링은 원래 "양방향 확인"이라고 적혀 있었으나 그것은 `bc53d9e`
    이전(완전 집합 동등) 규칙의 서술이고 오늘 코드는 단방향 부분집합이다 -
    CLAUDE.md에 남아 있는 것과 같은 드리프트라서 여기서 정정한다. 이 테스트가
    잡는 것은 남은 그 한 방향을 마저 지운 구현이다: 후보가 블록에 없는 포트를
    요구하면(9포트 후보 vs 5포트 블록) 여전히 거부되어야 한다. 반대 방향
    (블록이 후보보다 포트를 더 가짐)은 **의도적으로 허용**되며 그쪽을 고정하는
    것은 `test_a_candidate_whose_ports_are_a_subset_is_allowed`와
    `test_a_five_port_body_really_does_land_in_a_nine_port_block`이다."""
    cands, rej = compatible_swaps({"tb": DECK_5}, LIB, set())
    assert cands == [SwapCandidate(block_path="AMP", topology_id="five_port")]
    assert _reasons(rej, "nine_port") == {"ports"}


def test_a_model_the_deck_never_instantiates_is_rejected():
    other = Topology(
        id="other", description="d", addresses=[],
        ports=["vinp", "vinn", "vout", "vdd", "vss"], assumes_scale=1e-6,
        provenance="authored", verified_at="nominal",
        subckt_body="M1 vout vinp vdd vss PMOS_NOT_IN_DECK W=2 L=1\n"
                    "M2 vout vinn vss vss PMOS_NOT_IN_DECK W=2 L=1\n",
    )
    cands, rej = compatible_swaps({"tb": DECK_5}, {"other": other}, set())
    assert cands == []
    assert _reasons(rej, "other") == {"models"}


def test_a_scale_mismatch_is_rejected():
    deck = DECK_5.replace(".option scale=1.0u", ".option scale=1.0n")
    cands, rej = compatible_swaps({"tb": deck}, {"five_port": FIVE_PORT}, set())
    assert cands == []
    assert _reasons(rej, "five_port") == {"scale"}


def test_a_block_missing_from_one_testbench_is_not_a_candidate():
    other_deck = DECK_5.replace("AMP", "OTHER")
    cands, rej = compatible_swaps({"a": DECK_5, "b": other_deck}, {"five_port": FIVE_PORT}, set())
    assert cands == []
    assert _reasons(rej, "five_port") == {"missing_in_testbench"}


def test_a_tried_pair_is_dropped_but_its_siblings_survive():
    two_blocks = DECK_9 + DECK_9.replace("AMP", "AMP2").replace("* t\n.option scale=1.0u\n", "")
    cands, rej = compatible_swaps({"tb": two_blocks}, LIB, {("AMP", "nine_port")})
    assert cands == [SwapCandidate(block_path="AMP2", topology_id="nine_port")]
    # 그리고 그 탈락이 **기록된다**. 그냥 continue하면 "이미 써 봤다"가 부재로만
    # 나타나, 라이브러리 소진이 "판정이 사라짐"과 똑같이 보인다. 사유를 지우는
    # 변형은 여기서 걸린다.
    tried = [r for r in rej if r.reason == "already_tried"]
    assert [(r.block_path, r.topology_id) for r in tried] == [("AMP", "nine_port")]
    assert "already attempted" in tried[0].detail


def test_the_reason_for_zero_candidates_distinguishes_four_different_facts():
    """후보 0개는 하나의 관측이지만 사실은 넷이다. 넷을 한 줄로 뭉개면
    "검사했고 후보가 없음"과 "검사가 사라짐"이 로그에서 같아진다 - 이 저장소가
    다섯 번 반복한 침묵한 게이트의 모양이다. 사유 코드를 상수 하나로 되돌리는
    변형이 여기서 걸린다."""
    flat_deck = "* t\n.option scale=1.0u\nRf a b 10k\n.end\n"
    assert unavailable_reason({"tb": flat_deck}, LIB, []) == "no_subckt_definitions"
    # 정의는 있는데 라이브러리가 비었다 - 기각 목록도 비므로 그것으로는
    # 구별할 수 없다.
    assert unavailable_reason({"tb": DECK_5}, {}, []) == "empty_library"

    _, all_tried = compatible_swaps(
        {"tb": DECK_5}, {"five_port": FIVE_PORT}, {("AMP", "five_port")}
    )
    assert unavailable_reason({"tb": DECK_5}, {"five_port": FIVE_PORT}, all_tried) == (
        "all_pairs_already_tried"
    )

    _, refused = compatible_swaps({"tb": DECK_5}, {"nine_port": NINE_PORT}, set())
    assert unavailable_reason({"tb": DECK_5}, {"nine_port": NINE_PORT}, refused) == (
        "all_pairs_rejected"
    )


def test_the_candidate_order_is_deterministic():
    two_blocks = DECK_9 + DECK_9.replace("AMP", "AMP2").replace("* t\n.option scale=1.0u\n", "")
    first, _ = compatible_swaps({"tb": two_blocks}, LIB, set())
    second, _ = compatible_swaps({"tb": two_blocks}, LIB, set())
    assert first == second == sorted(first, key=lambda c: (c.block_path, c.topology_id))


def test_an_identical_body_swap_is_rejected_as_a_no_op():
    """토폴로지 본문이 그 블록의 현재 본문과 컴포넌트 시퀀스가 완전히 같으면
    (포트/모델/스케일이 전부 통과해도) no-op이므로 후보가 아니다. 이 검사를
    빼면(=identical_body 체크를 제거하면) 이 쌍이 다시 후보로 살아난다 -
    그것이 이 테스트가 잡는 변형이다."""
    deck = f"""* t
.option scale=1.0u
.subckt AMP vinp vinn vout vdd vss
{FIVE_PORT.subckt_body}.ends AMP
.end
"""
    cands, rej = compatible_swaps({"tb": deck}, {"five_port": FIVE_PORT}, set())
    assert cands == []
    assert _reasons(rej, "five_port") == {"identical_body"}


def test_the_bandgap_deck_offers_swaps_that_the_old_rule_made_impossible():
    from pathlib import Path

    from analogcoder.topologies import TOPOLOGY_LIBRARY

    text = Path("benchmarks/bandgap/netlist.cir").read_text()
    cands, rej = compatible_swaps({"dc": text}, TOPOLOGY_LIBRARY, set())
    paths = {c.block_path for c in cands}
    assert {"ERRAMP", "BUF_N"} <= paths
    assert all(c.topology_id.startswith("folded_cascode_") for c in cands)
    # 포트 규칙이 부분집합으로 완화된 뒤: ERRAMP/TRIMAMP/BUF_N/BUF_P는 9포트라
    # miller_basic(5포트)의 포트 부분집합 검사와 부동 넷 검사를 모두 통과한다
    # (bandgap의 바이어스 체인을 그 스코프의 형제 인스턴스들이 공유하므로) -
    # 그다음 models 규칙에서 걸린다(bandgap 덱은 sky130_fd_pr__cap_mim_m3_1을
    # 안 쓴다). BGR_CORE/BANDGAP은 vinp/vinn/vout 자체가 없어 여전히 ports로
    # 걸린다. 그래서 miller_basic의 기각 사유 집합은 이제 둘 다 나온다 - F1의
    # 완전 동등 규칙 아래서는 전부 ports 하나였다.
    assert {r.reason for r in rej if r.topology_id == "miller_basic"} == {"ports", "models"}
    # BGR_CORE/BANDGAP은 포트가 다르므로 후보에 없어야 한다
    assert "BGR_CORE" not in paths
    assert "BANDGAP" not in paths
    # folded_cascode_nmos_in_cs의 본문은 TRIMAMP 본문과, folded_cascode_pmos_in_cs의
    # 본문은 BUF_P 본문과 컴포넌트 시퀀스가 완전히 같다 (실측, 리뷰어가 diff로 확인) -
    # 이 둘은 "회로를 하나도 안 바꾸는 스왑"이라 후보가 아니라 identical_body로 기각된다.
    assert SwapCandidate(block_path="TRIMAMP", topology_id="folded_cascode_nmos_in_cs") not in cands
    assert SwapCandidate(block_path="BUF_P", topology_id="folded_cascode_pmos_in_cs") not in cands
    assert "identical_body" in {
        r.reason for r in rej if r.block_path == "TRIMAMP" and r.topology_id == "folded_cascode_nmos_in_cs"
    }
    assert "identical_body" in {
        r.reason for r in rej if r.block_path == "BUF_P" and r.topology_id == "folded_cascode_pmos_in_cs"
    }
    # ERRAMP/BUF_N은 사이징이 달라 진짜 스왑이므로 두 folded_cascode 항목 모두의 후보로 남는다.
    for block in ("ERRAMP", "BUF_N"):
        for topology_id in ("folded_cascode_nmos_in_cs", "folded_cascode_pmos_in_cs"):
            assert SwapCandidate(block_path=block, topology_id=topology_id) in cands
    # 정확한 후보 집합 고정: 8개(no-op 필터링 전)에서 no-op 2개가 빠져 6개.
    assert cands == sorted(
        [
            SwapCandidate(block_path="BUF_N", topology_id="folded_cascode_nmos_in_cs"),
            SwapCandidate(block_path="BUF_N", topology_id="folded_cascode_pmos_in_cs"),
            SwapCandidate(block_path="BUF_P", topology_id="folded_cascode_nmos_in_cs"),
            SwapCandidate(block_path="ERRAMP", topology_id="folded_cascode_nmos_in_cs"),
            SwapCandidate(block_path="ERRAMP", topology_id="folded_cascode_pmos_in_cs"),
            SwapCandidate(block_path="TRIMAMP", topology_id="folded_cascode_pmos_in_cs"),
        ],
        key=lambda c: (c.block_path, c.topology_id),
    )


def test_the_two_stage_opamp_deck_behaves_as_before():
    """추가 실측 (브리프에는 없었다): `miller_basic`의 본문은
    `benchmarks/two_stage_opamp`의 `OPAMP2STAGE` 본문과 컴포넌트 시퀀스가
    완전히 같다 - 이 벤치마크가 애초에 그 토폴로지에서 뽑아낸 것이기
    때문이다. identical_body 규칙 이전에는 "OPAMP2STAGE를 miller_basic으로
    교체"도 조용한 no-op 후보였다. 그래서 후보는 `miller_nulling_resistor`
    하나뿐이고, `miller_basic`은 identical_body로 기각된다."""
    from pathlib import Path

    from analogcoder.topologies import TOPOLOGY_LIBRARY

    text = Path("benchmarks/two_stage_opamp/netlist.cir").read_text()
    cands, rej = compatible_swaps({"ac": text}, TOPOLOGY_LIBRARY, set())
    assert cands == [SwapCandidate(block_path="OPAMP2STAGE", topology_id="miller_nulling_resistor")]
    assert {r.reason for r in rej if r.topology_id == "miller_basic" and r.block_path == "OPAMP2STAGE"} == {
        "identical_body"
    }


def test_a_candidate_whose_ports_are_a_subset_is_allowed():
    """9포트 블록(AMP) + 5포트 후보(five_port). 남는 네 포트(nbias/ncas/
    pbias/pcas)의 넷을 AMP의 두 인스턴스(Xu1/Xu2)가 서로 공유한다 - bandgap의
    바이어스 체인과 같은 모양. 부동 넷 검사를 통과하므로 후보가 된다."""
    cands, rej = compatible_swaps({"tb": DECK_9_SHARED_BIAS}, LIB, set())
    assert SwapCandidate(block_path="AMP", topology_id="five_port") in cands
    assert _reasons(rej, "five_port") == set()


def test_a_leftover_port_whose_net_has_no_other_user_is_rejected():
    """인스턴스(Xu1)는 하나 있지만, 남는 포트의 넷을 그 스코프의 다른 어떤
    소자도 참조하지 않는다 - 유일 참여자이므로 ports 사유로 거부된다."""
    cands, rej = compatible_swaps({"tb": DECK_9_LONE_INSTANCE}, LIB, set())
    assert SwapCandidate(block_path="AMP", topology_id="five_port") not in cands
    assert _reasons(rej, "five_port") == {"ports"}


def test_every_instance_must_pass_the_floating_check():
    """같은 정의(AMP)를 세 번 인스턴스화: Xu1a/Xu1b는 남는 포트의 넷을
    공유해 통과하지만 Xu2는 별도의 넷을 혼자 써서 실패한다. 인스턴스 하나라도
    실패하면 전체가 거부되어야 한다."""
    cands, rej = compatible_swaps({"tb": DECK_9_ONE_INSTANCE_FLOATS}, LIB, set())
    assert SwapCandidate(block_path="AMP", topology_id="five_port") not in cands
    assert _reasons(rej, "five_port") == {"ports"}


def test_a_candidate_requiring_a_port_the_block_lacks_is_still_rejected():
    """완화는 한 방향뿐이다 - 후보가 블록에 없는 포트를 요구하면(9포트 후보 vs
    5포트 블록) 여전히 거부된다."""
    cands, rej = compatible_swaps({"tb": DECK_5}, {"nine_port": NINE_PORT}, set())
    assert cands == []
    assert _reasons(rej, "nine_port") == {"ports"}


def test_a_definition_with_no_instance_and_leftover_ports_is_rejected():
    """AMP(9포트)는 이 덱 어디서도 인스턴스화되지 않는다. five_port(5포트)는
    포트 부분집합이라 남는 포트가 있지만, 인스턴스가 없어 부동 여부를 판정할
    수 없으므로 추측하지 않고 거부한다."""
    cands, rej = compatible_swaps({"tb": DECK_9}, LIB, set())
    assert SwapCandidate(block_path="AMP", topology_id="five_port") not in cands
    assert _reasons(rej, "five_port") == {"ports"}


def test_an_instance_with_fewer_nodes_than_declared_ports_is_rejected_not_crashed():
    """Xu1은 노드가 5개뿐인데(위치 인자가 모자란 잘못된 SPICE) AMP는 포트를
    9개 선언한다 - 남는 포트(nbias 등) 위치가 인스턴스 노드 목록 범위를
    벗어난다. 이 방어 분기가 없으면 IndexError로 죽는다; 대신 판정 불가로
    보고 거부한다."""
    cands, rej = compatible_swaps({"tb": DECK_9_SHORT_INSTANCE}, LIB, set())
    assert SwapCandidate(block_path="AMP", topology_id="five_port") not in cands
    assert _reasons(rej, "five_port") == {"ports"}


def test_the_port_subset_relaxation_admits_nothing_today():
    """이 완화는 규칙으로서 옳지만 오늘의 라이브러리/덱에서는 0쌍을 추가한다 -
    미리보기 리뷰가 잡은 결함(초판은 `cands`를 버리고 사유를 `topology_id`만
    으로 접어 6개 블록 중 마지막 하나만 봤다) 이후 다시 쓴 버전: `cands`에
    두 5포트 항목이 후보로 전혀 나오지 않는 것을 직접 확인하고, 기각 사유를
    (block_path, topology_id)별로 전부 고정한다.

    실측: `BANDGAP`/`BGR_CORE`는 `vinp`/`vinn`/`vout` 자체가 없어 포트
    부분집합 검사에서부터 걸려 `ports`. `ERRAMP`/`TRIMAMP`/`BUF_N`/`BUF_P`는
    9포트라 포트 부분집합 검사와 부동 넷 검사(bandgap의 바이어스 체인을
    같은 스코프의 형제 인스턴스들이 공유하므로 통과)를 모두 통과하고, 그
    다음 `models`에서 걸린다 - 5포트 항목 둘 다 `sky130_fd_pr__cap_mim_m3_1`을
    쓰는데 bandgap 덱은 MOS 캡만 쓰기 때문이다. 라이브러리나 덱이 바뀌어
    이 사실이 참이 아니게 되면 이 테스트가 깨지고, 그때 이 사실을 다시
    적어야 한다."""
    from pathlib import Path

    from analogcoder.topologies import TOPOLOGY_LIBRARY

    text = Path("benchmarks/bandgap/netlist_loops.cir").read_text()
    cands, rej = compatible_swaps({"loops": text}, TOPOLOGY_LIBRARY, set())
    five_port = {"miller_basic", "miller_nulling_resistor"}

    # 두 5포트 항목은 어떤 블록에 대해서도 후보가 아니다 - 이것이 이 테스트의
    # 이름이 주장하는 사실인데, 초판은 이걸 전혀 확인하지 않았다.
    assert not any(c.topology_id in five_port for c in cands)

    reasons = {
        (r.block_path, r.topology_id): r.reason
        for r in rej
        if r.topology_id in five_port
    }
    expected_blocks = {"BANDGAP", "BGR_CORE", "ERRAMP", "TRIMAMP", "BUF_N", "BUF_P"}
    assert {block for block, _ in reasons} == expected_blocks
    for topology_id in five_port:
        assert reasons[("BANDGAP", topology_id)] == "ports"
        assert reasons[("BGR_CORE", topology_id)] == "ports"
        for block in ("ERRAMP", "TRIMAMP", "BUF_N", "BUF_P"):
            assert reasons[(block, topology_id)] == "models"


# --- 포트 규칙이 실제로 무엇을 허용하는가 (감사 §3.4 / §6 첫 항목) -------------
#
# CLAUDE.md는 포트 규칙을 "bidirectional set equality"로 적고 단방향 검사를
# 결함으로 서술하지만, `bc53d9e`가 그것을 **의도적으로** 부분집합으로 완화했다.
# 아래 두 테스트는 그 완화가 실제로 무엇을 만드는지 - CLAUDE.md가 실패
# 시나리오로 적은 바로 그 모양("5포트 본문이 9포트 블록에 들어가 바이어스 포트
# 네 개가 남는다")이 오늘 도달 가능하다는 것 - 을 못박는다.


def test_a_five_port_body_really_does_land_in_a_nine_port_block():
    """CLAUDE.md가 결함으로 적은 시나리오를 스왑 적용까지 끝까지 재현한다.

    실측: 5포트 본문이 9포트 블록의 후보가 되고, 스왑 후 `.subckt` 헤더는
    여전히 9포트를 선언하며 새 본문은 그중 네 개(nbias/ncas/pbias/pcas)를
    안 쓴다. **다만 그 넷들은 뜨지 않는다** - 부동 넷 검사가 통과한 이유가
    곧 그것이다: 같은 스코프의 다른 인스턴스(Xu2)가 같은 넷을 참조한다.
    즉 CLAUDE.md의 시나리오는 도달 가능하되 결론(조용히 뜬 노드가 된다)이
    틀렸다 - 뜨는 갈래는 `_leftover_ports_float_reason`이 거부한다.
    """
    from analogcoder.netlist import apply_topology_swap, parse_netlist

    cands, _ = compatible_swaps({"tb": DECK_9_SHARED_BIAS}, LIB, set())
    assert SwapCandidate(block_path="AMP", topology_id="five_port") in cands

    swapped = apply_topology_swap(DECK_9_SHARED_BIAS, "AMP", FIVE_PORT.subckt_body)
    parsed = parse_netlist(swapped)
    sub = parsed.subckts["AMP"]

    # 헤더는 그대로 9포트를 선언한다 (apply_topology_swap은 본문만 바꾼다).
    assert sub.ports == ["vinp", "vinn", "vout", "vdd", "vss", "nbias", "ncas", "pbias", "pcas"]
    used_inside = {n for c in sub.components for n in c.nodes}
    assert [p for p in sub.ports if p not in used_inside] == ["nbias", "ncas", "pbias", "pcas"]

    # 그러나 호출부의 그 넷들은 형제 인스턴스가 여전히 참조한다 - 뜬 넷이 아니다.
    top_users: dict[str, int] = {}
    for comp in parsed.top_components:
        for net in comp.nodes:
            top_users[net] = top_users.get(net, 0) + 1
    for net in ("nbias", "ncas", "pbias", "pcas"):
        assert top_users[net] >= 2


def test_a_leftover_port_colliding_with_an_internal_node_of_the_new_body_is_rejected():
    """부동 넷 검사가 원리적으로 못 보는 반대 갈래: 남는 포트의 **이름**이
    새 본문의 내부 노드 이름과 겹치면, 스왑 후 그 노드는 내부 노드가 아니라
    헤더가 선언한 포트가 되어 호출부가 넘긴 외부 넷에 묶인다 - 뜨는 것이
    아니라 **단락**이다.

    이것은 가상의 형태가 아니다: 출하 라이브러리의 `miller_basic` /
    `miller_nulling_resistor` 본문은 내부 노드 `nbias`/`pbias`를 쓰고,
    bandgap의 네 앰프(9포트)의 남는 포트가 정확히 `nbias`/`ncas`/`pbias`/
    `pcas`다. 오늘은 `models` 규칙이 먼저 거부해서 도달하지 않을 뿐이다
    (아래 test_the_leftover_collision_check_is_silent_on_every_shipped_spec).
    """
    collide = Topology(
        id="collide", description="d", addresses=[],
        ports=["vinp", "vinn", "vout", "vdd", "vss"], assumes_scale=1e-6,
        provenance="authored", verified_at="nominal",
        # pbias는 이 본문의 **내부** 노드다 - ports에 없다.
        subckt_body="M1 pbias vinp vdd vss NMOSG W=2 L=1\nM2 vout vinn pbias vss NMOSG W=2 L=1\n",
    )
    cands, rej = compatible_swaps({"tb": DECK_9_SHARED_BIAS}, {"collide": collide}, set())
    assert cands == []
    assert _reasons(rej, "collide") == {"leftover_port_collision"}
    detail = next(r.detail for r in rej if r.topology_id == "collide")
    assert "pbias" in detail


def test_the_leftover_collision_check_is_silent_on_every_shipped_spec():
    """이 검사가 아무것도 안 할 때의 모양을 못박는다.

    실측(감사 재현): 오늘 출하 12개 스펙 전체에서 남는 포트를 가진
    (테스트벤치, 블록, 토폴로지) 삼중은 **256개**이고 그 **256개 전부**가
    이름 충돌을 갖는다 - 즉 `_leftover_ports_float_reason`은 한 번도 충돌
    없는 쌍 위에서 불린 적이 없다. 그런데도 `leftover_port_collision` 기각은
    **0건**인데, `models` 규칙이 먼저 거부하기 때문이다 (5포트 항목 둘 다
    `sky130_fd_pr__cap_mim_m3_1`을 쓰는데 bandgap 덱은 MOS 캡만 쓴다).

    그래서 충돌 검사는 `models`/`scale` **뒤에** 놓여 있다 - 앞에 놓으면
    오늘 256번 발화해서 "이 검사가 실제로 문제가 되는 쌍을 잡았다"와
    "어차피 models가 잡을 쌍 위에서 울렸다"가 구별되지 않는다. 순서를
    바꾸지 말 것. 라이브러리나 덱이 바뀌어 models가 통과하는 순간 이 검사가
    살아난다.
    """
    from pathlib import Path

    from analogcoder.netlist import parse_netlist
    from analogcoder.topologies import TOPOLOGY_LIBRARY
    from analogcoder.topology_match import _wrap_topology_body

    text = Path("benchmarks/bandgap/netlist_loops.cir").read_text()
    cands, rej = compatible_swaps({"loops": text}, TOPOLOGY_LIBRARY, set())
    assert [r for r in rej if r.reason == "leftover_port_collision"] == []

    # 충돌은 실재한다 - 오직 순서 때문에 안 보일 뿐이라는 것까지 못박는다.
    for topology_id in ("miller_basic", "miller_nulling_resistor"):
        topology = TOPOLOGY_LIBRARY[topology_id]
        body = _wrap_topology_body(topology).subckts["TMP"]
        internal = {n for c in body.components for n in c.nodes} - set(topology.ports)
        assert {"nbias", "pbias"} <= internal
        for block in ("ERRAMP", "TRIMAMP", "BUF_N", "BUF_P"):
            leftover = set(parse_netlist(text).subckts[block].ports) - set(topology.ports)
            assert internal & leftover == {"nbias", "pbias"}
            assert (
                next(
                    r.reason
                    for r in rej
                    if r.block_path == block and r.topology_id == topology_id
                )
                == "models"
            )
    assert not any(c.topology_id.startswith("miller_") for c in cands)
