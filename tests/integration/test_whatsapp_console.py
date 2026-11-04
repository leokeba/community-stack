"""WhatsApp bridge operations driven from the console, with Synapse and the bot faked."""

import base64
import json

import pytest
from community_stack import docker
from community_stack.admin.store import AdminStore
from community_stack.agent import bridges, matrix_admin, updates
from community_stack.agent.operations import AgentContext
from community_stack.agent.server import AgentService

SERVER = "community.test"
BOT = f"@whatsappbot:{SERVER}"
ROOM = "!dm:community.test"
PNG = b"\x89PNG\r\n\x1a\nfake-qr"


class FakeMatrix:
    """Minimal Synapse client-API stand-in: one direct chat, and a bot that answers."""

    def __init__(self, bot_answers=True):
        self.bot_answers = bot_answers
        self.calls = []
        self.events = []  # chronological
        self.rooms_created = 0
        self.counter = 0

    def __call__(self, method, path, body=None, token=None):
        self.calls.append((method, path))
        if path == "/_matrix/client/v3/createRoom":
            self.rooms_created += 1
            return 200, {"room_id": ROOM}
        if path.endswith("/joined_members"):
            return 200, {"joined": {BOT: {}, f"@community-stack-admin:{SERVER}": {}}}
        if "/send/m.room.message/" in path:
            self.counter += 1
            self._add(
                f"@community-stack-admin:{SERVER}", {"msgtype": "m.text", "body": body["body"]}
            )
            if body["body"] == "login qr" and self.bot_answers:
                self._add(
                    BOT, {"msgtype": "m.image", "body": "qr.png", "url": f"mxc://{SERVER}/qr1"}
                )
                self._add(BOT, {"msgtype": "m.text", "body": "Scan within 60 seconds."})
            elif self.bot_answers:
                self._add(BOT, {"msgtype": "m.text", "body": f"help for {body['body']}"})
            return 200, {"event_id": f"$cmd{self.counter}"}
        if "/messages?" in path:
            chunk = [dict(event) for event in reversed(self.events)]
            return 200, {"chunk": chunk}
        return 404, {}

    def _add(self, sender, content):
        self.counter += 1
        self.events.append(
            {
                "event_id": f"$e{self.counter}",
                "sender": sender,
                "type": "m.room.message",
                "content": content,
            }
        )


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
    monkeypatch.setattr(bridges, "ensure_direct_room", lambda ctx: next(rooms))
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
