"""Matrix Client-Server calls for Safe Mode, made as the appservice's bot user.

Only the endpoints Safe Mode needs are used: room state, joined members, membership
changes, join and leave, sending a notice, and appservice registration. The appservice
token is the bot's only credential. Homeserver admin APIs are never called.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from community_stack.safemode.manager import GatewayError, RoomFacts

DEFAULT_TIMEOUT = 15
INFINITE = float("inf")


class SynapseGateway:
    """Implements ``RoomGateway`` plus the bot's messaging calls."""

    def __init__(
        self, base_url: str, bot_user_id: str, as_token: str, timeout: float = DEFAULT_TIMEOUT
    ):
        self.base_url = base_url.rstrip("/")
        self.bot_user_id = bot_user_id
        self.as_token = as_token
        self.timeout = timeout

    # Low-level -------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        as_user: str | None = None,
        query: dict[str, str] | None = None,
    ) -> Any:
        params = dict(query or {})
        if as_user:
            params["user_id"] = as_user
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.as_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            errcode = _errcode(exc.read())
            raise GatewayError(f"Matrix returned {exc.code} {errcode}".strip()) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GatewayError("Matrix could not be reached") from exc
        return json.loads(raw) if raw else {}

    # RoomGateway -----------------------------------------------------------

    def room_facts(self, room_id: str) -> RoomFacts:
        room = urllib.parse.quote(room_id, safe="")
        events = self.request(
            "GET", f"/_matrix/client/v3/rooms/{room}/state", as_user=self.bot_user_id
        )
        by_type: dict[tuple[str, str], dict[str, Any]] = {}
        for event in events:
            by_type[(event.get("type", ""), event.get("state_key", ""))] = event

        create = by_type.get(("m.room.create", ""), {})
        create_content = create.get("content", {})
        room_version = str(create_content.get("room_version", "1"))
        creators = {create.get("sender", "")} - {""}
        if _at_least_12(room_version):
            creators |= set(create_content.get("additional_creators", []) or [])

        encryption = by_type.get(("m.room.encryption", ""), {}).get("content", {})
        history = by_type.get(("m.room.history_visibility", ""), {}).get("content", {})
        join = by_type.get(("m.room.join_rules", ""), {}).get("content", {})
        power = by_type.get(("m.room.power_levels", ""), {}).get("content", {})

        bot_power = self._power(power, creators)
        return RoomFacts(
            room_id=room_id,
            room_version=room_version,
            encrypted=bool(encryption.get("algorithm")),
            history_visibility=str(history.get("history_visibility", "shared")),
            join_rule=str(join.get("join_rule", "public")),
            creators=frozenset(creators),
            bot_can_invite=bot_power >= _level(power, "invite", 0),
            bot_can_kick=bot_power >= _level(power, "kick", 50),
        )

    def _power(self, power: dict[str, Any], creators: set[str]) -> float:
        if self.bot_user_id in creators:
            return INFINITE
        users = power.get("users", {}) or {}
        if self.bot_user_id in users:
            return float(users[self.bot_user_id])
        return float(power.get("users_default", 0))

    def membership(self, room_id: str, user_id: str) -> str | None:
        room = urllib.parse.quote(room_id, safe="")
        member = urllib.parse.quote(user_id, safe="")
        try:
            event = self.request(
                "GET",
                f"/_matrix/client/v3/rooms/{room}/state/m.room.member/{member}",
                as_user=self.bot_user_id,
            )
        except GatewayError as exc:
            if "M_NOT_FOUND" in str(exc):
                return None
            raise
        return event.get("membership")

    def kick(self, room_id: str, user_id: str) -> None:
        room = urllib.parse.quote(room_id, safe="")
        self.request(
            "POST",
            f"/_matrix/client/v3/rooms/{room}/kick",
            {"user_id": user_id, "reason": "Safe Mode"},
            as_user=self.bot_user_id,
        )

    def invite(self, room_id: str, user_id: str) -> None:
        room = urllib.parse.quote(room_id, safe="")
        self.request(
            "POST",
            f"/_matrix/client/v3/rooms/{room}/invite",
            {"user_id": user_id},
            as_user=self.bot_user_id,
        )

    def create_room(self, name: str, invite: list[str]) -> str:
        """Create a room the bot owns, already compatible with Safe Mode: end-to-end encrypted,
        invite-only, ``joined`` history and no guests. The bot is its creator, so in room
        version 12 nobody can remove it. Returns the room ID."""
        reply = self.request(
            "POST",
            "/_matrix/client/v3/createRoom",
            {
                "preset": "private_chat",
                "name": name,
                "invite": invite,
                "initial_state": [
                    {
                        "type": "m.room.encryption",
                        "state_key": "",
                        "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                    },
                    {
                        "type": "m.room.history_visibility",
                        "state_key": "",
                        "content": {"history_visibility": "joined"},
                    },
                    {
                        "type": "m.room.guest_access",
                        "state_key": "",
                        "content": {"guest_access": "forbidden"},
                    },
                ],
            },
            as_user=self.bot_user_id,
        )
        room_id = reply.get("room_id")
        if not isinstance(room_id, str) or not room_id:
            raise GatewayError("Matrix did not return a room ID")
        return room_id

    # Bot messaging ---------------------------------------------------------

    def joined_members(self, room_id: str) -> list[str]:
        room = urllib.parse.quote(room_id, safe="")
        reply = self.request(
            "GET", f"/_matrix/client/v3/rooms/{room}/joined_members", as_user=self.bot_user_id
        )
        return sorted((reply.get("joined") or {}).keys())

    def join(self, room_id: str) -> None:
        room = urllib.parse.quote(room_id, safe="")
        self.request("POST", f"/_matrix/client/v3/rooms/{room}/join", {}, as_user=self.bot_user_id)

    def leave(self, room_id: str) -> None:
        room = urllib.parse.quote(room_id, safe="")
        self.request("POST", f"/_matrix/client/v3/rooms/{room}/leave", {}, as_user=self.bot_user_id)

    def send_notice(self, room_id: str, text: str, txn_id: str) -> None:
        room = urllib.parse.quote(room_id, safe="")
        self.request(
            "PUT",
            f"/_matrix/client/v3/rooms/{room}/send/m.room.message/{urllib.parse.quote(txn_id, safe='')}",
            {"msgtype": "m.notice", "body": text},
            as_user=self.bot_user_id,
        )

    def register_bot(self) -> None:
        """Create the bot's account on the homeserver. Repeating this is harmless."""
        localpart = self.bot_user_id.split(":", 1)[0].lstrip("@")
        try:
            self.request(
                "POST",
                "/_matrix/client/v3/register",
                {"type": "m.login.application_service", "username": localpart},
            )
        except GatewayError as exc:
            if "M_USER_IN_USE" not in str(exc):
                raise


def _at_least_12(room_version: str) -> bool:
    try:
        return int(room_version) >= 12
    except ValueError:
        return False


def _level(power: dict[str, Any], key: str, default: int) -> float:
    value = power.get(key, default)
    return float(value) if isinstance(value, (int, float)) else float(default)


def _errcode(raw: bytes) -> str:
    try:
        return str(json.loads(raw).get("errcode", ""))
    except (ValueError, AttributeError):
        return ""
