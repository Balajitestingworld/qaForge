"""Reuse authenticated browser sessions per (env, app, role).

Logging in through Okta for every test is slow and can trip rate limits / lockout policies.
After one successful login the context's storage state (cookies + local storage) is saved
and reused until it is older than `okta.session_ttl_minutes`.

These files contain live session cookies: they are written with 0600 permissions, live
outside the evidence folder and are git-ignored. Never attach them to evidence or JIRA.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from qaforge.compliance.logger import get_logger
from qaforge.config.settings import Settings

log = get_logger("auth.session")


class SessionStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.dir = settings.path(settings.okta.session_dir)
        self.ttl_s = settings.okta.session_ttl_minutes * 60

    def _file(self, app: str, role: str) -> Path:
        return self.dir / f"{self.settings.env}-{app}-{role}.json"

    def load(self, app: str, role: str) -> dict[str, Any] | None:
        f = self._file(app, role)
        if not f.exists():
            return None
        age = time.time() - f.stat().st_mtime
        if age > self.ttl_s:
            log.info("Cached session for %s/%s expired (%.0f min old)", app, role, age / 60)
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, app: str, role: str, state: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        f = self._file(app, role)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(f)  # atomic, safe with parallel workers
        log.info("Saved authenticated session for %s/%s", app, role)

    def invalidate(self, app: str, role: str) -> None:
        self._file(app, role).unlink(missing_ok=True)
