"""WhatsApp bridge setup pages: linking the account and finding the group, from the console."""

from __future__ import annotations

import base64
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from community_stack.admin.common import flash
from community_stack.agent.client import AgentRefused, AgentUnavailable
from community_stack.config import ConfigError, load_config

QR_TTL_SECONDS = 180
QR_LIMIT = 8
_qr_images: dict[str, tuple[float, str, bytes]] = {}


def _stash_qr(data_url: str) -> str | None:
    """Keep a linking QR image in memory for a few minutes; it is never written to disk."""
    header, _, payload = data_url.partition(",")
    media_type = header.removeprefix("data:").removesuffix(";base64")
    if not header.endswith(";base64") or not media_type.startswith("image/"):
        return None
    now = time.monotonic()
    for key in [key for key, (expires, _, _) in _qr_images.items() if expires < now]:
        del _qr_images[key]
    while len(_qr_images) >= QR_LIMIT:
        del _qr_images[next(iter(_qr_images))]
    image_id = secrets.token_urlsafe(16)
    _qr_images[image_id] = (now + QR_TTL_SECONDS, media_type, base64.b64decode(payload))
    return image_id


def register_whatsapp(
    app: FastAPI,
    *,
    agent: Any,
    audit: Any,
    checked_form: Any,
    config_path: Path | str,
    page: Any,
    publisher: Any,
    render: Any,
    require: Any,
    storage: Any,
) -> None:
    async def whatsapp_page(
        request: Request,
        session: dict[str, Any],
        outcome: dict[str, Any] | None = None,
        replies: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        try:
            config = load_config(config_path) if Path(config_path).is_file() else {}
        except ConfigError:
            config = {}
        bridge, error, rooms, stale_portals = None, None, [], []
        if agent.available():
            try:
                bridge = await run_in_threadpool(
                    agent.call, "bridge.whatsapp.status", session["username"]
                )
                stale_portals = (
                    await run_in_threadpool(
                        agent.call, "bridge.whatsapp.stale_portals", session["username"]
                    )
                )["portals"]
                if bridge.get("command_chat_created"):
                    rooms = (
                        await run_in_threadpool(
                            agent.call, "bridge.whatsapp.rooms", session["username"]
                        )
                    )["rooms"]
            except (AgentUnavailable, AgentRefused) as exc:
                error = str(exc)
        return render(
            request,
            "whatsapp.html",
            page(
                session,
                agent_available=agent.available(),
                bridge=bridge,
                bridge_error=error,
                rooms=rooms,
                stale_portals=stale_portals,
                bridge_enabled=publisher.whatsapp_bridge_enabled,
                matrix_connected=publisher.matrix_auth is not None,
                channel_connected="whatsapp" in storage.list_connections(),
                matrix_domain=config.get("domain", {}).get("matrix", "your Matrix server"),
                replies=replies,
                **({"flash": outcome} if outcome else {}),
            ),
            status_code=status_code,
        )

    @app.get("/admin/whatsapp/qr/{image_id}")
    async def whatsapp_qr(request: Request, image_id: str) -> Response:
        require(request, "settings")
        entry = _qr_images.get(image_id)
        if entry is None or entry[0] < time.monotonic():
            return Response(status_code=404)
        return Response(
            content=entry[2],
            media_type=entry[1],
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/admin/whatsapp")
    async def whatsapp_setup(request: Request) -> Response:
        return await whatsapp_page(request, require(request, "settings"))

    @app.post("/admin/whatsapp/room-relay")
    async def whatsapp_room_relay(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        room_id = form.get("room_id", "")
        try:
            result = await run_in_threadpool(
                agent.call, "bridge.whatsapp.room_relay", session["username"], {"room_id": room_id}
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(
                request,
                session["username"],
                "bridge.whatsapp.room_relay",
                room_id,
                outcome="failed",
            )
            return await whatsapp_page(
                request, session, flash(f"WhatsApp relay: {exc}", "error"), status_code=400
            )
        audit(
            request, session["username"], "bridge.whatsapp.room_relay", room_id, outcome="succeeded"
        )
        return await whatsapp_page(
            request,
            session,
            flash("WhatsApp relay: the bridge answered."),
            replies={"command": "set-relay", **result},
        )

    @app.post("/admin/whatsapp/room-invite")
    async def whatsapp_room_invite(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        room_id = form.get("room_id", "")
        user_id = form.get("user_id", "").strip()
        try:
            result = await run_in_threadpool(
                agent.call,
                "bridge.whatsapp.room_invite",
                session["username"],
                {"room_id": room_id, "user_id": user_id},
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(
                request,
                session["username"],
                "bridge.whatsapp.room_invite",
                "WhatsApp portal room",
                outcome="failed",
            )
            return await whatsapp_page(
                request, session, flash(f"WhatsApp room invite: {exc}", "error"), status_code=400
            )
        audit(
            request,
            session["username"],
            "bridge.whatsapp.room_invite",
            room_id,
            outcome="succeeded",
            user_id=result["user_id"],
        )
        return await whatsapp_page(
            request,
            session,
            flash(f"Invited {result['user_id']} to the WhatsApp portal room."),
        )

    @app.post("/admin/whatsapp/command")
    async def whatsapp_command(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        command = form.get("command", "")
        try:
            result = await run_in_threadpool(
                agent.call,
                "bridge.whatsapp.command",
                session["username"],
                {"command": command},
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(
                request, session["username"], "bridge.whatsapp.command", command, outcome="failed"
            )
            return await whatsapp_page(
                request, session, flash(f"WhatsApp bridge: {exc}", "error"), status_code=400
            )
        # Replies can contain a linking QR code: they are shown to this administrator only,
        # and only the command name is written to the activity log.
        for reply in result.get("replies", []):
            if reply.get("image"):
                image_id = _stash_qr(reply["image"])
                reply["image"] = f"/admin/whatsapp/qr/{image_id}" if image_id else None
        audit(request, session["username"], "bridge.whatsapp.command", command, outcome="succeeded")
        return await whatsapp_page(
            request,
            session,
            flash(f"WhatsApp bridge: sent '{command}'."),
            replies=result,
        )

    @app.post("/admin/whatsapp/group-add")
    async def whatsapp_group_add(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        try:
            result = await run_in_threadpool(
                agent.call,
                "bridge.whatsapp.group_add",
                session["username"],
                {"invite_link": form.get("invite_link", "").strip()},
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(
                request,
                session["username"],
                "bridge.whatsapp.group_add",
                "WhatsApp invite link",
                outcome="failed",
            )
            return await whatsapp_page(
                request, session, flash(f"WhatsApp group setup: {exc}", "error"), status_code=400
            )
        audit(
            request,
            session["username"],
            "bridge.whatsapp.group_add",
            result.get("group_id", "WhatsApp group"),
            outcome="succeeded",
        )
        return await whatsapp_page(
            request,
            session,
            flash(f"WhatsApp group '{result['name']}' was added and joined."),
        )

    @app.post("/admin/whatsapp/group-disallow")
    async def whatsapp_group_disallow(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        try:
            await run_in_threadpool(
                agent.call,
                "bridge.whatsapp.group_disallow",
                session["username"],
                {"group_id": form.get("group_id", "")},
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(
                request,
                session["username"],
                "bridge.whatsapp.group_disallow",
                "WhatsApp group permission",
                outcome="failed",
            )
            return await whatsapp_page(
                request,
                session,
                flash(f"WhatsApp group permission: {exc}", "error"),
                status_code=400,
            )
        audit(
            request,
            session["username"],
            "bridge.whatsapp.group_disallow",
            "WhatsApp group permission",
            outcome="succeeded",
        )
        return await whatsapp_page(
            request, session, flash("WhatsApp group removed from the allowlist.")
        )

    @app.post("/admin/whatsapp/orphan-portal-remove")
    async def whatsapp_orphan_portal_remove(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        try:
            await run_in_threadpool(
                agent.call,
                "bridge.whatsapp.orphan_portal_remove",
                session["username"],
                {
                    "group_id": form.get("group_id", ""),
                    "confirmation": form.get("confirmation", ""),
                },
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(
                request,
                session["username"],
                "bridge.whatsapp.orphan_portal_remove",
                "saved WhatsApp group record",
                outcome="failed",
            )
            return await whatsapp_page(
                request, session, flash(f"WhatsApp group removal: {exc}", "error"), status_code=400
            )
        audit(
            request,
            session["username"],
            "bridge.whatsapp.orphan_portal_remove",
            "saved WhatsApp group record",
            outcome="succeeded",
        )
        return await whatsapp_page(request, session, flash("Saved WhatsApp group record removed."))

    @app.post("/admin/whatsapp/stale-portal-reset")
    async def whatsapp_stale_portal_reset(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        try:
            await run_in_threadpool(
                agent.call,
                "bridge.whatsapp.portal_mapping_reset",
                session["username"],
                {
                    "group_id": form.get("group_id", ""),
                    "confirmation": form.get("confirmation", ""),
                },
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(
                request,
                session["username"],
                "bridge.whatsapp.portal_mapping_reset",
                "stale WhatsApp portal mapping",
                outcome="failed",
            )
            return await whatsapp_page(
                request,
                session,
                flash(f"WhatsApp portal recovery: {exc}", "error"),
                status_code=400,
            )
        audit(
            request,
            session["username"],
            "bridge.whatsapp.portal_mapping_reset",
            "stale WhatsApp portal mapping",
            outcome="succeeded",
        )
        return await whatsapp_page(
            request,
            session,
            flash(
                "The stale Matrix room reference was cleared. Start the bridge and send a new "
                "message in the WhatsApp group if its portal does not reappear."
            ),
        )

    @app.post("/admin/whatsapp/portal-remove")
    async def whatsapp_portal_remove(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        room_id = form.get("room_id", "")
        try:
            await run_in_threadpool(
                agent.call,
                "bridge.whatsapp.portal_remove",
                session["username"],
                {
                    "room_id": room_id,
                    "confirmation": form.get("confirmation", ""),
                },
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(
                request,
                session["username"],
                "bridge.whatsapp.portal_remove",
                "bridged WhatsApp room",
                outcome="failed",
            )
            return await whatsapp_page(
                request, session, flash(f"WhatsApp portal removal: {exc}", "error"), status_code=400
            )
        audit(
            request,
            session["username"],
            "bridge.whatsapp.portal_remove",
            "bridged WhatsApp room",
            outcome="succeeded",
        )
        return await whatsapp_page(
            request, session, flash("WhatsApp group portal and its Matrix room were removed.")
        )
