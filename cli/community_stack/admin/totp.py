"""Time-based one-time passwords (RFC 6238) for two-step login, and their QR codes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import secrets
import struct
import time
from urllib.parse import quote, urlencode

import segno

STEP_SECONDS = 30
DIGITS = 6
# Accept the previous and next code too, so a phone clock a little off still works.
WINDOW = 1


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_SECONDS)


def matching_step(secret: str, code: str, last_used_step: int | None, now: float | None = None):
    """The time step a code belongs to, or None. Codes at or before the last used step
    are refused, so a code seen over someone's shoulder cannot be replayed."""
    code = "".join(code.split())
    if not (code.isdigit() and len(code) == DIGITS):
        return None
    step = current_step(now)
    for candidate in range(step - WINDOW, step + WINDOW + 1):
        if last_used_step is not None and candidate <= last_used_step:
            continue
        if hmac.compare_digest(code_at(secret, candidate), code):
            return candidate
    return None


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    label = quote(f"{issuer}:{account}")
    query = urlencode(
        {"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": DIGITS, "period": 30}
    )
    return f"otpauth://totp/{label}?{query}"


def qr_svg(uri: str) -> str:
    buffer = io.BytesIO()
    segno.make(uri, error="m").save(
        buffer, kind="svg", scale=5, border=2, xmldecl=False, svgns=True, title="Setup QR code"
    )
    return buffer.getvalue().decode("utf-8")


def new_recovery_codes(count: int = 10) -> list[str]:
    return [f"{secrets.token_hex(4)}-{secrets.token_hex(4)}" for _ in range(count)]


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256("".join(code.lower().split()).encode()).hexdigest()
