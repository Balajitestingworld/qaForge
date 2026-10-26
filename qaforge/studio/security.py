"""Studio users, roles and the audit trail.

* Passwords: PBKDF2-HMAC-SHA256, 310 000 iterations, per-user salt (stdlib only).
* Lockout: 5 failed sign-ins lock the account for 15 minutes.
* Roles:   viewer  - see tests, runs and evidence
           tester  - viewer + build/record tests, capture locators, edit data, start runs
           admin   - tester + environment config, users, audit log
* Audit:   every sign-in and every change is appended to studio_data/audit.log.jsonl
           (who, when, from where, what, which object) with sensitive values masked.

For an enterprise rollout, put Studio behind the bank's reverse proxy / SSO; the role model
and audit trail stay the same.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from qaforge.compliance.logger import utc_now_iso
from qaforge.compliance.masker import masker

ROLES = ["viewer", "tester", "admin"]
ITERATIONS = 310_000
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
USER_RE = re.compile(r"^[a-z][a-z0-9._-]{2,40}$")


class AuthError(ValueError):
    pass


def role_at_least(role: str, needed: str) -> bool:
    return ROLES.index(role) >= ROLES.index(needed)


def check_password_strength(pw: str) -> None:
    if len(pw) < 12 or not re.search(r"[A-Z]", pw) or not re.search(r"[a-z]", pw) or not re.search(r"\d", pw):
        raise AuthError("Password needs at least 12 characters with upper case, lower case and a digit")


def _hash(pw: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, ITERATIONS).hex()


class UserStore:
    def __init__(self, data_dir: Path) -> None:
        self.file = data_dir / "users.json"
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        if not self.file.exists():
            return {}
        return json.loads(self.file.read_text(encoding="utf-8"))

    def _save(self, users: dict[str, Any]) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(users, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.file)

    def empty(self) -> bool:
        return not self._load()

    def list(self) -> list[dict[str, Any]]:
        return [{"username": u, **{k: v for k, v in d.items() if k not in ("hash", "salt")}}
                for u, d in sorted(self._load().items())]

    def get(self, username: str) -> dict[str, Any] | None:
        return self._load().get(username)

    def create(self, username: str, password: str, role: str, display: str = "") -> None:
        username = username.strip().lower()
        if not USER_RE.match(username):
            raise AuthError("Username: 3-40 characters, lowercase letters, digits, dot, dash or underscore")
        if role not in ROLES:
            raise AuthError(f"Role must be one of {', '.join(ROLES)}")
        check_password_strength(password)
        with self._lock:
            users = self._load()
            if username in users:
                raise AuthError(f"User '{username}' already exists")
            salt = secrets.token_bytes(16)
            users[username] = {"hash": _hash(password, salt), "salt": salt.hex(), "role": role,
                               "display": display or username, "created": utc_now_iso(), "failed": 0,
                               "locked_until": 0, "active": True}
            self._save(users)

    def set_password(self, username: str, password: str) -> None:
        check_password_strength(password)
        with self._lock:
            users = self._load()
            if username not in users:
                raise AuthError("Unknown user")
            salt = secrets.token_bytes(16)
            users[username].update(hash=_hash(password, salt), salt=salt.hex(), failed=0, locked_until=0)
            self._save(users)

    def update(self, username: str, **fields: Any) -> None:
        with self._lock:
            users = self._load()
            if username not in users:
                raise AuthError("Unknown user")
            if "role" in fields and fields["role"] not in ROLES:
                raise AuthError("Unknown role")
            users[username].update({k: v for k, v in fields.items() if k in ("role", "active", "display")})
            self._save(users)

    def verify(self, username: str, password: str) -> dict[str, Any]:
        username = (username or "").strip().lower()
        with self._lock:
            users = self._load()
            u = users.get(username)
            if u is None or not u.get("active", True):
                _hash(password or "x", b"0" * 16)  # same timing whether or not the user exists
                raise AuthError("Wrong username or password")
            if u.get("locked_until", 0) > time.time():
                raise AuthError("Account locked after repeated failures. Try again in 15 minutes or ask an admin.")
            if not hmac.compare_digest(_hash(password or "", bytes.fromhex(u["salt"])), u["hash"]):
                u["failed"] = u.get("failed", 0) + 1
                if u["failed"] >= MAX_FAILURES:
                    u["locked_until"], u["failed"] = time.time() + LOCK_SECONDS, 0
                self._save(users)
                raise AuthError("Wrong username or password")
            u["failed"], u["last_login"] = 0, utc_now_iso()
            self._save(users)
            return {"username": username, "role": u["role"], "display": u.get("display", username)}


class AuditLog:
    def __init__(self, data_dir: Path) -> None:
        self.file = data_dir / "audit.log.jsonl"
        self._lock = threading.Lock()

    def write(self, user: str | None, ip: str | None, action: str, target: str = "", detail: Any = None) -> None:
        line = {"ts": utc_now_iso(), "user": user or "-", "ip": ip or "-", "action": action,
                "target": masker.mask(target)}
        if detail is not None:
            line["detail"] = masker.mask_obj(detail)
        self.file.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    def tail(self, n: int = 300, user: str | None = None) -> list[dict[str, Any]]:
        if not self.file.exists():
            return []
        lines = self.file.read_text(encoding="utf-8").splitlines()[-5000:]
        rows = [json.loads(x) for x in lines if x.strip()]
        if user:
            rows = [r for r in rows if r.get("user") == user]
        return list(reversed(rows[-n:]))


def secret_key(data_dir: Path) -> bytes:
    f = data_dir / "secret_key"
    if not f.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        f.write_bytes(secrets.token_bytes(32))
        os.chmod(f, 0o600)
    return f.read_bytes()
