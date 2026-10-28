"""Subscription management supporting explicit opt-in, topic filtering, and strict opt-out."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Subscriber:
    channel: str  # 'email' or 'sms'
    destination: str  # email address or phone number
    status: str = "active"  # 'active' or 'unsubscribed'
    topics: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: f"sub_{secrets.token_hex(6)}")
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    unsubscribed_at: str | None = None

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

    def __init__(self, db_path: Path | str = "data/subscriptions/subscribers.db") -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

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
            conn.commit()

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
    ) -> Subscriber:
        """Add or reactivate a subscriber with explicit opt-in."""
        clean_dest = destination.strip().lower()
        self.validate_destination(channel, clean_dest)
        sub = Subscriber(channel=channel, destination=clean_dest, topics=topics or [])

        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO subscribers (
                    id, channel, destination, status, topics_json, created_at, unsubscribed_at
                ) VALUES (?, ?, ?, 'active', ?, ?, NULL)
                ON CONFLICT(channel, destination) DO UPDATE SET
                    status='active',
                    topics_json=excluded.topics_json,
                    unsubscribed_at=NULL
                ;
                """,
                (sub.id, sub.channel, sub.destination, json.dumps(sub.topics), now),
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
