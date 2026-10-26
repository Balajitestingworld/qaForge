"""Payments back office (checker) - page objects and flows."""
from __future__ import annotations

from apps.backoffice.pages.payments_queue_page import PaymentsQueuePage
from qaforge.core.base_page import BaseFlow
from qaforge.core.session import AppSession


class ApprovalFlows(BaseFlow):
    def find_payment(self, reference: str) -> dict[str, str]:
        with self.act.group(f"Find payment {reference} in queue"):
            queue = PaymentsQueuePage(self.session).open()
            queue.search(reference)
            return queue.grid.read_row(reference=reference)

    def approve_payment(self, reference: str) -> dict[str, str]:
        with self.act.group(f"Approve payment {reference}"):
            queue = PaymentsQueuePage(self.session).open()
            queue.search(reference)
            queue.approve(reference)
            self.act.expect_text(queue.flash, f"Payment {reference} approved", "approval message")
            return queue.grid.read_row(reference=reference)


class Backoffice:
    def __init__(self, session: AppSession) -> None:
        self.session = session
        self.approvals = ApprovalFlows(session)
