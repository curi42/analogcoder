import ast
import re

from analogcoder.netlist import (
    _is_subckt_close,
    _is_subckt_open,
    parse_netlist,
    parse_spice_value,
    strip_inline_comment,
)

_PARAM_DIRECTIVE_RE = re.compile(r"^\s*\.param\b(.*)$", re.IGNORECASE)
_ASSIGN_RE = re.compile(r"(\w+)\s*=\s*('[^']*'|\{[^}]*\}|\S+)")


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


def _resolve_environment(raw_params: dict[str, str], seed: dict[str, float]) -> dict[str, float]:
    """raw_params를 고정점에 도달할 때까지 반복 해소한다.

    한 번의 통과로는 부족하다: `.param wp='wn*2'`가 `.param wn=4`보다 먼저
    선언될 수 있다. 통과 한 번에 최소 하나는 새로 풀리므로 파라미터 수만큼
    반복하면 충분하고, 그래도 남는 것은 순환이거나 범위 밖이다."""
    env = dict(seed)
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


def _collect_global_raw_params(text: str) -> dict[str, str]:
    raw: dict[str, str] = {}
    depth = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue
        code, _ = strip_inline_comment(stripped)
        lower = code.lower()
        if _is_subckt_open(lower):
            depth += 1
            continue
        if _is_subckt_close(lower):
            depth = max(0, depth - 1)
            continue
        if depth:
            continue
        match = _PARAM_DIRECTIVE_RE.match(code)
        if match:
            for name, value in _ASSIGN_RE.findall(match.group(1)):
                raw[name] = value
    return raw


def _instance_overrides(parsed, subckt_name: str) -> tuple[dict[str, str], set[str]] | None:
    """subckt_name의 모든 직접 인스턴스가 합의하는 오버라이드와, 인스턴스마다
    값이 갈린 파라미터 이름 집합을 함께 돌려준다 (인스턴스가 하나도 없으면
    None).

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
        if component.value != subckt_name:
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

    우선순위는 낮은 것부터: 전역 .param, 서브회로 .subckt 줄 기본값,
    인스턴스 오버라이드.

    인스턴스 오버라이드는 한 단계만 전파한다. 서브회로가 다른 서브회로 안에서
    인스턴스화되고 그 바깥쪽이 서로 다른 파라미터로 여러 번 인스턴스화되는
    경우까지는 따라가지 않는다 - 전체 트리 전파는 E2가 인스턴스 트리를 만든
    뒤에 가능하다."""
    parsed = parse_netlist(text)
    global_env = _resolve_environment(_collect_global_raw_params(text), {})

    envs: dict[str | None, dict[str, float]] = {None: global_env}
    for path, subckt in parsed.subckts.items():
        raw = dict(subckt.defaults)
        result = _instance_overrides(parsed, subckt.name)
        if result is not None:
            agreed, disagreeing = result
            raw.update(agreed)
            for name in disagreeing:
                raw.pop(name, None)
        envs[path] = _resolve_environment(raw, global_env)
    return envs
