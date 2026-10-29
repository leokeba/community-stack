"""SMS alerts through an Android phone running SMS Gateway, and confirmed public sign-up."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from community_stack.announcements.adapters import http
from community_stack.announcements.adapters.base import (
    ChannelAdapter,
    ChannelCapabilities,
    DeliveryResult,
    HealthResult,
    UpdateMode,
    WithdrawMode,
)
from community_stack.announcements.models import Announcement
from community_stack.subscriptions.manager import SubscriberManager

PHONE = re.compile(r"^\+[1-9][0-9]{7,14}$")
SMS_MAX_CHARACTERS = 320
BATCH = 50
CODE_TTL = timedelta(minutes=15)
CODE_ATTEMPTS = 5
CODES_PER_NUMBER_PER_DAY = 3
CODES_PER_CLIENT_PER_DAY = 10


def normalize_phone(number: str) -> str:
    cleaned = re.sub(r"[\s().-]", "", number.strip())
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if not PHONE.fullmatch(cleaned):
        raise ValueError("Enter the number in international format, for example +33612345678.")
    return cleaned


class SmsGatewayAdapter(http.ShortTextChannel, ChannelAdapter):
    """Texts every confirmed SMS subscriber. Edits are not texted; withdrawals send a short
    correction, because a wrong alert must not stand."""

    max_characters = SMS_MAX_CHARACTERS

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        subscribers: SubscriberManager,
        stop_url: str | None = None,
        transport: http.Transport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.subscribers = subscribers
        self.stop_url = stop_url
        self._transport = transport or http.send

    @property
    def name(self) -> str:
        return "sms"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label="SMS alerts",
            max_text_length=SMS_MAX_CHARACTERS,
            shortens_long_text=True,
            custom_text=True,
            update_mode=UpdateMode.NONE,
            withdraw_mode=WithdrawMode.NOTICE,
        )

    def preview_text(self, announcement: Announcement) -> str:
        text = super().preview_text(announcement)
        return f"{text}\nStop: {self.stop_url}" if self.stop_url else text

    def render_text(self, announcement: Announcement) -> str:
        text = super().render_text(announcement)
        return f"{text}\nStop: {self.stop_url}" if self.stop_url else text

    def _auth(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def send(self, numbers: list[str], text: str, message_key: str) -> DeliveryResult:
        failures = []
        for start in range(0, len(numbers), BATCH):
            batch = numbers[start : start + BATCH]
            # A stable ID per batch lets the gateway reject a repeat instead of texting twice.
            message_id = hashlib.sha256(f"{message_key}:{start}".encode()).hexdigest()[:32]
            status, body = self._transport(
                "POST",
                f"{self.base_url}/3rdparty/v1/messages",
                self._auth(),
                {"id": message_id, "textMessage": {"text": text}, "phoneNumbers": batch},
            )
            if not (200 <= status < 300 or status == 409):
                failures.append((status, http.error_text(status, body)))
        if failures:
            status, detail = failures[0]
            return DeliveryResult(
                success=False,
                error=f"SMS gateway refused {len(failures)} batch(es): {detail}",
                retryable=http.retryable(status),
                idempotent=True,
            )
        return DeliveryResult(
            success=True, external_id=f"{len(numbers)} recipients", idempotent=True
        )

    def publish(self, announcement: Announcement) -> DeliveryResult:
        numbers = self.subscribers.get_active_recipients(channel="sms")
        if not numbers:
            return DeliveryResult(success=True, external_id="no subscribers", idempotent=True)
        return self.send(numbers, self.preview_text(announcement), f"publish:{announcement.id}")

    def update(self, announcement: Announcement) -> DeliveryResult:
        return DeliveryResult(success=False, error="Edits are not sent by SMS.")

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        numbers = self.subscribers.get_active_recipients(channel="sms")
        if not numbers:
            return DeliveryResult(success=True, idempotent=True)
        return self.send(
            numbers,
            "Correction: a recent alert was withdrawn by the organizers. Please disregard it.",
            f"withdraw:{announcement_id}",
        )

    def health_check(self) -> HealthResult:
        status, body = self._transport(
            "GET", f"{self.base_url}/3rdparty/v1/devices", self._auth(), None
        )
        if status != 200:
            return HealthResult(
                ok=False, details=f"Gateway refused: {http.error_text(status, body)}"
            )
        devices = body if isinstance(body, list) else (body or {}).get("devices", [])
        count = len(self.subscribers.get_active_recipients(channel="sms"))
        return HealthResult(
            ok=bool(devices),
            details=f"{len(devices)} phone(s) connected; {count} confirmed subscriber(s).",
        )


class SmsSignup:
    """Confirmation codes for joining and leaving the SMS list."""

    def __init__(self, db_path: Path, secret: str) -> None:
        self.db_path = db_path
        self.secret = secret.encode()
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sms_codes (
                    number TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    client_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _hash(self, value: str) -> str:
        return hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()

    def new_code(self, number: str, purpose: str, client: str) -> str:
        """A 6-digit code for ``number``; raises ValueError when sending too many."""
        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=1)).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM sms_codes WHERE created_at < ?", (since,))
            per_number = conn.execute(
                "SELECT COUNT(*) FROM sms_codes WHERE number = ?", (number,)
            ).fetchone()[0]
            per_client = conn.execute(
                "SELECT COUNT(*) FROM sms_codes WHERE client_hash = ?", (self._hash(client),)
            ).fetchone()[0]
            if per_number >= CODES_PER_NUMBER_PER_DAY or per_client >= CODES_PER_CLIENT_PER_DAY:
                raise ValueError("Too many codes were requested today. Try again tomorrow.")
            code = f"{secrets.randbelow(1_000_000):06d}"
            conn.execute(
                "INSERT INTO sms_codes (number, purpose, code_hash, client_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    number,
                    purpose,
                    self._hash(f"{number}:{purpose}:{code}"),
                    self._hash(client),
                    now.isoformat(),
                ),
            )
            return code

    def confirm(self, number: str, purpose: str, code: str) -> bool:
        cutoff = (datetime.now(timezone.utc) - CODE_TTL).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT rowid, code_hash, attempts FROM sms_codes WHERE number = ? AND purpose = ? "
                "AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
                (number, purpose, cutoff),
            ).fetchone()
            if row is None or row["attempts"] >= CODE_ATTEMPTS:
                return False
            conn.execute(
                "UPDATE sms_codes SET attempts = attempts + 1 WHERE rowid = ?", (row["rowid"],)
            )
            if not hmac.compare_digest(
                row["code_hash"], self._hash(f"{number}:{purpose}:{code.strip()}")
            ):
                return False
            conn.execute(
                "UPDATE sms_codes SET attempts = ? WHERE rowid = ?", (CODE_ATTEMPTS, row["rowid"])
            )
            return True
