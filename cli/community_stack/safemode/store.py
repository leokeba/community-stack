"""SQLite persistence for Safe Mode.

Only operational state is stored: protected rooms, enrolled accounts, each account's
suspension state, the membership snapshot needed to restore it, and salted hashes of
one-time recovery codes. No activation or deactivation timestamps are kept.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

ACTIVE = "ACTIVE"  # represented by the absence of an access_state row
SUSPENDING = "SUSPENDING"
SUSPENDED = "SUSPENDED"
RESTORING = "RESTORING"
PARTIAL = "PARTIAL"

PENDING = "pending"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"

SCHEMA = """
CREATE TABLE IF NOT EXISTS protected_rooms (
    room_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    encryption_enabled INTEGER NOT NULL,
    history_visibility TEXT NOT NULL,
    room_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enrolled_users (
    user_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS access_state (
    user_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    operation_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suspended_memberships (
    user_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    previous TEXT NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY (user_id, room_id)
);
CREATE TABLE IF NOT EXISTS recovery_codes (
    user_id TEXT PRIMARY KEY,
    salt BLOB NOT NULL,
    code_hash BLOB NOT NULL,
    failed_attempts INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class ProtectedRoom:
    room_id: str
    name: str
    enabled: bool
    encryption_enabled: bool
    history_visibility: str
    room_version: str


@dataclass(frozen=True)
class Membership:
    room_id: str
    previous: str
    result: str


class Store:
    """SQLite access. Each thread gets its own connection, so the bot's request threads and
    its reconciler never share one. A busy timeout lets the host CLI and the bot container
    share the file safely."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._opened: list[sqlite3.Connection] = []
        self._opened_lock = threading.Lock()
        self.conn.executescript(SCHEMA)

    @property
    def conn(self) -> sqlite3.Connection:
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            return existing
        # Each connection is used only by the thread that opened it; close() may run elsewhere.
        connection = sqlite3.connect(
            self.path, timeout=10, isolation_level=None, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        self._local.conn = connection
        with self._opened_lock:
            self._opened.append(connection)
        return connection

    def close(self) -> None:
        with self._opened_lock:
            for connection in self._opened:
                connection.close()
            self._opened.clear()
        self._local = threading.local()

    # Rooms -----------------------------------------------------------------

    def upsert_room(self, room: ProtectedRoom) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO protected_rooms
                   (room_id, name, enabled, encryption_enabled, history_visibility, room_version)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(room_id) DO UPDATE SET
                     name = excluded.name, enabled = excluded.enabled,
                     encryption_enabled = excluded.encryption_enabled,
                     history_visibility = excluded.history_visibility,
                     room_version = excluded.room_version""",
                (
                    room.room_id,
                    room.name,
                    int(room.enabled),
                    int(room.encryption_enabled),
                    room.history_visibility,
                    room.room_version,
                ),
            )

    def set_room_enabled(self, room_id: str, enabled: bool) -> bool:
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE protected_rooms SET enabled = ? WHERE room_id = ?", (int(enabled), room_id)
            )
        return cursor.rowcount == 1

    def room(self, room_id: str) -> ProtectedRoom | None:
        row = self.conn.execute(
            "SELECT * FROM protected_rooms WHERE room_id = ?", (room_id,)
        ).fetchone()
        return _room(row) if row else None

    def enabled_rooms(self) -> list[ProtectedRoom]:
        rows = self.conn.execute(
            "SELECT * FROM protected_rooms WHERE enabled = 1 ORDER BY room_id"
        ).fetchall()
        return [_room(row) for row in rows]

    # Accounts --------------------------------------------------------------

    def enroll(self, user_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO enrolled_users (user_id) VALUES (?)", (user_id,)
            )

    def is_enrolled(self, user_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM enrolled_users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row is not None

    def state(self, user_id: str) -> tuple[str, str] | None:
        """(state, operation_id), or None when the account is ACTIVE."""
        row = self.conn.execute(
            "SELECT state, operation_id FROM access_state WHERE user_id = ?", (user_id,)
        ).fetchone()
        return (row["state"], row["operation_id"]) if row else None

    def set_state(self, user_id: str, state: str | None, operation_id: str = "") -> None:
        with self.conn:
            if state is None:
                self.conn.execute("DELETE FROM access_state WHERE user_id = ?", (user_id,))
            else:
                self.conn.execute(
                    """INSERT INTO access_state (user_id, state, operation_id) VALUES (?, ?, ?)
                       ON CONFLICT(user_id) DO UPDATE SET
                         state = excluded.state, operation_id = excluded.operation_id""",
                    (user_id, state, operation_id),
                )

    def begin_suspension(self, user_id: str, operation_id: str, snapshot: dict[str, str]) -> None:
        """Record the state and the membership snapshot in one transaction, before any kick.

        ``INSERT OR IGNORE`` keeps the first snapshot if the operation is ever repeated, so a
        retry cannot overwrite the original previous membership with ``leave``.
        """
        with self.conn:
            self.conn.execute(
                """INSERT INTO access_state (user_id, state, operation_id) VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     state = excluded.state, operation_id = excluded.operation_id""",
                (user_id, SUSPENDING, operation_id),
            )
            for room_id, previous in snapshot.items():
                self.conn.execute(
                    """INSERT OR IGNORE INTO suspended_memberships
                       (user_id, room_id, previous, result) VALUES (?, ?, ?, ?)""",
                    (user_id, room_id, previous, PENDING),
                )

    def memberships(self, user_id: str) -> list[Membership]:
        rows = self.conn.execute(
            "SELECT room_id, previous, result FROM suspended_memberships "
            "WHERE user_id = ? ORDER BY room_id",
            (user_id,),
        ).fetchall()
        return [Membership(row["room_id"], row["previous"], row["result"]) for row in rows]

    def set_result(self, user_id: str, room_id: str, result: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE suspended_memberships SET result = ? WHERE user_id = ? AND room_id = ?",
                (result, user_id, room_id),
            )

    def reset_results(self, user_id: str, result: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE suspended_memberships SET result = ? WHERE user_id = ?", (result, user_id)
            )

    def clear_memberships(self, user_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM suspended_memberships WHERE user_id = ?", (user_id,))

    def pending_accounts(self, states: tuple[str, ...]) -> list[tuple[str, str]]:
        marks = ",".join("?" for _ in states)
        rows = self.conn.execute(
            f"SELECT user_id, state FROM access_state WHERE state IN ({marks}) ORDER BY user_id",
            states,
        ).fetchall()
        return [(row["user_id"], row["state"]) for row in rows]

    # Recovery codes --------------------------------------------------------

    def set_recovery_code(self, user_id: str, salt: bytes, code_hash: bytes) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO recovery_codes (user_id, salt, code_hash, failed_attempts)
                   VALUES (?, ?, ?, 0)
                   ON CONFLICT(user_id) DO UPDATE SET
                     salt = excluded.salt, code_hash = excluded.code_hash, failed_attempts = 0""",
                (user_id, salt, code_hash),
            )

    def recovery_code(self, user_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT salt, code_hash, failed_attempts FROM recovery_codes WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    def record_failed_code(self, user_id: str) -> int:
        with self.conn:
            self.conn.execute(
                "UPDATE recovery_codes SET failed_attempts = failed_attempts + 1 WHERE user_id = ?",
                (user_id,),
            )
            row = self.conn.execute(
                "SELECT failed_attempts FROM recovery_codes WHERE user_id = ?", (user_id,)
            ).fetchone()
        return int(row["failed_attempts"]) if row else 0

    def consume_recovery_code(self, user_id: str) -> None:
        """Codes are one-time: deleting the hash makes a used code worthless."""
        with self.conn:
            self.conn.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))


def _room(row: sqlite3.Row) -> ProtectedRoom:
    return ProtectedRoom(
        room_id=row["room_id"],
        name=row["name"],
        enabled=bool(row["enabled"]),
        encryption_enabled=bool(row["encryption_enabled"]),
        history_visibility=row["history_visibility"],
        room_version=row["room_version"],
    )
