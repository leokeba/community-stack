"""Abstract base class and contracts for announcement channel adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from community_stack.announcements.models import Announcement


@dataclass
class DeliveryResult:
    """Outcome of a publication, update, or withdrawal attempt on a channel."""

    success: bool
    external_id: str | None = None
    error: str | None = None
    retryable: bool = False
    idempotent: bool = False


@dataclass
class HealthResult:
    """Health check outcome for a channel adapter."""

    ok: bool
    details: str


class UpdateMode(str, Enum):
    """How a channel reflects an edit to a published announcement."""

    IN_PLACE = "in_place"
    FOLLOW_UP = "follow_up"
    NONE = "none"


class WithdrawMode(str, Enum):
    """How a channel reflects a withdrawn announcement."""

    DELETE = "delete"
    NOTICE = "notice"
    NONE = "none"


UPDATE_DESCRIPTIONS = {
    UpdateMode.IN_PLACE: "Edits change the published post.",
    UpdateMode.FOLLOW_UP: "Edits are sent as a new message; the original stays.",
    UpdateMode.NONE: "Edits are not sent to this channel.",
}
WITHDRAW_DESCRIPTIONS = {
    WithdrawMode.DELETE: "Withdrawal removes the post.",
    WithdrawMode.NOTICE: "Withdrawal sends a retraction notice; the original stays.",
    WithdrawMode.NONE: "Withdrawal cannot be sent to this channel.",
}


@dataclass(frozen=True)
class ChannelCapabilities:
    """What a channel can do, so editors can be warned before publishing."""

    label: str
    max_text_length: int | None = None
    supports_media: bool = False
    shortens_long_text: bool = False
    update_mode: UpdateMode = UpdateMode.NONE
    withdraw_mode: WithdrawMode = WithdrawMode.NONE

    @property
    def update_description(self) -> str:
        return UPDATE_DESCRIPTIONS[self.update_mode]

    @property
    def withdraw_description(self) -> str:
        return WITHDRAW_DESCRIPTIONS[self.withdraw_mode]


@dataclass(frozen=True)
class ChannelCheck:
    """Pre-publication check of one announcement against one channel."""

    name: str
    capabilities: ChannelCapabilities
    text_length: int

    @property
    def over_limit(self) -> bool:
        limit = self.capabilities.max_text_length
        return limit is not None and self.text_length > limit


class ChannelAdapter(ABC):
    """Abstract interface for all outbound announcement adapters."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique adapter name identifier (e.g. 'web_feed', 'matrix_room', 'rss')."""

    @abstractmethod
    def publish(self, announcement: Announcement) -> DeliveryResult:
        """Publish an announcement to the target channel."""

    @abstractmethod
    def update(self, announcement: Announcement) -> DeliveryResult:
        """Update an existing announcement on the target channel."""

    @abstractmethod
    def withdraw(self, announcement_id: str) -> DeliveryResult:
        """Retract or withdraw an announcement on the target channel."""

    @abstractmethod
    def health_check(self) -> HealthResult:
        """Verify channel connectivity and credentials."""

    def update_post(self, announcement: Announcement, external_id: str | None) -> DeliveryResult:
        """Update using the channel's ID for the earlier post, when the channel needs it."""
        return self.update(announcement)

    def withdraw_post(self, announcement_id: str, external_id: str | None) -> DeliveryResult:
        """Withdraw using the channel's ID for the earlier post, when the channel needs it."""
        return self.withdraw(announcement_id)

    @property
    def capabilities(self) -> ChannelCapabilities:
        """Declared channel behaviour; override with what the channel really does."""
        return ChannelCapabilities(label=self.name)

    def render_text(self, announcement: Announcement) -> str:
        """Plain text this channel will show, used for length checks and previews."""
        return f"{announcement.title}\n\n{announcement.body}"

    def check(self, announcement: Announcement) -> ChannelCheck:
        """Check an announcement against this channel's limits."""
        return ChannelCheck(
            name=self.name,
            capabilities=self.capabilities,
            text_length=len(self.render_text(announcement)),
        )
