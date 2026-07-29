"""이 저장소가 내보내는 JSON 산출물의 **전송 형식** 한 곳.

`json.dump`의 기본 동작은 비유한 float를 `NaN`/`Infinity`/`-Infinity`라는
**bare 토큰**으로 쓰는 것인데, 그것은 RFC 8259가 아니다. 그래서:

- node의 `JSON.parse`는 파일 **전체**를 `SyntaxError`로 거부한다.
- `jq` 1.7.1은 거부하지 **않고** `-Infinity`를 `-1.797e308`로 바꿔 준다 -
  어느 시뮬레이션에서도 관측된 적 없는 숫자가 소비자에게 값으로 전달된다.

두 번째가 더 나쁘다. 파싱 거부는 보이지만 조용한 값 변조는 안 보이고, 하필
**코너에서 깨진 실행**, 즉 사람이 가장 열어 볼 이유가 큰 실행에서만 나온다
(`runs/pvt_sonnet_1/result.json`에 리터럴 `NaN`이 8개 있었다).

비유한 값은 예외 경로가 아니라 **정상 경로**에서 나온다:
`judge_tools.evaluate_criteria`는 측정이 없는 기준의 `actual`/`margin`에
`math.nan`을 싣고, `pvt.corner_severity`는 측정이 하나라도 없는 코너에
`-math.inf`를 낸다 - 그 함수의 독스트링 자체가 "어느 코너에서 AC 응답이 0 dB를
안 넘어 ugbw가 안 나오는 것"을 정상 경우로 적는다.

**`null`로 접지 않는다.** 이 산출물에서 `null`은 이미 "그 필드에 값이 없다"를
뜻하고(예: `worst_case_corners[...]["value"]`), `NaN`은 "쟀는데 값이 안 나왔다"는
**다른 사실**이다. 이 저장소는 그 구별로 여러 번 값을 치렀다
(`corner_unattributed_failure`, `deltas_between`이 없는 기준을 0.0으로 읽지
않는 것). 문자열 표지는 유효한 JSON이면서 그 구별을 보존한다.

**정규화가 먼저, `allow_nan=False`는 그 뒤의 못이다.** 순서가 중요하다:
`allow_nan=False`만 켜면 `ValueError`가 `write_result_json`에서 터지고
`cli.main()`의 다음 줄인 `write_report_md`까지 날아간다 - 최적화 단계가
크래시해서 `result.json`도 `report.md`도 안 써졌던 사건과 정확히 같은 모양이다.
`allow_nan=False`는 나중에 누가 정규화를 우회하는 경로를 추가했을 때 조용히
비표준 JSON이 나가는 대신 **여기서** 터지게 하는 용도다.

이 모듈은 `cli_curate.py`가 큐레이션 산출물에 대해 먼저 한 결정을 그대로
올린 것이다. 대조군이 저장소 안에 이미 있는데 본체가 안 고쳐져 있었다.
"""

import json
import math

# 비유한 float를 JSON으로 내보낼 때 쓰는 문자열 표지. 값은 파이썬/JS가 쓰는
# 이름과 같게 두어, 산출물을 눈으로 읽는 사람이 번역표를 안 봐도 되게 한다.
NON_FINITE_JSON = {"nan": "NaN", "inf": "Infinity", "-inf": "-Infinity"}

# 읽는 쪽의 역표. 표지 -> float.
_RESTORE = {
    NON_FINITE_JSON["nan"]: math.nan,
    NON_FINITE_JSON["inf"]: math.inf,
    NON_FINITE_JSON["-inf"]: -math.inf,
}


def json_safe(value):
    """비유한 float를 문자열 표지로 바꿔 **유효한** RFC 8259 JSON을 낸다.

    dict/list/tuple을 재귀적으로 훑고 그 밖의 값은 그대로 둔다. 유한 float는
    건드리지 않는다 - 이 저장소 산출물의 거의 전부가 그것이다.
    """
    if isinstance(value, float):
        if math.isnan(value):
            return NON_FINITE_JSON["nan"]
        if math.isinf(value):
            return NON_FINITE_JSON["inf" if value > 0 else "-inf"]
        return value
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def restore_non_finite(value):
    """`json_safe`의 역. 표지 문자열을 다시 float로 되돌린다.

    **표지는 전송 형식이지 값이 아니다.** 산출물을 다시 읽어 계산하는 소비자는
    숫자를 받아야 한다 - `scripts/paired_tuner_probe.py`는 `history.jsonl`의
    judge 이벤트를 `attempt_log.deltas_between`으로 **뺀다**. 표지를 그대로
    넘기면 그 뺄셈이 `TypeError`가 된다.

    한계는 적어 둔다: 값이 정확히 `"NaN"`/`"Infinity"`/`"-Infinity"`인 **진짜
    문자열**은 float로 복원된다. 오늘 이 산출물들이 담는 문자열은 refdes,
    코너 라벨, 게이트 피드백, 사유 코드뿐이라 도달하지 않는다. 그 셋 중 하나를
    문자열 값으로 담는 필드가 생기면 여기가 그 필드를 조용히 망가뜨리는
    자리다.
    """
    if isinstance(value, str):
        return _RESTORE.get(value, value)
    if isinstance(value, dict):
        return {k: restore_non_finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [restore_non_finite(v) for v in value]
    return value


def dumps(value, **kwargs) -> str:
    """한 줄짜리 JSON(예: `history.jsonl`의 한 이벤트). 정규화 먼저,
    `allow_nan=False`는 그 뒤의 못."""
    return json.dumps(json_safe(value), allow_nan=False, **kwargs)


def dump(value, fp, **kwargs) -> None:
    """파일로 쓰는 JSON(예: `result.json`). `dumps`와 같은 순서."""
    json.dump(json_safe(value), fp, allow_nan=False, **kwargs)
