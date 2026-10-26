from __future__ import annotations

from playwright.sync_api import Locator

from qaforge.core.base_page import BasePage


class DashboardPage(BasePage):
    path = "/retail/dashboard"
    title = "Retail dashboard"

    def __init__(self, session) -> None:  # type: ignore[no-untyped-def]
        super().__init__(session)
        self.heading = self.tid("dashboard-title")
        self.add_beneficiary_link = self.tid("nav-add-beneficiary")
        self.transfer_link = self.tid("nav-transfer")

    def loaded_anchor(self) -> Locator:
        return self.tid("dashboard-title")

    def sensitive_locators(self) -> list[Locator]:
        return [self.tid("primary-account"), self.tid("available-balance")]

    def go_to_add_beneficiary(self) -> None:
        self.act.click(self.add_beneficiary_link, "Menu: Add beneficiary")

    def go_to_transfer(self) -> None:
        self.act.click(self.transfer_link, "Menu: Transfer funds")
