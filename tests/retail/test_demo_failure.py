"""Intentionally failing example - run with:  pytest -m demo_failure

Shows what a real failure produces: a field-level mismatch table in the evidence report,
failure screenshots, the Playwright trace, and a de-duplicated JIRA defect (dry run).
"""
from __future__ import annotations

import pytest

from apps.retail_banking import RetailBanking
from qaforge.data.factories import BeneficiaryFactory, TransferFactory


@pytest.mark.demo_failure
@pytest.mark.jira("BANK-103")
def test_payment_event_carries_channel_code(open_app, ctx, events):
    retail = RetailBanking(open_app("retail", role="maker"))
    payee = ctx.save("payee", BeneficiaryFactory.build())
    retail.payments.add_beneficiary(payee)
    transfer = ctx.save("transfer", TransferFactory.build(mode="NEFT"))
    ctx.save("txn_ref", retail.payments.transfer(payee, transfer).reference)

    # The demo bank does not publish a channel code yet -> this verification fails on purpose
    events.expect_and_verify("payments.initiated", [
        {"field": "txnRef", "expected": "ctx.txn_ref"},
        {"field": "amount", "expected": "ctx.transfer.amount", "rule": "decimal"},
        {"field": "channel", "expected": "INTERNET_BANKING"},
    ], key=ctx["txn_ref"])
