"""Authenticated, server-rendered announcement administration."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import mimetypes
import os
import re
import secrets
import tempfile
from collections.abc import AsyncIterator
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import available_timezones

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
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
from starlette.exceptions import HTTPException as StarletteHTTPException

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
from community_stack.announcements.connectors import CONNECTORS, build_connector, merge_values
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
# Everything a console page uses is served from here; nothing is loaded from elsewhere.
CONSOLE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; object-src 'none'"
)
# The dashboard shows service status; it must never wait on a slow agent.
DASHBOARD_AGENT_TIMEOUT_SECONDS = 5.0

mimetypes.add_type("font/woff2", ".woff2")

logger = logging.getLogger(__name__)

# Announcement, delivery, job and health states as people read them, with a colour tone.
STATUS_BADGES = {
    "draft": ("Draft", "neutral"),
    "in_review": ("In review", "warning"),
    "approved": ("Approved", "info"),
    "scheduled": ("Scheduled", "info"),
    "published": ("Published", "success"),
    "updated": ("Updated", "success"),
    "withdrawn": ("Withdrawn", "neutral"),
    "delivered": ("Delivered", "success"),
    "failed": ("Failed", "danger"),
    "queued": ("Waiting to retry", "warning"),
    "running": ("In progress", "info"),
    "ok": ("OK", "success"),
    "warning": ("Needs attention", "warning"),
    "problem": ("Problem", "danger"),
}


def status_badge(value: Any, label: str | None = None, tone: str | None = None) -> Markup:
    key = str(getattr(value, "value", value))
    known_label, known_tone = STATUS_BADGES.get(
        key, (key.replace("_", " ").capitalize(), "neutral")
    )
    return Markup('<span class="badge badge-{}">{}</span>').format(
        tone or known_tone, label or known_label
    )


def flash(text: str, tone: str = "success", details: list[str] | None = None, **extra: Any):
    """A message shown once at the top of the next page: success, warning or error."""
    return {"tone": tone, "text": text, "details": details or [], **extra}


def static_version() -> str:
    """Changes whenever a static file does, so browsers never mix old scripts with new pages."""
    digest = hashlib.sha256()
    for path in sorted(item for item in STATIC_DIR.rglob("*") if item.is_file()):
        digest.update(path.relative_to(STATIC_DIR).as_posix().encode() + path.read_bytes())
    return digest.hexdigest()[:12]


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

    def configured_channel_links() -> dict[str, str]:
        return next(
            (
                adapter.channel_links.copy()
                for adapter in publisher.adapters
                if isinstance(adapter, WebFeedAdapter)
            ),
            {},
        )

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.globals["badge"] = status_badge
    templates.env.globals["static_version"] = static_version()

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

    @app.middleware("http")
    async def console_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/admin") and response.headers.get(
            "content-type", ""
        ).startswith("text/html"):
            response.headers.setdefault("Content-Security-Policy", CONSOLE_CSP)
            response.headers.setdefault("Referrer-Policy", "same-origin")
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    @app.exception_handler(LoginRequired)
    async def login_required(_request: Request, _exc: LoginRequired) -> Response:
        return login_redirect()

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        """Console errors are pages in the console; public and JSON endpoints keep JSON."""
        path = request.url.path
        if not path.startswith("/admin") or path.startswith(
            ("/admin/preview", "/admin/static/", "/admin/media/", "/admin/channels/signal/qr")
        ):
            return await http_exception_handler(request, exc)
        message = exc.detail
        if message == HTTPStatus(exc.status_code).phrase:
            message = {
                404: "This page does not exist.",
                405: "This address only works from a button in the console.",
            }.get(exc.status_code, message)
        session = authenticated(request)
        context = page(session) if session else {"csrf_token": ""}
        response = render(
            request,
            "error.html",
            {**context, "status_code": exc.status_code, "message": message},
            status_code=exc.status_code,
        )
        response.headers.update(exc.headers or {})
        return response

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
        context = {"template_name": template, **(context or {})}
        if "username" in context and "flash" not in context:
            context["flash"] = auth_store.pop_flash(request.cookies.get(SESSION_COOKIE))
        return templates.TemplateResponse(
            request=request,
            name=template,
            context=context,
            status_code=status_code,
        )

    def redirect_with(request: Request, url: str, message: dict[str, Any]) -> RedirectResponse:
        """Post/redirect/get with a message for the page the redirect lands on."""
        auth_store.set_flash(request.cookies.get(SESSION_COOKIE), message)
        return RedirectResponse(url, status_code=303)

    def channel_label(name: str) -> str:
        return next((a.capabilities.label for a in publisher.adapters if a.name == name), name)

    def delivery_message(text: str, results: dict[str, Any]) -> dict[str, Any]:
        failed = sorted(
            channel_label(name) for name, result in results.items() if not result.success
        )
        if not failed:
            return flash(text)
        return flash(
            f"{text} Some channels failed: {', '.join(failed)}.",
            "warning",
            ["Failures that are safe to repeat are retried automatically. See the delivery list."],
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

    status_agent = AgentClient(agent.socket_path, timeout=DASHBOARD_AGENT_TIMEOUT_SECONDS)

    async def service_status(session: dict[str, Any]) -> dict[str, Any] | None:
        """Running services for the dashboard strip, or why they are unknown."""
        if not can(session["role"], "system"):
            return None
        if not agent.available():
            return {"error": "System agent not running"}
        try:
            status = await run_in_threadpool(status_agent.call, "status.get", session["username"])
        except (AgentUnavailable, AgentRefused):
            return {"error": "System agent not answering"}
        if not status.get("docker_available"):
            return {"error": "Docker is not reachable"}
        return {"containers": status.get("containers") or []}

    @app.get("/admin")
    async def dashboard(request: Request) -> Response:
        session = require(request, "view")
        steps = setup_steps() if can(session["role"], "system") else []
        everything = storage.list_announcements(limit=None)
        month = datetime.now(publisher.timezone).strftime("%Y-%m")
        failed = storage.get_failed_deliveries()
        titles = {a.id: a.title for a in everything}
        failures: dict[str, dict[str, Any]] = {}
        for delivery in failed:
            entry = failures.setdefault(
                delivery["adapter_name"],
                {
                    "label": channel_label(delivery["adapter_name"]),
                    "count": 0,
                    "error": delivery.get("last_error"),
                    "announcement_id": delivery["announcement_id"],
                },
            )
            entry["count"] += 1
        failing = set(failures)
        enabled = storage.get_channel_settings()
        recent = everything[:8]
        return render(
            request,
            "dashboard.html",
            page(
                session,
                setup_done=sum(step["done"] for step in steps),
                setup_total=len(steps),
                next_step=next((step for step in steps if not step["done"]), None),
                announcements=recent,
                deliveries={a.id: storage.get_delivery_states(a.id) for a in recent},
                awaiting_review=[
                    a
                    for a in everything
                    if a.status == AnnouncementStatus.IN_REVIEW
                    and storage.last_submitter(a.id) != session["username"]
                ]
                if can(session["role"], "approve")
                else [],
                counts={
                    "published": sum(
                        1
                        for a in everything
                        if a.status in (AnnouncementStatus.PUBLISHED, AnnouncementStatus.UPDATED)
                        and (a.published_at or "").startswith(month)
                    ),
                    "scheduled": sum(a.status == AnnouncementStatus.SCHEDULED for a in everything),
                    "in_review": sum(a.status == AnnouncementStatus.IN_REVIEW for a in everything),
                },
                failed_count=len(failed),
                failures=list(failures.values()),
                jobs=[
                    {**job, "title": titles.get(job["announcement_id"], "")}
                    for job in storage.get_active_jobs()
                ],
                channels=[
                    {
                        "label": adapter.capabilities.label,
                        "state": "failing"
                        if adapter.name in failing
                        else ("on" if enabled.get(adapter.name, True) else "off"),
                    }
                    for adapter in publisher.adapters
                ],
                services=await service_status(session),
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
        auth_store.set_flash(
            token, flash(f"Welcome, {username}. Next, work through the setup checklist.")
        )
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

    ANNOUNCEMENT_FILTERS = {
        "draft": ("Drafts", (AnnouncementStatus.DRAFT, AnnouncementStatus.APPROVED)),
        "in_review": ("In review", (AnnouncementStatus.IN_REVIEW,)),
        "scheduled": ("Scheduled", (AnnouncementStatus.SCHEDULED,)),
        "published": ("Published", (AnnouncementStatus.PUBLISHED, AnnouncementStatus.UPDATED)),
        "withdrawn": ("Withdrawn", (AnnouncementStatus.WITHDRAWN,)),
    }

    @app.get("/admin/announcements")
    async def announcement_list(request: Request) -> Response:
        session = require(request, "view")
        everything = storage.list_announcements(limit=None)
        selected = request.query_params.get("status", "")
        if selected not in ANNOUNCEMENT_FILTERS:
            selected = ""
        filters = [{"key": "", "label": "All", "count": len(everything)}] + [
            {"key": key, "label": label, "count": sum(a.status in states for a in everything)}
            for key, (label, states) in ANNOUNCEMENT_FILTERS.items()
        ]
        shown = (
            [a for a in everything if a.status in ANNOUNCEMENT_FILTERS[selected][1]]
            if selected
            else everything
        )
        return render(
            request,
            "announcements.html",
            page(session, announcements=shown, filters=filters, selected=selected),
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
        return redirect_with(request, "/admin/templates", flash("Template saved."))

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
        return redirect_with(request, "/admin/templates", flash("Template saved."))

    @app.post("/admin/templates/{template_id}/delete")
    async def delete_template(request: Request, template_id: str) -> Response:
        session, _ = await checked_form(request, "edit")
        template = template_or_404(template_id)
        storage.delete_template(template_id)
        audit(request, session["username"], "template.deleted", template_id, name=template["name"])
        return redirect_with(
            request, "/admin/templates", flash(f"Deleted the template {template['name']}.")
        )

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
        return redirect_with(request, "/admin/templates", flash(f"Saved as the template {name}."))

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
        return redirect_with(
            request, f"/admin/announcements/{announcement.id}", flash("Draft saved.")
        )

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
        return redirect_with(
            request, f"/admin/announcements/{announcement_id}", flash("Image added.")
        )

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
        return redirect_with(
            request, f"/admin/announcements/{announcement_id}", flash("Image description saved.")
        )

    @app.post("/admin/announcements/{announcement_id}/image/remove")
    async def remove_image(request: Request, announcement_id: str) -> Response:
        session, _ = await checked_form(request, "edit")
        editable_announcement(announcement_id)
        storage.remove_image(announcement_id)
        audit(request, session["username"], "announcement.image_removed", announcement_id)
        return redirect_with(
            request, f"/admin/announcements/{announcement_id}", flash("Image removed.")
        )

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
        message = flash("Changes saved.")
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
            message = flash(
                "Changes saved. It changed after review, so it is a draft again and needs a new review.",
                "warning",
            )
        audit(
            request,
            session["username"],
            "announcement.updated",
            announcement_id,
            title=announcement.title,
        )
        return redirect_with(request, f"/admin/announcements/{announcement_id}", message)

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
        return redirect_with(
            request,
            f"/admin/announcements/{announcement_id}",
            delivery_message("Published.", results),
        )

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
        return redirect_with(
            request,
            f"/admin/announcements/{announcement_id}",
            flash("Submitted for review. Someone else needs to approve it."),
        )

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
        text = (
            "Approved. It can now be published or scheduled."
            if approve
            else "Sent back for changes, with your note."
        )
        return redirect_with(request, f"/admin/announcements/{announcement_id}", flash(text))

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
        return redirect_with(
            request,
            f"/admin/announcements/{announcement_id}",
            delivery_message(
                "Published without review. Your reason is in the activity log.", results
            ),
        )

    @app.post("/admin/settings/review")
    async def update_review_setting(request: Request) -> Response:
        session, form = await checked_form(request, "settings")
        enabled = form.get("review_required") == "true"
        storage.set_setting("review_required", "true" if enabled else "false")
        audit(request, session["username"], "settings.review", required=enabled)
        state = "on" if enabled else "off"
        return redirect_with(
            request, "/admin/settings", flash(f"Review before publishing is now {state}.")
        )

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
        when = local.strftime("%A %d %B %Y, %H:%M")
        return redirect_with(
            request,
            f"/admin/announcements/{announcement_id}",
            flash(f"Scheduled for {when} ({publisher.timezone})."),
        )

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
        return redirect_with(
            request,
            f"/admin/announcements/{announcement_id}",
            flash("Schedule cancelled. The announcement is a draft again."),
        )

    @app.post("/admin/announcements/{announcement_id}/withdraw")
    async def withdraw_announcement(request: Request, announcement_id: str) -> Response:
        session, form = await checked_form(request, "edit")
        if form.get("confirm") != "withdraw":
            raise HTTPException(
                status_code=400, detail="Explicit withdrawal confirmation is required."
            )
        try:
            announcement, results = publisher.withdraw(announcement_id)
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
        return redirect_with(
            request,
            f"/admin/announcements/{announcement_id}",
            delivery_message("Withdrawn from public channels.", results),
        )

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
        text = f"Retried {len(results)} failed deliveries." if results else "Nothing to retry."
        return redirect_with(request, f"/admin/announcements/{announcement_id}", flash(text))

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
                adapter.channel_links = configured_channel_links()
                if not settings.get("matrix_room", True):
                    adapter.channel_links.pop("Matrix announcements", None)
                if settings.get("web_feed", True):
                    adapter.rebuild()
        audit(request, session["username"], "settings.updated", enabled=sorted(enabled_names))
        return redirect_with(request, "/admin/settings", flash("Publishing destinations saved."))

    def channels_page(
        request: Request,
        session: dict[str, Any],
        message: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        """The channels page; `message` (an error from a form) replaces the stored flash."""
        saved = storage.list_connections()
        enabled = storage.get_channel_settings()
        connectors = [
            {
                "type": connector,
                "connected": kind in saved,
                "enabled": enabled.get(kind, True),
                "available": kind != "whatsapp"
                or (publisher.whatsapp_bridge_enabled and publisher.matrix_auth is not None),
                "values": {
                    field.name: saved.get(kind, {}).get(field.name, "")
                    for field in connector.fields
                    if not field.secret
                },
            }
            for kind, connector in CONNECTORS.items()
        ]
        context = page(session, connectors=connectors)
        if message:
            context["flash"] = message
        return render(request, "channels.html", context, status_code=status_code)

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
        if kind == "whatsapp" and not publisher.whatsapp_bridge_enabled:
            return channels_page(
                request,
                session,
                flash("Enable the WhatsApp bridge first on the Services page.", "error"),
                409,
            )
        if kind == "whatsapp" and publisher.matrix_auth is None:
            return channels_page(
                request,
                session,
                flash(
                    "Connect Matrix announcements first; WhatsApp forwarding uses that account.",
                    "error",
                ),
                409,
            )
        saved = storage.list_connections().get(kind, {})
        try:
            values = await run_in_threadpool(
                connector.validate, merge_values(connector, form, saved)
            )
        except (UnsafeURLError, ValueError) as exc:
            return channels_page(request, session, flash(f"{connector.label}: {exc}", "error"), 400)
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
        return redirect_with(
            request,
            f"/admin/channels#channel-{kind}",
            flash(f"{connector.label} saved. Use 'Test connection' to check it."),
        )

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
                **({"flash": flash(error, "error")} if error else {}),
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
        return redirect_with(
            request, "/admin/mirroring", flash(f"Mirroring {raw.get('room', '')}.".strip())
        )

    @app.post("/admin/mirroring/remove")
    async def remove_mirrored_room(request: Request) -> Response:
        session, form = await checked_form(request, "settings")
        rooms = mirror()
        if rooms is not None:
            rooms.remove_room(form.get("room_id", ""))
        audit(request, session["username"], "mirroring.room_removed", form.get("room_id", ""))
        return redirect_with(request, "/admin/mirroring", flash("Stopped mirroring that room."))

    async def matrix_users_page(
        request: Request,
        session: dict[str, Any],
        outcome: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        listing, error = None, None
        if agent.available():
            try:
                listing = await run_in_threadpool(
                    agent.call, "matrix.users.list", session["username"]
                )
            except (AgentUnavailable, AgentRefused) as exc:
                error = str(exc)
        return render(
            request,
            "matrix_users.html",
            page(
                session,
                agent_available=agent.available(),
                listing=listing,
                error=error,
                **({"flash": outcome} if outcome else {}),
            ),
            status_code=status_code,
        )

    @app.get("/admin/matrix-users")
    async def matrix_users(request: Request) -> Response:
        return await matrix_users_page(request, require(request, "system"))

    async def matrix_user_action(
        request: Request, op: str, title: str, fields: tuple[str, ...]
    ) -> HTMLResponse:
        session, form = await checked_form(request, "system")
        params = {name: form.get(name, "").strip() for name in fields if form.get(name, "").strip()}
        try:
            result = await run_in_threadpool(
                agent.call, op, session["username"], params, form.get("password") or None
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(
                request,
                session["username"],
                f"system.{op}",
                params.get("user_id") or params.get("username"),
                outcome="failed",
            )
            return await matrix_users_page(request, session, flash(f"{title}: {exc}", "error"), 400)
        # Temporary passwords are shown once and never written to the activity log or
        # stored as a message: this response is the only place they appear.
        audit(
            request, session["username"], f"system.{op}", result.get("user_id"), outcome="succeeded"
        )
        message = flash(f"{title}: {result.get('user_id')}")
        if result.get("temporary_password"):
            message.update(
                secret=result["temporary_password"],
                secret_label="Temporary password, shown only now",
            )
        return await matrix_users_page(request, session, message)

    @app.post("/admin/matrix-users/create")
    async def create_matrix_user(request: Request) -> Response:
        return await matrix_user_action(
            request, "matrix.users.create", "New Matrix account", ("username", "display_name")
        )

    @app.post("/admin/matrix-users/reset-password")
    async def reset_matrix_password(request: Request) -> Response:
        return await matrix_user_action(
            request, "matrix.users.reset_password", "New temporary password", ("user_id",)
        )

    @app.post("/admin/matrix-users/deactivate")
    async def deactivate_matrix_user(request: Request) -> Response:
        return await matrix_user_action(
            request, "matrix.users.deactivate", "Account deactivated", ("user_id",)
        )

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
        return redirect_with(
            request,
            "/admin/channels#channel-signal",
            flash(f"Announcements will be posted to the Signal group {values['group_name']}."),
        )

    @app.post("/admin/channels/{kind}/test")
    async def test_channel(request: Request, kind: str) -> Response:
        session, _ = await checked_form(request, "settings")
        connector = connector_for(kind)
        if kind == "whatsapp" and not (
            publisher.whatsapp_bridge_enabled and publisher.matrix_auth is not None
        ):
            raise HTTPException(
                status_code=409,
                detail="Enable the WhatsApp bridge and connect Matrix announcements first.",
            )
        values = storage.list_connections().get(kind)
        if values is None:
            raise HTTPException(status_code=409, detail="This channel is not connected.")
        try:
            adapter = next((a for a in publisher.adapters if a.name == kind), None)
            if adapter is None:
                adapter = build_connector(kind, values, publisher.matrix_auth)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        result = await run_in_threadpool(adapter.health_check)
        audit(request, session["username"], "channel.tested", kind, ok=result.ok)
        return redirect_with(
            request,
            f"/admin/channels#channel-{kind}",
            flash(f"{connector.label}: {result.details}", "success" if result.ok else "error"),
        )

    @app.post("/admin/channels/{kind}/disconnect")
    async def disconnect_channel(request: Request, kind: str) -> Response:
        session, _ = await checked_form(request, "settings")
        connector = connector_for(kind)
        storage.delete_connection(kind)
        publisher.reload_connections()
        audit(request, session["username"], "channel.disconnected", kind)
        return redirect_with(
            request,
            f"/admin/channels#channel-{kind}",
            flash(f"{connector.label} disconnected. Its saved credentials were deleted."),
        )

    @app.get("/admin/whatsapp")
    async def whatsapp_setup(request: Request) -> Response:
        session = require(request, "settings")
        try:
            config = load_config(config_path) if Path(config_path).is_file() else {}
        except ConfigError:
            config = {}
        return render(
            request,
            "whatsapp.html",
            page(
                session,
                bridge_enabled=publisher.whatsapp_bridge_enabled,
                matrix_connected=publisher.matrix_auth is not None,
                channel_connected="whatsapp" in storage.list_connections(),
                matrix_domain=config.get("domain", {}).get("matrix", "your Matrix server"),
            ),
        )

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
                **({"flash": flash(error, "error")} if error else {}),
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
        return redirect_with(
            request,
            "/admin/accounts",
            flash(f"Created the account {username}. Share the temporary password privately."),
        )

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
        return redirect_with(
            request, "/admin/accounts", flash(f"{account['username']} is now {role}.")
        )

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
        text = (
            f"{account['username']} can no longer sign in."
            if disable
            else f"{account['username']} can sign in again."
        )
        return redirect_with(request, "/admin/accounts", flash(text))

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
        return redirect_with(
            request,
            "/admin/accounts",
            flash(f"Password reset for {account['username']}. Share it with them privately."),
        )

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
        return redirect_with(
            request,
            "/admin/accounts",
            flash(
                f"Two-step login is off for {account['username']}. "
                "They can turn it on again from My account."
            ),
        )

    @app.get("/admin/account")
    async def my_account(request: Request) -> Response:
        session = require(request, "view")
        return account_page(request, session)

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
        return redirect_with(request, "/admin/account", flash("Two-step login is off."))

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
                return redirect_with(
                    request,
                    "/admin/account",
                    flash("Password changed. Your other sessions were signed out."),
                )
        return account_page(request, session, 400, error=error)

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
            return await backups_page(
                request, session, flash(f"{title} failed: {exc}", "error"), 400
            )
        audit(request, session["username"], f"system.{op}", outcome="succeeded")
        details = []
        if result.get("id"):
            encrypted = "encrypted" if result.get("encrypted") else "not encrypted"
            details.append(f"Snapshot {result['id']}, {encrypted}.")
        details += [f"Warning: {line}" for line in result.get("warnings") or []]
        details += list(result.get("messages") or []) + list(result.get("checks") or [])
        tone = "warning" if result.get("warnings") else "success"
        return redirect_with(request, "/admin/backups", flash(f"{title} finished.", tone, details))

    @app.get("/admin/system")
    async def system(request: Request) -> Response:
        # Its backup actions moved to Backups and its service list to Services.
        require(request, "system")
        return RedirectResponse("/admin/backups", status_code=303)

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
        message: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        values, status, components, error = None, None, [], None
        if agent.available():
            try:
                config = await run_in_threadpool(agent.call, "config.get", session["username"])
                values = config["values"]
                status = await run_in_threadpool(agent.call, "status.get", session["username"])
                components = (
                    await run_in_threadpool(agent.call, "services.versions", session["username"])
                )["components"]
            except (AgentUnavailable, AgentRefused) as exc:
                error = str(exc)
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
                timezones=sorted(available_timezones()),
                error=error,
                **({"flash": message} if message else {}),
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
                request, session, flash(f"Updating {service} failed: {exc}", "error"), 400
            )
        audit(request, session["username"], "system.services.update", service, outcome="succeeded")
        return redirect_with(
            request, "/admin/services", flash(f"Updated {service}.", details=result["steps"])
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
            return await services_page(request, session, flash(str(exc), "error"), 400)
        if not plan["changes"]:
            return await services_page(
                request, session, flash("Nothing was changed.", "warning"), 400
            )
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
        publisher.reload_connections()
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
                "link": "/admin/health",
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
                "link": "/admin/backups#restore-drill",
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
                **({"flash": outcome} if outcome else {}),
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
            return await backups_page(request, session, flash(f"{title}: {exc}", "error"), 400)
        audit(request, session["username"], f"system.{op}", outcome="succeeded")
        return redirect_with(request, "/admin/backups", flash(title, details=result["steps"]))

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
