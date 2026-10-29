"""Canonical announcement domain model and validation."""

from __future__ import annotations

import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class AnnouncementStatus(str, Enum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    UPDATED = "updated"
    WITHDRAWN = "withdrawn"


# Not yet public: editable, and withdrawn without telling any channel.
UNPUBLISHED = (
    AnnouncementStatus.DRAFT,
    AnnouncementStatus.IN_REVIEW,
    AnnouncementStatus.APPROVED,
    AnnouncementStatus.SCHEDULED,
)


class ModelValidationError(Exception):
    """Raised when an announcement fails validation constraints."""


def generate_announcement_id() -> str:
    """Generate a stable, timestamp-prefixed unique announcement identifier."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    rand = secrets.token_hex(4)
    return f"ann_{ts}_{rand}"


@dataclass
class Announcement:
    title: str
    body: str
    id: str = field(default_factory=generate_announcement_id)
    published_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    language: str = "en"
    status: AnnouncementStatus = AnnouncementStatus.DRAFT
    canonical_url: str = ""
    expires_at: str | None = None
    audience: str = "public"
    author: str | None = None
    tags: list[str] = field(default_factory=list)
    scheduled_at: str | None = None
    # Optional text to send instead of title + body, keyed by channel name.
    channel_text: dict[str, str] = field(default_factory=dict)
    image_id: str | None = None
    # Filled in by storage from the media table; not part of the announcement record.
    image: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.status, str) and not isinstance(self.status, AnnouncementStatus):
            self.status = AnnouncementStatus(self.status)
        self.validate()

    def validate(self) -> None:
        """Validate announcement fields against architectural constraints."""
        if not self.title or not self.title.strip():
            raise ModelValidationError("Announcement title cannot be empty.")
        if len(self.title) > 200:
            raise ModelValidationError(
                f"Announcement title exceeds maximum length of 200 characters (got {len(self.title)})."
            )

        if not self.body or not self.body.strip():
            raise ModelValidationError("Announcement body cannot be empty.")
        if len(self.body) > 50_000:
            raise ModelValidationError(
                f"Announcement body exceeds maximum length of 50,000 characters (got {len(self.body)})."
            )

        if not re.match(r"^[a-zA-Z0-9_-]{4,64}$", self.id):
            raise ModelValidationError(f"Invalid announcement ID format: '{self.id}'.")

        if not re.match(r"^[a-z]{2,3}(-[A-Z]{2})?$", self.language):
            raise ModelValidationError(
                f"Language code '{self.language}' must follow BCP 47 format (e.g. 'en', 'es', 'pt-BR')."
            )

        try:
            datetime.fromisoformat(self.published_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ModelValidationError(
                "Announcement publication time must be an ISO 8601 timestamp."
            ) from exc

        try:
            datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ModelValidationError(
                "Announcement update time must be an ISO 8601 timestamp."
            ) from exc

        for channel, text in self.channel_text.items():
            if not re.match(r"^[a-z0-9_]{1,40}$", channel):
                raise ModelValidationError("Invalid channel name for custom text.")
            if len(text) > 5_000:
                raise ModelValidationError("Custom channel text is limited to 5,000 characters.")

        if self.scheduled_at:
            try:
                datetime.fromisoformat(self.scheduled_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ModelValidationError(
                    "Announcement schedule time must be an ISO 8601 timestamp."
                ) from exc

        if self.canonical_url and not self.canonical_url.startswith(("http://", "https://")):
            raise ModelValidationError(
                f"Canonical URL must start with http:// or https://: '{self.canonical_url}'"
            )
        if self.audience != "public":
            raise ModelValidationError("Announcements in this publishing workflow must be public.")

    def to_dict(self) -> dict[str, Any]:
        """Convert announcement to standard dictionary representation."""
        data = asdict(self)
        data.pop("image", None)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Announcement:
        """Instantiate announcement from dictionary."""
        copy_data = dict(data)
        if "status" in copy_data:
            copy_data["status"] = AnnouncementStatus(copy_data["status"])
        return cls(**copy_data)
