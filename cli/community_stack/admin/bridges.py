"""Console pages for the bridges that share the generic bridge operations.

WhatsApp keeps its own page, which has the walk-through written for it. Every other
supported bridge uses this page: status, linking, room discovery and relay, all through
the same allowlisted operations.
"""

from __future__ import annotations

import re
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from community_stack.admin import whatsapp as console
from community_stack.admin.common import flash
from community_stack.admin.whatsapp import _stash_qr
from community_stack.agent.client import AgentRefused, AgentUnavailable
from community_stack.bridges.catalogue import LOGIN_COMMAND, MESSENGER, SIGNAL, BridgeSpec

GENERIC_SPECS: tuple[BridgeSpec, ...] = (SIGNAL, MESSENGER)
FLOW = re.compile(r"[a-z][a-z0-9-]{0,31}")


def register_bridges(
    app: FastAPI,
    *,
    agent: Any,
    audit: Any,
    checked_form: Any,
    page: Any,
    render: Any,
    require: Any,
) -> None:
    async def bridge_page(
        request: Request,
        session: dict[str, Any],
        spec: BridgeSpec,
        outcome: dict[str, Any] | None = None,
        replies: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        bridge, rooms, error = None, [], None
        if agent.available():
            try:
                bridge = await run_in_threadpool(
                    agent.call, f"bridge.{spec.key}.status", session["username"]
                )
                if bridge.get("command_chat_created"):
                    rooms = (
                        await run_in_threadpool(
                            agent.call, f"bridge.{spec.key}.rooms", session["username"]
                        )
                    )["rooms"]
            except (AgentUnavailable, AgentRefused) as exc:
                error = str(exc)
        return render(
            request,
            "bridge.html",
            page(
                session,
                spec=spec,
                agent_available=agent.available(),
                bridge=bridge,
                bridge_error=error,
                rooms=rooms,
                replies=replies,
                **({"flash": outcome} if outcome else {}),
            ),
            status_code=status_code,
        )

    def spec_or_404(key: str) -> BridgeSpec:
        spec = next((item for item in GENERIC_SPECS if item.key == key), None)
        if spec is None:
            raise HTTPException(status_code=404, detail="Unknown bridge.")
        return spec

    @app.get("/admin/bridges/{key}")
    async def bridge_setup(request: Request, key: str) -> Response:
        spec = spec_or_404(key)
        return await bridge_page(request, require(request, "settings"), spec)

    @app.get("/admin/bridges/{key}/qr/{image_id}")
    async def bridge_qr(request: Request, key: str, image_id: str) -> Response:
        spec_or_404(key)
        require(request, "settings")
        entry = console._qr_images.get(image_id)
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
        try:
            result = await run_in_threadpool(
                agent.call, f"bridge.{spec.key}.command", session["username"], {"command": command}
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(
                request,
                session["username"],
                f"bridge.{spec.key}.command",
                command,
                outcome="failed",
            )
            return await bridge_page(
                request, session, spec, flash(f"{spec.name}: {exc}", "error"), status_code=400
            )
        for reply in result.get("replies", []):
            if reply.get("image"):
                image_id = _stash_qr(reply["image"])
                reply["image"] = f"/admin/bridges/{spec.key}/qr/{image_id}" if image_id else None
        audit(
            request, session["username"], f"bridge.{spec.key}.command", command, outcome="succeeded"
        )
        return await bridge_page(
            request, session, spec, flash(f"{spec.name}: sent '{command}'."), replies=result
        )

    @app.post("/admin/bridges/{key}/room-relay")
    async def bridge_room_relay(request: Request, key: str) -> Response:
        spec = spec_or_404(key)
        session, form = await checked_form(request, "system")
        room_id = form.get("room_id", "")
        try:
            result = await run_in_threadpool(
                agent.call,
                f"bridge.{spec.key}.room_relay",
                session["username"],
                {"room_id": room_id},
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(
                request,
                session["username"],
                f"bridge.{spec.key}.room_relay",
                room_id,
                outcome="failed",
            )
            return await bridge_page(
                request, session, spec, flash(f"{spec.name} relay: {exc}", "error"), status_code=400
            )
        audit(
            request,
            session["username"],
            f"bridge.{spec.key}.room_relay",
            room_id,
            outcome="succeeded",
        )
        return await bridge_page(
            request,
            session,
            spec,
            flash(f"{spec.name} relay: the bridge answered."),
            replies={"command": "set-relay", **result},
        )

    @app.post("/admin/bridges/{key}/room-invite")
    async def bridge_room_invite(request: Request, key: str) -> Response:
        spec = spec_or_404(key)
        session, form = await checked_form(request, "system")
        room_id = form.get("room_id", "")
        user_id = form.get("user_id", "").strip()
        try:
            result = await run_in_threadpool(
                agent.call,
                f"bridge.{spec.key}.room_invite",
                session["username"],
                {"room_id": room_id, "user_id": user_id},
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(
                request,
                session["username"],
                f"bridge.{spec.key}.room_invite",
                "bridge portal room",
                outcome="failed",
            )
            return await bridge_page(
                request,
                session,
                spec,
                flash(f"{spec.name} room invite: {exc}", "error"),
                status_code=400,
            )
        audit(
            request,
            session["username"],
            f"bridge.{spec.key}.room_invite",
            room_id,
            outcome="succeeded",
            user_id=result["user_id"],
        )
        return await bridge_page(
            request,
            session,
            spec,
            flash(f"Invited {result['user_id']} to the {spec.name} room."),
        )
