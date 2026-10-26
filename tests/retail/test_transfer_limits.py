"""Data-driven negative tests: limits come from testdata/transfer_limits.yaml (one JIRA key per row)."""
from __future__ import annotations

import pytest

from apps.retail_banking import RetailBanking
from qaforge.data.factories import BeneficiaryFactory, TransferFactory
from qaforge.data.loader import load_cases


@pytest.fixture
def maker_with_payee(open_app, ctx):
    retail = RetailBanking(open_app("retail", role="maker"))
    payee = ctx.save("payee", BeneficiaryFactory.build())
    retail.payments.add_beneficiary(payee)
    return retail, payee


@pytest.mark.regression
@pytest.mark.parametrize("case", load_cases("testdata/transfer_limits.yaml"))
def test_transfer_limit_is_enforced(maker_with_payee, ctx, events, case):
    retail, payee = maker_with_payee
    transfer = ctx.save("transfer", TransferFactory.build(mode=case["mode"], amount=case["amount"]))

    message = retail.payments.attempt_transfer_expecting_error(payee, transfer)

    assert case["expected_error"] in message
    # a rejected payment must never reach the payment backbone
    events.expect_none("payments.initiated", where={"beneficiary.accountNumber": payee.account_number},
                       within_s=3)
