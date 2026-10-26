"""Read-only database verification (Oracle, PostgreSQL, DB2, SQL Server via SQLAlchemy).

Compliance guard rails:
  * only SELECT / WITH statements are executed - anything else is refused before it runs
  * bind parameters only (no string formatting of values into SQL)
  * connection URL comes from a secret, never from config
  * every query and its (masked) result rows go into the evidence pack

    db = DbVerifier.from_secret(secrets, "CORE_DB_URL")
    row = db.wait_for_row("SELECT status, amount FROM payments WHERE txn_ref = :ref", {"ref": ref})
    verify(row, "payment_row", ctx=ctx, evidence=evidence, source="db:payments")
"""
from __future__ import annotations

import re
import time
from typing import Any

from qaforge.compliance.evidence import EvidencePack
from qaforge.compliance.logger import get_logger, utc_now_iso
from qaforge.compliance.masker import masker
from qaforge.config.secrets import SecretProvider

log = get_logger("db")
_READ_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_FORBIDDEN = re.compile(r"\b(insert|update|delete|merge|drop|alter|truncate|grant|revoke|create|exec|call)\b",
                        re.IGNORECASE)


class ReadOnlyViolation(PermissionError):
    pass


class DbVerifier:
    def __init__(self, url: str, evidence: EvidencePack | None = None, name: str = "db") -> None:
        try:
            from sqlalchemy import create_engine
        except ImportError as exc:  # pragma: no cover
            raise ImportError("DB verification needs: pip install 'qaforge[db]' plus your driver") from exc
        masker.register_secret(url)
        self.engine = create_engine(url, pool_pre_ping=True, pool_size=2)
        self.evidence = evidence
        self.name = name

    @classmethod
    def from_secret(cls, secrets: SecretProvider, secret_name: str, evidence: EvidencePack | None = None,
                    name: str = "db") -> DbVerifier:
        return cls(secrets.get(secret_name), evidence, name)

    def bind_evidence(self, evidence: EvidencePack) -> DbVerifier:
        self.evidence = evidence
        return self

    @staticmethod
    def _guard(sql: str) -> None:
        body = re.sub(r"'[^']*'", "''", sql)  # ignore string literals when scanning keywords
        if not _READ_ONLY.match(body) or _FORBIDDEN.search(body):
            raise ReadOnlyViolation("Only read-only SELECT/WITH queries are allowed in verification")

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        from sqlalchemy import text

        self._guard(sql)
        started, t0 = utc_now_iso(), time.monotonic()
        with self.engine.connect() as con:
            rows = [dict(r._mapping) for r in con.execute(text(sql), params or {})]
        if self.evidence is not None:
            self.evidence.add_step(f"DB query on {self.name}: {len(rows)} row(s)", "passed", started,
                                   int((time.monotonic() - t0) * 1000), app=self.name,
                                   detail=" ".join(sql.split())[:300])
        return rows

    def wait_for_row(self, sql: str, params: dict[str, Any] | None = None, timeout_s: float = 60,
                     poll_s: float = 2) -> dict[str, Any]:
        """Downstream systems are eventually consistent - poll until the row exists."""
        deadline = time.monotonic() + timeout_s
        while True:
            rows = self.query(sql, params)
            if rows:
                if self.evidence is not None:
                    self.evidence.attach_json(f"db_{self.name}_{int(time.time())}.json", rows, kind="db-rows")
                return rows[0]
            if time.monotonic() >= deadline:
                raise AssertionError(f"No row from {self.name} within {timeout_s}s for params {masker.mask(params)}")
            time.sleep(poll_s)
