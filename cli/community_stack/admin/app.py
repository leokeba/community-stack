"""Authenticated, server-rendered announcement administration."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
from collections.abc import AsyncIterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from community_stack.admin.accounts import register_accounts
from community_stack.admin.activitypub import register_activitypub
from community_stack.admin.announcements import register_announcements
from community_stack.admin.auth import register_auth
from community_stack.admin.backups import register_backups
from community_stack.admin.bridges import register_bridges
from community_stack.admin.channels import register_channels
from community_stack.admin.common import (
    CONSOLE_CSP,
    DEFAULT_AGENT_SOCKET,
    LOGIN_IP_LIMIT,
    PREAUTH_CSRF_COOKIE,
    SESSION_COOKIE,
    STATIC_DIR,
    TEMPLATE_DIR,
    WORKER_INTERVAL_SECONDS,
    LoginRequired,
    flash,
    logger,
    static_version,
    status_badge,
)
from community_stack.admin.continuity import register_continuity
from community_stack.admin.dashboard import register_dashboard
from community_stack.admin.matrix_users import register_matrix_users
from community_stack.admin.mirroring import register_mirroring
from community_stack.admin.request_limits import RequestBodyLimitMiddleware
from community_stack.admin.safemode import register_safemode
from community_stack.admin.services import register_services
from community_stack.admin.settings import register_settings
from community_stack.admin.store import PERMISSIONS, SESSION_TTL, AdminStore, can, tokens_match
from community_stack.admin.subscriptions import register_subscriptions
from community_stack.admin.system import register_system
from community_stack.admin.templates import register_templates
from community_stack.admin.vikunja_users import register_vikunja_users
from community_stack.agent.client import AgentClient, AgentRefused, AgentUnavailable
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.matrix_mirror import mirror_for
from community_stack.announcements.models import Announcement, AnnouncementStatus
from community_stack.announcements.platform_content import generate_platform_text
from community_stack.announcements.platforms import PLATFORM_CATALOG, PLATFORMS
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.config import ConfigError, load_config
from community_stack.config_edit import get_value
from community_stack.continuity.service import ContinuityService

# What each sign-up choice receives. Announcements tagged 'important' reach the second group.
__all__ = ["LOGIN_IP_LIMIT", "AdminStore", "create_app", "tokens_match"]


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
    publisher = AnnouncementPublisher(storage=storage, config_path=config_path, feed_dir=feed_dir)
    continuity_service = ContinuityService(storage, publisher, config_path)

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
            storage, publisher, lambda: storage.get_setting("review_required", "false") == "true"
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
    app.add_middleware(RequestBodyLimitMiddleware)
    app.mount("/admin/static", StaticFiles(directory=STATIC_DIR), name="admin-static")
    app.state.storage = storage
    app.state.auth_store = auth_store
    app.state.publisher = publisher
    app.state.continuity = continuity_service
    app.state.agent = agent

    @app.middleware("http")
    async def console_headers(request: Request, call_next):
        oauth_callback = request.url.path == "/admin/channels/pixelfed/callback"
        try:
            response = await call_next(request)
        finally:
            if oauth_callback:
                request.scope["query_string"] = b""
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if response.headers.get("content-type", "").startswith("text/html"):
            policy = (
                CONSOLE_CSP if request.url.path.startswith("/admin") else "frame-ancestors 'none'"
            )
            response.headers.setdefault("Content-Security-Policy", policy)
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
        response: Response, token: str, name: str = SESSION_COOKIE, path: str | None = None
    ) -> None:
        if name == PREAUTH_CSRF_COOKIE:
            path = "/"
        elif path is None:
            path = "/admin"
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
            request=request, name=template, context=context, status_code=status_code
        )

    def redirect_with(request: Request, url: str, message: dict[str, Any]) -> RedirectResponse:
        """Post/redirect/get with a message for the page the redirect lands on."""
        auth_store.set_flash(request.cookies.get(SESSION_COOKIE), message)
        return RedirectResponse(url, status_code=303)

    def channel_label(name: str) -> str:
        return next((a.capabilities.label for a in publisher.adapters if a.name == name), name)

    def delivery_message(text: str, results: dict[str, Any]) -> dict[str, Any]:
        prepared = sorted(
            channel_label(name) for name, result in results.items() if result.prepared
        )
        failed = sorted(
            channel_label(name) for name, result in results.items() if not result.success
        )
        if prepared:
            automatic_success = any(
                result.success and not result.prepared for result in results.values()
            )
            text = (
                "Published to automatic channels."
                if automatic_success
                else "No automatic channel was published."
            )
            text += f" Prepared for manual publishing on: {', '.join(prepared)}."
        if failed:
            return flash(
                f"{text} Some channels failed: {', '.join(failed)}.",
                "warning",
                [
                    "Failures that are safe to repeat are retried automatically. See the delivery list."
                ],
            )
        return flash(text)

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
        request: Request, actor: str | None, action: str, target: str | None = None, **details: Any
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
            if (
                adapter.name not in PLATFORMS
                and adapter.capabilities.custom_text
                and enabled.get(adapter.name, True)
            )
        ]

    def parse_channel_text(
        form: dict[str, str], announcement: Announcement | None = None
    ) -> dict[str, str]:
        title = form.get("title", announcement.title if announcement else "").strip()
        body = form.get("body", announcement.body if announcement else "").strip()
        tags = parse_tags(form) if "tags" in form else (announcement.tags if announcement else [])
        canonical_url = publisher.canonical_url_for(announcement) if announcement else ""
        generated = (
            {
                platform.identifier: generate_platform_text(
                    title=title,
                    body=body,
                    canonical_url=canonical_url,
                    tags=tags,
                    connect_url=publisher.connect_url,
                    platform=platform,
                )
                for platform in PLATFORM_CATALOG
            }
            if title and body
            else {}
        )
        saved = announcement.channel_text if announcement else {}
        names = [*PLATFORMS, *(adapter.name for adapter in custom_text_channels())]
        result = {}
        for name in names:
            text = form.get(f"channel_text:{name}", "").strip()
            if text and (text != generated.get(name) or text == saved.get(name)):
                result[name] = text
        return result

    def editor_page(
        session: dict[str, Any],
        announcement: Any,
        error: str | None,
        submitted_values: dict[str, str] | None = None,
    ) -> dict:
        saved = getattr(announcement, "channel_text", None) or {}
        submitted = submitted_values or (announcement if isinstance(announcement, dict) else {})
        title = submitted.get("title", getattr(announcement, "title", "")).strip()
        body = submitted.get("body", getattr(announcement, "body", "")).strip()
        if "tags" in submitted:
            tags = parse_tags(submitted)
        else:
            tags = getattr(announcement, "tags", [])
        image = getattr(announcement, "image", None)
        status = getattr(getattr(announcement, "status", None), "value", None)
        image_editable = (
            status is None or status == "draft" or (status == "scheduled" and not review_required())
        )
        image_alt = submitted.get("image_alt", image.alt if image else "")
        canonical_url = (
            publisher.canonical_url_for(announcement)
            if isinstance(announcement, Announcement)
            else ""
        )
        generated = (
            {
                platform.identifier: generate_platform_text(
                    title=title,
                    body=body,
                    canonical_url=canonical_url,
                    tags=tags,
                    connect_url=publisher.connect_url,
                    platform=platform,
                )
                for platform in PLATFORM_CATALOG
            }
            if title and body
            else {}
        )

        def platform_channel(platform):
            name = platform.identifier
            value = submitted.get(f"channel_text:{name}", saved.get(name, generated.get(name, "")))
            return {
                "name": name,
                "label": platform.name,
                "limit": (
                    platform.capabilities.max_text_length
                    if platform.character_limit_verified
                    else None
                ),
                "limit_verified": platform.character_limit_verified,
                "value": value,
                "is_generated": name not in saved and value == generated.get(name),
            }

        channels = [platform_channel(platform) for platform in PLATFORM_CATALOG]
        channels.extend(
            {
                "name": adapter.name,
                "label": adapter.capabilities.label,
                "limit": adapter.capabilities.max_text_length,
                "limit_verified": True,
                "value": submitted.get(f"channel_text:{adapter.name}", saved.get(adapter.name, "")),
                "is_generated": False,
            }
            for adapter in custom_text_channels()
        )
        return page(
            session,
            announcement=announcement,
            error=error,
            custom_channels=channels,
            form_values={
                "title": title,
                "body": body,
                "author": submitted.get("author", getattr(announcement, "author", "")) or "",
                "tags": tags,
                "image_alt": image_alt,
                "remove_image": submitted.get("remove_image") == "true",
            },
            image=image,
            image_editable=image_editable,
        )

    # The community's own fediverse account: public, unauthenticated endpoints.

    def community_name() -> str:
        try:
            return load_config(config_path)["project"]["name"]
        except (ConfigError, KeyError, OSError):
            return "Community"

    register_activitypub(app, storage=storage, publisher=publisher, community_name=community_name)

    TEMPLATE_ID = re.compile(r"^tpl_[A-Za-z0-9_-]{8,40}$")

    def template_or_404(template_id: str) -> dict:
        template = storage.get_template(template_id) if TEMPLATE_ID.fullmatch(template_id) else None
        if template is None:
            raise HTTPException(status_code=404, detail="Template not found.")
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

    register_subscriptions(
        app,
        audit=audit,
        checked_form=checked_form,
        client_host=client_host,
        community_name=community_name,
        config_path=config_path,
        page=page,
        preauth_csrf_or_forbidden=preauth_csrf_or_forbidden,
        preauth_csrf_token=preauth_csrf_token,
        publisher=publisher,
        redirect_with=redirect_with,
        render=render,
        require=require,
        set_cookie=set_cookie,
        storage=storage,
        submitted_form=submitted_form,
    )

    register_templates(
        app,
        announcement_or_404=announcement_or_404,
        as_editor_values=as_editor_values,
        audit=audit,
        checked_form=checked_form,
        editor_page=editor_page,
        page=page,
        parse_channel_text=parse_channel_text,
        parse_tags=parse_tags,
        redirect_with=redirect_with,
        render=render,
        require=require,
        storage=storage,
        template_or_404=template_or_404,
    )

    register_announcements(
        app,
        announcement_or_404=announcement_or_404,
        as_editor_values=as_editor_values,
        audit=audit,
        auth_store=auth_store,
        checked_form=checked_form,
        delivery_message=delivery_message,
        editor_page=editor_page,
        page=page,
        parse_channel_text=parse_channel_text,
        parse_tags=parse_tags,
        publisher=publisher,
        redirect_with=redirect_with,
        render=render,
        require=require,
        require_approved=require_approved,
        review_required=review_required,
        storage=storage,
        template_or_404=template_or_404,
    )

    register_settings(
        app,
        agent=agent,
        audit=audit,
        checked_form=checked_form,
        config_path=config_path,
        configured_channel_links=configured_channel_links,
        csrf_or_forbidden=csrf_or_forbidden,
        page=page,
        publisher=publisher,
        redirect_with=redirect_with,
        render=render,
        require=require,
        review_required=review_required,
        storage=storage,
    )

    register_channels(
        app,
        audit=audit,
        checked_form=checked_form,
        config_path=config_path,
        page=page,
        publisher=publisher,
        redirect_with=redirect_with,
        render=render,
        require=require,
        storage=storage,
    )

    register_mirroring(
        app,
        audit=audit,
        checked_form=checked_form,
        mirror=mirror,
        page=page,
        publisher=publisher,
        redirect_with=redirect_with,
        render=render,
        require=require,
    )

    register_matrix_users(
        app,
        agent=agent,
        audit=audit,
        checked_form=checked_form,
        page=page,
        render=render,
        require=require,
    )

    register_safemode(
        app,
        agent=agent,
        audit=audit,
        checked_form=checked_form,
        page=page,
        render=render,
        require=require,
    )

    register_bridges(
        app,
        agent=agent,
        audit=audit,
        checked_form=checked_form,
        config_path=config_path,
        page=page,
        publisher=publisher,
        render=render,
        require=require,
        storage=storage,
    )

    register_vikunja_users(
        app,
        agent=agent,
        audit=audit,
        checked_form=checked_form,
        page=page,
        render=render,
        require=require,
    )

    register_accounts(
        app,
        audit=audit,
        auth_store=auth_store,
        checked_form=checked_form,
        page=page,
        redirect_with=redirect_with,
        render=render,
        require=require,
    )

    register_system(
        app,
        agent=agent,
        audit=audit,
        backups_page=backups_page,
        checked_form=checked_form,
        page=page,
        redirect_with=redirect_with,
        render=render,
        require=require,
    )

    register_services(
        app,
        agent=agent,
        audit=audit,
        checked_form=checked_form,
        page=page,
        publisher=publisher,
        redirect_with=redirect_with,
        render=render,
        require=require,
    )

    register_backups(
        app,
        agent=agent,
        audit=audit,
        backups_page=backups_page,
        checked_form=checked_form,
        config_path=config_path,
        redirect_with=redirect_with,
        require=require,
        storage=storage,
    )

    register_continuity(
        app,
        audit=audit,
        auth_store=auth_store,
        checked_form=checked_form,
        config_path=config_path,
        continuity_service=continuity_service,
        page=page,
        publisher=publisher,
        redirect_with=redirect_with,
        render=render,
        require=require,
        storage=storage,
    )

    register_dashboard(
        app,
        agent=agent,
        channel_label=channel_label,
        page=page,
        publisher=publisher,
        render=render,
        require=require,
        setup_steps=setup_steps,
        storage=storage,
    )

    register_auth(
        app,
        audit=audit,
        auth_store=auth_store,
        authenticated=authenticated,
        client_host=client_host,
        csrf_or_forbidden=csrf_or_forbidden,
        login_redirect=login_redirect,
        preauth_csrf_or_forbidden=preauth_csrf_or_forbidden,
        preauth_csrf_token=preauth_csrf_token,
        render=render,
        set_cookie=set_cookie,
        setup_token=setup_token,
        submitted_form=submitted_form,
    )

    return app
