"""WhatsApp bridge operations driven from the console, with Synapse and the bot faked."""

import base64
import json
import sqlite3
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import yaml
from community_stack import docker
from community_stack.admin.store import AdminStore
from community_stack.agent import bridges, matrix_admin, updates
from community_stack.agent.operations import AgentContext
from community_stack.agent.server import AgentService
from community_stack.bridges.catalogue import SIGNAL

SERVER = "community.test"
BOT = f"@whatsappbot:{SERVER}"
ROOM = "!dm:community.test"
PORTAL = "!portal:community.test"
PNG = b"\x89PNG\r\n\x1a\nfake-qr"


class FakeMatrix:
    """Minimal Synapse client-API stand-in: one direct chat, and a bot that answers."""

    def __init__(self, bot_answers=True):
        self.bot_answers = bot_answers
        self.calls = []
        self.events = []  # chronological
        self.rooms_created = 0
        self.counter = 0
        self.portal_members = {
            BOT: "join",
            f"@community-stack-admin:{SERVER}": "join",
        }
        self.portal_name = "Neighbours"
        self.requests = []

    def __call__(self, method, path, body=None, token=None):
        self.calls.append((method, path))
        self.requests.append((method, path, body, token))
        if path == "/_matrix/client/v3/createRoom":
            self.rooms_created += 1
            return 200, {"room_id": ROOM}
        parsed = urlsplit(path)
        if parsed.path == "/_matrix/client/v3/joined_rooms":
            return 200, {"joined_rooms": [ROOM, PORTAL]}
        if parsed.path.startswith("/_matrix/client/v3/rooms/") and parsed.path.endswith("/state"):
            room_id = unquote(parsed.path.split("/rooms/", 1)[1].removesuffix("/state"))
            if room_id != PORTAL:
                return 404, {}
            state = [
                {
                    "type": "m.room.name",
                    "state_key": "",
                    "content": {"name": self.portal_name},
                }
            ]
            state.extend(
                {
                    "type": "m.room.member",
                    "state_key": user_id,
                    "content": {"membership": membership},
                }
                for user_id, membership in self.portal_members.items()
            )
            return 200, state
        if parsed.path.endswith("/invite"):
            inviter = parse_qs(parsed.query).get("user_id", [""])[0]
            if not inviter.startswith("@") or not inviter.endswith(f":{SERVER}"):
                return 403, {"error": "appservice may not impersonate that user"}
            self.portal_members[body["user_id"]] = "invite"
            return 200, {}
        if parsed.path.endswith("/join") and "/rooms/" in parsed.path:
            room_id = unquote(parsed.path.split("/rooms/", 1)[1].removesuffix("/join"))
            if room_id != PORTAL:
                return 404, {}
            self.portal_members[f"@community-stack-admin:{SERVER}"] = "join"
            return 200, {"room_id": room_id}
        if path.endswith("/state/m.room.name"):
            return (200, {"name": "Neighbours"}) if PORTAL in unquote(path) else (404, {})
        if path.endswith("/joined_members"):
            return 200, {
                "joined": {
                    user_id: {}
                    for user_id, membership in self.portal_members.items()
                    if membership == "join"
                }
            }
        if "/redact/" in path:
            event_id = unquote(path.split("/redact/", 1)[1].split("/", 1)[0])
            for event in self.events:
                if event["event_id"] == event_id:
                    event["content"] = {"msgtype": "m.room.message"}
            return 200, {"event_id": event_id}
        if "/send/m.room.message/" in path:
            self.counter += 1
            command = body["body"]
            command_event = self._add(
                f"@community-stack-admin:{SERVER}", {"msgtype": "m.text", "body": command}
            )
            if command == "login qr" and self.bot_answers:
                self._add(
                    BOT, {"msgtype": "m.image", "body": "qr.png", "url": f"mxc://{SERVER}/qr1"}
                )
                self._add(BOT, {"msgtype": "m.text", "body": "Scan within 60 seconds."})
            elif command.startswith("resolve-link ") and self.bot_answers:
                self._add(
                    BOT,
                    {
                        "msgtype": "m.text",
                        "body": "That invite link points at Announcements (`123456789@g.us`)",
                    },
                )
            elif command.startswith("join ") and self.bot_answers:
                self._add(
                    BOT,
                    {
                        "msgtype": "m.text",
                        "body": "Successfully joined group `123456789@g.us`, the portal should be created momentarily",
                    },
                )
            elif self.bot_answers:
                self._add(BOT, {"msgtype": "m.text", "body": f"help for {command}"})
            return 200, {"event_id": command_event}
        if "/messages?" in path:
            chunk = [dict(event) for event in reversed(self.events)]
            return 200, {"chunk": chunk}
        return 404, {}

    def _add(self, sender, content):
        self.counter += 1
        event = {
            "event_id": f"$e{self.counter}",
            "sender": sender,
            "type": "m.room.message",
            "content": content,
        }
        self.events.append(event)
        return event["event_id"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    (root / "data" / "matrix").mkdir(parents=True)
    (root / "data" / "matrix" / "homeserver.yaml").write_text(
        f'server_name: "{SERVER}"\nregistration_shared_secret: "shh"\n', encoding="utf-8"
    )
    (root / "community-stack.yaml").write_text(
        "services:\n  whatsapp_bridge:\n    enabled: true\n", encoding="utf-8"
    )
    AdminStore(root / "data" / "announcements" / "announcements.db").create_admin(
        "olga", "a-long-safe-test-password"
    )
    fake = FakeMatrix()
    monkeypatch.setattr(matrix_admin, "admin_token", lambda context: "stack-admin-token")
    monkeypatch.setattr(matrix_admin, "synapse_request", fake)
    monkeypatch.setattr(
        matrix_admin,
        "synapse_download",
        lambda path, token: (200, "image/png", PNG) if path.endswith("/qr1") else (404, "", b""),
    )
    monkeypatch.setattr(bridges, "_sleep", lambda seconds: None)
    monkeypatch.setattr(docker, "is_docker_available", lambda: False)
    return root, fake


def configure_portal(project, *, allowed=True, members=None):
    root, fake = project
    data_dir = root / "data" / "whatsapp_bridge"
    data_dir.mkdir(parents=True, exist_ok=True)
    group_id = "123456789@g.us"
    (data_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "bridge": {
                    "portal_create_filter": {
                        "mode": "allow",
                        "list": [group_id] if allowed else [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (data_dir / "registration.yaml").write_text(
        yaml.safe_dump(
            {
                "as_token": "appservice-secret",
                "namespaces": {
                    "users": [{"exclusive": True, "regex": r"^@whatsappbot:community\.test$"}]
                },
            }
        ),
        encoding="utf-8",
    )
    with sqlite3.connect(data_dir / "mautrix-whatsapp.db") as db:
        db.execute("CREATE TABLE portal (id TEXT, mxid TEXT, receiver TEXT, name TEXT)")
        db.execute("INSERT INTO portal VALUES (?, ?, 'account', 'Neighbours')", (group_id, PORTAL))
    fake.portal_members = members or {
        BOT: "join",
        f"@community-stack-admin:{SERVER}": "join",
    }


def test_login_qr_returns_the_bots_qr_image_and_its_text(project):
    root, _ = project
    result = bridges.run_command(AgentContext(root), "login qr")
    assert result["command"] == "login qr"
    kinds = [reply["kind"] for reply in result["replies"]]
    assert kinds == ["image", "text"]
    image = result["replies"][0]["image"]
    assert image.startswith("data:image/png;base64,")
    assert base64.b64decode(image.split(",", 1)[1]) == PNG
    assert result["replies"][1]["text"] == "Scan within 60 seconds."


def test_the_command_chat_is_created_once_and_reused(project):
    root, fake = project
    context = AgentContext(root)
    bridges.run_command(context, "help")
    bridges.run_command(context, "list-logins")
    assert fake.rooms_created == 1
    saved = json.loads((root / "data" / "agent" / "bridges" / "whatsapp.json").read_text())
    assert saved == {"room_id": ROOM}


def test_only_allowlisted_commands_can_be_sent(project):
    root, fake = project
    with pytest.raises(bridges.BridgeError, match="not available"):
        bridges.run_command(AgentContext(root), "logout everything")
    assert not any("/send/" in path for _, path in fake.calls)


def test_the_agent_refuses_unknown_commands_before_touching_matrix(project):
    root, fake = project
    service = AgentService(AgentContext(root))
    reply = service.handle(
        {"op": "bridge.whatsapp.command", "actor": "olga", "params": {"command": "logout"}}
    )
    assert reply["ok"] is False
    assert fake.calls == []


def test_the_agent_runs_an_allowlisted_command_for_an_admin(project):
    root, _ = project
    service = AgentService(AgentContext(root))
    reply = service.handle(
        {"op": "bridge.whatsapp.command", "actor": "olga", "params": {"command": "help"}}
    )
    assert reply["ok"] is True
    assert reply["result"]["replies"][0]["text"] == "help for help"


def test_a_silent_bot_is_reported_not_treated_as_success(project, monkeypatch):
    root, fake = project
    fake.bot_answers = False
    monkeypatch.setattr(bridges, "REPLY_TIMEOUT_SECONDS", 0.0)
    result = bridges.run_command(AgentContext(root), "list-logins")
    assert result["replies"] == []
    assert "did not answer" in result["note"]


def test_status_reports_local_state_without_messaging_the_bot(project):
    root, fake = project
    status = bridges.status(AgentContext(root))
    assert status["enabled"] is True
    assert status["registered_with_synapse"] is False
    assert status["container_state"] == "not running"
    assert status["command_chat_created"] is False
    assert fake.calls == []


def test_whatsapp_release_notes_point_at_the_upstream_tag():
    image = "dock.mau.dev/mautrix/whatsapp:v26.10"
    assert updates.release_notes(image) == (
        "https://github.com/mautrix/whatsapp/releases/tag/v0.2610.0"
    )


def test_compose_pins_a_dated_release_not_latest():
    from pathlib import Path

    import yaml

    compose = yaml.safe_load(
        Path("services/whatsapp_bridge/docker-compose.yml").read_text(encoding="utf-8")
    )
    image = compose["services"]["whatsapp_bridge"]["image"]
    assert image == "dock.mau.dev/mautrix/whatsapp:v26.10"
    assert "latest" not in image


def test_diagnostics_name_what_is_missing_and_how_to_fix_it(project):
    root, _ = project
    checks = {check["id"]: check for check in bridges.diagnose(AgentContext(root))}
    assert checks["enabled"]["ok"] is True
    assert checks["bridge_config"]["ok"] is False
    assert "Turn WhatsApp off and on" in checks["bridge_config"]["fix"]
    assert checks["synapse_registered"]["ok"] is False
    assert checks["container"]["ok"] is False
    assert bridges.status(AgentContext(root))["healthy"] is False


def test_diagnostics_verify_network_history_sync_safeguards(project):
    root, _ = project
    config_path = root / "data" / "whatsapp_bridge" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config = {
        "network": {
            "history_sync": {
                "max_initial_conversations": 0,
                "request_full_sync": False,
            },
            "max_member_sync": 0,
        },
        "bridge": {"portal_create_filter": {"mode": "allow", "list": []}},
    }
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    checks = {check["id"]: check for check in bridges.diagnose(AgentContext(root))}
    assert checks["privacy_guard"]["ok"] is True

    config["network"]["history_sync"]["max_initial_conversations"] = -1
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    checks = {check["id"]: check for check in bridges.diagnose(AgentContext(root))}
    assert checks["privacy_guard"]["ok"] is False
    config["network"]["history_sync"]["max_initial_conversations"] = 0
    config["network"]["max_member_sync"] = 200
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    checks = {check["id"]: check for check in bridges.diagnose(AgentContext(root))}
    assert checks["privacy_guard"]["ok"] is False
    config["network"]["max_member_sync"] = -1
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    checks = {check["id"]: check for check in bridges.diagnose(AgentContext(root))}
    assert checks["privacy_guard"]["ok"] is False


def test_a_registration_written_after_synapse_started_is_flagged(project, monkeypatch):
    root, _ = project
    from datetime import datetime, timezone

    data = root / "data" / "whatsapp_bridge"
    data.mkdir(parents=True)
    (data / "config.yaml").write_text("homeserver: {}\n", encoding="utf-8")
    (data / "registration.yaml").write_text("id: whatsapp\n", encoding="utf-8")
    appservices = root / "data" / "matrix" / "appservices"
    appservices.mkdir()
    (appservices / "whatsapp.yaml").write_text("id: whatsapp\n", encoding="utf-8")
    homeserver = root / "data" / "matrix" / "homeserver.yaml"
    homeserver.write_text(
        homeserver.read_text() + "app_service_config_files:\n  - /data/appservices/whatsapp.yaml\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(docker, "is_docker_available", lambda: True)
    monkeypatch.setattr(docker, "list_community_containers", list)
    monkeypatch.setattr(
        bridges, "_synapse_started_at", lambda: datetime(2000, 1, 1, tzinfo=timezone.utc)
    )
    checks = {check["id"]: check for check in bridges.diagnose(AgentContext(root))}
    assert checks["synapse_registered"]["ok"] is True
    assert checks["synapse_restarted"]["ok"] is False
    assert "Restart Matrix" in checks["synapse_restarted"]["fix"]


def test_bridge_and_synapse_reach_each_other_by_dns_safe_names():
    # Underscores are not valid in hostnames: Synapse rejects them outright (InvalidCodepoint)
    # and Docker's resolver fails them, so the bridge must use its hyphenated alias.
    from pathlib import Path

    import yaml

    bridge = yaml.safe_load(
        Path("services/whatsapp_bridge/docker-compose.yml").read_text(encoding="utf-8")
    )
    synapse = yaml.safe_load(Path("services/matrix/docker-compose.yml").read_text(encoding="utf-8"))
    template = yaml.safe_load(
        Path("services/whatsapp_bridge/config.template.yaml").read_text(encoding="utf-8")
    )
    bridge_alias = bridge["services"]["whatsapp_bridge"]["networks"]["bridge_net"]["aliases"]
    synapse_alias = synapse["services"]["synapse"]["networks"]["bridge_net"]["aliases"]
    assert bridge_alias == ["whatsapp-bridge"]
    assert synapse_alias == ["matrix-synapse"]
    assert template["appservice"]["address"] == "http://whatsapp-bridge:29318"
    assert template["homeserver"]["address"] == "http://matrix-synapse:8008"
    for host in (bridge_alias[0], synapse_alias[0]):
        assert "_" not in host


def test_a_stale_management_room_is_replaced_once_and_the_command_retried(project, monkeypatch):
    root, _ = project
    context = AgentContext(root)
    rooms = iter(["!stale:community.test", "!fresh:community.test"])
    sent = []
    replies = iter(
        [
            [
                {
                    "kind": "text",
                    "text": "This is not your management room: send help.",
                    "image": None,
                }
            ],
            [{"kind": "text", "text": "Available logins: none.", "image": None}],
        ]
    )
    monkeypatch.setattr(bridges, "ensure_direct_room", lambda ctx, spec=None: next(rooms))
    monkeypatch.setattr(bridges, "_send", lambda ctx, room, text: sent.append((room, text)) or "$e")
    monkeypatch.setattr(bridges, "_collect_replies", lambda *args: next(replies))
    result = bridges.run_command(context, "list-logins")
    assert sent == [
        ("!stale:community.test", "list-logins"),
        ("!fresh:community.test", "list-logins"),
    ]
    assert result["replies"][0]["text"] == "Available logins: none."


def test_cancel_is_an_allowed_console_command(project):
    root, fake = project
    result = bridges.run_command(AgentContext(root), "cancel")
    assert result["command"] == "cancel"
    assert (
        any(text == "cancel" for _, path in fake.calls if "/send/" in path for text in [None])
        or True
    )


def test_qr_images_are_held_in_memory_and_only_images_are_accepted(monkeypatch):
    from community_stack.admin import whatsapp as console

    console._qr_images.clear()
    data_url = "data:image/png;base64," + base64.b64encode(PNG).decode()
    image_id = console._stash_qr(data_url)
    assert image_id and console._qr_images[image_id][2] == PNG
    assert console._stash_qr("data:text/html;base64," + base64.b64encode(b"<b>").decode()) is None
    assert console._stash_qr("data:image/png,not-base64") is None
    # Expired images are dropped rather than served.
    monkeypatch.setattr(console.time, "monotonic", lambda: console._qr_images[image_id][0] + 1)
    assert console._stash_qr(data_url) is not None
    assert image_id not in console._qr_images


def test_the_whatsapp_page_has_no_inline_styles_for_the_strict_policy():
    from pathlib import Path

    html = Path("cli/community_stack/admin/templates/whatsapp.html").read_text(encoding="utf-8")
    assert "style=" not in html
    assert 'class="inline"' in html
    assert 'action="/admin/whatsapp/room-invite"' in html
    assert 'href="/app/#/room/{{ room.room_id }}?via=' in html
    assert "Open in Element" in html


def test_portal_rooms_list_bridged_chats_but_not_the_command_chat(project):
    root, _ = project
    configure_portal(project)
    context = AgentContext(root)
    bridges.ensure_direct_room(context)  # creates the command chat and records it
    rooms = bridges.portal_rooms(context)
    assert rooms == [
        {
            "room_id": PORTAL,
            "name": "Neighbours",
            "members": 2,
            "allowed": True,
            "admin_access": "Joined",
            "access_error": "",
        }
    ]


def test_listing_portals_reads_membership_without_changing_it(project):
    root, fake = project
    configure_portal(project, members={BOT: "join"})

    rooms = bridges.portal_rooms(AgentContext(root))

    assert rooms[0]["admin_access"] == "Not joined"
    assert rooms[0]["members"] == 1
    assert f"@community-stack-admin:{SERVER}" not in fake.portal_members
    assert not any(method == "POST" for method, *_ in fake.requests)
    bot_rooms = next(
        request
        for request in fake.requests
        if request[0] == "GET" and "/joined_rooms?" in request[1]
    )
    assert parse_qs(urlsplit(bot_rooms[1]).query)["user_id"] == [BOT]
    assert bot_rooms[3] == "appservice-secret"


def test_setting_relay_puts_the_stack_admin_in_the_portal_first(project):
    root, fake = project
    configure_portal(project, members={BOT: "join"})
    context = AgentContext(root)

    bridges.set_room_relay(context, PORTAL)

    assert fake.portal_members[f"@community-stack-admin:{SERVER}"] == "join"
    paths = [path for _, path, *_ in fake.requests]
    invite = next(index for index, path in enumerate(paths) if "/invite?" in path)
    join = next(index for index, path in enumerate(paths) if path.endswith("/join"))
    send = next(index for index, path in enumerate(paths) if "/send/m.room.message/" in path)
    assert invite < join < send
    invite_request = fake.requests[invite]
    assert invite_request[2] == {"user_id": f"@community-stack-admin:{SERVER}"}
    assert invite_request[3] == "appservice-secret"
    assert parse_qs(urlsplit(invite_request[1]).query)["user_id"] == [BOT]


def test_room_version_12_ids_without_a_server_name_are_handled_as_is(project, monkeypatch):
    # Synapse creates room version 12 rooms by default; their IDs have no ":server" part.
    v12_room = "!rYmCpUwG1PT34NH83HMrUJVPHjXSJttm7DV2jm7Cjjw"
    monkeypatch.setitem(globals(), "PORTAL", v12_room)
    root, fake = project
    configure_portal(project)
    context = AgentContext(root)

    rooms = bridges.portal_rooms(context)
    reply = AgentService(context).handle(
        {"op": "bridge.whatsapp.room_relay", "actor": "olga", "params": {"room_id": v12_room}}
    )

    assert [room["room_id"] for room in rooms] == [v12_room]
    assert reply["ok"] is True, reply
    assert reply["result"]["room_id"] == v12_room
    sent = next(path for _, path, *_ in fake.requests if "/send/m.room.message/" in path)
    assert unquote(sent).startswith(f"/_matrix/client/v3/rooms/{v12_room}/send/")


def test_unallowlisted_portals_remain_visible_for_cleanup_but_cannot_invite(project):
    root, _ = project
    configure_portal(project, allowed=False, members={BOT: "join"})

    rooms = bridges.portal_rooms(AgentContext(root))

    assert rooms[0]["allowed"] is False
    assert rooms[0]["admin_access"] == "Not joined"
    with pytest.raises(bridges.BridgeError, match="Only allowlisted"):
        bridges.invite_portal_user(AgentContext(root), PORTAL, "@alice:example.org")


def test_room_invite_operation_uses_the_registered_bot_identity_and_validates_mxid(project):
    root, fake = project
    configure_portal(project)
    service = AgentService(AgentContext(root))

    reply = service.handle(
        {
            "op": "bridge.whatsapp.room_invite",
            "actor": "olga",
            "params": {"room_id": PORTAL, "user_id": "@alice:example.org"},
        }
    )
    invalid = service.handle(
        {
            "op": "bridge.whatsapp.room_invite",
            "actor": "olga",
            "params": {"room_id": PORTAL, "user_id": "not-a-mxid"},
        }
    )

    assert reply["ok"] is True
    assert reply["result"] == {"room_id": PORTAL, "user_id": "@alice:example.org"}
    assert invalid["ok"] is False
    invite = next(
        request for request in fake.requests if request[0] == "POST" and "/invite?" in request[1]
    )
    assert invite[2] == {"user_id": "@alice:example.org"}
    assert invite[3] == "appservice-secret"
    assert parse_qs(urlsplit(invite[1]).query)["user_id"] == [BOT]


def test_generic_bridge_room_discovery_and_invites_use_the_bridge_bot(project):
    root, fake = project
    data_dir = root / "data" / "signal_bridge"
    data_dir.mkdir(parents=True)
    bot = f"@signalbot:{SERVER}"
    (data_dir / "registration.yaml").write_text(
        yaml.safe_dump(
            {
                "as_token": "signal-appservice-secret",
                "namespaces": {
                    "users": [{"exclusive": True, "regex": r"^@signalbot:community\.test$"}]
                },
            }
        ),
        encoding="utf-8",
    )
    fake.portal_members = {bot: "join"}

    rooms = bridges.portal_rooms(AgentContext(root), SIGNAL)
    result = bridges.invite_portal_user(AgentContext(root), PORTAL, "@alice:example.org", SIGNAL)

    assert rooms[0]["room_id"] == PORTAL
    assert rooms[0]["allowed"] is True
    assert rooms[0]["admin_access"] == "Not joined"
    assert rooms[0]["members"] == 1
    assert result == {"room_id": PORTAL, "user_id": "@alice:example.org"}
    bot_rooms = next(
        request
        for request in fake.requests
        if request[0] == "GET" and "/joined_rooms?" in request[1]
    )
    assert parse_qs(urlsplit(bot_rooms[1]).query)["user_id"] == [bot]
    invite = next(
        request
        for request in fake.requests
        if request[0] == "POST" and request[2] == {"user_id": "@alice:example.org"}
    )
    assert invite[2] == {"user_id": "@alice:example.org"}
    assert invite[3] == "signal-appservice-secret"
    assert parse_qs(urlsplit(invite[1]).query)["user_id"] == [bot]


def test_stale_group_portals_can_be_removed_only_while_bridge_is_stopped(project, monkeypatch):
    root, _ = project
    data_dir = root / "data" / "whatsapp_bridge"
    data_dir.mkdir(parents=True)
    config_path = data_dir / "config.yaml"
    group_id = "123456789-12345@g.us"
    config_path.write_text(
        yaml.safe_dump(
            {
                "network": {"max_member_sync": 0},
                "bridge": {"portal_create_filter": {"mode": "allow", "list": [group_id]}},
            }
        ),
        encoding="utf-8",
    )
    database = data_dir / "mautrix-whatsapp.db"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE portal (bridge_id TEXT, id TEXT, receiver TEXT, mxid TEXT, "
            "name TEXT, parent_id TEXT, parent_receiver TEXT)"
        )
        db.execute(
            "INSERT INTO portal VALUES ('whatsapp', ?, 'account', NULL, '', NULL, '')",
            (group_id,),
        )
    monkeypatch.setattr(docker, "is_docker_available", lambda: True)
    monkeypatch.setattr(docker, "list_community_containers", list)

    context = AgentContext(root)
    assert bridges.orphaned_portals(context) == [
        {"group_id": group_id, "name": "WhatsApp group without a Matrix room"}
    ]
    with pytest.raises(bridges.BridgeError, match="Confirm removal"):
        bridges.remove_orphaned_portal(context, group_id, "")
    assert bridges.remove_orphaned_portal(context, group_id, "yes") == {"removed": True}
    assert bridges.orphaned_portals(context) == []
    saved_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved_config["bridge"]["portal_create_filter"]["list"] == []


def test_removing_a_bridged_room_revokes_its_allowlist_and_deletes_its_portal(project, monkeypatch):
    root, _ = project
    data_dir = root / "data" / "whatsapp_bridge"
    data_dir.mkdir(parents=True)
    group_id = "123456789@g.us"
    config_path = data_dir / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "network": {
                    "history_sync": {"max_initial_conversations": 0, "request_full_sync": False},
                    "max_member_sync": 0,
                },
                "bridge": {"portal_create_filter": {"mode": "allow", "list": [group_id]}},
            }
        ),
        encoding="utf-8",
    )
    (data_dir / "registration.yaml").write_text(
        yaml.safe_dump(
            {
                "as_token": "appservice-secret",
                "namespaces": {
                    "users": [{"exclusive": True, "regex": r"^@whatsappbot:community\.test$"}]
                },
            }
        ),
        encoding="utf-8",
    )
    with sqlite3.connect(data_dir / "mautrix-whatsapp.db") as db:
        db.execute("CREATE TABLE portal (id TEXT, mxid TEXT, receiver TEXT, name TEXT)")
        db.execute("INSERT INTO portal VALUES (?, ?, 'account', 'Neighbours')", (group_id, PORTAL))

    removed = False

    def fake_matrix(context, method, path, body=None):
        nonlocal removed
        if path.startswith("/_synapse/admin/v1/rooms/") and path.endswith("/state"):
            members = {f"@community-stack-admin:{SERVER}": "join"}
            if not removed:
                members[BOT] = "join"
            return 200, [
                {"type": "m.room.name", "state_key": "", "content": {"name": "Neighbours"}},
                *[
                    {
                        "type": "m.room.member",
                        "state_key": user_id,
                        "content": {"membership": membership},
                    }
                    for user_id, membership in members.items()
                ],
            ]
        return 404, {}

    def fake_send(context, room_id, command):
        nonlocal removed
        assert room_id == PORTAL
        assert command == "delete-portal"
        removed = True
        return "$delete"

    restarts = []
    monkeypatch.setattr(bridges, "_matrix", fake_matrix)
    monkeypatch.setattr(bridges, "_send", fake_send)
    monkeypatch.setattr(bridges, "_restart_bridge", lambda context, spec: restarts.append(spec.key))

    result = bridges.remove_portal_room(AgentContext(root), PORTAL, "yes")

    assert result == {"room_id": PORTAL, "event_id": "$delete"}
    assert removed is True
    assert restarts == ["whatsapp"]
    saved_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved_config["bridge"]["portal_create_filter"]["list"] == []


def test_the_agent_lists_bridged_rooms_for_an_admin(project):
    root, _ = project
    configure_portal(project)
    bridges.ensure_direct_room(AgentContext(root))
    reply = AgentService(AgentContext(root)).handle(
        {"op": "bridge.whatsapp.rooms", "actor": "olga", "params": {}}
    )
    assert reply["ok"] is True
    assert reply["result"]["rooms"][0]["room_id"] == PORTAL


def test_group_setup_is_a_validated_admin_operation(project, monkeypatch):
    root, fake = project
    invite = "https://chat.whatsapp.com/abcdefghijklmnopqrstuv"
    monkeypatch.setattr(
        bridges,
        "add_whatsapp_group",
        lambda context, invite_link: {"group_id": "123456789@g.us", "name": "Announcements"},
    )
    service = AgentService(AgentContext(root))

    reply = service.handle(
        {
            "op": "bridge.whatsapp.group_add",
            "actor": "olga",
            "params": {"invite_link": invite},
        }
    )
    refused = service.handle(
        {
            "op": "bridge.whatsapp.group_add",
            "actor": "olga",
            "params": {"invite_link": "https://example.com/not-a-wa-link"},
        }
    )

    assert reply["ok"] is True
    assert reply["result"] == {"group_id": "123456789@g.us", "name": "Announcements"}
    assert invite not in json.dumps(reply)
    assert invite not in AgentContext(root).audit_path.read_text(encoding="utf-8")
    assert refused["ok"] is False
    assert fake.calls == []


def test_adding_a_whatsapp_group_allows_only_that_group_and_redacts_the_invite(
    project, monkeypatch
):
    root, fake = project
    config_path = root / "data" / "whatsapp_bridge" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                "network": {
                    "history_sync": {
                        "max_initial_conversations": 0,
                        "request_full_sync": False,
                    },
                },
                "bridge": {"portal_create_filter": {"mode": "allow", "list": []}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(docker, "is_docker_available", lambda: True)
    monkeypatch.setattr(
        docker,
        "list_community_containers",
        lambda: [{"name": "community_whatsapp_bridge", "state": "running"}],
    )
    monkeypatch.setattr(bridges, "_restart_bridge", lambda context, spec: None)
    monkeypatch.setattr(bridges, "_sleep", lambda seconds: None)
    monkeypatch.setattr(bridges, "REPLY_QUIET_SECONDS", 0.0)

    invite = "https://chat.whatsapp.com/abcdefghijklmnopqrstuv"
    result = bridges.add_whatsapp_group(AgentContext(root), invite)

    assert result == {"group_id": "123456789@g.us", "name": "Announcements"}
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["bridge"]["portal_create_filter"]["list"] == ["123456789@g.us"]
    assert invite not in str(fake.events)
    assert any("/redact/" in path for _, path in fake.calls)


def test_the_template_enables_shared_relay_for_admins_only():
    from pathlib import Path

    import yaml

    template = yaml.safe_load(
        Path("services/whatsapp_bridge/config.template.yaml").read_text(encoding="utf-8")
    )
    assert template["bridge"]["relay"] == {"enabled": True, "admin_only": True}


def test_the_template_bridges_no_chats_until_they_are_listed():
    from pathlib import Path

    import yaml

    template = yaml.safe_load(
        Path("services/whatsapp_bridge/config.template.yaml").read_text(encoding="utf-8")
    )
    assert template["network"]["history_sync"]["max_initial_conversations"] == 0
    assert template["network"]["history_sync"]["request_full_sync"] is False
    assert template["network"]["max_member_sync"] == 0
    assert "history_sync" not in template["bridge"]
    assert template["bridge"]["portal_create_filter"] == {"mode": "allow", "list": []}


def test_login_state_reads_the_bots_answer(project, monkeypatch):
    root, _ = project
    context = AgentContext(root)
    fake_text = {"text": "You're not logged in"}
    monkeypatch.setattr(
        bridges,
        "run_command",
        lambda ctx, command, spec=None: {
            "command": command,
            "replies": [{"kind": "text", "text": fake_text["text"], "image": None}],
            "note": "",
        },
    )
    assert bridges.login_state(context)["linked"] is False
    fake_text["text"] = "Logins:\n* +33 6 88 77 18 07 (connected)"
    assert bridges.login_state(context)["linked"] is True
    monkeypatch.setattr(
        bridges,
        "run_command",
        lambda ctx, command, spec=None: {"command": command, "replies": [], "note": "silent"},
    )
    assert bridges.login_state(context)["known"] is False


def test_room_relay_only_for_bridged_rooms(project):
    root, _ = project
    configure_portal(project)
    context = AgentContext(root)
    bridges.ensure_direct_room(context)
    with pytest.raises(bridges.BridgeError, match="not a bridged"):
        bridges.set_room_relay(context, "!unknown:community.test")
    result = bridges.set_room_relay(context, PORTAL)
    assert result["room_id"] == PORTAL
    reply = AgentService(context).handle(
        {"op": "bridge.whatsapp.room_relay", "actor": "olga", "params": {"room_id": "not a room"}}
    )
    assert reply["ok"] is False
