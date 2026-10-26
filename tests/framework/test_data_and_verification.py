"""Self-tests for data generation, context, ledger, TOTP, comparison and JIRA publishing logic."""
from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from qaforge.auth.totp import totp
from qaforge.compliance.masker import luhn_valid
from qaforge.config.settings import DataConfig, Settings
from qaforge.data.context import ScenarioContext
from qaforge.data.factories import BeneficiaryFactory, CustomerFactory, TransferFactory, use_generator
from qaforge.data.generators import DataGenerator
from qaforge.data.ledger import RunLedger
from qaforge.integrations.jira.publisher import RecordingJira, ResultsPublisher, failure_signature
from qaforge.verifiers.compare import VerificationError, verify


def test_generators_produce_valid_formats_and_are_reproducible():
    a, b = DataGenerator(DataConfig(), seed=42), DataGenerator(DataConfig(), seed=42)
    assert [a.account_number() for _ in range(3)] == [b.account_number() for _ in range(3)]
    g = DataGenerator(DataConfig(), seed=7)
    for _ in range(50):
        assert luhn_valid(g.card_number())
        assert re.fullmatch(r"[A-Z]{4}0\d{6}", g.ifsc())
        assert re.fullmatch(r"[A-Z]{3}P[A-Z]\d{4}[A-Z]", g.pan_format())
        assert re.fullmatch(r"9\d{9}", g.mobile())


def test_factories_build_valid_models_with_overrides():
    use_generator(DataGenerator(DataConfig(), seed=1))
    assert CustomerFactory.build().email.endswith("@example.test")
    assert BeneficiaryFactory.build(nickname="Rent").nickname == "Rent"
    assert TransferFactory.build(amount="10").amount == Decimal("10.00")


def test_totp_matches_rfc6238_vector():
    # RFC 6238 Appendix B: secret "12345678901234567890", T=59 -> 94287082 (8 digits, SHA1)
    assert totp(b"12345678901234567890", at=59, digits=8) == "94287082"


def test_context_dotted_paths_and_ledger_reuse(tmp_path: Path):
    ledger = RunLedger(tmp_path / "l.sqlite", "local", "RUN-1")
    ctx = ScenarioContext("t1", ledger)
    use_generator(DataGenerator(DataConfig(), seed=3))
    payee = ctx.save("payee", BeneficiaryFactory.build(), entity="beneficiary")
    assert ctx.get_path("payee.ifsc") == payee.ifsc
    found = ledger.latest("beneficiary", this_run_only=True)
    assert found and found["data"]["account_number"] == payee.account_number
    with pytest.raises(KeyError):
        ctx["never_saved"]


def test_verify_reports_every_field_and_raises_on_mismatch():
    ctx = ScenarioContext("t")
    ctx.save("amount", Decimal("1500.00"))
    event = {"amount": "1500.0", "currency": "INR", "meta": {"id": "x"}}
    ok = verify(event, [{"field": "amount", "expected": "ctx.amount", "rule": "decimal"},
                        {"field": "currency", "expected": "INR"},
                        {"field": "meta.id", "rule": "exists"}], ctx=ctx)
    assert ok.passed and len(ok.rows) == 3
    with pytest.raises(VerificationError, match="channel"):
        verify(event, [{"field": "channel", "expected": "IB"}], ctx=ctx)


def _fake_result(tmp_path: Path, outcome: str) -> dict:
    d = tmp_path / "t"
    d.mkdir(exist_ok=True)
    detail = {"steps": [{"name": "Confirm", "status": "failed"}], "run": {"env": "local"}}
    (d / "test.json").write_text(json.dumps(detail))
    return {"run_id": "R1", "nodeid": "tests/a.py::test_x", "test_name": "test_x", "jira_keys": ["BANK-9"],
            "outcome": outcome, "error": "AssertionError: boom", "evidence_dir": str(d), "duration_s": 1}


def test_jira_publisher_comments_and_raises_deduplicated_defects(tmp_path: Path):
    settings = Settings.model_validate({"env": "local", "jira": {"enabled": True, "project_key": "BANK"}})
    result = _fake_result(tmp_path, "failed")
    sig = failure_signature({**result, "steps": [{"name": "Confirm", "status": "failed"}]})

    new = RecordingJira()
    outcome = ResultsPublisher(settings, None, tmp_path, client=new).publish([result])  # type: ignore[arg-type]
    assert outcome["defects"][0]["action"] == "created"
    assert any(c["call"] == "add_comment" and c["issue"] == "BANK-9" for c in new.calls)
    assert any(c["call"] == "link" for c in new.calls)

    existing = RecordingJira(existing_signatures={sig})
    outcome = ResultsPublisher(settings, None, tmp_path, client=existing).publish([result])  # type: ignore[arg-type]
    assert outcome["defects"][0]["action"] == "commented"
    assert not any(c["call"] == "create_issue" for c in existing.calls)
