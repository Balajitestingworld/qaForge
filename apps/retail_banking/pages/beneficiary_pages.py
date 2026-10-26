from __future__ import annotations

from playwright.sync_api import Locator

from qaforge.components import DataGrid
from qaforge.core.base_page import BasePage
from qaforge.data.models import Beneficiary


class AddBeneficiaryPage(BasePage):
    path = "/retail/beneficiaries/new"
    title = "Add beneficiary"

    def __init__(self, session) -> None:  # type: ignore[no-untyped-def]
        super().__init__(session)
        self.name = self.tid("ben-name")
        self.account = self.tid("ben-account")
        self.account_confirm = self.tid("ben-account-confirm")
        self.ifsc = self.tid("ben-ifsc")
        self.nickname = self.tid("ben-nickname")
        self.submit_button = self.tid("ben-submit")
        self.form_error = self.tid("form-error")

    def loaded_anchor(self) -> Locator:
        return self.tid("add-beneficiary-title")

    def sensitive_locators(self) -> list[Locator]:
        return [self.account, self.account_confirm]

    def fill_form(self, b: Beneficiary, confirm_account: str | None = None) -> None:
        self.act.fill(self.name, b.name, "Beneficiary name")
        self.act.fill(self.account, b.account_number, "Account number")
        self.act.fill(self.account_confirm, confirm_account or b.account_number, "Confirm account number")
        self.act.fill(self.ifsc, b.ifsc, "IFSC")
        self.act.fill(self.nickname, b.nickname, "Nickname")

    def submit(self) -> None:
        self.act.click(self.submit_button, "Submit beneficiary")


class BeneficiaryListPage(BasePage):
    path = "/retail/beneficiaries"
    title = "Beneficiaries"

    def __init__(self, session) -> None:  # type: ignore[no-untyped-def]
        super().__init__(session)
        self.flash = self.tid("flash-success")
        self.grid = DataGrid(self.tid("beneficiary-grid"), session, "Beneficiary list")

    def loaded_anchor(self) -> Locator:
        return self.tid("beneficiary-grid")

    def sensitive_locators(self) -> list[Locator]:
        return [self.tid("beneficiary-grid").locator("td[data-sensitive]")]
