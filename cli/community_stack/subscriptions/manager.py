"""Subscription management supporting explicit opt-in, topic filtering, and strict opt-out."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# One list for the CLI, the publisher and the public sign-up pages.
DEFAULT_DB_PATH = "data/announcements/subscribers.db"
# Where the CLI kept its list before CR2.1. Rows are imported on first use.
LEGACY_DB_PATH = "data/subscriptions/subscribers.db"


CONSENT_COLUMNS = ("consent_at", "consent_version", "consent_source", "confirmed_at")
# A confirmation link is valid for three days; unconfirmed sign-ups are purged after a week.
CONFIRMATION_TTL = timedelta(days=3)
PENDING_RETENTION = timedelta(days=7)


@dataclass
class Subscriber:
    channel: str  # 'email' or 'sms'
    destination: str  # email address or phone number
    status: str = "active"  # 'active' or 'unsubscribed'
    topics: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: f"sub_{secrets.token_hex(6)}")
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    unsubscribed_at: str | None = None
    # Consent record (CR2.2). Legacy rows have a source of "legacy" and no invented times.
    consent_at: str | None = None
    consent_version: str | None = None
    consent_source: str | None = None
    confirmed_at: str | None = None

    def mask_destination(self) -> str:
        """Mask destination for privacy-preserving display."""
        if self.channel == "email" and "@" in self.destination:
            user, domain = self.destination.split("@", 1)
            masked_user = user[0] + "***" if len(user) > 1 else "***"
            return f"{masked_user}@{domain}"
        elif self.channel == "sms":
            if len(self.destination) > 6:
                return self.destination[:3] + "****" + self.destination[-3:]
            return "****"
        return "***"


class SubscriberManager:
    """Manages newsletter and SMS subscriber lists with guaranteed opt-out."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        if Path(db_path) == Path(DEFAULT_DB_PATH):
            self._import_legacy_rows()

    def _import_legacy_rows(self) -> None:
        """Copy subscribers from the old CLI database. Existing rows are never overwritten."""
        legacy = Path(LEGACY_DB_PATH)
        if not legacy.is_file() or legacy.resolve() == self.db_path:
            return
        source = sqlite3.connect(str(legacy))
        try:
            rows = source.execute(
                "SELECT id, channel, destination, status, topics_json, created_at, unsubscribed_at, "
                "'legacy' FROM subscribers"
            ).fetchall()
        except sqlite3.Error:
            return
        finally:
            source.close()
        with self._get_conn() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO subscribers (
                    id, channel, destination, status, topics_json, created_at, unsubscribed_at,
                    consent_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    status TEXT NOT NULL,
                    topics_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    unsubscribed_at TEXT,
                    UNIQUE(channel, destination)
                );
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(subscribers)")}
            if "consent_source" not in columns:
                for column in CONSENT_COLUMNS:
                    conn.execute(f"ALTER TABLE subscribers ADD COLUMN {column} TEXT")
                # Rows that existed before consent was recorded are marked, never dated.
                conn.execute("UPDATE subscribers SET consent_source = 'legacy'")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(subscribers)")}
            if "confirmation_hash" not in columns:
                conn.execute("ALTER TABLE subscribers ADD COLUMN confirmation_hash TEXT")
                conn.execute("ALTER TABLE subscribers ADD COLUMN confirmation_expires_at TEXT")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signup_requests (
                    client_key TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS email_sends (
                    announcement_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    sent_at TEXT,
                    PRIMARY KEY (announcement_id, operation, destination)
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS consent_texts (
                    version TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            conn.commit()

    def register_consent_text(self, version: str, text: str) -> None:
        """Store the exact words people agreed to. A version's text can never change."""
        if not version or not text.strip():
            raise ValueError("A consent version and its text are required.")
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            existing = conn.execute(
                "SELECT text FROM consent_texts WHERE version = ?", (version,)
            ).fetchone()
            if existing is not None:
                if existing["text"] != text:
                    raise ValueError(f"Consent version '{version}' already has different text.")
                return
            conn.execute(
                "INSERT INTO consent_texts (version, text, created_at) VALUES (?, ?, ?)",
                (version, text, now),
            )
            conn.commit()

    def consent_text(self, version: str) -> str | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT text FROM consent_texts WHERE version = ?", (version,)
            ).fetchone()
        return row["text"] if row else None

    def validate_destination(self, channel: str, destination: str) -> None:
        dest = destination.strip()
        if channel == "email":
            if not re.match(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$", dest):
                raise ValueError(f"Invalid email address: '{destination}'")
        elif channel == "sms":
            if not re.match(r"^\+?[0-9]{8,15}$", dest):
                raise ValueError(f"Invalid phone number format for SMS: '{destination}'")
        else:
            raise ValueError(f"Unsupported subscription channel: '{channel}'")

    def add_subscriber(
        self,
        channel: str,
        destination: str,
        topics: list[str] | None = None,
        consent_version: str | None = None,
        consent_source: str | None = None,
    ) -> Subscriber:
        """Add or reactivate a subscriber, recording the consent they gave."""
        clean_dest = destination.strip().lower()
        self.validate_destination(channel, clean_dest)
        if (consent_version is None) != (consent_source is None):
            raise ValueError("Consent needs both a version and a source.")
        if consent_version is not None:
            if self.consent_text(consent_version) is None:
                raise ValueError(f"Consent version '{consent_version}' has no stored text.")
            if not consent_source or len(consent_source) > 40:
                raise ValueError("The consent source must be 1 to 40 characters.")
        sub = Subscriber(channel=channel, destination=clean_dest, topics=topics or [])

        now = datetime.now(timezone.utc).isoformat()
        consent_at = now if consent_version else None
        sub.consent_at = consent_at
        sub.consent_version = consent_version
        sub.consent_source = consent_source
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO subscribers (
                    id, channel, destination, status, topics_json, created_at, unsubscribed_at,
                    consent_at, consent_version, consent_source
                ) VALUES (?, ?, ?, 'active', ?, ?, NULL, ?, ?, ?)
                ON CONFLICT(channel, destination) DO UPDATE SET
                    status='active',
                    topics_json=excluded.topics_json,
                    unsubscribed_at=NULL,
                    consent_at=COALESCE(excluded.consent_at, consent_at),
                    consent_version=COALESCE(excluded.consent_version, consent_version),
                    consent_source=COALESCE(excluded.consent_source, consent_source)
                ;
                """,
                (
                    sub.id,
                    sub.channel,
                    sub.destination,
                    json.dumps(sub.topics),
                    now,
                    consent_at,
                    consent_version,
                    consent_source,
                ),
            )
            conn.commit()
        return sub

    def unsubscribe(self, channel: str, destination: str) -> bool:
        """Immediately honor an opt-out request."""
        clean_dest = destination.strip().lower()
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                UPDATE subscribers SET
                    status='unsubscribed',
                    unsubscribed_at=?
                WHERE channel = ? AND destination = ?;
                """,
                (now, channel, clean_dest),
            )
            conn.commit()
            return cursor.rowcount > 0

    def list_subscribers(
        self, channel: str | None = None, active_only: bool = True
    ) -> list[Subscriber]:
        """List subscribers with optional filtering."""
        query = "SELECT * FROM subscribers"
        conditions: list[str] = []
        params: list[str] = []

        if channel:
            conditions.append("channel = ?")
            params.append(channel)
        if active_only:
            conditions.append("status = 'active'")

        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC;"

        results: list[Subscriber] = []
        with self._get_conn() as conn:
            cursor = conn.execute(query, tuple(params))
            for row in cursor.fetchall():
                results.append(
                    Subscriber(
                        id=row["id"],
                        channel=row["channel"],
                        destination=row["destination"],
                        status=row["status"],
                        topics=json.loads(row["topics_json"]),
                        created_at=row["created_at"],
                        unsubscribed_at=row["unsubscribed_at"],
                        consent_at=row["consent_at"],
                        consent_version=row["consent_version"],
                        consent_source=row["consent_source"],
                        confirmed_at=row["confirmed_at"],
                    )
                )
        return results

    # Lifecycle (CR2.3). Sign-ups stay pending until the person confirms by link.

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def request_subscription(
        self,
        channel: str,
        destination: str,
        consent_version: str,
        consent_source: str,
        topics: list[str] | None = None,
    ) -> tuple[Subscriber, str | None]:
        """Start a sign-up. Returns the subscriber and a one-time confirmation token.

        Nothing is sent to the address until the token is used. An address that is already
        active gets no new token, and an unsubscribed address must confirm again.
        """
        clean_dest = destination.strip().lower()
        self.validate_destination(channel, clean_dest)
        if self.consent_text(consent_version) is None:
            raise ValueError(f"Consent version '{consent_version}' has no stored text.")
        if not consent_source or len(consent_source) > 40:
            raise ValueError("The consent source must be 1 to 40 characters.")

        existing = self.get_subscriber(channel, clean_dest)
        if existing is not None and existing.status == "active":
            return existing, None

        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires = (now + CONFIRMATION_TTL).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO subscribers (
                    id, channel, destination, status, topics_json, created_at, unsubscribed_at,
                    consent_at, consent_version, consent_source,
                    confirmation_hash, confirmation_expires_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, NULL, ?, ?, ?, ?, ?)
                ON CONFLICT(channel, destination) DO UPDATE SET
                    status='pending',
                    topics_json=excluded.topics_json,
                    unsubscribed_at=NULL,
                    consent_at=excluded.consent_at,
                    consent_version=excluded.consent_version,
                    consent_source=excluded.consent_source,
                    confirmed_at=NULL,
                    confirmation_hash=excluded.confirmation_hash,
                    confirmation_expires_at=excluded.confirmation_expires_at
                ;
                """,
                (
                    f"sub_{secrets.token_hex(6)}",
                    channel,
                    clean_dest,
                    json.dumps(topics or []),
                    now.isoformat(),
                    now.isoformat(),
                    consent_version,
                    consent_source,
                    self._token_hash(token),
                    expires,
                ),
            )
            conn.commit()
        subscriber = self.get_subscriber(channel, clean_dest)
        assert subscriber is not None
        return subscriber, token

    def confirm_subscription(self, token: str) -> Subscriber | None:
        """Activate the sign-up behind a token. A token works once and only before it expires."""
        if not token:
            return None
        now = datetime.now(timezone.utc)
        with self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT channel, destination, confirmation_expires_at FROM subscribers "
                "WHERE confirmation_hash = ? AND status = 'pending'",
                (self._token_hash(token),),
            ).fetchone()
            if row is None or datetime.fromisoformat(row["confirmation_expires_at"]) < now:
                conn.rollback()
                return None
            conn.execute(
                """
                UPDATE subscribers SET
                    status='active', confirmed_at=?, unsubscribed_at=NULL,
                    confirmation_hash=NULL, confirmation_expires_at=NULL
                WHERE channel = ? AND destination = ?
                """,
                (now.isoformat(), row["channel"], row["destination"]),
            )
            conn.commit()
        return self.get_subscriber(row["channel"], row["destination"])

    def get_subscriber(self, channel: str, destination: str) -> Subscriber | None:
        clean_dest = destination.strip().lower()
        match = [
            s
            for s in self.list_subscribers(channel=channel, active_only=False)
            if s.destination == clean_dest
        ]
        return match[0] if match else None

    def change_preferences(self, channel: str, destination: str, topics: list[str]) -> Subscriber:
        """Change which topics an active or pending subscriber receives."""
        sub = self.get_subscriber(channel, destination)
        if sub is None or sub.status == "unsubscribed":
            raise ValueError("No active subscription was found for that address.")
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE subscribers SET topics_json = ? WHERE channel = ? AND destination = ?",
                (json.dumps(topics), channel, sub.destination),
            )
            conn.commit()
        updated = self.get_subscriber(channel, destination)
        assert updated is not None
        return updated

    def delete_subscriber(self, channel: str, destination: str) -> bool:
        """Erase a subscriber's record entirely. Returns False when nothing matched."""
        clean_dest = destination.strip().lower()
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM subscribers WHERE channel = ? AND destination = ?",
                (channel, clean_dest),
            )
            # The send ledger and sign-up limits also hold the address, so they go too.
            conn.execute("DELETE FROM email_sends WHERE destination = ?", (clean_dest,))
            conn.execute("DELETE FROM signup_requests WHERE destination = ?", (clean_dest,))
            conn.commit()
            return cursor.rowcount > 0

    def purge_stale_pending(self, now: datetime | None = None) -> int:
        """Delete sign-ups that were never confirmed within the retention period."""
        cutoff = ((now or datetime.now(timezone.utc)) - PENDING_RETENTION).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM subscribers WHERE status = 'pending' AND created_at < ?", (cutoff,)
            )
            conn.commit()
            return cursor.rowcount

    def export_records(self) -> list[dict]:
        """Everything held about each subscriber, except secrets. For the person concerned."""
        records = []
        for sub in self.list_subscribers(active_only=False):
            records.append(
                {
                    "channel": sub.channel,
                    "destination": sub.destination,
                    "status": sub.status,
                    "topics": sub.topics,
                    "created_at": sub.created_at,
                    "unsubscribed_at": sub.unsubscribed_at,
                    "consent_at": sub.consent_at,
                    "consent_version": sub.consent_version,
                    "consent_source": sub.consent_source,
                    "confirmed_at": sub.confirmed_at,
                }
            )
        return records

    # Sign-up abuse limits (CR2.6). Only hashed client keys are stored.

    def allow_signup(self, client_key: str, destination: str) -> bool:
        """Record a sign-up request if it is within limits, so bots and mail-bombing stop early.

        Limits: five requests an hour from one client, and three requests a day for one address.
        """
        now = datetime.now(timezone.utc)
        hour_ago = (now - timedelta(hours=1)).isoformat()
        day_ago = (now - timedelta(days=1)).isoformat()
        with self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM signup_requests WHERE created_at < ?", (day_ago,))
            from_client = conn.execute(
                "SELECT COUNT(*) FROM signup_requests WHERE client_key = ? AND created_at >= ?",
                (client_key, hour_ago),
            ).fetchone()[0]
            for_address = conn.execute(
                "SELECT COUNT(*) FROM signup_requests WHERE destination = ?",
                (destination.strip().lower(),),
            ).fetchone()[0]
            if from_client >= 5 or for_address >= 3:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO signup_requests (client_key, destination, created_at) VALUES (?, ?, ?)",
                (client_key, destination.strip().lower(), now.isoformat()),
            )
            conn.commit()
        return True

    # Per-person send ledger (CR2.5). A retry mails only people who have not received it yet.

    def queue_email_send(
        self, announcement_id: str, operation: str, destinations: list[str]
    ) -> None:
        """Record who should receive an announcement. The audience is fixed on the first attempt."""
        with self._get_conn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM email_sends WHERE announcement_id = ? AND operation = ? LIMIT 1",
                (announcement_id, operation),
            ).fetchone()
            if exists is None:
                conn.executemany(
                    "INSERT OR IGNORE INTO email_sends (announcement_id, operation, destination) "
                    "VALUES (?, ?, ?)",
                    [(announcement_id, operation, d) for d in destinations],
                )
                conn.commit()

    def pending_email_sends(self, announcement_id: str, operation: str) -> list[str]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT destination FROM email_sends WHERE announcement_id = ? AND operation = ? "
                "AND sent_at IS NULL ORDER BY destination",
                (announcement_id, operation),
            ).fetchall()
        return [row["destination"] for row in rows]

    def email_send_progress(self, announcement_id: str, operation: str) -> tuple[int, int]:
        """Return (people in the audience, people already sent to)."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total, COUNT(sent_at) AS sent FROM email_sends "
                "WHERE announcement_id = ? AND operation = ?",
                (announcement_id, operation),
            ).fetchone()
        return row["total"], row["sent"]

    def mark_email_sent(self, announcement_id: str, operation: str, destination: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE email_sends SET sent_at = ? WHERE announcement_id = ? AND operation = ? "
                "AND destination = ?",
                (datetime.now(timezone.utc).isoformat(), announcement_id, operation, destination),
            )
            conn.commit()

    def get_active_recipients(self, channel: str, topic: str | None = None) -> list[str]:
        """Get verified active destination addresses eligible for a topic."""
        active = self.list_subscribers(channel=channel, active_only=True)
        recipients: list[str] = []
        for s in active:
            if not topic or not s.topics or topic in s.topics:
                recipients.append(s.destination)
        return recipients
