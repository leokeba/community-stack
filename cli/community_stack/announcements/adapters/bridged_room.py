"""Announcements to a chat group on another network, through its bridged Matrix room.

The stack's announcer account posts in the Matrix room a bridge links to the group, and
the bridge relays the message through the linked WhatsApp or Signal account. Each linked
group the organizers choose is its own channel, so it can be turned on and off alone.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from community_stack.announcements.adapters.base import (
    ChannelCapabilities,
    HealthResult,
    UpdateMode,
    WithdrawMode,
)
from community_stack.announcements.adapters.matrix_room import MatrixRoomAdapter

# The announcements container reaches Synapse on the internal network.
INTERNAL_HOMESERVER = "http://community_synapse:8008"


def destination_name(bridge: str, room_id: str) -> str:
    """A stable channel name for one bridged room, safe in URLs and form values."""
    return f"{bridge}_{hashlib.sha256(room_id.encode()).hexdigest()[:10]}"


class BridgedRoomAdapter(MatrixRoomAdapter):
    """Post announcements as plain messages in one bridged room; edits and withdrawals follow up."""

    # Bridges ignore m.notice, so announcements must be ordinary messages to be relayed.
    msgtype = "m.text"

    def __init__(
        self,
        name: str,
        label: str,
        homeserver_url: str,
        room_id: str,
        access_token: str,
        max_text_length: int | None = None,
        transport: Callable[[str, str, dict, dict], tuple[int, dict]] | None = None,
    ) -> None:
        super().__init__(homeserver_url, room_id, access_token, transport)
        self._name = name
        self._label = label
        self._max_text_length = max_text_length

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label=self._label,
            max_text_length=self._max_text_length,
            update_mode=UpdateMode.FOLLOW_UP,
            withdraw_mode=WithdrawMode.NOTICE,
        )

    def health_check(self) -> HealthResult:
        result = self.verify_access(require_public=False)
        if result.ok:
            return HealthResult(
                ok=True,
                details="The announcer account can post in the bridged room. Relay and the "
                "linked account are checked on the bridge page.",
            )
        return result
