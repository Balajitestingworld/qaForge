from __future__ import annotations

from decimal import Decimal

from playwright.sync_api import Locator

from qaforge.core.base_page import BasePage
from qaforge.data.models import Beneficiary, Transfer, TransferReceipt


class TransferPage(BasePage):
    path = "/retail/transfer"
    title = "Transfer funds"

    def __init__(self, session) -> None:  # type: ignore[no-untyped-def]
        super().__init__(session)
        self.beneficiary = self.tid("tr-beneficiary")
        self.mode = self.tid("tr-mode")
        # primary + rule-based fallback: if the test id disappears, the label is used and the run is flagged
        self.amount = self.chain(self.tid("tr-amount"), self.page.get_by_label("Amount (INR)", exact=True), name="amount")
        self.remarks = self.tid("tr-remarks")
        self.continue_button = self.tid("tr-continue")
        self.form_error = self.tid("form-error")

    def loaded_anchor(self) -> Locator:
        return self.tid("transfer-title")

    def fill_transfer(self, payee: Beneficiary, t: Transfer) -> None:
        self.act.select(self.beneficiary, "Beneficiary", label=f"{payee.nickname} - {payee.name}")
        self.act.select(self.mode, "Payment mode", label=t.mode)
        self.act.fill(self.amount, f"{t.amount:.2f}", "Amount")
        self.act.fill(self.remarks, t.remarks, "Remarks")

    def continue_to_review(self) -> None:
        self.act.click(self.continue_button, "Continue to review")


class TransferReviewPage(BasePage):
    title = "Review transfer"

    def __init__(self, session) -> None:  # type: ignore[no-untyped-def]
        super().__init__(session)
        self.review_beneficiary = self.tid("rv-beneficiary")
        self.review_account = self.tid("rv-account")
        self.review_ifsc = self.tid("rv-ifsc")
        self.review_mode = self.tid("rv-mode")
        self.review_amount = self.tid("rv-amount")
        self.confirm_button = self.tid("rv-confirm")

    def loaded_anchor(self) -> Locator:
        return self.tid("review-title")

    def sensitive_locators(self) -> list[Locator]:
        return [self.review_account]

    def check_summary(self, payee: Beneficiary, t: Transfer) -> None:
        self.act.expect_text(self.review_beneficiary, payee.name, "review beneficiary", exact=True)
        self.act.expect_text(self.review_ifsc, payee.ifsc, "review IFSC", exact=True)
        self.act.expect_text(self.review_mode, t.mode, "review mode", exact=True)

    def confirm(self) -> None:
        self.act.click(self.confirm_button, "Confirm and pay")


class TransferSuccessPage(BasePage):
    title = "Transfer submitted"

    def __init__(self, session) -> None:  # type: ignore[no-untyped-def]
        super().__init__(session)
        self.heading = self.tid("success-title")
        self.reference = self.tid("txn-reference")
        self.status = self.tid("txn-status")
        self.amount = self.tid("txn-amount")
        self.mode = self.tid("txn-mode")
        self.beneficiary = self.tid("txn-beneficiary")

    def loaded_anchor(self) -> Locator:
        return self.heading

    def read_receipt(self) -> TransferReceipt:
        amount_text = self.act.read_text(self.amount, "amount")
        return TransferReceipt(
            reference=self.act.read_text(self.reference, "transaction reference"),
            status=self.act.read_text(self.status, "status"),
            amount=Decimal(amount_text.replace("₹", "").replace(",", "")),
            mode=self.act.read_text(self.mode, "mode"),
            beneficiary_name=self.act.read_text(self.beneficiary, "beneficiary"),
        )
