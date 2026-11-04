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
from community_stack.safemode.store import ProtectedRoom, Store

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

    def text(self) -> str:
        if self.state == s.ACTIVE:
            return "Safe Mode is off."
        if self.failed == 0:
            return (
                "Safe Mode activated.\n\n"
                f"{self.rooms_checked} protected rooms checked.\n"
                f"{self.suspended} memberships suspended.\n\n"
                "Normal community rooms remain accessible.\n\n"
                "Server-side access has been suspended. Local message history previously "
                "downloaded to your devices may still be accessible."
            )
        return (
            "Safe Mode partially activated.\n\n"
            f"{self.suspended} rooms successfully suspended.\n"
            f"{self.failed} rooms could not be suspended.\n\n"
            "Your Safe Mode status is PARTIAL.\n\n"
            "Use !safe status for details."
        )


class AccessManager:
    def __init__(self, store: Store, gateway: RoomGateway, bot_user_id: str) -> None:
        self.store = store
        self.gateway = gateway
        self.bot_user_id = bot_user_id
        self._lock = threading.RLock()

    # Administration --------------------------------------------------------

    def protect_room(self, room_id: str, name: str) -> list[str]:
        """Mark a room protected if it is compatible. Returns the problems, empty on success."""
        facts = self.gateway.room_facts(room_id)
        problems = compatibility_problems(facts, self.bot_user_id)
        if problems:
            return problems
        with self._lock:
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
        with self._lock:
            return self.store.set_room_enabled(room_id, False)

    def enroll(self, user_id: str) -> None:
        with self._lock:
            self.store.enroll(user_id)

    def issue_recovery_code(self, user_id: str) -> str:
        """Create a new one-time code. Shown once to the administrator, then stored only as a hash."""
        if not self.store.is_enrolled(user_id):
            raise SafeModeError("This account is not enrolled in Safe Mode.")
        code = _generate_code()
        salt = secrets.token_bytes(16)
        with self._lock:
            self.store.set_recovery_code(user_id, salt, _hash_code(code, salt))
        return code

    # Member commands -------------------------------------------------------

    def handle(self, sender: str, command: Command) -> str:
        """Run one command for an authenticated sender and return the reply text."""
        if not self.store.is_enrolled(sender):
            return "Safe Mode is not available for this account. Ask an administrator."
        if command.verb == "help":
            return HELP
        if command.verb == "status":
            return self.status_text(sender)
        try:
            if command.verb == "on":
                return self.activate(sender).text()
            if command.verb == "off":
                return self.restore(sender, command.code).text()
        except SafeModeError as exc:
            return str(exc)
        raise SafeModeError("Unknown command.")

    def activate(self, user_id: str) -> ActivationReport:
        with self._lock:
            current = self.store.state(user_id)
            if current is not None and current[0] == s.RESTORING:
                raise SafeModeError("Access is being restored. Try again once that finishes.")
            if current is None:
                rooms = self.store.enabled_rooms()
                snapshot = self._snapshot(user_id, rooms)
                self.store.begin_suspension(user_id, _new_id(), snapshot)
            # SUSPENDING, SUSPENDED and PARTIAL: finish what is outstanding; never re-snapshot.
            self._drive_suspension(user_id)
            return self._activation_report(user_id)

    def restore(self, user_id: str, code: str | None) -> Reply:
        with self._lock:
            current = self.store.state(user_id)
            if current is None:
                return Reply("Safe Mode is not on.")
            if current[0] == s.RESTORING:
                return Reply(
                    "Access is already being restored. Accept the invitations to continue."
                )
            if current[0] == s.SUSPENDING:
                return Reply(
                    "Safe Mode is still being turned on. Send !safe on to finish it first."
                )
            if not code:
                return Reply(
                    "Turning Safe Mode off needs your recovery code, which you keep separately. "
                    "Send !safe off <recovery code>."
                )
            error = self._check_code(user_id, code)
            if error:
                return Reply(error)
            # The code is one-time: consume it before any Matrix call, so a crash cannot reuse it.
            self.store.consume_recovery_code(user_id)
            self.store.set_state(user_id, s.RESTORING, _new_id())
            self.store.reset_results(user_id, s.PENDING)
            self._drive_restore(user_id)
            return Reply(self._restore_text(user_id))

    def admin_restore(self, user_id: str, confirmed: bool) -> str:
        """Administrative restore without the member's code. Explicitly confirmed, and only
        for an account that is actually suspended. Member codes are never bypassed silently."""
        if not confirmed:
            raise SafeModeError("Administrative restore needs --confirm.")
        with self._lock:
            current = self.store.state(user_id)
            if current is None:
                return "Safe Mode is not on for this account."
            if current[0] in (s.SUSPENDING, s.RESTORING):
                return "An operation is in progress for this account. Run access doctor."
            self.store.consume_recovery_code(user_id)  # an old member code must not survive
            self.store.set_state(user_id, s.RESTORING, _new_id())
            self.store.reset_results(user_id, s.PENDING)
            self._drive_restore(user_id)
            return self._restore_text(user_id)

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
        """Finish operations interrupted by a restart. Returns the accounts it acted on."""
        touched: list[str] = []
        with self._lock:
            for user_id, state in self.store.pending_accounts(
                (s.SUSPENDING, s.PARTIAL, s.RESTORING)
            ):
                if state == s.RESTORING:
                    self._drive_restore(user_id)
                else:
                    self._drive_suspension(user_id)
                touched.append(user_id)
        return touched

    # Internals -------------------------------------------------------------

    def _snapshot(self, user_id: str, rooms: list[ProtectedRoom]) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for room in rooms:
            membership = self._membership_or_none(room.room_id, user_id)
            if membership in ("join", "invite"):
                snapshot[room.room_id] = membership
        return snapshot

    def _membership_or_none(self, room_id: str, user_id: str) -> str | None:
        try:
            return self.gateway.membership(room_id, user_id)
        except GatewayError:
            return "unknown"

    def _drive_suspension(self, user_id: str) -> None:
        for membership in self.store.memberships(user_id):
            if membership.result == s.DONE:
                continue
            try:
                current = self.gateway.membership(membership.room_id, user_id)
                if current in ("join", "invite"):
                    self.gateway.kick(membership.room_id, user_id)
                self.store.set_result(user_id, membership.room_id, s.DONE)
            except GatewayError:
                self.store.set_result(user_id, membership.room_id, s.FAILED)
        failed = any(m.result != s.DONE for m in self.store.memberships(user_id))
        self.store.set_state(
            user_id, s.PARTIAL if failed else s.SUSPENDED, _operation(self, user_id)
        )

    def _drive_restore(self, user_id: str) -> None:
        for membership in self.store.memberships(user_id):
            if membership.result in (s.DONE, s.SKIPPED):
                continue
            if membership.previous != "join":
                # Invitations were never joined; they are not restored, so nothing to do.
                self.store.set_result(user_id, membership.room_id, s.DONE)
                continue
            room = self.store.room(membership.room_id)
            if room is None or not room.enabled:
                self.store.set_result(user_id, membership.room_id, s.SKIPPED)
                continue
            try:
                if self.gateway.membership(membership.room_id, user_id) != "join":
                    self.gateway.invite(membership.room_id, user_id)
                self.store.set_result(user_id, membership.room_id, s.DONE)
            except GatewayError:
                self.store.set_result(user_id, membership.room_id, s.FAILED)
        if all(m.result in (s.DONE, s.SKIPPED) for m in self.store.memberships(user_id)):
            self.store.clear_memberships(user_id)
            self.store.set_state(user_id, None)  # ACTIVE only once every restore has succeeded

    def _check_code(self, user_id: str, code: str) -> str | None:
        row = self.store.recovery_code(user_id)
        if row is None:
            return "No recovery code is set for this account. Ask an administrator."
        if row["failed_attempts"] >= MAX_CODE_ATTEMPTS:
            return "Too many wrong codes. Ask an administrator to issue a new code."
        if hmac.compare_digest(
            bytes(row["code_hash"]), _hash_code(_normalise(code), bytes(row["salt"]))
        ):
            return None
        self.store.record_failed_code(user_id)
        return "That recovery code is not correct."

    def _activation_report(self, user_id: str) -> ActivationReport:
        state = self.store.state(user_id)
        memberships = self.store.memberships(user_id)
        done = sum(1 for m in memberships if m.result == s.DONE)
        failed = [m for m in memberships if m.result != s.DONE]
        report = ActivationReport(
            state=state[0] if state else s.ACTIVE,
            rooms_checked=len(self.store.enabled_rooms()),
            suspended=done,
            failed=len(failed),
        )
        report.failed_room_ids = [m.room_id for m in failed]
        return report

    def _restore_text(self, user_id: str) -> str:
        state = self.store.state(user_id)
        if state is None:
            return (
                "Safe Mode turned off. Historical messages from before your suspension "
                "may not be available."
            )
        pending = sum(1 for m in self.store.memberships(user_id) if m.result != s.DONE)
        return (
            f"Access is being restored. {pending} room invitations are not yet sent. "
            "Accept the invitations in your client. Send !safe status to check."
        )


@dataclass(frozen=True)
class Reply:
    message: str

    def text(self) -> str:
        return self.message


def _operation(manager: AccessManager, user_id: str) -> str:
    current = manager.store.state(user_id)
    return current[1] if current else ""


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
