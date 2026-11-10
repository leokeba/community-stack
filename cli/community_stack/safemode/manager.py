"""The Access Manager: the rules for suspending and restoring Safe Mode access.

Matrix is reached only through ``RoomGateway``. The manager never holds homeserver admin
credentials. Every state change is written to the store before the Matrix call it
authorises, so an interrupted run is finished by ``reconcile()`` rather than left half done.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass, field
from typing import Protocol

from community_stack.safemode import store as s
from community_stack.safemode.commands import Command
from community_stack.safemode.store import ProtectedRoom, StateConflict, Store

MAX_CODE_ATTEMPTS = 5
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I/L, easy to read aloud
JOINED_HISTORY = "joined"


class GatewayError(Exception):
    """Matrix refused or could not complete a room operation."""


class SafeModeError(Exception):
    """A request the manager refuses; the message is safe to show to the sender."""


@dataclass(frozen=True)
class RoomFacts:
    room_id: str
    room_version: str
    encrypted: bool
    history_visibility: str
    join_rule: str
    creators: frozenset[str]
    bot_can_invite: bool
    bot_can_kick: bool


class RoomGateway(Protocol):
    def room_facts(self, room_id: str) -> RoomFacts: ...
    def membership(self, room_id: str, user_id: str) -> str | None: ...
    def kick(self, room_id: str, user_id: str) -> None: ...
    def invite(self, room_id: str, user_id: str) -> None: ...


def compatibility_problems(facts: RoomFacts, bot_user_id: str) -> list[str]:
    """Reasons a room cannot be marked protected. An empty list means it is compatible."""
    problems: list[str] = []
    if not facts.encrypted:
        problems.append("the room is not end-to-end encrypted")
    if facts.history_visibility != JOINED_HISTORY:
        problems.append(
            f"history visibility is '{facts.history_visibility}', it must be '{JOINED_HISTORY}'"
        )
    if facts.join_rule != "invite":
        problems.append("anyone can join the room; it must be invite-only")
    if not facts.bot_can_kick or not facts.bot_can_invite:
        problems.append("the service account needs permission to invite and remove members")
    other_creators = sorted(facts.creators - {bot_user_id})
    if other_creators:
        # In room version 12 a creator has infinite power and cannot be removed by others.
        # Even on older versions a person-created room needs a manual, explicit decision.
        problems.append("the room was created by a person, not the service account")
    return problems


@dataclass
class ActivationReport:
    state: str
    rooms_checked: int = 0
    suspended: int = 0
    failed: int = 0
    failed_room_ids: list[str] = field(default_factory=list)  # ids only, never names
    needs_new_code: bool = False

    def text(self) -> str:
        if self.state == s.ACTIVE:
            return "Safe Mode is off."
        if self.failed == 0:
            body = (
                "Safe Mode activated.\n\n"
                f"{self.rooms_checked} protected rooms checked.\n"
                f"{self.suspended} memberships suspended.\n\n"
                "Normal community rooms remain accessible.\n\n"
                "Server-side access has been suspended. Local message history previously "
                "downloaded to your devices may still be accessible."
            )
        else:
            body = (
                "Safe Mode partially activated.\n\n"
                f"{self.suspended} rooms successfully suspended.\n"
                f"{self.failed} rooms could not be suspended or checked.\n\n"
                "Your Safe Mode status is PARTIAL. Send !safe on again to retry.\n\n"
                "Use !safe status for details."
            )
        if self.needs_new_code:
            body += (
                "\n\nNo recovery code is set for this account. To turn Safe Mode off later, "
                "ask an administrator for a new one."
            )
        return body


class AccessManager:
    """Member commands run in two steps:

    1. ``submit`` records the command's effect in the store (state, operation id, snapshot
       rows, spent code) without calling Matrix. It is fast and safe to run before the
       homeserver's transaction is acknowledged, so an accepted ``!safe on`` survives a crash.
    2. ``drive`` does the Matrix work for the account's current operation. Drives for one
       account run one at a time; a drive whose operation was replaced stops at the next
       room, and the newer operation's drive runs after it.
    """

    def __init__(self, store: Store, gateway: RoomGateway, bot_user_id: str) -> None:
        self.store = store
        self.gateway = gateway
        self.bot_user_id = bot_user_id
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # Administration --------------------------------------------------------

    def protect_room(self, room_id: str, name: str) -> list[str]:
        """Mark a room protected if it is compatible. Returns the problems, empty on success."""
        facts = self.gateway.room_facts(room_id)
        problems = compatibility_problems(facts, self.bot_user_id)
        if problems:
            return problems
        self.store.upsert_room(
            ProtectedRoom(
                room_id=room_id,
                name=name,
                enabled=True,
                encryption_enabled=facts.encrypted,
                history_visibility=facts.history_visibility,
                room_version=facts.room_version,
            )
        )
        return []

    def unprotect_room(self, room_id: str) -> bool:
        return self.store.set_room_enabled(room_id, False)

    def enroll(self, user_id: str) -> None:
        self.store.enroll(user_id)

    def issue_recovery_code(self, user_id: str) -> str:
        """Create a new one-time code. Shown once to the administrator, then stored only as a hash."""
        if not self.store.is_enrolled(user_id):
            raise SafeModeError("This account is not enrolled in Safe Mode.")
        code = _generate_code()
        salt = secrets.token_bytes(16)
        self.store.set_recovery_code(user_id, salt, _hash_code(code, salt))
        return code

    # Member commands -------------------------------------------------------

    def submit(self, sender: str, command: Command) -> Submitted:
        """Step 1: record the command. Never calls Matrix."""
        if not self.store.is_enrolled(sender):
            return Submitted(
                reply="Safe Mode is not available for this account. Ask an administrator."
            )
        if command.verb == "help":
            return Submitted(reply=HELP)
        if command.verb == "status":
            return Submitted(reply=self.status_text(sender))
        if command.verb == "on":
            return Submitted(operation_id=self._start_activation(sender), verb="on")
        if command.verb == "off":
            return self._start_restore(sender, command.code)
        raise SafeModeError("Unknown command.")

    def drive(self, user_id: str, operation_id: str | None = None) -> bool:
        """Step 2: do the Matrix work for the account's current operation.

        With ``operation_id``, does nothing unless that operation is still current. Returns
        True when this call carried the operation as far as it can go now (so its outcome
        should be reported), False when a newer operation replaced it.
        """
        with self._user_lock(user_id):
            current = self.store.state(user_id)
            if current is None:
                return operation_id is None
            state, current_op = current
            if operation_id is not None and current_op != operation_id:
                return False
            if state == s.RESTORING:
                return self._drive_restore(user_id, current_op)
            if state in (s.SUSPENDING, s.PARTIAL):
                return self._drive_suspension(user_id, current_op)
            return True

    def outcome_text(self, user_id: str, verb: str) -> str:
        if verb == "on":
            return self._activation_report(user_id).text()
        return self._restore_text(user_id)

    def handle(self, sender: str, command: Command) -> str:
        """Run one command to completion and return the reply text (tests and operator tools)."""
        submitted = self.submit(sender, command)
        if submitted.reply is not None:
            return submitted.reply
        self.drive(sender, submitted.operation_id)
        return self.outcome_text(sender, submitted.verb)

    def activate(self, user_id: str) -> ActivationReport:
        self.drive(user_id, self._start_activation(user_id))
        return self._activation_report(user_id)

    def restore(self, user_id: str, code: str | None) -> Reply:
        submitted = self._start_restore(user_id, code)
        if submitted.reply is not None:
            return Reply(submitted.reply)
        self.drive(user_id, submitted.operation_id)
        return Reply(self._restore_text(user_id))

    def start_admin_restore(self, user_id: str) -> str | None:
        """Record an administrative restore without the member's code. Returns why it was
        refused, or None once RESTORING is recorded and a drive can finish it."""
        try:
            self.store.start_restore(user_id, _new_id(), code_hash=None)
        except StateConflict as exc:
            if exc.state == s.ACTIVE:
                return "Safe Mode is not on for this account."
            return "An operation is in progress for this account. Run access doctor."
        return None

    def admin_restore(self, user_id: str, confirmed: bool) -> str:
        """Administrative restore without the member's code. Explicitly confirmed, and only
        for an account that is actually suspended. Member codes are never bypassed silently."""
        if not confirmed:
            raise SafeModeError("Administrative restore needs --confirm.")
        refused = self.start_admin_restore(user_id)
        if refused:
            return refused
        self.drive(user_id)
        return self._restore_text(user_id)

    def enforce(self, user_id: str, room_id: str) -> bool:
        """A suspended account was invited to or joined a protected room: remove it again.
        Returns True when the room was put back on the account's outstanding list."""
        room = self.store.room(room_id)
        if room is None or not room.enabled:
            return False
        if not self.store.reopen_room(user_id, room_id):
            return False
        self.drive(user_id)
        return True

    def is_suspended(self, user_id: str) -> bool:
        current = self.store.state(user_id)
        return current is not None and current[0] in s.SUSPENDED_STATES

    def status_text(self, user_id: str) -> str:
        # Describes state only. No room names, counts of other people, or timestamps.
        current = self.store.state(user_id)
        if current is None:
            return "Safe Mode is off. Normal and protected rooms are accessible."
        state = current[0]
        if state == s.SUSPENDING:
            return "Safe Mode is being turned on. Server-side access is not yet confirmed as suspended."
        if state == s.SUSPENDED:
            return "Safe Mode is on. Access to protected rooms is suspended. Normal rooms remain accessible."
        if state == s.PARTIAL:
            return (
                "Safe Mode is PARTIAL. Some protected rooms could not be suspended yet. "
                "Send !safe on to retry, or contact an administrator."
            )
        return self._restore_text(user_id)

    # Reconciliation --------------------------------------------------------

    def reconcile(self) -> list[str]:
        """Finish operations interrupted by a restart or a failure. Returns the accounts."""
        touched: list[str] = []
        for user_id, _state in self.store.pending_accounts((s.SUSPENDING, s.PARTIAL, s.RESTORING)):
            self.drive(user_id)
            touched.append(user_id)
        return touched

    # Internals -------------------------------------------------------------

    def _user_lock(self, user_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(user_id, threading.Lock())

    def _start_activation(self, user_id: str) -> str:
        operation_id = _new_id()
        # Turning Safe Mode on always succeeds in recording, whatever the current state: it
        # takes over from an unfinished restore, and picks up rooms protected since last time.
        self.store.start_suspension(
            user_id, operation_id, [room.room_id for room in self.store.enabled_rooms()]
        )
        return operation_id

    def _start_restore(self, user_id: str, code: str | None) -> Submitted:
        current = self.store.state(user_id)
        if current is None:
            return Submitted(reply="Safe Mode is not on.")
        if current[0] == s.RESTORING:
            return Submitted(
                reply="Access is already being restored. Accept the invitations to continue."
            )
        if current[0] == s.SUSPENDING:
            return Submitted(
                reply="Safe Mode is still being turned on. Wait for its confirmation, then try again."
            )
        if not code:
            return Submitted(
                reply="Turning Safe Mode off needs your recovery code, which you keep separately. "
                "Send !safe off <recovery code>."
            )
        error, matched = self._check_code(user_id, code)
        if error:
            return Submitted(reply=error)
        operation_id = _new_id()
        try:
            # Spends the code and records RESTORING in one transaction, before any Matrix call.
            self.store.start_restore(user_id, operation_id, code_hash=matched)
        except StateConflict as exc:
            if exc.state == "CODE_CHANGED":
                return Submitted(reply="That recovery code is not correct.")
            return Submitted(reply=self.status_text(user_id))
        return Submitted(operation_id=operation_id, verb="off")

    def _drive_suspension(self, user_id: str, operation_id: str) -> bool:
        for membership in self.store.memberships(user_id):
            if membership.result in (s.DONE, s.SKIPPED):
                continue
            if not self.store.is_current(user_id, operation_id):
                return False
            try:
                current = self.gateway.membership(membership.room_id, user_id)
                if membership.previous == s.UNKNOWN:
                    # Record the membership before kicking, so a restore knows what to bring back.
                    self.store.resolve_previous(
                        user_id,
                        membership.room_id,
                        current if current in ("join", "invite") else s.NOT_MEMBER,
                    )
                if current in ("join", "invite"):
                    self.gateway.kick(membership.room_id, user_id)
                result = s.DONE
            except GatewayError:
                result = s.FAILED
            if not self.store.set_result(user_id, membership.room_id, result, operation_id):
                return False
        failed = any(m.result not in (s.DONE, s.SKIPPED) for m in self.store.memberships(user_id))
        return self.store.finish_operation(
            user_id, operation_id, s.PARTIAL if failed else s.SUSPENDED
        )

    def _drive_restore(self, user_id: str, operation_id: str) -> bool:
        for membership in self.store.memberships(user_id):
            if membership.result in (s.DONE, s.SKIPPED):
                continue
            if not self.store.is_current(user_id, operation_id):
                return False
            room = self.store.room(membership.room_id)
            if membership.previous != "join":
                # Never joined before suspension (invited only, or not a member): nothing to restore.
                result = s.DONE
            elif room is None or not room.enabled:
                result = s.SKIPPED
            else:
                try:
                    if self.gateway.membership(membership.room_id, user_id) != "join":
                        self.gateway.invite(membership.room_id, user_id)
                    result = s.DONE
                except GatewayError:
                    result = s.FAILED
            if not self.store.set_result(user_id, membership.room_id, result, operation_id):
                return False
        if all(m.result in (s.DONE, s.SKIPPED) for m in self.store.memberships(user_id)):
            # ACTIVE only once every restore has succeeded.
            return self.store.finish_operation(user_id, operation_id, None)
        return self.store.is_current(user_id, operation_id)

    def _check_code(self, user_id: str, code: str) -> tuple[str | None, bytes]:
        """(error, matched hash). The hash is used to spend exactly the code that was checked."""
        row = self.store.recovery_code(user_id)
        if row is None:
            return "No recovery code is set for this account. Ask an administrator.", b""
        if row["failed_attempts"] >= MAX_CODE_ATTEMPTS:
            return "Too many wrong codes. Ask an administrator to issue a new code.", b""
        stored = bytes(row["code_hash"])
        if hmac.compare_digest(stored, _hash_code(_normalise(code), bytes(row["salt"]))):
            return None, stored
        self.store.record_failed_code(user_id)
        return "That recovery code is not correct.", b""

    def _activation_report(self, user_id: str) -> ActivationReport:
        state = self.store.state(user_id)
        memberships = self.store.memberships(user_id)
        done = sum(1 for m in memberships if m.result == s.DONE and m.previous != s.NOT_MEMBER)
        failed = [m for m in memberships if m.result not in (s.DONE, s.SKIPPED)]
        report = ActivationReport(
            state=state[0] if state else s.ACTIVE,
            rooms_checked=len(self.store.enabled_rooms()),
            suspended=done,
            failed=len(failed),
            needs_new_code=self.store.recovery_code(user_id) is None,
        )
        report.failed_room_ids = [m.room_id for m in failed]
        return report

    def _restore_text(self, user_id: str) -> str:
        state = self.store.state(user_id)
        if state is None:
            # Verified on Synapse: with `joined` history, messages sent while an account was out
            # of a room stay hidden from it after it rejoins; earlier ones are visible again.
            return (
                "Safe Mode turned off. Accept the invitations to rejoin your rooms. "
                "Messages sent while Safe Mode was on stay hidden from you."
            )
        pending = sum(
            1 for m in self.store.memberships(user_id) if m.result not in (s.DONE, s.SKIPPED)
        )
        return (
            f"Access is being restored. {pending} room invitations are not yet sent. "
            "Accept the invitations in your client. Send !safe status to check."
        )


@dataclass(frozen=True)
class Submitted:
    """The result of ``submit``: a reply to send now, or an operation to drive and report."""

    reply: str | None = None
    operation_id: str | None = None
    verb: str = ""


@dataclass(frozen=True)
class Reply:
    message: str

    def text(self) -> str:
        return self.message


def _generate_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(16))
    return "-".join(raw[i : i + 4] for i in range(0, 16, 4))


def _normalise(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _hash_code(code: str, salt: bytes) -> bytes:
    return hashlib.scrypt(_normalise(code).encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def _new_id() -> str:
    return secrets.token_hex(8)


HELP = (
    "Safe Mode commands:\n"
    "!safe on: suspend your access to protected rooms.\n"
    "!safe status: show your Safe Mode state.\n"
    "!safe off <recovery code>: restore access, using the recovery code you keep separately.\n"
    "!safe help: show this message.\n\n"
    "Safe Mode does not erase messages already on your devices."
)
