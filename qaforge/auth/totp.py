"""RFC 6238 TOTP (the algorithm behind Okta Verify / Google Authenticator codes).

Pure standard library - no third-party dependency to approve. Test users get a TOTP
factor enrolled in non-prod Okta; the base32 seed is stored in the vault and resolved
through the SecretProvider like any other secret.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time


def _decode_seed(seed: str) -> bytes:
    cleaned = seed.replace(" ", "").upper()
    cleaned += "=" * (-len(cleaned) % 8)
    return base64.b32decode(cleaned)


def totp(seed: str | bytes, at: float | None = None, digits: int = 6, period: int = 30,
         algorithm: str = "sha1") -> str:
    key = seed if isinstance(seed, bytes) else _decode_seed(seed)
    counter = int((time.time() if at is None else at) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), getattr(hashlib, algorithm)).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def seconds_remaining(period: int = 30, at: float | None = None) -> float:
    now = time.time() if at is None else at
    return period - (now % period)


def fresh_totp(seed: str, min_validity_s: float = 4.0, period: int = 30) -> str:
    """Avoid submitting a code that expires in flight: wait for the next window if needed."""
    remaining = seconds_remaining(period)
    if remaining < min_validity_s:
        time.sleep(remaining + 0.25)
    return totp(seed, period=period)
