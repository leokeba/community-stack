"""The Synapse gateway, tested against a local HTTP server that imitates the Client-Server API."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from community_stack.safemode.manager import GatewayError
from community_stack.safemode.synapse import SynapseGateway

BOT = "@safemode:example.org"
ALICE = "@alice:example.org"
ROOM = "!room:example.org"


class FakeSynapse:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], tuple[int, object]] = {}
        self.requests: list[dict] = []

    def route(self, method: str, path: str, status: int, body: object) -> None:
        self.routes[(method, path)] = (status, body)


@pytest.fixture
def fake():
    state = FakeSynapse()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep test output quiet
            return

        def _serve(self):
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b""
            parsed = urlparse(self.path)
            record = {
                "method": self.command,
                "path": parsed.path,
                "query": {k: v[0] for k, v in parse_qs(parsed.query).items()},
                "body": json.loads(raw) if raw else None,
                "auth": self.headers.get("Authorization"),
            }
            state.requests.append(record)
            status, body = state.routes.get(
                (self.command, parsed.path), (404, {"errcode": "M_NOT_FOUND"})
            )
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = _serve

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()
    server.server_close()


@pytest.fixture
def gateway(fake):
    return SynapseGateway(fake.base_url, BOT, "as-secret")


def _state(
    room_version="11", creator=ALICE, power=None, encryption=True, history="joined", join="invite"
):
    events = [
        {
            "type": "m.room.create",
            "state_key": "",
            "sender": creator,
            "content": {"room_version": room_version},
        },
        {"type": "m.room.join_rules", "state_key": "", "content": {"join_rule": join}},
        {
            "type": "m.room.history_visibility",
            "state_key": "",
            "content": {"history_visibility": history},
        },
        {
            "type": "m.room.power_levels",
            "state_key": "",
            "content": power
            if power is not None
            else {"users": {ALICE: 100, BOT: 100}, "users_default": 0, "kick": 50, "invite": 0},
        },
    ]
    if encryption:
        events.append(
            {
                "type": "m.room.encryption",
                "state_key": "",
                "content": {"algorithm": "m.megolm.v1.aes-sha2"},
            }
        )
    return events


def test_room_facts_are_read_from_state_events(fake, gateway):
    fake.route("GET", "/_matrix/client/v3/rooms/%21room%3Aexample.org/state", 200, _state())
    facts = gateway.room_facts(ROOM)
    assert facts.encrypted is True
    assert facts.history_visibility == "joined"
    assert facts.join_rule == "invite"
    assert facts.room_version == "11"
    assert facts.creators == frozenset({ALICE})
    assert facts.bot_can_kick and facts.bot_can_invite
    request = fake.requests[-1]
    assert request["query"]["user_id"] == BOT, "room reads are made as the bot, via the appservice"
    assert request["auth"] == "Bearer as-secret"


def test_bot_that_created_the_room_has_infinite_power_in_version_12(fake, gateway):
    state = _state(
        room_version="12",
        creator=BOT,
        power={"users": {}, "users_default": 0, "kick": 50, "invite": 0},
    )
    fake.route("GET", "/_matrix/client/v3/rooms/%21room%3Aexample.org/state", 200, state)
    facts = gateway.room_facts(ROOM)
    assert facts.creators == frozenset({BOT})
    assert facts.bot_can_kick and facts.bot_can_invite


def test_bot_without_kick_power_is_reported(fake, gateway):
    power = {"users": {ALICE: 100, BOT: 10}, "users_default": 0, "kick": 50, "invite": 0}
    fake.route(
        "GET", "/_matrix/client/v3/rooms/%21room%3Aexample.org/state", 200, _state(power=power)
    )
    facts = gateway.room_facts(ROOM)
    assert facts.bot_can_kick is False
    assert facts.bot_can_invite is True


def test_version_12_additional_creators_are_counted(fake, gateway):
    state = _state(room_version="12", creator=BOT)
    state[0]["content"]["additional_creators"] = [ALICE]
    fake.route("GET", "/_matrix/client/v3/rooms/%21room%3Aexample.org/state", 200, state)
    assert gateway.room_facts(ROOM).creators == frozenset({BOT, ALICE})


def test_missing_membership_reads_as_none(fake, gateway):
    assert gateway.membership(ROOM, ALICE) is None  # 404 M_NOT_FOUND from the fake


def test_membership_is_read_for_a_present_user(fake, gateway):
    fake.route(
        "GET",
        "/_matrix/client/v3/rooms/%21room%3Aexample.org/state/m.room.member/%40alice%3Aexample.org",
        200,
        {"membership": "join"},
    )
    assert gateway.membership(ROOM, ALICE) == "join"


def test_kick_and_invite_use_the_bot_identity(fake, gateway):
    fake.route("POST", "/_matrix/client/v3/rooms/%21room%3Aexample.org/kick", 200, {})
    fake.route("POST", "/_matrix/client/v3/rooms/%21room%3Aexample.org/invite", 200, {})
    gateway.kick(ROOM, ALICE)
    gateway.invite(ROOM, ALICE)
    kick, invite = fake.requests[-2], fake.requests[-1]
    assert kick["body"] == {"user_id": ALICE, "reason": "Safe Mode"}
    assert kick["query"]["user_id"] == BOT
    assert invite["body"] == {"user_id": ALICE}


def test_homeserver_refusals_become_gateway_errors(fake, gateway):
    fake.route(
        "POST",
        "/_matrix/client/v3/rooms/%21room%3Aexample.org/kick",
        403,
        {"errcode": "M_FORBIDDEN"},
    )
    with pytest.raises(GatewayError, match="M_FORBIDDEN"):
        gateway.kick(ROOM, ALICE)


def test_unreachable_homeserver_is_a_gateway_error():
    dead = SynapseGateway("http://127.0.0.1:9", BOT, "as-secret", timeout=1)
    with pytest.raises(GatewayError, match="could not be reached"):
        dead.joined_members(ROOM)


def test_joined_members_lists_the_room_members(fake, gateway):
    fake.route(
        "GET",
        "/_matrix/client/v3/rooms/%21room%3Aexample.org/joined_members",
        200,
        {"joined": {ALICE: {}, BOT: {}}},
    )
    assert gateway.joined_members(ROOM) == [ALICE, BOT]


def test_notices_are_sent_as_the_bot(fake, gateway):
    fake.route(
        "PUT",
        "/_matrix/client/v3/rooms/%21room%3Aexample.org/send/m.room.message/txn1",
        200,
        {"event_id": "$e"},
    )
    gateway.send_notice(ROOM, "hello", "txn1")
    sent = fake.requests[-1]
    assert sent["body"] == {"msgtype": "m.notice", "body": "hello"}
    assert sent["query"]["user_id"] == BOT


def test_bot_registration_tolerates_an_existing_account(fake, gateway):
    fake.route("POST", "/_matrix/client/v3/register", 400, {"errcode": "M_USER_IN_USE"})
    gateway.register_bot()  # no exception


def test_bot_registration_surfaces_other_failures(fake, gateway):
    fake.route("POST", "/_matrix/client/v3/register", 403, {"errcode": "M_FORBIDDEN"})
    with pytest.raises(GatewayError):
        gateway.register_bot()


def test_created_rooms_are_owned_by_the_bot_and_compatible(fake, gateway):
    fake.route("POST", "/_matrix/client/v3/createRoom", 200, {"room_id": "!new"})
    assert gateway.create_room("Organising", [ALICE]) == "!new"
    request = fake.requests[-1]
    assert request["query"]["user_id"] == BOT
    body = request["body"]
    assert body["preset"] == "private_chat" and body["invite"] == [ALICE]
    state = {event["type"]: event["content"] for event in body["initial_state"]}
    assert state["m.room.encryption"]["algorithm"] == "m.megolm.v1.aes-sha2"
    assert state["m.room.history_visibility"] == {"history_visibility": "joined"}
    assert state["m.room.guest_access"] == {"guest_access": "forbidden"}


def test_a_create_room_reply_without_an_id_is_an_error(fake, gateway):
    fake.route("POST", "/_matrix/client/v3/createRoom", 200, {})
    with pytest.raises(GatewayError):
        gateway.create_room("Organising", [])
