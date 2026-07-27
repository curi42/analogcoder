import json
import os

import pytest

from analogcoder.patterns import find_patterns
from analogcoder.signal_path import build_signal_paths
from analogcoder.structure import derive_structure

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures", "structure_golden")

# test_netlist_golden.py와 같은 10개 덱. 목록이 줄어들면 커버리지가 조용히
# 사라지므로 개수도 함께 단언한다.
NETLISTS = [
    "benchmarks/bandgap/netlist.cir",
    "benchmarks/bandgap/netlist_startup.cir",
    "benchmarks/bandgap/netlist_psrr.cir",
    "benchmarks/bandgap/netlist_settling.cir",
    "benchmarks/bandgap/netlist_loops.cir",
    "benchmarks/two_stage_opamp/netlist.cir",
    "benchmarks/two_stage_opamp/netlist_psr_plus.cir",
    "benchmarks/two_stage_opamp/netlist_psr_minus.cir",
    "benchmarks/two_stage_opamp/netlist_settling.cir",
    "benchmarks/inverting_amp/netlist.cir",
]


def _snapshot(text: str) -> dict:
    structure = derive_structure(text, "golden")
    paths = build_signal_paths(structure)
    return {
        "blocks": {
            (path or "<top>"): {
                "ports": block.ports,
                "instance_count": block.instance_count,
                "components": [
                    {
                        "refdes": f.refdes,
                        "ctype": f.ctype,
                        "device_class": f.device_class,
                        "model": f.model,
                        "nodes": f.nodes,
                        "terminals": [[t.name, t.role] for t in f.terminals],
                    }
                    for f in block.components
                ],
            }
            for path, block in sorted(structure.blocks.items(), key=lambda kv: kv[0] or "")
        },
        "tunable": sorted([e.refdes, e.param] for e in structure.tunable),
        "net_blocks": {
            net: {name: sorted(roles) for name, roles in sorted(b.items())}
            for net, b in sorted(paths.net_blocks.items())
        },
        "supply_nets": sorted(paths.supply_nets),
        "mismatches": sorted(e.mismatch for e in paths.instances if e.mismatch),
        "patterns": sorted(
            [m.kind, m.block or "<top>", list(m.members)] for m in find_patterns(structure)
        ),
    }


def _golden_path(rel: str) -> str:
    return os.path.join(GOLDEN_DIR, rel.replace("/", "__") + ".json")


def test_the_golden_set_covers_every_benchmark_netlist():
    assert len(NETLISTS) == 10
    for rel in NETLISTS:
        assert os.path.exists(os.path.join(REPO, rel)), rel


@pytest.mark.parametrize("rel", NETLISTS)
def test_derived_structure_matches_the_golden_snapshot(rel):
    with open(os.path.join(REPO, rel)) as f:
        actual = _snapshot(f.read())
    with open(_golden_path(rel)) as f:
        expected = json.load(f)

    assert actual == expected, (
        f"{rel}의 파생 결과가 골든 스냅샷과 다르다. 의도한 변경이라면 스냅샷을 "
        f"다시 만들되, 무엇이 왜 바뀌었는지 커밋 메시지에 적을 것."
    )


@pytest.mark.parametrize("rel", NETLISTS)
def test_derivation_is_reproducible(rel):
    # analyzer는 같은 bandgap 넷리스트에 대해 component_roles를 93개, 26개,
    # 1개로 냈다. 이 테스트가 그것과 대비되는 지점이며, E2가 존재하는 이유다.
    with open(os.path.join(REPO, rel)) as f:
        text = f.read()

    assert _snapshot(text) == _snapshot(text)
