"""Turn a Playwright codegen recording into no-code steps.

Testers record by clicking through the application (Playwright's recorder, launched from
Studio with the Okta session already signed in). The generated script is parsed with `ast`
- never executed - and each recognised line becomes a step. Locators that already exist in a
page object are replaced by that element, so recorded tests reuse the exact POM locators.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

from qaforge.nocode.locator_expr import LocatorExprError, canonical, to_python, _chain

SENSITIVE_HINT = re.compile(r"password|passcode|pin\b|otp|one.time|cvv|secret|credentials", re.I)
_ACTIONS = {"click": "click", "dblclick": "click", "fill": "fill", "press_sequentially": "fill", "type": "fill",
            "press": "press", "check": "check", "uncheck": "uncheck", "select_option": "select",
            "set_input_files": "upload"}
_EXPECTS = {"to_be_visible": "expect_visible", "to_be_hidden": "expect_hidden", "to_have_text": "expect_text",
            "to_contain_text": "expect_text", "to_have_value": "expect_value"}


@dataclass
class ImportResult:
    steps: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reused: int = 0
    inline: int = 0


def _const(node: ast.AST | None) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts and isinstance(node.elts[0], ast.Constant):
        return node.elts[0].value
    return None


def _locator_of(node: ast.AST) -> str | None:
    try:
        return to_python(_chain(node), "page")
    except LocatorExprError:
        return None


def _statements(tree: ast.Module) -> list[ast.stmt]:
    out: list[ast.stmt] = []

    def visit(body: list[ast.stmt]) -> None:
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.With)):
                visit(st.body)
            else:
                out.append(st)

    visit(tree.body)
    return out


def import_recording(source: str, base_url: str = "", known: dict[str, str] | None = None,
                     app: str | None = None, role: str | None = None) -> ImportResult:
    known = known or {}
    res = ImportResult()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        res.warnings.append(f"Recording could not be read: {exc.msg}")
        return res
    if app:
        res.steps.append({"action": "open_app", "options": {"app": app, "role": role} if role else {"app": app}})

    def add(step: dict[str, Any], loc: str | None) -> None:
        if loc is not None:
            ref = None
            try:
                ref = known.get(canonical(loc))
            except LocatorExprError:
                pass
            if ref:
                step["target"] = ref
                res.reused += 1
            else:
                step["locator"] = loc
                res.inline += 1
        res.steps.append(step)

    for st in _statements(tree):
        if not isinstance(st, ast.Expr) or not isinstance(st.value, ast.Call):
            if isinstance(st, ast.Assign) and "expect_popup" in ast.unparse(st):
                res.warnings.append(f"Line {st.lineno}: pop-up windows are not converted - add those steps by hand")
            continue
        call = st.value
        f = call.func
        if not isinstance(f, ast.Attribute):
            continue
        # expect(<locator|page>).to_xxx(...)
        if isinstance(f.value, ast.Call) and isinstance(f.value.func, ast.Name) and f.value.func.id == "expect":
            subject = f.value.args[0] if f.value.args else None
            if f.attr == "to_have_url":
                pattern = _const(call.args[0]) if call.args else None
                if isinstance(pattern, str):
                    res.steps.append({"action": "expect_url", "value": re.escape(pattern)})
                elif call.args:
                    inner = call.args[0]
                    if isinstance(inner, ast.Call) and inner.args and isinstance(inner.args[0], ast.Constant):
                        res.steps.append({"action": "expect_url", "value": inner.args[0].value})
                continue
            action = _EXPECTS.get(f.attr)
            loc = _locator_of(subject) if subject is not None else None
            if action is None or loc is None:
                res.warnings.append(f"Line {st.lineno}: check '{f.attr}' was not converted")
                continue
            step: dict[str, Any] = {"action": action}
            if action in ("expect_text", "expect_value"):
                step["value"] = str(_const(call.args[0]) if call.args else "")
                if f.attr == "to_have_text":
                    step["options"] = {"exact": True}
            add(step, loc)
            continue
        # page.goto(url)
        if f.attr == "goto" and isinstance(f.value, ast.Name):
            url = str(_const(call.args[0]) or "")
            if f.value.id != "page":
                res.warnings.append(f"Line {st.lineno}: navigation in a second window was not converted")
                continue
            if base_url and url.startswith(base_url.rstrip("/")):
                url = url[len(base_url.rstrip("/")):] or "/"
            res.steps.append({"action": "goto_url", "value": url})
            continue
        action = _ACTIONS.get(f.attr)
        if action is None:
            continue
        loc = _locator_of(f.value)
        if loc is None:
            src = ast.unparse(f.value)
            if src.split(".")[0] != "page":
                res.warnings.append(f"Line {st.lineno}: action in another window ({src.split('.')[0]}) not converted")
            else:
                res.warnings.append(f"Line {st.lineno}: locator '{src[:60]}' could not be converted")
            continue
        step = {"action": action}
        if action in ("fill", "press", "select", "upload"):
            value = _const(call.args[0]) if call.args else None
            if value is None:
                res.warnings.append(f"Line {st.lineno}: value for '{f.attr}' was not a plain value")
                value = ""
            step["value"] = str(value)
            if action == "select":
                step["options"] = {"by": "value"}
            if action == "fill" and SENSITIVE_HINT.search(loc):
                step["value"] = "${secret:SET_SECRET_NAME}"
                step["options"] = {"sensitive": True}
                res.warnings.append(f"Line {st.lineno}: a secret was typed while recording; it was removed. "
                                    "Choose the secret name in the step, or use 'Open app and sign in'.")
        add(step, loc)
    # click immediately followed by fill on the same element: the click is redundant
    cleaned: list[dict[str, Any]] = []
    for s in res.steps:
        if (cleaned and s["action"] == "fill" and cleaned[-1]["action"] == "click"
                and cleaned[-1].get("locator", cleaned[-1].get("target")) == s.get("locator", s.get("target"))):
            cleaned.pop()
        cleaned.append(s)
    res.steps = cleaned
    return res
