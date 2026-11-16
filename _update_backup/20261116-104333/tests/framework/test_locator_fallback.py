"""Fallback locators are used only when the primary fails, and are never silent."""
from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from qaforge.compliance.evidence import EvidencePack, RunInfo
from qaforge.config.settings import Settings
from qaforge.core.actions import Actions
from qaforge.core.locator_chain import LocatorChain, LocatorFallbackError

HTML = """<label for="a">Amount</label><input id="a" data-testid="amount-new">
<button data-testid="save">Save</button>"""


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        yield pg
        b.close()


def _actions(page, tmp_path: Path, policy: str = "warn") -> Actions:
    page.set_content(HTML)
    settings = Settings.model_validate({"env": "local", "locators": {"fallback_policy": policy},
                                        "evidence": {"screenshot": "off"}})
    pack = EvidencePack(RunInfo.create("R", "local", tmp_path, {}), tmp_path, "t::x", "x", [], [], "off", False)
    return Actions(page, pack, settings, app="demo")


def test_primary_used_when_it_works(page, tmp_path):
    act = _actions(page, tmp_path)
    act.click(LocatorChain("save", [page.get_by_test_id("save"), page.get_by_role("button", name="Save")]), "Save")
    assert act.evidence.locator_fallbacks == []
    assert not [s for s in act.evidence.steps if s.status == "fallback"]


def test_fallback_is_used_and_reported(page, tmp_path):
    act = _actions(page, tmp_path)
    chain = LocatorChain("amount", [page.get_by_test_id("amount"), page.get_by_label("Amount", exact=True)])
    act.fill(chain, "1500", "Amount")
    assert page.get_by_label("Amount").input_value() == "1500"
    fb = act.evidence.locator_fallbacks
    assert len(fb) == 1 and fb[0]["element"] == "amount" and fb[0]["primary_count"] == 0
    loud = [s for s in act.evidence.steps if s.status == "fallback"]
    assert loud and loud[0].name == "LOCATOR FALLBACK USED: amount"
    record = act.evidence.finalize("passed", None)
    assert record["needs_locator_fix"] is True


def test_fail_policy_stops_the_test(page, tmp_path):
    act = _actions(page, tmp_path, policy="fail")
    chain = LocatorChain("amount", [page.get_by_test_id("amount"), page.get_by_label("Amount", exact=True)])
    with pytest.raises(LocatorFallbackError, match="LOCATOR FALLBACK USED"):
        act.fill(chain, "1", "Amount")


def test_no_candidate_fails_with_primary_error(page, tmp_path):
    act = _actions(page, tmp_path)
    page.set_default_timeout(1500)
    chain = LocatorChain("ghost", [page.get_by_test_id("nope"), page.get_by_test_id("nope2")])
    with pytest.raises(Exception, match="nope"):
        act.click(chain, "Ghost")
    assert act.evidence.locator_fallbacks == []


def test_common_wrapped_actions_work(page, tmp_path):
    act = _actions(page, tmp_path)
    page.set_content("""<button id="source" draggable="true">Source</button>
    <div id="target">Target</div>
    <script>window.__ready = 'no';</script>""")
    act.hover(page.locator("#source"), "Source")
    assert act.evaluate("(function(){ window.__ready = 'yes'; return window.__ready; })()", "Set flag") == "yes"
    act.drag_and_drop(page.locator("#source"), page.locator("#target"), "Move item")
    act.wait_for_load_state("domcontentloaded", "Page ready")
