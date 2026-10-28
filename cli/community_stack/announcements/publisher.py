"""Announcement publishing orchestrator and adapter coordinator."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from community_stack.announcements.adapters.base import (
    ChannelAdapter,
    DeliveryResult,
)
from community_stack.announcements.adapters.matrix_room import MatrixRoomAdapter
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.models import Announcement, AnnouncementStatus
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.config import load_config
from community_stack.secrets import parse_env_file


class AnnouncementPublisher:
    """Orchestrates publishing across canonical storage and channel adapters."""

    def __init__(
        self,
        storage: AnnouncementStorage | None = None,
        adapters: list[ChannelAdapter] | None = None,
        config_path: Path | str = "community-stack.yaml",
        feed_dir: Path | str = "data/landing/feed",
    ) -> None:
        self.config_path = Path(config_path)
        self.feed_dir = Path(feed_dir)
        self.storage = storage or AnnouncementStorage()

        if adapters is not None:
            self.adapters = adapters
        else:
            self.adapters = self._load_default_adapters()

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
        return adapters

    def create_draft(
        self,
        title: str,
        body: str,
        author: str | None = None,
        tags: list[str] | None = None,
    ) -> Announcement:
        """Create and persist a draft without dispatching it to any channel."""
        ann = Announcement(
            title=title,
            body=body,
            author=author,
            tags=tags or [],
            status=AnnouncementStatus.DRAFT,
        )
        self.storage.save_announcement(ann)
        return ann

    def preview(self, announcement_id: str) -> Announcement:
        """Return a persisted announcement for preview without publishing it."""
        ann = self.storage.get_announcement(announcement_id)
        if ann is None:
            raise KeyError(f"Announcement '{announcement_id}' not found.")
        return ann

    def publish_draft(self, announcement_id: str) -> tuple[Announcement, dict[str, DeliveryResult]]:
        """Publish a saved draft and dispatch it independently to each adapter."""
        ann = self.preview(announcement_id)
        if ann.status != AnnouncementStatus.DRAFT:
            raise ValueError(f"Announcement '{announcement_id}' is not a draft.")
        ann.status = AnnouncementStatus.PUBLISHED
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
        results: dict[str, DeliveryResult] = {}
        channel_settings = self.storage.get_channel_settings()
        for adapter in self.adapters:
            if not channel_settings.get(adapter.name, True) and operation != "withdraw":
                continue
            try:
                if operation == "publish":
                    res = adapter.publish(ann)
                elif operation == "update":
                    res = adapter.update(ann)
                else:
                    res = adapter.withdraw(ann.id)
            except Exception as exc:
                res = DeliveryResult(success=False, error=str(exc), retryable=False)

            results[adapter.name] = res
            delivery_status = (
                "withdrawn"
                if operation == "withdraw" and res.success
                else "delivered"
                if res.success
                else "failed"
            )
            self.storage.record_delivery(
                announcement_id=ann.id,
                adapter_name=adapter.name,
                status=delivery_status,
                error=res.error,
                external_id=res.external_id,
                operation=operation,
                retryable=res.retryable,
                idempotent=res.idempotent,
            )
        return results

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

        if ann.status == AnnouncementStatus.DRAFT:
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

        was_draft = ann.status == AnnouncementStatus.DRAFT
        ann.status = AnnouncementStatus.WITHDRAWN
        self.storage.save_announcement(ann)
        return (ann, {}) if was_draft else (ann, self._dispatch(ann, "withdraw"))

    def retry_failed(self, announcement_id: str | None = None) -> dict[str, DeliveryResult]:
        """Retry delivery for previously failed adapter attempts."""
        failed = self.storage.get_failed_deliveries()
        if announcement_id:
            failed = [f for f in failed if f["announcement_id"] == announcement_id]

        adapter_map = {a.name: a for a in self.adapters}
        channel_settings = self.storage.get_channel_settings()
        retry_results: dict[str, DeliveryResult] = {}

        for item in failed:
            ann_id = item["announcement_id"]
            adapter_name = item["adapter_name"]
            operation = item["operation"]
            adapter = adapter_map.get(adapter_name)

            if (
                not adapter
                or (not channel_settings.get(adapter_name, True) and operation != "withdraw")
                or not item["retryable"]
                or not item["idempotent"]
            ):
                continue

            ann = self.storage.get_announcement(ann_id)
            if ann is None:
                continue

            try:
                if operation == "publish":
                    res = adapter.publish(ann)
                elif operation == "update":
                    res = adapter.update(ann)
                elif operation == "withdraw":
                    res = adapter.withdraw(ann_id)
                else:
                    continue
            except Exception as exc:
                res = DeliveryResult(success=False, error=str(exc), retryable=False)

            key = f"{ann_id}:{adapter_name}"
            retry_results[key] = res
            new_status = (
                "withdrawn"
                if operation == "withdraw" and res.success
                else "delivered"
                if res.success
                else "failed"
            )
            self.storage.record_delivery(
                announcement_id=ann_id,
                adapter_name=adapter_name,
                status=new_status,
                error=res.error,
                external_id=res.external_id,
                operation=operation,
                retryable=res.retryable,
                idempotent=res.idempotent,
            )

        return retry_results
