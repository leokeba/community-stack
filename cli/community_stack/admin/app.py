"""Authenticated, server-rendered announcement administration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import secrets
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import available_timezones

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from community_stack.admin.store import (
    PERMISSIONS,
    ROLE_DESCRIPTIONS,
    ROLES,
    SESSION_TTL,
    AdminStore,
    can,
    tokens_match,
)
from community_stack.agent.client import AgentClient, AgentRefused, AgentUnavailable
from community_stack.agent.settings import SETTINGS
from community_stack.announcements.adapters.http import UnsafeURLError
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.connectors import CONNECTORS, merge_values
from community_stack.announcements.models import AnnouncementStatus, ModelValidationError
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.config import ConfigError, load_config
from community_stack.config_edit import get_value

__all__ = ["LOGIN_IP_LIMIT", "AdminStore", "create_app", "tokens_match"]

SESSION_COOKIE = "community_admin_session"
PREAUTH_CSRF_COOKIE = "community_admin_csrf"
LOGIN_IP_LIMIT = 20
ANY_USERNAME = "\0any"
TEMPLATE_DIR = Path(__file__).parent / "templates"
WORKER_INTERVAL_SECONDS = 15.0
DEFAULT_AGENT_SOCKET = "/run/community-stack-agent/agent.sock"

logger = logging.getLogger(__name__)


class LoginRequired(Exception):
    """Raised when a page needs a signed-in account."""


def create_app(
    *,
    db_path: Path | str | None = None,
    feed_dir: Path | str | None = None,
    config_path: Path | str | None = None,
    setup_token: str | None = None,
    run_worker: bool | None = None,
    worker_interval: float | None = None,
    agent_socket: Path | str | None = None,
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
    agent = AgentClient(
        agent_socket or os.environ.get("COMMUNITY_STACK_AGENT_SOCKET", DEFAULT_AGENT_SOCKET)
    )
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
    app.state.agent = agent

    @app.exception_handler(LoginRequired)
    async def login_required(_request: Request, _exc: LoginRequired) -> Response:
        return login_redirect()

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

    def page(session: dict[str, Any], **context: Any) -> dict[str, Any]:
        """Template context for a signed-in page, including what the role may do."""
        return {
            "username": session["username"],
            "role": session["role"],
            "csrf_token": session["csrf_token"],
            "perms": {name: can(session["role"], name) for name in PERMISSIONS},
            **context,
        }

    def login_redirect() -> RedirectResponse:
        return RedirectResponse("/admin/login", status_code=303)

    def authenticated(request: Request) -> dict[str, Any] | None:
        return auth_store.get_session(request.cookies.get(SESSION_COOKIE))

    def require(request: Request, permission: str) -> dict[str, Any]:
        session = authenticated(request)
        if not session:
            raise LoginRequired
        if not can(session["role"], permission):
            raise HTTPException(status_code=403, detail="Your role does not allow this.")
        return session

    async def submitted_form(request: Request) -> dict[str, str]:
        form = await request.form()
        return {key: str(value) for key, value in form.items()}

    def client_host(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def csrf_or_forbidden(
        request: Request, session: dict[str, Any] | None, form: dict[str, str]
    ) -> None:
        if not (session and tokens_match(session["csrf_token"], form.get("csrf_token"))):
            raise HTTPException(status_code=403, detail="Invalid or expired CSRF token.")

    async def checked_form(request: Request, permission: str) -> tuple[dict[str, Any], dict]:
        session = require(request, permission)
        form = await submitted_form(request)
        csrf_or_forbidden(request, session, form)
        return session, form

    def audit(
        request: Request,
        actor: str | None,
        action: str,
        target: str | None = None,
        **details: Any,
    ) -> None:
        auth_store.record_event(actor, action, target, details, client_host(request))

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

    def parse_tags(form: dict[str, str]) -> list[str]:
        return [item.strip() for item in form.get("tags", "").split(",") if item.strip()]

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/admin")
    async def dashboard(request: Request) -> Response:
        session = require(request, "view")
        steps = setup_steps() if can(session["role"], "system") else []
        return render(
            request,
            "dashboard.html",
            page(
                session,
                setup_done=sum(step["done"] for step in steps),
                setup_total=len(steps),
                announcements=storage.list_announcements(limit=10),
                failed_count=len(storage.get_failed_deliveries()),
                queued_count=len(storage.get_active_jobs()),
            ),
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
        username = form.get("username", "").strip()
        try:
            await run_in_threadpool(auth_store.create_admin, username, form.get("password", ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        auth_store.clear_failed_attempts(host, "setup")
        audit(request, username, "setup.completed", username, role="owner")
        auth_store.delete_session(request.cookies.get(SESSION_COOKIE))
        token, _ = auth_store.new_session(username)
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
            audit(request, None, "login.failed", username[:80])
            return render(
                request,
                "login.html",
                {"csrf_token": csrf_token, "error": "Login failed."},
                status_code=401,
            )
        auth_store.clear_failed_attempts(host, username)
        audit(request, username, "login.succeeded")
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
        audit(request, session["username"], "logout")
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie(
            SESSION_COOKIE, path="/admin", secure=True, httponly=True, samesite="strict"
        )
        return response

    @app.get("/admin/announcements")
    async def announcement_list(request: Request) -> Response:
        session = require(request, "view")
        return render(
            request,
            "announcements.html",
            page(session, announcements=storage.list_announcements(limit=None)),
        )

    @app.get("/admin/announcements/new")
    async def new_announcement_page(request: Request) -> Response:
        session = require(request, "edit")
        return render(request, "editor.html", page(session, announcement=None, error=None))

    @app.post("/admin/announcements/new")
    async def create_announcement(request: Request) -> Response:
        session, form = await checked_form(request, "edit")
        try:
            announcement = publisher.create_draft(
                title=form.get("title", ""),
                body=form.get("body", ""),
                author=form.get("author") or None,
                tags=parse_tags(form),
            )
        except ModelValidationError as exc:
            return render(
                request,
                "editor.html",
                page(session, announcement=form, error=str(exc)),
                status_code=400,
            )
        audit(
            request,
            session["username"],
            "announcement.created",
            announcement.id,
            title=announcement.title,
        )
        return RedirectResponse(f"/admin/announcements/{announcement.id}", status_code=303)

    @app.get("/admin/announcements/{announcement_id}")
    async def announcement_detail(request: Request, announcement_id: str) -> Response:
        session = require(request, "view")
        try:
            announcement = publisher.preview(announcement_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        scheduled_local = (
            datetime.fromisoformat(announcement.scheduled_at)
            .astimezone(publisher.timezone)
            .strftime("%A %d %B %Y, %H:%M")
            if announcement.scheduled_at
            else None
        )
        return render(
            request,
            "detail.html",
            page(
                session,
                announcement=announcement,
                scheduled_local=scheduled_local,
                timezone_name=str(publisher.timezone),
                now_local=datetime.now(publisher.timezone).strftime("%Y-%m-%dT%H:%M"),
                deliveries=storage.get_delivery_states(announcement_id),
                jobs=storage.get_active_jobs(announcement_id),
                checks=publisher.channel_checks(announcement),
                delivery_attempts=storage.get_delivery_attempts(announcement_id),
            ),
        )

    @app.get("/admin/announcements/{announcement_id}/edit")
    async def edit_announcement_page(request: Request, announcement_id: str) -> Response:
        session = require(request, "edit")
        try:
            announcement = publisher.preview(announcement_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        if announcement.status.value == "withdrawn":
            raise HTTPException(status_code=409, detail="Withdrawn announcements cannot be edited.")
        return render(request, "editor.html", page(session, announcement=announcement, error=None))

    @app.post("/admin/announcements/{announcement_id}/update")
    async def update_announcement(request: Request, announcement_id: str) -> Response:
        session, form = await checked_form(request, "edit")
        try:
            announcement, _ = publisher.update(
                announcement_id,
                title=form.get("title"),
                body=form.get("body"),
                author=form.get("author") or None,
                tags=parse_tags(form) if "tags" in form else None,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        except (ValueError, ModelValidationError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(
            request,
            session["username"],
            "announcement.updated",
            announcement_id,
            title=announcement.title,
        )
        return RedirectResponse(f"/admin/announcements/{announcement_id}", status_code=303)

    @app.post("/admin/announcements/{announcement_id}/publish")
    async def publish_announcement(request: Request, announcement_id: str) -> Response:
        session, form = await checked_form(request, "edit")
        if form.get("confirm") != "publish":
            raise HTTPException(
                status_code=400, detail="Explicit publish confirmation is required."
            )
        try:
            announcement, results = publisher.publish_draft(announcement_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit(
            request,
            session["username"],
            "announcement.published",
            announcement_id,
            title=announcement.title,
            channels=sorted(results),
        )
        return RedirectResponse(f"/admin/announcements/{announcement_id}", status_code=303)

    @app.post("/admin/announcements/{announcement_id}/schedule")
    async def schedule_announcement(request: Request, announcement_id: str) -> Response:
        session, form = await checked_form(request, "edit")
        try:
            local = datetime.strptime(form.get("scheduled_for", ""), "%Y-%m-%dT%H:%M")
        except ValueError:
            raise HTTPException(status_code=400, detail="Choose a date and time.") from None
        try:
            announcement = publisher.schedule(
                announcement_id, local.replace(tzinfo=publisher.timezone)
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit(
            request,
            session["username"],
            "announcement.scheduled",
            announcement_id,
            title=announcement.title,
            scheduled_at=announcement.scheduled_at,
        )
        return RedirectResponse(f"/admin/announcements/{announcement_id}", status_code=303)

    @app.post("/admin/announcements/{announcement_id}/unschedule")
    async def unschedule_announcement(request: Request, announcement_id: str) -> Response:
        session, _ = await checked_form(request, "edit")
        try:
            announcement = publisher.unschedule(announcement_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit(
            request,
            session["username"],
            "announcement.unscheduled",
            announcement_id,
            title=announcement.title,
        )
        return RedirectResponse(f"/admin/announcements/{announcement_id}", status_code=303)

    @app.post("/admin/announcements/{announcement_id}/withdraw")
    async def withdraw_announcement(request: Request, announcement_id: str) -> Response:
        session, form = await checked_form(request, "edit")
        if form.get("confirm") != "withdraw":
            raise HTTPException(
                status_code=400, detail="Explicit withdrawal confirmation is required."
            )
        try:
            announcement, _ = publisher.withdraw(announcement_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit(
            request,
            session["username"],
            "announcement.withdrawn",
            announcement_id,
            title=announcement.title,
        )
        return RedirectResponse(f"/admin/announcements/{announcement_id}", status_code=303)

    @app.post("/admin/announcements/{announcement_id}/retry")
    async def retry_announcement(request: Request, announcement_id: str) -> Response:
        session, _ = await checked_form(request, "edit")
        results = publisher.retry_failed(announcement_id)
        audit(
            request,
            session["username"],
            "announcement.retried",
            announcement_id,
            channels=sorted(key.split(":", 1)[1] for key in results),
        )
        return RedirectResponse(f"/admin/announcements/{announcement_id}", status_code=303)

    @app.get("/admin/settings")
    async def settings_page(request: Request) -> Response:
        session = require(request, "settings")
        settings = storage.get_channel_settings()
        channels = [
            {
                "name": adapter.name,
                "label": adapter.capabilities.label,
                "enabled": settings.get(adapter.name, True),
            }
            for adapter in publisher.adapters
        ]
        return render(request, "settings.html", page(session, channels=channels))

    @app.post("/admin/settings")
    async def update_settings(request: Request) -> Response:
        session, _ = await checked_form(request, "settings")
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
        audit(request, session["username"], "settings.updated", enabled=sorted(enabled_names))
        return RedirectResponse("/admin/settings", status_code=303)

    def channels_page(
        request: Request,
        session: dict[str, Any],
        message: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        saved = storage.list_connections()
        enabled = storage.get_channel_settings()
        connectors = [
            {
                "type": connector,
                "connected": kind in saved,
                "enabled": enabled.get(kind, True),
                "values": {
                    field.name: saved.get(kind, {}).get(field.name, "")
                    for field in connector.fields
                    if not field.secret
                },
            }
            for kind, connector in CONNECTORS.items()
        ]
        return render(
            request,
            "channels.html",
            page(session, connectors=connectors, message=message),
            status_code=status_code,
        )

    def connector_for(kind: str):
        connector = CONNECTORS.get(kind)
        if connector is None:
            raise HTTPException(status_code=404, detail="Unknown channel type.")
        return connector

    @app.get("/admin/channels")
    async def channels(request: Request) -> Response:
        return channels_page(request, require(request, "settings"))

    @app.post("/admin/channels/{kind}/save")
    async def save_channel(request: Request, kind: str) -> Response:
        session, form = await checked_form(request, "settings")
        connector = connector_for(kind)
        saved = storage.list_connections().get(kind, {})
        try:
            values = await run_in_threadpool(
                connector.validate, merge_values(connector, form, saved)
            )
        except (UnsafeURLError, ValueError) as exc:
            message = {"kind": kind, "ok": False, "text": str(exc)}
            return channels_page(request, session, message, 400)
        storage.save_connection(kind, values, session["username"])
        publisher.reload_connections()
        public = {f.name: values.get(f.name) for f in connector.fields if not f.secret}
        audit(
            request,
            session["username"],
            "channel.updated" if saved else "channel.connected",
            kind,
            **public,
        )
        message = {"kind": kind, "ok": True, "text": "Saved. Use 'Test connection' to check it."}
        return channels_page(request, session, message)

    @app.post("/admin/channels/{kind}/test")
    async def test_channel(request: Request, kind: str) -> Response:
        session, _ = await checked_form(request, "settings")
        connector = connector_for(kind)
        values = storage.list_connections().get(kind)
        if values is None:
            raise HTTPException(status_code=409, detail="This channel is not connected.")
        result = await run_in_threadpool(connector.build(values).health_check)
        audit(request, session["username"], "channel.tested", kind, ok=result.ok)
        return channels_page(
            request, session, {"kind": kind, "ok": result.ok, "text": result.details}
        )

    @app.post("/admin/channels/{kind}/disconnect")
    async def disconnect_channel(request: Request, kind: str) -> Response:
        session, _ = await checked_form(request, "settings")
        connector_for(kind)
        storage.delete_connection(kind)
        publisher.reload_connections()
        audit(request, session["username"], "channel.disconnected", kind)
        message = {
            "kind": kind,
            "ok": True,
            "text": "Disconnected. Saved credentials were deleted.",
        }
        return channels_page(request, session, message)

    def accounts_page(
        request: Request, session: dict[str, Any], error: str | None = None, status_code=200
    ) -> HTMLResponse:
        assignable = [r for r in ROLES if r != "owner" or session["role"] == "owner"]
        return render(
            request,
            "accounts.html",
            page(
                session,
                accounts=auth_store.list_accounts(),
                assignable_roles=assignable,
                role_descriptions=ROLE_DESCRIPTIONS,
                error=error,
            ),
            status_code=status_code,
        )

    def managed_account(session: dict[str, Any], form: dict[str, str]) -> dict[str, Any]:
        """The account a form targets, if the signed-in role may manage it."""
        account = auth_store.get_account(form.get("username", ""))
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found.")
        if account["username"] == session["username"]:
            raise HTTPException(
                status_code=400,
                detail="Change your own password on the My account page; "
                "ask another owner or admin to change your role or access.",
            )
        if account["role"] == "owner" and session["role"] != "owner":
            raise HTTPException(status_code=403, detail="Only owners can manage owners.")
        return account

    @app.get("/admin/accounts")
    async def accounts(request: Request) -> Response:
        return accounts_page(request, require(request, "accounts"))

    @app.post("/admin/accounts/create")
    async def create_account(request: Request) -> Response:
        session, form = await checked_form(request, "accounts")
        role = form.get("role", "")
        if role == "owner" and session["role"] != "owner":
            raise HTTPException(status_code=403, detail="Only owners can create owners.")
        username = form.get("username", "").strip()
        try:
            await run_in_threadpool(
                auth_store.create_account, username, form.get("password", ""), role
            )
        except ValueError as exc:
            return accounts_page(request, session, str(exc), 400)
        audit(request, session["username"], "account.created", username, role=role)
        return RedirectResponse("/admin/accounts", status_code=303)

    @app.post("/admin/accounts/role")
    async def change_role(request: Request) -> Response:
        session, form = await checked_form(request, "accounts")
        account = managed_account(session, form)
        role = form.get("role", "")
        if role == "owner" and session["role"] != "owner":
            raise HTTPException(status_code=403, detail="Only owners can grant the owner role.")
        try:
            auth_store.set_role(account["username"], role)
        except ValueError as exc:
            return accounts_page(request, session, str(exc), 400)
        audit(
            request,
            session["username"],
            "account.role_changed",
            account["username"],
            previous=account["role"],
            role=role,
        )
        return RedirectResponse("/admin/accounts", status_code=303)

    @app.post("/admin/accounts/access")
    async def change_access(request: Request) -> Response:
        session, form = await checked_form(request, "accounts")
        account = managed_account(session, form)
        disable = form.get("action") == "disable"
        try:
            auth_store.set_disabled(account["username"], disable)
        except ValueError as exc:
            return accounts_page(request, session, str(exc), 400)
        action = "account.disabled" if disable else "account.enabled"
        audit(request, session["username"], action, account["username"])
        return RedirectResponse("/admin/accounts", status_code=303)

    @app.post("/admin/accounts/password")
    async def reset_password(request: Request) -> Response:
        session, form = await checked_form(request, "accounts")
        account = managed_account(session, form)
        try:
            await run_in_threadpool(
                auth_store.set_password, account["username"], form.get("password", "")
            )
        except ValueError as exc:
            return accounts_page(request, session, str(exc), 400)
        audit(request, session["username"], "account.password_reset", account["username"])
        return RedirectResponse("/admin/accounts", status_code=303)

    @app.get("/admin/account")
    async def my_account(request: Request) -> Response:
        session = require(request, "view")
        return render(
            request,
            "account.html",
            page(
                session,
                role_description=ROLE_DESCRIPTIONS[session["role"]],
                error=None,
                saved=request.query_params.get("saved") == "1",
            ),
        )

    @app.post("/admin/account")
    async def change_my_password(request: Request) -> Response:
        session, form = await checked_form(request, "view")
        if not await run_in_threadpool(
            auth_store.verify_password, session["username"], form.get("current_password", "")
        ):
            error = "Your current password is not correct."
        else:
            try:
                await run_in_threadpool(
                    auth_store.set_password,
                    session["username"],
                    form.get("new_password", ""),
                    request.cookies.get(SESSION_COOKIE),
                )
            except ValueError as exc:
                error = str(exc)
            else:
                audit(request, session["username"], "password.changed", session["username"])
                return RedirectResponse("/admin/account?saved=1", status_code=303)
        return render(
            request,
            "account.html",
            page(
                session,
                role_description=ROLE_DESCRIPTIONS[session["role"]],
                error=error,
                saved=False,
            ),
            status_code=400,
        )

    async def system_page(
        request: Request,
        session: dict[str, Any],
        outcome: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        status, agent_error = None, None
        if agent.available():
            try:
                status = await run_in_threadpool(agent.call, "status.get", session["username"])
            except (AgentUnavailable, AgentRefused) as exc:
                agent_error = str(exc)
        return render(
            request,
            "system.html",
            page(
                session,
                agent_available=agent.available(),
                agent_error=agent_error,
                status=status,
                outcome=outcome,
            ),
            status_code=status_code,
        )

    async def run_system_operation(
        request: Request,
        session: dict[str, Any],
        op: str,
        title: str,
        password: str | None = None,
    ) -> HTMLResponse:
        try:
            result = await run_in_threadpool(agent.call, op, session["username"], None, password)
        except (AgentUnavailable, AgentRefused) as exc:
            audit(request, session["username"], f"system.{op}", outcome="refused")
            return await system_page(request, session, {"title": title, "error": str(exc)}, 400)
        audit(request, session["username"], f"system.{op}", outcome="succeeded")
        return await system_page(request, session, {"title": title, "result": result})

    @app.get("/admin/system")
    async def system(request: Request) -> Response:
        return await system_page(request, require(request, "system"))

    @app.post("/admin/system/backup")
    async def system_backup(request: Request) -> Response:
        session, _ = await checked_form(request, "system")
        return await run_system_operation(request, session, "backup.create", "Backup")

    @app.post("/admin/system/verify")
    async def system_verify(request: Request) -> Response:
        session, _ = await checked_form(request, "system")
        return await run_system_operation(request, session, "backup.verify", "Backup check")

    @app.post("/admin/system/drill")
    async def system_drill(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        return await run_system_operation(
            request, session, "restore.drill", "Restore drill", form.get("password", "")
        )

    async def services_page(
        request: Request,
        session: dict[str, Any],
        error: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        values, status = None, None
        if agent.available():
            try:
                config = await run_in_threadpool(agent.call, "config.get", session["username"])
                values = config["values"]
                status = await run_in_threadpool(agent.call, "status.get", session["username"])
            except (AgentUnavailable, AgentRefused) as exc:
                error = error or str(exc)
        return render(
            request,
            "services.html",
            page(
                session,
                agent_available=agent.available(),
                settings=SETTINGS.values(),
                values=values,
                status=status,
                timezones=sorted(available_timezones()),
                error=error,
            ),
            status_code=status_code,
        )

    def submitted_changes(form: dict[str, str]) -> dict[str, str]:
        return {key: form[f"setting:{key}"] for key in SETTINGS if f"setting:{key}" in form}

    @app.get("/admin/services")
    async def services(request: Request) -> Response:
        return await services_page(request, require(request, "system"))

    @app.post("/admin/services/review")
    async def review_services(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        changes = submitted_changes(form)
        try:
            plan = await run_in_threadpool(
                agent.call, "config.plan", session["username"], {"changes": changes}
            )
        except (AgentUnavailable, AgentRefused) as exc:
            return await services_page(request, session, str(exc), 400)
        if not plan["changes"]:
            return await services_page(request, session, "Nothing was changed.", 400)
        applied = {change["key"]: form[f"setting:{change['key']}"] for change in plan["changes"]}
        return render(
            request, "services_review.html", page(session, plan=plan, changes=applied, result=None)
        )

    @app.post("/admin/services/apply")
    async def apply_services(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        changes = submitted_changes(form)
        try:
            result = await run_in_threadpool(
                agent.call,
                "config.apply",
                session["username"],
                {"changes": changes},
                form.get("password") or None,
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(
                request,
                session["username"],
                "system.config.apply",
                outcome="failed",
                settings=sorted(changes),
            )
            return render(
                request,
                "services_review.html",
                page(session, plan=None, changes=changes, result={"error": str(exc)}),
                status_code=400,
            )
        audit(
            request,
            session["username"],
            "system.config.apply",
            outcome="succeeded",
            settings=sorted(changes),
        )
        return render(
            request, "services_review.html", page(session, plan=None, changes={}, result=result)
        )

    def setup_steps() -> list[dict[str, Any]]:
        try:
            config = load_config(config_path) if Path(config_path).is_file() else {}
        except ConfigError:
            config = {}
        managers = [
            a
            for a in auth_store.list_accounts()
            if a["role"] in ("owner", "admin") and not a["disabled_at"]
        ]
        published = storage.list_announcements(
            status=AnnouncementStatus.PUBLISHED, limit=1
        ) or storage.list_announcements(status=AnnouncementStatus.UPDATED, limit=1)
        return [
            {"label": "Create the first owner account", "done": True, "link": None},
            {
                "label": "Add a second owner or admin, so the community is never locked out",
                "done": len(managers) >= 2,
                "link": "/admin/accounts",
            },
            {
                "label": "Install the system agent on the server",
                "done": agent.available(),
                "link": "/admin/system",
            },
            {
                "label": "Set the community time zone",
                "done": "timezone" in config.get("project", {}),
                "link": "/admin/services",
            },
            {
                "label": "Turn on encrypted backups",
                "done": get_value(config, ("operations", "backups", "encryption")) == "restic",
                "link": "/admin/services",
            },
            {
                "label": "Run a restore drill",
                "done": auth_store.has_event("system.restore.drill", outcome="succeeded"),
                "link": "/admin/system",
            },
            {
                "label": "Connect a publishing channel",
                "done": bool(storage.list_connections()),
                "link": "/admin/channels",
            },
            {
                "label": "Publish your first announcement",
                "done": bool(published),
                "link": "/admin/announcements/new",
            },
        ]

    @app.get("/admin/start")
    async def start(request: Request) -> Response:
        session = require(request, "system")
        return render(request, "start.html", page(session, steps=setup_steps()))

    @app.get("/admin/audit")
    async def audit_log(request: Request) -> Response:
        session = require(request, "audit")
        return render(request, "audit.html", page(session, events=auth_store.list_events()))

    return app
