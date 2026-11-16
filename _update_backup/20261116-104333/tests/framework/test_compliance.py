"""Self-tests for masking, evidence integrity and the production guard."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from qaforge.compliance.evidence import EvidencePack, RunInfo, verify_pack
from qaforge.compliance.masker import FULL_MASK, Masker
from qaforge.config.settings import ProductionGuardError, load_settings


def test_masker_hides_registered_secrets_and_sensitive_patterns():
    m = Masker()
    m.register_secret("S3cr3t!Pass")
    text = ("login with S3cr3t!Pass card 4000 0000 0000 0002 acct 501000123456 pan ABCPK1234Z "
            "auth Bearer eyJhbGciOiJIUzI1NiJ9.abc mail asha.k@example.test {\"otp\": \"123456\"} amount 1500.00")
    out = m.mask(text)
    assert "S3cr3t!Pass" not in out and FULL_MASK in out
    assert "XXXX XXXX XXXX 0002" in out
    assert "XXXXXXXX3456" in out
    assert "XXXXX1234X" in out
    assert "eyJhbGci" not in out
    assert "a***@example.test" in out
    assert '"otp": "******' in out
    assert "1500.00" in out  # amounts are not account numbers


def test_mask_obj_blanks_sensitive_keys_recursively():
    m = Masker()
    data = {"user": {"password": "x", "name": "Asha"}, "items": [{"cvv": "123"}]}
    assert m.mask_obj(data) == {"user": {"password": FULL_MASK, "name": "Asha"}, "items": [{"cvv": FULL_MASK}]}


def _run(tmp: Path) -> RunInfo:
    return RunInfo.create("RUN-1", "local", tmp, {})


def test_evidence_pack_is_sealed_and_tamper_evident(tmp_path: Path):
    pack = EvidencePack(_run(tmp_path), tmp_path, "tests/x.py::test_a", "test_a", ["BANK-1"], [], "each_step", True)
    pack.add_step("Open page", "passed", "2026-01-01T00:00:00Z", 12)
    pack.attach_json("event.json", {"accountNumber": "501000123456", "password": "p"})
    record = pack.finalize("passed", None)

    ok, problems = verify_pack(pack.dir)
    assert ok, problems
    ok, problems = verify_pack(Path(record["evidence_zip"]))
    assert ok, problems
    stored = json.loads((pack.dir / "event.json").read_text())
    assert stored == {"accountNumber": "XXXXXXXX3456", "password": FULL_MASK}

    (pack.dir / "event.json").write_text('{"accountNumber": "changed"}')
    ok, problems = verify_pack(pack.dir)
    assert not ok and any("modified: event.json" in p for p in problems)

    with zipfile.ZipFile(record["evidence_zip"]) as zf:
        names = zf.namelist()
    assert any(n.endswith("report.html") for n in names) and any(n.endswith("manifest.json") for n in names)


def test_production_guard_blocks_prod(monkeypatch: pytest.MonkeyPatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.delenv("QA_ALLOW_PRODUCTION", raising=False)
    with pytest.raises(ProductionGuardError):
        load_settings(env="prod", root_dir=root)


def test_env_variable_overrides(monkeypatch: pytest.MonkeyPatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("QAF__browser__headless", "false")
    monkeypatch.setenv("QAF__events__default_timeout_s", "5")
    s = load_settings(env="local", root_dir=root)
    assert s.browser.headless is False and s.events.default_timeout_s == 5
