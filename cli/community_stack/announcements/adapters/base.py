"""Abstract base class and contracts for announcement channel adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

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
