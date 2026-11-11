"""Console pages for the chat bridges: one overview, and one page per bridge.

Every supported bridge uses the same page and the same agent operations: linking the
account, linking chats to new or existing Matrix rooms, relay, invitations, and repairing
links when something breaks. What differs between bridges (QR linking, WhatsApp invite
links, announcements forwarding) comes from the catalogue entry, not from separate pages.
"""

from __future__ import annotations

import base64
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from community_stack.admin.common import flash
from community_stack.agent.client import AgentRefused, AgentUnavailable
from community_stack.bridges.catalogue import (
    CATALOG,
    LOGIN_COMMAND,
    BridgeSpec,
    supported,
)

FLOW = re.compile(r"[a-z][a-z0-9-]{0,31}")
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


def _room_label(result: dict[str, Any]) -> str:
    return result.get("name") or result.get("room_id") or "the room"


def _relay_note(result: dict[str, Any]) -> str:
    if result.get("relay"):
        return " Relay is on, so every member's messages reach the chat."
    return " Relay could not be turned on; use “Relay on” in the room's row."


# One entry per console action: the agent operation, the form fields it takes, the target
# written to the activity log, and the message shown when it succeeds.
ACTIONS: dict[str, tuple[str, tuple[str, ...], str, Callable[[BridgeSpec, dict], str]]] = {
    "restart": ("restart", (), "", lambda spec, r: f"The {spec.name} bridge was restarted."),
    "command-chat-reset": (
        "command_chat_reset",
        (),
        "",
        lambda spec, r: f"A new command chat with the {spec.name} bot was opened.",
    ),
    "chat-allow": (
        "chat_allow",
        ("chat_id",),
        "chat_id",
        lambda spec, r: (
            f"The bridge will create a room for this {spec.chat_noun} with its next message."
        ),
    ),
    "chat-disallow": (
        "chat_disallow",
        ("chat_id",),
        "chat_id",
        lambda spec, r: f"The bridge will no longer create a room for this {spec.chat_noun}.",
    ),
    "chat-forget": (
        "chat_forget",
        ("chat_id", "confirmation"),
        "chat_id",
        lambda spec, r: f"The {spec.chat_noun} record was forgotten.",
    ),
    "room-attach": (
        "room_attach",
        ("chat_id", "room_id", "move"),
        "room_id",
        lambda spec, r: (
            f"The {spec.chat_noun} is now linked to the room."
            + (" Its previous room was kept for its Matrix members." if r.get("moved_from") else "")
            + (
                " The stack admin was made a room admin so it could add the bridge bot."
                if r.get("admin_promoted")
                else ""
            )
            + _relay_note(r)
        ),
    ),
    "room-create": (
        "room_create",
        ("chat_id",),
        "chat_id",
        lambda spec, r: f"A new room was created for the {spec.chat_noun}." + _relay_note(r),
    ),
    "room-detach": (
        "room_detach",
        ("room_id", "confirmation"),
        "room_id",
        lambda spec, r: (
            f"The room was unlinked and kept for its Matrix members. The {spec.chat_noun} "
            "appears under Unlinked chats, ready to be linked again."
        ),
    ),
    "room-delete": (
        "room_delete",
        ("room_id", "confirmation"),
        "room_id",
        lambda spec, r: "The room was deleted.",
    ),
    "room-sync": (
        "room_sync",
        ("room_id",),
        "room_id",
        lambda spec, r: f"Asked the bridge to refresh the room from {spec.name}.",
    ),
    "room-relay": (
        "room_relay",
        ("room_id", "enabled"),
        "room_id",
        lambda spec, r: "Relay updated." if r.get("ok") else "The bridge answered; see its reply.",
    ),
    "room-invite": (
        "room_invite",
        ("room_id", "user_id"),
        "room_id",
        lambda spec, r: f"Invited {r['user_id']}. They still need to accept the invitation.",
    ),
    "room-leave": (
        "room_leave",
        ("room_id", "confirmation"),
        "room_id",
        lambda spec, r: "The bridge bot left the room. The room itself was kept.",
    ),
    "link-repair": (
        "link_repair",
        ("chat_id", "confirmation"),
        "chat_id",
        lambda spec, r: (
            f"The broken link was forgotten. Link the {spec.chat_noun} to a room again from "
            "Unlinked chats."
        ),
    ),
    "group-add": (
        "group_add",
        ("invite_link", "room_id"),
        "",
        lambda spec, r: (
            f"Joined {r['name']}."
            + (
                f" It is linked to its room.{_relay_note(r)}"
                if r.get("room_id")
                else f" {r.get('note', '')}"
            )
        ),
    ),
}


def register_bridges(
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
    def matrix_domain() -> str:
        """The Matrix server name, for "via" in Element links. Only a hint: read leniently."""
        try:
            config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return ""
        domain = config.get("domain") if isinstance(config, dict) else None
        matrix = domain.get("matrix") if isinstance(domain, dict) else None
        return matrix if isinstance(matrix, str) else ""

    def spec_or_404(key: str) -> BridgeSpec:
        spec = next((item for item in supported() if item.key == key), None)
        if spec is None:
            raise HTTPException(status_code=404, detail="Unknown bridge.")
        return spec

    async def call(session: dict[str, Any], operation: str, params: dict | None = None):
        return await run_in_threadpool(agent.call, operation, session["username"], params or {})

    async def bridge_page(
        request: Request,
        session: dict[str, Any],
        spec: BridgeSpec,
        outcome: dict[str, Any] | None = None,
        replies: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        bridge, overview, candidates, error = None, None, [], None
        if agent.available():
            try:
                bridge = await call(session, f"bridge.{spec.key}.status")
                if bridge.get("enabled") and bridge.get("registered_with_synapse"):
                    overview = await call(session, f"bridge.{spec.key}.overview")
                    candidates = (await call(session, f"bridge.{spec.key}.matrix_rooms"))["rooms"]
            except (AgentUnavailable, AgentRefused) as exc:
                error = str(exc)
        problems = 0
        if bridge:
            problems += sum(not check["ok"] for check in bridge["checks"])
        if overview:
            problems += len(overview["broken"]) + len(overview["leftovers"])
        announce_rooms = {
            destination["room_id"]
            for destination in storage.list_bridged_destinations()
            if destination["bridge"] == spec.key
        }
        return render(
            request,
            "bridge.html",
            page(
                session,
                spec=spec,
                agent_available=agent.available(),
                bridge=bridge,
                bridge_error=error,
                overview=overview,
                candidates=candidates,
                problems=problems,
                matrix_domain=matrix_domain(),
                announcements_ready=spec.announcements and spec.key in publisher.enabled_bridges,
                announce_rooms=announce_rooms,
                replies=replies,
                **({"flash": outcome} if outcome else {}),
            ),
            status_code=status_code,
        )

    @app.get("/admin/bridges")
    async def bridges_index(request: Request) -> Response:
        session = require(request, "system")
        rows, error = [], None
        for spec in CATALOG.values():
            row: dict[str, Any] = {"spec": spec, "status": None}
            if spec.status == "supported" and agent.available():
                try:
                    row["status"] = await call(session, f"bridge.{spec.key}.status")
                except (AgentUnavailable, AgentRefused) as exc:
                    error = str(exc)
            rows.append(row)
        return render(
            request,
            "bridges.html",
            page(session, rows=rows, agent_available=agent.available(), bridge_error=error),
        )

    @app.get("/admin/whatsapp")
    async def whatsapp_moved(request: Request) -> Response:
        require(request, "system")
        return RedirectResponse("/admin/bridges/whatsapp", status_code=308)

    @app.get("/admin/bridges/{key}")
    async def bridge_setup(request: Request, key: str) -> Response:
        spec = spec_or_404(key)
        return await bridge_page(request, require(request, "system"), spec)

    @app.get("/admin/bridges/{key}/qr/{image_id}")
    async def bridge_qr(request: Request, key: str, image_id: str) -> Response:
        spec_or_404(key)
        require(request, "system")
        entry = _qr_images.get(image_id)
        if entry is None or entry[0] < time.monotonic():
            return Response(status_code=404)
        return Response(
            content=entry[2], media_type=entry[1], headers={"Cache-Control": "no-store"}
        )

    @app.post("/admin/bridges/{key}/command")
    async def bridge_command(request: Request, key: str) -> Response:
        spec = spec_or_404(key)
        session, form = await checked_form(request, "system")
        command = form.get("command", "").strip()
        flow = form.get("flow", "").strip()
        if command == LOGIN_COMMAND and flow:
            if not FLOW.fullmatch(flow):
                return await bridge_page(
                    request,
                    session,
                    spec,
                    flash("Enter a login option the bot listed.", "error"),
                    status_code=400,
                )
            command = f"{LOGIN_COMMAND} {flow}"
        operation = f"bridge.{spec.key}.command"
        try:
            result = await call(session, operation, {"command": command})
        except (AgentUnavailable, AgentRefused) as exc:
            audit(request, session["username"], operation, command, outcome="failed")
            return await bridge_page(
                request, session, spec, flash(f"{spec.name}: {exc}", "error"), status_code=400
            )
        # Replies can contain a linking QR code: they are shown to this administrator only,
        # and only the command name is written to the activity log.
        for reply in result.get("replies", []):
            if reply.get("image"):
                image_id = _stash_qr(reply["image"])
                reply["image"] = f"/admin/bridges/{spec.key}/qr/{image_id}" if image_id else None
        audit(request, session["username"], operation, command, outcome="succeeded")
        return await bridge_page(
            request, session, spec, flash(f"{spec.name}: sent '{command}'."), replies=result
        )

    @app.post("/admin/bridges/{key}/{action}")
    async def bridge_action(request: Request, key: str, action: str) -> Response:
        spec = spec_or_404(key)
        if action not in ACTIONS or (action == "group-add" and not spec.invite_links):
            raise HTTPException(status_code=404, detail="Unknown action.")
        operation_name, fields, target_field, message = ACTIONS[action]
        session, form = await checked_form(request, "system")
        params = {name: form.get(name, "").strip() for name in fields if form.get(name, "").strip()}
        operation = f"bridge.{spec.key}.{operation_name}"
        # Invite links are bearer credentials: never written to the activity log.
        target = params.get(target_field, "") if target_field else spec.name
        try:
            result = await call(session, operation, params)
        except (AgentUnavailable, AgentRefused) as exc:
            audit(request, session["username"], operation, target, outcome="failed")
            return await bridge_page(
                request, session, spec, flash(f"{spec.name}: {exc}", "error"), status_code=400
            )
        audit(request, session["username"], operation, target, outcome="succeeded")
        replies = (
            {"command": operation_name, **result} if result.get("replies") is not None else None
        )
        return await bridge_page(
            request, session, spec, flash(message(spec, result)), replies=replies
        )
