"""Channels page: WhatsApp and Signal groups that receive announcements through a bridge.

Choosing a group is one click. The console then makes sure the stack's announcer account
exists (the agent creates it the first time), turns relay on in the group's room so the
bridge forwards the announcer's messages, invites the announcer through the bridge bot,
joins it, and saves the room as a channel of its own.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from community_stack.admin.common import flash
from community_stack.agent.client import AgentRefused, AgentUnavailable
from community_stack.announcements.adapters.bridged_room import (
    INTERNAL_HOMESERVER,
    BridgedRoomAdapter,
    destination_name,
)
from community_stack.bridges.catalogue import BridgeSpec, supported
from community_stack.matrix_ids import ROOM_ID


def announcement_bridges() -> list[BridgeSpec]:
    return [spec for spec in supported() if spec.announcements]


def matrix_call(
    access_token: str, method: str, path: str, payload: dict | None = None
) -> tuple[int, dict]:
    """One client-API request as the announcer account."""
    client = BridgedRoomAdapter("announcer", "", INTERNAL_HOMESERVER, "!unused", access_token)
    return client._http_request(method, path, payload)


class BridgedChannelError(Exception):
    """A step of connecting a bridged group failed; the message is shown to people."""


def register_bridged_channels(
    app: FastAPI,
    *,
    agent: Any,
    audit: Any,
    checked_form: Any,
    publisher: Any,
    redirect_with: Any,
    storage: Any,
):
    """Add the bridged-group routes; return the coroutine that builds their page section."""

    async def call(session: dict[str, Any], operation: str, params: dict | None = None):
        return await run_in_threadpool(agent.call, operation, session["username"], params or {})

    def spec_or_404(key: str) -> BridgeSpec:
        spec = next((item for item in announcement_bridges() if item.key == key), None)
        if spec is None:
            raise HTTPException(status_code=404, detail="Unknown bridge.")
        return spec

    def destination_or_404(name: str) -> dict[str, str]:
        destination = next(
            (d for d in storage.list_bridged_destinations() if d["name"] == name), None
        )
        if destination is None:
            raise HTTPException(status_code=404, detail="Unknown chat group.")
        return destination

    async def linked_rooms(session: dict[str, Any], spec: BridgeSpec) -> list[dict[str, Any]]:
        return (await call(session, f"bridge.{spec.key}.overview"))["rooms"]

    async def bridge_section(session: dict[str, Any], spec: BridgeSpec) -> dict[str, Any]:
        saved = [d for d in storage.list_bridged_destinations() if d["bridge"] == spec.key]
        section: dict[str, Any] = {
            "spec": spec,
            "enabled": spec.key in publisher.enabled_bridges,
            "destinations": saved,
            "rooms": [],
            "error": None,
        }
        if not section["enabled"]:
            return section
        if not agent.available():
            section["error"] = (
                "The system agent is not reachable, so linked groups cannot be listed."
            )
            return section
        try:
            rooms = await linked_rooms(session, spec)
        except (AgentUnavailable, AgentRefused) as exc:
            section["error"] = str(exc)
            return section
        chosen = {d["room_id"]: d for d in saved}
        section["rooms"] = [{**room, "destination": chosen.get(room["room_id"])} for room in rooms]
        linked = {room["room_id"] for room in rooms}
        for destination in saved:
            destination["linked"] = destination["room_id"] in linked
        return section

    async def bridged_sections(session: dict[str, Any]) -> list[dict[str, Any]]:
        return list(
            await asyncio.gather(
                *(bridge_section(session, spec) for spec in announcement_bridges())
            )
        )

    async def announcer(session: dict[str, Any]) -> dict[str, str]:
        """The announcer's credentials, asking the agent for new ones if they no longer work."""
        saved = storage.get_announcer()
        if saved:
            status, account = await run_in_threadpool(
                matrix_call, saved["access_token"], "GET", "/_matrix/client/v3/account/whoami"
            )
            if status == 200 and account.get("user_id") == saved["user_id"]:
                return saved
            if status not in (401, 403):
                raise BridgedChannelError(
                    f"Matrix did not answer for the announcer account ({account.get('error', status)})."
                )
        created = await call(session, "matrix.announcer.session")
        storage.save_announcer(created["user_id"], created["access_token"])
        return storage.get_announcer()

    async def connect(session: dict[str, Any], spec: BridgeSpec, room_id: str) -> dict[str, Any]:
        if spec.key not in publisher.enabled_bridges:
            raise BridgedChannelError(f"Turn the {spec.name} bridge on under Services first.")
        room = next((r for r in await linked_rooms(session, spec) if r["room_id"] == room_id), None)
        if room is None:
            raise BridgedChannelError(
                f"That room is no longer linked to a {spec.name} {spec.chat_noun}. Reload the page."
            )
        if room["encrypted"]:
            raise BridgedChannelError(
                "This room is end-to-end encrypted; the announcer cannot post in it."
            )
        account = await announcer(session)
        if not room["relay"]:
            relay = await call(
                session, f"bridge.{spec.key}.room_relay", {"room_id": room_id, "enabled": "on"}
            )
            if not relay.get("ok"):
                raise BridgedChannelError(
                    relay.get("note")
                    or f"The {spec.name} bridge did not turn relay on. Check that an account "
                    "is linked on the bridge page."
                )
        status, joined = await run_in_threadpool(
            matrix_call, account["access_token"], "GET", "/_matrix/client/v3/joined_rooms"
        )
        if status != 200:
            raise BridgedChannelError(
                f"Could not list the announcer's rooms ({joined.get('error', status)})."
            )
        if room_id not in joined.get("joined_rooms", []):
            await call(
                session,
                f"bridge.{spec.key}.room_invite",
                {"room_id": room_id, "user_id": account["user_id"]},
            )
            status, reply = await run_in_threadpool(
                matrix_call,
                account["access_token"],
                "POST",
                f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/join",
                {},
            )
            if status != 200:
                raise BridgedChannelError(
                    f"The announcer could not join the room ({reply.get('error', status)})."
                )
        return room

    def back_to(form: dict[str, str], spec: BridgeSpec) -> str:
        if form.get("back") == "bridge":
            return f"/admin/bridges/{spec.key}#rooms"
        return f"/admin/channels#bridged-{spec.key}"

    @app.post("/admin/channels/bridged/{key}/add")
    async def add_bridged_group(request: Request, key: str) -> Response:
        spec = spec_or_404(key)
        session, form = await checked_form(request, "settings")
        room_id = form.get("room_id", "")
        if not ROOM_ID.fullmatch(room_id):
            raise HTTPException(status_code=400, detail="That is not a valid room ID.")
        try:
            room = await connect(session, spec, room_id)
        except (BridgedChannelError, AgentUnavailable, AgentRefused) as exc:
            audit(
                request,
                session["username"],
                "channel.connected",
                spec.key,
                room_id=room_id,
                outcome="failed",
            )
            return redirect_with(
                request, back_to(form, spec), flash(f"{spec.name} announcements: {exc}", "error")
            )
        name = destination_name(spec.key, room_id)
        added = storage.save_bridged_destination(
            name, spec.key, room_id, room["name"], session["username"]
        )
        publisher.reload_connections()
        audit(
            request,
            session["username"],
            "channel.connected" if added else "channel.updated",
            name,
            bridge=spec.key,
            room_id=room_id,
        )
        relay_note = "" if room["relay"] else " Relay was turned on in its room."
        return redirect_with(
            request,
            back_to(form, spec),
            flash(
                f"Announcements will be posted in the {spec.name} {spec.chat_noun} "
                f"{room['name']}.{relay_note} Turn it on or off under Publishing settings."
            ),
        )

    @app.post("/admin/channels/bridged/{name}/test")
    async def test_bridged_group(request: Request, name: str) -> Response:
        session, _ = await checked_form(request, "settings")
        destination = destination_or_404(name)
        spec = spec_or_404(destination["bridge"])
        adapter = next((a for a in publisher.adapters if a.name == name), None)
        if adapter is None:
            message = flash(
                f"{spec.name}: this group cannot be used while the {spec.name} bridge is off "
                "or the announcer account is missing. Choose it again to repair it.",
                "error",
            )
        else:
            result = await run_in_threadpool(adapter.health_check)
            ok, details = result.ok, result.details
            if ok and agent.available():
                try:
                    rooms = await linked_rooms(session, spec)
                except (AgentUnavailable, AgentRefused) as exc:
                    ok, details = False, f"Could not check the bridge: {exc}"
                else:
                    room = next((r for r in rooms if r["room_id"] == destination["room_id"]), None)
                    if room is None:
                        ok, details = False, f"The room is no longer linked to a {spec.chat_noun}."
                    elif not room["relay"]:
                        ok = False
                        details = (
                            "Relay is off in this room, so the bridge will not forward "
                            "announcements. Use Repair to turn it on."
                        )
                    else:
                        details = (
                            f"The announcer can post in the room and relay is on, so "
                            f"announcements reach the {spec.name} {spec.chat_noun}."
                        )
            audit(request, session["username"], "channel.tested", name, ok=ok)
            message = flash(f"{spec.name}: {details}", "success" if ok else "error")
        return redirect_with(request, f"/admin/channels#bridged-{spec.key}", message)

    @app.post("/admin/channels/bridged/{name}/remove")
    async def remove_bridged_group(request: Request, name: str) -> Response:
        session, _ = await checked_form(request, "settings")
        destination = destination_or_404(name)
        spec = spec_or_404(destination["bridge"])
        storage.delete_bridged_destination(name)
        publisher.reload_connections()
        account = storage.get_announcer()
        if destination["account"] == "announcer" and account:
            # Best effort: the announcer stops reading a room it no longer posts in.
            await run_in_threadpool(
                matrix_call,
                account["access_token"],
                "POST",
                f"/_matrix/client/v3/rooms/{quote(destination['room_id'], safe='')}/leave",
                {},
            )
        audit(
            request,
            session["username"],
            "channel.disconnected",
            name,
            bridge=spec.key,
            room_id=destination["room_id"],
        )
        return redirect_with(
            request,
            f"/admin/channels#bridged-{spec.key}",
            flash(
                f"Announcements are no longer posted in {destination['room_name']}. "
                f"The {spec.chat_noun} and its bridge are unchanged."
            ),
        )

    return bridged_sections
