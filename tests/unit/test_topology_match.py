from analogcoder.topologies import Topology
from analogcoder.topology_match import SwapCandidate, compatible_swaps

FIVE_PORT = Topology(
    id="five_port", description="d", addresses=[],
    ports=["vinp", "vinn", "vout", "vdd", "vss"], assumes_scale=1e-6,
    subckt_body="M1 vout vinp vdd vss NMOSG W=2 L=1\nM2 vout vinn vss vss NMOSG W=2 L=1\n",
)
NINE_PORT = Topology(
    id="nine_port", description="d", addresses=[],
    ports=["vinp", "vinn", "vout", "vdd", "vss", "nbias", "ncas", "pbias", "pcas"],
    assumes_scale=1e-6,
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
    cands, _ = compatible_swaps({"tb": two_blocks}, LIB, {("AMP", "nine_port")})
    assert cands == [SwapCandidate(block_path="AMP2", topology_id="nine_port")]


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
    # 5포트 항목은 전부 포트 사유로 기각되어야 한다
    assert {r.reason for r in rej if r.topology_id == "miller_basic"} == {"ports"}
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
