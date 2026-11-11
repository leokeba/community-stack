"""Forward announcements into a Matrix portal room linked to WhatsApp.

Kept for code and tests that build the single legacy "whatsapp" channel directly; the
console now connects any number of bridged groups through BridgedRoomAdapter.
"""

from __future__ import annotations

from collections.abc import Callable

from community_stack.announcements.adapters.bridged_room import BridgedRoomAdapter

WHATSAPP_MAX_TEXT_LENGTH = 65_536


class WhatsAppAnnouncementAdapter(BridgedRoomAdapter):
    """Post only canonical announcements to a Matrix room bridged to a WhatsApp group."""

    def __init__(
        self,
        homeserver_url: str,
        room_id: str,
        access_token: str,
        transport: Callable[[str, str, dict], tuple[int, dict]] | None = None,
    ) -> None:
        matrix_transport = None
        if transport is not None:
            matrix_transport = lambda method, path, _headers, payload: transport(
                method, path, payload
            )
        super().__init__(
            "whatsapp",
            "WhatsApp announcements",
            homeserver_url,
            room_id,
            access_token,
            WHATSAPP_MAX_TEXT_LENGTH,
            matrix_transport,
        )
