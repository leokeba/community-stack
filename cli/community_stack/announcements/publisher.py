"""Announcement publishing orchestrator and adapter coordinator."""

from __future__ import annotations

from pathlib import Path

from community_stack.announcements.adapters.base import (
    ChannelAdapter,
    DeliveryResult,
)
from community_stack.announcements.adapters.matrix_room import MatrixRoomAdapter
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.models import Announcement, AnnouncementStatus
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.config import load_config


class AnnouncementPublisher:
    """Orchestrates publishing across canonical storage and channel adapters."""

    def __init__(
        self,
        storage: AnnouncementStorage | None = None,
        adapters: list[ChannelAdapter] | None = None,
        config_path: Path | str = "community-stack.yaml",
    ) -> None:
        self.config_path = Path(config_path)
        self.storage = storage or AnnouncementStorage()

        if adapters is not None:
            self.adapters = adapters
        else:
            self.adapters = self._load_default_adapters()

    def _load_default_adapters(self) -> list[ChannelAdapter]:
        adapters: list[ChannelAdapter] = []
        # Always configure WebFeedAdapter
        base_url = "https://community.example.org"
        community_name = "Community Communications"
        if self.config_path.is_file():
            try:
                cfg = load_config(self.config_path)
                domain = cfg.get("domain", {}).get("web", "community.example.org")
                base_url = f"https://{domain}"
                community_name = cfg.get("project", {}).get("name", "Community")
            except Exception:
                pass

        adapters.append(WebFeedAdapter(base_url=base_url, community_name=community_name))
        # Add Matrix room adapter
        adapters.append(MatrixRoomAdapter())
        return adapters

    def publish(
        self,
        title: str,
        body: str,
        author: str | None = None,
        tags: list[str] | None = None,
    ) -> tuple[Announcement, dict[str, DeliveryResult]]:
        """Publish a new announcement to storage and all active channel adapters."""
        ann = Announcement(
            title=title,
            body=body,
            author=author,
            tags=tags or [],
            status=AnnouncementStatus.PUBLISHED,
        )

        # 1. Store durably in SQLite
        self.storage.save_announcement(ann)

        # 2. Dispatch to each adapter independently
        results: dict[str, DeliveryResult] = {}
        for adapter in self.adapters:
            try:
                res = adapter.publish(ann)
            except Exception as exc:
                res = DeliveryResult(success=False, error=str(exc), retryable=False)

            results[adapter.name] = res
            status = "delivered" if res.success else "failed"
            self.storage.record_delivery(
                announcement_id=ann.id,
                adapter_name=adapter.name,
                status=status,
                error=res.error,
                external_id=res.external_id,
            )

        return ann, results

    def update(
        self,
        announcement_id: str,
        title: str | None = None,
        body: str | None = None,
        author: str | None = None,
    ) -> tuple[Announcement, dict[str, DeliveryResult]]:
        """Update an existing announcement across all channels."""
        ann = self.storage.get_announcement(announcement_id)
        if not ann:
            raise KeyError(f"Announcement '{announcement_id}' not found.")

        if title is not None:
            ann.title = title
        if body is not None:
            ann.body = body
        if author is not None:
            ann.author = author

        ann.status = AnnouncementStatus.UPDATED
        ann.validate()

        self.storage.save_announcement(ann)

        results: dict[str, DeliveryResult] = {}
        for adapter in self.adapters:
            try:
                res = adapter.update(ann)
            except Exception as exc:
                res = DeliveryResult(success=False, error=str(exc), retryable=False)

            results[adapter.name] = res
            status = "delivered" if res.success else "failed"
            self.storage.record_delivery(
                announcement_id=ann.id,
                adapter_name=adapter.name,
                status=status,
                error=res.error,
                external_id=res.external_id,
            )

        return ann, results

    def withdraw(self, announcement_id: str) -> tuple[Announcement, dict[str, DeliveryResult]]:
        """Retract an announcement across all channels."""
        ann = self.storage.get_announcement(announcement_id)
        if not ann:
            raise KeyError(f"Announcement '{announcement_id}' not found.")

        ann.status = AnnouncementStatus.WITHDRAWN
        self.storage.save_announcement(ann)

        results: dict[str, DeliveryResult] = {}
        for adapter in self.adapters:
            try:
                res = adapter.withdraw(announcement_id)
            except Exception as exc:
                res = DeliveryResult(success=False, error=str(exc), retryable=False)

            results[adapter.name] = res
            status = "withdrawn" if res.success else "failed"
            self.storage.record_delivery(
                announcement_id=ann.id,
                adapter_name=adapter.name,
                status=status,
                error=res.error,
            )

        return ann, results

    def retry_failed(self, announcement_id: str | None = None) -> dict[str, DeliveryResult]:
        """Retry delivery for previously failed adapter attempts."""
        failed = self.storage.get_failed_deliveries()
        if announcement_id:
            failed = [f for f in failed if f["announcement_id"] == announcement_id]

        adapter_map = {a.name: a for a in self.adapters}
        retry_results: dict[str, DeliveryResult] = {}

        for item in failed:
            ann_id = item["announcement_id"]
            adapter_name = item["adapter_name"]
            adapter = adapter_map.get(adapter_name)

            if not adapter:
                continue

            ann = self.storage.get_announcement(ann_id)
            if not ann:
                continue

            try:
                res = adapter.publish(ann)
            except Exception as exc:
                res = DeliveryResult(success=False, error=str(exc), retryable=False)

            key = f"{ann_id}:{adapter_name}"
            retry_results[key] = res
            new_status = "delivered" if res.success else "failed"
            self.storage.record_delivery(
                announcement_id=ann_id,
                adapter_name=adapter_name,
                status=new_status,
                error=res.error,
                external_id=res.external_id,
            )

        return retry_results
