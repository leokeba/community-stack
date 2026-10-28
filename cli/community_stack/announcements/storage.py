"""Durable SQLite storage for announcements and adapter delivery states."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
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
        conn.execute("PRAGMA foreign_keys = ON")
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
                    operation TEXT NOT NULL DEFAULT 'publish',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT NOT NULL,
                    last_error TEXT,
                    external_id TEXT,
                    retryable INTEGER NOT NULL DEFAULT 0,
                    idempotent INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (announcement_id, adapter_name),
                    FOREIGN KEY (announcement_id) REFERENCES announcements(id) ON DELETE CASCADE
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS channel_settings (
                    adapter_name TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL
                );
            """)
            announcement_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(announcements)")
            }
            if "updated_at" not in announcement_columns:
                conn.execute(
                    "ALTER TABLE announcements ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"
                )
                conn.execute(
                    "UPDATE announcements SET updated_at = COALESCE(NULLIF(created_at, ''), published_at)"
                )

            columns = {row["name"] for row in conn.execute("PRAGMA table_info(delivery_states)")}
            if "operation" not in columns:
                conn.execute(
                    "ALTER TABLE delivery_states ADD COLUMN operation TEXT NOT NULL DEFAULT 'publish'"
                )
            if "attempts" not in columns:
                conn.execute(
                    "ALTER TABLE delivery_states ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
                )
            if "retryable" not in columns:
                conn.execute(
                    "ALTER TABLE delivery_states ADD COLUMN retryable INTEGER NOT NULL DEFAULT 0"
                )
            if "idempotent" not in columns:
                conn.execute(
                    "ALTER TABLE delivery_states ADD COLUMN idempotent INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS delivery_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    announcement_id TEXT NOT NULL,
                    adapter_name TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempted_at TEXT NOT NULL,
                    error TEXT,
                    external_id TEXT,
                    retryable INTEGER NOT NULL DEFAULT 0,
                    idempotent INTEGER NOT NULL DEFAULT 0,
                    legacy_snapshot INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (announcement_id, adapter_name, attempt_number),
                    FOREIGN KEY (announcement_id) REFERENCES announcements(id) ON DELETE CASCADE
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS delivery_attempts_announcement_idx
                ON delivery_attempts (announcement_id, attempted_at, attempt_id);
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS delivery_jobs (
                    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    announcement_id TEXT NOT NULL,
                    adapter_name TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    run_after TEXT NOT NULL,
                    lease_expires_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (announcement_id) REFERENCES announcements(id) ON DELETE CASCADE
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS delivery_jobs_due_idx
                ON delivery_jobs (status, run_after);
            """)
            conn.execute("""
                INSERT OR IGNORE INTO delivery_attempts (
                    announcement_id, adapter_name, attempt_number, operation, status,
                    attempted_at, error, external_id, retryable, idempotent, legacy_snapshot
                )
                SELECT announcement_id, adapter_name, attempts, operation, status,
                    last_attempt_at, last_error, external_id, retryable, idempotent, 1
                FROM delivery_states
                WHERE attempts > 0;
            """)
            conn.commit()

    def save_announcement(self, ann: Announcement) -> None:
        """Insert or update an announcement."""
        now = datetime.now(timezone.utc).isoformat()
        ann.updated_at = now
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
                    published_at=excluded.published_at,
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
            cursor = conn.execute(
                "SELECT * FROM announcements WHERE id = ? AND audience = 'public';",
                (ann_id,),
            )
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
                updated_at=row["updated_at"],
            )

    def list_announcements(
        self,
        status: AnnouncementStatus | None = None,
        limit: int | None = 50,
    ) -> list[Announcement]:
        """List announcements ordered by publication time descending."""
        query = "SELECT * FROM announcements WHERE audience = 'public'"
        params: list[str | int] = []

        if status:
            query += " AND status = ?"
            params.append(status.value)

        query += " ORDER BY published_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        query += ";"

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
                        updated_at=row["updated_at"],
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
        operation: str = "publish",
        retryable: bool = False,
        idempotent: bool = False,
    ) -> None:
        """Record the outcome of a delivery attempt for an adapter."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                """
                SELECT attempts FROM delivery_states
                WHERE announcement_id = ? AND adapter_name = ?;
                """,
                (announcement_id, adapter_name),
            ).fetchone()
            attempt_number = (current["attempts"] if current else 0) + 1
            conn.execute(
                """
                INSERT INTO delivery_attempts (
                    announcement_id, adapter_name, attempt_number, operation, status,
                    attempted_at, error, external_id, retryable, idempotent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    announcement_id,
                    adapter_name,
                    attempt_number,
                    operation,
                    status,
                    now,
                    error,
                    external_id,
                    int(retryable),
                    int(idempotent),
                ),
            )
            conn.execute(
                """
                INSERT INTO delivery_states (
                    announcement_id, adapter_name, status, operation, attempts,
                    last_attempt_at, last_error, external_id, retryable, idempotent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(announcement_id, adapter_name) DO UPDATE SET
                    status=excluded.status,
                    operation=excluded.operation,
                    attempts=excluded.attempts,
                    last_attempt_at=excluded.last_attempt_at,
                    last_error=excluded.last_error,
                    external_id=COALESCE(excluded.external_id, delivery_states.external_id),
                    retryable=excluded.retryable,
                    idempotent=excluded.idempotent
                ;
                """,
                (
                    announcement_id,
                    adapter_name,
                    status,
                    operation,
                    attempt_number,
                    now,
                    error,
                    external_id,
                    int(retryable),
                    int(idempotent),
                ),
            )
            conn.commit()

    def get_delivery_attempts(
        self, announcement_id: str, adapter_name: str | None = None
    ) -> list[dict]:
        """Fetch append-only delivery attempt records in chronological order."""
        query = "SELECT * FROM delivery_attempts WHERE announcement_id = ?"
        params: list[str] = [announcement_id]
        if adapter_name is not None:
            query += " AND adapter_name = ?"
            params.append(adapter_name)
        query += " ORDER BY attempted_at, attempt_id;"
        with self._get_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(row) for row in rows]

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

    def get_channel_settings(self) -> dict[str, bool]:
        """Return persisted channel enablement overrides."""
        with self._get_connection() as conn:
            rows = conn.execute("SELECT adapter_name, enabled FROM channel_settings")
            return {row["adapter_name"]: bool(row["enabled"]) for row in rows}

    def set_channel_enabled(self, adapter_name: str, enabled: bool) -> None:
        """Persist the enabled state of an available adapter."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO channel_settings (adapter_name, enabled) VALUES (?, ?)
                ON CONFLICT(adapter_name) DO UPDATE SET enabled=excluded.enabled;
                """,
                (adapter_name, int(enabled)),
            )

    def enqueue_delivery_job(
        self,
        announcement_id: str,
        adapter_name: str,
        operation: str,
        max_attempts: int,
        run_after: datetime | None = None,
        claim_for: timedelta | None = None,
    ) -> dict:
        """Queue a delivery, superseding any queued job for the same channel.

        With ``claim_for`` the job is returned already leased to the caller, so a
        background worker cannot pick it up while the caller runs it inline.
        """
        now = datetime.now(timezone.utc)
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            superseded = conn.execute(
                """
                SELECT operation FROM delivery_jobs
                WHERE announcement_id = ? AND adapter_name = ? AND status = 'queued';
                """,
                (announcement_id, adapter_name),
            ).fetchall()
            # An update that replaces a never-delivered publish must still publish.
            if operation == "update" and any(row["operation"] == "publish" for row in superseded):
                operation = "publish"
            conn.execute(
                """
                UPDATE delivery_jobs SET status = 'cancelled', updated_at = ?
                WHERE announcement_id = ? AND adapter_name = ? AND status = 'queued';
                """,
                (now.isoformat(), announcement_id, adapter_name),
            )
            status = "running" if claim_for else "queued"
            cursor = conn.execute(
                """
                INSERT INTO delivery_jobs (
                    announcement_id, adapter_name, operation, status, attempts, max_attempts,
                    run_after, lease_expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    announcement_id,
                    adapter_name,
                    operation,
                    status,
                    1 if claim_for else 0,
                    max_attempts,
                    (run_after or now).isoformat(),
                    (now + claim_for).isoformat() if claim_for else None,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM delivery_jobs WHERE job_id = ?;", (cursor.lastrowid,)
            ).fetchone()
            conn.commit()
            return dict(row)

    def claim_due_jobs(self, limit: int, lease: timedelta) -> list[dict]:
        """Lease due queued jobs, and running jobs whose worker lease expired."""
        now = datetime.now(timezone.utc)
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT job_id FROM delivery_jobs
                WHERE (status = 'queued' AND run_after <= ?)
                   OR (status = 'running' AND lease_expires_at <= ?)
                ORDER BY run_after, job_id LIMIT ?;
                """,
                (now.isoformat(), now.isoformat(), limit),
            ).fetchall()
            job_ids = [row["job_id"] for row in rows]
            claimed = []
            for job_id in job_ids:
                conn.execute(
                    """
                    UPDATE delivery_jobs
                    SET status = 'running', attempts = attempts + 1,
                        lease_expires_at = ?, updated_at = ?
                    WHERE job_id = ?;
                    """,
                    ((now + lease).isoformat(), now.isoformat(), job_id),
                )
                claimed.append(
                    dict(
                        conn.execute(
                            "SELECT * FROM delivery_jobs WHERE job_id = ?;", (job_id,)
                        ).fetchone()
                    )
                )
            conn.commit()
            return claimed

    def finish_job(
        self,
        job_id: int,
        status: str,
        error: str | None = None,
        retry_at: datetime | None = None,
    ) -> None:
        """Close a leased job, or requeue it for ``retry_at``."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            if retry_at is not None:
                conn.execute(
                    """
                    UPDATE delivery_jobs
                    SET status = 'queued', run_after = ?, lease_expires_at = NULL,
                        last_error = ?, updated_at = ?
                    WHERE job_id = ?;
                    """,
                    (retry_at.isoformat(), error, now, job_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE delivery_jobs
                    SET status = ?, lease_expires_at = NULL, last_error = ?, updated_at = ?
                    WHERE job_id = ?;
                    """,
                    (status, error, now, job_id),
                )

    def get_active_jobs(self, announcement_id: str | None = None) -> list[dict]:
        """Return queued or running delivery jobs, soonest first."""
        query = "SELECT * FROM delivery_jobs WHERE status IN ('queued', 'running')"
        params: tuple[str, ...] = ()
        if announcement_id is not None:
            query += " AND announcement_id = ?"
            params = (announcement_id,)
        query += " ORDER BY run_after, job_id;"
        with self._get_connection() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]
