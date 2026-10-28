"""Admin accounts, roles, sessions, login rate limits and the audit log."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SESSION_TTL = timedelta(hours=12)
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_LIMIT = 5
PASSWORD_ITERATIONS = 310_000
AUDIT_RETENTION = timedelta(days=400)

ROLES = ("owner", "admin", "publisher", "reviewer")
ROLE_DESCRIPTIONS = {
    "owner": "Everything, including managing other owners.",
    "admin": "Manage accounts (except owners) and publishing destinations, and publish.",
    "publisher": "Write, publish, edit and withdraw announcements.",
    "reviewer": "Read announcements and delivery history.",
}
PERMISSIONS = {
    "view": {"owner", "admin", "publisher", "reviewer"},
    "edit": {"owner", "admin", "publisher"},
    "settings": {"owner", "admin"},
    "accounts": {"owner", "admin"},
    "audit": {"owner", "admin"},
    "system": {"owner", "admin"},
}


def can(role: str | None, permission: str) -> bool:
    return role in PERMISSIONS[permission]


def tokens_match(expected: str | None, actual: str | None) -> bool:
    return bool(expected and actual) and hmac.compare_digest(
        expected.encode("utf-8"), actual.encode("utf-8")
    )


def password_hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS).hex()


def read_account(db_path: Path | str, username: str) -> dict[str, Any] | None:
    """Look up an active account without writing to, or creating, the database.

    The control agent runs as root, so it must never create files the console
    (running as another user) would then be unable to write.
    """
    path = Path(db_path)
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT username, role, password_salt, password_hash FROM admin_users "
            "WHERE username = ? AND disabled_at IS NULL",
            (username,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return dict(row) if row else None


def account_password_matches(account: dict[str, Any] | None, password: str) -> bool:
    if len(password) > 1024:
        return False
    salt = bytes.fromhex(account["password_salt"]) if account else bytes(16)
    actual = password_hash(password, salt)
    expected = account["password_hash"] if account else "0" * 64
    return hmac.compare_digest(actual, expected) and account is not None


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AdminStore:
    """Authentication, session, rate-limit, account and audit persistence."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS admin_users (
                    username TEXT PRIMARY KEY,
                    password_salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    token_hash TEXT PRIMARY KEY,
                    username TEXT,
                    csrf_token TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_login_attempts (
                    attempt_key TEXT NOT NULL,
                    attempted_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS admin_login_attempts_idx
                    ON admin_login_attempts (attempt_key, attempted_at);
                CREATE TABLE IF NOT EXISTS admin_audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    actor TEXT,
                    action TEXT NOT NULL,
                    target TEXT,
                    details_json TEXT NOT NULL,
                    client TEXT
                );
                CREATE INDEX IF NOT EXISTS admin_audit_events_time_idx
                    ON admin_audit_events (occurred_at);
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(admin_users)")}
            # Accounts created before roles existed were the single administrator.
            if "role" not in columns:
                conn.execute(
                    "ALTER TABLE admin_users ADD COLUMN role TEXT NOT NULL DEFAULT 'owner'"
                )
            if "disabled_at" not in columns:
                conn.execute("ALTER TABLE admin_users ADD COLUMN disabled_at TEXT")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _password_hash(password: str, salt: bytes) -> str:
        return password_hash(password, salt)

    @staticmethod
    def _validate_password(password: str) -> None:
        if len(password) < 12:
            raise ValueError("Use a password with at least 12 characters.")
        if len(password) > 1024:
            raise ValueError("Password must not exceed 1024 characters.")

    @staticmethod
    def _active_owner_count(conn: sqlite3.Connection) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM admin_users WHERE role = 'owner' AND disabled_at IS NULL"
        ).fetchone()[0]

    # Accounts

    def has_admin(self) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM admin_users LIMIT 1").fetchone() is not None

    def create_admin(self, username: str, password: str) -> None:
        """Create the first account, as owner, during initial setup."""
        self._insert_account(username, password, "owner", first=True)

    def create_account(self, username: str, password: str, role: str) -> None:
        self._insert_account(username, password, role, first=False)

    def _insert_account(self, username: str, password: str, role: str, first: bool) -> None:
        normalized = username.strip()
        if not normalized or len(normalized) > 80:
            raise ValueError("Choose a username between 1 and 80 characters.")
        if role not in ROLES:
            raise ValueError("Unknown role.")
        self._validate_password(password)

        salt = secrets.token_bytes(16)
        password_hash = self._password_hash(password, salt)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if first and conn.execute("SELECT 1 FROM admin_users LIMIT 1").fetchone():
                raise ValueError("An administrator has already been created.")
            if conn.execute(
                "SELECT 1 FROM admin_users WHERE username = ?", (normalized,)
            ).fetchone():
                raise ValueError("That username is already taken.")
            conn.execute(
                """
                INSERT INTO admin_users (username, password_salt, password_hash, created_at, role)
                VALUES (?, ?, ?, ?, ?)
                """,
                (normalized, salt.hex(), password_hash, _now().isoformat(), role),
            )

    def get_account(self, username: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT username, role, created_at, disabled_at FROM admin_users "
                "WHERE username = ?",
                (username,),
            ).fetchone()
        return dict(row) if row else None

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT username, role, created_at, disabled_at FROM admin_users "
                "ORDER BY username COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_role(self, username: str, role: str) -> None:
        if role not in ROLES:
            raise ValueError("Unknown role.")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE admin_users SET role = ? WHERE username = ?", (role, username))
            if self._active_owner_count(conn) == 0:
                conn.rollback()
                raise ValueError("At least one active owner must remain.")

    def set_disabled(self, username: str, disabled: bool) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE admin_users SET disabled_at = ? WHERE username = ?",
                (_now().isoformat() if disabled else None, username),
            )
            if self._active_owner_count(conn) == 0:
                conn.rollback()
                raise ValueError("At least one active owner must remain.")
            if disabled:
                conn.execute("DELETE FROM admin_sessions WHERE username = ?", (username,))

    def set_password(self, username: str, password: str, keep_token: str | None = None) -> None:
        """Replace a password and end the account's other sessions."""
        self._validate_password(password)
        salt = secrets.token_bytes(16)
        password_hash = self._password_hash(password, salt)
        keep_hash = self._token_hash(keep_token) if keep_token else ""
        with self._connect() as conn:
            conn.execute(
                "UPDATE admin_users SET password_salt = ?, password_hash = ? WHERE username = ?",
                (salt.hex(), password_hash, username),
            )
            conn.execute(
                "DELETE FROM admin_sessions WHERE username = ? AND token_hash != ?",
                (username, keep_hash),
            )

    def verify_password(self, username: str, password: str) -> bool:
        if len(password) > 1024:
            return False
        with self._connect() as conn:
            user = conn.execute(
                "SELECT password_salt, password_hash FROM admin_users "
                "WHERE username = ? AND disabled_at IS NULL",
                (username.strip(),),
            ).fetchone()
        # Hash even for unknown users so timing does not reveal which usernames exist.
        salt = bytes.fromhex(user["password_salt"]) if user else bytes(16)
        actual = self._password_hash(password, salt)
        expected = user["password_hash"] if user else "0" * 64
        return hmac.compare_digest(actual, expected) and user is not None

    # Sessions

    def new_session(self, username: str | None) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expiry = _now() + SESSION_TTL
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM admin_sessions WHERE expires_at <= ?",
                (_now().isoformat(),),
            )
            conn.execute(
                """
                INSERT INTO admin_sessions (token_hash, username, csrf_token, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (self._token_hash(token), username, csrf_token, expiry.isoformat()),
            )
        return token, csrf_token

    def get_session(self, token: str | None) -> dict[str, Any] | None:
        """Return the session with the account's current role, if both are valid."""
        if not token:
            return None
        token_hash = self._token_hash(token)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT s.username, s.csrf_token, s.expires_at, u.role
                FROM admin_sessions s
                JOIN admin_users u ON u.username = s.username AND u.disabled_at IS NULL
                WHERE s.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if datetime.fromisoformat(row["expires_at"]) <= _now():
                conn.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash,))
                return None
            return dict(row)

    def delete_session(self, token: str | None) -> None:
        if token:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM admin_sessions WHERE token_hash = ?",
                    (self._token_hash(token),),
                )

    # Login rate limits

    @staticmethod
    def _attempt_key(client_host: str, username: str) -> str:
        return hashlib.sha256(f"{client_host}\0{username}".encode()).hexdigest()

    def is_rate_limited(self, client_host: str, username: str, limit: int = LOGIN_LIMIT) -> bool:
        since = (_now() - LOGIN_WINDOW).isoformat()
        key = self._attempt_key(client_host, username)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM admin_login_attempts WHERE attempted_at < ?",
                ((_now() - timedelta(days=1)).isoformat(),),
            )
            attempts = conn.execute(
                """
                SELECT COUNT(*) AS count FROM admin_login_attempts
                WHERE attempt_key = ? AND attempted_at >= ?
                """,
                (key, since),
            ).fetchone()["count"]
        return attempts >= limit

    def record_failed_attempt(self, client_host: str, username: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO admin_login_attempts (attempt_key, attempted_at) VALUES (?, ?)",
                (self._attempt_key(client_host, username), _now().isoformat()),
            )

    def clear_failed_attempts(self, client_host: str, username: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM admin_login_attempts WHERE attempt_key = ?",
                (self._attempt_key(client_host, username),),
            )

    # Audit log

    def record_event(
        self,
        actor: str | None,
        action: str,
        target: str | None = None,
        details: dict[str, Any] | None = None,
        client: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM admin_audit_events WHERE occurred_at < ?",
                ((_now() - AUDIT_RETENTION).isoformat(),),
            )
            conn.execute(
                """
                INSERT INTO admin_audit_events
                    (occurred_at, actor, action, target, details_json, client)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (_now().isoformat(), actor, action, target, json.dumps(details or {}), client),
            )

    def list_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM admin_audit_events ORDER BY event_id DESC LIMIT ?", (limit,)
            ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["details"] = json.loads(event.pop("details_json"))
            events.append(event)
        return events

    def has_event(self, action: str, **details: Any) -> bool:
        """Whether an event with this action and these detail values was ever recorded."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT details_json FROM admin_audit_events WHERE action = ?", (action,)
            ).fetchall()
        return any(
            all(json.loads(row["details_json"]).get(k) == v for k, v in details.items())
            for row in rows
        )
