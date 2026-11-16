"""Self-tests for the no-code layer: locator parsing, recordings, generated page objects, specs."""
from __future__ import annotations

from pathlib import Path

import pytest

from qaforge.nocode.codegen_import import import_recording
from qaforge.nocode.locator_expr import LocatorExprError, canonical, parse, to_python
from qaforge.nocode.pom_writer import ElementDef, GeneratedPage, PomError, save_generated
from qaforge.nocode.registry import locator_index, read_pages
from qaforge.nocode.spec import Spec

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("bad", [
    '__import__("os").system("x")', 'page.get_by_role("x").evaluate("1")', "page.locator(open('f').read())",
    "page.locator(x)", "lambda: 1", "page.get_by_text(**{'a': 1})", "",
])
def test_unsafe_or_invalid_locators_are_rejected(bad):
    with pytest.raises(LocatorExprError):
        parse(bad)


def test_locators_normalise_between_pom_codegen_and_spy():
    a = canonical("self.page.get_by_role('button', name='Save').first")
    b = canonical('page.get_by_role("button", name="Save").first')
    assert a == b == 'page.get_by_role("button", name="Save").first'
    assert to_python(parse('page.locator("x").filter(has=page.get_by_text("y"))'), "self.page") == \
        'self.page.locator("x").filter(has=self.page.get_by_text("y"))'


def test_pom_reader_returns_exact_locators_and_fallbacks():
    pages = {p.ref: p for p in read_pages(ROOT)}
    tp = pages["retail_banking.TransferPage"]
    els = {e.name: e for e in tp.elements}
    assert els["beneficiary"].locator == 'page.get_by_test_id("tr-beneficiary")'
    assert els["amount"].fallbacks == ['page.get_by_label("Amount (INR)", exact=True)']
    assert locator_index(list(pages.values()))['page.get_by_test_id("tr-mode")'] == "retail_banking.TransferPage.mode"


def test_recording_reuses_pom_elements_and_strips_secrets():
    src = '''def test_x(page):
    page.goto("http://h/retail/transfer")
    page.get_by_test_id("tr-amount").fill("10")
    page.get_by_label("Password").fill("Secret#1")
    page.get_by_role("button", name="Continue").click()
'''
    known = locator_index(read_pages(ROOT))
    res = import_recording(src, "http://h", known, app="retail", role="maker")
    assert res.steps[0] == {"action": "open_app", "options": {"app": "retail", "role": "maker"}}
    assert res.steps[1] == {"action": "goto_url", "value": "/retail/transfer"}
    assert res.steps[2]["target"] == "retail_banking.TransferPage.amount"
    assert res.steps[3]["value"].startswith("${secret:") and "Secret#1" not in str(res.steps)


def test_generated_page_object_compiles_and_protects_hand_written_files(tmp_path):
    page = GeneratedPage(app="crm", class_name="SearchPage", title="Search", path="/s", loaded_anchor="query",
                         elements=[ElementDef(name="query", locator='page.get_by_label("Query")',
                                              fallbacks=['page.locator("#q")'], sensitive=True)])
    py = save_generated(tmp_path, page)
    text = py.read_text()
    assert 'self.chain(self.page.get_by_label("Query"), self.page.locator("#q"), name="query")' in text
    assert "return [self.query]" in text
    hand = tmp_path / "apps" / "crm" / "pages" / "other_page.py"
    hand.write_text("class OtherPage: pass\n")
    with pytest.raises(PomError):
        save_generated(tmp_path, GeneratedPage(app="crm", class_name="OtherPage", title="x"))


def test_spec_validation_explains_problems_in_plain_words():
    with pytest.raises(ValueError, match="needs a screen element"):
        Spec.model_validate({"id": "t_one", "title": "T", "steps": [{"action": "click"}]})
    with pytest.raises(ValueError, match="Not JIRA keys"):
        Spec.model_validate({"id": "t_one", "title": "T", "jira": ["bank 1"], "steps": []})
