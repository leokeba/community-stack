"""Safe Mode: command parsing, room compatibility and the Access Manager's state rules."""

from __future__ import annotations

import pytest
from community_stack.safemode import store as s
from community_stack.safemode.commands import CommandError, parse
from community_stack.safemode.manager import (
    AccessManager,
    GatewayError,
    RoomFacts,
    SafeModeError,
    compatibility_problems,
)
from community_stack.safemode.store import Store

BOT = "@safety:example.org"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"


def good_facts(room_id: str, creators=frozenset({BOT})) -> RoomFacts:
    return RoomFacts(
        room_id=room_id,
        room_version="11",
        encrypted=True,
        history_visibility="joined",
        join_rule="invite",
        creators=creators,
        bot_can_invite=True,
        bot_can_kick=True,
    )


class FakeGateway:
    """In-memory homeserver: room facts plus per-user membership."""

    def __init__(self) -> None:
        self.facts: dict[str, RoomFacts] = {}
        self.members: dict[str, dict[str, str]] = {}
        self.failing_kicks: set[str] = set()
        self.failing_invites: set[str] = set()
        self.kicks: list[tuple[str, str]] = []
        self.invites: list[tuple[str, str]] = []

    def add_room(self, room_id: str, members: dict[str, str], facts: RoomFacts | None = None):
        self.facts[room_id] = facts or good_facts(room_id)
        self.members[room_id] = dict(members)

    def room_facts(self, room_id):
        return self.facts[room_id]

    def membership(self, room_id, user_id):
        return self.members[room_id].get(user_id)

    def kick(self, room_id, user_id):
        if room_id in self.failing_kicks:
            raise GatewayError("forbidden")
        self.kicks.append((room_id, user_id))
        self.members[room_id][user_id] = "leave"

    def invite(self, room_id, user_id):
        if room_id in self.failing_invites:
            raise GatewayError("forbidden")
        self.invites.append((room_id, user_id))
        self.members[room_id][user_id] = "invite"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "safemode.db"


@pytest.fixture
def gateway():
    gw = FakeGateway()
    gw.add_room("!a:example.org", {ALICE: "join", BOB: "join"})
    gw.add_room("!b:example.org", {ALICE: "join"})
    gw.add_room("!c:example.org", {BOB: "join"})
    return gw


@pytest.fixture
def manager(db_path, gateway):
    store = Store(db_path)
    mgr = AccessManager(store, gateway, BOT)
    for room_id, name in [
        ("!a:example.org", "Room A"),
        ("!b:example.org", "Room B"),
        ("!c:example.org", "Room C"),
    ]:
        assert mgr.protect_room(room_id, name) == []
    mgr.enroll(ALICE)
    mgr.enroll(BOB)
    yield mgr
    store.close()


# Command parsing ---------------------------------------------------------


@pytest.mark.parametrize(
    "body, verb, code",
    [
        ("!safe on", "on", None),
        ("  !SAFE   ON  ", "on", None),
        ("!safe status", "status", None),
        ("!safe help", "help", None),
        ("!safe off", "off", None),
        ("!safe off ABCD-EFGH-JKLM-NPQR", "off", "ABCD-EFGH-JKLM-NPQR"),
    ],
)
def test_valid_commands_parse(body, verb, code):
    command = parse(body, content={"msgtype": "m.text", "body": body})
    assert command.verb == verb
    assert command.code == code


def test_ordinary_chat_is_ignored():
    assert parse("hello there") is None
    assert parse("!safety on") is None
    assert parse(None) is None


@pytest.mark.parametrize("body", ["!safe", "!safe launch", "!safe on now", "!safe off a b"])
def test_malformed_commands_are_rejected(body):
    with pytest.raises(CommandError):
        parse(body)


def test_replies_are_rejected_so_quoted_commands_cannot_be_replayed():
    content = {"body": "!safe off ABCD", "m.relates_to": {"m.in_reply_to": {"event_id": "$x"}}}
    with pytest.raises(CommandError):
        parse("!safe off ABCD", content=content)


def test_quoted_text_in_the_body_is_rejected():
    with pytest.raises(CommandError):
        parse("!safe off\n> !safe off ABCD")


def test_forwarded_content_is_rejected():
    with pytest.raises(CommandError):
        parse("!safe on", content={"m.forward_info": {}})


def test_repr_never_shows_a_recovery_code():
    command = parse("!safe off ABCD-EFGH-JKLM-NPQR")
    assert "ABCD" not in repr(command)


# Room compatibility ------------------------------------------------------


def test_compatible_room_has_no_problems():
    assert compatibility_problems(good_facts("!x:e"), BOT) == []


def test_unencrypted_shared_history_open_room_is_refused():
    facts = RoomFacts(
        room_id="!x:e",
        room_version="11",
        encrypted=False,
        history_visibility="shared",
        join_rule="public",
        creators=frozenset({BOT}),
        bot_can_invite=True,
        bot_can_kick=True,
    )
    problems = compatibility_problems(facts, BOT)
    assert any("end-to-end" in p for p in problems)
    assert any("shared" in p for p in problems)
    assert any("invite-only" in p for p in problems)


def test_room_created_by_a_person_is_refused(manager, gateway):
    gateway.facts["!person:example.org"] = good_facts(
        "!person:example.org", creators=frozenset({ALICE})
    )
    problems = manager.protect_room("!person:example.org", "Person room")
    assert any("created by a person" in p for p in problems)
    assert not manager.store.room("!person:example.org")


def test_missing_bot_power_is_refused(manager, gateway):
    facts = good_facts("!weak:example.org")
    gateway.facts["!weak:example.org"] = RoomFacts(**{**facts.__dict__, "bot_can_kick": False})
    assert manager.protect_room("!weak:example.org", "Weak") != []


# Activation --------------------------------------------------------------


def test_activation_suspends_every_protected_membership_account_wide(manager, gateway):
    report = manager.activate(ALICE)
    assert report.state == s.SUSPENDED
    assert {room for room, _ in gateway.kicks} == {"!a:example.org", "!b:example.org"}
    assert gateway.membership("!a:example.org", ALICE) == "leave"
    assert gateway.membership("!a:example.org", BOB) == "join"  # other people unaffected
    assert "suspended" in report.text().lower()


def test_activation_is_idempotent(manager, gateway):
    manager.activate(ALICE)
    before = list(gateway.kicks)
    report = manager.activate(ALICE)
    assert report.state == s.SUSPENDED
    assert gateway.kicks == before


def test_partial_failure_reports_partial_and_retries_on_next_command(manager, gateway):
    gateway.failing_kicks.add("!b:example.org")
    report = manager.activate(ALICE)
    assert report.state == s.PARTIAL
    assert "PARTIAL" in report.text()
    assert "!b:example.org" not in [room for room, _ in gateway.kicks]

    gateway.failing_kicks.clear()
    assert manager.activate(ALICE).state == s.SUSPENDED
    assert gateway.membership("!b:example.org", ALICE) == "leave"


def test_retry_never_overwrites_the_original_snapshot(manager, gateway):
    gateway.failing_kicks.add("!b:example.org")
    manager.activate(ALICE)
    gateway.failing_kicks.clear()
    manager.activate(ALICE)
    # The snapshot still says Alice was joined to !b, even though she is now left there.
    previous = {m.room_id: m.previous for m in manager.store.memberships(ALICE)}
    assert previous["!b:example.org"] == "join"


def test_unenrolled_sender_is_refused(manager, gateway):
    from community_stack.safemode.commands import Command

    reply = manager.handle("@stranger:example.org", Command("on"))
    assert "not available" in reply
    assert gateway.kicks == []


def test_status_text_hides_room_names_and_other_people(manager):
    manager.activate(ALICE)
    text = manager.handle(ALICE, parse("!safe status"))
    assert "Room A" not in text and BOB not in text and "!a:" not in text


# Reconciliation after an interruption ------------------------------------


def test_restart_during_activation_is_completed_by_reconcile(db_path, gateway):
    store = Store(db_path)
    mgr = AccessManager(store, gateway, BOT)
    for room_id in ["!a:example.org", "!b:example.org", "!c:example.org"]:
        mgr.protect_room(room_id, room_id)
    mgr.enroll(ALICE)
    # Simulate a crash after the snapshot was written and before any kick ran.
    store.begin_suspension(ALICE, "op1", {"!a:example.org": "join", "!b:example.org": "join"})
    store.close()

    restarted = AccessManager(Store(db_path), gateway, BOT)
    assert restarted.reconcile() == [ALICE]
    assert restarted.store.state(ALICE)[0] == s.SUSPENDED
    assert gateway.membership("!a:example.org", ALICE) == "leave"


# Restore -----------------------------------------------------------------


def issue(manager) -> str:
    return manager.issue_recovery_code(ALICE)


def test_off_without_a_code_is_refused_and_changes_nothing(manager, gateway):
    manager.activate(ALICE)
    reply = manager.restore(ALICE, None).text()
    assert "recovery code" in reply
    assert manager.store.state(ALICE)[0] == s.SUSPENDED
    assert gateway.membership("!a:example.org", ALICE) == "leave"


def test_recovery_codes_are_stored_only_as_hashes(manager, db_path):
    code = issue(manager)
    row = manager.store.recovery_code(ALICE)
    assert code.replace("-", "").encode() not in bytes(row["code_hash"])
    assert bytes(row["code_hash"]) != code.encode()


def test_wrong_code_is_refused_and_locks_after_repeated_failures(manager):
    issue(manager)
    manager.activate(ALICE)
    for _ in range(5):
        assert "not correct" in manager.restore(ALICE, "WRONG-CODE-1234-5678").text()
    assert "Too many" in manager.restore(ALICE, "WRONG-CODE-1234-5678").text()


def test_correct_code_restores_only_rooms_joined_before_suspension(manager, gateway):
    code = issue(manager)
    manager.activate(ALICE)
    reply = manager.restore(ALICE, code.lower().replace("-", " "))
    assert manager.store.state(ALICE) is None  # ACTIVE
    assert sorted(room for room, _ in gateway.invites) == ["!a:example.org", "!b:example.org"]
    assert "Historical messages" in reply.text()


def test_code_is_one_time(manager):
    code = issue(manager)
    manager.activate(ALICE)
    manager.restore(ALICE, code)
    manager.activate(ALICE)
    assert "No recovery code is set" in manager.restore(ALICE, code).text()


def test_a_restore_is_never_granted_to_invite_only_memberships(db_path, gateway):
    gateway.members["!b:example.org"][BOB] = "invite"  # Bob was invited to B but never joined
    store = Store(db_path)
    mgr = AccessManager(store, gateway, BOT)
    for room_id in ["!a:example.org", "!b:example.org", "!c:example.org"]:
        mgr.protect_room(room_id, room_id)
    mgr.enroll(BOB)
    code = mgr.issue_recovery_code(BOB)
    mgr.activate(BOB)
    mgr.restore(BOB, code)
    assert "!b:example.org" not in [room for room, _ in gateway.invites if _ == BOB]


def test_failed_restore_stays_restoring_and_reconcile_completes_it(manager, gateway):
    code = issue(manager)
    manager.activate(ALICE)
    gateway.failing_invites.add("!b:example.org")
    manager.restore(ALICE, code)
    assert manager.store.state(ALICE)[0] == s.RESTORING
    assert "Safe Mode is not on" not in manager.status_text(ALICE)

    gateway.failing_invites.clear()
    manager.reconcile()
    assert manager.store.state(ALICE) is None


def test_activation_is_refused_while_restoring(manager, gateway):
    code = issue(manager)
    manager.activate(ALICE)
    gateway.failing_invites.add("!b:example.org")
    manager.restore(ALICE, code)
    with pytest.raises(SafeModeError):
        manager.activate(ALICE)
    assert "already being restored" in manager.restore(ALICE, code).text()


def test_disabled_room_is_skipped_on_restore(manager, gateway):
    code = issue(manager)
    manager.activate(ALICE)
    manager.unprotect_room("!b:example.org")
    manager.restore(ALICE, code)
    assert manager.store.state(ALICE) is None
    assert "!b:example.org" not in [room for room, _ in gateway.invites]


def test_restore_without_suspension_is_a_no_op(manager):
    issue(manager)
    assert manager.restore(ALICE, "ANY").text() == "Safe Mode is not on."


def test_administrative_restore_needs_explicit_confirmation(manager):
    manager.activate(ALICE)
    with pytest.raises(SafeModeError, match="--confirm"):
        manager.admin_restore(ALICE, confirmed=False)
    assert manager.store.state(ALICE)[0] == s.SUSPENDED


def test_administrative_restore_brings_back_rooms_without_a_member_code(manager, gateway):
    manager.activate(ALICE)
    message = manager.admin_restore(ALICE, confirmed=True)
    assert manager.store.state(ALICE) is None
    assert "Historical messages" in message
    assert sorted(room for room, _ in gateway.invites) == ["!a:example.org", "!b:example.org"]


def test_administrative_restore_invalidates_an_outstanding_member_code(manager):
    code = issue(manager)
    manager.activate(ALICE)
    manager.admin_restore(ALICE, confirmed=True)
    manager.activate(ALICE)
    assert "not correct" not in manager.restore(ALICE, code).text()
    assert "No recovery code is set" in manager.restore(ALICE, code).text()


def test_administrative_restore_of_an_active_account_is_a_no_op(manager):
    assert manager.admin_restore(ALICE, confirmed=True) == "Safe Mode is not on for this account."
