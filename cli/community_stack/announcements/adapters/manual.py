"""Adapter for content prepared for a person to publish on a social platform."""

from __future__ import annotations

from community_stack.announcements.adapters.base import (
    ChannelAdapter,
    ChannelCapabilities,
    DeliveryResult,
    HealthResult,
    UpdateMode,
    WithdrawMode,
)
from community_stack.announcements.models import Announcement
from community_stack.announcements.platforms import PlatformSpec


class ManualChannelAdapter(ChannelAdapter):
    """Record a prepared publication without contacting the external platform."""

    def __init__(self, platform: PlatformSpec) -> None:
        self.platform = platform

    @property
    def name(self) -> str:
        return self.platform.identifier

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label=self.platform.name,
            max_text_length=(
                self.platform.capabilities.max_text_length
                if self.platform.character_limit_verified
                else None
            ),
            supports_media=bool(
                self.platform.supported_media and "image" in self.platform.supported_media
            ),
            custom_text=True,
            update_mode=UpdateMode.MANUAL,
            withdraw_mode=WithdrawMode.MANUAL,
        )

    def render_text(self, announcement: Announcement) -> str:
        return announcement.channel_text.get(self.name) or super().render_text(announcement)

    def publish(self, announcement: Announcement) -> DeliveryResult:
        check = self.check(announcement)
        if check.over_limit:
            return DeliveryResult(
                success=False,
                error=(
                    f"{self.platform.name} copy is {check.text_length} characters; "
                    f"the verified limit is {check.capabilities.max_text_length}."
                ),
            )
        return DeliveryResult(success=True, prepared=True)

    def update(self, announcement: Announcement) -> DeliveryResult:
        return self.publish(announcement)

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        return DeliveryResult(success=True, prepared=True)

    def health_check(self) -> HealthResult:
        return HealthResult(
            ok=True, details="Manual posting is available; no external connection is checked."
        )
