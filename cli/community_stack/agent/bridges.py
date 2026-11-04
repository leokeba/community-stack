"""Operate the WhatsApp bridge through its Matrix bot, for the admin console.

The bridge's own bot is the only control surface used here: the agent joins a private
direct chat with `@whatsappbot:<server>` as the stack's admin account, sends one
allowlisted command, and returns the bot's replies. The linked-device QR code is
fetched from Synapse and returned once to the administrator; it is never logged.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from community_stack import docker
from community_stack.agent import matrix_admin
from community_stack.bridges.catalogue import WHATSAPP

BOT_LOCALPART = WHATSAPP.bot_localpart
CONTAINER_NAME = WHATSAPP.container
REGISTRATION_NAME = WHATSAPP.registration_file
# Commands the console may send. Anything else, including logout, stays a CLI-only action.
COMMANDS: dict[str, str] = {
    "cancel": "Cancel a linking attempt that is waiting for a scan.",
    "help": "Show the bridge's commands.",
    "list-logins": "Show which WhatsApp accounts are linked.",
    "login qr": "Start linking a WhatsApp device; the bot replies with a QR code.",
}
COMMAND_PATTERN = re.compile("|".join(re.escape(command) for command in COMMANDS))
NOT_MANAGEMENT_ROOM = "not your management room"
REPLY_TIMEOUT_SECONDS = 25.0
REPLY_QUIET_SECONDS = 4.0
POLL_SECONDS = 2.0
JOIN_TIMEOUT_SECONDS = 20.0
MAX_TEXT_CHARS = 4000
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
# Replaceable in tests.
_sleep: Callable[[float], None] = time.sleep


class BridgeError(Exception):
    """A bridge action the agent refused or could not finish; shown to people."""


def _state_path(context) -> Path:
    return context.project_dir / "data" / "agent" / "bridges" / "whatsapp.json"


def _server_name(context) -> str:
    try:
        server = matrix_admin._homeserver(context).get("server_name")
    except matrix_admin.MatrixAdminError as exc:
        raise BridgeError(str(exc)) from exc
    if not server:
        raise BridgeError("Matrix has no server name yet; set up Matrix first.")
    return server


def _matrix(context, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    try:
        return matrix_admin._call(context, method, path, body)
    except matrix_admin.MatrixAdminError as exc:
        raise BridgeError(str(exc)) from exc


def _load_room(context) -> str | None:
    path = _state_path(context)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("room_id")


def _forget_room(context) -> None:
    _state_path(context).unlink(missing_ok=True)


def _save_room(context, room_id: str) -> None:
    path = _state_path(context)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps({"room_id": room_id}), encoding="utf-8")
    path.chmod(0o600)


def _joined(context, room_id: str) -> set[str] | None:
    status, reply = _matrix(
        context, "GET", f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/joined_members"
    )
    if status != 200:
        return None
    return set(reply.get("joined", {}))


def ensure_direct_room(context) -> str:
    """The admin's direct chat with the bridge bot, created once and then reused."""
    bot = f"@{BOT_LOCALPART}:{_server_name(context)}"
    room_id = _load_room(context)
    if room_id:
        joined = _joined(context, room_id)
        if joined is not None and bot in joined:
            return room_id
    status, created = _matrix(
        context,
        "POST",
        "/_matrix/client/v3/createRoom",
        {
            "is_direct": True,
            "preset": "trusted_private_chat",
            "invite": [bot],
            "name": "WhatsApp bridge commands",
        },
    )
    if status != 200:
        raise BridgeError("Synapse refused to open a chat with the WhatsApp bridge bot.")
    room_id = created["room_id"]
    _save_room(context, room_id)
    deadline = time.monotonic() + JOIN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if bot in (_joined(context, room_id) or set()):
            return room_id
        _sleep(POLL_SECONDS)
    raise BridgeError(
        "The WhatsApp bridge bot did not join the chat. Check that the bridge is running."
    )


def _send(context, room_id: str, text: str) -> str:
    txn = secrets.token_hex(8)
    path = f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/send/m.room.message/{txn}"
    status, reply = _matrix(context, "PUT", path, {"msgtype": "m.text", "body": text})
    if status != 200 or "event_id" not in reply:
        raise BridgeError("Synapse did not accept the command.")
    return reply["event_id"]


def _fetch_image(context, mxc: str) -> str | None:
    match = re.fullmatch(r"mxc://([A-Za-z0-9.:_-]+)/([A-Za-z0-9_-]+)", mxc or "")
    if not match:
        return None
    token = matrix_admin.admin_token(context)
    status, content_type, data = matrix_admin.synapse_download(
        f"/_matrix/client/v1/media/download/{match.group(1)}/{match.group(2)}", token
    )
    if status != 200 or content_type not in IMAGE_TYPES:
        return None
    return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"


def _reply_view(context, event: dict) -> dict[str, Any]:
    content = event.get("content", {})
    if content.get("msgtype") == "m.image":
        return {
            "kind": "image",
            "text": "Scan this code in WhatsApp: Settings, Linked devices, Link a device.",
            "image": _fetch_image(context, content.get("url", "")),
        }
    return {"kind": "text", "text": str(content.get("body", ""))[:MAX_TEXT_CHARS], "image": None}


def _collect_replies(context, room_id: str, after_event: str, bot: str) -> list[dict[str, Any]]:
    """Wait for the bot's answers to one command: until it goes quiet, or time runs out."""
    deadline = time.monotonic() + REPLY_TIMEOUT_SECONDS
    seen: list[dict] = []
    last_change = None
    path = f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/messages?dir=b&limit=50"
    while time.monotonic() < deadline:
        status, page = _matrix(context, "GET", path)
        if status == 200:
            newer = []
            for event in page.get("chunk", []):
                if event.get("event_id") == after_event:
                    break
                if event.get("sender") == bot and event.get("type") == "m.room.message":
                    newer.append(event)
            if len(newer) != len(seen):
                seen = newer
                last_change = time.monotonic()
        if (
            seen
            and last_change is not None
            and time.monotonic() - last_change >= REPLY_QUIET_SECONDS
        ):
            break
        _sleep(POLL_SECONDS)
    # Messages arrive newest-first; present them in the order the bot sent them.
    return [_reply_view(context, event) for event in reversed(seen)]


def run_command(context, command: str) -> dict[str, Any]:
    """Send one allowlisted command to the bridge bot and return its replies."""
    if command not in COMMANDS:
        raise BridgeError("That command is not available from the console.")
    bot = f"@{BOT_LOCALPART}:{_server_name(context)}"
    room_id = ensure_direct_room(context)
    event_id = _send(context, room_id, command)
    replies = _collect_replies(context, room_id, event_id, bot)
    if replies and NOT_MANAGEMENT_ROOM in replies[0]["text"]:
        # The bridge keeps its own record of the management room; if it was reset, the
        # cached chat is stale. Open a fresh chat once, which the bridge marks as management.
        _forget_room(context)
        room_id = ensure_direct_room(context)
        event_id = _send(context, room_id, command)
        replies = _collect_replies(context, room_id, event_id, bot)
    return {
        "command": command,
        "replies": replies,
        "note": ""
        if replies
        else "The bridge bot did not answer. Check that the bridge is running, then try again.",
    }


def _synapse_started_at() -> datetime | None:
    """When the Synapse container last started, or None if Docker cannot say."""
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.StartedAt}}", "community_synapse"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    if result.returncode != 0:
        return None
    # Docker reports nanoseconds; Python parses microseconds.
    raw = result.stdout.strip().removesuffix("Z")
    head, _, fraction = raw.partition(".")
    try:
        return datetime.fromisoformat(
            f"{head}.{fraction[:6]}+00:00" if fraction else f"{head}+00:00"
        )
    except ValueError:
        return None


def diagnose(context) -> list[dict[str, Any]]:
    """Each check says what it found and, when it fails, what to do about it."""
    config_path = context.config_path
    config = (
        (yaml.safe_load(config_path.read_text(encoding="utf-8")) or {})
        if config_path.is_file()
        else {}
    )
    enabled = bool(config.get("services", {}).get(WHATSAPP.service, {}).get("enabled", False))
    data_dir = context.project_dir / "data" / WHATSAPP.service
    homeserver = context.project_dir / "data" / "matrix" / "homeserver.yaml"
    registration = context.project_dir / "data" / "matrix" / "appservices" / REGISTRATION_NAME
    registration_entry = f"/data/appservices/{REGISTRATION_NAME}"
    checks: list[dict[str, Any]] = []

    def add(check_id: str, ok: bool, message: str, fix: str = "") -> None:
        checks.append({"id": check_id, "ok": ok, "message": message, "fix": "" if ok else fix})

    add(
        "enabled",
        enabled,
        "Bridge is enabled." if enabled else "Bridge is turned off.",
        "Turn WhatsApp on under Services.",
    )
    add(
        "matrix",
        homeserver.is_file(),
        "Matrix is set up." if homeserver.is_file() else "Matrix has not been set up.",
        "Set up Matrix on the Services page before the bridge.",
    )
    add(
        "bridge_config",
        (data_dir / "config.yaml").is_file(),
        "Bridge configuration exists."
        if (data_dir / "config.yaml").is_file()
        else "Bridge configuration is missing.",
        "Turn WhatsApp off and on again to regenerate it.",
    )
    add(
        "bridge_registration",
        (data_dir / "registration.yaml").is_file(),
        "Bridge registration exists."
        if (data_dir / "registration.yaml").is_file()
        else "Bridge registration is missing.",
        "Turn WhatsApp off and on again to regenerate it.",
    )

    listed = False
    if homeserver.is_file():
        try:
            listed = registration_entry in (
                (yaml.safe_load(homeserver.read_text(encoding="utf-8")) or {}).get(
                    "app_service_config_files"
                )
                or []
            )
        except yaml.YAMLError:
            listed = False
    add(
        "synapse_registered",
        listed and registration.is_file(),
        "Synapse lists the bridge registration."
        if listed and registration.is_file()
        else "Synapse does not list the bridge registration.",
        "Turn WhatsApp off and on again so Synapse is given its registration.",
    )

    started = (
        _synapse_started_at() if docker.is_docker_available() and registration.is_file() else None
    )
    registration_time = (
        datetime.fromtimestamp(registration.stat().st_mtime, tz=timezone.utc)
        if registration.is_file()
        else None
    )
    stale = started is not None and registration_time is not None and registration_time > started
    add(
        "synapse_restarted",
        not stale,
        "Synapse started after the bridge registration was last written."
        if not stale
        else "Synapse started before the bridge registration was written.",
        "Restart Matrix so it reads the bridge registration.",
    )

    containers = docker.list_community_containers() if docker.is_docker_available() else []
    container = next((c for c in containers if c.get("name") == CONTAINER_NAME), None)
    running = bool(container) and container.get("state") == "running"
    add(
        "container",
        running,
        f"Bridge container is {container.get('state')}."
        if container
        else "Bridge container is not running.",
        "Start the bridge on the Services page.",
    )
    return checks


def status(context) -> dict[str, Any]:
    """Local state only: no Matrix or bot messages are sent to answer this."""
    checks = diagnose(context)
    by_id = {check["id"]: check for check in checks}
    container = by_id["container"]
    return {
        "enabled": by_id["enabled"]["ok"],
        "registered_with_synapse": by_id["synapse_registered"]["ok"],
        "container_state": "running" if container["ok"] else "not running",
        "container_status": "",
        "command_chat_created": _load_room(context) is not None,
        "commands": dict(COMMANDS),
        "checks": checks,
        "healthy": all(check["ok"] for check in checks),
    }
