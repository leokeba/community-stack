"""The Safe Mode bot: appservice HTTP authentication, event handling and privacy in logs."""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from community_stack.safemode.bot import SafetyBot, make_handler
from community_stack.safemode.manager import AccessManager, GatewayError, RoomFacts
from community_stack.safemode.store import ProtectedRoom, Store

BOT = "@safemode:example.org"
ALICE = "@alice:example.org"
MALLORY = "@mallory:example.org"
HS_TOKEN = "hs-secret-value"
DM = "!dm:example.org"
GROUP = "!group:example.org"
ROOM_A = "!a:example.org"


class FakeBotGateway:
    bot_user_id = BOT

    def __init__(self) -> None:
        self.members = {DM: [ALICE, BOT], GROUP: [ALICE, MALLORY, BOT]}
        self.sent: list[tuple[str, str]] = []
        self.joined: list[str] = []
        self.left: list[str] = []
        self.kicks: list[tuple[str, str]] = []
        self.failing = False
        self.room_members = {ROOM_A: {ALICE: "join"}}

    def joined_members(self, room_id):
        if self.failing:
            raise GatewayError("down")
        return list(self.members.get(room_id, []))

    def join(self, room_id):
        self.joined.append(room_id)

    def leave(self, room_id):
        self.left.append(room_id)

    def send_notice(self, room_id, text, txn_id):
        self.sent.append((room_id, text))

    def room_facts(self, room_id):
        return RoomFacts(room_id, "11", True, "joined", "invite", frozenset({BOT}), True, True)

    def membership(self, room_id, user_id):
        return self.room_members.get(room_id, {}).get(user_id)

    def kick(self, room_id, user_id):
        self.kicks.append((room_id, user_id))
        self.room_members[room_id][user_id] = "leave"

    def invite(self, room_id, user_id):
        self.room_members.setdefault(room_id, {})[user_id] = "invite"


class InlineExecutor:
    """Runs the bot's background work at once, so tests can assert right after a transaction."""

    def submit(self, fn, *args):
        fn(*args)


class DeferredExecutor:
    """Holds background work until the test runs it."""

    def __init__(self) -> None:
        self.tasks = []

    def submit(self, fn, *args):
        self.tasks.append((fn, args))

    def run(self):
        while self.tasks:
            fn, args = self.tasks.pop(0)
            fn(*args)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "safemode.db")
    gateway = FakeBotGateway()
    manager = AccessManager(store, gateway, BOT)
    store.upsert_room(ProtectedRoom(ROOM_A, "Room A", True, True, "joined", "11"))
    manager.enroll(ALICE)
    clock = Clock()
    bot = SafetyBot(manager, gateway, clock=clock, executor=InlineExecutor())
    yield bot, gateway, manager, clock
    store.close()


def message(body, sender=ALICE, room=DM, event_id="$ev1", content_extra=None):
    content = {"msgtype": "m.text", "body": body}
    content.update(content_extra or {})
    return {
        "type": "m.room.message",
        "sender": sender,
        "room_id": room,
        "event_id": event_id,
        "content": content,
    }


def invite(sender, room=DM):
    return {
        "type": "m.room.member",
        "sender": sender,
        "room_id": room,
        "state_key": BOT,
        "content": {"membership": "invite"},
    }


def test_a_help_command_in_a_direct_room_gets_the_help_text(setup):
    bot, gateway, _, _ = setup
    bot.handle_transaction("t1", [message("!safe help")])
    assert gateway.sent[-1][0] == DM
    assert "Safe Mode commands" in gateway.sent[-1][1]


def test_on_suspends_and_replies_with_the_outcome(setup):
    bot, gateway, _, _ = setup
    bot.handle_transaction("t1", [message("!safe on")])
    assert gateway.kicks == [(ROOM_A, ALICE)]
    assert "Safe Mode activated" in gateway.sent[-1][1]


def test_on_is_recorded_before_acknowledging_and_the_kicks_run_afterwards(setup):
    _, gateway, manager, clock = setup
    deferred = DeferredExecutor()
    bot = SafetyBot(manager, gateway, clock=clock, executor=deferred)
    bot.handle_transaction("t1", [message("!safe on")])
    # The transaction returns with the suspension recorded but no Matrix change made yet.
    assert manager.store.state(ALICE)[0] == "SUSPENDING"
    assert gateway.kicks == [] and gateway.sent == []
    deferred.run()
    assert gateway.kicks == [(ROOM_A, ALICE)]
    assert "Safe Mode activated" in gateway.sent[-1][1]


def test_a_recorded_on_is_finished_by_reconcile_if_the_worker_never_ran(setup):
    _, gateway, manager, clock = setup
    bot = SafetyBot(manager, gateway, clock=clock, executor=DeferredExecutor())
    bot.handle_transaction("t1", [message("!safe on")])  # the bot then stops; tasks are lost
    assert bot.reconcile() == [ALICE]
    assert gateway.kicks == [(ROOM_A, ALICE)]


def member_event(target, membership, sender=None, room=ROOM_A):
    return {
        "type": "m.room.member",
        "sender": sender or target,
        "room_id": room,
        "state_key": target,
        "content": {"membership": membership},
    }


def test_a_suspended_account_let_back_into_a_protected_room_is_removed_again(setup):
    bot, gateway, manager, _ = setup
    bot.handle_transaction("t1", [message("!safe on")])
    gateway.room_members[ROOM_A][ALICE] = "invite"
    bot.handle_transaction("t2", [member_event(ALICE, "invite", sender=MALLORY)])
    assert gateway.kicks == [(ROOM_A, ALICE), (ROOM_A, ALICE)]
    assert manager.store.state(ALICE)[0] == "SUSPENDED"
    # Restore still brings back only what was joined before suspension.
    assert {m.room_id: m.previous for m in manager.store.memberships(ALICE)} == {ROOM_A: "join"}


def test_joins_to_unprotected_rooms_and_by_active_accounts_are_left_alone(setup):
    bot, gateway, _, _ = setup
    bot.handle_transaction("t1", [member_event(ALICE, "join")])  # Alice is not suspended
    bot.handle_transaction("t2", [message("!safe on")])
    gateway.room_members[GROUP] = {ALICE: "join"}
    bot.handle_transaction("t3", [member_event(ALICE, "join", room=GROUP)])  # not protected
    assert gateway.kicks == [(ROOM_A, ALICE)]


def encrypted(sender=ALICE, room=DM):
    return {
        "type": "m.room.encrypted",
        "sender": sender,
        "room_id": room,
        "content": {"algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "AwgA"},
    }


def test_an_encrypted_message_gets_a_clear_warning_and_changes_nothing(setup):
    bot, gateway, manager, _ = setup
    bot.handle_transaction("t1", [encrypted()])
    assert "NOT changed" in gateway.sent[-1][1]
    assert manager.store.state(ALICE) is None and gateway.kicks == []


def test_encrypted_messages_from_unknown_accounts_or_groups_get_no_reply(setup):
    bot, gateway, _, _ = setup
    bot.handle_transaction("t1", [encrypted(sender=MALLORY)])
    bot.handle_transaction("t2", [encrypted(room=GROUP)])
    assert gateway.sent == []


def test_unknown_senders_get_no_reply_and_no_action(setup):
    bot, gateway, _, _ = setup
    bot.handle_transaction("t1", [message("!safe on", sender=MALLORY)])
    assert gateway.sent == []
    assert gateway.kicks == []


def test_commands_in_group_rooms_are_ignored(setup):
    bot, gateway, _, _ = setup
    bot.handle_transaction("t1", [message("!safe on", room=GROUP)])
    assert gateway.sent == []
    assert gateway.kicks == []


def test_the_bots_own_messages_are_ignored(setup):
    bot, gateway, _, _ = setup
    bot.handle_transaction("t1", [message("!safe on", sender=BOT)])
    assert gateway.sent == [] and gateway.kicks == []


def test_ordinary_chat_is_ignored(setup):
    bot, gateway, _, _ = setup
    bot.handle_transaction("t1", [message("see you at the square")])
    assert gateway.sent == []


def test_quoted_commands_are_refused_with_a_message(setup):
    bot, gateway, _, _ = setup
    bot.handle_transaction(
        "t1",
        [
            message(
                "!safe on", content_extra={"m.relates_to": {"m.in_reply_to": {"event_id": "$x"}}}
            )
        ],
    )
    assert gateway.kicks == []
    assert "Replies and forwarded" in gateway.sent[-1][1]


def test_repeated_commands_are_rate_limited(setup):
    bot, gateway, _, clock = setup
    bot.handle_transaction("t1", [message("!safe status", event_id="$a")])
    bot.handle_transaction("t2", [message("!safe status", event_id="$b")])
    assert "wait a few seconds" in gateway.sent[-1][1]
    clock.now += 10
    bot.handle_transaction("t3", [message("!safe status", event_id="$c")])
    assert "wait a few seconds" not in gateway.sent[-1][1]


def test_a_retried_transaction_is_not_processed_twice(setup):
    bot, gateway, _, _ = setup
    events = [message("!safe on")]
    bot.handle_transaction("same", events)
    bot.handle_transaction("same", events)
    assert len(gateway.sent) == 1


def test_an_invite_from_an_enrolled_account_joins_and_sends_help(setup):
    bot, gateway, _, _ = setup
    gateway.members[DM] = [ALICE, BOT]
    bot.handle_transaction("t1", [invite(ALICE, DM)])
    assert gateway.joined == [DM]
    assert "Safe Mode commands" in gateway.sent[-1][1]


def test_an_invite_from_an_unknown_account_is_declined(setup):
    bot, gateway, _, _ = setup
    bot.handle_transaction("t1", [invite(MALLORY, DM)])
    assert gateway.joined == []
    assert gateway.left == [DM]
    assert gateway.sent == []


def test_an_invite_into_a_group_is_left_after_joining(setup):
    bot, gateway, _, _ = setup
    bot.handle_transaction("t1", [invite(ALICE, GROUP)])
    assert gateway.left == [GROUP]


def test_a_failing_event_does_not_stop_the_rest_of_the_transaction(setup):
    bot, gateway, _, _ = setup
    gateway.failing = True
    bot.handle_transaction("t1", [message("!safe status", event_id="$x")])
    gateway.failing = False
    bot.handle_transaction("t2", [message("!safe help", event_id="$y")])
    assert "Safe Mode commands" in gateway.sent[-1][1]


def test_message_bodies_never_reach_the_logs(setup, caplog):
    bot, gateway, _, _ = setup
    caplog.set_level(logging.DEBUG)
    gateway.failing = True
    secret = "ABCD-EFGH-JKLM-NPQR"
    bot.handle_transaction("t1", [message(f"!safe off {secret}")])
    assert secret not in caplog.text
    assert "!safe" not in caplog.text


# HTTP appservice endpoint ------------------------------------------------


@pytest.fixture
def server(setup):
    bot, gateway, _, _ = setup
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(bot, HS_TOKEN))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", gateway
    httpd.shutdown()
    httpd.server_close()


def _put(base, path, payload, token=HS_TOKEN):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        method="PUT",
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_transactions_without_the_homeserver_token_are_refused(server):
    base, gateway = server
    status, _ = _put(
        base, "/_matrix/app/v1/transactions/t1", {"events": [message("!safe help")]}, token=None
    )
    assert status == 403
    assert gateway.sent == []


def test_transactions_with_the_wrong_token_are_refused(server):
    base, gateway = server
    status, _ = _put(
        base, "/_matrix/app/v1/transactions/t1", {"events": [message("!safe help")]}, token="nope"
    )
    assert status == 403
    assert gateway.sent == []


def test_an_authenticated_transaction_is_processed_and_acknowledged(server):
    base, gateway = server
    status, _ = _put(base, "/_matrix/app/v1/transactions/t1", {"events": [message("!safe help")]})
    assert status == 200
    assert "Safe Mode commands" in gateway.sent[-1][1]


def test_unknown_paths_are_not_found(server):
    base, _ = server
    status, _ = _put(base, "/_matrix/app/v1/users/anything", {})
    assert status == 404


def _post(base, path, payload, token=HS_TOKEN):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_operator_drive_requests_need_the_homeserver_token(server):
    base, _ = server
    status, _ = _post(base, "/_safemode/v1/drive", {"user_id": ALICE}, token="nope")
    assert status == 403


def test_an_administrative_restore_is_carried_out_by_the_bot(setup, server):
    _, _, manager, _ = setup
    base, gateway = server
    manager.activate(ALICE)
    assert manager.start_admin_restore(ALICE) is None  # recorded only; no Matrix call yet
    assert gateway.room_members[ROOM_A][ALICE] == "leave"
    status, body = _post(base, "/_safemode/v1/drive", {"user_id": ALICE})
    assert status == 200 and "off" in body["text"]
    assert gateway.room_members[ROOM_A][ALICE] == "invite"


def test_health_is_open_without_a_token(server):
    base, _ = server
    with urllib.request.urlopen(base + "/health", timeout=5) as response:
        assert json.loads(response.read()) == {"status": "ok"}
