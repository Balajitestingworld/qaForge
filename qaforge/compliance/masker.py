"""Central masking of sensitive values before anything is written to logs, reports or JIRA.

Three layers:
  1. Exact secrets   - every secret resolved through the SecretProvider is registered here
                       automatically, so a password can never leak even if a page echoes it.
  2. Sensitive keys  - dict / JSON keys such as password, otp, cvv, token are always blanked.
  3. Patterns        - card numbers (Luhn-valid), long account numbers, PAN, Aadhaar-like,
                       bearer tokens and e-mail local parts are partially masked.

Screenshots are masked separately with Playwright's `mask=` option (see core/actions.py).
"""
from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Mapping
from typing import Any

FULL_MASK = "******"

SENSITIVE_KEYS = {
    "password", "passwd", "pwd", "passcode", "otp", "totp", "pin", "mpin", "cvv", "cvc",
    "secret", "token", "access_token", "refresh_token", "id_token", "authorization",
    "api_key", "apikey", "client_secret", "private_key", "cookie", "set-cookie", "session",
    "credentials.passcode", "totp_seed",
}


def luhn_valid(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if len(digits) < 12:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _keep_last(value: str, keep: int = 4, char: str = "X") -> str:
    digits_seen = 0
    out = []
    total_digits = sum(c.isdigit() for c in value)
    for c in value:
        if c.isdigit():
            digits_seen += 1
            out.append(c if digits_seen > total_digits - keep else char)
        else:
            out.append(c)
    return "".join(out)


class Masker:
    _CARD = re.compile(r"(?<![\w])(?:\d[ -]?){12,18}\d(?![\w])")
    _ACCOUNT = re.compile(r"(?<![\w.])\d{9,18}(?![\w.])")
    _PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
    _AADHAAR = re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}\b")
    _BEARER = re.compile(r"(?i)(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
    _EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
    _KV = re.compile(
        r"(?i)(\"?(?:%s)\"?\s*[:=]\s*\"?)([^\"&,\s}]+)" % "|".join(re.escape(k) for k in SENSITIVE_KEYS)
    )

    def __init__(self, mask_emails: bool = True) -> None:
        self._secrets: set[str] = set()
        self._lock = threading.Lock()
        self.mask_emails = mask_emails

    # ---- registration -------------------------------------------------
    def register_secret(self, value: str | None) -> None:
        if value and len(value) >= 4:
            with self._lock:
                self._secrets.add(value)

    def register_many(self, values: Iterable[str | None]) -> None:
        for v in values:
            self.register_secret(v)

    # ---- masking -------------------------------------------------------
    def mask(self, text: Any) -> str:
        if text is None:
            return ""
        s = str(text)
        if not s:
            return s
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for secret in secrets:
            if secret in s:
                s = s.replace(secret, FULL_MASK)
        s = self._BEARER.sub(lambda m: f"{m.group(1)} {FULL_MASK}", s)
        s = self._KV.sub(lambda m: f"{m.group(1)}{FULL_MASK}", s)
        s = self._CARD.sub(lambda m: _keep_last(m.group(0)) if luhn_valid(m.group(0)) else m.group(0), s)
        s = self._AADHAAR.sub(lambda m: _keep_last(m.group(0)), s)
        s = self._ACCOUNT.sub(lambda m: _keep_last(m.group(0)), s)
        s = self._PAN.sub(lambda m: "XXXXX" + m.group(0)[5:9] + "X", s)
        if self.mask_emails:
            s = self._EMAIL.sub(lambda m: f"{m.group(1)}***{m.group(2)}", s)
        return s

    def mask_value_for_key(self, key: str, value: Any) -> Any:
        if key.lower() in SENSITIVE_KEYS:
            return FULL_MASK
        return self.mask_obj(value)

    def mask_obj(self, obj: Any) -> Any:
        """Recursively mask dicts / lists / pydantic models, returning plain JSON-able data."""
        if hasattr(obj, "model_dump"):
            obj = obj.model_dump(mode="json")
        if isinstance(obj, Mapping):
            return {str(k): self.mask_value_for_key(str(k), v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [self.mask_obj(v) for v in obj]
        if isinstance(obj, (int, float, bool)) or obj is None:
            return obj
        return self.mask(obj)


# One process-wide masker; secrets registered anywhere are masked everywhere.
masker = Masker()
