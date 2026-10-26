"""Executes a no-code spec through the same audited framework coded tests use.

Values in steps can reference data:
    ${payee.account_number}   data generated in the test's data section or saved by a step
    ${row.amount}             the current row of a data-driven test
    ${gen.mobile}             a fresh generated value (any DataGenerator method without inputs)
    ${secret:NAME}            a vaulted secret (always hidden in evidence)
    ${run_tag} ${today}       run tag / today's date (YYYY-MM-DD)
"""
from __future__ import annotations

import re
import time
import typing
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from playwright.sync_api import Locator
from pydantic import BaseModel

from qaforge.compliance.evidence import EvidencePack
from qaforge.compliance.logger import utc_now_iso
from qaforge.components.data_grid import DataGrid
from qaforge.config.secrets import SecretProvider
from qaforge.config.settings import Settings
from qaforge.core.base_page import BaseComponent, BasePage
from qaforge.core.locator_chain import LocatorChain
from qaforge.core.session import AppSession
from qaforge.data.context import ScenarioContext
from qaforge.data.generators import DataGenerator
from qaforge.nocode import registry
from qaforge.nocode.keywords import BY_NAME
from qaforge.nocode.locator_expr import build_expr
from qaforge.nocode.spec import Spec, Step, load_spec

REF = re.compile(r"\$\{([^}]+)\}")
MAX_WAIT_S = 30
MAX_REUSE_DEPTH = 5


class NoCodeStepError(AssertionError):
    """Raised with the step number and plain-language description so testers can find the problem."""


def _humanize(name: str) -> str:
    text = name.replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def _fmt(v: Any) -> str:
    if isinstance(v, BaseModel):
        return v.model_dump_json()
    return str(v)


def _parse_kv(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for line in str(value or "").replace(";", "\n").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


class SpecRunner:
    def __init__(self, spec: Spec, row: dict[str, Any] | None, *, root: Path, open_app: Callable[..., AppSession],
                 ctx: ScenarioContext, events: Any, verify: Callable[..., Any], evidence: EvidencePack,
                 settings: Settings, secrets: SecretProvider, gen: DataGenerator) -> None:
        self.spec, self.row, self.root = spec, row, root
        self.open_app, self.ctx, self.events, self.verify = open_app, ctx, events, verify
        self.evidence, self.settings, self.secrets, self.gen = evidence, settings, secrets, gen
        self.sessions: dict[str, AppSession] = {}
        self.current: AppSession | None = None
        self._pages: dict[tuple[str, type], BasePage] = {}
        self._depth = 0

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        title = self.spec.title + (f" [{self.row.get('id', 'row')}]" if self.row else "")
        self.evidence.test_name = title
        self.evidence.note(f"No-code test '{self.spec.id}' from tests/nocode/specs/{self.spec.id}.yaml")
        if self.row is not None:
            self.ctx.save("row", self.row)
        self._build_data(self.spec)
        self._run_steps(self.spec, prefix="")

    def _build_data(self, spec: Spec) -> None:
        facs = registry.factories()
        for item in spec.data:
            if item.factory not in facs:
                raise NoCodeStepError(f"Data '{item.name}': unknown kind of data '{item.factory}'")
            overrides = {k: self.resolve(v) for k, v in item.overrides.items()}
            self.ctx.save(item.name, facs[item.factory].build(**overrides))

    def _run_steps(self, spec: Spec, prefix: str) -> None:
        for i, step in enumerate(spec.steps, 1):
            if not step.enabled:
                continue
            label = self.step_label(step)
            number = f"{prefix}{i}"
            try:
                _IMPL[step.action](self, step)
            except NoCodeStepError:
                raise
            except Exception as exc:  # noqa: BLE001 - rewrap with the tester's step number
                detail = str(exc).split("\nCall log:")[0].strip()
                raise NoCodeStepError(f"Step {number} '{label}' failed: {type(exc).__name__}: {detail}") from exc

    # -------------------------------------------------------------- helpers
    @property
    def session(self) -> AppSession:
        if self.current is None:
            raise NoCodeStepError("No app is open yet - add an 'Open app and sign in' step first")
        return self.current

    def step_label(self, step: Step) -> str:
        if step.description:
            return step.description
        kw = BY_NAME[step.action]
        what = ""
        if step.target:
            what = _humanize(step.target.rpartition(".")[2]) if kw.target != "page" else step.target.split(".")[-1]
        elif step.locator:
            what = step.locator if len(step.locator) < 60 else step.locator[:57] + "..."
        return f"{kw.label} {what}".strip()

    def element_label(self, step: Step) -> str:
        if step.description:
            return step.description
        if step.target:
            return _humanize(step.target.rpartition(".")[2])
        return step.locator or "element"

    def resolve(self, value: Any) -> Any:
        if isinstance(value, str):
            m = REF.fullmatch(value.strip())
            if m:
                return self._ref(m.group(1).strip())
            return REF.sub(lambda mm: _fmt(self._ref(mm.group(1).strip())), value)
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        return value

    def _ref(self, ref: str) -> Any:
        if ref.startswith("secret:"):
            return self.secrets.get(ref[7:].strip())
        if ref.startswith("gen."):
            method = ref[4:]
            fn = getattr(self.gen, method, None)
            if method.startswith("_") or not callable(fn):
                raise NoCodeStepError(f"'{ref}' is not a known generated value")
            return fn()
        if ref == "run_tag":
            return self.gen.run_tag
        if ref == "today":
            return date.today().isoformat()
        try:
            return self.ctx.get_path(ref)
        except (KeyError, AttributeError, IndexError, ValueError) as exc:
            raise NoCodeStepError(f"'${{{ref}}}' has no value yet - generate or remember it in an earlier step") \
                from exc

    def page_object(self, page_ref: str) -> BasePage:
        cls = registry.load_page_class(page_ref, self.root)
        key = (self.session.label, cls)
        if key not in self._pages:
            self._pages[key] = cls(self.session)
        return self._pages[key]

    def target(self, step: Step) -> Any:
        """Locator (or component) for a step: a page-object element, or an inline locator."""
        if step.target:
            page_ref, attr = registry.split_element_ref(step.target)
            page = self.page_object(page_ref)
            if not hasattr(page, attr):
                raise NoCodeStepError(f"Page {page_ref} has no element '{attr}'")
            return getattr(page, attr)
        return build_expr(str(self.resolve(step.locator)), self.session.page)

    def locator(self, step: Step) -> Locator | LocatorChain:
        t = self.target(step)
        if isinstance(t, BaseComponent):
            return t.root
        if not isinstance(t, (Locator, LocatorChain)):
            raise NoCodeStepError(f"'{step.target}' is not a screen element")
        return t

    def rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for r in rows or []:
            rule = r.get("rule") or "equals"
            expected = r.get("expected")
            if rule != "exists" and isinstance(expected, str) and not expected.startswith("ctx."):
                expected = self.resolve(expected)
            out.append({"field": r["field"], "expected": expected, "rule": rule})
        return out


# ---------------------------------------------------------------------- keywords
def _open_app(r: SpecRunner, s: Step) -> None:
    app, role = s.options["app"], s.options.get("role") or None
    name = s.options.get("as") or app
    r.sessions[name] = r.open_app(app, role=role)
    r.current = r.sessions[name]


def _switch_app(r: SpecRunner, s: Step) -> None:
    name = s.options["session"]
    if name not in r.sessions:
        raise NoCodeStepError(f"No app session named '{name}' is open (open it first)")
    r.current = r.sessions[name]


def _open_page(r: SpecRunner, s: Step) -> None:
    page = r.page_object(str(s.target))
    if page.path:
        page.open()
    else:
        page.wait_until_loaded()


def _goto_url(r: SpecRunner, s: Step) -> None:
    url = str(r.resolve(s.value))
    if url.startswith("/"):
        url = r.session.base_url.rstrip("/") + url
    r.session.act.goto(url, s.description or f"Go to {url}")


def _click(r: SpecRunner, s: Step) -> None:
    r.session.act.click(r.locator(s), r.element_label(s))


def _fill(r: SpecRunner, s: Step) -> None:
    sensitive = bool(s.options.get("sensitive")) or "${secret:" in str(s.value)
    r.session.act.fill(r.locator(s), r.resolve(s.value), r.element_label(s), sensitive=sensitive)


def _select(r: SpecRunner, s: Step) -> None:
    value = str(r.resolve(s.value))
    if s.options.get("by") == "value":
        r.session.act.select(r.locator(s), r.element_label(s), value=value)
    else:
        r.session.act.select(r.locator(s), r.element_label(s), label=value)


def _check(r: SpecRunner, s: Step) -> None:
    r.session.act.check(r.locator(s), r.element_label(s), True)


def _uncheck(r: SpecRunner, s: Step) -> None:
    r.session.act.check(r.locator(s), r.element_label(s), False)


def _press(r: SpecRunner, s: Step) -> None:
    r.session.act.press(r.locator(s), str(r.resolve(s.value)), r.element_label(s))


def _upload(r: SpecRunner, s: Step) -> None:
    base = (r.root / "testdata" / "files").resolve()
    path = (base / str(r.resolve(s.value))).resolve()
    if base not in path.parents or not path.exists():
        raise NoCodeStepError(f"File '{s.value}' not found in testdata/files")
    r.session.act.upload(r.locator(s), str(path), r.element_label(s))


def _expect_visible(r: SpecRunner, s: Step) -> None:
    r.session.act.expect_visible(r.locator(s), r.element_label(s))


def _expect_hidden(r: SpecRunner, s: Step) -> None:
    r.session.act.expect_hidden(r.locator(s), r.element_label(s))


def _expect_text(r: SpecRunner, s: Step) -> None:
    r.session.act.expect_text(r.locator(s), str(r.resolve(s.value)), r.element_label(s),
                              exact=bool(s.options.get("exact")))


def _expect_value(r: SpecRunner, s: Step) -> None:
    r.session.act.expect_value(r.locator(s), str(r.resolve(s.value)), r.element_label(s))


def _expect_url(r: SpecRunner, s: Step) -> None:
    r.session.act.expect_url(re.compile(str(r.resolve(s.value))))


def _assert_value(r: SpecRunner, s: Step) -> None:
    actual = r.resolve(s.value)
    rule = s.options.get("rule") or "equals"
    expected = r.resolve(s.options.get("expected")) if rule != "exists" else None
    r.verify({"value": actual}, [{"field": "value", "expected": expected, "rule": rule}],
             source="test", name=s.description or f"{s.value} {rule} {s.options.get('expected', '')}".strip())


def _verify_values(r: SpecRunner, s: Step) -> None:
    source = s.options["source"]
    r.verify(r.resolve("${" + source + "}"), r.rows(s.options.get("rows") or []), source=f"saved:{source}",
             name=s.description or f"Check saved '{source}'")


def _capture_text(r: SpecRunner, s: Step) -> None:
    r.ctx.save(str(s.save_as), r.session.act.read_text(r.locator(s), r.element_label(s)))


def _capture_value(r: SpecRunner, s: Step) -> None:
    r.ctx.save(str(s.save_as), r.session.act.read_value(r.locator(s), r.element_label(s)))


def _read_grid_row(r: SpecRunner, s: Step) -> None:
    t = r.target(s)
    if isinstance(t, LocatorChain):
        t = r.session.act.resolve(t, r.element_label(s))
    grid = t if isinstance(t, DataGrid) else DataGrid(t.root if isinstance(t, BaseComponent) else t,
                                                       r.session, r.element_label(s))
    criteria = {k: str(v) for k, v in _parse_kv(r.resolve(s.value)).items()}
    if not criteria:
        raise NoCodeStepError("Say which row to read, e.g. Reference=${txn_ref}")
    r.ctx.save(str(s.save_as), grid.read_row(**criteria))


def _generate_data(r: SpecRunner, s: Step) -> None:
    facs = registry.factories()
    name = s.options["factory"]
    if name not in facs:
        raise NoCodeStepError(f"Unknown kind of data '{name}'")
    overrides = {k: r.resolve(v) for k, v in _parse_kv(s.options.get("overrides")).items()}
    r.ctx.save(str(s.save_as), facs[name].build(**overrides))


def _set_value(r: SpecRunner, s: Step) -> None:
    r.ctx.save(str(s.save_as), r.resolve(s.value))


def _mark_created(r: SpecRunner, s: Step) -> None:
    r.ctx.mark_created(s.options["key"], entity=s.options["entity"])


def _verify_event(r: SpecRunner, s: Step) -> None:
    mapping: Any = s.options.get("mapping") or r.rows(s.options.get("rows") or [])
    key = r.resolve(s.options.get("key")) if s.options.get("key") else None
    timeout = float(s.options["timeout_s"]) if s.options.get("timeout_s") else None
    r.events.expect_and_verify(s.options["topic"], mapping, key=str(key) if key is not None else None,
                               timeout_s=timeout)


def _expect_no_event(r: SpecRunner, s: Step) -> None:
    where = None
    if s.options.get("field"):
        where = {s.options["field"]: r.resolve(s.options.get("equals", ""))}
    key = r.resolve(s.options.get("key")) if s.options.get("key") else None
    r.events.expect_none(s.options["topic"], key=str(key) if key is not None else None, where=where,
                         within_s=float(s.options.get("within_s") or 10))


def _use_steps(r: SpecRunner, s: Step) -> None:
    if r._depth >= MAX_REUSE_DEPTH:
        raise NoCodeStepError("Reusable steps are nested too deeply (check for a loop)")
    sub = load_spec(r.root, s.options["spec"])
    r._depth += 1
    try:
        with r.session.act.group(sub.title) if r.current else _null():
            r._build_data(sub)
            r._run_steps(sub, prefix=f"{s.options['spec']}.")
    finally:
        r._depth -= 1


class _null:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *a: object) -> None:
        return None


def _coerce(value: Any, annotation: Any) -> Any:
    origin = typing.get_origin(annotation)
    target = annotation if origin is None else None
    if inspect_is_model(target) and isinstance(value, dict):
        return target.model_validate(value)
    if target is Decimal and not isinstance(value, Decimal):
        return Decimal(str(value))
    return value


def inspect_is_model(t: Any) -> bool:
    return isinstance(t, type) and issubclass(t, BaseModel)


def _run_flow(r: SpecRunner, s: Step) -> None:
    cls, method = registry.load_flow(s.options["flow"], r.root)
    fn = getattr(cls, method)
    try:
        hints = typing.get_type_hints(fn)
    except Exception:  # noqa: BLE001
        hints = {}
    args = {k: _coerce(r.resolve(v), hints.get(k)) for k, v in _parse_kv(s.options.get("args")).items()}
    result = getattr(cls(r.session), method)(**args)
    if s.save_as:
        r.ctx.save(s.save_as, result)


def _checkpoint(r: SpecRunner, s: Step) -> None:
    r.session.act.checkpoint(str(r.resolve(s.value)))


def _note(r: SpecRunner, s: Step) -> None:
    r.evidence.note(str(r.resolve(s.value)))


def _wait(r: SpecRunner, s: Step) -> None:
    seconds = min(float(r.resolve(s.value)), MAX_WAIT_S)
    started = utc_now_iso()
    time.sleep(seconds)
    r.evidence.add_step(f"Waited {seconds:g}s", "passed", started, int(seconds * 1000),
                        detail="Fixed waits slow runs down; prefer 'Check shown'")


_IMPL: dict[str, Callable[[SpecRunner, Step], None]] = {
    "open_app": _open_app, "switch_app": _switch_app, "open_page": _open_page, "goto_url": _goto_url,
    "click": _click, "fill": _fill, "select": _select, "check": _check, "uncheck": _uncheck, "press": _press,
    "upload": _upload, "expect_visible": _expect_visible, "expect_hidden": _expect_hidden,
    "expect_text": _expect_text, "expect_value": _expect_value, "expect_url": _expect_url,
    "assert_value": _assert_value, "verify_values": _verify_values, "capture_text": _capture_text,
    "capture_value": _capture_value, "read_grid_row": _read_grid_row, "generate_data": _generate_data,
    "set_value": _set_value, "mark_created": _mark_created, "verify_event": _verify_event,
    "expect_no_event": _expect_no_event, "use_steps": _use_steps, "run_flow": _run_flow,
    "checkpoint": _checkpoint, "note": _note, "wait": _wait,
}

missing = set(BY_NAME) - set(_IMPL)
assert not missing, f"keywords without implementation: {missing}"
