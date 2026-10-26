"""RunLedger - persistent record of every entity the automation created.

Why: banking journeys are long. A beneficiary created today is used by tomorrow's payment
suite, a customer onboarded in the retail app is verified in CRM, and the data team needs
to know which records are synthetic so they can be cleaned. The ledger answers
"what did automation create, where, when, and in which test?".

SQLite (stdlib) with WAL mode - safe for parallel pytest-xdist workers on one machine.
For a shared ledger across agents, point a subclass at your team database.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from qaforge.compliance.logger import get_logger, utc_now_iso

log = get_logger("ledger")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    env         TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    test_id     TEXT,
    entity_type TEXT NOT NULL,
    entity_key  TEXT NOT NULL,
    data_json   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_entities_lookup ON entities (env, entity_type, status);
CREATE INDEX IF NOT EXISTS ix_entities_key ON entities (env, entity_type, entity_key);
"""


class RunLedger:
    def __init__(self, path: Path, env: str, run_id: str) -> None:
        self.path = path
        self.env = env
        self.run_id = run_id
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def record(self, entity_type: str, key: str, data: Any, test_id: str | None = None,
               status: str = "active") -> int:
        now = utc_now_iso()
        with self._lock, self._connect() as con:
            cur = con.execute(
                "INSERT INTO entities (env, run_id, test_id, entity_type, entity_key, data_json, status, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (self.env, self.run_id, test_id, entity_type, key, json.dumps(data, default=str), status, now, now),
            )
            log.info("Ledger: recorded %s '%s'", entity_type, key)
            return int(cur.lastrowid or 0)

    def find(self, entity_type: str, status: str | None = "active", this_run_only: bool = False,
             **data_filters: Any) -> list[dict[str, Any]]:
        sql = "SELECT * FROM entities WHERE env=? AND entity_type=?"
        params: list[Any] = [self.env, entity_type]
        if status:
            sql += " AND status=?"
            params.append(status)
        if this_run_only:
            sql += " AND run_id=?"
            params.append(self.run_id)
        sql += " ORDER BY id DESC"
        with self._connect() as con:
            rows = [dict(r) for r in con.execute(sql, params).fetchall()]
        out = []
        for r in rows:
            r["data"] = json.loads(r.pop("data_json"))
            if all(str(r["data"].get(k)) == str(v) for k, v in data_filters.items()):
                out.append(r)
        return out

    def latest(self, entity_type: str, **filters: Any) -> dict[str, Any] | None:
        found = self.find(entity_type, **filters)
        return found[0] if found else None

    def set_status(self, entity_type: str, key: str, status: str) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                "UPDATE entities SET status=?, updated_at=? WHERE env=? AND entity_type=? AND entity_key=?",
                (status, utc_now_iso(), self.env, entity_type, key),
            )
