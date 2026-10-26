"""Base classes for page objects, reusable components and business flows.

Rules of the road (enforced in review / lint):
  * locators live in page objects only, defined once in __init__ or as properties
  * prefer data-testid -> role/label -> CSS; XPath only with a comment explaining why
  * page methods describe user intent ("submit_transfer"), not mechanics ("click_button3")
  * page methods never assert business outcomes - flows and tests do
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from playwright.sync_api import Locator, Page

from qaforge.core.locator_chain import LocatorChain

if TYPE_CHECKING:
    from qaforge.core.session import AppSession


class BasePage:
    #: path relative to the app's base_url, e.g. "/retail/transfer"
    path: str = ""
    #: human name used in evidence
    title: str = ""

    def __init__(self, session: AppSession) -> None:
        self.session = session
        self.page: Page = session.page
        self.act = session.act
        self.settings = session.settings
        self.title = self.title or type(self).__name__
        # evaluated at screenshot time, after the subclass has defined its locators
        self.act.register_sensitive_provider(self.sensitive_locators)

    # -- conveniences -------------------------------------------------------
    @property
    def url(self) -> str:
        return urljoin(self.session.base_url.rstrip("/") + "/", self.path.lstrip("/"))

    def tid(self, test_id: str) -> Locator:
        return self.page.get_by_test_id(test_id)

    def chain(self, *locators: Locator, name: str) -> LocatorChain:
        """Ordered fallback locators: primary first. Any fallback is reported loudly in the evidence."""
        return LocatorChain(name, list(locators))

    # -- lifecycle ----------------------------------------------------------
    def open(self) -> BasePage:
        self.act.goto(self.url, f"Open {self.title}")
        self.wait_until_loaded()
        return self

    def wait_until_loaded(self) -> None:
        anchor = self.loaded_anchor()
        if anchor is not None:
            self.act.expect_visible(anchor, f"{self.title} loaded")

    # -- overridables -------------------------------------------------------
    def loaded_anchor(self) -> Locator | LocatorChain | None:
        """An element that proves the page is ready (heading, form, grid)."""
        return None

    def sensitive_locators(self) -> list[Locator | LocatorChain]:
        """Elements to black out in screenshots (account numbers, balances, PII)."""
        return []


class BaseComponent:
    """A reusable widget scoped to a root locator (grid, date picker, modal...)."""

    def __init__(self, root: Locator, session: AppSession, name: str) -> None:
        self.root = root
        self.session = session
        self.act = session.act
        self.name = name


class BaseFlow:
    """Business journeys composed from pages. Flows return domain results, e.g. a receipt."""

    def __init__(self, session: AppSession, **deps: Any) -> None:
        self.session = session
        self.act = session.act
        self.settings = session.settings
        for k, v in deps.items():
            setattr(self, k, v)
