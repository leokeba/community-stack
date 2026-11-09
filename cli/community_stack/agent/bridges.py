"""Operate the mautrix bridges through their Matrix bots, for the admin console.

The bridge's own bot is the only control surface used here: the agent joins a private
direct chat with the bot as the stack's admin account, sends one allowlisted command, and
returns the bot's replies. Linking QR codes are fetched from Synapse and returned once to
the administrator; they are never logged. Every function takes the bridge it works on,
and defaults to WhatsApp so existing callers keep their behaviour.
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

from community_stack import docker, provision
from community_stack.agent import matrix_admin
from community_stack.bridges.catalogue import LOGIN_COMMAND, WHATSAPP, BridgeSpec

# Commands the console may send. Anything else, including logout, stays a CLI-only action.
GROUP_ID_PATTERN = provision.WHATSAPP_GROUP_ID_PATTERN
COMMANDS: dict[str, str] = dict(WHATSAPP.commands)
COMMAND_PATTERN = re.compile("|".join(re.escape(command) for command in COMMANDS))
# A login flow named by the bot, for bridges whose flows are not fixed in the catalogue.
LOGIN_FLOW_PATTERN = re.compile(rf"{LOGIN_COMMAND} [a-z][a-z0-9-]{{0,31}}")
NOT_MANAGEMENT_ROOM = "not your management room"
REPLY_TIMEOUT_SECONDS = 25.0
REPLY_QUIET_SECONDS = 4.0
POLL_SECONDS = 2.0
JOIN_TIMEOUT_SECONDS = 20.0
MAX_TEXT_CHARS = 4000
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
NOT_LINKED_MARKERS = ("not logged in", "no user logins", "no logins")
INVITE_LINK_PATTERN = re.compile(
    r"https://chat\.whatsapp\.com/[A-Za-z0-9_-]{10,80}(?:\?[A-Za-z0-9=&_-]+)?"
)
# Replaceable in tests.
_sleep: Callable[[float], None] = time.sleep


class BridgeError(Exception):
    """A bridge action the agent refused or could not finish; shown to people."""


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


def _state_path(context, spec: BridgeSpec = WHATSAPP) -> Path:
    return context.project_dir / "data" / "agent" / "bridges" / f"{spec.key}.json"


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
        context, "GET", f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/joined_members"
    )
    if status != 200:
        return None
    return set(reply.get("joined", {}))


def _room_name(context, room_id: str) -> str:
    status, state = _matrix(
        context, "GET", f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state/m.room.name"
    )
    return (state.get("name") or "") if status == 200 else ""


def _appservice_token(context, spec: BridgeSpec) -> tuple[str, str]:
    path = context.project_dir / "data" / spec.service / "registration.yaml"
    try:
        registration = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise BridgeError(f"The {spec.name} appservice registration cannot be read.") from exc
    if not isinstance(registration, dict):
        raise BridgeError(f"The {spec.name} appservice registration is invalid.")

    token = registration.get("as_token")
    bot = f"@{spec.bot_localpart}:{_server_name(context)}"
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


def _appservice_request(
    context,
    method: str,
    path: str,
    body: Any,
    token: str,
    bot: str,
    spec: BridgeSpec,
) -> tuple[int, Any]:
    separator = "&" if "?" in path else "?"
    path = f"{path}{separator}{urlencode({'user_id': bot})}"
    try:
        return matrix_admin.synapse_request(method, path, body, token)
    except matrix_admin.MatrixAdminError as exc:
        raise BridgeError(str(exc)) from exc


def _room_state(
    context, room_id: str, token: str, bot: str, spec: BridgeSpec
) -> list[dict[str, Any]] | None:
    status, state = _appservice_request(
        context,
        "GET",
        f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state",
        None,
        token,
        bot,
        spec,
    )
    if status == 404:
        return None
    if (
        status != 200
        or not isinstance(state, list)
        or not all(isinstance(event, dict) for event in state)
    ):
        detail = state.get("error", status) if isinstance(state, dict) else status
        raise BridgeError(f"Synapse could not inspect bridged room {room_id}: {detail}")
    return state


def _invite_as_bot(context, room_id: str, user_id: str, spec: BridgeSpec) -> None:
    token, bot = _appservice_token(context, spec)
    status, reply = _appservice_request(
        context,
        "POST",
        f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/invite",
        {"user_id": user_id},
        token,
        bot,
        spec,
    )
    if status != 200:
        detail = reply.get("error", status) if isinstance(reply, dict) else status
        raise BridgeError(f"Synapse could not invite {user_id} to the room: {detail}")


def _ensure_stack_admin_joined(
    context, room_id: str, membership: str | None, spec: BridgeSpec
) -> tuple[str, str]:
    if membership in {"join", "leave", "ban"}:
        return membership, ""

    admin = f"@{matrix_admin.ADMIN_LOCALPART}:{_server_name(context)}"
    try:
        if membership != "invite":
            _invite_as_bot(context, room_id, admin, spec)
        status, reply = _matrix(
            context,
            "POST",
            f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/join",
        )
        if status != 200:
            detail = reply.get("error", status) if isinstance(reply, dict) else status
            raise BridgeError(f"Synapse could not add the stack admin to the room: {detail}")
        return "join", ""
    except BridgeError as exc:
        return membership or "not_joined", str(exc)


def _whatsapp_portal_access(context, spec: BridgeSpec) -> dict[str, bool]:
    database = context.project_dir / "data" / spec.service / "mautrix-whatsapp.db"
    if not database.is_file():
        return {}
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            records = db.execute(
                "SELECT id, mxid FROM portal WHERE mxid IS NOT NULL AND id GLOB '[0-9]*@g.us'"
            ).fetchall()
    except sqlite3.Error as exc:
        raise BridgeError("Could not read the WhatsApp bridge portal database.") from exc

    allowed_groups = set(_whatsapp_allowed_groups(context))
    portal_access: dict[str, bool] = {}
    for group_id, room_id in records:
        canonical_room_id = _canonical_portal_room_id(context, room_id)
        if (
            not isinstance(group_id, str)
            or not GROUP_ID_PATTERN.fullmatch(group_id)
            or canonical_room_id is None
        ):
            continue
        portal_access[canonical_room_id] = group_id in allowed_groups
    return portal_access


def _whatsapp_allowed_groups(context) -> list[str]:
    config_path = context.project_dir / "data" / WHATSAPP.service / "config.yaml"
    if config_path.is_file():
        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise BridgeError("Could not read the WhatsApp bridge allowlist.") from exc
    else:
        config = {}
    if not isinstance(config, dict):
        raise BridgeError("The WhatsApp bridge configuration is invalid.")
    bridge_config = config.get("bridge", {})
    portal_filter = (
        bridge_config.get("portal_create_filter", {}) if isinstance(bridge_config, dict) else {}
    )
    allowed = portal_filter.get("list", []) if isinstance(portal_filter, dict) else []
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        raise BridgeError("The WhatsApp bridge portal allowlist is invalid.")
    return allowed if portal_filter.get("mode") == "allow" else []


def _canonical_portal_room_id(context, room_id: Any) -> str | None:
    if not isinstance(room_id, str):
        return None
    if room_id.startswith("!") and ":" not in room_id:
        room_id = f"{room_id}:{_server_name(context)}"
    if not re.fullmatch(r"![A-Za-z0-9._=~-]{1,255}:[A-Za-z0-9.:-]{1,255}", room_id):
        return None
    return room_id


def _matrix_room_admin_status(context, room_id: str) -> int:
    try:
        status, _ = matrix_admin._call(
            context, "GET", f"/_synapse/admin/v1/rooms/{quote(room_id, safe='')}"
        )
    except matrix_admin.MatrixAdminError as exc:
        raise BridgeError("Could not verify a WhatsApp portal's Matrix room.") from exc
    if status not in (200, 404):
        raise BridgeError("Could not verify a WhatsApp portal's Matrix room.")
    return status


def ensure_direct_room(context, spec: BridgeSpec = WHATSAPP) -> str:
    """The admin's direct chat with the bridge bot, created once and then reused."""
    bot = f"@{spec.bot_localpart}:{_server_name(context)}"
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


def _send(context, room_id: str, text: str) -> str:
    txn = secrets.token_hex(8)
    path = f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/send/m.room.message/{txn}"
    status, reply = _matrix(context, "PUT", path, {"msgtype": "m.text", "body": text})
    if status != 200 or "event_id" not in reply:
        raise BridgeError("Synapse did not accept the command.")
    return reply["event_id"]


def _redact_command_event(context, room_id: str, event_id: str) -> None:
    txn = secrets.token_hex(8)
    path = (
        f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/redact/"
        f"{quote(event_id, safe='')}/{txn}"
    )
    status, _ = _matrix(context, "PUT", path, {"reason": "Remove a WhatsApp invite link"})
    if status != 200:
        raise BridgeError(
            "Synapse could not redact the invite link from the bridge chat. Revoke that "
            "WhatsApp invite link before trying again."
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
    return [_reply_view(context, event, spec) for event in reversed(seen)]


def run_command(
    context, command: str, spec: BridgeSpec = WHATSAPP, redact_sensitive: bool = False
) -> dict[str, Any]:
    """Send one allowlisted command to the bridge bot and return its replies."""
    sensitive_prefixes = (
        "resolve-link https://chat.whatsapp.com/",
        "join https://chat.whatsapp.com/",
    )
    sensitive_command = (
        redact_sensitive
        and spec.key == WHATSAPP.key
        and command.startswith(sensitive_prefixes)
        and bool(INVITE_LINK_PATTERN.fullmatch(command.partition(" ")[2]))
    )
    if not is_allowed_command(spec, command) and not sensitive_command:
        raise BridgeError("That command is not available from the console.")
    bot = f"@{spec.bot_localpart}:{_server_name(context)}"
    room_id = ensure_direct_room(context, spec)

    def send_and_collect(current_room: str) -> list[dict[str, Any]]:
        event_id = _send(context, current_room, command)
        try:
            return _collect_replies(context, current_room, event_id, bot, spec)
        finally:
            if sensitive_command:
                _redact_command_event(context, current_room, event_id)

    replies = send_and_collect(room_id)
    if replies and NOT_MANAGEMENT_ROOM in replies[0]["text"]:
        # The bridge keeps its own record of the management room; if it was reset, the cached
        # chat is stale. Open a fresh chat once, which the bridge marks as its management room.
        _forget_room(context, spec)
        room_id = ensure_direct_room(context, spec)
        replies = send_and_collect(room_id)
    return {
        "command": command,
        "replies": replies,
        "note": ""
        if replies
        else "The bridge bot did not answer. Check that the bridge is running, then try again.",
    }


def _restart_bridge(context, spec: BridgeSpec) -> None:
    result = docker.run_compose(
        context.services_dir / spec.service,
        ["restart", spec.service],
        env_file=context.env_path,
        capture_output=True,
    )
    if result.returncode != 0:
        raise BridgeError(
            f"The {spec.name} configuration was updated, but the bridge could not restart. "
            "Stop it from the Services page until the issue is resolved."
        )
    from community_stack.agent import settings

    try:
        settings.wait_for(context, spec.service, True)
    except settings.SettingsError as exc:
        raise BridgeError(
            f"The {spec.name} bridge did not become healthy after its configuration changed. "
            "Stop it from the Services page until the issue is resolved."
        ) from exc


def add_whatsapp_group(context, invite_link: str) -> dict[str, str]:
    """Resolve and allow one group, then join it without retaining its invite link."""
    invite_link = invite_link.strip()
    if not INVITE_LINK_PATTERN.fullmatch(invite_link):
        raise BridgeError("Enter a valid WhatsApp group invite link.")
    if not docker.is_docker_available():
        raise BridgeError("Docker is unavailable; start the WhatsApp bridge from Services first.")
    container = next(
        (
            item
            for item in docker.list_community_containers()
            if item.get("name") == WHATSAPP.container
        ),
        None,
    )
    if not container or container.get("state") != "running":
        raise BridgeError("Start the WhatsApp bridge from the Services page first.")

    config_path = context.project_dir / "data" / WHATSAPP.service / "config.yaml"
    try:
        privacy_changed = provision.ensure_whatsapp_privacy_config(config_path)
    except provision.ProvisionError as exc:
        raise BridgeError(str(exc)) from exc
    if privacy_changed:
        _restart_bridge(context, WHATSAPP)

    resolved = run_command(context, f"resolve-link {invite_link}", redact_sensitive=True)
    resolved_text = "\n".join(
        reply["text"] for reply in resolved["replies"] if reply["kind"] == "text"
    )
    match = re.search(
        rf"That invite link points at (.+?) \(`?({GROUP_ID_PATTERN.pattern})`?\)", resolved_text
    )
    if not match:
        raise BridgeError("The bridge could not resolve that WhatsApp group invite link.")
    group_name, group_id = match.groups()

    try:
        allowlist_changed = provision.allow_whatsapp_group(config_path, group_id)
    except provision.ProvisionError as exc:
        raise BridgeError(str(exc)) from exc
    if allowlist_changed:
        _restart_bridge(context, WHATSAPP)

    joined = run_command(context, f"join {invite_link}", redact_sensitive=True)
    joined_text = "\n".join(reply["text"] for reply in joined["replies"] if reply["kind"] == "text")
    if f"Successfully joined group `{group_id}`" not in joined_text:
        raise BridgeError(
            f"{group_name} is allowlisted, but the bridge did not confirm joining it. "
            "Check the bridge status and retry."
        )
    return {"group_id": group_id, "name": group_name}


def disallow_whatsapp_group(context, group_id: str) -> dict[str, bool]:
    """Revoke one group's permission and reload the running bridge if needed."""
    if not GROUP_ID_PATTERN.fullmatch(group_id):
        raise BridgeError("The WhatsApp group ID is invalid.")
    if not docker.is_docker_available():
        raise BridgeError("Docker is unavailable; cannot safely apply the group permission change.")
    container = next(
        (
            item
            for item in docker.list_community_containers()
            if item.get("name") == WHATSAPP.container
        ),
        None,
    )
    config_path = context.project_dir / "data" / WHATSAPP.service / "config.yaml"
    try:
        changed = provision.disallow_whatsapp_group(config_path, group_id)
    except provision.ProvisionError as exc:
        raise BridgeError(str(exc)) from exc
    if changed and container and container.get("state") == "running":
        _restart_bridge(context, WHATSAPP)
    return {"allowed": False}


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
        f"Turn {spec.name} on under Services.",
    )
    add(
        "matrix",
        homeserver.is_file(),
        "Matrix is set up." if homeserver.is_file() else "Matrix has not been set up.",
        "Set up Matrix on the Services page before the bridge.",
    )
    has_config = (data_dir / "config.yaml").is_file()
    add(
        "bridge_config",
        has_config,
        "Bridge configuration exists." if has_config else "Bridge configuration is missing.",
        f"Turn {spec.name} off and on again to regenerate it.",
    )
    if spec.key == WHATSAPP.key and has_config:
        try:
            bridge_config = (
                yaml.safe_load((data_dir / "config.yaml").read_text(encoding="utf-8")) or {}
            )
            history_sync = bridge_config.get("network", {}).get("history_sync", {})
            network_settings = bridge_config.get("network", {})
            bridge_settings = bridge_config.get("bridge", {})
            portal_filter = bridge_settings.get("portal_create_filter", {})
            privacy_ok = (
                history_sync.get("max_initial_conversations") == 0
                and history_sync.get("request_full_sync") is False
                and network_settings.get("max_member_sync") == 0
                and portal_filter.get("mode") == "allow"
                and isinstance(portal_filter.get("list"), list)
            )
        except (OSError, AttributeError, yaml.YAMLError):
            privacy_ok = False
        add(
            "privacy_guard",
            privacy_ok,
            "History backfill is off; eager non-admin group member sync is disabled; portal creation is allow-only. Standalone contact/profile sync may still create ghosts."
            if privacy_ok
            else "WhatsApp history, eager member-sync, or portal allowlist safeguards are not applied.",
            "Keep the bridge stopped, then turn it off and on from Services to apply the safeguards.",
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
        "Synapse lists the bridge registration."
        if listed and registration.is_file()
        else "Synapse does not list the bridge registration.",
        f"Turn {spec.name} off and on again so Synapse is given its registration.",
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
    container = next((c for c in containers if c.get("name") == spec.container), None)
    running = bool(container) and container.get("state") == "running"
    add(
        "container",
        running,
        f"Bridge container is {container.get('state')}."
        if container
        else "Bridge container is not running.",
        f"Start {spec.name} on the Services page.",
    )
    return checks


def portal_rooms(context, spec: BridgeSpec = WHATSAPP) -> list[dict[str, Any]]:
    """List rooms joined by the bridge bot, independent of the stack admin's membership."""
    token, bot = _appservice_token(context, spec)
    status, joined = _appservice_request(
        context, "GET", "/_matrix/client/v3/joined_rooms", None, token, bot, spec
    )
    if status != 200:
        detail = joined.get("error", status) if isinstance(joined, dict) else status
        raise BridgeError(f"Synapse did not list rooms for the {spec.name} bot: {detail}")
    room_ids = joined.get("joined_rooms") if isinstance(joined, dict) else None
    if not isinstance(room_ids, list):
        raise BridgeError(f"Synapse returned an invalid room list for the {spec.name} bot.")

    command_room = _load_room(context, spec)
    whatsapp_access = _whatsapp_portal_access(context, spec) if spec.key == WHATSAPP.key else {}
    admin = f"@{matrix_admin.ADMIN_LOCALPART}:{_server_name(context)}"
    rooms: list[dict[str, Any]] = []
    for room_id in room_ids:
        if not isinstance(room_id, str) or not re.fullmatch(
            r"![A-Za-z0-9._=~-]{1,255}:[A-Za-z0-9.:-]{1,255}", room_id
        ):
            continue
        if room_id == command_room:
            continue
        if spec.key == WHATSAPP.key and room_id not in whatsapp_access:
            continue
        state = _room_state(context, room_id, token, bot, spec)
        if state is None:
            continue
        memberships = {
            event.get("state_key"): event.get("content", {}).get("membership")
            for event in state
            if event.get("type") == "m.room.member"
        }
        if memberships.get(bot) != "join":
            continue
        name_event = next((event for event in state if event.get("type") == "m.room.name"), {})
        name = name_event.get("content", {}).get("name") or ""
        if name == command_chat_name(spec):
            continue
        is_allowed = whatsapp_access.get(room_id, True)
        access_error = ""
        admin_membership = memberships.get(admin)
        if spec.key == WHATSAPP.key and is_allowed:
            admin_membership, access_error = _ensure_stack_admin_joined(
                context, room_id, admin_membership, spec
            )
            if admin_membership == "join":
                memberships[admin] = "join"
        access_labels = {
            "join": "Joined",
            "invite": "Invite pending",
            "leave": "Not joined",
            "ban": "Banned",
        }
        rooms.append(
            {
                "room_id": room_id,
                "name": name or "(unnamed room)",
                "members": sum(value == "join" for value in memberships.values()),
                "allowed": is_allowed,
                "admin_access": access_labels.get(admin_membership, "Not joined"),
                "access_error": access_error,
            }
        )
    return sorted(rooms, key=lambda room: room["name"].lower())


def invite_portal_user(
    context, room_id: str, user_id: str, spec: BridgeSpec = WHATSAPP
) -> dict[str, str]:
    """Invite a Matrix account to one allowlisted WhatsApp portal through the bridge bot."""
    if not matrix_admin.USER_ID.fullmatch(user_id):
        raise BridgeError("Enter a valid Matrix user ID, such as @alice:example.org.")
    room = next((item for item in portal_rooms(context, spec) if item["room_id"] == room_id), None)
    if not room:
        raise BridgeError(f"That room is not an active {spec.name} bridge portal.")
    if not room.get("allowed"):
        raise BridgeError("Only allowlisted WhatsApp rooms can invite new Matrix accounts.")
    _invite_as_bot(context, room_id, user_id, spec)
    return {"room_id": room_id, "user_id": user_id}


def orphaned_portals(context) -> list[dict[str, str]]:
    """List saved WhatsApp group portals that have no Matrix room."""
    database = context.project_dir / "data" / WHATSAPP.service / "mautrix-whatsapp.db"
    if not database.is_file():
        return []
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT id, name FROM portal WHERE mxid IS NULL AND id GLOB '[0-9]*@g.us'"
            ).fetchall()
    except sqlite3.Error as exc:
        raise BridgeError("Could not read saved WhatsApp group records.") from exc
    return [
        {
            "group_id": group_id,
            "name": name.strip()
            if name and name.strip()
            else "WhatsApp group without a Matrix room",
        }
        for group_id, name in rows
        if GROUP_ID_PATTERN.fullmatch(group_id)
    ]


def stale_portals(context) -> list[dict[str, str]]:
    """List allowlisted groups whose saved Matrix room has been deleted."""
    database = context.project_dir / "data" / WHATSAPP.service / "mautrix-whatsapp.db"
    if not database.is_file():
        return []
    allowed_groups = set(_whatsapp_allowed_groups(context))
    if not allowed_groups:
        return []
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT id, mxid, name FROM portal WHERE mxid IS NOT NULL AND id GLOB '[0-9]*@g.us'"
            ).fetchall()
    except sqlite3.Error as exc:
        raise BridgeError("Could not read saved WhatsApp portal records.") from exc

    states: dict[str, dict[str, Any]] = {}
    for group_id, room_id, name in rows:
        if not isinstance(group_id, str) or not GROUP_ID_PATTERN.fullmatch(group_id):
            continue
        if group_id not in allowed_groups:
            continue
        canonical_room_id = _canonical_portal_room_id(context, room_id)
        if canonical_room_id is None:
            raise BridgeError("A saved WhatsApp portal has an invalid Matrix room ID.")
        status = _matrix_room_admin_status(context, canonical_room_id)
        portal = states.setdefault(
            group_id,
            {
                "name": name.strip()
                if isinstance(name, str) and name.strip()
                else "WhatsApp group",
                "missing": 0,
                "live": False,
            },
        )
        if status == 404:
            portal["missing"] += 1
        else:
            portal["live"] = True

    return [
        {"group_id": group_id, "name": portal["name"]}
        for group_id, portal in states.items()
        if portal["missing"] and not portal["live"]
    ]


def reset_stale_portal(context, group_id: str, confirmation: str) -> dict[str, int]:
    """Clear deleted Matrix room references for one allowlisted group while its bridge is stopped."""
    if confirmation != "yes":
        raise BridgeError("Confirm resetting the stale Matrix room mapping.")
    if not GROUP_ID_PATTERN.fullmatch(group_id):
        raise BridgeError("That saved WhatsApp group record is invalid.")
    if not docker.is_docker_available():
        raise BridgeError(
            "Docker is unavailable; stop the WhatsApp bridge before resetting this mapping."
        )
    container = next(
        (
            item
            for item in docker.list_community_containers()
            if item.get("name") == WHATSAPP.container
        ),
        None,
    )
    if container and container.get("state") == "running":
        raise BridgeError("Stop the WhatsApp bridge from Services before resetting this mapping.")
    if group_id not in _whatsapp_allowed_groups(context):
        raise BridgeError("Only an allowlisted WhatsApp group can have its room mapping reset.")

    database = context.project_dir / "data" / WHATSAPP.service / "mautrix-whatsapp.db"
    if not database.is_file():
        raise BridgeError("The WhatsApp bridge database is missing.")
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            mapped_rows = db.execute(
                "SELECT receiver, mxid FROM portal "
                "WHERE id = ? AND mxid IS NOT NULL ORDER BY receiver, mxid",
                (group_id,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise BridgeError("Could not read the saved WhatsApp portal mapping.") from exc
    if not mapped_rows:
        raise BridgeError("That WhatsApp group has no saved Matrix room mapping.")

    for _, room_id in mapped_rows:
        canonical_room_id = _canonical_portal_room_id(context, room_id)
        if canonical_room_id is None:
            raise BridgeError("The saved WhatsApp portal has an invalid Matrix room ID.")
        if _matrix_room_admin_status(context, canonical_room_id) != 404:
            raise BridgeError("The saved Matrix room still exists; refusing to reset its mapping.")

    try:
        with sqlite3.connect(database, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            current_rows = db.execute(
                "SELECT receiver, mxid FROM portal "
                "WHERE id = ? AND mxid IS NOT NULL ORDER BY receiver, mxid",
                (group_id,),
            ).fetchall()
            if current_rows != mapped_rows:
                raise BridgeError("The saved portal mapping changed; refresh and try again.")
            cursor = db.execute(
                "UPDATE portal SET mxid = NULL WHERE id = ? AND mxid IS NOT NULL", (group_id,)
            )
            if cursor.rowcount != len(mapped_rows):
                raise BridgeError("The saved portal mapping changed; refresh and try again.")
            db.commit()
    except sqlite3.Error as exc:
        raise BridgeError("Could not reset the stale WhatsApp portal mapping.") from exc
    return {"reset": len(mapped_rows)}


def remove_orphaned_portal(context, group_id: str, confirmation: str) -> dict[str, str]:
    """Remove a saved group record only when it has no Matrix room and the bridge is stopped."""
    if confirmation != "yes":
        raise BridgeError("Confirm removal of this saved group record.")
    if not GROUP_ID_PATTERN.fullmatch(group_id):
        raise BridgeError("That saved WhatsApp group record is invalid.")
    if not docker.is_docker_available():
        raise BridgeError(
            "Docker is unavailable; stop the WhatsApp bridge before removing this record."
        )
    container = next(
        (
            item
            for item in docker.list_community_containers()
            if item.get("name") == WHATSAPP.container
        ),
        None,
    )
    if container and container.get("state") == "running":
        raise BridgeError(
            "Stop the WhatsApp bridge from Services before removing this saved record."
        )

    database = context.project_dir / "data" / WHATSAPP.service / "mautrix-whatsapp.db"
    if not database.is_file():
        raise BridgeError("The WhatsApp bridge database is missing.")
    config_path = context.project_dir / "data" / WHATSAPP.service / "config.yaml"
    try:
        provision.disallow_whatsapp_group(config_path, group_id)
        with sqlite3.connect(database, timeout=10) as db:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT receiver FROM portal WHERE id = ? AND mxid IS NULL", (group_id,)
            ).fetchall()
            if not rows:
                raise BridgeError("That saved group has a Matrix room or no longer exists.")
            for (receiver,) in rows:
                child = db.execute(
                    "SELECT 1 FROM portal WHERE parent_id = ? AND parent_receiver = ? LIMIT 1",
                    (group_id, receiver),
                ).fetchone()
                if child:
                    raise BridgeError(
                        "This saved group has dependent portals and cannot be removed safely."
                    )
            db.execute("DELETE FROM portal WHERE id = ? AND mxid IS NULL", (group_id,))
            db.commit()
    except provision.ProvisionError as exc:
        raise BridgeError(str(exc)) from exc
    except sqlite3.Error as exc:
        raise BridgeError("Could not remove the saved WhatsApp group record.") from exc
    return {"removed": True}


def remove_portal_room(context, room_id: str, confirmation: str) -> dict[str, str]:
    """Unallow and delete a bridged WhatsApp portal room after explicit confirmation."""
    if confirmation != "yes":
        raise BridgeError("Confirm that removing this portal will also delete its Matrix room.")
    if room_id not in {room["room_id"] for room in portal_rooms(context)}:
        raise BridgeError("That room is not a bridged WhatsApp room.")
    database = context.project_dir / "data" / WHATSAPP.service / "mautrix-whatsapp.db"
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT id FROM portal WHERE mxid = ? AND id GLOB '[0-9]*@g.us' LIMIT 1",
                (room_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise BridgeError("Could not identify the WhatsApp group for this room.") from exc
    if not row or not GROUP_ID_PATTERN.fullmatch(row[0]):
        raise BridgeError("Could not safely identify the WhatsApp group for this room.")

    group_id = row[0]
    config_path = context.project_dir / "data" / WHATSAPP.service / "config.yaml"
    try:
        provision.disallow_whatsapp_group(config_path, group_id)
    except provision.ProvisionError as exc:
        raise BridgeError(str(exc)) from exc

    event_id = _send(context, room_id, "delete-portal")
    deadline = time.monotonic() + REPLY_TIMEOUT_SECONDS
    bot = f"@{WHATSAPP.bot_localpart}:{_server_name(context)}"
    while time.monotonic() < deadline:
        status, members = _matrix(
            context,
            "GET",
            f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/joined_members",
        )
        if status == 404 or (status == 200 and bot not in members.get("joined", {})):
            _restart_bridge(context, WHATSAPP)
            return {"room_id": room_id, "event_id": event_id}
        _sleep(POLL_SECONDS)
    raise BridgeError(
        "The group was removed from the allowlist, but the bridge did not confirm portal deletion. "
        "Keep the bridge stopped and check its status."
    )


def login_state(context, spec: BridgeSpec = WHATSAPP) -> dict[str, Any]:
    """Whether an account is linked, judged from the bot's own answer to list-logins."""
    result = run_command(context, "list-logins", spec)
    texts = [reply["text"] for reply in result["replies"] if reply["kind"] == "text"]
    if not texts:
        return {"known": False, "linked": False, "detail": result["note"]}
    detail = " ".join(texts)
    linked = not any(marker in detail.lower() for marker in NOT_LINKED_MARKERS)
    return {"known": True, "linked": linked, "detail": detail[:MAX_TEXT_CHARS]}


def set_room_relay(context, room_id: str, spec: BridgeSpec = WHATSAPP) -> dict[str, Any]:
    """Make the linked service account relay for one bridged room: only rooms the bridge made."""
    if room_id not in {room["room_id"] for room in portal_rooms(context, spec)}:
        raise BridgeError(f"That room is not a bridged {spec.name} room.")
    bot = f"@{spec.bot_localpart}:{_server_name(context)}"
    event_id = _send(context, room_id, "set-relay")
    replies = _collect_replies(context, room_id, event_id, bot, spec)
    return {
        "room_id": room_id,
        "replies": replies,
        "note": ""
        if replies
        else "The bridge bot did not answer. Check that the bridge is running, then try again.",
    }


def status(context, spec: BridgeSpec = WHATSAPP) -> dict[str, Any]:
    """Local state only: no Matrix or bot messages are sent to answer this."""
    checks = diagnose(context, spec)
    by_id = {check["id"]: check for check in checks}
    container = by_id["container"]
    privacy_guard = by_id.get("privacy_guard", {}).get("ok", False)
    allowed_groups: list[str] = []
    if spec.key == WHATSAPP.key:
        config_path = context.project_dir / "data" / spec.service / "config.yaml"
        if config_path.is_file():
            try:
                config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise BridgeError("Could not read the WhatsApp bridge configuration.") from exc
            allowed = config.get("bridge", {}).get("portal_create_filter", {}).get("list", [])
            if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
                raise BridgeError("The WhatsApp bridge portal allowlist is invalid.")
            allowed_groups = allowed
    return {
        "enabled": by_id["enabled"]["ok"],
        "registered_with_synapse": by_id["synapse_registered"]["ok"],
        "container_state": "running" if container["ok"] else "not running",
        "container_status": "",
        "command_chat_created": _load_room(context, spec) is not None,
        "allowed_groups": allowed_groups,
        "privacy_guard_ok": privacy_guard,
        "orphaned_portals": orphaned_portals(context) if spec.key == WHATSAPP.key else [],
        "commands": dict(spec.commands),
        "checks": checks,
        "healthy": all(check["ok"] for check in checks),
    }
