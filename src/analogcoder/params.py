import ast
import re

from analogcoder.netlist import (
    Component,
    ParsedNetlist,
    TracedTarget,
    _is_subckt_close,
    _is_subckt_open,
    logical_lines,
    parse_netlist,
    parse_spice_value,
    split_tokens,
)

_PARAM_DIRECTIVE_RE = re.compile(r"^\s*\.param\b(.*)$", re.IGNORECASE)
# 마지막 대안 `.+?(?=\s+\w+\s*=|$)`가 핵심이다: 따옴표도 중괄호도 없는 값은
# 다음 `이름=` 경계(다음 .param 항목)나 줄 끝까지 통째로 삼킨다. 이전에는
# `\S+`라 `wp = wn * 2`에서 공백 뒤 `* 2`가 잘려나가 wp가 wn과 같은 값으로
# 조용히 틀리게 풀렸다 - 못 푸는 게 아니라 틀리게 푸는 게 이 프로젝트가
# 막으려는 정확히 그 실패 모드다.
_ASSIGN_RE = re.compile(r"(\w+)\s*=\s*('[^']*'|\{[^}]*\}|.+?(?=\s+\w+\s*=|$))")


class _Unresolvable(Exception):
    """평가가 이 모듈이 다루기로 한 범위를 벗어났다."""


_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Pow: lambda a, b: a**b,
}


def _eval(node: ast.AST, env: dict[str, float]) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise _Unresolvable()
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise _Unresolvable()
        return env[node.id]
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            return -_eval(node.operand, env)
        if isinstance(node.op, ast.UAdd):
            return _eval(node.operand, env)
        raise _Unresolvable()
    if isinstance(node, ast.BinOp):
        handler = _BINOPS.get(type(node.op))
        if handler is None:
            raise _Unresolvable()
        try:
            return handler(_eval(node.left, env), _eval(node.right, env))
        except ZeroDivisionError:
            raise _Unresolvable() from None
    raise _Unresolvable()


def resolve_value(raw: str, env: dict[str, float]) -> float | None:
    """raw를 수치로 해소하거나, 이 모듈이 다루기로 한 범위 밖이면 None.

    범위는 의도적으로 좁다: 산술(+ - * / **), 단항 부호, 괄호, SPICE 접미사
    리터럴, 다른 파라미터 참조까지. 함수·조건식·미정의 이름·순환 참조는 전부
    None이다. 조용히 틀린 숫자를 내놓는 것보다 명시적 '모름'이 낫다는
    것이 이 프로젝트에서 세 번 반복된 교훈이다.

    평가는 ast 화이트리스트로 하며 eval을 쓰지 않는다."""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] == "'":
        s = s[1:-1].strip()
    elif s.startswith("{") and s.endswith("}"):
        s = s[1:-1].strip()
    if not s:
        return None
    try:
        return parse_spice_value(s)
    except ValueError:
        pass
    try:
        tree = ast.parse(s, mode="eval")
    except (SyntaxError, ValueError):
        return None
    try:
        return _eval(tree.body, env)
    except _Unresolvable:
        return None
    except RecursionError:
        return None


def _resolve_environment(
    raw_params: dict[str, str], seed: dict[str, float], shadowed: frozenset[str] = frozenset()
) -> dict[str, float]:
    """raw_params를 고정점에 도달할 때까지 반복 해소한다.

    한 번의 통과로는 부족하다: `.param wp='wn*2'`가 `.param wn=4`보다 먼저
    선언될 수 있다. 통과 한 번에 최소 하나는 새로 풀리므로 파라미터 수만큼
    반복하면 충분하고, 그래도 남는 것은 순환이거나 범위 밖이다.

    seed는 raw_params가 없는 이름에는 값을 대주지만, raw_params에 있는
    이름은 로컬에서 새로 푸는 대상이다 - seed에 같은 이름이 이미 있다고
    "이미 풀렸다"고 착각하면 전역 < 서브회로 기본값 < 인스턴스 오버라이드
    우선순위가 뒤집힌다 (전역이 항상 이긴다). 그래서 raw_params의 이름은
    seed에서 지우고 시작하고, 로컬에서 끝내 못 풀면 seed 값으로 되돌아가지
    않고 그냥 빠진 채로 남긴다 - 로컬 선언이 있는데 그게 안 풀렸다고 전역을
    노출하는 것도 같은 종류의 추측이다.

    shadowed는 로컬에 선언은 있으나 값을 확정할 수 없어 raw_params에서 아예
    빠진 이름들이다 (인스턴스마다 값이 갈렸거나, 인터페이스와 본문 .param이
    같은 이름을 두고 충돌한 경우). raw_params에 없다는 이유로 seed 값이
    비치면 "모른다"고 판정해 놓고 전역값을 내주는 셈이 되므로, 이쪽도
    똑같이 가려야 한다."""
    env = {k: v for k, v in seed.items() if k not in raw_params and k not in shadowed}
    for _ in range(len(raw_params) + 1):
        progressed = False
        for name, raw in raw_params.items():
            if name in env:
                continue
            value = resolve_value(raw, env)
            if value is not None:
                env[name] = value
                progressed = True
        if not progressed:
            break
    return env


def _collect_raw_params(text: str) -> dict[str | None, dict[str, str]]:
    """스코프 경로(최상위는 None) → 그 스코프에서 선언된 `.param` 원본 문자열.

    서브회로 본문의 `.param`도 수집한다. 예전에는 depth를 세어 최상위 것만
    읽었기 때문에 `.subckt` 안에 선언된 파라미터를 참조하는 소자는 값을
    해소하지 못하고 폴백을 탔다 - 안전한 방향이긴 해도 커버리지 구멍이었다.

    `logical_lines`를 쓰므로 `+` 연속 줄에 이어진 할당도 잃지 않는다."""
    collected: dict[str | None, dict[str, str]] = {None: {}}
    stack: list[str] = []
    for code, _indices in logical_lines(text.splitlines()):
        lower = code.lower()
        if _is_subckt_open(lower):
            stack.append(split_tokens(code)[1])
            continue
        if _is_subckt_close(lower):
            if stack:
                stack.pop()
            continue
        match = _PARAM_DIRECTIVE_RE.match(code)
        if match:
            scope = ".".join(stack) if stack else None
            target = collected.setdefault(scope, {})
            for name, value in _ASSIGN_RE.findall(match.group(1)):
                target[name] = value
    return collected


def _resolve_subckt_reference(parsed, component_scope: str | None, name: str) -> str | None:
    """인스턴스 줄의 짧은 이름(component.value)이 실제로 가리키는 서브회로
    정의의 전체 경로를 SPICE 스코프 규칙대로 찾는다: 인스턴스 자신의
    스코프에서 먼저 찾고, 못 찾으면 한 단계씩 바깥으로 올라가고, 마지막에
    최상위(스코프 없는 이름)를 본다.

    서로 다른 바깥 스코프에 같은 짧은 이름의 서브회로가 있는 경우
    (`A.LOAD`, `B.LOAD`) 인스턴스를 짧은 이름만으로 매칭하면 둘을 같은
    정의로 오인해 오버라이드가 서로 새어 들어간다 - 이 프로젝트는 여러
    블록에 걸친 같은 이름 충돌을 예외가 아니라 정상 케이스로 다룬다
    (bandgap 벤치마크의 네 증폭기)."""
    if component_scope:
        parts = component_scope.split(".")
        for i in range(len(parts), 0, -1):
            candidate = ".".join([*parts[:i], name])
            if candidate in parsed.subckts:
                return candidate
    if name in parsed.subckts:
        return name
    return None


def _instance_overrides(parsed, subckt_path: str, subckt_name: str) -> tuple[dict[str, str], set[str]] | None:
    """subckt_path가 가리키는 서브회로 정의를 실제로 인스턴스화하는 컴포넌트들이
    합의하는 오버라이드와, 인스턴스마다 값이 갈린 파라미터 이름 집합을 함께
    돌려준다 (그런 인스턴스가 하나도 없으면 None).

    후보를 짧은 이름(subckt_name)만으로 고르고 나면, 그 중 실제로 이
    subckt_path를 참조하는 것만 스코프 규칙(_resolve_subckt_reference)으로
    걸러낸다 - 그래야 다른 스코프의 동명 서브회로 인스턴스가 섞여 들지
    않는다.

    ctype이 X인 줄만 서브회로 인스턴스화로 본다. SPICE에서 서브회로를
    부르는 것은 X-접두 줄뿐이다 - value가 우연히 subckt 이름과 같은 M/Q
    같은 다른 소자(예: 트랜지스터 모델명이 "CORE")까지 매칭하면 그
    트랜지스터의 파라미터가 서브회로 기본값 자리로 새어 들어간다.

    합의된 오버라이드는 기본값 위에 그대로 얹으면 되지만, 갈린 파라미터는
    호출자가 .subckt 줄 기본값째로 지워야 한다 - 값이 진짜로 인스턴스마다
    다른데 이 프로젝트는 소자를 서브회로 정의로 주소지정하므로 단일 정답이
    없고, 기본값으로 슬쩍 되돌아가는 것도 추측이기는 마찬가지다."""
    components = list(parsed.top_components)
    for subckt in parsed.subckts.values():
        components.extend(subckt.components)

    seen: dict[str, set[str]] = {}
    found = False
    for component in components:
        if component.ctype != "X" or component.value != subckt_name:
            continue
        if _resolve_subckt_reference(parsed, component.scope, subckt_name) != subckt_path:
            continue
        found = True
        for name, value in component.params.items():
            seen.setdefault(name, set()).add(value)
    if not found:
        return None
    agreed = {name: next(iter(values)) for name, values in seen.items() if len(values) == 1}
    disagreeing = {name for name, values in seen.items() if len(values) > 1}
    return agreed, disagreeing


def build_param_envs(text: str) -> dict[str | None, dict[str, float]]:
    """스코프 경로(최상위는 None) → 해소된 파라미터 환경.

    우선순위는 낮은 것부터: 전역 .param, 서브회로 본문 .param,
    .subckt 줄 기본값, 인스턴스 오버라이드.

    본문 .param과 .subckt 줄 기본값이 같은 이름을 선언하면 어느 쪽이 이기는지가
    방언마다 다르므로 그 이름은 해소 불가로 둔다 - 인스턴스마다 값이 갈릴 때와
    같은 규칙이고, 같은 이유다.

    인스턴스 오버라이드는 한 단계만 전파한다. 서브회로가 다른 서브회로 안에서
    인스턴스화되고 그 바깥쪽이 서로 다른 파라미터로 여러 번 인스턴스화되는
    경우까지는 따라가지 않는다 - 전체 트리 전파는 E2가 인스턴스 트리를 만든
    뒤에 가능하다."""
    parsed = parse_netlist(text)
    raw_by_scope = _collect_raw_params(text)
    global_env = _resolve_environment(raw_by_scope.get(None, {}), {})

    envs: dict[str | None, dict[str, float]] = {None: global_env}
    for path, subckt in parsed.subckts.items():
        body = raw_by_scope.get(path, {})
        raw = {**body, **subckt.defaults}
        shadowed = set(body) & set(subckt.defaults)
        result = _instance_overrides(parsed, path, subckt.name)
        if result is not None:
            agreed, disagreeing = result
            raw.update(agreed)
            shadowed |= disagreeing
            # 인스턴스가 하나의 값으로 합의한 이름은 더 이상 경합이 아니다.
            # 본문 .param과 .subckt 줄 기본값이 충돌해도, 명시적 오버라이드가
            # 가장 높은 우선순위라는 규칙이 그대로 적용된다.
            shadowed -= set(agreed)
        for name in shadowed:
            raw.pop(name, None)
        envs[path] = _resolve_environment(raw, global_env, frozenset(shadowed))
    return envs


# --- 인스턴스 파라미터 추적 ------------------------------------------------
# 래퍼 셀 스타일 덱에서는 소자 크기가 서브회로 본문이 아니라 **인스턴스
# 줄**에서 정해진다:
#
#   .subckt WRAP_PAIR b1 b2 d1 d2 g1 g2 s1 s2
#   ma1 d1 g1 s1 b1 TN33_LVT w=wn l=ln m=ma1 nf=nf_n
#   .ends
#   xin1 ... WRAP_PAIR wn=2e-6 ln=3e-6 ma1=4 nf_n=1
#
# 튜닝은 `xin1.wn`에 일어난다. 면적 게이트가 이것을 판정하려면 `wn`이 무엇을
# 키우는지 알아야 하는데, 이름에서 읽어내는 것은 추측이다. 대신 값이 어디에
# 도달하는지를 따라간다 - 도달점의 토큰 이름은 SPICE 표준 문법이다.

# 중첩 래퍼를 따라가는 최대 깊이. 순환 참조(서브회로가 자기를 인스턴스화)나
# 비정상적으로 깊은 계층에서 무한히 도는 것을 막는다. 넘으면 추측하지 않고
# "판단 불가"(None)로 끝낸다.
_MAX_TRACE_DEPTH = 8


def free_names(raw: str) -> set[str]:
    """raw 표현식이 참조하는 파라미터 이름들. 숫자 리터럴이면 빈 집합.

    `w=wn`이면 {"wn"}, `w='wn*2'`면 {"wn"}, `l=3e-6`이면 빈 집합이다.
    resolve_value와 같은 ast 화이트리스트를 쓰므로 판정 범위가 어긋나지
    않는다 - 여기서 이름이 보이는데 저기서 못 푸는 경우는 있어도 (그때는
    total_width가 None이 되어 판단 불가로 떨어진다) 그 반대는 없다."""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] == "'":
        s = s[1:-1].strip()
    elif s.startswith("{") and s.endswith("}"):
        s = s[1:-1].strip()
    if not s:
        return set()
    try:
        parse_spice_value(s)
        return set()
    except ValueError:
        pass
    try:
        tree = ast.parse(s, mode="eval")
    except (SyntaxError, ValueError):
        return set()
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def _all_components(parsed: ParsedNetlist) -> list[Component]:
    components = list(parsed.top_components)
    for subckt in parsed.subckts.values():
        components.extend(subckt.components)
    return components


def _token_value(component: Component, token: str, env: dict[str, float]) -> float | None:
    """소자가 쓴 토큰 값을 대소문자 무시로 찾아 env에서 해소한다.

    SPICE는 대소문자를 구분하지 않아 같은 덱에서 `W=30`과 `w=1`이 함께
    나온다 - 한쪽 표기만 보면 조용히 못 찾는다."""
    for name, raw in component.params.items():
        if name.lower() == token:
            return resolve_value(raw, env)
    return None


def _total_width(device: Component, env: dict[str, float]) -> float | None:
    """이 인스턴스에서 device의 총 폭 = w x m, `.option scale` 반영.

    m은 병렬 소자의 **개수**이므로 폭에 곱해지되 scale은 곱하지 않는다
    (`w=2u m=2`는 2um 소자 두 개, 총 폭 4um). nf는 여기 들어오지 않는다 -
    손가락은 w를 나눌 뿐 총 폭을 바꾸지 않는다."""
    w = _token_value(device, "w", env)
    if w is None:
        return None
    m = _token_value(device, "m", env)
    return w * device.geometry_scale * (1.0 if m is None else m)


def _instance_env(
    parsed: ParsedNetlist,
    raw_by_scope: dict[str | None, dict[str, str]],
    global_env: dict[str, float],
    subckt_path: str,
    overrides: dict[str, str],
    outer_env: dict[str, float],
) -> dict[str, float]:
    """**이 인스턴스 하나**에 대한 서브회로 내부 파라미터 환경.

    build_param_envs는 서브회로 *정의* 단위로 풀고, 인스턴스마다 값이 갈린
    이름은 (정당하게) 버린다. 그런데 이 설계 스타일에서는 값이 갈리는 것이
    정상이다 - 같은 셀을 ma1=4/1/2로 세 번 인스턴스화한다. 그래서 정의 단위
    환경은 여기서 필요한 숫자를 정확히 필요할 때 주지 못한다.

    우선순위(낮은 것부터): 본문 .param < .subckt 줄 기본값 < 인스턴스 오버라이드.

    인스턴스가 준 값은 바깥 스코프의 표현식일 수 있으므로 outer_env에서 먼저
    해소한다. 바깥에서 확정하지 못한 오버라이드는 기본값으로 되돌아가지 않고
    가린다 - 인스턴스가 실제로 쓰는 값과 다른 숫자를 내주는 것이 모른다고
    말하는 것보다 나쁘다는, 이 모듈에서 반복된 원칙이다."""
    subckt = parsed.subckts[subckt_path]
    raw = {**raw_by_scope.get(subckt_path, {}), **subckt.defaults}
    shadowed: set[str] = set()
    for name, raw_value in overrides.items():
        value = resolve_value(raw_value, outer_env)
        if value is None:
            shadowed.add(name)
            raw.pop(name, None)
            continue
        raw[name] = repr(value)
    return _resolve_environment(raw, global_env, frozenset(shadowed))


def _trace(
    parsed: ParsedNetlist,
    raw_by_scope: dict[str | None, dict[str, str]],
    global_env: dict[str, float],
    subckt_path: str,
    env: dict[str, float],
    param: str,
    depth: int,
) -> list[TracedTarget] | None:
    """subckt_path 본문에서 param이 도달하는 소자/토큰들. 판단 불가면 None.

    본문 소자가 다시 (덱 안에 정의된) 서브회로 인스턴스이면 그 안으로
    따라 들어간다. 덱이 정의하지 않은 서브회로 - sky130 PDK 프리미티브가
    그렇다, parse_netlist는 include를 따라가지 않는다 - 는 잎으로 본다."""
    if depth >= _MAX_TRACE_DEPTH:
        return None
    targets: list[TracedTarget] = []
    for device in parsed.subckts[subckt_path].components:
        for token, raw_value in device.params.items():
            if param not in free_names(raw_value):
                continue
            nested = (
                _resolve_subckt_reference(parsed, device.scope, device.value)
                if device.ctype == "X" and device.value is not None
                else None
            )
            if nested is None:
                targets.append(
                    TracedTarget(device=device, token=token, total_width=_total_width(device, env))
                )
                continue
            inner_env = _instance_env(
                parsed, raw_by_scope, global_env, nested, device.params, env
            )
            deeper = _trace(
                parsed, raw_by_scope, global_env, nested, inner_env, token, depth + 1
            )
            if deeper is None:
                # 중간에서 끊긴 추적이다. 일부만 들고 판정하면 나머지 경로가
                # 없는 것처럼 보이므로, 통째로 판단 불가로 둔다.
                return None
            targets.extend(deeper)
    return targets or None


def annotate_traced_params(
    text: str, parsed: ParsedNetlist, envs: dict[str | None, dict[str, float]]
) -> None:
    """모든 서브회로 인스턴스에 대해 component.traced_params를 채운다.

    parsed/envs를 받아 쓰는 이유는 호출자(index_baseline_components)가 이미
    둘 다 갖고 있기 때문이다 - 여기서 다시 파싱하면 같은 덱을 세 번 읽는다."""
    raw_by_scope = _collect_raw_params(text)
    global_env = envs[None]
    for component in _all_components(parsed):
        if component.ctype != "X" or component.value is None or not component.params:
            continue
        path = _resolve_subckt_reference(parsed, component.scope, component.value)
        if path is None:
            continue
        outer_env = envs.get(component.scope, global_env)
        env = _instance_env(
            parsed, raw_by_scope, global_env, path, component.params, outer_env
        )
        for name in component.params:
            targets = _trace(parsed, raw_by_scope, global_env, path, env, name, 0)
            if targets:
                component.traced_params[name] = targets
