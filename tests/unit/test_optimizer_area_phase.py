"""면적 최소화 단계 - 표식, 설정, 조립."""
import json

import pytest

from analogcoder.area import DEFAULT_AREA_MODEL, total_area
from analogcoder.optimizer import AREA_OBJECTIVE, _objective_value


def test_the_area_objective_marker_is_not_a_string():
    """측정값 이름 공간과 겹칠 수 없어야 한다.

    목적 이름은 측정값 딕셔너리를 색인하는 데 쓰인다. 문자열 표식은 언젠가
    같은 이름의 진짜 measure와 부딪히고, 그 충돌은 조용하다 - 예외도 로그도
    없이 다른 양이 목적값 자리에 들어가고 탐색이 그것을 성실하게 내린다."""
    assert not isinstance(AREA_OBJECTIVE, str)
    assert AREA_OBJECTIVE != "area"


def test_the_default_area_model_is_the_shipped_total_area():
    """경계는 새로 계산하지 않는다 - 오늘의 함수를 가리킬 뿐이다."""
    assert DEFAULT_AREA_MODEL is total_area


def test_the_marker_reads_derived_area_and_a_name_reads_measurements():
    """목적값 선택 규칙 자체를 핀한다.

    오라클 밖으로 뽑는 이유는, 규칙이 오라클 안에만 있으면 시뮬레이터를
    세워야만 잴 수 있고 그러면 이 분기가 사실상 검사되지 않기 때문이다.
    덱이 `area`라는 measure를 내놓아도 표식과 섞이지 않는 것을 함께 본다."""
    measurements = {"area": 999.0, "iq_ua": 212.99}
    assert _objective_value(AREA_OBJECTIVE, measurements, derived_area=41.0) == 41.0
    assert _objective_value("iq_ua", measurements, derived_area=41.0) == 212.99
    # 없는 이름은 None이다 - 0이 아니다. 0이면 수락 규칙이 "목적값이 최선보다
    # 낮다"를 참으로 읽어 재지 못한 후보를 수락한다.
    assert _objective_value("nope", measurements, derived_area=41.0) is None
