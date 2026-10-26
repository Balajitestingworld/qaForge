# qaforge:generated - edit in QA Forge Studio (source: payments_search_page.locators.yaml). Manual edits are overwritten.
from __future__ import annotations

from playwright.sync_api import Locator

from qaforge.core.base_page import BasePage


class PaymentsSearchPage(BasePage):
    "Payments search"

    path = "/backoffice/payments"
    title = "Payments search"

    def __init__(self, session) -> None:  # type: ignore[no-untyped-def]
        super().__init__(session)
        self.app_header = self.page.get_by_test_id("app-header")  # header
        self.signed_in_user = self.page.get_by_test_id("signed-in-user")  # span '******'
        self.payments_queue_link = self.chain(self.page.get_by_test_id("nav-queue"), self.page.get_by_role("link", name="Payments queue", exact=True), name="payments_queue_link")  # link 'Payments queue'
        self.sign_out_link = self.chain(self.page.get_by_role("link", name="Sign out", exact=True), self.page.get_by_text("Sign out", exact=True), name="sign_out_link")  # link 'Sign out'
        self.payments_queue_heading = self.chain(self.page.get_by_test_id("queue-title"), self.page.get_by_role("heading", name="Payments queue", exact=True), name="payments_queue_heading")  # heading 'Payments queue'
        self.bo_payments_grid = self.chain(self.page.get_by_test_id("bo-payments-grid"), self.page.get_by_role("table"), name="bo_payments_grid")  # table

    def loaded_anchor(self) -> Locator:
        return self.app_header
