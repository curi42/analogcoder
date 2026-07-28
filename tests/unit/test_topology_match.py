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
    cands, rej = compatible_swaps({"tb": DECK_9}, LIB, set())
    assert cands == [SwapCandidate(block_path="AMP", topology_id="nine_port")]
    assert _reasons(rej, "five_port") == {"ports"}


def test_a_nine_port_topology_is_rejected_for_a_five_port_block():
    """양방향 확인 - 한 방향만 보는 구현은 여기서 걸린다."""
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
