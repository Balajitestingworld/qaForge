"""End-to-end payment journey across two applications and the event backbone.

Maker (retail portal)  -> adds a beneficiary and makes a NEFT transfer
Event backbone (Kafka) -> beneficiary.created / payments.initiated carry exactly what was entered
Checker (back office)  -> sees the same payment pending, approves it
Event backbone (Kafka) -> payments.approved is published
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.backoffice import Backoffice
from apps.retail_banking import RetailBanking
from qaforge.data.factories import BeneficiaryFactory, TransferFactory


@pytest.mark.smoke
@pytest.mark.jira("BANK-101")
def test_neft_transfer_maker_checker_with_event_verification(open_app, ctx, events, verify):
    retail = RetailBanking(open_app("retail", role="maker"))

    # 1. Maker adds a beneficiary - generated data is saved in the scenario context and the ledger
    payee = ctx.save("payee", BeneficiaryFactory.build())
    retail.payments.add_beneficiary(payee)
    ctx.mark_created("payee", entity="beneficiary")
    events.expect_and_verify("beneficiary.created", "beneficiary_created", key=payee.account_number)

    # 2. Maker transfers money - the reference shown on screen is captured for every later check
    transfer = ctx.save("transfer", TransferFactory.build(mode="NEFT"))
    receipt = retail.payments.transfer(payee, transfer)
    ctx.save("txn_ref", receipt.reference, entity="payment")
    assert receipt.status == "PENDING_APPROVAL"
    assert receipt.amount == transfer.amount

    # 3. The event published to the backbone must match what the maker entered, field by field
    events.expect_and_verify("payments.initiated", "payment_initiated", key=ctx["txn_ref"])

    # 4. Checker sees the same payment in the back office and approves it
    backoffice = Backoffice(open_app("backoffice", role="checker"))
    row = backoffice.approvals.find_payment(ctx["txn_ref"])
    verify(row, [
        {"field": "Beneficiary", "expected": "ctx.payee.name"},
        {"field": "Amount", "expected": "ctx.transfer.amount", "rule": "decimal"},
        {"field": "Mode", "expected": "ctx.transfer.mode"},
        {"field": "Status", "expected": "PENDING_APPROVAL"},
    ], source="ui:backoffice.payments_queue", name="Back-office queue shows the maker's payment")

    approved_row = backoffice.approvals.approve_payment(ctx["txn_ref"])
    assert approved_row["Status"] == "APPROVED"

    # 5. Approval is published downstream
    events.expect_and_verify("payments.approved", "payment_approved", key=ctx["txn_ref"])


@pytest.mark.regression
@pytest.mark.jira("BANK-102")
def test_imps_transfer_reusing_beneficiary_from_ledger(open_app, ctx, events, ledger):
    """Reuse data created earlier (this run or previous runs) instead of re-creating it."""
    retail = RetailBanking(open_app("retail", role="maker"))

    # The demo bank keeps data in memory, so only reuse beneficiaries created in this run.
    record = ledger.latest("beneficiary", this_run_only=True)
    if record is None:
        payee = ctx.save("payee", BeneficiaryFactory.build())
        retail.payments.add_beneficiary(payee)
        ctx.mark_created("payee", entity="beneficiary")
    else:
        payee = ctx.save("payee", BeneficiaryFactory.model.model_validate(record["data"]))
        retail.session.evidence.note(f"Reused beneficiary from ledger (created by {record['test_id']})")

    transfer = ctx.save("transfer", TransferFactory.build(mode="IMPS", amount=Decimal("2500.00")))
    receipt = retail.payments.transfer(payee, transfer)
    ctx.save("txn_ref", receipt.reference, entity="payment")

    events.expect_and_verify("payments.initiated", "payment_initiated", key=ctx["txn_ref"])
