#!/usr/bin/env python3
"""덱의 DC 해가 유일한지 잰다. 솔버를 서로 다른 초기 추정으로 밀어 본다.

**왜 이 도구가 존재하는가.** `benchmarks/two_stage_opamp/netlist.cir` 의 자기
바이어스 체인이 **안정한 DC 해를 둘** 갖는다는 것이 2026-07-30 에 밝혀졌다
(`docs/superpowers/specs/2026-07-30-two-stage-opamp-bistable-bias.md`). 두 상태의
UGBW 가 13배 다르고, `vout` 은 양쪽 다 0.55 V 라 **DC 출력으로는 구별되지
않는다.** 그리고 45코너 스윕에서 소자 크기를 건드리지 않았는데도 127개 측정값이
상태에 따라 달라졌다 - 코너를 바꾸는 것만으로 뒤집힌다.

그 발견은 즉시 더 큰 질문을 만든다: **`benchmarks/bandgap` 도 그런가.** 이
저장소의 거의 모든 측정이 그 덱에서 나왔다. 이 스크립트가 그 질문에 답한다.

**방법.** `.nodeset` 으로 바이어스 체인의 초기 추정을 여러 방향으로 밀고(꺼진
쪽, 켜진 쪽, 중간), 전부 같은 해로 오는지 본다. `.nodeset` 은 뉴턴 반복의 초기
추정일 뿐이므로 최종 해는 항상 진짜 DC 해다 - 답을 강제하는 것이 아니라 어느
분지로 갈지를 유도한다.

크기 교란 몇 개에서도 반복한다. `two_stage_opamp` 의 전환은 **고립된 크기**에서
일어났으므로(W=5.999999 정상 / 6.0 이상 / 6.000001 정상) 출하 크기만 보면 놓친다.

**이 스크립트를 쓰다 밟은 함정 셋. 전부 "측정이 조용히 아무것도 재지 않는"
모양이고, 이 저장소가 열두 번 지불한 부류다.**

1. **`.end` 뒤에 컨트롤 블록을 붙이면 ngspice 가 통째로 무시한다.** `print` 가
   아무것도 내지 않는데 종료 코드는 0 이다.
2. **`print v(a) v(b) v(c)` 는 이름 하나가 틀리면 그 줄 전체를 버린다.** 멀쩡한
   프로브까지 사라지고, 표에서는 "회로가 안 돈다" 와 구별되지 않는다. 그래서
   프로브를 **한 줄씩** 낸다.
3. **`.nodeset` 줄을 출력에서 되읽으면 내가 넣은 값을 측정값으로 보고한다.**
   1·2 때문에 `print` 가 죽은 상태에서 정규식이 덱 반향을 잡아 "다섯 해가 전부
   다르다"는 결과를 만들었다. 값이 내가 넣은 값과 **정확히** 같았던 것이 단서다.
4. **공급이 시간 의존 소스인 덱에서는 `op` 자체가 뜻이 없다.** `netlist_startup.cir`
   의 공급은 PWL 램프이고 `op` 는 그것을 **t=0**, 즉 **0 V** 에서 평가한다. 이
   스크립트는 거기서 "다중 해가 있다"를 **네 행에 걸쳐 보고했고**, 같은 덱을
   단독으로 다시 돌리면 전부 무효가 나왔다 - 재현조차 안 되는 숫자였다. 지금은
   `_refuse_reason` 이 그 덱을 **거부**하고, 거부는 요약에서 무효와 **다른 칸**에
   적힌다.

그래서 이 스크립트는 프로브가 안 나온 것을 **세어서 보고하고**, 한 줄의 프로브가
전부 없으면 그 줄을 "같은 해"가 아니라 **무효**로 적는다. 데이터가 없는 것을
일치로 읽는 것이 이 부류의 실수다.

사용:

    .venv/bin/python scripts/dc_solution_uniqueness.py            # 밴드갭 덱 전부
    .venv/bin/python scripts/dc_solution_uniqueness.py <덱경로>    # 하나만
"""

import os
import re
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(REPO, "src"))

from analogcoder.netlist import apply_changes, parse_netlist, resolve_includes  # noqa: E402

_BG = os.path.join(REPO, "benchmarks", "bandgap")

# **밴드갭의 모든 테스트벤치 덱.** 하나만 재고 "밴드갭은 유일하다"고 적으면 그
# 주장이 실제로 재지 않은 덱까지 덮는다. 여섯 덱이 같은 `BANDGAP`/`BGR_CORE`
# 본체를 공유하므로 유일성 논증이 이어질 것으로 **예상**되지만, 예상은 측정이
# 아니다 - 그리고 `netlist_seed_topology.cir` 은 실제로 다르다(BUF_P 의 본체가
# BUF_N 의 것으로 바뀐, 굶은 NMOS 폴드).
DECKS = [
    os.path.join(_BG, name) for name in (
        "netlist.cir",
        "netlist_loops.cir",
        "netlist_psrr.cir",
        "netlist_settling.cir",
        "netlist_startup.cir",
        "netlist_seed_topology.cir",
    )
]

# 바이어스 체인의 노드들. `BANDGAP` 본체 안에서 nbias/ncas/pbias/pcas 가 네
# 증폭기 **전부**에 분배되므로 여기가 갈리면 회로 전체가 갈린다. 최상위
# 인스턴스는 `Xdut` 이고 이 네 넷은 `BANDGAP` 의 포트가 아니라 내부 넷이므로
# `xdut.<net>` 으로 접근한다.
PROBES = [
    "v(xdut.nbias)", "v(xdut.ncas)", "v(xdut.pbias)", "v(xdut.pcas)",
    "v(vbg1)", "v(vbg0)",
]

# 초기 추정 다섯 가지. `none` 은 힌트 없음, 즉 출하 상태에서 솔버가 스스로 가는
# 곳이다. `off` 는 바이어스가 완전히 꺼진 쪽으로 미는 것이고, 그것이 이
# 회로에서 실재하는 축퇴 상태다 - CLAUDE.md 가 "a cascoded amp with a CS output
# stage can latch itself off" 로 적어 둔 것.
NUDGES = {
    "none": "",
    "low": ".nodeset v(xdut.nbias)=0.2 v(xdut.pbias)=1.6\n",
    "high": ".nodeset v(xdut.nbias)=1.2 v(xdut.pbias)=0.4\n",
    "off": ".nodeset v(xdut.nbias)=0 v(xdut.pbias)=1.8\n",
    "mid": ".nodeset v(xdut.nbias)=0.9 v(xdut.pbias)=0.9\n",
}

# 크기 교란. 출하 상태 + `perturbations.py` 가 소유한 모양 중 셋 + 시동 소자
# 축소. 마지막 것은 일부러 넣었다 - `BGR_CORE.Xsu_b` 는 바이어스 체인에 트리클을
# 흘려 분지를 하나로 만드는 소자이고, 그것을 줄이면 무슨 일이 나는지가 이
# 측정의 대조군이다.
SIZES = {
    "shipped": [],
    "tail_both_3": [
        {"refdes": "TRIMAMP.Xt", "param": "W", "new_value": "3"},
        {"refdes": "BUF_P.Xt", "param": "W", "new_value": "3"},
    ],
    "tail_trim_3": [{"refdes": "TRIMAMP.Xt", "param": "W", "new_value": "3"}],
    "cc_trim_20": [{"refdes": "TRIMAMP.Xcc", "param": "W", "new_value": "20"}],
    "su_b_small": [{"refdes": "BGR_CORE.Xsu_b", "param": "W", "new_value": "0.2"}],
}

# `curation.COMPARISON_REL_TOLERANCE` 와 같은 값이고 같은 이유다 - 이 저장소가
# 실측 잡음(최대 4.2e-5)과 진짜 신호(0.102) 사이에서 유도한 상수이므로 새 비율을
# 고르지 않는다.
REL_TOLERANCE = 1e-3


def _run(text: str, nudge: str) -> dict:
    # 프로브를 **한 줄씩** 낸다 - 위 함정 2.
    ctrl = "\n.control\nop\n" + "".join(f"print {pr}\n" for pr in PROBES) + "quit\n.endc\n"
    lines = text.rstrip().splitlines()
    # 컨트롤 블록은 `.end` **앞에** - 위 함정 1.
    end_at = next(
        (i for i in range(len(lines) - 1, -1, -1) if lines[i].strip().lower() == ".end"),
        None,
    )
    if end_at is None:
        raise RuntimeError("덱에 .end 가 없다 - 삽입 위치를 추측하지 않는다")
    deck = "\n".join(lines[:end_at]) + "\n" + nudge + ctrl + ".end\n"

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "d.cir")
        with open(path, "w") as f:
            f.write(deck)
        proc = subprocess.run(
            ["ngspice", "-b", path], capture_output=True, text=True, timeout=600, cwd=tmp
        )
    # 내가 넣은 `.nodeset` 줄을 명시적으로 뺀다 - 위 함정 3.
    out = "\n".join(
        ln for ln in (proc.stdout + proc.stderr).splitlines()
        if not ln.strip().lower().startswith(".nodeset")
    )
    vals = {}
    for probe in PROBES:
        m = re.search(re.escape(probe) + r"\s*=\s*([-+0-9.eE]+)", out)
        if m:
            vals[probe] = float(m.group(1))
    return vals


# SPICE 의 시간 의존 소스 함수들. `op` 는 이들을 **t=0 에서** 평가한다.
_TIME_DEPENDENT = ("pwl", "pulse", "sin", "sine", "exp", "sffm", "am", "trnoise", "trrandom")


def _refuse_reason(text: str) -> str | None:
    """`op` 기반 유일성 측정이 이 덱에서 뜻을 갖는가. 아니면 이유를 돌려준다.

    **이것을 넣은 이유가 이 스크립트의 네 번째 조용한 실패다.**
    `benchmarks/bandgap/netlist_startup.cir` 의 공급은 `Vdd vdd 0 PWL(0 0 100n
    1.8 1 1.8)` 이고, `op` 는 PWL 을 **t=0** 에서 평가하므로 그 덱의 동작점은
    **공급이 0 V 인 죽은 회로**다. 그 상태는 회로가 실제로 쓰는 상태가 아니다 -
    그 테스트벤치는 과도 해석을 위해 존재한다. 그런데 이 스크립트는 거기서
    "다중 해가 있다"를 **네 행에 걸쳐 보고했고**, 같은 덱을 단독으로 다시 돌리면
    전부 무효가 나왔다. 재현되지도 않는 숫자를 발견으로 적는 것이 이 저장소가
    반복해서 지불한 것이다.

    **이름으로 공급을 알아보지 않는다** - 그것은 이 저장소가 금지한 추측이다
    (`^Vdd` 로 레일을 알아보는 것과 같은 자리). 판정하는 사실은 "최상위 독립
    전압원 중 시간 의존 함수를 쓰는 것이 있는가" 이고, 그것은 파싱으로 결정된다.

    **그래서 이 거부는 일부러 넓다, 그리고 그 값을 명시한다.** 시간 의존 소스가
    **공급**이면 `op` 는 무의미하지만, 그것이 **자극**(계단 입력 등)이고 공급은
    DC 라면 `op` 는 계단 이전의 정당한 상태다. 둘을 가르려면 "어느 소스가
    공급인가" 를 알아야 하고 그것이 바로 금지된 추측이다. 그래서 닫히는 쪽으로
    실패한다 - 비용은 실측 하나를 버리는 것이고(`netlist_settling.cir` 이 그
    경우다: 거부 이전에 돌렸을 때 다섯 초기추정 전부 일치했다), 그 비용을
    **거부** 칸에 적어 무효와 구별한다. 되살리려면 공급을 파싱으로 식별하는
    방법이 먼저 필요하고, 그것은 코너 모델 일반화 작업과 같은 문제다.
    """
    parsed = parse_netlist(text)
    offenders = []
    for comp in parsed.top_components:
        if comp.ctype not in ("V", "I"):
            continue
        # 값 토큰 전체를 본다 - `DC 0 AC 1 PWL(...)` 처럼 섞일 수 있다.
        blob = " ".join([comp.value or ""] + list(comp.nodes[2:])).lower()
        if any(fn + "(" in blob.replace(" (", "(") for fn in _TIME_DEPENDENT):
            offenders.append(comp.refdes)
    if offenders:
        return (
            f"최상위 소스 {', '.join(offenders)} 가 시간 의존 함수를 쓴다. "
            f"`op` 는 그것을 t=0 에서 평가하므로 이 덱의 동작점은 회로가 실제로 "
            f"쓰는 상태가 아니다 - 이 측정은 여기서 뜻을 갖지 않는다"
        )
    return None


def _one_deck(deck_path: str) -> tuple[list, list, list]:
    base = resolve_includes(open(deck_path).read(), os.path.dirname(deck_path))
    print(f"###### 덱: {os.path.relpath(deck_path, REPO)}\n")
    reason = _refuse_reason(base)
    if reason is not None:
        print(f"  거부: {reason}\n")
        return None

    split_rows, void_rows, agree_rows = [], [], []
    for size_name, changes in SIZES.items():
        text = apply_changes(base, changes) if changes else base
        rows = {n: _run(text, nudge) for n, nudge in NUDGES.items()}

        print(f"=== {size_name}")
        print(f"  {'probe':<18}" + "".join(f"{n:>14}" for n in NUDGES))
        split_here, present = [], set()
        for probe in PROBES:
            line = f"  {probe:<18}"
            got = []
            for n in NUDGES:
                v = rows[n].get(probe)
                if v is not None:
                    got.append(v)
                    present.add(probe)
                line += f"{v:>14.6g}" if v is not None else f"{'-':>14}"
            if len(got) >= 2:
                scale = max(abs(v) for v in got) or 1.0
                rel = (max(got) - min(got)) / scale
                if rel > REL_TOLERANCE:
                    split_here.append((probe, rel))
                    line += "   <== 갈린다"
            print(line)

        gone = [pr for pr in PROBES if pr not in present]
        if gone:
            print(f"  !! 값이 안 나온 프로브 {len(gone)}개: {', '.join(gone)}"
                  f" — 그 프로브에 대해서는 아무 결론도 낼 수 없다")
        if split_here:
            split_rows.append(size_name)
            print("  ** 다중 해: "
                  + ", ".join(f"{p} ({r:.3g} 상대차)" for p, r in split_here))
        elif not present:
            # **데이터가 없는 것을 "일치"로 읽지 않는다.**
            void_rows.append(size_name)
            print("  (무효 — 프로브가 하나도 안 나왔다. 이 크기에서는 판정 불가)")
        else:
            agree_rows.append(size_name)
            print(f"  (초기추정 {len(NUDGES)}종 전부 같은 해 — 이 크기에서 유일하다는 증거)")
        print()

    print(f"  요약: 다중 해 {len(split_rows)}행 / 일치 {len(agree_rows)}행 / "
          f"무효 {len(void_rows)}행")
    if split_rows:
        print(f"  **다중 해가 있다**: {', '.join(split_rows)}")
    if void_rows:
        print(f"  판정 불가(해가 아예 안 나옴): {', '.join(void_rows)}")
    print()
    return split_rows, void_rows, agree_rows


def main() -> int:
    decks = sys.argv[1:] or DECKS
    print(f"초기추정 {len(NUDGES)}종 x 크기 {len(SIZES)}종 x 덱 {len(decks)}개, "
          f"허용오차 {REL_TOLERANCE:g} 상대\n")

    totals = {}
    for deck in decks:
        totals[os.path.basename(deck)] = _one_deck(deck)

    print("=" * 72)
    # **거부는 무효와 다른 사실이다.** "이 측정이 여기서 뜻을 갖지 않는다"와
    # "재 봤는데 값이 안 나왔다"를 같은 칸에 적으면, 앞의 것이 뒤의 것처럼
    # 읽히면서 덱에 문제가 있다는 인상을 남긴다.
    refused = [d for d, r in totals.items() if r is None]
    scored = {d: r for d, r in totals.items() if r is not None}
    any_split = [d for d, (s, _, _) in scored.items() if s]
    all_void = [d for d, (s, v, a) in scored.items() if not a and not s]
    agreed = [d for d, (s, _, a) in scored.items() if a and not s]
    print(f"전체 판정: 덱 {len(totals)}개 = 측정 {len(scored)}개 + 거부 {len(refused)}개")
    print(f"  측정한 것 중: 다중 해 {len(any_split)} / 일치 {len(agreed)} / "
          f"전부무효 {len(all_void)}")
    for deck, r in totals.items():
        if r is None:
            print(f"  {deck:<32} 거부   (op 가 뜻을 갖지 않는 덱)")
            continue
        s, v, a = r
        mark = "다중해" if s else ("무효" if not a else "일치")
        print(f"  {deck:<32} {mark:<6} (다중 {len(s)} / 일치 {len(a)} / 무효 {len(v)})")
    if any_split:
        print(f"\n  **다중 해가 있는 덱**: {', '.join(any_split)}")
    elif agreed:
        print(f"""
  {len(agreed)}개 덱 전부에서, 바이어스를 0 V 까지 밀어도 같은 해로 온다.
  이것은 "유일함이 증명됐다"가 아니다 - {len(NUDGES)}개 방향으로 밀어서 안
  갈렸다는 것이고, `two_stage_opamp` 의 전환이 고립된 크기에서 일어난 것을 보면
  촘촘한 크기 훑기가 더 강한 증거다. 그러나 축퇴 상태(바이어스 꺼짐) 쪽으로
  명시적으로 밀어도 돌아오는 것은, `BGR_CORE.Xsu_b` 트리클이 실제로 분지를
  하나로 만든다는 뜻이다 - CLAUDE.md 가 그 소자를 넣은 이유로 적은 것과 일치한다.""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
