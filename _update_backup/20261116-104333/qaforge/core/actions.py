"""Audited actions - the ONLY way tests and page objects should touch the browser.

Each action:
  * logs a structured, masked step (who/what/where/when)
  * waits using Playwright auto-waiting + explicit expectations
  * takes a masked screenshot according to the evidence policy
  * on failure: captures a failure screenshot, records the error, re-raises

Page objects call `self.act.click(self.submit, "Submit transfer")`, never `locator.click()`.

Every action accepts a plain Locator or a LocatorChain (ordered fallbacks, see
core/locator_chain.py). Fallbacks are rule-based and always reported loudly in the evidence.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page
from playwright.sync_api import expect as pw_expect

from qaforge.compliance.evidence import EvidencePack
from qaforge.compliance.logger import bind, get_logger, unbind, utc_now_iso
from qaforge.compliance.masker import FULL_MASK, masker
from qaforge.config.settings import Settings
from qaforge.core.locator_chain import LocatorChain, LocatorFallbackError, describe_locator

Target = Locator | LocatorChain

T = TypeVar("T")
log = get_logger("actions")


class Actions:
    def __init__(self, page: Page, evidence: EvidencePack, settings: Settings, app: str | None = None) -> None:
        self.page = page
        self.evidence = evidence
        self.settings = settings
        self.app = app
        self._sensitive: list[Locator] = []
        self._sensitive_providers: list[Callable[[], list[Locator]]] = []
        self._group: list[str] = []
        page.set_default_timeout(settings.browser.default_timeout_ms)
        page.set_default_navigation_timeout(settings.browser.navigation_timeout_ms)

    # --------------------------------------------------------- masking
    def register_sensitive(self, *locators: Target) -> None:
        """Locators blacked out in every screenshot taken by this session."""
        for loc in locators:
            self._sensitive.extend(loc.candidates if isinstance(loc, LocatorChain) else [loc])

    def register_sensitive_provider(self, provider: Callable[[], list[Locator]]) -> None:
        """Page objects register a callable; it is evaluated at each screenshot."""
        if provider not in self._sensitive_providers:
            self._sensitive_providers.append(provider)
        if len(self._sensitive_providers) > 50:  # long journeys create many page objects
            self._sensitive_providers = self._sensitive_providers[-50:]

    def _mask_locators(self) -> list[Locator]:
        dynamic: list[Locator] = []
        for provider in self._sensitive_providers:
            try:
                for loc in provider():
                    dynamic.extend(loc.candidates if isinstance(loc, LocatorChain) else [loc])
            except Exception:  # noqa: BLE001 - a broken provider must not block evidence
                continue
        return self._sensitive + dynamic + [self.page.locator(sel) for sel in self.settings.evidence.mask_selectors]

    # ------------------------------------------------------- screenshots
    def screenshot(self, label: str, full_page: bool | None = None) -> Any:
        path = self.evidence.next_screenshot_path(label)
        try:
            self.page.screenshot(
                path=str(path),
                full_page=self.settings.evidence.full_page_screenshots if full_page is None else full_page,
                mask=self._mask_locators(),
                mask_color="#1d2433",
                animations="disabled",
            )
            return path
        except Exception as exc:  # noqa: BLE001 - evidence capture must not hide the real error
            log.warning("Screenshot failed for '%s': %s", label, exc)
            return None

    # ------------------------------------------------------------ core
    def _describe(self, target: Target | str | None) -> str | None:
        if target is None:
            return None
        if isinstance(target, LocatorChain):
            return " | ".join(target.descriptions())
        return describe_locator(target) if isinstance(target, Locator) else str(target)

    def resolve(self, target: Target, name: str, wait: bool = True) -> Locator:
        """Pick the locator to act on. Plain locators pass straight through.

        For a chain: wait until ANY candidate is attached (so a slow page never triggers a
        false fallback), then take the first candidate - in the declared order - that finds
        exactly one element. Anything other than the primary is reported as a fallback.
        """
        if not isinstance(target, LocatorChain):
            return target
        if wait:
            try:
                target.any_of().first.wait_for(state="attached")
            except PlaywrightError:
                return target.primary  # nothing matches at all: fail with the primary's normal error
        counts = [c.count() for c in target.candidates]
        if counts[0] == 1:
            return target.primary
        for i, n in enumerate(counts[1:], start=1):
            if n == 1:
                self._report_fallback(target, name, i, counts)
                return target.candidates[i]
        return target.primary  # ambiguous or missing everywhere: let Playwright raise its strict error

    def _report_fallback(self, chain: LocatorChain, name: str, used: int, counts: list[int]) -> None:
        descs = chain.descriptions()
        why = "found nothing" if counts[0] == 0 else f"found {counts[0]} elements"
        detail = (f"Primary locator {descs[0]} {why}; used fallback #{used} {descs[used]}. "
                  f"Update the page object '{chain.name}' so the primary works again.")
        record = {"element": chain.name, "step": name, "app": self.app, "primary": descs[0],
                  "primary_count": counts[0], "used": descs[used], "fallback_no": used, "at": utc_now_iso()}
        self.evidence.locator_fallbacks.append(record)
        shot = self.screenshot(f"LOCATOR_FALLBACK_{chain.name}")
        policy = self.settings.locators.fallback_policy
        self.evidence.add_step(f"LOCATOR FALLBACK USED: {chain.name}", "fallback" if policy == "warn" else "failed",
                               utc_now_iso(), 0, app=self.app, detail=detail, screenshot=shot)
        log.warning("LOCATOR FALLBACK USED for '%s' in step '%s': %s", chain.name, name, detail,
                    extra={"data": record})
        if policy == "fail":
            raise LocatorFallbackError(f"LOCATOR FALLBACK USED for '{chain.name}' and this environment does not "
                                       f"allow fallbacks (locators.fallback_policy: fail). {detail}")

    def run(
        self,
        name: str,
        fn: Callable[[], T],
        target: Target | str | None = None,
        detail: str | None = None,
        screenshot: bool = True,
    ) -> T:
        full_name = " › ".join([*self._group, name]) if self._group else name
        started, t0 = utc_now_iso(), time.monotonic()
        bind(step=full_name)
        log.info("STEP %s", full_name, extra={"data": {"target": self._describe(target), "detail": detail}})
        try:
            result = fn()
        except Exception as exc:
            shot = self.screenshot(f"FAILED_{name}")
            self.evidence.add_step(full_name, "failed", started, int((time.monotonic() - t0) * 1000),
                                   app=self.app, detail=detail or self._describe(target), screenshot=shot,
                                   error=f"{type(exc).__name__}: {exc}".split("\nCall log:")[0][:1500])
            log.error("FAILED %s: %s", full_name, exc)
            unbind("step")
            raise
        shot = None
        if screenshot and self.settings.evidence.screenshot in ("each_step", "on_failure"):
            shot = self.screenshot(name)
        self.evidence.add_step(full_name, "passed", started, int((time.monotonic() - t0) * 1000),
                               app=self.app, detail=detail, screenshot=shot)
        unbind("step")
        return result

    @contextmanager
    def group(self, title: str) -> Iterator[None]:
        """Group several actions under a business step: `with act.group("Add beneficiary"): ...`"""
        self._group.append(title)
        try:
            yield
        finally:
            self._group.pop()

    # ---------------------------------------------------------- actions
    def goto(self, url: str, name: str | None = None) -> None:
        self.run(name or f"Navigate to {url}", lambda: self.page.goto(url, wait_until="domcontentloaded"),
                 detail=url)

    def click(self, locator: Target, name: str) -> None:
        self.run(name, lambda: self.resolve(locator, name).click(), target=locator)

    def fill(self, locator: Target, value: Any, name: str, sensitive: bool = False) -> None:
        text = "" if value is None else str(value)
        if sensitive:
            masker.register_secret(text)
        shown = FULL_MASK if sensitive else masker.mask(text)
        self.run(f"{name} = {shown}", lambda: self.resolve(locator, name).fill(text), target=locator,
                 screenshot=not sensitive)

    def type_slowly(self, locator: Target, value: str, name: str, delay_ms: int = 40,
                    sensitive: bool = False) -> None:
        shown = FULL_MASK if sensitive else masker.mask(value)
        self.run(f"{name} = {shown}", lambda: self.resolve(locator, name).press_sequentially(value, delay=delay_ms), target=locator)

    def select(self, locator: Target, name: str, *, label: str | None = None, value: str | None = None) -> None:
        def _do() -> None:
            if label is not None:
                self.resolve(locator, name).select_option(label=label)
            else:
                self.resolve(locator, name).select_option(value=value)

        self.run(f"{name} = {label if label is not None else value}", _do, target=locator)

    def check(self, locator: Target, name: str, checked: bool = True) -> None:
        self.run(f"{name} = {'on' if checked else 'off'}", lambda: self.resolve(locator, name).set_checked(checked), target=locator)

    def press(self, locator: Target, key: str, name: str) -> None:
        self.run(f"{name} [{key}]", lambda: self.resolve(locator, name).press(key), target=locator)

    def hover(self, locator: Target, name: str, force: bool = False) -> None:
        self.run(f"Hover {name}", lambda: self.resolve(locator, name).hover(force=force), target=locator)

    def double_click(self, locator: Target, name: str) -> None:
        self.run(f"Double click {name}", lambda: self.resolve(locator, name).dblclick(), target=locator)

    def right_click(self, locator: Target, name: str) -> None:
        self.run(f"Right click {name}", lambda: self.resolve(locator, name).click(button="right"), target=locator)

    def drag_and_drop(self, source: Target, target: Target, name: str) -> None:
        self.run(f"Drag and drop {name}", lambda: self.resolve(source, name).drag_to(self.resolve(target, name)),
                 target=source)

    def clear(self, locator: Target, name: str) -> None:
        self.run(f"Clear {name}", lambda: self.resolve(locator, name).clear(), target=locator)

    def focus(self, locator: Target, name: str) -> None:
        self.run(f"Focus {name}", lambda: self.resolve(locator, name).focus(), target=locator)

    def blur(self, locator: Target, name: str) -> None:
        self.run(f"Blur {name}", lambda: self.resolve(locator, name).blur(), target=locator)

    def wait_for_load_state(self, state: str = "networkidle", name: str = "page state") -> None:
        self.run(f"Wait for {name} ({state})", lambda: self.page.wait_for_load_state(state), screenshot=False)

    def wait_for_url(self, pattern: str | re.Pattern[str], name: str = "page URL", timeout_ms: int | None = None) -> None:
        self.run(f"Verify {name} matches {pattern}", lambda: pw_expect(self.page).to_have_url(pattern, timeout=timeout_ms),
                 screenshot=False)

    def evaluate(self, expression: str, arg: Any = None, name: str = "evaluate") -> Any:
        return self.run(f"Evaluate {name}", lambda: self.page.evaluate(expression, arg), screenshot=False)

    def accept_dialog(self, name: str = "Accept dialog", timeout_ms: int | None = None) -> None:
        def _do() -> None:
            dialog = self.page.wait_for_event("dialog", timeout=timeout_ms)
            dialog.accept()
        self.run(name, _do, screenshot=False)

    def dismiss_dialog(self, name: str = "Dismiss dialog", timeout_ms: int | None = None) -> None:
        def _do() -> None:
            dialog = self.page.wait_for_event("dialog", timeout=timeout_ms)
            dialog.dismiss()
        self.run(name, _do, screenshot=False)

    def upload(self, locator: Target, file_path: str, name: str) -> None:
        self.run(f"{name} = {file_path}", lambda: self.resolve(locator, name).set_input_files(file_path), target=locator)

    def read_text(self, locator: Target, name: str) -> str:
        """Capture a value displayed by the app (e.g. a transaction reference)."""
        value = self.run(f"Capture {name}", lambda: self.resolve(locator, name).inner_text().strip(), target=locator,
                         screenshot=False)
        self.evidence.add_step(f"Captured {name} = {masker.mask(value)}", "info", utc_now_iso(), 0, app=self.app)
        return value

    def read_value(self, locator: Target, name: str) -> str:
        return self.run(f"Capture {name}", lambda: self.resolve(locator, name).input_value(), target=locator, screenshot=False)

    # ------------------------------------------------------- assertions
    def expect_visible(self, locator: Target, name: str, timeout_ms: int | None = None) -> None:
        self.run(f"Verify visible: {name}", lambda: pw_expect(self.resolve(locator, name)).to_be_visible(timeout=timeout_ms),
                 target=locator)

    def expect_hidden(self, locator: Target, name: str, timeout_ms: int | None = None) -> None:
        self.run(f"Verify hidden: {name}", lambda: pw_expect(self.resolve(locator, name, wait=False)).to_be_hidden(timeout=timeout_ms),
                 target=locator, screenshot=False)

    def expect_text(self, locator: Target, expected: str | re.Pattern[str], name: str,
                    exact: bool = False) -> None:
        def _do() -> None:
            if exact or isinstance(expected, re.Pattern):
                pw_expect(self.resolve(locator, name)).to_have_text(expected)
            else:
                pw_expect(self.resolve(locator, name)).to_contain_text(expected)

        self.run(f"Verify {name} shows '{masker.mask(expected)}'", _do, target=locator)

    def expect_value(self, locator: Target, expected: str, name: str) -> None:
        self.run(f"Verify {name} value '{masker.mask(expected)}'", lambda: pw_expect(self.resolve(locator, name)).to_have_value(expected),
                 target=locator)

    def expect_url(self, pattern: str | re.Pattern[str], name: str = "page URL") -> None:
        self.run(f"Verify {name} matches {pattern}", lambda: pw_expect(self.page).to_have_url(pattern),
                 screenshot=False)

    def checkpoint(self, name: str) -> None:
        """Explicit evidence screenshot at a business milestone."""
        self.run(f"Checkpoint: {name}", lambda: None)
