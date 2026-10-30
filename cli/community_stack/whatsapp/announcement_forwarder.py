"""Forward announcements into a Matrix portal room linked to WhatsApp."""

from __future__ import annotations

from collections.abc import Callable

from community_stack.announcements.adapters.base import (
    ChannelCapabilities,
    HealthResult,
    UpdateMode,
    WithdrawMode,
)
from community_stack.announcements.adapters.matrix_room import MatrixRoomAdapter

WHATSAPP_MAX_TEXT_LENGTH = 65_536


class WhatsAppAnnouncementAdapter(MatrixRoomAdapter):
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
        super().__init__(homeserver_url, room_id, access_token, matrix_transport)

    @property
    def name(self) -> str:
        return "whatsapp"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label="WhatsApp announcements",
            max_text_length=WHATSAPP_MAX_TEXT_LENGTH,
            update_mode=UpdateMode.FOLLOW_UP,
            withdraw_mode=WithdrawMode.NOTICE,
        )

    def health_check(self) -> HealthResult:
        result = self.verify_access(require_public=False)
        if result.ok:
            return HealthResult(
                ok=True,
                details="Matrix portal room is reachable. This does not verify WhatsApp account linkage.",
            )
        return result
