"""The bridge console: command chat, rooms, linking chats to rooms, relay and repairs.

A fake Synapse models what the agent relies on: room state through the admin API, room
membership and power levels, the appservice bot identity, and a bridge bot that answers
commands the way mautrix bridgev2 does and records links in a real portal table.
"""

import base64
import json
import re
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import yaml
from community_stack import docker
from community_stack.admin import bridges as console
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.agent import bridges, matrix_admin
from community_stack.agent.operations import CATALOG, AgentContext
from community_stack.agent.server import AgentService, AgentSocketServer
from community_stack.bridges.catalogue import MESSENGER, SIGNAL, WHATSAPP
from fastapi.testclient import TestClient

SERVER = "community.test"
BOT = f"@whatsappbot:{SERVER}"
ADMIN = f"@community-stack-admin:{SERVER}"
ALICE = f"@alice:{SERVER}"
# Room version 12 IDs have no server part; older rooms do. Both must work everywhere.
PORTAL = "!rYmCpUwG1PT34NH83HMrUJVPHjXSJttm7DV2jm7Cjjw"
EXISTING = "!existing:community.test"
GROUP = "120363411717803463@g.us"
OTHER_GROUP = "120363434598470447@g.us"
INVITE = "https://chat.whatsapp.com/abcdefghijklmnopqrstuv"
LOGIN = "33600000000"
PASSWORD = "a-long-safe-test-password"
PNG = b"\x89PNG\r\n\x1a\nfake-qr"
AS_TOKEN = "appservice-secret"
ADMIN_TOKEN = "stack-admin-token"


class FakeSynapse:
    """Just enough Synapse, and a bridge bot, for the agent's bridge code."""

    def __init__(self, database: Path):
        self.database = database
        self.rooms: dict[str, dict] = {}
        self.requests: list[tuple] = []
        self.sent: list[tuple[str, str]] = []
        self.bot_answers = True
        self.counter = 0

    # --- rooms ---------------------------------------------------------------------------

    def add_room(
        self, room_id, name, creator=ADMIN, version="12", members=None, levels=None, space=False
    ):
        self.rooms[room_id] = {
            "name": name,
            "creator": creator,
            "version": version,
            "space": space,
            "members": dict(members or {creator: "join"}),
            "levels": levels,
            "events": [],
        }
        return room_id

    def state(self, room_id):
        room = self.rooms[room_id]
        create = {"room_version": room["version"]}
        if room["space"]:
            create["type"] = "m.space"
        state = [
            {
                "type": "m.room.create",
                "state_key": "",
                "sender": room["creator"],
                "content": create,
            },
            {"type": "m.room.name", "state_key": "", "content": {"name": room["name"]}},
        ]
        state += [
            {"type": "m.room.member", "state_key": user, "content": {"membership": membership}}
            for user, membership in room["members"].items()
        ]
        if room["levels"] is not None:
            state.append(
                {"type": "m.room.power_levels", "state_key": "", "content": room["levels"]}
            )
        return state

    def power(self, room_id, user):
        room = self.rooms[room_id]
        if room["version"] == "12" and user == room["creator"]:
            return 1000
        levels = room["levels"] or {}
        return levels.get("users", {}).get(user, levels.get("users_default", 0))

    # --- the bridge's database ----------------------------------------------------------------

    def db(self):
        return sqlite3.connect(self.database)

    def record(self, chat_id):
        with self.db() as db:
            row = db.execute(
                "SELECT mxid, relay_login_id FROM portal WHERE id = ? AND receiver = ''", (chat_id,)
            ).fetchone()
        return {"room_id": row[0], "relay": row[1]} if row else None

    def link(self, chat_id, room_id):
        with self.db() as db:
            db.execute("UPDATE portal SET mxid = ? WHERE id = ?", (room_id, chat_id))

    def new_portal_room(self, chat_id):
        room_id = f"!created{self.counter}"
        self.counter += 1
        self.add_room(room_id, "Neighbours", creator=BOT, members={BOT: "join", ADMIN: "invite"})
        self.link(chat_id, room_id)
        return room_id

    # --- requests ------------------------------------------------------------------------

    def __call__(self, method, path, body=None, token=None):
        self.requests.append((method, path, body, token))
        parsed = urlsplit(path)
        route = parsed.path
        query = parse_qs(parsed.query)
        actor = query.get("user_id", [None])[0] if token == AS_TOKEN else ADMIN

        def room_from(pattern):
            match = re.fullmatch(pattern, route)
            return unquote(match.group(1)) if match else None

        if route == "/_matrix/client/v3/createRoom":
            room_id = "!commands:community.test"
            self.add_room(room_id, body["name"], members={ADMIN: "join", BOT: "join"})
            return 200, {"room_id": room_id}
        if route == "/_matrix/client/v3/joined_rooms":
            rooms = [r for r, room in self.rooms.items() if room["members"].get(actor) == "join"]
            return 200, {"joined_rooms": rooms}
        if route == "/_synapse/admin/v1/rooms":
            return 200, {
                "rooms": [
                    {
                        "room_id": room_id,
                        "name": room["name"],
                        "joined_members": sum(m == "join" for m in room["members"].values()),
                        "room_type": "m.space" if room["space"] else None,
                    }
                    for room_id, room in self.rooms.items()
                ]
            }
        if room_id := room_from(r"/_synapse/admin/v1/rooms/([^/]+)/state"):
            if room_id not in self.rooms:
                return 404, {"errcode": "M_NOT_FOUND"}
            return 200, {"state": self.state(room_id)}
        if room_id := room_from(r"/_synapse/admin/v1/join/([^/]+)"):
            self.rooms[room_id]["members"][body["user_id"]] = "join"
            return 200, {"room_id": room_id}
        if room_id := room_from(r"/_synapse/admin/v1/rooms/([^/]+)/make_room_admin"):
            room = self.rooms[room_id]
            room["levels"] = {
                **(room["levels"] or {}),
                "users": {**(room["levels"] or {}).get("users", {}), body["user_id"]: 100},
            }
            return 200, {}
        if room_id := room_from(r"/_matrix/client/v3/rooms/([^/]+)/invite"):
            room = self.rooms.get(room_id)
            if room is None or room["members"].get(actor) != "join":
                return 403, {"error": "You are not in the room"}
            # The bridge bot accepts invitations from the bridge's admins straight away.
            room["members"][body["user_id"]] = "join" if body["user_id"] == BOT else "invite"
            return 200, {}
        if room_id := room_from(r"/_matrix/client/v3/rooms/([^/]+)/join"):
            room = self.rooms.get(room_id)
            if room is None or room["members"].get(actor) not in {"invite", "join"}:
                return 403, {"error": "You are not invited"}
            room["members"][actor] = "join"
            return 200, {"room_id": room_id}
        if room_id := room_from(r"/_matrix/client/v3/rooms/([^/]+)/leave"):
            self.rooms[room_id]["members"][actor] = "leave"
            return 200, {}
        if room_id := room_from(r"/_matrix/client/v3/rooms/([^/]+)/joined_members"):
            room = self.rooms.get(room_id)
            if room is None:
                return 404, {}
            if room["members"].get(actor) != "join":
                return 403, {}
            return 200, {"joined": {u: {} for u, m in room["members"].items() if m == "join"}}
        if room_id := room_from(r"/_matrix/client/v3/rooms/([^/]+)/state/m\.room\.power_levels/"):
            room = self.rooms[room_id]
            if self.power(room_id, actor) < (room["levels"] or {}).get("state_default", 50):
                return 403, {"error": "Not enough power"}
            room["levels"] = body
            return 200, {"event_id": "$levels"}
        if room_id := room_from(r"/_matrix/client/v3/rooms/([^/]+)/redact/([^/]+)/[^/]+"):
            event_id = unquote(route.split("/redact/", 1)[1].split("/", 1)[0])
            for event in self.rooms[room_id]["events"]:
                if event["event_id"] == event_id:
                    event["content"] = {}
            return 200, {"event_id": "$redaction"}
        if room_id := room_from(r"/_matrix/client/v3/rooms/([^/]+)/send/m\.room\.message/[^/]+"):
            event_id = self._post(room_id, actor, body)
            self._bot(room_id, body.get("body", ""))
            return 200, {"event_id": event_id}
        if room_id := room_from(r"/_matrix/client/v3/rooms/([^/]+)/messages"):
            if room_id not in self.rooms:
                return 404, {}
            return 200, {"chunk": [dict(e) for e in reversed(self.rooms[room_id]["events"])]}
        return 404, {"error": f"unhandled {method} {route}"}

    def _post(self, room_id, sender, content):
        self.counter += 1
        event = {
            "event_id": f"$e{self.counter}",
            "sender": sender,
            "type": "m.room.message",
            "content": content,
        }
        self.rooms[room_id]["events"].append(event)
        if sender != BOT:
            self.sent.append((room_id, content.get("body", "")))
        return event["event_id"]

    def _say(self, room_id, text):
        self._post(room_id, BOT, {"msgtype": "m.notice", "body": text})

    def _bot(self, room_id, text):
        """Answer a command the way the bridgev2 bot does."""
        if not self.bot_answers:
            return
        is_management = room_id == "!commands:community.test"
        if not is_management:
            if not text.startswith("!wa "):
                return
            text = text.removeprefix("!wa ")
        command, _, argument = text.partition(" ")
        if is_management:
            self._management(room_id, command, argument)
        else:
            self._portal(room_id, command, argument)

    def _management(self, room_id, command, argument):
        if command == "login" and argument == "qr":
            self._post(
                room_id, BOT, {"msgtype": "m.image", "body": "qr.png", "url": f"mxc://{SERVER}/qr1"}
            )
            self._say(room_id, "Scan within 60 seconds.")
        elif command == "resolve-link":
            self._say(room_id, f"That invite link points at Neighbours (`{GROUP}`)")
        elif command == "join":
            with self.db() as db:
                if not db.execute("SELECT 1 FROM portal WHERE id = ?", (GROUP,)).fetchone():
                    db.execute(
                        "INSERT INTO portal (bridge_id, id, receiver, name) "
                        "VALUES ('whatsapp', ?, '', 'Neighbours')",
                        (GROUP,),
                    )
            config = yaml.safe_load(
                (self.database.parent / "config.yaml").read_text(encoding="utf-8")
            )
            if GROUP in config["bridge"]["portal_create_filter"]["list"]:
                self.new_portal_room(GROUP)
            self._say(
                room_id,
                f"Successfully joined group `{GROUP}`, the portal should be created momentarily",
            )
        elif command == "create-portal":
            self.new_portal_room(argument)
        elif command == "doin":
            target, _, inner = argument.partition(" ")
            if inner == "unbridge":
                with self.db() as db:
                    db.execute("UPDATE portal SET mxid = NULL WHERE mxid = ?", (target,))
        else:
            self._say(room_id, f"help for {command}{' ' + argument if argument else ''}")

    def _portal(self, room_id, command, argument):
        room = self.rooms[room_id]
        if command == "bridge":
            if self.power(room_id, BOT) < 50:
                self._say(room_id, "I don't have sufficient permissions for `m.bridge` events.")
                return
            record = self.record(argument)
            if record is None:
                self._say(room_id, f"No portal found with ID {argument}.")
            elif record["room_id"]:
                self._say(room_id, "That chat is already bridged to another room.")
            else:
                self.link(argument, room_id)
                self._say(room_id, f"Successfully plumbed this room to {argument} on WhatsApp")
        elif command == "unbridge":
            with self.db() as db:
                db.execute("UPDATE portal SET mxid = NULL WHERE mxid = ?", (room_id,))
            room["members"][BOT] = "leave"
        elif command == "set-relay":
            with self.db() as db:
                db.execute("UPDATE portal SET relay_login_id = ? WHERE mxid = ?", (LOGIN, room_id))
            self._say(
                room_id,
                "Messages sent by users who haven't logged in will now be relayed "
                f"through +{LOGIN} ({ADMIN}'s login)",
            )
        elif command == "unset-relay":
            with self.db() as db:
                db.execute("UPDATE portal SET relay_login_id = '' WHERE mxid = ?", (room_id,))
            self._say(room_id, "Stopped relaying messages for users who haven't logged in")
        elif command == "delete-portal":
            with self.db() as db:
                db.execute("DELETE FROM portal WHERE mxid = ?", (room_id,))
            del self.rooms[room_id]


def write_config(path: Path, allowed=()):
    config = {
        "network": {
            "history_sync": {"max_initial_conversations": 0, "request_full_sync": False},
            "max_member_sync": 0,
        },
        "bridge": {
            "command_prefix": "!wa",
            "portal_create_filter": {"mode": "allow", "list": list(allowed)},
        },
    }
    path.write_text(yaml.safe_dump(config), encoding="utf-8")


def allowed(root: Path) -> list[str]:
    config = yaml.safe_load(
        (root / "data" / "whatsapp_bridge" / "config.yaml").read_text(encoding="utf-8")
    )
    return config["bridge"]["portal_create_filter"]["list"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    (root / "data" / "matrix").mkdir(parents=True)
    (root / "data" / "matrix" / "homeserver.yaml").write_text(
        f'server_name: "{SERVER}"\nregistration_shared_secret: "shh"\n', encoding="utf-8"
    )
    (root / "community-stack.yaml").write_text(
        f"domain:\n  matrix: {SERVER}\nservices:\n  whatsapp_bridge:\n    enabled: true\n",
        encoding="utf-8",
    )
    AdminStore(root / "data" / "announcements" / "announcements.db").create_admin("olga", PASSWORD)
    data = root / "data" / "whatsapp_bridge"
    data.mkdir(parents=True)
    write_config(data / "config.yaml")
    (data / "registration.yaml").write_text(
        yaml.safe_dump(
            {
                "as_token": AS_TOKEN,
                "namespaces": {
                    "users": [{"exclusive": True, "regex": r"^@whatsappbot:community\.test$"}]
                },
            }
        ),
        encoding="utf-8",
    )
    with sqlite3.connect(data / "mautrix-whatsapp.db") as db:
        db.execute(
            "CREATE TABLE portal (bridge_id TEXT, id TEXT, receiver TEXT, mxid TEXT, "
            "parent_id TEXT, parent_receiver TEXT, name TEXT, relay_login_id TEXT)"
        )
    fake = FakeSynapse(data / "mautrix-whatsapp.db")
    containers = {"state": "running"}
    restarts: list[str] = []
    monkeypatch.setattr(matrix_admin, "admin_token", lambda context: ADMIN_TOKEN)
    monkeypatch.setattr(matrix_admin, "synapse_request", fake)
    monkeypatch.setattr(
        matrix_admin,
        "synapse_download",
        lambda path, token: (200, "image/png", PNG) if path.endswith("/qr1") else (404, "", b""),
    )
    monkeypatch.setattr(bridges, "_sleep", lambda seconds: None)
    for name in ("REPLY_TIMEOUT_SECONDS", "JOIN_TIMEOUT_SECONDS", "PORTAL_TIMEOUT_SECONDS"):
        monkeypatch.setattr(bridges, name, 0.05)
    monkeypatch.setattr(bridges, "REPLY_QUIET_SECONDS", 0.0)
    monkeypatch.setattr(docker, "is_docker_available", lambda: True)
    monkeypatch.setattr(
        docker,
        "list_community_containers",
        lambda: [{"name": WHATSAPP.container, "state": containers["state"]}],
    )
    monkeypatch.setattr(
        bridges, "restart_bridge", lambda context, spec: restarts.append(spec.key) or {}
    )
    fake.containers = containers
    fake.restarts = restarts
    return root, fake


def add_chat(fake, chat_id=GROUP, room_id=None, name="Neighbours", relay=""):
    with fake.db() as db:
        db.execute(
            "INSERT INTO portal (bridge_id, id, receiver, mxid, name, relay_login_id) "
            "VALUES ('whatsapp', ?, '', ?, ?, ?)",
            (chat_id, room_id, name, relay),
        )


def linked_portal(fake, relay=""):
    """A group already bridged into a room version 12 room, created by the bot."""
    fake.add_room(PORTAL, "Neighbours", creator=BOT, members={BOT: "join", ADMIN: "join"})
    add_chat(fake, room_id=PORTAL, relay=relay)


# --- The bot's command chat --------------------------------------------------------------------


def test_login_qr_returns_the_bots_qr_image_and_its_text(project):
    root, _ = project
    result = bridges.run_command(AgentContext(root), "login qr")
    assert [reply["kind"] for reply in result["replies"]] == ["image", "text"]
    image = result["replies"][0]["image"]
    assert base64.b64decode(image.split(",", 1)[1]) == PNG
    assert result["replies"][1]["text"] == "Scan within 60 seconds."


def test_the_command_chat_is_created_once_and_reused(project):
    root, fake = project
    context = AgentContext(root)
    bridges.run_command(context, "help")
    bridges.run_command(context, "list-logins")
    assert sum(path == "/_matrix/client/v3/createRoom" for _, path, *_ in fake.requests) == 1
    saved = json.loads((root / "data" / "agent" / "bridges" / "whatsapp.json").read_text())
    assert saved == {"room_id": "!commands:community.test"}


def test_only_allowlisted_commands_reach_the_bot(project):
    root, fake = project
    with pytest.raises(bridges.BridgeError, match="not available"):
        bridges.run_command(AgentContext(root), "logout everything")
    reply = AgentService(AgentContext(root)).handle(
        {"op": "bridge.whatsapp.command", "actor": "olga", "params": {"command": "doin x y"}}
    )
    assert reply["ok"] is False
    assert fake.sent == []


def test_a_silent_bot_is_reported_not_treated_as_success(project):
    root, fake = project
    fake.bot_answers = False
    result = bridges.run_command(AgentContext(root), "list-logins")
    assert result["replies"] == []
    assert "did not answer" in result["note"]


def test_a_stale_management_room_is_replaced_once_and_the_command_retried(project, monkeypatch):
    root, _ = project
    rooms = iter(["!stale:community.test", "!fresh:community.test"])
    sent = []
    replies = iter(
        [
            [{"kind": "text", "text": "This is not your management room.", "image": None}],
            [{"kind": "text", "text": "Available logins: none.", "image": None}],
        ]
    )
    monkeypatch.setattr(bridges, "ensure_direct_room", lambda ctx, spec=None: next(rooms))
    monkeypatch.setattr(bridges, "_send", lambda ctx, room, text: sent.append((room, text)) or "$e")
    monkeypatch.setattr(bridges, "_collect_replies", lambda *args: next(replies))
    result = bridges.run_command(AgentContext(root), "list-logins")
    assert [room for room, _ in sent] == ["!stale:community.test", "!fresh:community.test"]
    assert result["replies"][0]["text"] == "Available logins: none."


def test_the_command_chat_can_be_reset(project):
    root, fake = project
    context = AgentContext(root)
    bridges.run_command(context, "help")
    bridges.reset_command_chat(context, WHATSAPP)
    assert sum(path == "/_matrix/client/v3/createRoom" for _, path, *_ in fake.requests) == 2


# --- Status and diagnosis ----------------------------------------------------------------------


def test_status_reports_local_state_without_messaging_the_bot(project):
    root, fake = project
    status = bridges.status(AgentContext(root))
    assert status["enabled"] is True
    assert status["safeguards_ok"] is True
    assert status["registered_with_synapse"] is False
    assert status["command_chat_created"] is False
    assert fake.requests == []


def test_diagnostics_name_what_is_missing_and_how_to_fix_it(project):
    root, fake = project
    (root / "data" / "whatsapp_bridge" / "config.yaml").unlink()
    fake.containers["state"] = "exited"
    checks = {check["id"]: check for check in bridges.diagnose(AgentContext(root))}
    assert checks["bridge_config"]["ok"] is False
    assert "Turn WhatsApp off and on" in checks["bridge_config"]["fix"]
    assert checks["synapse_registered"]["ok"] is False
    assert checks["container"]["ok"] is False


@pytest.mark.parametrize(
    "path, value",
    [
        (("network", "history_sync", "max_initial_conversations"), -1),
        (("network", "max_member_sync"), 200),
        (("bridge", "portal_create_filter", "mode"), "deny"),
    ],
)
def test_diagnostics_flag_a_missing_safeguard(project, path, value):
    root, _ = project
    config_path = root / "data" / "whatsapp_bridge" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    section = config
    for key in path[:-1]:
        section = section[key]
    section[path[-1]] = value
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    checks = {check["id"]: check for check in bridges.diagnose(AgentContext(root))}
    assert checks["safeguards"]["ok"] is False


def test_a_registration_written_after_synapse_started_is_flagged(project, monkeypatch):
    root, _ = project
    from datetime import datetime, timezone

    appservices = root / "data" / "matrix" / "appservices"
    appservices.mkdir()
    (appservices / "whatsapp.yaml").write_text("id: whatsapp\n", encoding="utf-8")
    homeserver = root / "data" / "matrix" / "homeserver.yaml"
    homeserver.write_text(
        homeserver.read_text() + "app_service_config_files:\n  - /data/appservices/whatsapp.yaml\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        bridges, "_synapse_started_at", lambda: datetime(2000, 1, 1, tzinfo=timezone.utc)
    )
    checks = {check["id"]: check for check in bridges.diagnose(AgentContext(root))}
    assert checks["synapse_registered"]["ok"] is True
    assert checks["synapse_restarted"]["ok"] is False
    assert "Restart Matrix" in checks["synapse_restarted"]["fix"]


# --- What the page shows --------------------------------------------------------------------


def test_the_overview_sorts_rooms_chats_and_problems_without_changing_anything(project):
    root, fake = project
    context = AgentContext(root)
    bridges.ensure_direct_room(context)
    linked_portal(fake, relay=LOGIN)
    add_chat(fake, OTHER_GROUP, name="")
    add_chat(fake, "120363000000000001@g.us", room_id="!gone:community.test", name="Old group")
    fake.add_room("!leftover", "Test", creator=BOT, members={BOT: "join"})
    write_config(root / "data" / "whatsapp_bridge" / "config.yaml", ["120363999999999999@g.us"])
    before = len(fake.requests)

    overview = bridges.overview(context, WHATSAPP)

    assert overview["rooms"] == [
        {
            "room_id": PORTAL,
            "chat_id": GROUP,
            "name": "Neighbours",
            "chat_name": "Neighbours",
            "members": 2,
            "admin_joined": True,
            "relay": True,
            "allowed": False,
            "encrypted": False,
        }
    ]
    assert [(chat["chat_id"], chat["allowed"]) for chat in overview["chats"]] == [
        ("120363999999999999@g.us", True),
        (OTHER_GROUP, False),
    ]
    assert overview["chats"][1]["name"] == "Unnamed group"
    assert [item["room_id"] for item in overview["broken"]] == ["!gone:community.test"]
    assert "no longer exists" in overview["broken"][0]["problem"]
    assert [item["room_id"] for item in overview["leftovers"]] == ["!leftover"]
    assert all(method == "GET" for method, *_ in fake.requests[before:])


def test_rooms_to_link_exclude_spaces_command_chats_and_bridged_rooms(project):
    root, fake = project
    context = AgentContext(root)
    bridges.ensure_direct_room(context)
    linked_portal(fake)
    fake.add_room(EXISTING, "General", creator=ALICE, version="10")
    fake.add_room("!space", "Our space", space=True)
    rooms = bridges.matrix_rooms(context, WHATSAPP)
    assert [room["room_id"] for room in rooms] == [EXISTING]


# --- Linking a chat to a room --------------------------------------------------------------------


def test_a_chat_can_be_linked_to_an_existing_room_made_by_someone_else(project):
    root, fake = project
    add_chat(fake)
    fake.add_room(
        EXISTING,
        "General",
        creator=ALICE,
        version="10",
        members={ALICE: "join"},
        levels={"users": {ALICE: 100}, "state_default": 50, "events": {}},
    )

    result = bridges.attach_room(AgentContext(root), WHATSAPP, GROUP, EXISTING)

    assert fake.record(GROUP) == {"room_id": EXISTING, "relay": LOGIN}
    assert result["relay"] is True
    # Nobody had given the stack admin power there, so the server's admin API did.
    assert result["admin_promoted"] is True
    assert fake.rooms[EXISTING]["members"][BOT] == "join"
    assert fake.rooms[EXISTING]["levels"]["users"][BOT] == 100
    assert (EXISTING, f"!wa bridge {GROUP}") in fake.sent
    # Linking a room by hand never adds the chat to the allowlist.
    assert allowed(root) == []


def test_linking_into_a_room_version_12_room_the_admin_created(project):
    root, fake = project
    add_chat(fake)
    room = fake.add_room("!AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_abcde", "Announcements")
    result = bridges.attach_room(AgentContext(root), WHATSAPP, GROUP, room)
    assert result["admin_promoted"] is False
    assert fake.record(GROUP)["room_id"] == room


def test_a_linked_chat_moves_only_when_asked_and_its_old_room_is_kept(project):
    root, fake = project
    linked_portal(fake)
    fake.add_room(EXISTING, "General", version="10", levels={"users": {ADMIN: 100}})
    context = AgentContext(root)

    with pytest.raises(bridges.BridgeError, match="already linked"):
        bridges.attach_room(context, WHATSAPP, GROUP, EXISTING)
    result = bridges.attach_room(context, WHATSAPP, GROUP, EXISTING, move=True)

    assert result["moved_from"] == PORTAL
    assert fake.record(GROUP)["room_id"] == EXISTING
    assert PORTAL in fake.rooms and fake.rooms[PORTAL]["members"][ADMIN] == "join"
    assert (PORTAL, "!wa unbridge") in fake.sent


@pytest.mark.parametrize(
    "setup, room, error",
    [
        ("space", "!space", "space"),
        ("command", "!commands:community.test", "command chat"),
        ("taken", PORTAL, "already linked to another"),
        ("missing", "!nowhere:community.test", "does not exist"),
    ],
)
def test_linking_refuses_rooms_that_cannot_hold_a_chat(project, setup, room, error):
    root, fake = project
    context = AgentContext(root)
    add_chat(fake, OTHER_GROUP)
    if setup == "space":
        fake.add_room("!space", "Our space", space=True)
    if setup == "command":
        bridges.ensure_direct_room(context)
    if setup == "taken":
        linked_portal(fake)
    with pytest.raises(bridges.BridgeError, match=error):
        bridges.attach_room(context, WHATSAPP, OTHER_GROUP, room)


def test_linking_a_chat_the_bridge_has_not_seen_says_what_to_do(project):
    root, fake = project
    fake.add_room(EXISTING, "General")
    with pytest.raises(bridges.BridgeError, match="has not seen"):
        bridges.attach_room(AgentContext(root), WHATSAPP, GROUP, EXISTING)


def test_creating_a_room_for_a_known_chat_allows_it_and_turns_relay_on(project):
    root, fake = project
    add_chat(fake)
    result = bridges.create_room(AgentContext(root), WHATSAPP, GROUP)
    assert result["room_id"] == fake.record(GROUP)["room_id"]
    assert result["relay"] is True
    assert allowed(root) == [GROUP]
    assert fake.restarts == ["whatsapp"]


# --- Unlinking and repairs ----------------------------------------------------------------------


def test_unlinking_keeps_the_room_and_stops_the_bridge_recreating_it(project):
    root, fake = project
    linked_portal(fake)
    write_config(root / "data" / "whatsapp_bridge" / "config.yaml", [GROUP])
    context = AgentContext(root)

    with pytest.raises(bridges.BridgeError, match="Confirm"):
        bridges.detach_room(context, WHATSAPP, PORTAL, "")
    bridges.detach_room(context, WHATSAPP, PORTAL, "yes")

    assert fake.record(GROUP)["room_id"] is None
    assert PORTAL in fake.rooms
    assert fake.rooms[PORTAL]["members"][BOT] == "leave"
    assert allowed(root) == []
    assert fake.restarts == ["whatsapp"]


def test_a_link_to_a_deleted_room_is_forgotten_through_the_bot(project):
    root, fake = project
    add_chat(fake, room_id="!gone:community.test")
    write_config(root / "data" / "whatsapp_bridge" / "config.yaml", [GROUP])
    context = AgentContext(root)

    bridges.repair_link(context, WHATSAPP, GROUP, "yes")

    assert fake.record(GROUP)["room_id"] is None
    assert ("!commands:community.test", "doin !gone:community.test unbridge") in fake.sent
    assert allowed(root) == []
    with pytest.raises(bridges.BridgeError, match="not broken"):
        bridges.repair_link(context, WHATSAPP, GROUP, "yes")


def test_the_bot_leaves_a_leftover_room_but_never_a_linked_one(project):
    root, fake = project
    linked_portal(fake)
    fake.add_room("!leftover", "Test", creator=BOT, members={BOT: "join", ADMIN: "join"})
    context = AgentContext(root)
    with pytest.raises(bridges.BridgeError, match="linked to a chat"):
        bridges.leave_room(context, WHATSAPP, PORTAL, "yes")
    bridges.leave_room(context, WHATSAPP, "!leftover", "yes")
    assert fake.rooms["!leftover"]["members"][BOT] == "leave"
    assert fake.rooms["!leftover"]["members"][ADMIN] == "join"


def test_deleting_a_room_revokes_the_chat_and_removes_the_room(project):
    root, fake = project
    linked_portal(fake)
    write_config(root / "data" / "whatsapp_bridge" / "config.yaml", [GROUP])
    bridges.remove_portal_room(AgentContext(root), PORTAL, "yes", WHATSAPP)
    assert PORTAL not in fake.rooms
    assert fake.record(GROUP) is None
    assert allowed(root) == []


def test_chat_records_are_forgotten_only_while_the_bridge_is_stopped(project):
    root, fake = project
    add_chat(fake)
    context = AgentContext(root)
    with pytest.raises(bridges.BridgeError, match="Stop the WhatsApp bridge"):
        bridges.forget_chat(context, WHATSAPP, GROUP, "yes")
    fake.containers["state"] = "exited"
    assert bridges.forget_chat(context, WHATSAPP, GROUP, "yes") == {"removed": True}
    assert fake.record(GROUP) is None


# --- Using a room ------------------------------------------------------------------------------


def test_relay_can_be_turned_on_and_off_with_the_bridge_prefix(project):
    root, fake = project
    linked_portal(fake)
    context = AgentContext(root)
    assert bridges.set_room_relay(context, PORTAL, WHATSAPP)["ok"] is True
    assert fake.record(GROUP)["relay"] == LOGIN
    assert bridges.set_room_relay(context, PORTAL, WHATSAPP, enabled=False)["ok"] is True
    assert [text for room, text in fake.sent if room == PORTAL] == [
        "!wa set-relay",
        "!wa unset-relay",
    ]


def test_commands_in_a_room_join_the_stack_admin_first(project):
    root, fake = project
    fake.add_room(PORTAL, "Neighbours", creator=BOT, members={BOT: "join"})
    add_chat(fake, room_id=PORTAL)
    bridges.set_room_relay(AgentContext(root), PORTAL, WHATSAPP)
    assert fake.rooms[PORTAL]["members"][ADMIN] == "join"


def test_the_configured_command_prefix_wins(project):
    root, fake = project
    linked_portal(fake)
    config_path = root / "data" / "whatsapp_bridge" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["bridge"]["command_prefix"] = "!whatsapp"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    bridges.sync_room(AgentContext(root), WHATSAPP, PORTAL)
    assert (PORTAL, "!whatsapp sync-portal") in fake.sent


def test_people_are_invited_by_the_bridge_bot_only_to_linked_rooms(project):
    root, fake = project
    linked_portal(fake)
    context = AgentContext(root)
    bridges.invite_portal_user(context, PORTAL, "@bob:example.org", WHATSAPP)
    invite = next(r for r in fake.requests if r[0] == "POST" and "/invite?" in r[1])
    assert invite[2] == {"user_id": "@bob:example.org"}
    assert invite[3] == AS_TOKEN
    assert parse_qs(urlsplit(invite[1]).query)["user_id"] == [BOT]
    with pytest.raises(bridges.BridgeError, match="not linked"):
        bridges.invite_portal_user(context, "!other:community.test", "@bob:example.org", WHATSAPP)
    with pytest.raises(bridges.BridgeError, match="valid Matrix user"):
        bridges.invite_portal_user(context, PORTAL, "bob", WHATSAPP)


# --- WhatsApp groups from invite links ---------------------------------------------------------


def test_a_group_added_from_an_invite_link_gets_a_new_room_with_relay(project):
    root, fake = project
    from community_stack.agent import whatsapp_bridge

    result = whatsapp_bridge.add_group(AgentContext(root), INVITE)

    assert result["group_id"] == GROUP
    assert result["room_id"] == fake.record(GROUP)["room_id"]
    assert result["relay"] is True
    assert allowed(root) == [GROUP]
    commands = fake.rooms["!commands:community.test"]["events"]
    assert INVITE not in json.dumps(commands)
    assert INVITE not in json.dumps(result)


def test_a_group_added_from_an_invite_link_can_go_into_an_existing_room(project):
    root, fake = project
    from community_stack.agent import whatsapp_bridge

    fake.add_room(EXISTING, "General", version="10", levels={"users": {ADMIN: 100}})
    result = whatsapp_bridge.add_group(AgentContext(root), INVITE, EXISTING)

    assert result["room_id"] == EXISTING
    assert fake.record(GROUP)["room_id"] == EXISTING
    assert allowed(root) == []
    assert [r for r in fake.rooms if r.startswith("!created")] == []


def test_a_group_that_already_has_a_room_is_not_added_twice(project):
    root, fake = project
    from community_stack.agent import whatsapp_bridge

    linked_portal(fake)
    with pytest.raises(bridges.BridgeError, match="already has a Matrix room"):
        whatsapp_bridge.add_group(AgentContext(root), INVITE)


# --- Agent operations ----------------------------------------------------------------------------


def test_every_bridge_gets_the_same_operations_and_only_whatsapp_joins_by_link():
    actions = [
        "status",
        "overview",
        "rooms",
        "matrix_rooms",
        "command",
        "login_state",
        "command_chat_reset",
        "restart",
        "chat_allow",
        "chat_disallow",
        "chat_forget",
        "room_attach",
        "room_create",
        "room_detach",
        "room_delete",
        "room_sync",
        "room_relay",
        "room_invite",
        "room_leave",
        "link_repair",
    ]
    for spec in (WHATSAPP, SIGNAL, MESSENGER):
        for action in actions:
            assert f"bridge.{spec.key}.{action}" in CATALOG
    assert "bridge.whatsapp.group_add" in CATALOG
    assert "bridge.signal.group_add" not in CATALOG
    chat = {param.name: param for param in CATALOG["bridge.whatsapp.room_attach"].params}
    assert chat["chat_id"].pattern.fullmatch(GROUP)
    assert not chat["chat_id"].pattern.fullmatch("not a group")
    signal_chat = {param.name: param for param in CATALOG["bridge.signal.room_attach"].params}
    assert signal_chat["chat_id"].pattern.fullmatch("aGVsbG8gd29ybGQ=")
    assert not signal_chat["chat_id"].pattern.fullmatch("two words")


def test_operations_accept_room_version_12_ids_and_keep_invite_links_out_of_the_log(project):
    root, fake = project
    linked_portal(fake)
    service = AgentService(AgentContext(root))
    relay = service.handle(
        {"op": "bridge.whatsapp.room_relay", "actor": "olga", "params": {"room_id": PORTAL}}
    )
    assert relay["ok"] is True, relay
    service.handle(
        {"op": "bridge.whatsapp.group_add", "actor": "olga", "params": {"invite_link": INVITE}}
    )
    assert INVITE not in AgentContext(root).audit_path.read_text(encoding="utf-8")


# --- Console pages ----------------------------------------------------------------------------


def test_qr_images_are_held_in_memory_and_only_images_are_accepted(monkeypatch):
    console._qr_images.clear()
    data_url = "data:image/png;base64," + base64.b64encode(PNG).decode()
    image_id = console._stash_qr(data_url)
    assert image_id and console._qr_images[image_id][2] == PNG
    assert console._stash_qr("data:text/html;base64," + base64.b64encode(b"<b>").decode()) is None
    assert console._stash_qr("data:image/png,not-base64") is None
    monkeypatch.setattr(console.time, "monotonic", lambda: console._qr_images[image_id][0] + 1)
    assert console._stash_qr(data_url) is not None
    assert image_id not in console._qr_images


def test_the_bridge_templates_have_no_inline_styles_and_no_server_name_parsing():
    for name in ("bridge.html", "bridges.html"):
        html = Path(f"cli/community_stack/admin/templates/{name}").read_text(encoding="utf-8")
        assert "style=" not in html
        # Room version 12 IDs have no ":server" part to split off.
        assert "split(':'" not in html


@pytest.fixture
def client(project):
    root, fake = project
    config = yaml.safe_load(Path("config/example.yaml").read_text(encoding="utf-8"))
    config["domain"]["matrix"] = SERVER
    config["services"]["whatsapp_bridge"]["enabled"] = True
    (root / "community-stack.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    directory = Path(tempfile.mkdtemp(prefix="bcs-"))
    server = AgentSocketServer(directory / "agent.sock", AgentService(AgentContext(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    app = create_app(
        db_path=root / "data" / "announcements" / "announcements.db",
        feed_dir=root / "feed",
        config_path=root / "community-stack.yaml",
        setup_token="unused",
        run_worker=False,
        agent_socket=directory / "agent.sock",
    )
    client = TestClient(app, base_url="https://community.test", follow_redirects=False)
    page = client.get("/admin/login")
    client.post(
        "/admin/login", data={"csrf_token": csrf(page), "username": "olga", "password": PASSWORD}
    )
    yield root, fake, client
    server.shutdown()
    server.server_close()
    shutil.rmtree(directory, ignore_errors=True)


def csrf(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def test_the_old_whatsapp_address_leads_to_the_bridge_page(client):
    _, _, browser = client
    response = browser.get("/admin/whatsapp")
    assert response.status_code == 308
    assert response.headers["location"] == "/admin/bridges/whatsapp"


def test_the_bridges_overview_lists_every_bridge_and_is_in_the_menu(client):
    _, _, browser = client
    page = browser.get("/admin/bridges")
    assert page.status_code == 200
    for name in ("WhatsApp", "Signal", "Facebook Messenger", "Telegram"):
        assert name in page.text
    assert 'href="/admin/bridges"' in page.text
    assert 'aria-current="page"' in page.text


def test_the_bridge_page_shows_rooms_problems_and_rooms_to_link(client, monkeypatch):
    root, fake, browser = client
    appservices = root / "data" / "matrix" / "appservices"
    appservices.mkdir()
    (appservices / "whatsapp.yaml").write_text("id: whatsapp\n", encoding="utf-8")
    homeserver = root / "data" / "matrix" / "homeserver.yaml"
    homeserver.write_text(
        homeserver.read_text() + "app_service_config_files:\n  - /data/appservices/whatsapp.yaml\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bridges, "_synapse_started_at", lambda: None)
    linked_portal(fake)
    add_chat(fake, OTHER_GROUP, name="Garden club")
    fake.add_room(EXISTING, "General", version="10", levels={"users": {ADMIN: 100}})

    page = browser.get("/admin/bridges/whatsapp")

    assert page.status_code == 200
    assert "Neighbours" in page.text and "Garden club" in page.text
    assert f'href="/app/#/room/{PORTAL}?via={SERVER}"' in page.text
    assert f'<option value="{EXISTING}">General' in page.text

    linked = browser.post(
        "/admin/bridges/whatsapp/room-attach",
        data={"csrf_token": csrf(page), "chat_id": OTHER_GROUP, "room_id": EXISTING},
    )
    assert linked.status_code == 200, linked.text
    assert "now linked to the room" in linked.text
    assert fake.record(OTHER_GROUP)["room_id"] == EXISTING

    missing = browser.post(
        "/admin/bridges/whatsapp/room-detach",
        data={"csrf_token": csrf(page), "room_id": EXISTING},
    )
    assert missing.status_code == 400
    assert (
        browser.post(
            "/admin/bridges/signal/group-add",
            data={"csrf_token": csrf(page), "invite_link": INVITE},
        ).status_code
        == 404
    )
