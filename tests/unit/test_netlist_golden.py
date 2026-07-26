import json
import os

import pytest

from analogcoder.netlist import parse_netlist

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures", "netlist_golden")

# 10개 벤치마크 넷리스트. 이 목록이 줄어들면 커버리지가 조용히 사라지므로
# 개수도 함께 단언한다.
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


def _component_dict(component) -> dict:
    return {
        "refdes": component.refdes,
        "ctype": component.ctype,
        "nodes": component.nodes,
        "value": component.value,
        "params": dict(sorted(component.params.items())),
        "scope": component.scope,
        "geometry_scale": component.geometry_scale,
    }


def _snapshot(text: str) -> dict:
    parsed = parse_netlist(text)
    return {
        "top_components": [_component_dict(c) for c in parsed.top_components],
        "subckts": {
            key: {
                "ports": subckt.ports,
                "components": [_component_dict(c) for c in subckt.components],
            }
            for key, subckt in sorted(parsed.subckts.items())
        },
    }


def _golden_path(rel_netlist: str) -> str:
    return os.path.join(GOLDEN_DIR, rel_netlist.replace("/", "__") + ".json")


def test_the_golden_set_covers_every_benchmark_netlist():
    # 목록이 조용히 줄어들면 이 파일 전체가 무의미해진다.
    assert len(NETLISTS) == 10
    for rel in NETLISTS:
        assert os.path.exists(os.path.join(REPO, rel)), rel


@pytest.mark.parametrize("rel", NETLISTS)
def test_parse_result_matches_the_golden_snapshot(rel):
    with open(os.path.join(REPO, rel)) as f:
        actual = _snapshot(f.read())
    with open(_golden_path(rel)) as f:
        expected = json.load(f)

    assert actual == expected, (
        f"{rel}의 파스 결과가 골든 스냅샷과 다르다. 의도한 변경이라면 "
        f"스냅샷을 다시 만들되, 무엇이 왜 바뀌었는지 커밋 메시지에 적을 것."
    )
