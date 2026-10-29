"""Authenticated, server-rendered announcement administration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import secrets
import tempfile
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import available_timezones

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.concurrency import run_in_threadpool

from community_stack.admin import totp
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
from community_stack.agent.health import LOG_LINES, LOG_SERVICES
from community_stack.agent.settings import SETTINGS
from community_stack.announcements.adapters import http
from community_stack.announcements.adapters.http import UnsafeURLError
from community_stack.announcements.adapters.signal import SIGNAL_API, signal_send
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.connectors import CONNECTORS, merge_values
from community_stack.announcements.fediverse import (
    ACTIVITY_JSON,
    CONTEXT,
    FediverseAdapter,
)
from community_stack.announcements.formatting import render_html
from community_stack.announcements.matrix_mirror import MODERATOR_POWER, mirror_for
from community_stack.announcements.media import (
    MAX_UPLOAD_BYTES,
    MEDIA_ID,
    MediaError,
    process_image,
    validate_alt,
)
from community_stack.announcements.models import (
    UNPUBLISHED,
    Announcement,
    AnnouncementStatus,
    ModelValidationError,
)
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.sms import SmsGatewayAdapter, SmsSignup, normalize_phone
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.config import ConfigError, load_config
from community_stack.config_edit import get_value
from community_stack.continuity.offline_exporter import OfflineArchiveExporter

__all__ = ["LOGIN_IP_LIMIT", "AdminStore", "create_app", "tokens_match"]

SESSION_COOKIE = "community_admin_session"
PREAUTH_CSRF_COOKIE = "community_admin_csrf"
SECOND_STEP_COOKIE = "community_admin_second_step"
LOGIN_IP_LIMIT = 20
ANY_USERNAME = "\0any"
TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
WORKER_INTERVAL_SECONDS = 15.0
MAX_INBOX_BYTES = 256 * 1024
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

    def mirror():
        return mirror_for(
            storage,
            publisher,
            lambda: storage.get_setting("review_required", "false") == "true",
        )

    async def deliver_due_jobs() -> None:
        while True:
            try:
                await run_in_threadpool(publisher.process_due_jobs)
            except Exception:
                logger.exception("Background delivery run failed.")
            try:
                rooms = mirror()
                if rooms is not None:
                    await run_in_threadpool(rooms.sync_once)
            except Exception:
                logger.exception("Matrix room mirroring failed.")
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
    app.mount("/admin/static", StaticFiles(directory=STATIC_DIR), name="admin-static")
    app.state.storage = storage
    app.state.auth_store = auth_store
    app.state.publisher = publisher
    app.state.agent = agent

    @app.exception_handler(LoginRequired)
    async def login_required(_request: Request, _exc: LoginRequired) -> Response:
        return login_redirect()

    def set_cookie(
        response: Response, token: str, name: str = SESSION_COOKIE, path: str = "/admin"
    ) -> None:
        response.set_cookie(
            name,
            token,
            max_age=int(SESSION_TTL.total_seconds()),
            httponly=True,
            secure=True,
            samesite="strict",
            path=path,
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

    def review_required() -> bool:
        return storage.get_setting("review_required", "false") == "true"

    def announcement_or_404(announcement_id: str) -> Announcement:
        try:
            return publisher.preview(announcement_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None

    def require_approved(announcement: Announcement) -> None:
        if review_required() and announcement.status not in (
            AnnouncementStatus.APPROVED,
            AnnouncementStatus.SCHEDULED,
        ):
            raise HTTPException(
                status_code=409, detail="Review is on: this announcement must be approved first."
            )

    def parse_tags(form: dict[str, str]) -> list[str]:
        return [item.strip() for item in form.get("tags", "").split(",") if item.strip()]

    def custom_text_channels():
        enabled = storage.get_channel_settings()
        return [
            adapter
            for adapter in publisher.adapters
            if adapter.capabilities.custom_text and enabled.get(adapter.name, True)
        ]

    def parse_channel_text(form: dict[str, str]) -> dict[str, str]:
        return {
            adapter.name: form[f"channel_text:{adapter.name}"].strip()
            for adapter in custom_text_channels()
            if form.get(f"channel_text:{adapter.name}", "").strip()
        }

    def editor_page(session: dict[str, Any], announcement: Any, error: str | None) -> dict:
        saved = getattr(announcement, "channel_text", None) or {}
        submitted = announcement if isinstance(announcement, dict) else {}
        channels = [
            {
                "name": adapter.name,
                "label": adapter.capabilities.label,
                "limit": adapter.capabilities.max_text_length,
                "value": submitted.get(f"channel_text:{adapter.name}", saved.get(adapter.name, "")),
            }
            for adapter in custom_text_channels()
        ]
        return page(session, announcement=announcement, error=error, custom_channels=channels)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # The community's own fediverse account: public, unauthenticated endpoints.

    def fediverse():
        adapter = next((a for a in publisher.adapters if isinstance(a, FediverseAdapter)), None)
        if adapter is None:
            raise HTTPException(status_code=404)
        return adapter.fediverse

    def activity_json(data: dict, status_code: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status_code, media_type=ACTIVITY_JSON)

    def community_name() -> str:
        try:
            return load_config(config_path)["project"]["name"]
        except (ConfigError, KeyError, OSError):
            return "Community"

    # Public SMS sign-up: every join and leave is confirmed with a texted code.

    def sms_adapter():
        return next((a for a in publisher.adapters if isinstance(a, SmsGatewayAdapter)), None)

    def sms_signup() -> SmsSignup:
        secret = storage.get_setting("sms_code_secret", "")
        if not secret:
            secret = secrets.token_hex(32)
            storage.set_setting("sms_code_secret", secret)
        return SmsSignup(storage.db_path.parent / "subscribers.db", secret)

    def subscribe_page(request: Request, step: str, status_code: int = 200, **context) -> Response:
        csrf_token = preauth_csrf_token(request)
        response = render(
            request,
            "subscribe.html",
            {
                "csrf_token": csrf_token,
                "step": step,
                "available": sms_adapter() is not None,
                "community": community_name(),
                "error": None,
                "number": "",
                **context,
            },
            status_code=status_code,
        )
        set_cookie(response, csrf_token, PREAUTH_CSRF_COOKIE, path="/subscribe")
        return response

    @app.get("/subscribe")
    async def subscribe_form(request: Request) -> Response:
        return subscribe_page(request, "join")

    @app.get("/subscribe/stop")
    async def unsubscribe_form(request: Request) -> Response:
        return subscribe_page(request, "leave")

    @app.post("/subscribe/{purpose}/code")
    async def subscribe_send_code(request: Request, purpose: str) -> Response:
        if purpose not in ("join", "leave"):
            raise HTTPException(status_code=404)
        form = await submitted_form(request)
        preauth_csrf_or_forbidden(request, form)
        adapter = sms_adapter()
        if adapter is None:
            raise HTTPException(status_code=404)
        # Bots fill in every field, including this invisible one.
        if form.get("website"):
            return subscribe_page(request, "code", purpose=purpose, number="")
        if purpose == "join" and form.get("consent") != "yes":
            return subscribe_page(request, purpose, 400, error="Please tick the consent box.")
        try:
            number = normalize_phone(form.get("number", ""))
            code = sms_signup().new_code(number, purpose, client_host(request))
        except ValueError as exc:
            return subscribe_page(request, purpose, 400, error=str(exc))
        text = f"{code} is your code to {'join' if purpose == 'join' else 'leave'} {community_name()} alerts."
        await run_in_threadpool(
            adapter.send, [number], text, f"code:{number}:{secrets.token_hex(4)}"
        )
        return subscribe_page(request, "code", purpose=purpose, number=number)

    @app.post("/subscribe/{purpose}/confirm")
    async def subscribe_confirm(request: Request, purpose: str) -> Response:
        if purpose not in ("join", "leave"):
            raise HTTPException(status_code=404)
        form = await submitted_form(request)
        preauth_csrf_or_forbidden(request, form)
        adapter = sms_adapter()
        if adapter is None:
            raise HTTPException(status_code=404)
        try:
            number = normalize_phone(form.get("number", ""))
        except ValueError as exc:
            return subscribe_page(request, purpose, 400, error=str(exc))
        if not sms_signup().confirm(number, purpose, form.get("code", "")):
            return subscribe_page(
                request,
                "code",
                400,
                purpose=purpose,
                number=number,
                error="That code did not work. Check it, or ask for a new one later.",
            )
        if purpose == "join":
            adapter.subscribers.add_subscriber("sms", number)
        else:
            adapter.subscribers.unsubscribe("sms", number)
        return subscribe_page(request, "joined" if purpose == "join" else "left")

    @app.get("/.well-known/webfinger")
    async def webfinger(request: Request) -> Response:
        document = fediverse().webfinger(request.query_params.get("resource", ""))
        if document is None:
            raise HTTPException(status_code=404)
        return JSONResponse(document, media_type="application/jrd+json")

    @app.get("/ap/actor")
    async def ap_actor() -> Response:
        name = community_name()
        return activity_json(fediverse().actor(name, f"Public announcements from {name}."))

    @app.get("/ap/followers")
    async def ap_followers() -> Response:
        account = fediverse()
        return activity_json(
            {
                "@context": "https://www.w3.org/ns/activitystreams",
                "id": f"{account.base_url}/ap/followers",
                "type": "OrderedCollection",
                "totalItems": account.follower_count(),
            }
        )

    def public_announcements() -> list[Announcement]:
        return [
            a
            for a in storage.list_announcements(limit=20)
            if a.status in (AnnouncementStatus.PUBLISHED, AnnouncementStatus.UPDATED)
        ]

    @app.get("/ap/outbox")
    async def ap_outbox() -> Response:
        account = fediverse()
        items = [account.activity("Create", a) for a in public_announcements()]
        return activity_json(
            {
                "@context": "https://www.w3.org/ns/activitystreams",
                "id": f"{account.base_url}/ap/outbox",
                "type": "OrderedCollection",
                "totalItems": len(items),
                "orderedItems": items,
            }
        )

    @app.get("/ap/notes/{announcement_id}")
    async def ap_note(announcement_id: str) -> Response:
        account = fediverse()
        announcement = storage.get_announcement(announcement_id)
        if announcement and announcement.status == AnnouncementStatus.WITHDRAWN:
            return activity_json(
                {"id": account.note_id(announcement_id), "type": "Tombstone"}, status_code=410
            )
        if not announcement or announcement.status not in (
            AnnouncementStatus.PUBLISHED,
            AnnouncementStatus.UPDATED,
        ):
            raise HTTPException(status_code=404)
        return activity_json({"@context": CONTEXT, **account.note(announcement)})

    @app.post("/ap/inbox")
    async def ap_inbox(request: Request) -> Response:
        account = fediverse()
        if int(request.headers.get("content-length") or 0) > MAX_INBOX_BYTES:
            raise HTTPException(status_code=413)
        body = await request.body()
        if len(body) > MAX_INBOX_BYTES:
            raise HTTPException(status_code=413)
        status, message = await run_in_threadpool(
            account.handle_inbox, request.url.path, dict(request.headers), body, http.send
        )
        return JSONResponse({"result": message}, status_code=status)

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
                awaiting_review=[
                    a
                    for a in storage.list_announcements(
                        status=AnnouncementStatus.IN_REVIEW, limit=50
                    )
                    if storage.last_submitter(a.id) != session["username"]
                ]
                if can(session["role"], "approve")
                else [],
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
        if auth_store.totp_enabled(username):
            response = RedirectResponse("/admin/login/verify", status_code=303)
            set_cookie(response, auth_store.begin_pending_login(username), SECOND_STEP_COOKIE)
            return response
        audit(request, username, "login.succeeded")
        return signed_in_response(request, username)

    def signed_in_response(request: Request, username: str) -> RedirectResponse:
        auth_store.delete_session(request.cookies.get(SESSION_COOKIE))
        token, _ = auth_store.new_session(username)
        response = RedirectResponse("/admin", status_code=303)
        set_cookie(response, token)
        response.delete_cookie(
            SECOND_STEP_COOKIE, path="/admin", secure=True, httponly=True, samesite="strict"
        )
        return response

    @app.get("/admin/login/verify")
    async def second_step_page(request: Request) -> Response:
        if not auth_store.pending_login(request.cookies.get(SECOND_STEP_COOKIE), count=False):
            return login_redirect()
        csrf_token = preauth_csrf_token(request)
        response = render(request, "login_verify.html", {"csrf_token": csrf_token, "error": None})
        set_cookie(response, csrf_token, PREAUTH_CSRF_COOKIE)
        return response

    @app.post("/admin/login/verify")
    async def second_step(request: Request) -> Response:
        form = await submitted_form(request)
        csrf_token = preauth_csrf_or_forbidden(request, form)
        pending = request.cookies.get(SECOND_STEP_COOKIE)
        username = auth_store.pending_login(pending)
        if not username:
            return login_redirect()
        used = auth_store.verify_second_factor(username, form.get("code", ""))
        if not used:
            auth_store.record_failed_attempt(client_host(request), ANY_USERNAME)
            audit(request, None, "login.second_step_failed", username)
            return render(
                request,
                "login_verify.html",
                {"csrf_token": csrf_token, "error": "That code did not work. Try the newest one."},
                status_code=401,
            )
        auth_store.end_pending_login(pending)
        audit(request, username, "login.succeeded", second_step=used)
        return signed_in_response(request, username)

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

    TEMPLATE_ID = re.compile(r"^tpl_[A-Za-z0-9_-]{8,40}$")

    def template_or_404(template_id: str) -> dict:
        template = storage.get_template(template_id) if TEMPLATE_ID.fullmatch(template_id) else None
        if template is None:
            raise HTTPException(status_code=404, detail="Template not found.")
        return template

    def template_from_form(form: dict[str, str], template_id: str | None = None) -> dict:
        name = " ".join(form.get("name", "").split())
        if not name or len(name) > 80:
            raise ModelValidationError("Give the template a name of up to 80 characters.")
        template = {
            "id": template_id,
            "name": name,
            "title": form.get("title", "").strip(),
            "body": form.get("body", "").strip(),
            "author": form.get("author") or None,
            "tags": parse_tags(form),
            "channel_text": parse_channel_text(form),
        }
        # Templates must be usable as announcements, so they follow the same rules.
        Announcement(
            title=template["title"], body=template["body"], channel_text=template["channel_text"]
        )
        return template

    def as_editor_values(template: dict) -> dict:
        values = {
            "name": template.get("name", ""),
            "title": template["title"],
            "body": template["body"],
            "author": template.get("author") or "",
            "tags": ", ".join(template.get("tags") or []),
        }
        values.update(
            {f"channel_text:{k}": v for k, v in (template.get("channel_text") or {}).items()}
        )
        return values

    @app.get("/admin/announcements/new")
    async def new_announcement_page(request: Request) -> Response:
        session = require(request, "edit")
        template_id = request.query_params.get("template")
        prefill = as_editor_values(template_or_404(template_id)) if template_id else None
        context = editor_page(session, prefill, None)
        context["templates"] = storage.list_templates()
        return render(request, "editor.html", context)

    @app.get("/admin/templates")
    async def templates_page(request: Request) -> Response:
        session = require(request, "edit")
        return render(
            request,
            "templates.html",
            page(session, templates=storage.list_templates(), error=None),
        )

    @app.get("/admin/templates/new")
    async def new_template_page(request: Request) -> Response:
        session = require(request, "edit")
        context = editor_page(session, None, None)
        context.update(template_mode=True, form_action="/admin/templates/new")
        return render(request, "editor.html", context)

    @app.post("/admin/templates/new")
    async def create_template(request: Request) -> Response:
        session, form = await checked_form(request, "edit")
        try:
            template_id = storage.save_template(template_from_form(form), session["username"])
        except ModelValidationError as exc:
            context = editor_page(session, form, str(exc))
            context.update(template_mode=True, form_action="/admin/templates/new")
            return render(request, "editor.html", context, status_code=400)
        audit(request, session["username"], "template.created", template_id, name=form.get("name"))
        return RedirectResponse("/admin/templates", status_code=303)

    @app.get("/admin/templates/{template_id}/edit")
    async def edit_template_page(request: Request, template_id: str) -> Response:
        session = require(request, "edit")
        context = editor_page(session, as_editor_values(template_or_404(template_id)), None)
        context.update(template_mode=True, form_action=f"/admin/templates/{template_id}/update")
        return render(request, "editor.html", context)

    @app.post("/admin/templates/{template_id}/update")
    async def update_template(request: Request, template_id: str) -> Response:
        session, form = await checked_form(request, "edit")
        template_or_404(template_id)
        try:
            storage.save_template(template_from_form(form, template_id), session["username"])
        except ModelValidationError as exc:
            context = editor_page(session, form, str(exc))
            context.update(template_mode=True, form_action=f"/admin/templates/{template_id}/update")
            return render(request, "editor.html", context, status_code=400)
        audit(request, session["username"], "template.updated", template_id, name=form.get("name"))
        return RedirectResponse("/admin/templates", status_code=303)

    @app.post("/admin/templates/{template_id}/delete")
    async def delete_template(request: Request, template_id: str) -> Response:
        session, _ = await checked_form(request, "edit")
        template = template_or_404(template_id)
        storage.delete_template(template_id)
        audit(request, session["username"], "template.deleted", template_id, name=template["name"])
        return RedirectResponse("/admin/templates", status_code=303)

    @app.post("/admin/announcements/{announcement_id}/save-as-template")
    async def save_as_template(request: Request, announcement_id: str) -> Response:
        session, form = await checked_form(request, "edit")
        announcement = announcement_or_404(announcement_id)
        name = " ".join(form.get("name", "").split())[:80] or announcement.title[:80]
        template_id = storage.save_template(
            {
                "name": name,
                "title": announcement.title,
                "body": announcement.body,
                "author": announcement.author,
                "tags": announcement.tags,
                "channel_text": announcement.channel_text,
            },
            session["username"],
        )
        audit(request, session["username"], "template.created", template_id, name=name)
        return RedirectResponse("/admin/templates", status_code=303)

    @app.post("/admin/announcements/new")
    async def create_announcement(request: Request) -> Response:
        session, form = await checked_form(request, "edit")
        try:
            announcement = publisher.create_draft(
                title=form.get("title", ""),
                body=form.get("body", ""),
                author=form.get("author") or None,
                tags=parse_tags(form),
                channel_text=parse_channel_text(form),
            )
        except ModelValidationError as exc:
            return render(
                request, "editor.html", editor_page(session, form, str(exc)), status_code=400
            )
        audit(
            request,
            session["username"],
            "announcement.created",
            announcement.id,
            title=announcement.title,
        )
        return RedirectResponse(f"/admin/announcements/{announcement.id}", status_code=303)

    def editable_announcement(announcement_id: str):
        try:
            announcement = publisher.preview(announcement_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        editable = announcement.status == AnnouncementStatus.DRAFT or (
            announcement.status == AnnouncementStatus.SCHEDULED and not review_required()
        )
        if not editable:
            raise HTTPException(
                status_code=409,
                detail="Images can only be changed on drafts (edit the announcement to reopen it).",
            )
        return announcement

    @app.post("/admin/announcements/{announcement_id}/image")
    async def upload_image(request: Request, announcement_id: str) -> Response:
        session = require(request, "edit")
        if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES + 65536:
            raise HTTPException(status_code=413, detail="Images must be 10 MB or smaller.")
        form = await request.form()
        if not tokens_match(session["csrf_token"], str(form.get("csrf_token", ""))):
            raise HTTPException(status_code=403, detail="Invalid or expired CSRF token.")
        editable_announcement(announcement_id)
        upload = form.get("image")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="Choose an image to upload.")
        try:
            alt = validate_alt(str(form.get("alt", "")))
            data = await upload.read(MAX_UPLOAD_BYTES + 1)
            processed = await run_in_threadpool(process_image, data)
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        image = storage.save_image(announcement_id, processed, alt, session["username"])
        audit(
            request,
            session["username"],
            "announcement.image_added",
            announcement_id,
            width=image.width,
            height=image.height,
        )
        return RedirectResponse(f"/admin/announcements/{announcement_id}", status_code=303)

    @app.post("/admin/announcements/{announcement_id}/image/alt")
    async def change_image_alt(request: Request, announcement_id: str) -> Response:
        session, form = await checked_form(request, "edit")
        announcement = editable_announcement(announcement_id)
        if not announcement.image:
            raise HTTPException(status_code=404, detail="This announcement has no image.")
        try:
            storage.set_image_alt(announcement.image.id, validate_alt(form.get("alt", "")))
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(request, session["username"], "announcement.image_described", announcement_id)
        return RedirectResponse(f"/admin/announcements/{announcement_id}", status_code=303)

    @app.post("/admin/announcements/{announcement_id}/image/remove")
    async def remove_image(request: Request, announcement_id: str) -> Response:
        session, _ = await checked_form(request, "edit")
        editable_announcement(announcement_id)
        storage.remove_image(announcement_id)
        audit(request, session["username"], "announcement.image_removed", announcement_id)
        return RedirectResponse(f"/admin/announcements/{announcement_id}", status_code=303)

    @app.get("/admin/media/{media_id}")
    async def media(request: Request, media_id: str) -> Response:
        require(request, "view")
        image = storage.get_media(media_id) if MEDIA_ID.fullmatch(media_id) else None
        if image is None or not image.path.is_file():
            raise HTTPException(status_code=404, detail="Image not found.")
        return FileResponse(
            image.path,
            media_type=image.content_type,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.post("/admin/preview")
    async def preview(request: Request) -> Response:
        _, form = await checked_form(request, "edit")
        try:
            draft = Announcement(
                title=form.get("title", "").strip() or "(no title yet)",
                body=form.get("body", "").strip() or "(no message yet)",
                author=form.get("author") or None,
                channel_text=parse_channel_text(form),
            )
        except ModelValidationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(
            {
                "html": render_html(draft.body),
                "channels": [
                    {
                        "name": check.name,
                        "label": check.capabilities.label,
                        "length": check.text_length,
                        "limit": check.capabilities.max_text_length,
                        "over_limit": check.over_limit,
                        "shortened": check.over_limit and check.capabilities.shortens_long_text,
                        "preview": check.preview,
                    }
                    for check in publisher.channel_checks(draft)
                ],
            }
        )

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
                review_required=review_required(),
                submitter=storage.last_submitter(announcement_id),
                review_history=storage.review_history(announcement_id),
                body_html=Markup(render_html(announcement.body)),
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
        return render(request, "editor.html", editor_page(session, announcement, None))

    @app.post("/admin/announcements/{announcement_id}/update")
    async def update_announcement(request: Request, announcement_id: str) -> Response:
        session, form = await checked_form(request, "edit")
        before = announcement_or_404(announcement_id).status
        try:
            announcement, _ = publisher.update(
                announcement_id,
                title=form.get("title"),
                body=form.get("body"),
                author=form.get("author") or None,
                tags=parse_tags(form) if "tags" in form else None,
                channel_text=parse_channel_text(form),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        except (ValueError, ModelValidationError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if review_required() and before in (
            AnnouncementStatus.IN_REVIEW,
            AnnouncementStatus.APPROVED,
            AnnouncementStatus.SCHEDULED,
        ):
            # What gets published must be exactly what was approved.
            publisher.set_review_status(announcement_id, (before,), AnnouncementStatus.DRAFT)
            storage.add_review_event(
                announcement_id, "returned_to_draft", session["username"], "Edited after review."
            )
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
        require_approved(announcement_or_404(announcement_id))
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

    @app.post("/admin/announcements/{announcement_id}/submit")
    async def submit_for_review(request: Request, announcement_id: str) -> Response:
        session, _ = await checked_form(request, "edit")
        try:
            announcement = publisher.set_review_status(
                announcement_id, (AnnouncementStatus.DRAFT,), AnnouncementStatus.IN_REVIEW
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        storage.add_review_event(announcement_id, "submitted", session["username"])
        audit(
            request,
            session["username"],
            "announcement.submitted",
            announcement_id,
            title=announcement.title,
        )
        return RedirectResponse(f"/admin/announcements/{announcement_id}", status_code=303)

    async def review_decision(request: Request, announcement_id: str, approve: bool) -> Response:
        session, form = await checked_form(request, "approve")
        announcement_or_404(announcement_id)
        if storage.last_submitter(announcement_id) == session["username"]:
            raise HTTPException(
                status_code=403, detail="Someone other than the submitter must review it."
            )
        note = " ".join(form.get("note", "").split())[:1000] or None
        if not approve and not note:
            raise HTTPException(status_code=400, detail="Say what needs to change.")
        target = AnnouncementStatus.APPROVED if approve else AnnouncementStatus.DRAFT
        try:
            announcement = publisher.set_review_status(
                announcement_id, (AnnouncementStatus.IN_REVIEW,), target
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        action = "approved" if approve else "changes_requested"
        storage.add_review_event(announcement_id, action, session["username"], note)
        audit(
            request,
            session["username"],
            f"announcement.{action}",
            announcement_id,
            title=announcement.title,
        )
        return RedirectResponse(f"/admin/announcements/{announcement_id}", status_code=303)

    @app.post("/admin/announcements/{announcement_id}/approve")
    async def approve_announcement(request: Request, announcement_id: str) -> Response:
        return await review_decision(request, announcement_id, approve=True)

    @app.post("/admin/announcements/{announcement_id}/request-changes")
    async def request_changes(request: Request, announcement_id: str) -> Response:
        return await review_decision(request, announcement_id, approve=False)

    @app.post("/admin/announcements/{announcement_id}/publish-urgently")
    async def publish_urgently(request: Request, announcement_id: str) -> Response:
        """Safety notices that cannot wait for a reviewer: owners and admins, with a reason."""
        session, form = await checked_form(request, "settings")
        announcement = announcement_or_404(announcement_id)
        if announcement.status not in UNPUBLISHED:
            raise HTTPException(status_code=409, detail="This announcement is already public.")
        reason = " ".join(form.get("reason", "").split())[:500]
        if len(reason) < 10:
            raise HTTPException(status_code=400, detail="Explain why this cannot wait for review.")
        if not await run_in_threadpool(
            auth_store.verify_password, session["username"], form.get("password", "")
        ):
            raise HTTPException(status_code=403, detail="Password confirmation failed.")
        try:
            announcement, results = publisher.publish_draft(announcement_id)
        except ValueError:
            # Submitted or in review: claim it from whatever unpublished state it is in.
            publisher.set_review_status(
                announcement_id,
                (AnnouncementStatus.IN_REVIEW,),
                AnnouncementStatus.APPROVED,
            )
            announcement, results = publisher.publish_draft(announcement_id)
        storage.add_review_event(
            announcement_id, "published_without_review", session["username"], reason
        )
        audit(
            request,
            session["username"],
            "announcement.published_urgently",
            announcement_id,
            title=announcement.title,
            reason=reason,
            channels=sorted(results),
        )
        return RedirectResponse(f"/admin/announcements/{announcement_id}", status_code=303)

    @app.post("/admin/settings/review")
    async def update_review_setting(request: Request) -> Response:
        session, form = await checked_form(request, "settings")
        enabled = form.get("review_required") == "true"
        storage.set_setting("review_required", "true" if enabled else "false")
        audit(request, session["username"], "settings.review", required=enabled)
        return RedirectResponse("/admin/settings", status_code=303)

    @app.post("/admin/announcements/{announcement_id}/schedule")
    async def schedule_announcement(request: Request, announcement_id: str) -> Response:
        session, form = await checked_form(request, "edit")
        current = announcement_or_404(announcement_id)
        if review_required() and current.status != AnnouncementStatus.APPROVED:
            raise HTTPException(
                status_code=409, detail="Review is on: this announcement must be approved first."
            )
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
        return render(
            request,
            "settings.html",
            page(session, channels=channels, review_required=review_required()),
        )

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

    def mirroring_page(
        request: Request, session: dict[str, Any], error: str | None = None, status_code: int = 200
    ):
        rooms = mirror()
        labels = {a.name: a.capabilities.label for a in publisher.adapters}
        return render(
            request,
            "mirroring.html",
            page(
                session,
                available=rooms is not None,
                rooms=rooms.rooms() if rooms else [],
                channel_labels=labels,
                error=error,
            ),
            status_code=status_code,
        )

    @app.get("/admin/mirroring")
    async def mirroring(request: Request) -> Response:
        return mirroring_page(request, require(request, "settings"))

    @app.post("/admin/mirroring/add")
    async def add_mirrored_room(request: Request) -> Response:
        session, _ = await checked_form(request, "settings")
        raw = await request.form()
        rooms = mirror()
        if rooms is None:
            raise HTTPException(
                status_code=409, detail="Set up the Matrix announcements channel first."
            )
        known = {a.name for a in publisher.adapters}
        channels = [c for c in raw.getlist("channel") if c in known]
        if not channels:
            return mirroring_page(request, session, "Choose at least one channel.", 400)
        try:
            added = await run_in_threadpool(
                rooms.add_room,
                str(raw.get("room", "")).strip(),
                str(raw.get("mode", "review")),
                MODERATOR_POWER if raw.get("who") != "everyone" else 0,
                channels,
                session["username"],
            )
        except ValueError as exc:
            return mirroring_page(request, session, str(exc), 400)
        audit(
            request,
            session["username"],
            "mirroring.room_added",
            added["room_id"],
            mode=str(raw.get("mode")),
            channels=channels,
        )
        return RedirectResponse("/admin/mirroring", status_code=303)

    @app.post("/admin/mirroring/remove")
    async def remove_mirrored_room(request: Request) -> Response:
        session, form = await checked_form(request, "settings")
        rooms = mirror()
        if rooms is not None:
            rooms.remove_room(form.get("room_id", ""))
        audit(request, session["username"], "mirroring.room_removed", form.get("room_id", ""))
        return RedirectResponse("/admin/mirroring", status_code=303)

    @app.get("/admin/channels/signal/link")
    async def signal_link(request: Request) -> Response:
        session = require(request, "settings")
        status, accounts = await run_in_threadpool(
            signal_send, "GET", f"{SIGNAL_API}/v1/accounts", {}, None
        )
        groups, error = [], None
        if status != 200 or not isinstance(accounts, list):
            error = "The Signal service is not running. Turn on Signal on the Services page first."
            accounts = []
        for account in accounts:
            number = account.get("number") or account.get("aci")
            g_status, found = await run_in_threadpool(
                signal_send, "GET", f"{SIGNAL_API}/v1/groups/{quote(number, safe='')}", {}, None
            )
            for group in found if g_status == 200 and isinstance(found, list) else []:
                groups.append({"number": number, "id": group.get("id"), "name": group.get("name")})
        return render(
            request,
            "signal_link.html",
            page(session, accounts=accounts, groups=groups, error=error),
        )

    @app.get("/admin/channels/signal/qr")
    async def signal_qr(request: Request) -> Response:
        require(request, "settings")
        status, image = await run_in_threadpool(
            signal_send,
            "GET",
            f"{SIGNAL_API}/v1/qrcodelink?device_name=Community%20Stack",
            {},
            None,
        )
        if status != 200 or not isinstance(image, bytes):
            raise HTTPException(status_code=503, detail="The Signal service is not running.")
        return Response(image, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/admin/channels/signal/use")
    async def signal_use_group(request: Request) -> Response:
        session, form = await checked_form(request, "settings")
        try:
            values = CONNECTORS["signal"].validate(form)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        storage.save_connection("signal", values, session["username"])
        publisher.reload_connections()
        audit(
            request, session["username"], "channel.connected", "signal", group=values["group_name"]
        )
        return RedirectResponse("/admin/channels", status_code=303)

    @app.post("/admin/channels/{kind}/test")
    async def test_channel(request: Request, kind: str) -> Response:
        session, _ = await checked_form(request, "settings")
        connector = connector_for(kind)
        values = storage.list_connections().get(kind)
        if values is None:
            raise HTTPException(status_code=409, detail="This channel is not connected.")
        adapter = next((a for a in publisher.adapters if a.name == kind), None)
        result = await run_in_threadpool((adapter or connector.build(values)).health_check)
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

    def account_page(
        request: Request,
        session: dict[str, Any],
        status_code: int = 200,
        **context: Any,
    ) -> HTMLResponse:
        username = session["username"]
        values = {
            "role_description": ROLE_DESCRIPTIONS[session["role"]],
            "error": None,
            "saved": False,
            "two_step": auth_store.totp_enabled(username),
            "codes_left": auth_store.recovery_codes_left(username),
            "setup": None,
            "recovery_codes": None,
            "two_step_error": None,
            **context,
        }
        return render(request, "account.html", page(session, **values), status_code=status_code)

    @app.post("/admin/accounts/two-step")
    async def reset_two_step(request: Request) -> Response:
        session, form = await checked_form(request, "accounts")
        account = managed_account(session, form)
        auth_store.disable_totp(account["username"])
        audit(request, session["username"], "two_step.reset", account["username"])
        return RedirectResponse("/admin/accounts", status_code=303)

    @app.get("/admin/account")
    async def my_account(request: Request) -> Response:
        session = require(request, "view")
        return account_page(request, session, saved=request.query_params.get("saved") == "1")

    @app.post("/admin/account/two-step/start")
    async def start_two_step(request: Request) -> Response:
        session, form = await checked_form(request, "view")
        if not await run_in_threadpool(
            auth_store.verify_password, session["username"], form.get("current_password", "")
        ):
            return account_page(
                request, session, 400, two_step_error="Your current password is not correct."
            )
        secret = auth_store.start_totp(session["username"])
        uri = totp.provisioning_uri(secret, session["username"], "Community Stack")
        setup = {"secret": secret, "qr": Markup(totp.qr_svg(uri))}
        return account_page(request, session, setup=setup)

    @app.post("/admin/account/two-step/confirm")
    async def confirm_two_step(request: Request) -> Response:
        session, form = await checked_form(request, "view")
        codes = auth_store.confirm_totp(session["username"], form.get("code", ""))
        if codes is None:
            return account_page(
                request,
                session,
                400,
                two_step_error="That code did not match. Start again and scan the new code.",
            )
        audit(request, session["username"], "two_step.enabled", session["username"])
        return account_page(request, session, recovery_codes=codes)

    @app.post("/admin/account/two-step/recovery-codes")
    async def new_recovery_codes(request: Request) -> Response:
        session, form = await checked_form(request, "view")
        if not auth_store.verify_second_factor(session["username"], form.get("code", "")):
            return account_page(request, session, 400, two_step_error="That code did not work.")
        codes = auth_store.regenerate_recovery_codes(session["username"])
        audit(request, session["username"], "two_step.recovery_codes_replaced")
        return account_page(request, session, recovery_codes=codes)

    @app.post("/admin/account/two-step/disable")
    async def disable_two_step(request: Request) -> Response:
        session, form = await checked_form(request, "view")
        password_ok = await run_in_threadpool(
            auth_store.verify_password, session["username"], form.get("current_password", "")
        )
        if not password_ok or not auth_store.verify_second_factor(
            session["username"], form.get("code", "")
        ):
            return account_page(
                request, session, 400, two_step_error="The password or the code did not work."
            )
        auth_store.disable_totp(session["username"])
        audit(request, session["username"], "two_step.disabled", session["username"])
        return account_page(request, session)

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
        return account_page(request, session, 400, error=error)

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
        outcome: dict[str, Any] | None = None,
    ) -> HTMLResponse:
        values, status, components = None, None, []
        if agent.available():
            try:
                config = await run_in_threadpool(agent.call, "config.get", session["username"])
                values = config["values"]
                status = await run_in_threadpool(agent.call, "status.get", session["username"])
                components = (
                    await run_in_threadpool(agent.call, "services.versions", session["username"])
                )["components"]
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
                components=components,
                outcome=outcome,
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

    @app.post("/admin/services/update")
    async def update_service(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        service = form.get("service", "")
        try:
            result = await run_in_threadpool(
                agent.call, "services.update", session["username"], {"service": service}
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(request, session["username"], "system.services.update", service, outcome="failed")
            return await services_page(
                request, session, None, 400, {"title": f"Updating {service}", "error": str(exc)}
            )
        audit(request, session["username"], "system.services.update", service, outcome="succeeded")
        return await services_page(
            request, session, outcome={"title": f"Updating {service}", "steps": result["steps"]}
        )

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
                "label": "Turn on two-step login for every owner and admin",
                "done": all(account["totp_enabled_at"] for account in managers),
                "link": "/admin/account",
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

    async def backups_page(
        request: Request,
        session: dict[str, Any],
        outcome: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        status, error = None, None
        if agent.available():
            try:
                status = await run_in_threadpool(agent.call, "backups.status", session["username"])
            except (AgentUnavailable, AgentRefused) as exc:
                error = str(exc)
        return render(
            request,
            "backups.html",
            page(
                session,
                agent_available=agent.available(),
                status=status,
                error=error,
                outcome=outcome,
            ),
            status_code=status_code,
        )

    @app.get("/admin/backups")
    async def backups(request: Request) -> Response:
        return await backups_page(request, require(request, "system"))

    async def backup_change(
        request: Request, op: str, title: str, fields: tuple[str, ...]
    ) -> HTMLResponse:
        session, form = await checked_form(request, "system")
        params = {name: form.get(name, "").strip() for name in fields if form.get(name, "").strip()}
        try:
            result = await run_in_threadpool(
                agent.call, op, session["username"], params, form.get("password") or None
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(request, session["username"], f"system.{op}", outcome="failed")
            return await backups_page(request, session, {"title": title, "error": str(exc)}, 400)
        audit(request, session["username"], f"system.{op}", outcome="succeeded")
        return await backups_page(request, session, {"title": title, "steps": result["steps"]})

    @app.post("/admin/backups/offsite")
    async def backups_offsite(request: Request) -> Response:
        return await backup_change(
            request,
            "backups.use_offsite",
            "Off-site backups",
            ("endpoint", "bucket", "prefix", "region", "access_key_id", "secret_access_key"),
        )

    @app.post("/admin/backups/local")
    async def backups_local(request: Request) -> Response:
        return await backup_change(request, "backups.use_local", "Backups on this server", ())

    @app.get("/admin/backups/offline-archive.zip")
    async def offline_archive(request: Request) -> Response:
        session = require(request, "system")
        try:
            name = load_config(config_path)["project"]["name"]
        except (ConfigError, KeyError, OSError):
            name = "Community"
        with tempfile.TemporaryDirectory() as directory:
            path = OfflineArchiveExporter(storage=storage, community_name=name).export_archive(
                Path(directory) / "offline-announcements.zip"
            )
            content = path.read_bytes()
        audit(request, session["username"], "announcements.offline_archive_downloaded")
        return Response(
            content,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="offline-announcements.zip"'},
        )

    @app.get("/admin/health")
    async def health_page(request: Request) -> Response:
        session = require(request, "system")
        service = request.query_params.get("service")
        lines = request.query_params.get("lines", "200")
        health_result, logs, error = None, None, None
        if agent.available():
            try:
                health_result = await run_in_threadpool(
                    agent.call, "health.get", session["username"]
                )
                if service:
                    logs = await run_in_threadpool(
                        agent.call,
                        "logs.get",
                        session["username"],
                        {"service": service, "lines": lines},
                    )
            except (AgentUnavailable, AgentRefused) as exc:
                error = str(exc)
        return render(
            request,
            "health.html",
            page(
                session,
                agent_available=agent.available(),
                health=health_result,
                logs=logs,
                error=error,
                log_services=LOG_SERVICES,
                log_lines=LOG_LINES,
                selected_service=service,
                selected_lines=lines,
            ),
        )

    @app.get("/admin/start")
    async def start(request: Request) -> Response:
        session = require(request, "system")
        return render(request, "start.html", page(session, steps=setup_steps()))

    @app.get("/admin/audit")
    async def audit_log(request: Request) -> Response:
        session = require(request, "audit")
        return render(request, "audit.html", page(session, events=auth_store.list_events()))

    return app
