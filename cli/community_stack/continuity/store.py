"""Durable state, events and resumable steps for continuity operations."""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CHANNEL_STATES = frozenset({"operational", "degraded", "unavailable", "retired"})
EVENT_ACTIONS = frozenset({"activated", "deactivated", "channel_state_changed"})
STEP_STATES = frozenset({"running", "completed", "failed"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ContinuityStore:
    """Persist manual channel states and append-only continuity history."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS continuity_channel_states (
                    channel_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK (
                        status IN ('operational', 'degraded', 'unavailable', 'retired')
                    ),
                    reason TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    actor TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS continuity_events (
                    event_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL CHECK (
                        action IN ('activated', 'deactivated', 'channel_state_changed')
                    ),
                    occurred_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    affected_channels_json TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS continuity_events_no_update
                BEFORE UPDATE ON continuity_events
                BEGIN
                    SELECT RAISE(ABORT, 'continuity events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS continuity_events_no_delete
                BEFORE DELETE ON continuity_events
                BEGIN
                    SELECT RAISE(ABORT, 'continuity events are append-only');
                END;
                CREATE TABLE IF NOT EXISTS continuity_current (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    event_id TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK (active IN (0, 1)),
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    affected_channels_json TEXT NOT NULL,
                    translations_json TEXT NOT NULL,
                    notice_translations_json TEXT NOT NULL DEFAULT '{}',
                    changed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS continuity_steps (
                    event_id TEXT NOT NULL,
                    step TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (event_id, step)
                );
                """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(continuity_current)")}
            if "notice_translations_json" not in columns:
                conn.execute(
                    "ALTER TABLE continuity_current ADD COLUMN notice_translations_json "
                    "TEXT NOT NULL DEFAULT '{}'"
                )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _event(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["affected_channels"] = json.loads(item.pop("affected_channels_json"))
        return item

    @staticmethod
    def _current(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["active"] = bool(item["active"])
        item["affected_channels"] = json.loads(item.pop("affected_channels_json"))
        item["translations"] = json.loads(item.pop("translations_json"))
        item["notice_translations"] = json.loads(item.pop("notice_translations_json"))
        return item

    def list_channel_states(self) -> dict[str, dict[str, str]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT channel_key, status, reason, updated_at, actor "
                "FROM continuity_channel_states ORDER BY channel_key"
            ).fetchall()
        return {row["channel_key"]: dict(row) for row in rows}

    def set_channel_state(self, channel: str, status: str, reason: str, actor: str) -> None:
        if status not in CHANNEL_STATES:
            raise ValueError("Choose operational, degraded, unavailable or retired.")
        changed_at = _now()
        normalized_reason = " ".join(reason.split())[:500]
        normalized_actor = actor.strip()[:100]
        if not normalized_reason:
            raise ValueError("A reason is required for a channel state.")
        if not normalized_actor:
            raise ValueError("An actor is required for a channel state.")
        event_id = f"cont_{secrets.token_hex(12)}"
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO continuity_channel_states "
                "(channel_key, status, reason, updated_at, actor) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(channel_key) DO UPDATE SET status=excluded.status, "
                "reason=excluded.reason, updated_at=excluded.updated_at, actor=excluded.actor",
                (channel, status, normalized_reason, changed_at, normalized_actor),
            )
            conn.execute(
                "INSERT INTO continuity_events "
                "(event_id, action, occurred_at, actor, reason, affected_channels_json) "
                "VALUES (?, 'channel_state_changed', ?, ?, ?, ?)",
                (
                    event_id,
                    changed_at,
                    normalized_actor,
                    normalized_reason,
                    json.dumps([channel]),
                ),
            )

    def start_activation(
        self,
        actor: str,
        reason: str,
        affected_channels: list[str],
        translations: dict[str, dict[str, str]],
        notice_translations: dict[str, dict[str, str]] | None = None,
        occurred_at: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create an activation once, or return the active operation for resumption."""
        actor = actor.strip()[:100]
        reason = " ".join(reason.split())[:500]
        if not actor or not reason:
            raise ValueError("An actor and reason are required to activate continuity mode.")
        affected_channels = sorted(set(affected_channels))
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM continuity_current WHERE singleton = 1").fetchone()
            current = self._current(row)
            if current and current["active"]:
                return current, False

            event_id = f"cont_{secrets.token_hex(12)}"
            occurred_at = occurred_at or _now()
            conn.execute(
                "INSERT INTO continuity_events "
                "(event_id, action, occurred_at, actor, reason, affected_channels_json) "
                "VALUES (?, 'activated', ?, ?, ?, ?)",
                (event_id, occurred_at, actor, reason, json.dumps(affected_channels)),
            )
            conn.execute(
                "INSERT INTO continuity_current "
                "(singleton, event_id, active, actor, reason, affected_channels_json, "
                "translations_json, notice_translations_json, changed_at) "
                "VALUES (1, ?, 1, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET event_id=excluded.event_id, active=1, "
                "actor=excluded.actor, reason=excluded.reason, "
                "affected_channels_json=excluded.affected_channels_json, "
                "translations_json=excluded.translations_json, "
                "notice_translations_json=excluded.notice_translations_json, "
                "changed_at=excluded.changed_at",
                (
                    event_id,
                    actor,
                    reason,
                    json.dumps(affected_channels),
                    json.dumps(translations, ensure_ascii=False),
                    json.dumps(notice_translations or {}, ensure_ascii=False),
                    occurred_at,
                ),
            )
            for channel in affected_channels:
                conn.execute(
                    "INSERT INTO continuity_channel_states "
                    "(channel_key, status, reason, updated_at, actor) "
                    "VALUES (?, 'unavailable', ?, ?, ?) "
                    "ON CONFLICT(channel_key) DO UPDATE SET status='unavailable', "
                    "reason=excluded.reason, updated_at=excluded.updated_at, actor=excluded.actor",
                    (channel, reason, occurred_at, actor),
                )
            current = self._current(
                conn.execute("SELECT * FROM continuity_current WHERE singleton = 1").fetchone()
            )
        if current is None:
            raise RuntimeError("The continuity activation state was not saved.")
        return current, True

    def start_deactivation(self, actor: str, reason: str) -> tuple[dict[str, Any], bool]:
        actor = actor.strip()[:100]
        reason = " ".join(reason.split())[:500]
        if not actor or not reason:
            raise ValueError("An actor and reason are required to deactivate continuity mode.")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM continuity_current WHERE singleton = 1").fetchone()
            current = self._current(row)
            if current is None:
                raise ValueError("Continuity mode has not been activated.")
            if not current["active"]:
                event = conn.execute(
                    "SELECT action FROM continuity_events WHERE event_id = ?",
                    (current["event_id"],),
                ).fetchone()
                if event and event["action"] == "deactivated":
                    return current, False
                raise ValueError("Continuity mode is not active.")

            event_id = f"cont_{secrets.token_hex(12)}"
            occurred_at = _now()
            conn.execute(
                "INSERT INTO continuity_events "
                "(event_id, action, occurred_at, actor, reason, affected_channels_json) "
                "VALUES (?, 'deactivated', ?, ?, ?, ?)",
                (event_id, occurred_at, actor, reason, json.dumps(current["affected_channels"])),
            )
            conn.execute(
                "UPDATE continuity_current SET event_id = ?, active = 0, actor = ?, "
                "reason = ?, changed_at = ? WHERE singleton = 1",
                (event_id, actor, reason, occurred_at),
            )
            current = self._current(
                conn.execute("SELECT * FROM continuity_current WHERE singleton = 1").fetchone()
            )
        if current is None:
            raise RuntimeError("The continuity deactivation state was not saved.")
        return current, True

    def get_current(self) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM continuity_current WHERE singleton = 1").fetchone()
        return self._current(row)

    def active_notice(self) -> dict[str, Any] | None:
        current = self.get_current()
        return current if current and current["active"] else None

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM continuity_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return self._event(row)

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM continuity_events ORDER BY occurred_at DESC, rowid DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [self._event(row) for row in rows if row is not None]

    def begin_step(self, event_id: str, step: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO continuity_steps (event_id, step, status, attempts, updated_at) "
                "VALUES (?, ?, 'running', 1, ?) ON CONFLICT(event_id, step) DO UPDATE SET "
                "status='running', attempts=continuity_steps.attempts + 1, "
                "error=NULL, updated_at=excluded.updated_at",
                (event_id, step, _now()),
            )

    def finish_step(self, event_id: str, step: str, error: str | None = None) -> None:
        status = "failed" if error else "completed"
        with self._connection() as conn:
            conn.execute(
                "UPDATE continuity_steps SET status = ?, error = ?, updated_at = ? "
                "WHERE event_id = ? AND step = ?",
                (status, error[:1000] if error else None, _now(), event_id, step),
            )

    def step_completed(self, event_id: str, step: str) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT status FROM continuity_steps WHERE event_id = ? AND step = ?",
                (event_id, step),
            ).fetchone()
        return bool(row and row["status"] == "completed")

    def list_steps(self, event_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT step, status, attempts, error, updated_at FROM continuity_steps "
                "WHERE event_id = ? ORDER BY step",
                (event_id,),
            ).fetchall()
        return [dict(row) for row in rows]
