"""WhatsApp and Signal groups chosen on the Channels page receive announcements."""

import re
from pathlib import Path

import pytest
import yaml
from community_stack.admin import bridged_channels
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.agent import matrix_admin
from community_stack.agent.client import AgentRefused
from community_stack.agent.operations import CATALOG, AgentContext
from community_stack.announcements.adapters.bridged_room import (
    BridgedRoomAdapter,
    destination_name,
)
from community_stack.announcements.models import Announcement
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage
from fastapi.testclient import TestClient

PASSWORD = "a-long-safe-test-password"
ANNOUNCER = "@community-stack-announcements:community.test"
TEST_GROUP = "!rYmCtestgroup"
NEIGHBOURS = "!neighbours:community.test"
ENCRYPTED = "!secret:community.test"
SIGNAL_GROUP = "!signalgroup:community.test"


def room(room_id, name, relay=True, encrypted=False):
    return {
        "room_id": room_id,
        "chat_id": f"{name}@g.us",
        "name": name,
        "chat_name": name,
        "members": 3,
        "admin_joined": True,
        "relay": relay,
        "allowed": True,
        "encrypted": encrypted,
    }


class FakeAgent:
    """The control agent as the console sees it: bridge rooms, relay, invites, the announcer."""

    def __init__(self):
        self.rooms = {
            "whatsapp": [
                room(TEST_GROUP, "Test"),
                room(NEIGHBOURS, "Neighbours", relay=False),
                room(ENCRYPTED, "Secret", encrypted=True),
            ],
            "signal": [room(SIGNAL_GROUP, "Signal crew")],
        }
        self.calls = []
        self.sessions = 0

    def available(self):
        return True

    def call(self, op, actor, params=None, password=None):
        self.calls.append((op, params or {}))
        if op == "matrix.announcer.session":
            self.sessions += 1
            return {"user_id": ANNOUNCER, "access_token": f"announcer-token-{self.sessions}"}
        bridge, action = op.split(".")[1:]
        if action == "overview":
            return {"rooms": self.rooms[bridge], "chats": [], "broken": [], "leftovers": []}
        if action == "room_relay":
            for item in self.rooms[bridge]:
                if item["room_id"] == params["room_id"]:
                    item["relay"] = True
            return {"room_id": params["room_id"], "ok": True, "replies": [], "note": ""}
        if action == "room_invite":
            return {"room_id": params["room_id"], "user_id": params["user_id"]}
        raise AgentRefused(f"Unexpected operation {op}")


class FakeMatrix:
    """Synapse's client API for the announcer account."""

    def __init__(self):
        self.valid_tokens = set()
        self.joined = set()
        self.calls = []

    def __call__(self, token, method, path, payload=None):
        self.calls.append((token, method, path))
        if token not in self.valid_tokens:
            return 401, {"errcode": "M_UNKNOWN_TOKEN", "error": "Unknown token"}
        if path.endswith("/account/whoami"):
            return 200, {"user_id": ANNOUNCER}
        if path.endswith("/joined_rooms"):
            return 200, {"joined_rooms": sorted(self.joined)}
        match = re.search(r"/rooms/([^/]+)/(join|leave)$", path)
        if match:
            room_id = match.group(1).replace("%21", "!").replace("%3A", ":")
            (self.joined.add if match.group(2) == "join" else self.joined.discard)(room_id)
            return 200, {"room_id": room_id}
        return 404, {"error": "unexpected"}


def write_config(path: Path, whatsapp=True, signal=True) -> None:
    config = yaml.safe_load(Path("config/example.yaml").read_text(encoding="utf-8"))
    config["domain"]["matrix"] = "community.test"
    config["services"]["whatsapp_bridge"]["enabled"] = whatsapp
    config["services"]["signal_bridge"] = {"enabled": signal}
    path.write_text(yaml.safe_dump(config), encoding="utf-8")


def csrf(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


@pytest.fixture
def console(tmp_path, monkeypatch):
    config_path = tmp_path / "community-stack.yaml"
    write_config(config_path)
    AdminStore(tmp_path / "announcements.db").create_admin("olga", PASSWORD)
    app = create_app(
        db_path=tmp_path / "announcements.db",
        feed_dir=tmp_path / "feed",
        config_path=config_path,
        setup_token="unused",
        run_worker=False,
    )
    agent, matrix = FakeAgent(), FakeMatrix()
    monkeypatch.setattr(app.state.agent, "available", agent.available)

    def call(op, actor, params=None, password=None):
        result = agent.call(op, actor, params, password)
        if op == "matrix.announcer.session":
            matrix.valid_tokens.add(result["access_token"])
        return result

    monkeypatch.setattr(app.state.agent, "call", call)
    monkeypatch.setattr(bridged_channels, "matrix_call", matrix)
    client = TestClient(app, base_url="https://community.test", follow_redirects=False)
    page = client.get("/admin/login")
    client.post(
        "/admin/login", data={"csrf_token": csrf(page), "username": "olga", "password": PASSWORD}
    )
    return app, client, agent, matrix


def choose(client, bridge, room_id, back=None):
    page = client.get("/admin/channels")
    data = {"csrf_token": csrf(page), "room_id": room_id}
    if back:
        data["back"] = back
    return client.post(f"/admin/channels/bridged/{bridge}/add", data=data)


def test_channels_page_lists_linked_groups_with_one_click_choice(console):
    _, client, _, _ = console
    page = client.get("/admin/channels")
    assert "Chat groups" in page.text
    assert 'id="bridged-whatsapp"' in page.text and 'id="bridged-signal"' in page.text
    assert page.text.count('action="/admin/channels/bridged/whatsapp/add"') == 2
    assert 'action="/admin/channels/bridged/signal/add"' in page.text
    assert "Encrypted rooms cannot receive announcements." in page.text
    # Choosing a group whose relay is off asks first, because relay changes the room.
    assert "Relay will be turned on" in page.text
    assert "Connect Matrix announcements" not in page.text


def test_choosing_groups_creates_the_announcer_once_and_makes_one_channel_each(console):
    app, client, agent, matrix = console

    first = choose(client, "whatsapp", TEST_GROUP)
    assert first.status_code == 303
    assert first.headers["location"] == "/admin/channels#bridged-whatsapp"
    assert choose(client, "whatsapp", NEIGHBOURS).status_code == 303
    third = choose(client, "signal", SIGNAL_GROUP, back="bridge")
    assert third.headers["location"] == "/admin/bridges/signal#rooms"

    assert agent.sessions == 1
    assert matrix.joined == {TEST_GROUP, NEIGHBOURS, SIGNAL_GROUP}
    invites = [params for op, params in agent.calls if op.endswith(".room_invite")]
    assert [item["user_id"] for item in invites] == [ANNOUNCER] * 3
    relays = [params["room_id"] for op, params in agent.calls if op.endswith(".room_relay")]
    assert relays == [NEIGHBOURS]

    labels = {a.name: a.capabilities.label for a in app.state.publisher.adapters}
    assert labels[destination_name("whatsapp", TEST_GROUP)] == "WhatsApp: Test"
    assert labels[destination_name("whatsapp", NEIGHBOURS)] == "WhatsApp: Neighbours"
    assert labels[destination_name("signal", SIGNAL_GROUP)] == "Signal: Signal crew"
    signal = next(a for a in app.state.publisher.adapters if a.name.startswith("signal_"))
    assert signal.capabilities.max_text_length == 2000

    page = client.get("/admin/channels")
    assert page.text.count("Receives announcements") == 3
    assert "announcer-token" not in page.text
    settings = client.get("/admin/settings")
    assert "WhatsApp: Neighbours" in settings.text

    events = app.state.auth_store.list_events()
    assert "announcer-token" not in str(events)
    assert {e["action"] for e in events} >= {"channel.connected"}


def test_choosing_a_group_again_is_harmless_and_a_lost_token_is_replaced(console):
    app, client, agent, matrix = console
    choose(client, "whatsapp", TEST_GROUP)
    matrix.valid_tokens.clear()

    again = choose(client, "whatsapp", TEST_GROUP)

    assert again.status_code == 303
    assert agent.sessions == 2
    assert app.state.storage.get_announcer()["access_token"] == "announcer-token-2"
    assert len(app.state.storage.list_bridged_destinations()) == 1


def test_encrypted_rooms_and_turned_off_bridges_are_refused(console, tmp_path):
    app, client, _, _ = console
    refused = choose(client, "whatsapp", ENCRYPTED)
    assert refused.status_code == 303
    page = client.get(refused.headers["location"])
    assert "end-to-end encrypted" in page.text

    write_config(tmp_path / "community-stack.yaml", signal=False)
    app.state.publisher.reload_connections()
    refused = choose(client, "signal", SIGNAL_GROUP)
    page = client.get(refused.headers["location"])
    assert "Turn the Signal bridge on under Services first." in page.text
    assert app.state.storage.list_bridged_destinations() == []


def test_test_reports_relay_and_stop_posting_leaves_the_room(console):
    app, client, agent, matrix = console
    choose(client, "whatsapp", TEST_GROUP)
    name = destination_name("whatsapp", TEST_GROUP)
    agent.rooms["whatsapp"][0]["relay"] = False
    page = client.get("/admin/channels")

    adapter = next(a for a in app.state.publisher.adapters if a.name == name)
    adapter._custom_transport = lambda method, path, headers, payload: {
        "/_matrix/client/v3/account/whoami": (200, {"user_id": ANNOUNCER}),
        "/_matrix/client/v3/joined_rooms": (200, {"joined_rooms": [TEST_GROUP]}),
    }.get(path, (404, {}))
    tested = client.post(f"/admin/channels/bridged/{name}/test", data={"csrf_token": csrf(page)})
    assert "Relay is off in this room" in client.get(tested.headers["location"]).text
    assert "Repair" in client.get("/admin/channels").text

    removed = client.post(f"/admin/channels/bridged/{name}/remove", data={"csrf_token": csrf(page)})
    assert removed.status_code == 303
    assert TEST_GROUP not in matrix.joined
    assert app.state.storage.list_bridged_destinations() == []
    assert not any(a.name == name for a in app.state.publisher.adapters)


def test_bridged_rooms_get_plain_messages_the_bridge_relays():
    sent = []

    def transport(method, path, headers, payload):
        sent.append(payload)
        return 200, {"event_id": "$event"}

    adapter = BridgedRoomAdapter(
        "whatsapp_abc", "WhatsApp: Test", "http://synapse", TEST_GROUP, "token", None, transport
    )
    announcement = Announcement(title="Water cut", body="Tomorrow morning.")
    assert adapter.publish(announcement).success
    adapter.update(announcement)
    adapter.withdraw(announcement.id)
    # The bridges drop m.notice (bridge_notices: false), so every message must be m.text.
    assert [payload["msgtype"] for payload in sent] == ["m.text"] * 3


def test_the_single_whatsapp_room_of_older_versions_is_kept(tmp_path):
    db_path = tmp_path / "announcements.db"
    storage = AnnouncementStorage(db_path)
    storage.save_connection("whatsapp", {"room_id": TEST_GROUP}, "olga")
    storage.save_connection(
        "matrix_room",
        {
            "homeserver_url": "http://community_synapse:8008",
            "room_id": "!announcements:community.test",
            "access_token": "matrix-token",
        },
        "olga",
    )

    storage = AnnouncementStorage(db_path)
    [destination] = storage.list_bridged_destinations()
    assert destination["name"] == "whatsapp"
    assert destination["account"] == "matrix_room"
    assert "whatsapp" not in storage.list_connections()

    config_path = tmp_path / "community-stack.yaml"
    write_config(config_path)
    publisher = AnnouncementPublisher(storage, config_path=config_path, feed_dir=tmp_path / "feed")
    adapter = next(a for a in publisher.adapters if a.name == "whatsapp")
    assert adapter.access_token == "matrix-token"
    assert not any(
        a.name == "whatsapp" and type(a).__name__ == "ManualChannelAdapter"
        for a in publisher.adapters
    )


class FakeSynapseAdmin:
    def __init__(self, exists=False, deactivated=False):
        self.users = {ANNOUNCER: {"deactivated": deactivated}} if exists else {}
        self.calls = []

    def __call__(self, method, path, body=None, token=None):
        self.calls.append((method, path, body))
        if path == "/_synapse/admin/v1/register":
            if method == "GET":
                return 200, {"nonce": "n"}
            return 200, {"user_id": "@community-stack-admin:community.test", "access_token": "a"}
        if path.endswith("/login"):
            return 200, {"access_token": "issued-token"}
        if path.endswith("/override_ratelimit"):
            return 200, {}
        if path.startswith("/_synapse/admin/v2/users/"):
            if method == "GET":
                return (200, self.users[ANNOUNCER]) if ANNOUNCER in self.users else (404, {})
            self.users[ANNOUNCER] = body
            return 201, {}
        return 404, {}


@pytest.fixture
def agent_project(tmp_path):
    root = tmp_path / "stack"
    (root / "data" / "matrix").mkdir(parents=True)
    (root / "data" / "matrix" / "homeserver.yaml").write_text(
        'server_name: "community.test"\nregistration_shared_secret: "shh"\n', encoding="utf-8"
    )
    return AgentContext(root)


def test_agent_creates_the_passwordless_announcer_and_issues_a_token(agent_project, monkeypatch):
    fake = FakeSynapseAdmin()
    monkeypatch.setattr(matrix_admin, "synapse_request", fake)

    result = matrix_admin.announcer_session(agent_project, {})

    assert result == {"user_id": ANNOUNCER, "access_token": "issued-token"}
    created = next(body for method, path, body in fake.calls if method == "PUT")
    assert "password" not in created and created["admin"] is False
    assert any(path.endswith("/override_ratelimit") for _, path, _ in fake.calls)
    assert CATALOG["matrix.announcer.session"].permission == "system"


def test_agent_reuses_the_announcer_and_refuses_a_deactivated_one(agent_project, monkeypatch):
    fake = FakeSynapseAdmin(exists=True)
    monkeypatch.setattr(matrix_admin, "synapse_request", fake)
    matrix_admin.announcer_session(agent_project, {})
    assert not any(method == "PUT" for method, _, _ in fake.calls)

    monkeypatch.setattr(
        matrix_admin, "synapse_request", FakeSynapseAdmin(exists=True, deactivated=True)
    )
    with pytest.raises(matrix_admin.MatrixAdminError, match="deactivated"):
        matrix_admin.announcer_session(agent_project, {})


def test_the_announcer_cannot_be_changed_from_the_accounts_page(agent_project, monkeypatch):
    monkeypatch.setattr(matrix_admin, "synapse_request", FakeSynapseAdmin())
    with pytest.raises(matrix_admin.MatrixAdminError, match="stack's own accounts"):
        matrix_admin.deactivate_user(agent_project, {"user_id": ANNOUNCER})
    with pytest.raises(matrix_admin.MatrixAdminError):
        matrix_admin.create_user(agent_project, {"username": "community-stack-announcements"})
