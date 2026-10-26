"""Retail payment journeys. Flows read like the business process and return domain results."""
from __future__ import annotations

from apps.retail_banking.pages.beneficiary_pages import AddBeneficiaryPage, BeneficiaryListPage
from apps.retail_banking.pages.dashboard_page import DashboardPage
from apps.retail_banking.pages.transfer_pages import TransferPage, TransferReviewPage, TransferSuccessPage
from qaforge.core.base_page import BaseFlow
from qaforge.data.models import Beneficiary, Transfer, TransferReceipt


class PaymentFlows(BaseFlow):
    def add_beneficiary(self, payee: Beneficiary) -> None:
        with self.act.group("Add beneficiary"):
            DashboardPage(self.session).open().go_to_add_beneficiary()
            form = AddBeneficiaryPage(self.session)
            form.wait_until_loaded()
            form.fill_form(payee)
            form.submit()
            listing = BeneficiaryListPage(self.session)
            listing.wait_until_loaded()
            self.act.expect_text(listing.flash, f"Beneficiary {payee.nickname} added", "success message")

    def transfer(self, payee: Beneficiary, t: Transfer) -> TransferReceipt:
        with self.act.group(f"{t.mode} transfer"):
            entry = TransferPage(self.session).open()
            entry.fill_transfer(payee, t)
            entry.continue_to_review()
            review = TransferReviewPage(self.session)
            review.wait_until_loaded()
            review.check_summary(payee, t)
            review.confirm()
            success = TransferSuccessPage(self.session)
            success.wait_until_loaded()
            return success.read_receipt()

    def attempt_transfer_expecting_error(self, payee: Beneficiary, t: Transfer) -> str:
        """Negative path: submit and return the validation message shown to the customer."""
        with self.act.group(f"{t.mode} transfer (expected rejection)"):
            entry = TransferPage(self.session).open()
            entry.fill_transfer(payee, t)
            entry.continue_to_review()
            self.act.expect_visible(entry.form_error, "validation message")
            return self.act.read_text(entry.form_error, "validation message")
