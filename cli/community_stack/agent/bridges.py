"""Operate any mautrix bridgev2 bridge for the admin console: rooms, links and repairs.

Everything here works the same for every bridge in the catalogue; what differs between
bridges (database file, chat ID format, required settings) is data in the catalogue.
Network-specific extras, such as joining a WhatsApp group from an invite link, live in
their own modules and build on these functions.

How the pieces fit:

* The bridge's bot is the control surface. The agent talks to it as the stack's admin
  account, which is also the account linked to the remote network. In the bot's
  management room commands need no prefix; in any other room they need the bridge's
  command prefix (``!wa``, ``!signal``...).
* The bridge's own database is read (never written while the bridge runs) to learn which
  remote chat is linked to which Matrix room. Links are changed only through bot commands:
  ``bridge`` attaches a chat to an existing room, ``unbridge`` detaches it and keeps the
  room for its Matrix members, ``delete-portal`` removes the room.
* The chat allowlist in the bridge's config (``portal_create_filter``) only decides whether
  the bridge may create a room by itself for an unlinked chat. Detaching a chat therefore
  also removes it from the allowlist, or its next message would create a new room.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import sqlite3
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import yaml

from community_stack import docker
from community_stack.agent import matrix_admin
from community_stack.bridges import config as bridge_config
from community_stack.bridges.catalogue import LOGIN_COMMAND, WHATSAPP, BridgeSpec, supported
from community_stack.matrix_ids import ROOM_ID

NOT_MANAGEMENT_ROOM = "not your management room"
REPLY_TIMEOUT_SECONDS = 25.0
REPLY_QUIET_SECONDS = 4.0
POLL_SECONDS = 2.0
JOIN_TIMEOUT_SECONDS = 20.0
PORTAL_TIMEOUT_SECONDS = 45.0
MAX_TEXT_CHARS = 4000
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
NOT_LINKED_MARKERS = ("not logged in", "no user logins", "no logins")
# A named login flow, for bridges whose flows are not fixed in the catalogue.
LOGIN_FLOW_PATTERN = re.compile(rf"{LOGIN_COMMAND} [a-z][a-z0-9-]{{0,31}}")
# Reply texts of the bridgev2 bot (mautrix-go bridgev2/commands).
RELAY_SET = "will now be relayed through"
RELAY_UNSET = "Stopped relaying"
NO_RELAY = "doesn't have a relay set"
# The power level the bridge bot gets in a room it manages: it sets the room's name, topic
# and avatar and invites the remote chat's members.
BOT_POWER = 100
SPACE = "m.space"
# Replaceable in tests.
_sleep: Callable[[float], None] = time.sleep


class BridgeError(Exception):
    """A bridge action the agent refused or could not finish; shown to people."""


# --- Paths and plain Matrix calls ---------------------------------------------------------


def _state_path(context, spec: BridgeSpec = WHATSAPP) -> Path:
    return context.project_dir / "data" / "agent" / "bridges" / f"{spec.key}.json"


def _config_path(context, spec: BridgeSpec) -> Path:
    return context.project_dir / "data" / spec.service / "config.yaml"


def _database_path(context, spec: BridgeSpec) -> Path:
    return context.project_dir / "data" / spec.service / spec.database_file


def _server_name(context) -> str:
    try:
        server = matrix_admin._homeserver(context).get("server_name")
    except matrix_admin.MatrixAdminError as exc:
        raise BridgeError(str(exc)) from exc
    if not server:
        raise BridgeError("Matrix has no server name yet; set up Matrix first.")
    return server


def _admin_id(context) -> str:
    return f"@{matrix_admin.ADMIN_LOCALPART}:{_server_name(context)}"


def _bot_id(context, spec: BridgeSpec) -> str:
    return f"@{spec.bot_localpart}:{_server_name(context)}"


def _matrix(context, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    """A request as the stack's admin account."""
    try:
        return matrix_admin._call(context, method, path, body)
    except matrix_admin.MatrixAdminError as exc:
        raise BridgeError(str(exc)) from exc


def _room_path(room_id: str) -> str:
    return quote(room_id, safe="")


def _error(reply: Any, status: int) -> Any:
    return reply.get("error", status) if isinstance(reply, dict) else status


def _check_room_id(room_id: str) -> None:
    if not ROOM_ID.fullmatch(room_id or ""):
        raise BridgeError("That is not a valid Matrix room ID.")


def _check_chat_id(spec: BridgeSpec, chat_id: str) -> None:
    if not spec.chat_id_ok(chat_id or ""):
        raise BridgeError(f"That {spec.name} {spec.chat_noun} ID is not valid.")


def _load_room(context, spec: BridgeSpec = WHATSAPP) -> str | None:
    path = _state_path(context, spec)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("room_id")


def _forget_room(context, spec: BridgeSpec = WHATSAPP) -> None:
    _state_path(context, spec).unlink(missing_ok=True)


def _save_room(context, room_id: str, spec: BridgeSpec = WHATSAPP) -> None:
    path = _state_path(context, spec)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps({"room_id": room_id}), encoding="utf-8")
    path.chmod(0o600)


def _joined(context, room_id: str) -> set[str] | None:
    status, reply = _matrix(
        context, "GET", f"/_matrix/client/v3/rooms/{_room_path(room_id)}/joined_members"
    )
    if status != 200:
        return None
    return set(reply.get("joined", {}))


# --- Calls as the bridge bot (appservice) ---------------------------------------------------


def _appservice_token(context, spec: BridgeSpec) -> tuple[str, str]:
    path = context.project_dir / "data" / spec.service / "registration.yaml"
    try:
        registration = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise BridgeError(f"The {spec.name} appservice registration cannot be read.") from exc
    if not isinstance(registration, dict):
        raise BridgeError(f"The {spec.name} appservice registration is invalid.")

    token = registration.get("as_token")
    bot = _bot_id(context, spec)
    namespaces_config = registration.get("namespaces", {})
    namespaces = namespaces_config.get("users", []) if isinstance(namespaces_config, dict) else []
    authorized = False
    if isinstance(namespaces, list):
        for namespace in namespaces:
            if not isinstance(namespace, dict) or namespace.get("exclusive") is not True:
                continue
            pattern = namespace.get("regex")
            if not isinstance(pattern, str):
                continue
            try:
                if re.fullmatch(pattern, bot):
                    authorized = True
                    break
            except re.error:
                continue
    if not isinstance(token, str) or not token or not authorized:
        raise BridgeError(
            f"The {spec.name} registration does not authorize its configured bot identity."
        )
    return token, bot


def _as_bot(context, spec: BridgeSpec, method: str, path: str, body: Any = None):
    """A request as the bridge bot, using the bridge's own appservice token."""
    token, bot = _appservice_token(context, spec)
    separator = "&" if "?" in path else "?"
    path = f"{path}{separator}{urlencode({'user_id': bot})}"
    try:
        return matrix_admin.synapse_request(method, path, body, token)
    except matrix_admin.MatrixAdminError as exc:
        raise BridgeError(str(exc)) from exc


def _bot_rooms(context, spec: BridgeSpec) -> list[str]:
    status, joined = _as_bot(context, spec, "GET", "/_matrix/client/v3/joined_rooms")
    if status != 200:
        raise BridgeError(
            f"Synapse did not list rooms for the {spec.name} bot: {_error(joined, status)}"
        )
    rooms = joined.get("joined_rooms") if isinstance(joined, dict) else None
    if not isinstance(rooms, list):
        raise BridgeError(f"Synapse returned an invalid room list for the {spec.name} bot.")
    return [room for room in rooms if isinstance(room, str) and ROOM_ID.fullmatch(room)]


def _invite_as_bot(context, room_id: str, user_id: str, spec: BridgeSpec) -> None:
    status, reply = _as_bot(
        context,
        spec,
        "POST",
        f"/_matrix/client/v3/rooms/{_room_path(room_id)}/invite",
        {"user_id": user_id},
    )
    if status != 200:
        raise BridgeError(
            f"Synapse could not invite {user_id} to the room: {_error(reply, status)}"
        )


# --- Room state, membership and power --------------------------------------------------------


def _room_state(context, room_id: str) -> list[dict[str, Any]] | None:
    """A room's current state through the admin API, whoever is in it; None if unknown."""
    status, reply = _matrix(context, "GET", f"/_synapse/admin/v1/rooms/{_room_path(room_id)}/state")
    if status == 404:
        return None
    state = reply.get("state") if isinstance(reply, dict) else None
    if status != 200 or not isinstance(state, list):
        raise BridgeError(f"Synapse could not read room {room_id}: {_error(reply, status)}")
    return [event for event in state if isinstance(event, dict)]


def _state_content(state: list[dict], event_type: str, state_key: str = "") -> dict[str, Any]:
    for event in state:
        if event.get("type") == event_type and event.get("state_key", "") == state_key:
            content = event.get("content")
            return content if isinstance(content, dict) else {}
    return {}


def _memberships(state: list[dict]) -> dict[str, str]:
    return {
        event.get("state_key"): (event.get("content") or {}).get("membership")
        for event in state
        if event.get("type") == "m.room.member"
    }


def _room_name(state: list[dict]) -> str:
    return str(_state_content(state, "m.room.name").get("name") or "")


def _power_of(state: list[dict], user_id: str) -> float:
    """A user's effective power level, including room version 12's unlimited creators."""
    create = next((event for event in state if event.get("type") == "m.room.create"), {})
    content = create.get("content") or {}
    creators = {create.get("sender"), content.get("creator")} | set(
        content.get("additional_creators") or []
    )
    if str(content.get("room_version", "1")) == "12" and user_id in creators:
        return float("inf")
    levels = _state_content(state, "m.room.power_levels")
    if not levels:
        return 100 if user_id in creators else 0
    users = levels.get("users") if isinstance(levels.get("users"), dict) else {}
    return float(users.get(user_id, levels.get("users_default", 0)))


def _state_level(state: list[dict], event_type: str) -> int:
    levels = _state_content(state, "m.room.power_levels")
    events = levels.get("events") if isinstance(levels.get("events"), dict) else {}
    return int(events.get(event_type, levels.get("state_default", 50 if levels else 0)))


def _ensure_stack_admin_joined(
    context, room_id: str, membership: str | None, spec: BridgeSpec
) -> tuple[str, str]:
    if membership in {"join", "ban"}:
        return membership, ""
    try:
        if membership != "invite":
            _invite_as_bot(context, room_id, _admin_id(context), spec)
        status, reply = _matrix(
            context, "POST", f"/_matrix/client/v3/rooms/{_room_path(room_id)}/join"
        )
        if status != 200:
            raise BridgeError(
                f"Synapse could not add the stack admin to the room: {_error(reply, status)}"
            )
        return "join", ""
    except BridgeError as exc:
        return membership or "not_joined", str(exc)


def _join_portal_as_admin(context, room_id: str, spec: BridgeSpec) -> None:
    """Put the stack admin in a portal (through the bridge bot) before it posts there."""
    state = _room_state(context, room_id)
    if state is None:
        raise BridgeError("That bridged room no longer exists.")
    membership = _memberships(state).get(_admin_id(context))
    membership, error = _ensure_stack_admin_joined(context, room_id, membership, spec)
    if membership != "join":
        raise BridgeError(error or "The stack admin is banned from that room.")


def _force_join_admin(context, room_id: str) -> None:
    """Join the stack admin to a room on this server, through Synapse's admin API."""
    status, reply = _matrix(
        context,
        "POST",
        f"/_synapse/admin/v1/join/{_room_path(room_id)}",
        {"user_id": _admin_id(context)},
    )
    if status != 200:
        raise BridgeError(
            f"The stack admin could not join that room. Invite {_admin_id(context)} to it "
            f"from Element, then try again. ({_error(reply, status)})"
        )


def _grant_bot_power(context, room_id: str, spec: BridgeSpec) -> bool:
    """Give the bridge bot the power it needs, as the stack admin.

    Returns True if the stack admin first had to be made a room admin through Synapse's
    admin API, because nobody had given it enough power in the room.
    """
    admin, bot = _admin_id(context), _bot_id(context, spec)
    promoted = False
    for attempt in range(2):
        state = _room_state(context, room_id) or []
        needed = max(_state_level(state, "m.bridge"), _state_level(state, "m.room.name"), 50)
        if _power_of(state, bot) >= needed:
            return promoted
        admin_power = _power_of(state, admin)
        target = BOT_POWER if admin_power >= BOT_POWER else int(admin_power)
        if target >= needed and admin_power >= _state_level(state, "m.room.power_levels"):
            levels = dict(_state_content(state, "m.room.power_levels"))
            levels["users"] = {**(levels.get("users") or {}), bot: target}
            status, reply = _matrix(
                context,
                "PUT",
                f"/_matrix/client/v3/rooms/{_room_path(room_id)}/state/m.room.power_levels/",
                levels,
            )
            if status != 200:
                raise BridgeError(
                    "The stack admin could not give the bridge bot power in that room: "
                    f"{_error(reply, status)}"
                )
            return promoted
        if attempt:
            break
        status, _ = _matrix(
            context,
            "POST",
            f"/_synapse/admin/v1/rooms/{_room_path(room_id)}/make_room_admin",
            {"user_id": admin},
        )
        promoted = status == 200
        if not promoted:
            break
    raise BridgeError(
        f"The stack admin does not have enough power in that room to let the {spec.name} bot "
        f"manage it. In Element, give {admin} administrator rights in the room, then try again."
    )


def _invite_bot(context, room_id: str, spec: BridgeSpec) -> None:
    """Invite the bridge bot as the stack admin and wait until it has joined."""
    bot = _bot_id(context, spec)
    membership = _memberships(_room_state(context, room_id) or []).get(bot)
    if membership == "join":
        return
    if membership == "ban":
        raise BridgeError(f"The {spec.name} bridge bot is banned from that room. Unban it first.")
    if membership != "invite":
        status, reply = _matrix(
            context,
            "POST",
            f"/_matrix/client/v3/rooms/{_room_path(room_id)}/invite",
            {"user_id": bot},
        )
        if status != 200:
            raise BridgeError(
                f"The stack admin could not invite the {spec.name} bot: {_error(reply, status)}"
            )
    deadline = time.monotonic() + JOIN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if bot in (_joined(context, room_id) or set()):
            return
        _sleep(POLL_SECONDS)
    raise BridgeError(
        f"The {spec.name} bridge bot did not join the room. Check that the bridge is running."
    )


# --- The bot's management room ("command chat") ---------------------------------------------


def is_allowed_command(spec: BridgeSpec, command: str) -> bool:
    """Exact commands from the catalogue, plus a named login flow where flows are not fixed."""
    if command in spec.commands:
        return True
    prefix = f"{LOGIN_COMMAND} "
    if not command.startswith(prefix):
        return False
    flow = command.removeprefix(prefix)
    if spec.login_flows:
        return flow in spec.login_flows
    return bool(LOGIN_FLOW_PATTERN.fullmatch(command))


def command_chat_name(spec: BridgeSpec) -> str:
    return f"{spec.name} bridge commands"


def ensure_direct_room(context, spec: BridgeSpec = WHATSAPP) -> str:
    """The admin's direct chat with the bridge bot, created once and then reused."""
    bot = _bot_id(context, spec)
    room_id = _load_room(context, spec)
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
            "name": command_chat_name(spec),
        },
    )
    if status != 200:
        raise BridgeError(f"Synapse refused to open a chat with the {spec.name} bridge bot.")
    room_id = created["room_id"]
    _save_room(context, room_id, spec)
    deadline = time.monotonic() + JOIN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if bot in (_joined(context, room_id) or set()):
            return room_id
        _sleep(POLL_SECONDS)
    raise BridgeError(
        f"The {spec.name} bridge bot did not join the chat. Check that the bridge is running."
    )


def reset_command_chat(context, spec: BridgeSpec) -> dict[str, str]:
    """Open a fresh command chat with the bot; the bot makes it its management room."""
    _forget_room(context, spec)
    return {"room_id": ensure_direct_room(context, spec)}


def _send(context, room_id: str, text: str) -> str:
    txn = secrets.token_hex(8)
    path = f"/_matrix/client/v3/rooms/{_room_path(room_id)}/send/m.room.message/{txn}"
    status, reply = _matrix(context, "PUT", path, {"msgtype": "m.text", "body": text})
    if status != 200 or "event_id" not in reply:
        raise BridgeError("Synapse did not accept the command.")
    return reply["event_id"]


def _redact_command_event(context, room_id: str, event_id: str) -> None:
    txn = secrets.token_hex(8)
    path = f"/_matrix/client/v3/rooms/{_room_path(room_id)}/redact/{quote(event_id, safe='')}/{txn}"
    status, _ = _matrix(context, "PUT", path, {"reason": "Remove an invite link"})
    if status != 200:
        raise BridgeError(
            "Synapse could not redact the invite link from the bridge chat. Revoke that "
            "invite link before trying again."
        )


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


def _reply_view(context, event: dict, spec: BridgeSpec = WHATSAPP) -> dict[str, Any]:
    content = event.get("content", {})
    if content.get("msgtype") == "m.image":
        return {
            "kind": "image",
            "text": f"Scan this code in {spec.name}, under linked devices.",
            "image": _fetch_image(context, content.get("url", "")),
        }
    return {"kind": "text", "text": str(content.get("body", ""))[:MAX_TEXT_CHARS], "image": None}


def _collect_replies(
    context, room_id: str, after_event: str, bot: str, spec: BridgeSpec = WHATSAPP
) -> list[dict[str, Any]]:
    """Wait for the bot's answers to one command: until it goes quiet, or time runs out."""
    deadline = time.monotonic() + REPLY_TIMEOUT_SECONDS
    seen: list[dict] = []
    last_change = None
    path = f"/_matrix/client/v3/rooms/{_room_path(room_id)}/messages?dir=b&limit=50"
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
    return [_reply_view(context, event, spec) for event in reversed(seen)]


def _texts(replies: list[dict[str, Any]]) -> str:
    return "\n".join(reply["text"] for reply in replies if reply["kind"] == "text")


def _bot_command(context, command: str, spec: BridgeSpec, redact: bool = False) -> dict[str, Any]:
    """Send any command in the management room. Callers decide what may be sent; with
    ``redact`` the command message is redacted afterwards and not echoed back."""
    bot = _bot_id(context, spec)
    room_id = ensure_direct_room(context, spec)

    def send_and_collect(current_room: str) -> list[dict[str, Any]]:
        event_id = _send(context, current_room, command)
        try:
            return _collect_replies(context, current_room, event_id, bot, spec)
        finally:
            if redact:
                _redact_command_event(context, current_room, event_id)

    replies = send_and_collect(room_id)
    if replies and NOT_MANAGEMENT_ROOM in replies[0]["text"]:
        # The bridge keeps its own record of the management room; if it was reset, the cached
        # chat is stale. Open a fresh chat once, which the bridge marks as its management room.
        _forget_room(context, spec)
        room_id = ensure_direct_room(context, spec)
        replies = send_and_collect(room_id)
    return {
        "command": command.split(" ", 1)[0] if redact else command,
        "replies": replies,
        "note": ""
        if replies
        else "The bridge bot did not answer. Check that the bridge is running, then try again.",
    }


def run_command(context, command: str, spec: BridgeSpec = WHATSAPP) -> dict[str, Any]:
    """Send one allowlisted command to the bridge bot and return its replies."""
    if not is_allowed_command(spec, command):
        raise BridgeError("That command is not available from the console.")
    return _bot_command(context, command, spec)


def _portal_command(context, room_id: str, command: str, spec: BridgeSpec) -> list[dict[str, Any]]:
    """Run a bot command inside a portal room, as the stack admin, and return the replies."""
    _join_portal_as_admin(context, room_id, spec)
    try:
        prefix = bridge_config.command_prefix(_config_path(context, spec), spec)
    except bridge_config.BridgeConfigError as exc:
        raise BridgeError(str(exc)) from exc
    event_id = _send(context, room_id, f"{prefix} {command}")
    return _collect_replies(context, room_id, event_id, _bot_id(context, spec), spec)


def login_state(context, spec: BridgeSpec = WHATSAPP) -> dict[str, Any]:
    """Whether an account is linked, judged from the bot's own answer to list-logins."""
    result = run_command(context, "list-logins", spec)
    texts = [reply["text"] for reply in result["replies"] if reply["kind"] == "text"]
    if not texts:
        return {"known": False, "linked": False, "detail": result["note"]}
    detail = " ".join(texts)
    linked = not any(marker in detail.lower() for marker in NOT_LINKED_MARKERS)
    return {"known": True, "linked": linked, "detail": detail[:MAX_TEXT_CHARS]}


# --- What the bridge knows: its portals -------------------------------------------------------


def _portal_records(context, spec: BridgeSpec) -> list[dict[str, Any]]:
    """Shared chats the bridge has recorded, read-only from its database."""
    database = _database_path(context, spec)
    if not spec.database_file or not database.is_file():
        return []
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(portal)")}
            relay = "relay_login_id" if "relay_login_id" in columns else "NULL"
            rows = db.execute(
                f"SELECT id, mxid, name, {relay} FROM portal WHERE receiver = '' ORDER BY id"
            ).fetchall()
    except sqlite3.Error as exc:
        raise BridgeError(f"Could not read the {spec.name} bridge's chat records.") from exc
    return [
        {
            "chat_id": chat_id,
            "room_id": room_id if isinstance(room_id, str) and ROOM_ID.fullmatch(room_id) else None,
            "name": name.strip() if isinstance(name, str) else "",
            "relay": bool(relay_login),
        }
        for chat_id, room_id, name, relay_login in rows
        if isinstance(chat_id, str) and spec.chat_id_ok(chat_id)
    ]


def _record(context, spec: BridgeSpec, chat_id: str) -> dict[str, Any] | None:
    return next((r for r in _portal_records(context, spec) if r["chat_id"] == chat_id), None)


def _record_for_room(context, spec: BridgeSpec, room_id: str) -> dict[str, Any] | None:
    return next((r for r in _portal_records(context, spec) if r["room_id"] == room_id), None)


def _wait_for_record(
    context, spec: BridgeSpec, chat_id: str, check: Callable[[dict | None], bool]
) -> dict[str, Any] | None:
    deadline = time.monotonic() + PORTAL_TIMEOUT_SECONDS
    while True:
        record = _record(context, spec, chat_id)
        if check(record) or time.monotonic() >= deadline:
            return record
        _sleep(POLL_SECONDS)


def _allowed_chats(context, spec: BridgeSpec) -> list[str]:
    try:
        return bridge_config.allowed_chats(_config_path(context, spec))
    except bridge_config.BridgeConfigError as exc:
        raise BridgeError(str(exc)) from exc


def _display_name(record: dict[str, Any] | None, spec: BridgeSpec) -> str:
    if record and record.get("name"):
        return record["name"]
    return f"Unnamed {spec.chat_noun}"


def _sort_key(item: dict[str, Any]) -> tuple[str, str]:
    return item["name"].lower(), item.get("room_id") or item.get("chat_id") or ""


def overview(context, spec: BridgeSpec) -> dict[str, Any]:
    """Everything the bridge page shows: linked rooms, unlinked chats and what needs repair.

    * rooms: Matrix rooms linked to a chat, with the bridge bot in them.
    * chats: chats the bridge has seen that have no room. Allowlisted ones are waiting for
      the bridge to create their room on their next message.
    * broken: chats linked to a room the bot is not in, or that no longer exists.
    * leftovers: rooms the bot is still in but that no chat is linked to.
    """
    records = _portal_records(context, spec)
    allowed = set(_allowed_chats(context, spec))
    bot_rooms = _bot_rooms(context, spec)
    command_room = _load_room(context, spec)
    admin, bot = _admin_id(context), _bot_id(context, spec)

    rooms: list[dict[str, Any]] = []
    broken: list[dict[str, Any]] = []
    chats: list[dict[str, Any]] = []
    linked = set()
    for record in records:
        room_id = record["room_id"]
        if room_id is None:
            chats.append(
                {
                    "chat_id": record["chat_id"],
                    "name": _display_name(record, spec),
                    "allowed": record["chat_id"] in allowed,
                }
            )
            continue
        linked.add(room_id)
        state = _room_state(context, room_id)
        if state is None or room_id not in bot_rooms:
            broken.append(
                {
                    "chat_id": record["chat_id"],
                    "name": _display_name(record, spec),
                    "room_id": room_id,
                    "problem": "The Matrix room no longer exists."
                    if state is None
                    else "The bridge bot is no longer in the Matrix room.",
                }
            )
            continue
        memberships = _memberships(state)
        rooms.append(
            {
                "room_id": room_id,
                "chat_id": record["chat_id"],
                "name": _room_name(state) or _display_name(record, spec),
                "chat_name": _display_name(record, spec),
                "members": sum(value == "join" for value in memberships.values()),
                "admin_joined": memberships.get(admin) == "join",
                "relay": record["relay"],
                "allowed": record["chat_id"] in allowed,
                "encrypted": bool(_state_content(state, "m.room.encryption")),
            }
        )
    known = {record["chat_id"] for record in records}
    chats.extend(
        {"chat_id": chat_id, "name": f"Unnamed {spec.chat_noun}", "allowed": True}
        for chat_id in sorted(allowed - known)
        if spec.chat_id_ok(chat_id)
    )

    leftovers = []
    for room_id in bot_rooms:
        if room_id in linked or room_id == command_room:
            continue
        state = _room_state(context, room_id) or []
        name = _room_name(state)
        memberships = _memberships(state)
        if name == command_chat_name(spec) or memberships.get(bot) != "join":
            continue
        leftovers.append(
            {
                "room_id": room_id,
                "name": name or "(unnamed room)",
                "members": sum(value == "join" for value in memberships.values()),
            }
        )

    return {
        "rooms": sorted(rooms, key=_sort_key),
        "chats": sorted(chats, key=lambda item: (not item["allowed"], *_sort_key(item))),
        "broken": sorted(broken, key=_sort_key),
        "leftovers": sorted(leftovers, key=_sort_key),
    }


def portal_rooms(context, spec: BridgeSpec = WHATSAPP) -> list[dict[str, Any]]:
    """Matrix rooms linked to a chat of this bridge (read-only)."""
    return overview(context, spec)["rooms"]


def _portal_room(context, spec: BridgeSpec, room_id: str) -> dict[str, Any]:
    _check_room_id(room_id)
    room = next((item for item in portal_rooms(context, spec) if item["room_id"] == room_id), None)
    if room is None:
        raise BridgeError(f"That room is not linked to a {spec.name} {spec.chat_noun}.")
    return room


def matrix_rooms(context, spec: BridgeSpec) -> list[dict[str, Any]]:
    """Rooms on this server a chat could be attached to: not spaces, not already bridged."""
    status, reply = _matrix(context, "GET", "/_synapse/admin/v1/rooms?limit=500&order_by=name")
    rooms = reply.get("rooms") if isinstance(reply, dict) else None
    if status != 200 or not isinstance(rooms, list):
        raise BridgeError(f"Synapse could not list its rooms: {_error(reply, status)}")
    excluded = set()
    for other in supported():
        excluded.update(r["room_id"] for r in _portal_records(context, other) if r["room_id"])
        command_room = _load_room(context, other)
        if command_room:
            excluded.add(command_room)
    command_names = {command_chat_name(other) for other in supported()}
    candidates = []
    for room in rooms:
        room_id = room.get("room_id")
        if not isinstance(room_id, str) or not ROOM_ID.fullmatch(room_id) or room_id in excluded:
            continue
        if room.get("room_type") == SPACE or room.get("name") in command_names:
            continue
        candidates.append(
            {
                "room_id": room_id,
                "name": room.get("name") or room.get("canonical_alias") or "(unnamed room)",
                "members": int(room.get("joined_members") or 0),
                "encrypted": bool(room.get("encryption")),
            }
        )
    return sorted(candidates, key=lambda room: (room["name"].lower(), room["room_id"]))


# --- Changing links ----------------------------------------------------------------------------


def _container_state(spec: BridgeSpec) -> str | None:
    if not docker.is_docker_available():
        return None
    container = next(
        (item for item in docker.list_community_containers() if item.get("name") == spec.container),
        None,
    )
    return container.get("state") if container else "missing"


def _require_running(spec: BridgeSpec) -> None:
    if _container_state(spec) != "running":
        raise BridgeError(f"Start the {spec.name} bridge first, under Services and updates.")


def restart_bridge(context, spec: BridgeSpec) -> dict[str, bool]:
    if not docker.is_docker_available():
        raise BridgeError("Docker is unavailable, so the bridge cannot be restarted.")
    result = docker.run_compose(
        context.services_dir / spec.service,
        ["restart", spec.service],
        env_file=context.env_path,
        capture_output=True,
    )
    if result.returncode != 0:
        raise BridgeError(
            f"The {spec.name} bridge could not restart. Check its logs under Health and logs."
        )
    from community_stack.agent import settings

    try:
        settings.wait_for(context, spec.service, True)
    except settings.SettingsError as exc:
        raise BridgeError(
            f"The {spec.name} bridge did not become healthy after restarting. "
            "Check its logs under Health and logs."
        ) from exc
    return {"restarted": True}


def _set_allowed(context, spec: BridgeSpec, chat_id: str, allowed: bool) -> bool:
    """Change the allowlist and reload a running bridge, which reads it at start-up."""
    _check_chat_id(spec, chat_id)
    path = _config_path(context, spec)
    try:
        if allowed:
            changed = bridge_config.allow_chat(path, spec, chat_id)
        else:
            changed = bridge_config.disallow_chat(path, spec, chat_id)
    except bridge_config.BridgeConfigError as exc:
        raise BridgeError(str(exc)) from exc
    if changed and _container_state(spec) == "running":
        restart_bridge(context, spec)
    return changed


def allow_chat(context, spec: BridgeSpec, chat_id: str) -> dict[str, bool]:
    """Let the bridge create a room for this chat by itself, on the chat's next message."""
    return {"changed": _set_allowed(context, spec, chat_id, True)}


def disallow_chat(context, spec: BridgeSpec, chat_id: str) -> dict[str, bool]:
    """Stop the bridge creating a room for this chat. Existing links are not touched."""
    return {"changed": _set_allowed(context, spec, chat_id, False)}


def set_room_relay(
    context, room_id: str, spec: BridgeSpec = WHATSAPP, enabled: bool = True
) -> dict[str, Any]:
    """Relay messages from Matrix people without their own login through the linked account."""
    _portal_room(context, spec, room_id)
    replies = _portal_command(context, room_id, "set-relay" if enabled else "unset-relay", spec)
    text = _texts(replies)
    ok = (RELAY_SET in text) if enabled else (RELAY_UNSET in text or NO_RELAY in text)
    return {
        "room_id": room_id,
        "ok": ok,
        "replies": replies,
        "note": ""
        if replies
        else "The bridge bot did not answer. Check that the bridge is running, then try again.",
    }


def _turn_on_relay(context, room_id: str, spec: BridgeSpec) -> bool:
    try:
        return set_room_relay(context, room_id, spec)["ok"]
    except BridgeError:
        return False


def sync_room(context, spec: BridgeSpec, room_id: str) -> dict[str, Any]:
    """Ask the bridge to refresh a room's name, picture and members from the chat."""
    _portal_room(context, spec, room_id)
    replies = _portal_command(context, room_id, "sync-portal", spec)
    return {"room_id": room_id, "replies": replies}


def invite_portal_user(
    context, room_id: str, user_id: str, spec: BridgeSpec = WHATSAPP
) -> dict[str, str]:
    """Invite a Matrix account to a bridged room, through the bridge bot."""
    if not matrix_admin.USER_ID.fullmatch(user_id):
        raise BridgeError("Enter a valid Matrix user ID, such as @alice:example.org.")
    _portal_room(context, spec, room_id)
    _invite_as_bot(context, room_id, user_id, spec)
    return {"room_id": room_id, "user_id": user_id}


def _unbridge(context, spec: BridgeSpec, room_id: str, chat_id: str) -> list[dict[str, Any]]:
    """Detach a chat from its room and wait until the bridge has forgotten the link."""
    if room_id in _bot_rooms(context, spec):
        replies = _portal_command(context, room_id, "unbridge", spec)
    else:
        # The bot cannot read commands in a room it left or that is gone; run the command
        # for that room from the management room instead (bridge admins only).
        replies = _bot_command(context, f"doin {room_id} unbridge", spec)["replies"]
    record = _wait_for_record(
        context, spec, chat_id, lambda current: current is None or current["room_id"] != room_id
    )
    if record is not None and record["room_id"] == room_id:
        detail = _texts(replies)
        raise BridgeError(
            f"The {spec.name} bridge did not detach the room."
            + (f" It answered: {detail[:300]}" if detail else "")
        )
    return replies


def attach_room(
    context, spec: BridgeSpec, chat_id: str, room_id: str, move: bool = False
) -> dict[str, Any]:
    """Link a chat to an existing Matrix room instead of letting the bridge create one."""
    _check_chat_id(spec, chat_id)
    _check_room_id(room_id)
    _require_running(spec)
    if room_id == _load_room(context, spec):
        raise BridgeError("That is the bridge's command chat; choose another room.")
    record = _record(context, spec, chat_id)
    if record is None:
        raise BridgeError(
            f"The {spec.name} bridge has not seen that {spec.chat_noun} yet. Send a message in "
            f"it from {spec.name}, then try again."
        )
    if record["room_id"] == room_id:
        return {
            "room_id": room_id,
            "chat_id": chat_id,
            "relay": record["relay"],
            "admin_promoted": False,
            "moved_from": "",
            "replies": [],
        }
    if _record_for_room(context, spec, room_id) is not None:
        raise BridgeError(f"That room is already linked to another {spec.name} {spec.chat_noun}.")
    if record["room_id"] and not move:
        raise BridgeError(
            f"That {spec.chat_noun} is already linked to another room. Choose “Move” to link "
            "it here instead; its current room is kept for its Matrix members."
        )
    state = _room_state(context, room_id)
    if state is None:
        raise BridgeError("That Matrix room does not exist on this server.")
    if _state_content(state, "m.room.create").get("type") == SPACE:
        raise BridgeError("That is a space, not a room.")

    if _memberships(state).get(_admin_id(context)) != "join":
        _force_join_admin(context, room_id)
    _invite_bot(context, room_id, spec)
    promoted = _grant_bot_power(context, room_id, spec)

    if record["room_id"]:
        _unbridge(context, spec, record["room_id"], chat_id)

    replies = _portal_command(context, room_id, f"bridge {chat_id}", spec)
    linked = _wait_for_record(
        context, spec, chat_id, lambda current: bool(current) and current["room_id"] == room_id
    )
    if not linked or linked["room_id"] != room_id:
        detail = _texts(replies)
        raise BridgeError(
            f"The {spec.name} bridge did not link the room."
            + (f" It answered: {detail[:300]}" if detail else " It did not answer.")
        )
    return {
        "room_id": room_id,
        "chat_id": chat_id,
        "relay": _turn_on_relay(context, room_id, spec),
        "admin_promoted": promoted,
        "moved_from": record["room_id"] or "",
        "replies": replies,
    }


def create_room(context, spec: BridgeSpec, chat_id: str) -> dict[str, Any]:
    """Have the bridge create a new Matrix room for a chat it has seen, now."""
    _check_chat_id(spec, chat_id)
    _require_running(spec)
    record = _record(context, spec, chat_id)
    if record is None:
        raise BridgeError(
            f"The {spec.name} bridge has not seen that {spec.chat_noun} yet. "
            "Send a message in it first."
        )
    if record["room_id"]:
        raise BridgeError(f"That {spec.chat_noun} already has a room.")
    _set_allowed(context, spec, chat_id, True)
    result = _bot_command(context, f"create-portal {chat_id}", spec)
    created = _wait_for_record(
        context, spec, chat_id, lambda current: bool(current and current["room_id"])
    )
    if not created or not created["room_id"]:
        detail = _texts(result["replies"])
        raise BridgeError(
            f"The {spec.name} bridge did not create the room yet. The {spec.chat_noun} is "
            "allowed, so its room appears with its next message."
            + (f" The bot answered: {detail[:300]}" if detail else "")
        )
    return {
        "room_id": created["room_id"],
        "chat_id": chat_id,
        "relay": _turn_on_relay(context, created["room_id"], spec),
        "replies": result["replies"],
    }


def detach_room(context, spec: BridgeSpec, room_id: str, confirmation: str) -> dict[str, Any]:
    """Unlink a room from its chat.

    The room stays for its Matrix members; the chat's remote members and the bot leave it.
    The chat is removed from the allowlist so that the bridge does not create a new room.
    """
    if confirmation != "yes":
        raise BridgeError("Confirm that you want to unlink this room.")
    _require_running(spec)
    room = _portal_room(context, spec, room_id)
    replies = _unbridge(context, spec, room_id, room["chat_id"])
    _set_allowed(context, spec, room["chat_id"], False)
    return {"room_id": room_id, "chat_id": room["chat_id"], "replies": replies}


def repair_link(context, spec: BridgeSpec, chat_id: str, confirmation: str) -> dict[str, Any]:
    """Forget a link to a room that is gone or that the bot left, through the bot itself."""
    if confirmation != "yes":
        raise BridgeError("Confirm that you want to forget this broken link.")
    _check_chat_id(spec, chat_id)
    _require_running(spec)
    broken = next(
        (item for item in overview(context, spec)["broken"] if item["chat_id"] == chat_id), None
    )
    if broken is None:
        raise BridgeError("That link is not broken; refresh the page.")
    replies = _bot_command(context, f"doin {broken['room_id']} unbridge", spec)["replies"]
    record = _wait_for_record(
        context, spec, chat_id, lambda current: not current or not current["room_id"]
    )
    if record and record["room_id"]:
        detail = _texts(replies)
        raise BridgeError(
            f"The {spec.name} bridge kept the broken link."
            + (f" It answered: {detail[:300]}" if detail else "")
        )
    _set_allowed(context, spec, chat_id, False)
    return {"chat_id": chat_id, "replies": replies}


def remove_portal_room(
    context, room_id: str, confirmation: str, spec: BridgeSpec = WHATSAPP
) -> dict[str, str]:
    """Delete a bridged room for everyone, and stop the bridge from re-creating it."""
    if confirmation != "yes":
        raise BridgeError("Confirm that removing this room deletes it for all its members.")
    room = _portal_room(context, spec, room_id)
    _join_portal_as_admin(context, room_id, spec)
    _set_allowed(context, spec, room["chat_id"], False)
    _portal_command(context, room_id, "delete-portal", spec)
    bot = _bot_id(context, spec)
    deadline = time.monotonic() + REPLY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status, members = _matrix(
            context, "GET", f"/_matrix/client/v3/rooms/{_room_path(room_id)}/joined_members"
        )
        if status in (403, 404) or (status == 200 and bot not in members.get("joined", {})):
            return {"room_id": room_id}
        _sleep(POLL_SECONDS)
    raise BridgeError(
        "The chat was removed from the allowlist, but the bridge did not confirm deleting the "
        "room. Check the bridge's logs under Health and logs."
    )


def leave_room(context, spec: BridgeSpec, room_id: str, confirmation: str) -> dict[str, str]:
    """Make the bridge bot leave a room no chat is linked to. The room itself is kept."""
    if confirmation != "yes":
        raise BridgeError("Confirm that the bridge bot should leave this room.")
    _check_room_id(room_id)
    if room_id not in {room["room_id"] for room in overview(context, spec)["leftovers"]}:
        raise BridgeError("That room is linked to a chat, or the bot is not in it.")
    status, reply = _as_bot(
        context, spec, "POST", f"/_matrix/client/v3/rooms/{_room_path(room_id)}/leave", {}
    )
    if status != 200:
        raise BridgeError(f"The bridge bot could not leave the room: {_error(reply, status)}")
    return {"room_id": room_id}


def forget_chat(context, spec: BridgeSpec, chat_id: str, confirmation: str) -> dict[str, bool]:
    """Delete the bridge's record of a chat that has no room, while the bridge is stopped."""
    if confirmation != "yes":
        raise BridgeError("Confirm that you want to forget this chat record.")
    _check_chat_id(spec, chat_id)
    state = _container_state(spec)
    if state is None:
        raise BridgeError("Docker is unavailable; stop the bridge before forgetting records.")
    if state == "running":
        raise BridgeError(
            f"Stop the {spec.name} bridge under Services and updates before forgetting records."
        )
    database = _database_path(context, spec)
    if not database.is_file():
        raise BridgeError(f"The {spec.name} bridge database is missing.")
    _set_allowed(context, spec, chat_id, False)
    try:
        with sqlite3.connect(database, timeout=10) as db:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT receiver FROM portal WHERE id = ? AND mxid IS NULL", (chat_id,)
            ).fetchall()
            if not rows:
                raise BridgeError(
                    "That chat has a room, or the bridge no longer has a record of it."
                )
            for (receiver,) in rows:
                child = db.execute(
                    "SELECT 1 FROM portal WHERE parent_id = ? AND parent_receiver = ? LIMIT 1",
                    (chat_id, receiver),
                ).fetchone()
                if child:
                    raise BridgeError(
                        "Other records depend on this chat; it cannot be forgotten safely."
                    )
            db.execute("DELETE FROM portal WHERE id = ? AND mxid IS NULL", (chat_id,))
            db.commit()
    except sqlite3.Error as exc:
        raise BridgeError(f"Could not forget the {spec.name} chat record.") from exc
    return {"removed": True}


# --- Status and diagnosis ----------------------------------------------------------------------


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


def diagnose(context, spec: BridgeSpec = WHATSAPP) -> list[dict[str, Any]]:
    """Each check says what it found and, when it fails, what to do about it."""
    config_path = context.config_path
    config = (
        (yaml.safe_load(config_path.read_text(encoding="utf-8")) or {})
        if config_path.is_file()
        else {}
    )
    enabled = bool(config.get("services", {}).get(spec.service, {}).get("enabled", False))
    data_dir = context.project_dir / "data" / spec.service
    homeserver = context.project_dir / "data" / "matrix" / "homeserver.yaml"
    registration = context.project_dir / "data" / "matrix" / "appservices" / spec.registration_file
    registration_entry = f"/data/appservices/{spec.registration_file}"
    checks: list[dict[str, Any]] = []

    def add(check_id: str, ok: bool, message: str, fix: str = "") -> None:
        checks.append({"id": check_id, "ok": ok, "message": message, "fix": "" if ok else fix})

    add(
        "enabled",
        enabled,
        f"{spec.name} bridge is enabled." if enabled else f"{spec.name} bridge is turned off.",
        f"Turn {spec.name} on under Services and updates.",
    )
    add(
        "matrix",
        homeserver.is_file(),
        "Matrix is set up." if homeserver.is_file() else "Matrix has not been set up.",
        "Set up Matrix under Services and updates before the bridge.",
    )
    has_config = (data_dir / "config.yaml").is_file()
    add(
        "bridge_config",
        has_config,
        "Bridge configuration exists." if has_config else "Bridge configuration is missing.",
        f"Turn {spec.name} off and on again to regenerate it.",
    )
    if has_config:
        guarded = bridge_config.safeguards_ok(data_dir / "config.yaml", spec)
        extra = (
            " History backfill and eager member import are off." if spec.required_settings else ""
        )
        add(
            "safeguards",
            guarded,
            f"The bridge only creates rooms by itself for chats you allow.{extra}"
            if guarded
            else "The bridge's chat allowlist or privacy settings are not applied.",
            f"Turn {spec.name} off and on again under Services and updates to apply them.",
        )
    has_registration = (data_dir / "registration.yaml").is_file()
    add(
        "bridge_registration",
        has_registration,
        "Bridge registration exists." if has_registration else "Bridge registration is missing.",
        f"Turn {spec.name} off and on again to regenerate it.",
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
        "Matrix knows about the bridge."
        if listed and registration.is_file()
        else "Matrix does not list the bridge's registration.",
        f"Turn {spec.name} off and on again so Matrix is given its registration.",
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
        "Matrix has loaded the bridge's current registration."
        if not stale
        else "Matrix started before the bridge registration was written.",
        "Restart Matrix under Services and updates so it reads the bridge registration.",
    )

    state = _container_state(spec)
    add(
        "container",
        state == "running",
        f"Bridge container is {state}."
        if state and state != "missing"
        else "Bridge container is not running.",
        f"Start {spec.name} under Services and updates, or restart it here.",
    )
    return checks


def status(context, spec: BridgeSpec = WHATSAPP) -> dict[str, Any]:
    """Local state only: no Matrix or bot messages are sent to answer this."""
    checks = diagnose(context, spec)
    by_id = {check["id"]: check for check in checks}
    return {
        "enabled": by_id["enabled"]["ok"],
        "registered_with_synapse": by_id["synapse_registered"]["ok"],
        "container_state": "running" if by_id["container"]["ok"] else "not running",
        "command_chat_created": _load_room(context, spec) is not None,
        "allowed_chats": _allowed_chats(context, spec),
        "safeguards_ok": by_id.get("safeguards", {}).get("ok", False),
        "commands": dict(spec.commands),
        "checks": checks,
        "healthy": all(check["ok"] for check in checks),
    }
