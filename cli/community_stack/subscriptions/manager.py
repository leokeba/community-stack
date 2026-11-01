"""Subscription management supporting explicit opt-in, topic filtering, and strict opt-out."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# One list for the CLI, the publisher and the public sign-up pages.
DEFAULT_DB_PATH = "data/announcements/subscribers.db"
# Where the CLI kept its list before CR2.1. Rows are imported on first use.
LEGACY_DB_PATH = "data/subscriptions/subscribers.db"


CONSENT_COLUMNS = ("consent_at", "consent_version", "consent_source", "confirmed_at")


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

    def get_active_recipients(self, channel: str, topic: str | None = None) -> list[str]:
        """Get verified active destination addresses eligible for a topic."""
        active = self.list_subscribers(channel=channel, active_only=True)
        recipients: list[str] = []
        for s in active:
            if not topic or not s.topics or topic in s.topics:
                recipients.append(s.destination)
        return recipients
