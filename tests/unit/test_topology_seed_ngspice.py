"""Task 6 proof: a seeded bandgap deck whose one failing criterion
(buf0_loop_gain) is fixable ONLY by a topology swap, verified against real
ngspice with no LLM in the loop.

benchmarks/bandgap/netlist_seed_topology.cir is netlist_loops.cir with
exactly one change: BUF_P's body replaced by BUF_N's NMOS-input-fold body
(see that file's header comment for the measured rationale - vt05, the node
BUF_P buffers, sits at ~0.5V, below what an NMOS input pair can reach).
"""

import os
import shutil

import pytest

from analogcoder.netlist import apply_topology_swap, resolve_includes
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.topologies import TOPOLOGY_LIBRARY
from analogcoder.topology_match import SwapCandidate, compatible_swaps

BENCH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "bandgap"))

pytestmark = pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not on PATH")


def _measure(netlist_text: str, dest, control_block: str) -> dict:
    """Writes netlist_text to `dest` (a tmp_path file) and runs it through
    real ngspice with `control_block` appended, returning the parsed
    `meas`/`let` measurements.

    `.include "pdk_corner.inc"` in this deck is a path relative to
    benchmarks/bandgap - writing the text to tmp_path unmodified would leave
    that include unresolved (NgspiceBackend sets cwd to the directory of the
    ORIGINAL netlist_path, which would be tmp_path here, not
    benchmarks/bandgap). resolve_includes rewrites the top-level .include to
    an absolute path first, matching test_bandgap_benchmark_ngspice.py's
    existing pattern.
    """
    text = resolve_includes(netlist_text, BENCH)
    dest.write_text(text)
    return NgspiceBackend(timeout=180).run(str(dest), {"control_block": control_block}).measurements


def test_the_seeded_deck_fails_buf0_loop_gain_and_the_swap_fixes_it(tmp_path):
    spec = load_spec(os.path.join(BENCH, "spec_seed_topology.yaml"))
    control_block = spec.canonical.control_block

    with open(os.path.join(BENCH, "netlist_seed_topology.cir")) as f:
        seeded = f.read()

    before = _measure(seeded, tmp_path / "before.cir", control_block)["buf0_gain_db"]
    assert before < 90.0

    fixed = apply_topology_swap(seeded, "BUF_P", TOPOLOGY_LIBRARY["folded_cascode_pmos_in_cs"].subckt_body)
    after = _measure(fixed, tmp_path / "after.cir", control_block)["buf0_gain_db"]
    assert after >= 90.0


def test_the_seeded_deck_offers_the_fixing_swap_among_exactly_these_candidates():
    """이 이름이 주장할 수 있는 것은 멤버십이 아니라 **집합 전체**다.

    `compatible_swaps`는 CLAUDE.md가 명시적으로 게이트가 아니라 **후보
    생성기**로 규정한 것이므로, 고치는 스왑 하나만 내놓는 것이 옳은 동작이
    아니다 - 내놓아야 하는 것은 "실제로 적용 가능한 쌍 전부"다. 그래서
    멤버십만 단언하면 이 테스트는 후보가 하나든 서른이든, 고치는 쌍이 어떤
    이웃과 함께 있든 통과한다. 실제로는 7개이고 그중 하나는 이름만 그럴듯한
    미끼다(아래 테스트가 실측으로 고정한다).

    집합 전체를 고정하면 세 규칙 중 무엇이 무너져도 여기서 걸린다: 포트
    양방향 동일성이 느슨해지면 쌍이 늘고, `identical_body`가 깨지면
    `(TRIMAMP, nmos)`·`(BUF_P는 이미 nmos 본문)` 같은 무변화 쌍이 들어오며,
    모델 규칙이 무너져도 마찬가지다."""
    with open(os.path.join(BENCH, "netlist_seed_topology.cir")) as f:
        seeded = f.read()

    cands, _rejections = compatible_swaps({"loops": seeded}, TOPOLOGY_LIBRARY, set())

    assert SwapCandidate("BUF_P", "folded_cascode_pmos_in_cs") in cands
    assert set(cands) == {
        SwapCandidate("BUF_N", "folded_cascode_nmos_in_cs"),
        SwapCandidate("BUF_N", "folded_cascode_pmos_in_cs"),
        SwapCandidate("BUF_P", "folded_cascode_nmos_in_cs"),
        SwapCandidate("BUF_P", "folded_cascode_pmos_in_cs"),
        SwapCandidate("ERRAMP", "folded_cascode_nmos_in_cs"),
        SwapCandidate("ERRAMP", "folded_cascode_pmos_in_cs"),
        SwapCandidate("TRIMAMP", "folded_cascode_pmos_in_cs"),
    }
    # 두 밀러 항목은 이 덱의 어느 블록과도 포트가 맞지 않는다 - 후보 목록이
    # 라이브러리 전체가 아니라는 사실 자체가 여기서 고정된다.
    assert not any(c.topology_id.startswith("miller") for c in cands)


def test_the_look_alike_candidate_on_the_same_block_does_not_fix_it(tmp_path):
    """후보 목록에는 `(BUF_P, folded_cascode_nmos_in_cs)`도 들어 있고, 그것은
    **고치지 않는다** - 씨앗 덱의 BUF_P가 이미 NMOS 입력 폴드를 들고 있고,
    그것이 바로 실패의 원인이기 때문이다.

    실측(이 테스트가 재는 것): 미스왑 **73.515 dB**, pmos 스왑 **100.158**,
    nmos 스왑 **73.517** - 즉 이름이 비슷한 쪽으로 스왑하면 한 이터레이션을
    쓰고 아무것도 얻지 못한다. `identical_body` 규칙이 이 미끼를 못 거르는
    것은 결함이 아니라 사실이다: 두 본문은 `Xcl`/`Xcc`/`XRz` 크기에서 텍스트로
    다르고(라이브러리 항목은 TRIMAMP에서 나왔다), 그 규칙은 텍스트 동일성만
    판정한다.

    이 사실이 어디에도 기록돼 있지 않으면, 위 테스트의 멤버십 단언을 읽는
    다음 사람은 "후보에 있다"를 "고쳐진다"로 읽는다."""
    spec = load_spec(os.path.join(BENCH, "spec_seed_topology.yaml"))
    control_block = spec.canonical.control_block

    with open(os.path.join(BENCH, "netlist_seed_topology.cir")) as f:
        seeded = f.read()

    decoy = apply_topology_swap(
        seeded, "BUF_P", TOPOLOGY_LIBRARY["folded_cascode_nmos_in_cs"].subckt_body
    )
    after = _measure(decoy, tmp_path / "decoy.cir", control_block)["buf0_gain_db"]

    # 기준(90 dB)을 못 넘는다 - 그리고 미스왑(73.515)에서 사실상 움직이지도
    # 않는다. 절대 임계만 걸면 "본문이 통째로 안 바뀌었다"는 변형도 통과한다.
    assert after < 90.0
    assert abs(after - 73.5171) < 0.5
