"""Auditable fallback locators - rule-based, ordered, never silent. No AI, no guessing.

A page object can give an element an ordered list of locators:

    self.amount = self.chain(self.tid("tr-amount"),                       # primary
                             self.page.get_by_label("Amount (INR)"),      # fallback 1
                             self.page.locator("#ta"), name="amount")     # fallback 2

At run time (core/actions.py) the primary is used whenever it finds exactly one element.
Only when it finds none or several does the next locator in the list get a chance, and only
if THAT one finds exactly one element. Every fallback:
  * adds a loud "LOCATOR FALLBACK USED" step (with screenshot) to the evidence,
  * is listed in the test record and in <run>/locator_fallbacks.json,
  * flags the test "needs locator fix" in reports, Studio and JIRA comments,
  * or fails the test outright when the environment sets locators.fallback_policy: fail
    (useful for certification runs where any drift must stop the run).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from playwright.sync_api import Locator


def describe_locator(loc: Locator) -> str:
    text = repr(loc)
    m = re.search(r"selector='(.*)'>$", text)
    return m.group(1) if m else text


@dataclass
class LocatorChain:
    name: str
    candidates: list[Locator] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ValueError(f"Locator chain '{self.name}' needs at least one locator")

    @property
    def primary(self) -> Locator:
        return self.candidates[0]

    @property
    def fallbacks(self) -> list[Locator]:
        return self.candidates[1:]

    def any_of(self) -> Locator:
        combined = self.candidates[0]
        for loc in self.candidates[1:]:
            combined = combined.or_(loc)
        return combined

    def descriptions(self) -> list[str]:
        return [describe_locator(c) for c in self.candidates]


class LocatorFallbackError(AssertionError):
    """Raised when fallback_policy is 'fail' and the primary locator no longer works."""
