"""SQLite persistence for Safe Mode.

Only operational state is stored: protected rooms, enrolled accounts, each account's
suspension state, the membership snapshot needed to restore it, and salted hashes of
one-time recovery codes. No activation or deactivation timestamps are kept.

Every state change is one ``BEGIN IMMEDIATE`` transaction, so the bot, the operator commands
in the container and the control agent on the host can share the file. Each suspension or
restore has an operation id; the writes made while carrying it out only apply while that id
is still current, so a newer operation always wins over an older one still in flight.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

ACTIVE = "ACTIVE"  # represented by the absence of an access_state row
SUSPENDING = "SUSPENDING"
SUSPENDED = "SUSPENDED"
RESTORING = "RESTORING"
PARTIAL = "PARTIAL"
SUSPENDED_STATES = (SUSPENDING, SUSPENDED, PARTIAL)

PENDING = "pending"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"

# Values of ``suspended_memberships.previous`` besides a Matrix membership ("join", "invite").
UNKNOWN = "unknown"  # not read yet; resolved before any kick
NOT_MEMBER = "none"  # resolved: the account was not in the room, so nothing was suspended

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


class StoreMissing(Exception):
    """The database does not exist and this caller must not create it."""


class StateConflict(Exception):
    """The account is not in a state that allows the requested change."""

    def __init__(self, state: str) -> None:
        super().__init__(state)
        self.state = state


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
    """SQLite access. Each thread gets its own connection, so the bot's worker threads and
    its reconciler never share one. A busy timeout lets several processes share the file.

    ``create=False`` opens an existing database only. The control agent runs as root on the
    host and must never create the file, or the bot (running as the community account) could
    not write to it.
    """

    def __init__(self, path: Path | str, *, create: bool = True) -> None:
        self.path = str(path)
        self.create = create
        if self.path != ":memory:":
            if create:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            elif not Path(self.path).is_file():
                raise StoreMissing(self.path)
        self._local = threading.local()
        self._opened: list[sqlite3.Connection] = []
        self._opened_lock = threading.Lock()
        if create:
            self.conn.executescript(SCHEMA)

    @property
    def conn(self) -> sqlite3.Connection:
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            return existing
        if self.create or self.path == ":memory:":
            target, uri = self.path, False
        else:
            target, uri = f"file:{Path(self.path).resolve()}?mode=rw", True
        # Each connection is used only by the thread that opened it; close() may run elsewhere.
        # isolation_level=None: no implicit transactions; transaction() opens them explicitly.
        connection = sqlite3.connect(
            target, timeout=10, isolation_level=None, check_same_thread=False, uri=uri
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        self._local.conn = connection
        with self._opened_lock:
            self._opened.append(connection)
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One write transaction. ``BEGIN IMMEDIATE`` takes the write lock up front, so a
        read-then-write inside it cannot interleave with another process."""
        conn = self.conn
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def close(self) -> None:
        with self._opened_lock:
            for connection in self._opened:
                connection.close()
            self._opened.clear()
        self._local = threading.local()

    # Rooms -----------------------------------------------------------------

    def upsert_room(self, room: ProtectedRoom) -> None:
        with self.transaction() as conn:
            conn.execute(
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
        with self.transaction() as conn:
            cursor = conn.execute(
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
        with self.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO enrolled_users (user_id) VALUES (?)", (user_id,))

    def is_enrolled(self, user_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM enrolled_users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row is not None

    def enrolled_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM enrolled_users").fetchone()[0])

    def state(self, user_id: str) -> tuple[str, str] | None:
        """(state, operation_id), or None when the account is ACTIVE."""
        row = self.conn.execute(
            "SELECT state, operation_id FROM access_state WHERE user_id = ?", (user_id,)
        ).fetchone()
        return (row["state"], row["operation_id"]) if row else None

    def memberships(self, user_id: str) -> list[Membership]:
        rows = self.conn.execute(
            "SELECT room_id, previous, result FROM suspended_memberships "
            "WHERE user_id = ? ORDER BY room_id",
            (user_id,),
        ).fetchall()
        return [Membership(row["room_id"], row["previous"], row["result"]) for row in rows]

    def pending_accounts(self, states: tuple[str, ...]) -> list[tuple[str, str]]:
        marks = ",".join("?" for _ in states)
        rows = self.conn.execute(
            f"SELECT user_id, state FROM access_state WHERE state IN ({marks}) ORDER BY user_id",
            states,
        ).fetchall()
        return [(row["user_id"], row["state"]) for row in rows]

    # State transitions -----------------------------------------------------

    def begin_suspension(self, user_id: str, operation_id: str, snapshot: dict[str, str]) -> None:
        """Record SUSPENDING and the membership snapshot in one transaction, before any kick.

        ``INSERT OR IGNORE`` keeps the first snapshot if the operation is ever repeated, so a
        retry cannot overwrite the original previous membership with ``leave``.
        """
        with self.transaction() as conn:
            self._set_state(conn, user_id, SUSPENDING, operation_id)
            for room_id, previous in snapshot.items():
                conn.execute(
                    """INSERT OR IGNORE INTO suspended_memberships
                       (user_id, room_id, previous, result) VALUES (?, ?, ?, ?)""",
                    (user_id, room_id, previous, PENDING),
                )

    def start_suspension(self, user_id: str, operation_id: str, room_ids: list[str]) -> str:
        """Begin a suspension, or take over from whatever operation is current. Returns the
        state it replaced (ACTIVE when there was none).

        Every protected room is added to the snapshot as ``unknown``; its real membership is
        read just before the kick. Rooms already in the snapshot keep their recorded previous
        membership. Taking over from a restore puts every room it brought back (or tried to)
        back to pending; rooms it skipped because they are no longer protected stay skipped.
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT state FROM access_state WHERE user_id = ?", (user_id,)
            ).fetchone()
            replaced = row["state"] if row else ACTIVE
            self._set_state(conn, user_id, SUSPENDING, operation_id)
            if replaced == RESTORING:
                conn.execute(
                    "UPDATE suspended_memberships SET result = ? WHERE user_id = ? AND result != ?",
                    (PENDING, user_id, SKIPPED),
                )
            for room_id in room_ids:
                conn.execute(
                    """INSERT OR IGNORE INTO suspended_memberships
                       (user_id, room_id, previous, result) VALUES (?, ?, ?, ?)""",
                    (user_id, room_id, UNKNOWN, PENDING),
                )
        return replaced

    def start_restore(self, user_id: str, operation_id: str, code_hash: bytes | None) -> None:
        """SUSPENDED or PARTIAL to RESTORING, spending the recovery code, in one transaction.

        With ``code_hash`` the code is spent only if it is still the one that was checked; a
        code reissued in between makes this fail rather than delete the new one. Without it
        (administrative restore) any outstanding code is removed.
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT state FROM access_state WHERE user_id = ?", (user_id,)
            ).fetchone()
            current = row["state"] if row else ACTIVE
            if current not in (SUSPENDED, PARTIAL):
                raise StateConflict(current)
            if code_hash is None:
                conn.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
            else:
                spent = conn.execute(
                    "DELETE FROM recovery_codes WHERE user_id = ? AND code_hash = ?",
                    (user_id, code_hash),
                )
                if spent.rowcount != 1:
                    raise StateConflict("CODE_CHANGED")
            self._set_state(conn, user_id, RESTORING, operation_id)
            conn.execute(
                "UPDATE suspended_memberships SET result = ? WHERE user_id = ?", (PENDING, user_id)
            )

    def reopen_room(self, user_id: str, room_id: str) -> bool:
        """A suspended account is back in a protected room: mark the room outstanding so the
        current operation removes it again. A room new to the snapshot is recorded as one the
        account was not in before suspension, so a restore does not bring it back."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT state FROM access_state WHERE user_id = ?", (user_id,)
            ).fetchone()
            if not row or row["state"] not in SUSPENDED_STATES:
                return False
            conn.execute(
                """INSERT INTO suspended_memberships (user_id, room_id, previous, result)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id, room_id) DO UPDATE SET result = excluded.result""",
                (user_id, room_id, NOT_MEMBER, PENDING),
            )
            conn.execute(
                "UPDATE access_state SET state = ? WHERE user_id = ?", (SUSPENDING, user_id)
            )
        return True

    def is_current(self, user_id: str, operation_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM access_state WHERE user_id = ? AND operation_id = ?",
            (user_id, operation_id),
        ).fetchone()
        return row is not None

    def set_result(
        self, user_id: str, room_id: str, result: str, operation_id: str | None = None
    ) -> bool:
        """Record one room's outcome. With ``operation_id``, only while it is still current."""
        with self.transaction() as conn:
            if operation_id is not None and not self.is_current(user_id, operation_id):
                return False
            conn.execute(
                "UPDATE suspended_memberships SET result = ? WHERE user_id = ? AND room_id = ?",
                (result, user_id, room_id),
            )
        return True

    def resolve_previous(self, user_id: str, room_id: str, previous: str) -> None:
        """Fill in a membership not read yet. A known value is never changed."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE suspended_memberships SET previous = ? "
                "WHERE user_id = ? AND room_id = ? AND previous = ?",
                (previous, user_id, room_id, UNKNOWN),
            )

    def finish_operation(self, user_id: str, operation_id: str, state: str | None) -> bool:
        """Move the account to ``state`` (None: ACTIVE, which also drops the snapshot), only if
        ``operation_id`` is still current. Returns False when a newer operation took over."""
        with self.transaction() as conn:
            if not self.is_current(user_id, operation_id):
                return False
            if state is None:
                conn.execute("DELETE FROM suspended_memberships WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM access_state WHERE user_id = ?", (user_id,))
            else:
                conn.execute(
                    "UPDATE access_state SET state = ? WHERE user_id = ?", (state, user_id)
                )
        return True

    @staticmethod
    def _set_state(conn: sqlite3.Connection, user_id: str, state: str, operation_id: str) -> None:
        conn.execute(
            """INSERT INTO access_state (user_id, state, operation_id) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 state = excluded.state, operation_id = excluded.operation_id""",
            (user_id, state, operation_id),
        )

    # Recovery codes --------------------------------------------------------

    def set_recovery_code(self, user_id: str, salt: bytes, code_hash: bytes) -> None:
        with self.transaction() as conn:
            conn.execute(
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
        with self.transaction() as conn:
            conn.execute(
                "UPDATE recovery_codes SET failed_attempts = failed_attempts + 1 WHERE user_id = ?",
                (user_id,),
            )
            row = conn.execute(
                "SELECT failed_attempts FROM recovery_codes WHERE user_id = ?", (user_id,)
            ).fetchone()
        return int(row["failed_attempts"]) if row else 0


def _room(row: sqlite3.Row) -> ProtectedRoom:
    return ProtectedRoom(
        room_id=row["room_id"],
        name=row["name"],
        enabled=bool(row["enabled"]),
        encryption_enabled=bool(row["encryption_enabled"]),
        history_visibility=row["history_visibility"],
        room_version=row["room_version"],
    )
