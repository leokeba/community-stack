"""Announcement publishing orchestrator and adapter coordinator."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from community_stack.announcements.adapters.base import (
    ChannelAdapter,
    ChannelCheck,
    DeliveryResult,
    UpdateMode,
    WithdrawMode,
)
from community_stack.announcements.adapters.matrix_room import MatrixRoomAdapter
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.connectors import CONNECTORS
from community_stack.announcements.models import UNPUBLISHED, Announcement, AnnouncementStatus
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.config import load_config
from community_stack.secrets import parse_env_file

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 5
JOB_LEASE = timedelta(minutes=5)
RETRY_BASE_DELAY = timedelta(seconds=30)
RETRY_MAX_DELAY = timedelta(hours=1)


def retry_delay(attempts: int) -> timedelta:
    """Exponential backoff after the given number of attempts: 30s, 2m, 8m, 32m, 1h."""
    return min(RETRY_BASE_DELAY * 4 ** max(attempts - 1, 0), RETRY_MAX_DELAY)


def community_timezone(config_path: Path | str) -> ZoneInfo:
    """The IANA time zone schedules are written in; UTC when unset or unknown."""
    path = Path(config_path)
    name = "UTC"
    if path.is_file():
        name = load_config(path).get("project", {}).get("timezone") or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


class AnnouncementPublisher:
    """Orchestrates publishing across canonical storage and channel adapters."""

    def __init__(
        self,
        storage: AnnouncementStorage | None = None,
        adapters: list[ChannelAdapter] | None = None,
        config_path: Path | str = "community-stack.yaml",
        feed_dir: Path | str = "data/landing/feed",
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self.config_path = Path(config_path)
        self.max_attempts = max_attempts
        self.feed_dir = Path(feed_dir)
        self.storage = storage or AnnouncementStorage()

        if adapters is not None:
            self.adapters = adapters
        else:
            self.adapters = self._load_default_adapters()

    @property
    def timezone(self) -> ZoneInfo:
        """Read on each use, so a time zone changed from the console applies at once."""
        return community_timezone(self.config_path)

    def _load_default_adapters(self) -> list[ChannelAdapter]:
        adapters: list[ChannelAdapter] = []
        base_url = "https://community.example.org"
        community_name = "Community Communications"
        channels: dict[str, Any] = {}
        if self.config_path.is_file():
            cfg = load_config(self.config_path)
            domain = cfg.get("domain", {}).get("web", "community.example.org")
            base_url = f"https://{domain}"
            community_name = cfg.get("project", {}).get("name", "Community")
            announcement_config = cfg["services"]["announcements"]
            if not announcement_config["enabled"]:
                return adapters
            channels = announcement_config.get("channels", {})

        web_config = channels.get("web_feed", {})
        if (
            not web_config.get("enabled", True)
            and "web_feed" not in self.storage.get_channel_settings()
        ):
            self.storage.set_channel_enabled("web_feed", False)
        matrix_config = channels.get("matrix_room", {})
        channel_links = {}
        if matrix_config.get("enabled", False):
            room_id = quote(matrix_config["room_id"], safe="!:")
            channel_links["Matrix announcements"] = f"https://matrix.to/#/{room_id}"
        adapters.append(
            WebFeedAdapter(
                output_dir=self.feed_dir,
                storage=self.storage,
                base_url=base_url,
                community_name=community_name,
                channel_links=channel_links,
            )
        )

        if matrix_config.get("enabled", False):
            token_env = matrix_config["access_token_env"]
            env_path = self.config_path.parent / ".env"
            env_values = parse_env_file(env_path)
            access_token = os.environ.get(token_env) or env_values.get(token_env)
            if not access_token:
                raise ValueError(
                    "Matrix announcements are enabled, but the access token "
                    f"environment variable {token_env} is not set."
                )
            adapters.append(
                MatrixRoomAdapter(
                    homeserver_url=matrix_config["homeserver_url"],
                    room_id=matrix_config["room_id"],
                    access_token=access_token,
                )
            )
        return adapters + self._connected_adapters()

    def _connected_adapters(self) -> list[ChannelAdapter]:
        """Adapters for channels organizers connected in the console."""
        adapters = []
        for kind, values in self.storage.list_connections().items():
            connector = CONNECTORS.get(kind)
            if connector is None:
                continue
            try:
                adapters.append(connector.build(values))
            except (KeyError, ValueError):
                logger.warning("Skipping the %s connection: its saved settings are invalid.", kind)
        return adapters

    def reload_connections(self) -> None:
        """Pick up channels connected or disconnected since the publisher started."""
        self.adapters = [a for a in self.adapters if a.name not in CONNECTORS]
        self.adapters.extend(self._connected_adapters())

    def create_draft(
        self,
        title: str,
        body: str,
        author: str | None = None,
        tags: list[str] | None = None,
        channel_text: dict[str, str] | None = None,
    ) -> Announcement:
        """Create and persist a draft without dispatching it to any channel."""
        ann = Announcement(
            title=title,
            body=body,
            author=author,
            tags=tags or [],
            status=AnnouncementStatus.DRAFT,
            channel_text=channel_text or {},
        )
        self.storage.save_announcement(ann)
        return ann

    def preview(self, announcement_id: str) -> Announcement:
        """Return a persisted announcement for preview without publishing it."""
        ann = self.storage.get_announcement(announcement_id)
        if ann is None:
            raise KeyError(f"Announcement '{announcement_id}' not found.")
        return ann

    def channel_checks(self, announcement: Announcement) -> list[ChannelCheck]:
        """Check an announcement against every channel it would be sent to."""
        channel_settings = self.storage.get_channel_settings()
        return [
            adapter.check(announcement)
            for adapter in self.adapters
            if channel_settings.get(adapter.name, True)
        ]

    def schedule(self, announcement_id: str, when: datetime) -> Announcement:
        """Schedule a draft for automatic publication at ``when`` (timezone-aware)."""
        ann = self.preview(announcement_id)
        if ann.status not in (
            AnnouncementStatus.DRAFT,
            AnnouncementStatus.APPROVED,
            AnnouncementStatus.SCHEDULED,
        ):
            raise ValueError("Only drafts can be scheduled.")
        if when.tzinfo is None:
            raise ValueError("Schedule times must include a time zone.")
        if when <= datetime.now(timezone.utc):
            raise ValueError("Choose a publication time in the future.")
        ann.status = AnnouncementStatus.SCHEDULED
        ann.scheduled_at = when.astimezone(timezone.utc).isoformat()
        self.storage.save_announcement(ann)
        return ann

    def unschedule(self, announcement_id: str) -> Announcement:
        """Turn a scheduled announcement back into a draft."""
        ann = self.preview(announcement_id)
        if not self.storage.transition_status(announcement_id, ("scheduled",), "draft"):
            raise ValueError("This announcement is not scheduled.")
        ann.status = AnnouncementStatus.DRAFT
        ann.scheduled_at = None
        self.storage.save_announcement(ann)
        return ann

    def set_review_status(
        self,
        announcement_id: str,
        from_statuses: tuple[AnnouncementStatus, ...],
        to: AnnouncementStatus,
    ) -> Announcement:
        """Move an unpublished announcement along the review workflow, atomically."""
        if not self.storage.transition_status(
            announcement_id, tuple(s.value for s in from_statuses), to.value
        ):
            raise ValueError("The announcement changed meanwhile; reload the page.")
        ann = self.preview(announcement_id)
        if to == AnnouncementStatus.DRAFT and ann.scheduled_at:
            ann.scheduled_at = None
            self.storage.save_announcement(ann)
        return ann

    def publish_due_scheduled(self) -> list[str]:
        """Publish every scheduled announcement whose time has come."""
        published = []
        for announcement_id in self.storage.list_due_scheduled(datetime.now(timezone.utc)):
            try:
                self.publish_draft(announcement_id)
            except (KeyError, ValueError):
                continue
            published.append(announcement_id)
        return published

    def publish_draft(self, announcement_id: str) -> tuple[Announcement, dict[str, DeliveryResult]]:
        """Publish a saved or scheduled draft and dispatch it to each channel."""
        ann = self.preview(announcement_id)
        if ann.status not in (
            AnnouncementStatus.DRAFT,
            AnnouncementStatus.APPROVED,
            AnnouncementStatus.SCHEDULED,
        ):
            raise ValueError(f"Announcement '{announcement_id}' is not a draft.")
        # Claim the publication atomically so two workers cannot both publish it.
        if not self.storage.transition_status(announcement_id, (ann.status.value,), "published"):
            raise ValueError(f"Announcement '{announcement_id}' was already published.")
        ann.status = AnnouncementStatus.PUBLISHED
        ann.scheduled_at = None
        ann.published_at = datetime.now(timezone.utc).isoformat()
        if not ann.canonical_url:
            web_adapter = next(
                (adapter for adapter in self.adapters if isinstance(adapter, WebFeedAdapter)),
                None,
            )
            if web_adapter:
                ann.canonical_url = web_adapter.canonical_url_for(ann)
        self.storage.save_announcement(ann)
        return ann, self._dispatch(ann, "publish")

    def _dispatch(self, ann: Announcement, operation: str) -> dict[str, DeliveryResult]:
        """Queue one job per channel and run it now; retryable failures stay queued."""
        results: dict[str, DeliveryResult] = {}
        channel_settings = self.storage.get_channel_settings()
        for adapter in self.adapters:
            if not channel_settings.get(adapter.name, True) and operation != "withdraw":
                continue
            capabilities = adapter.capabilities
            if (operation == "update" and capabilities.update_mode == UpdateMode.NONE) or (
                operation == "withdraw" and capabilities.withdraw_mode == WithdrawMode.NONE
            ):
                continue
            job = self.storage.enqueue_delivery_job(
                ann.id, adapter.name, operation, self.max_attempts, claim_for=JOB_LEASE
            )
            results[adapter.name] = self._run_job(job)
        return results

    def process_due_jobs(self, limit: int = 20) -> int:
        """Publish due scheduled announcements and run due jobs; return how many ran."""
        published = self.publish_due_scheduled()
        jobs = self.storage.claim_due_jobs(limit, JOB_LEASE)
        for job in jobs:
            self._run_job(job)
        return len(published) + len(jobs)

    def _run_job(self, job: dict[str, Any]) -> DeliveryResult:
        """Run one leased job, record the attempt, and close or requeue the job."""
        job_id = job["job_id"]
        operation = job["operation"]
        adapter = next((a for a in self.adapters if a.name == job["adapter_name"]), None)
        ann = self.storage.get_announcement(job["announcement_id"])
        if adapter is None or ann is None:
            error = "Channel is no longer configured." if ann else "Announcement no longer exists."
            self.storage.finish_job(job_id, "failed", error)
            return DeliveryResult(success=False, error=error)
        if operation != "withdraw":
            reason = None
            if ann.status == AnnouncementStatus.WITHDRAWN:
                reason = "Announcement was withdrawn before delivery."
            elif not self.storage.get_channel_settings().get(adapter.name, True):
                reason = "Channel was disabled before delivery."
            if reason:
                self.storage.finish_job(job_id, "cancelled", reason)
                return DeliveryResult(success=False, error=reason)

        try:
            external_id = self.storage.get_external_id(ann.id, adapter.name)
            if operation == "publish":
                res = adapter.publish(ann)
            elif operation == "update":
                res = adapter.update_post(ann, external_id)
            else:
                res = adapter.withdraw_post(ann.id, external_id)
        except Exception as exc:
            res = DeliveryResult(success=False, error=str(exc), retryable=False)

        self.storage.record_delivery(
            announcement_id=ann.id,
            adapter_name=adapter.name,
            status=(
                "withdrawn"
                if operation == "withdraw" and res.success
                else "delivered"
                if res.success
                else "failed"
            ),
            error=res.error,
            external_id=res.external_id,
            operation=operation,
            retryable=res.retryable,
            idempotent=res.idempotent,
        )
        if res.success:
            self.storage.finish_job(job_id, "succeeded")
        elif res.retryable and res.idempotent and job["attempts"] < job["max_attempts"]:
            retry_at = datetime.now(timezone.utc) + retry_delay(job["attempts"])
            self.storage.finish_job(job_id, "queued", res.error, retry_at=retry_at)
        else:
            self.storage.finish_job(job_id, "failed", res.error)
        return res

    def publish(
        self,
        title: str,
        body: str,
        author: str | None = None,
        tags: list[str] | None = None,
    ) -> tuple[Announcement, dict[str, DeliveryResult]]:
        """Create and immediately publish an announcement for CLI compatibility."""
        ann = self.create_draft(title, body, author, tags)
        return self.publish_draft(ann.id)

    def update(
        self,
        announcement_id: str,
        title: str | None = None,
        body: str | None = None,
        author: str | None = None,
        tags: list[str] | None = None,
        channel_text: dict[str, str] | None = None,
    ) -> tuple[Announcement, dict[str, DeliveryResult]]:
        """Update an existing announcement across all channels."""
        ann = self.preview(announcement_id)
        if ann.status == AnnouncementStatus.WITHDRAWN:
            raise ValueError(f"Announcement '{announcement_id}' has been withdrawn.")

        if title is not None:
            ann.title = title
        if body is not None:
            ann.body = body
        if author is not None:
            ann.author = author
        if tags is not None:
            ann.tags = tags
        if channel_text is not None:
            ann.channel_text = channel_text

        if ann.status in UNPUBLISHED:
            ann.validate()
            self.storage.save_announcement(ann)
            return ann, {}

        ann.status = AnnouncementStatus.UPDATED
        ann.validate()

        self.storage.save_announcement(ann)
        return ann, self._dispatch(ann, "update")

    def withdraw(self, announcement_id: str) -> tuple[Announcement, dict[str, DeliveryResult]]:
        """Retract an announcement across all channels."""
        ann = self.preview(announcement_id)
        if ann.status == AnnouncementStatus.WITHDRAWN:
            raise ValueError(f"Announcement '{announcement_id}' has already been withdrawn.")

        was_draft = ann.status in UNPUBLISHED
        ann.status = AnnouncementStatus.WITHDRAWN
        ann.scheduled_at = None
        self.storage.save_announcement(ann)
        return (ann, {}) if was_draft else (ann, self._dispatch(ann, "withdraw"))

    def retry_failed(self, announcement_id: str | None = None) -> dict[str, DeliveryResult]:
        """Retry previously failed, safely repeatable deliveries now."""
        failed = self.storage.get_failed_deliveries()
        if announcement_id:
            failed = [f for f in failed if f["announcement_id"] == announcement_id]

        adapter_names = {a.name for a in self.adapters}
        channel_settings = self.storage.get_channel_settings()
        retry_results: dict[str, DeliveryResult] = {}

        for item in failed:
            ann_id = item["announcement_id"]
            adapter_name = item["adapter_name"]
            operation = item["operation"]
            if (
                adapter_name not in adapter_names
                or operation not in ("publish", "update", "withdraw")
                or (not channel_settings.get(adapter_name, True) and operation != "withdraw")
                or not item["retryable"]
                or not item["idempotent"]
                or self.storage.get_announcement(ann_id) is None
            ):
                continue

            job = self.storage.enqueue_delivery_job(
                ann_id, adapter_name, operation, self.max_attempts, claim_for=JOB_LEASE
            )
            retry_results[f"{ann_id}:{adapter_name}"] = self._run_job(job)

        return retry_results
