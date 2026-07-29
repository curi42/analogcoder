"""교란 모양의 **단일 소유자**. `coverage_feasibility.py` 와
`reentry_feasibility.py` 가 같은 목록을 쓴다.

왜 공유하는가: 두 스크립트가 각자 목록을 들면 "필요조건 1을 재진입이 발화한
바로 그 덱 상태에서 쟀는가"를 나중에 아무도 확인할 수 없다. 이름이 같은 두
목록이 갈라지는 것은 이 저장소가 `compose.py` 에서 비싸게 배운 실패
모양이다(`netlist.py` 의 파싱 규칙을 손으로 베껴 두 방향으로 갈라졌다).

**한 종류의 교란으로만 잰 것이 명시된 한계였다**
(`docs/superpowers/specs/2026-07-29-theory-combination-results.md` §7-8).
그래서 이 목록은 일부러 축을 흩는다: 단일 블록 / 다중 블록, 서로 다른 소자
계열(FET 폭 · 저항 길이 · MOS 캡 폭), 그리고 양 방향(줄이기 · 키우기).

모든 값은 `benchmarks/bandgap` 의 출하 덱 기준이다.
"""

PERTURBATIONS = {
    "none": [],

    # --- 다중 블록, 테일 축소 --------------------------------------------
    # 실측된 모양. 8 -> 3 이 22기준 중 6개를 실패시키고 그 실패가 BUF_P /
    # BUF_N / TRIMAMP 세 블록을 가리킨다.
    "tail_both_4": [
        {"refdes": "TRIMAMP.Xt", "param": "W", "new_value": "4"},
        {"refdes": "BUF_P.Xt", "param": "W", "new_value": "4"},
    ],
    "tail_both_3": [
        {"refdes": "TRIMAMP.Xt", "param": "W", "new_value": "3"},
        {"refdes": "BUF_P.Xt", "param": "W", "new_value": "3"},
    ],
    "tail_both_2": [
        {"refdes": "TRIMAMP.Xt", "param": "W", "new_value": "2"},
        {"refdes": "BUF_P.Xt", "param": "W", "new_value": "2"},
    ],

    # --- 단일 블록 -------------------------------------------------------
    # 다중 블록 교란과 짝이 되게 두 방향 각각. 실패가 한 블록만 가리키는
    # 경우와 여러 블록을 가리키는 경우를 나누는 것이 로드맵 태스크 6(그래프
    # 사전확률)의 착수 조건이 요구하는 구분이다.
    "tail_trim_3": [{"refdes": "TRIMAMP.Xt", "param": "W", "new_value": "3"}],
    "tail_bufp_3": [{"refdes": "BUF_P.Xt", "param": "W", "new_value": "3"}],

    # --- 다른 소자 계열: 저항 길이, 양 방향 -------------------------------
    # CLAUDE.md 가 기록한 사실 - TRIMAMP.XRz.l 은 15에서 60으로 키우면 위상
    # 여유가 81 -> 125도로 좋아지고, 120에서 다시 무너진다(최적점이 단조가
    # 아니다). 그래서 이 축은 축소만이 아니라 **키우는 방향의 실패**도 준다.
    "rz_trim_60": [{"refdes": "TRIMAMP.XRz", "param": "l", "new_value": "60"}],
    "rz_trim_120": [{"refdes": "TRIMAMP.XRz", "param": "l", "new_value": "120"}],

    # --- 다른 소자 계열: MOS 캡 폭 ---------------------------------------
    # 밀러 보상 캡. 위상 여유 계열을 직접 친다.
    "cc_trim_20": [{"refdes": "TRIMAMP.Xcc", "param": "W", "new_value": "20"}],

    # --- 폴드/미러 축소 ---------------------------------------------------
    # 2026-07-30 의 교란 실행에서 튜너가 **키운** 바로 그 소자들을 반대로
    # 민다. 튜너가 찾은 해의 반대 방향이므로 실패 모양이 다르다.
    "mirror_trim_4": [
        {"refdes": "TRIMAMP.Xn1", "param": "W", "new_value": "4"},
        {"refdes": "TRIMAMP.Xn2", "param": "W", "new_value": "4"},
        {"refdes": "TRIMAMP.Xm1", "param": "W", "new_value": "4"},
        {"refdes": "TRIMAMP.Xm2", "param": "W", "new_value": "4"},
    ],

    # --- 출력단 축소 ------------------------------------------------------
    "out_trim_20": [{"refdes": "TRIMAMP.X6", "param": "W", "new_value": "20"}],
}
