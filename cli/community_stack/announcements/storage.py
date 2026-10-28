"""Durable SQLite storage for announcements and adapter delivery states."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from community_stack.announcements.models import Announcement, AnnouncementStatus


class AnnouncementStorage:
    """Manages persistence for canonical announcements and adapter delivery status."""

    def __init__(self, db_path: Path | str = "data/announcements/announcements.db") -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS announcements (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    language TEXT NOT NULL,
                    status TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    expires_at TEXT,
                    audience TEXT NOT NULL,
                    author TEXT,
                    tags_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS delivery_states (
                    announcement_id TEXT NOT NULL,
                    adapter_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT NOT NULL,
                    last_error TEXT,
                    external_id TEXT,
                    PRIMARY KEY (announcement_id, adapter_name),
                    FOREIGN KEY (announcement_id) REFERENCES announcements(id) ON DELETE CASCADE
                );
            """)
            conn.commit()

    def save_announcement(self, ann: Announcement) -> None:
        """Insert or update an announcement."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO announcements (
                    id, title, body, published_at, language, status,
                    canonical_url, expires_at, audience, author, tags_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    body=excluded.body,
                    language=excluded.language,
                    status=excluded.status,
                    canonical_url=excluded.canonical_url,
                    expires_at=excluded.expires_at,
                    audience=excluded.audience,
                    author=excluded.author,
                    tags_json=excluded.tags_json,
                    updated_at=excluded.updated_at
                ;
                """,
                (
                    ann.id,
                    ann.title,
                    ann.body,
                    ann.published_at,
                    ann.language,
                    ann.status.value,
                    ann.canonical_url,
                    ann.expires_at,
                    ann.audience,
                    ann.author,
                    json.dumps(ann.tags),
                    now,
                    now,
                ),
            )
            conn.commit()

    def get_announcement(self, ann_id: str) -> Announcement | None:
        """Fetch announcement by unique ID."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM announcements WHERE id = ?;", (ann_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return Announcement(
                id=row["id"],
                title=row["title"],
                body=row["body"],
                published_at=row["published_at"],
                language=row["language"],
                status=AnnouncementStatus(row["status"]),
                canonical_url=row["canonical_url"],
                expires_at=row["expires_at"],
                audience=row["audience"],
                author=row["author"],
                tags=json.loads(row["tags_json"]),
            )

    def list_announcements(
        self,
        status: AnnouncementStatus | None = None,
        limit: int = 50,
    ) -> list[Announcement]:
        """List announcements ordered by publication time descending."""
        query = "SELECT * FROM announcements"
        params: list[str | int] = []

        if status:
            query += " WHERE status = ?"
            params.append(status.value)

        query += " ORDER BY published_at DESC LIMIT ?;"
        params.append(limit)

        results: list[Announcement] = []
        with self._get_connection() as conn:
            cursor = conn.execute(query, tuple(params))
            for row in cursor.fetchall():
                results.append(
                    Announcement(
                        id=row["id"],
                        title=row["title"],
                        body=row["body"],
                        published_at=row["published_at"],
                        language=row["language"],
                        status=AnnouncementStatus(row["status"]),
                        canonical_url=row["canonical_url"],
                        expires_at=row["expires_at"],
                        audience=row["audience"],
                        author=row["author"],
                        tags=json.loads(row["tags_json"]),
                    )
                )
        return results

    def record_delivery(
        self,
        announcement_id: str,
        adapter_name: str,
        status: str,
        error: str | None = None,
        external_id: str | None = None,
    ) -> None:
        """Record the outcome of a delivery attempt for an adapter."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO delivery_states (
                    announcement_id, adapter_name, status, attempts,
                    last_attempt_at, last_error, external_id
                ) VALUES (?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(announcement_id, adapter_name) DO UPDATE SET
                    status=excluded.status,
                    attempts=delivery_states.attempts + 1,
                    last_attempt_at=excluded.last_attempt_at,
                    last_error=excluded.last_error,
                    external_id=COALESCE(excluded.external_id, delivery_states.external_id)
                ;
                """,
                (announcement_id, adapter_name, status, now, error, external_id),
            )
            conn.commit()

    def get_delivery_states(self, announcement_id: str) -> list[dict]:
        """Fetch all delivery records for a specific announcement."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM delivery_states WHERE announcement_id = ? ORDER BY adapter_name;",
                (announcement_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_failed_deliveries(self) -> list[dict]:
        """Return all failed deliveries that require retry."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM delivery_states WHERE status = 'failed';")
            return [dict(row) for row in cursor.fetchall()]
