"""Helpers for email sign-up: consent wording, signed unsubscribe links and confirmation mail.

The consent wording below is provisional. Decision D3 (legal review) is open, and the
text must be approved before a public form is offered to people.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from email.message import EmailMessage

# Set to True only after decision D3 (legal review) has signed off the wording below.
PRIVACY_NOTICE_REVIEWED = False
EMAIL_CONSENT_VERSION = "2026-11-email"
EMAIL_CONSENT_TEXT = (
    "I agree to receive public announcements from this community by email. "
    "I can stop at any time with the link in every email, and I can ask for my "
    "details to be deleted."
)
SIGNUP_SOURCE = "web_form"


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def _sign(secret: bytes, payload: str) -> str:
    digest = hmac.new(secret, payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def make_unsubscribe_token(secret: bytes, destination: str) -> str:
    """A token that proves an unsubscribe link was made for this address, without storing it."""
    encoded = base64.urlsafe_b64encode(destination.encode()).decode().rstrip("=")
    return f"{encoded}.{_sign(secret, 'unsubscribe|' + destination)}"


def read_unsubscribe_token(secret: bytes, token: str) -> str | None:
    """The address a valid token was made for, or None. Tampered tokens are refused."""
    try:
        encoded, signature = token.split(".", 1)
        padded = encoded + "=" * (-len(encoded) % 4)
        destination = base64.urlsafe_b64decode(padded.encode()).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(signature, _sign(secret, "unsubscribe|" + destination)):
        return None
    return destination


def confirmation_message(
    from_address: str,
    to_address: str,
    community: str,
    confirm_url: str,
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"Confirm your subscription to {community}"
    msg["From"] = from_address
    msg["To"] = to_address
    msg.set_content(
        f"Someone asked to receive announcements from {community} at this address.\n\n"
        f"To confirm, open this link:\n{confirm_url}\n\n"
        "The link works once and expires in three days.\n\n"
        "If you did not ask for this, ignore this message. Nothing will be sent to you.\n\n"
        f"{EMAIL_CONSENT_TEXT}\n"
    )
    return msg


def test_message(
    from_address: str,
    to_address: str,
    community: str,
    unsubscribe_url: str,
) -> EmailMessage:
    """A delivery test. It subscribes nobody, and it carries a working unsubscribe link."""
    msg = EmailMessage()
    msg["Subject"] = f"Test message from {community}"
    msg["From"] = from_address
    msg["To"] = to_address
    msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(
        f"This is a test from {community}. Nothing else was sent, and you have not been "
        "subscribed.\n\n"
        "If you can read this, email delivery is working. Check that it did not land in spam.\n\n"
        f"To stop any future emails: {unsubscribe_url}\n"
    )
    return msg
