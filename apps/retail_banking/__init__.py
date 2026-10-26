"""Retail internet banking - page objects and flows.

    retail = RetailBanking(open_app("retail", role="maker"))
    retail.payments.add_beneficiary(payee)
"""
from __future__ import annotations

from apps.retail_banking.flows.payments import PaymentFlows
from qaforge.core.session import AppSession


class RetailBanking:
    def __init__(self, session: AppSession) -> None:
        self.session = session
        self.payments = PaymentFlows(session)
