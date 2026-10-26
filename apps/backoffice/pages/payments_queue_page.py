from __future__ import annotations

from playwright.sync_api import Locator

from qaforge.components import DataGrid
from qaforge.core.base_page import BasePage


class PaymentsQueuePage(BasePage):
    path = "/backoffice/payments"
    title = "Payments queue"

    def __init__(self, session) -> None:  # type: ignore[no-untyped-def]
        super().__init__(session)
        self.search_box = self.tid("bo-search")
        self.search_button = self.tid("bo-search-btn")
        self.flash = self.tid("flash-success")
        self.grid = DataGrid(self.tid("bo-payments-grid"), session, "Payments queue")

    def loaded_anchor(self) -> Locator:
        return self.tid("queue-title")

    def sensitive_locators(self) -> list[Locator]:
        return [self.tid("bo-payments-grid").locator("td[data-sensitive]")]

    def search(self, reference: str) -> None:
        self.act.fill(self.search_box, reference, "Search reference")
        self.act.click(self.search_button, "Search")

    def approve(self, reference: str) -> None:
        self.act.click(self.tid(f"approve-{reference}"), f"Approve {reference}")
