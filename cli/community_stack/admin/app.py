"""Authenticated, server-rendered announcement administration."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.models import ModelValidationError
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage

SESSION_COOKIE = "community_admin_session"
PREAUTH_CSRF_COOKIE = "community_admin_csrf"
SESSION_TTL = timedelta(hours=12)
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_LIMIT = 5
LOGIN_IP_LIMIT = 20
ANY_USERNAME = "\0any"
PASSWORD_ITERATIONS = 310_000
TEMPLATE_DIR = Path(__file__).parent / "templates"
WORKER_INTERVAL_SECONDS = 15.0

logger = logging.getLogger(__name__)


class AdminStore:
    """Authentication, session, rate-limit and channel setting persistence."""

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
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _password_hash(password: str, salt: bytes) -> str:
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
        ).hex()

    def has_admin(self) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM admin_users LIMIT 1").fetchone() is not None

    def create_admin(self, username: str, password: str) -> None:
        normalized = username.strip()
        if not normalized or len(normalized) > 80:
            raise ValueError("Choose a username between 1 and 80 characters.")
        if len(password) < 12:
            raise ValueError("Use a password with at least 12 characters.")
        if len(password) > 1024:
            raise ValueError("Password must not exceed 1024 characters.")

        salt = secrets.token_bytes(16)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM admin_users LIMIT 1").fetchone():
                raise ValueError("An administrator has already been created.")
            conn.execute(
                """
                INSERT INTO admin_users (username, password_salt, password_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    normalized,
                    salt.hex(),
                    self._password_hash(password, salt),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def verify_password(self, username: str, password: str) -> bool:
        if len(password) > 1024:
            return False
        with self._connect() as conn:
            user = conn.execute(
                "SELECT password_salt, password_hash FROM admin_users WHERE username = ?",
                (username.strip(),),
            ).fetchone()
        salt = bytes.fromhex(user["password_salt"]) if user else bytes(16)
        actual = self._password_hash(password, salt)
        expected = user["password_hash"] if user else "0" * 64
        return hmac.compare_digest(actual, expected) and user is not None

    def new_session(self, username: str | None) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expiry = datetime.now(timezone.utc) + SESSION_TTL
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM admin_sessions WHERE expires_at <= ?",
                (datetime.now(timezone.utc).isoformat(),),
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
        if not token:
            return None
        token_hash = self._token_hash(token)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT username, csrf_token, expires_at FROM admin_sessions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
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

    @staticmethod
    def _attempt_key(client_host: str, username: str) -> str:
        return hashlib.sha256(f"{client_host}\0{username}".encode()).hexdigest()

    def is_rate_limited(self, client_host: str, username: str, limit: int = LOGIN_LIMIT) -> bool:
        since = (datetime.now(timezone.utc) - LOGIN_WINDOW).isoformat()
        key = self._attempt_key(client_host, username)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM admin_login_attempts WHERE attempted_at < ?",
                ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),),
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
                (
                    self._attempt_key(client_host, username),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def clear_failed_attempts(self, client_host: str, username: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM admin_login_attempts WHERE attempt_key = ?",
                (self._attempt_key(client_host, username),),
            )


def tokens_match(expected: str | None, actual: str | None) -> bool:
    return bool(expected and actual) and hmac.compare_digest(
        expected.encode("utf-8"), actual.encode("utf-8")
    )


def create_app(
    *,
    db_path: Path | str | None = None,
    feed_dir: Path | str | None = None,
    config_path: Path | str | None = None,
    setup_token: str | None = None,
    run_worker: bool | None = None,
    worker_interval: float | None = None,
) -> FastAPI:
    if setup_token is None:
        setup_token = os.environ.get("COMMUNITY_STACK_ADMIN_SETUP_TOKEN")
    if run_worker is None:
        run_worker = os.environ.get("COMMUNITY_STACK_EMBEDDED_WORKER", "1") != "0"
    if worker_interval is None:
        worker_interval = float(
            os.environ.get("COMMUNITY_STACK_WORKER_INTERVAL", WORKER_INTERVAL_SECONDS)
        )
    db_path = db_path or os.environ.get(
        "COMMUNITY_STACK_ANNOUNCEMENTS_DB_PATH", "data/announcements/announcements.db"
    )
    feed_dir = feed_dir or os.environ.get("COMMUNITY_STACK_FEED_DIR", "data/landing/feed")
    config_path = config_path or os.environ.get("COMMUNITY_STACK_CONFIG", "community-stack.yaml")
    storage = AnnouncementStorage(db_path=db_path)
    auth_store = AdminStore(db_path)
    publisher = AnnouncementPublisher(
        storage=storage,
        config_path=config_path,
        feed_dir=feed_dir,
    )
    configured_channel_links = next(
        (
            adapter.channel_links.copy()
            for adapter in publisher.adapters
            if isinstance(adapter, WebFeedAdapter)
        ),
        {},
    )
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    async def deliver_due_jobs() -> None:
        while True:
            try:
                await run_in_threadpool(publisher.process_due_jobs)
            except Exception:
                logger.exception("Background delivery run failed.")
            await asyncio.sleep(worker_interval)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(deliver_due_jobs()) if run_worker else None
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="Community Announcements Admin", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.storage = storage
    app.state.auth_store = auth_store
    app.state.publisher = publisher

    def session_for(request: Request) -> dict[str, Any] | None:
        return auth_store.get_session(request.cookies.get(SESSION_COOKIE))

    def csrf_valid(session: dict[str, Any] | None, value: str | None) -> bool:
        return bool(session) and tokens_match(session["csrf_token"], value)

    def set_cookie(response: Response, token: str, name: str = SESSION_COOKIE) -> None:
        response.set_cookie(
            name,
            token,
            max_age=int(SESSION_TTL.total_seconds()),
            httponly=True,
            secure=True,
            samesite="strict",
            path="/admin",
        )

    def render(
        request: Request,
        template: str,
        context: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name=template,
            context=context or {},
            status_code=status_code,
        )

    def login_redirect() -> RedirectResponse:
        return RedirectResponse("/admin/login", status_code=303)

    def authenticated(request: Request) -> dict[str, Any] | None:
        session = session_for(request)
        return session if session and session["username"] else None

    async def submitted_form(request: Request) -> dict[str, str]:
        form = await request.form()
        return {key: str(value) for key, value in form.items()}

    def client_host(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def csrf_or_forbidden(
        request: Request, session: dict[str, Any] | None, form: dict[str, str]
    ) -> None:
        if not csrf_valid(session, form.get("csrf_token")):
            raise HTTPException(status_code=403, detail="Invalid or expired CSRF token.")

    # Login and setup use a double-submit cookie so unauthenticated visitors
    # never create database rows.
    def preauth_csrf_token(request: Request) -> str:
        token = request.cookies.get(PREAUTH_CSRF_COOKIE, "")
        return token if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) else secrets.token_urlsafe(32)

    def preauth_csrf_or_forbidden(request: Request, form: dict[str, str]) -> str:
        token = request.cookies.get(PREAUTH_CSRF_COOKIE)
        if not tokens_match(token, form.get("csrf_token")):
            raise HTTPException(status_code=403, detail="Invalid or expired CSRF token.")
        return token

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/admin")
    async def dashboard(request: Request) -> Response:
        session = authenticated(request)
        if not session:
            return login_redirect()
        announcements = storage.list_announcements(limit=10)
        failures = storage.get_failed_deliveries()
        return render(
            request,
            "dashboard.html",
            {
                "username": session["username"],
                "csrf_token": session["csrf_token"],
                "announcements": announcements,
                "failed_count": len(failures),
                "queued_count": len(storage.get_active_jobs()),
            },
        )

    @app.get("/admin/setup")
    async def setup_page(request: Request) -> Response:
        if auth_store.has_admin():
            return login_redirect()
        csrf_token = preauth_csrf_token(request)
        response = render(
            request,
            "setup.html",
            {
                "csrf_token": csrf_token,
                "setup_available": bool(setup_token),
                "error": None,
            },
        )
        set_cookie(response, csrf_token, PREAUTH_CSRF_COOKIE)
        return response

    @app.post("/admin/setup")
    async def setup_admin(request: Request) -> Response:
        if auth_store.has_admin():
            return login_redirect()
        form = await submitted_form(request)
        preauth_csrf_or_forbidden(request, form)
        host = client_host(request)
        if auth_store.is_rate_limited(host, "setup"):
            raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
        if not tokens_match(setup_token, form.get("setup_token")):
            auth_store.record_failed_attempt(host, "setup")
            raise HTTPException(status_code=403, detail="Administrator setup token is invalid.")
        try:
            await run_in_threadpool(
                auth_store.create_admin, form.get("username", ""), form.get("password", "")
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        auth_store.clear_failed_attempts(host, "setup")
        auth_store.delete_session(request.cookies.get(SESSION_COOKIE))
        token, _ = auth_store.new_session(form["username"].strip())
        response = RedirectResponse("/admin", status_code=303)
        set_cookie(response, token)
        return response

    @app.get("/admin/login")
    async def login_page(request: Request) -> Response:
        csrf_token = preauth_csrf_token(request)
        response = render(
            request,
            "login.html",
            {"csrf_token": csrf_token, "error": None},
        )
        set_cookie(response, csrf_token, PREAUTH_CSRF_COOKIE)
        return response

    @app.post("/admin/login")
    async def login(request: Request) -> Response:
        form = await submitted_form(request)
        csrf_token = preauth_csrf_or_forbidden(request, form)
        username = form.get("username", "").strip()
        host = client_host(request)
        if auth_store.is_rate_limited(host, username) or auth_store.is_rate_limited(
            host, ANY_USERNAME, LOGIN_IP_LIMIT
        ):
            raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
        if not await run_in_threadpool(
            auth_store.verify_password, username, form.get("password", "")
        ):
            auth_store.record_failed_attempt(host, username)
            auth_store.record_failed_attempt(host, ANY_USERNAME)
            return render(
                request,
                "login.html",
                {"csrf_token": csrf_token, "error": "Login failed."},
                status_code=401,
            )
        auth_store.clear_failed_attempts(host, username)
        auth_store.delete_session(request.cookies.get(SESSION_COOKIE))
        token, _ = auth_store.new_session(username)
        response = RedirectResponse("/admin", status_code=303)
        set_cookie(response, token)
        return response

    @app.post("/admin/logout")
    async def logout(request: Request) -> Response:
        session = authenticated(request)
        if not session:
            return login_redirect()
        form = await submitted_form(request)
        csrf_or_forbidden(request, session, form)
        auth_store.delete_session(request.cookies.get(SESSION_COOKIE))
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie(
            SESSION_COOKIE, path="/admin", secure=True, httponly=True, samesite="strict"
        )
        return response

    @app.get("/admin/announcements")
    async def announcement_list(request: Request) -> Response:
        session = authenticated(request)
        if not session:
            return login_redirect()
        return render(
            request,
            "announcements.html",
            {
                "username": session["username"],
                "csrf_token": session["csrf_token"],
                "announcements": storage.list_announcements(limit=None),
            },
        )

    @app.get("/admin/announcements/new")
    async def new_announcement_page(request: Request) -> Response:
        session = authenticated(request)
        if not session:
            return login_redirect()
        return render(
            request,
            "editor.html",
            {
                "username": session["username"],
                "csrf_token": session["csrf_token"],
                "announcement": None,
                "error": None,
            },
        )

    @app.post("/admin/announcements/new")
    async def create_announcement(request: Request) -> Response:
        session = authenticated(request)
        if not session:
            return login_redirect()
        form = await submitted_form(request)
        csrf_or_forbidden(request, session, form)
        try:
            announcement = publisher.create_draft(
                title=form.get("title", ""),
                body=form.get("body", ""),
                author=form.get("author") or None,
                tags=[item.strip() for item in form.get("tags", "").split(",") if item.strip()],
            )
        except ModelValidationError as exc:
            return render(
                request,
                "editor.html",
                {
                    "username": session["username"],
                    "csrf_token": session["csrf_token"],
                    "announcement": form,
                    "error": str(exc),
                },
                status_code=400,
            )
        return RedirectResponse(f"/admin/announcements/{announcement.id}", status_code=303)

    @app.get("/admin/announcements/{announcement_id}")
    async def announcement_detail(request: Request, announcement_id: str) -> Response:
        session = authenticated(request)
        if not session:
            return login_redirect()
        try:
            announcement = publisher.preview(announcement_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        return render(
            request,
            "detail.html",
            {
                "username": session["username"],
                "csrf_token": session["csrf_token"],
                "announcement": announcement,
                "deliveries": storage.get_delivery_states(announcement_id),
                "jobs": storage.get_active_jobs(announcement_id),
                "delivery_attempts": storage.get_delivery_attempts(announcement_id),
            },
        )

    @app.get("/admin/announcements/{announcement_id}/edit")
    async def edit_announcement_page(request: Request, announcement_id: str) -> Response:
        session = authenticated(request)
        if not session:
            return login_redirect()
        try:
            announcement = publisher.preview(announcement_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        if announcement.status.value == "withdrawn":
            raise HTTPException(status_code=409, detail="Withdrawn announcements cannot be edited.")
        return render(
            request,
            "editor.html",
            {
                "username": session["username"],
                "csrf_token": session["csrf_token"],
                "announcement": announcement,
                "error": None,
            },
        )

    @app.post("/admin/announcements/{announcement_id}/update")
    async def update_announcement(request: Request, announcement_id: str) -> Response:
        session = authenticated(request)
        if not session:
            return login_redirect()
        form = await submitted_form(request)
        csrf_or_forbidden(request, session, form)
        try:
            publisher.update(
                announcement_id,
                title=form.get("title"),
                body=form.get("body"),
                author=form.get("author") or None,
                tags=[item.strip() for item in form.get("tags", "").split(",") if item.strip()]
                if "tags" in form
                else None,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        except (ValueError, ModelValidationError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/admin/announcements/{announcement_id}", status_code=303)

    @app.post("/admin/announcements/{announcement_id}/publish")
    async def publish_announcement(request: Request, announcement_id: str) -> Response:
        session = authenticated(request)
        if not session:
            return login_redirect()
        form = await submitted_form(request)
        csrf_or_forbidden(request, session, form)
        if form.get("confirm") != "publish":
            raise HTTPException(
                status_code=400, detail="Explicit publish confirmation is required."
            )
        try:
            publisher.publish_draft(announcement_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(f"/admin/announcements/{announcement_id}", status_code=303)

    @app.post("/admin/announcements/{announcement_id}/withdraw")
    async def withdraw_announcement(request: Request, announcement_id: str) -> Response:
        session = authenticated(request)
        if not session:
            return login_redirect()
        form = await submitted_form(request)
        csrf_or_forbidden(request, session, form)
        if form.get("confirm") != "withdraw":
            raise HTTPException(
                status_code=400, detail="Explicit withdrawal confirmation is required."
            )
        try:
            publisher.withdraw(announcement_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(f"/admin/announcements/{announcement_id}", status_code=303)

    @app.post("/admin/announcements/{announcement_id}/retry")
    async def retry_announcement(request: Request, announcement_id: str) -> Response:
        session = authenticated(request)
        if not session:
            return login_redirect()
        form = await submitted_form(request)
        csrf_or_forbidden(request, session, form)
        publisher.retry_failed(announcement_id)
        return RedirectResponse(f"/admin/announcements/{announcement_id}", status_code=303)

    @app.get("/admin/settings")
    async def settings_page(request: Request) -> Response:
        session = authenticated(request)
        if not session:
            return login_redirect()
        settings = storage.get_channel_settings()
        channels = [
            {"name": adapter.name, "enabled": settings.get(adapter.name, True)}
            for adapter in publisher.adapters
        ]
        return render(
            request,
            "settings.html",
            {
                "username": session["username"],
                "csrf_token": session["csrf_token"],
                "channels": channels,
            },
        )

    @app.post("/admin/settings")
    async def update_settings(request: Request) -> Response:
        session = authenticated(request)
        if not session:
            return login_redirect()
        form = await submitted_form(request)
        csrf_or_forbidden(request, session, form)
        raw_form = await request.form()
        enabled_names = set(raw_form.getlist("enabled_channel"))
        valid_names = {adapter.name for adapter in publisher.adapters}
        if not enabled_names.issubset(valid_names):
            raise HTTPException(status_code=400, detail="Unknown publishing destination.")
        for adapter in publisher.adapters:
            storage.set_channel_enabled(adapter.name, adapter.name in enabled_names)
            if isinstance(adapter, WebFeedAdapter):
                settings = storage.get_channel_settings()
                adapter.channel_links = configured_channel_links.copy()
                if not settings.get("matrix_room", True):
                    adapter.channel_links.pop("Matrix announcements", None)
                if settings.get("web_feed", True):
                    adapter.rebuild()
        return RedirectResponse("/admin/settings", status_code=303)

    return app
