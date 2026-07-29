"""조합 경로가 실제 ngspice에서 **정말로 코너를 바꾼다**는 증거.

`re.sub`가 0건 매치로 조용히 같은 덱을 내던 것이 이 저장소가 값을 치른
자리다(`netlist_startup.cir`의 45코너가 실은 15조건). 슬롯 채우기가 그
실패를 구조적으로 없앤다는 주장은, 두 코너가 **서로 다른 측정값**을 낸다는
것으로만 증명된다."""

import pathlib

import yaml

from analogcoder.pvt import run_full_pvt_sweep
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec

SIGNALS = """* 신호 선언부 조각 - 자극과 공급
Vin in 0 AC 1
"""

CORE = """* 넷리스트 조각 - 튜너가 고치는 유일한 조각
Rin in vminus 1k
Rf vminus vout {rf}
Eopamp vout 0 0 vminus 100k
.end
"""

CORNER_NOM = ".param rf=10k\n"
CORNER_HI = ".param rf=20k\n"

CONTROL = """.control
ac dec 10 1 1G
let g = vdb(vout)
meas ac gain_db find g at=1k
.endc"""


def _spec_dir(tmp_path):
    (tmp_path / "signals.cir").write_text(SIGNALS)
    (tmp_path / "core.cir").write_text(CORE)
    (tmp_path / "corner_nom.inc").write_text(CORNER_NOM)
    (tmp_path / "corner_hi.inc").write_text(CORNER_HI)
    raw = {
        "circuit_name": "composed_inverting_amp",
        "testbenches": [
            {
                "name": "ac",
                "compose": [
                    {"file": "signals.cir"},
                    {"corner_slot": True},
                    {"file": "core.cir", "tunable": True},
                ],
                "analyses": ["ac"],
                "control_block": CONTROL,
                "criteria": [
                    {
                        "name": "gain",
                        "measurement": "gain_db",
                        "operator": ">=",
                        "threshold": 19.0,
                    }
                ],
            }
        ],
        "pvt_corners": {
            "nominal": "nom",
            "corners": [
                {"id": "nom", "include": "corner_nom.inc"},
                {"id": "hi", "include": "corner_hi.inc"},
            ],
        },
    }
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(raw))
    return str(path)


def test_a_composed_sweep_really_runs_each_corner_as_its_own_circuit(tmp_path):
    spec = load_spec(_spec_dir(tmp_path))
    texts = {tb.name: pathlib.Path(tb.netlist_path).read_text() for tb in spec.testbenches}
    events = []

    sweep = run_full_pvt_sweep(
        texts, spec, NgspiceBackend(), log_event=lambda name, payload: events.append((name, payload))
    )

    values = {entry["corner"]["corner_id"]: entry["measurements"]["gain_db"] for entry in sweep["per_corner"]}
    # Rf 10k -> 20 dB, Rf 20k -> 26.02 dB. 두 코너가 같은 값을 내면 슬롯이
    # 실제로는 채워지지 않았다는 뜻이다.
    assert round(values["nom"], 2) == 20.0
    assert round(values["hi"], 2) == 26.02


def test_the_composed_sweep_records_what_the_composition_did(tmp_path):
    """조합 경로에도 `corner_render` 사건이 테스트벤치마다 한 번, 무조건
    남는다 - 없으면 "조합했고 멀쩡했다"와 "조합 경로가 사라졌다"가 같아진다."""
    spec = load_spec(_spec_dir(tmp_path))
    texts = {tb.name: pathlib.Path(tb.netlist_path).read_text() for tb in spec.testbenches}
    events = []

    run_full_pvt_sweep(
        texts, spec, NgspiceBackend(), log_event=lambda name, payload: events.append((name, payload))
    )

    renders = [payload for name, payload in events if name == "corner_render"]
    assert len(renders) == 1
    assert renders[0]["mode"] == "composed"
    assert renders[0]["states"]["corner_slot_filled"] == 1
    assert renders[0]["states"]["title_inserted"] == 1


def test_the_tuned_fragment_is_what_reaches_the_simulator(tmp_path):
    """버전 스택은 조각을 들고, 조합은 시뮬레이션 직전에 일어난다."""
    spec = load_spec(_spec_dir(tmp_path))
    tuned = CORE.replace("Rin in vminus 1k", "Rin in vminus 2k")

    sweep = run_full_pvt_sweep({"ac": tuned}, spec, NgspiceBackend())

    values = {entry["corner"]["corner_id"]: entry["measurements"]["gain_db"] for entry in sweep["per_corner"]}
    # Rin 2k, Rf 10k -> 13.98 dB
    assert round(values["nom"], 2) == 13.98
