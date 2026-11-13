"""Routes for the settings pages of the admin console."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile

from community_stack.admin.common import SOCIAL_ROWS, flash
from community_stack.admin.services import load_settings, settings_cards, timezone_names
from community_stack.admin.store import can
from community_stack.agent.client import AgentRefused, AgentUnavailable
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.community import SOCIAL_PLATFORMS
from community_stack.config import ConfigError, load_config
from community_stack.continuity.connect_page import MAX_LOGO_BYTES as MAX_PROFILE_IMAGE_BYTES
from community_stack.continuity.connect_page import ConnectError, store_profile_image


def register_settings(
    app: FastAPI,
    *,
    agent: Any,
    audit: Any,
    checked_form: Any,
    config_path: Any,
    configured_channel_links: Any,
    csrf_or_forbidden: Any,
    page: Any,
    publisher: Any,
    redirect_with: Any,
    render: Any,
    require: Any,
    review_required: Any,
    storage: Any,
) -> None:
    # Community profile (CR1.8). Read from the configuration; saved through the agent.
    community_labels = {
        "website": "Our website",
        "email": "Email updates",
        "matrix": "Matrix chat room",
        "activitypub": "Fediverse account",
        "rss": "Feeds (RSS, Atom and JSON Feed)",
    }

    def community_form_values(form: dict[str, str]) -> dict[str, Any]:
        social = []
        for index in range(SOCIAL_ROWS):
            social.append(
                {
                    "platform": form.get(f"social_platform_{index}", "").strip(),
                    "url": form.get(f"social_url_{index}", "").strip(),
                    "handle": form.get(f"social_handle_{index}", "").strip(),
                }
            )
        return {
            "name": form.get("name", ""),
            "description": form.get("description", ""),
            "url": form.get("url", ""),
            "logo": form.get("logo", ""),
            "cover": form.get("cover", ""),
            "contact_email": form.get("contact_email", ""),
            "emergency_instructions": form.get("emergency_instructions", ""),
            "social": social,
            "independent": {
                key: form.get(f"independent_{key}") == "on" for key in community_labels
            },
        }

    def community_current_values() -> dict[str, Any]:
        try:
            raw = load_config(config_path).get("community") or {}
        except (ConfigError, OSError):
            raw = {}
        social = [dict(item) for item in raw.get("social", [])][:SOCIAL_ROWS]
        social += [{}] * (SOCIAL_ROWS - len(social))
        independent = raw.get("independent") or {}
        return {
            "name": raw.get("name", ""),
            "description": raw.get("description", ""),
            "url": raw.get("url", ""),
            "logo": raw.get("logo", ""),
            "cover": raw.get("cover", ""),
            "contact_email": (raw.get("contact") or {}).get("email", ""),
            "emergency_instructions": raw.get("emergency_instructions", ""),
            "social": social,
            "independent": {
                key: bool(independent.get(key, key in ("website", "rss")))
                for key in community_labels
            },
        }

    async def community_page(
        request: Request,
        session: dict[str, Any],
        values: dict[str, Any],
        error: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        social_rows = [
            {
                "platform": item.get("platform", ""),
                "url": item.get("url", ""),
                "handle": item.get("handle", ""),
            }
            for item in values["social"]
        ]
        # The time zone is a server setting, changed through the agent like other settings.
        cards, settings_error = [], None
        if can(session["role"], "system"):
            config, settings_error = await load_settings(agent, session["username"])
            cards = settings_cards("community", config)
        return render(
            request,
            "community.html",
            page(
                session,
                v={**values, "social": social_rows},
                social_rows=social_rows,
                platforms=list(SOCIAL_PLATFORMS.items()),
                channels=list(community_labels.items()),
                flash_error=error,
                settings_cards=cards,
                settings_error=settings_error,
                timezones=timezone_names() if cards else [],
            ),
            status_code=status_code,
        )

    @app.get("/admin/community")
    async def community_profile_page(request: Request) -> Response:
        session = require(request, "settings")
        return await community_page(request, session, community_current_values())

    @app.post("/admin/community")
    async def community_profile_save(request: Request) -> Response:
        session = require(request, "settings")
        raw_form = await request.form()
        form = {key: value for key, value in raw_form.items() if isinstance(value, str)}
        csrf_or_forbidden(request, session, form)
        values = community_form_values(form)
        image_directory = (
            Path(config_path).parent / "data" / "announcements" / "media" / "community"
        )
        try:
            for kind in ("logo", "cover"):
                upload = raw_form.get(f"{kind}_upload")
                if not isinstance(upload, StarletteUploadFile) or not upload.filename:
                    continue
                try:
                    data = await upload.read(MAX_PROFILE_IMAGE_BYTES + 1)
                finally:
                    await upload.close()
                if len(data) > MAX_PROFILE_IMAGE_BYTES:
                    raise ConnectError(f"The {kind} must be 2 MB or smaller.")
                filename, _path, _created = await run_in_threadpool(
                    store_profile_image, data, kind, image_directory
                )
                values[kind] = filename
        except ConnectError as exc:
            audit(request, session["username"], "community.updated", outcome="invalid_image")
            return await community_page(request, session, values, str(exc), 400)
        except OSError:
            audit(request, session["username"], "community.updated", outcome="image_read_failed")
            return await community_page(
                request, session, values, "The uploaded image could not be read or saved.", 400
            )
        try:
            await run_in_threadpool(
                agent.call, "community.apply", session["username"], {"community": values}
            )
        except AgentUnavailable:
            audit(request, session["username"], "community.updated", outcome="failed")
            return await community_page(
                request,
                session,
                values,
                "The control agent is not running, so nothing was saved.",
                503,
            )
        except AgentRefused as exc:
            audit(request, session["username"], "community.updated", outcome="refused")
            return await community_page(request, session, values, str(exc), 400)
        publisher.refresh_connect_page()
        audit(request, session["username"], "community.updated", outcome="succeeded")
        return redirect_with(
            request,
            "/admin/community",
            flash("Community profile saved. The contact page is updated."),
        )

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
