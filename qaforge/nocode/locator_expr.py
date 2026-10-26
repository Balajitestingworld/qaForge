"""Safe handling of Playwright locator expressions - parsed, never eval()'d.

Locators reach the framework from three non-code sources: Locator Spy captures, Playwright
codegen recordings, and testers typing them in Studio. All of them pass through this module:

    calls = parse('page.get_by_role("button", name="Next").first')
    loc   = build(calls, page)                    # a real Playwright Locator
    text  = to_python(calls, root="self.page")    # canonical source used in generated page objects

Only whitelisted locator methods with literal arguments are accepted, so a locator string can
never smuggle code into a generated page object or into a test run.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any

ROOT_METHODS = {
    "locator", "get_by_role", "get_by_text", "get_by_label", "get_by_placeholder", "get_by_alt_text",
    "get_by_title", "get_by_test_id", "frame_locator",
}
CHAIN_METHODS = ROOT_METHODS | {"nth", "filter", "and_", "or_"}
PROPERTIES = {"first", "last", "content_frame"}
MAX_LEN = 2000


class LocatorExprError(ValueError):
    pass


@dataclass(frozen=True)
class Regex:
    pattern: str
    ignore_case: bool = False


@dataclass
class Call:
    name: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    is_property: bool = False


@dataclass
class Nested:
    calls: list[Call]


def parse(expr: str) -> list[Call]:
    src = (expr or "").strip()
    if not src:
        raise LocatorExprError("Locator is empty")
    if len(src) > MAX_LEN:
        raise LocatorExprError("Locator is too long")
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as exc:
        raise LocatorExprError(f"Not a valid locator expression: {exc.msg}") from exc
    return _chain(tree.body)


def is_valid(expr: str) -> bool:
    try:
        parse(expr)
        return True
    except LocatorExprError:
        return False


def _is_self_page(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "page"
            and isinstance(node.value, ast.Name) and node.value.id == "self")


def _chain(node: ast.AST) -> list[Call]:
    calls: list[Call] = []
    while True:
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                name, base = func.attr, func.value
            elif isinstance(func, ast.Name):
                name, base = func.id, None
            else:
                raise LocatorExprError("Unsupported call in locator")
            if name not in CHAIN_METHODS:
                raise LocatorExprError(f"'{name}' is not an allowed locator method")
            if any(k.arg is None for k in node.keywords):
                raise LocatorExprError("**kwargs are not allowed in locators")
            calls.append(Call(name, tuple(_literal(a) for a in node.args),
                              {k.arg: _literal(k.value) for k in node.keywords if k.arg}))
            if base is None:
                break
            node = base
        elif isinstance(node, ast.Attribute):
            if _is_self_page(node):
                break
            if node.attr not in PROPERTIES:
                raise LocatorExprError(f"'{node.attr}' is not an allowed locator property")
            calls.append(Call(node.attr, is_property=True))
            node = node.value
        elif isinstance(node, ast.Name):
            if node.id in ("page", "frame"):
                break
            raise LocatorExprError(f"Locator must start from the page, not '{node.id}'")
        else:
            raise LocatorExprError("Unsupported locator syntax")
    calls.reverse()
    if not calls or calls[0].name not in ROOT_METHODS:
        raise LocatorExprError("Locator must start with page.locator(...) or page.get_by_...(...)")
    return calls


def _literal(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool, type(None))):
        return node.value
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, (int, float))):
        return -node.operand.value
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "compile"
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "re"):
        if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
            raise LocatorExprError("re.compile needs a literal pattern")
        flags = node.args[1:] + [k.value for k in node.keywords]
        ignore = any(isinstance(f, ast.Attribute) and f.attr in ("I", "IGNORECASE") for f in flags)
        return Regex(node.args[0].value, ignore)
    if isinstance(node, (ast.Call, ast.Attribute)):
        return Nested(_chain(node))
    raise LocatorExprError("Locator arguments must be plain text, numbers, re.compile(...) or locators")


def _runtime(value: Any, page: Any) -> Any:
    if isinstance(value, Regex):
        return re.compile(value.pattern, re.IGNORECASE if value.ignore_case else 0)
    if isinstance(value, Nested):
        return build(value.calls, page)
    return value


def build(calls: list[Call], root: Any, page: Any | None = None) -> Any:
    """Apply the parsed chain to a Page / Frame / FrameLocator and return the Locator."""
    page = page or root
    obj = root
    for c in calls:
        if c.is_property:
            obj = getattr(obj, c.name)
        else:
            obj = getattr(obj, c.name)(*(_runtime(a, page) for a in c.args),
                                       **{k: _runtime(v, page) for k, v in c.kwargs.items()})
    return obj


def build_expr(expr: str, root: Any) -> Any:
    return build(parse(expr), root)


def _src(value: Any, root: str) -> str:
    if isinstance(value, Regex):
        return f"re.compile({value.pattern!r}{', re.IGNORECASE' if value.ignore_case else ''})"
    if isinstance(value, Nested):
        return to_python(value.calls, root)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # double quotes, like Playwright codegen
    return repr(value)


def to_python(calls: list[Call], root: str = "page") -> str:
    parts = []
    for c in calls:
        if c.is_property:
            parts.append(c.name)
            continue
        args = [_src(a, root) for a in c.args] + [f"{k}={_src(v, root)}" for k, v in c.kwargs.items()]
        parts.append(f"{c.name}({', '.join(args)})")
    return f"{root}." + ".".join(parts)


def canonical(expr: str) -> str:
    """Normalised form used to compare locators from POM files, recordings and the spy."""
    return to_python(parse(expr), "page")


def uses_regex(calls: list[Call]) -> bool:
    def _has(v: Any) -> bool:
        return isinstance(v, Regex) or (isinstance(v, Nested) and uses_regex(v.calls))

    return any(_has(a) for c in calls for a in (*c.args, *c.kwargs.values()))
