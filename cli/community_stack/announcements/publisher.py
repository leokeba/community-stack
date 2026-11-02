"""Announcement publishing orchestrator and adapter coordinator."""

from __future__ import annotations

import logging
import os
from copy import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from community_stack.announcements.adapters.base import (
    ChannelAdapter,
    ChannelCheck,
    DeliveryResult,
    UpdateMode,
    WithdrawMode,
)
from community_stack.announcements.adapters.manual import ManualChannelAdapter
from community_stack.announcements.adapters.matrix_room import MatrixRoomAdapter
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.connectors import (
    CONNECTORS,
    build_connector,
    build_email_adapter,
)
from community_stack.announcements.fediverse import Fediverse, FediverseAdapter
from community_stack.announcements.models import UNPUBLISHED, Announcement, AnnouncementStatus
from community_stack.announcements.platform_content import generate_platform_text
from community_stack.announcements.platforms import PLATFORM_CATALOG, PLATFORMS
from community_stack.announcements.sms import SmsGatewayAdapter
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.config import load_config
from community_stack.continuity.connect_page import build_site, load_identity, write_files
from community_stack.secrets import parse_env_file
from community_stack.subscriptions.email_signup import new_secret
from community_stack.subscriptions.manager import SubscriberManager

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 5
JOB_LEASE = timedelta(minutes=5)
RETRY_BASE_DELAY = timedelta(seconds=30)
RETRY_MAX_DELAY = timedelta(hours=1)


class ManualOutcomeConflict(ValueError):
    """A manual publication result does not match the current delivery task."""


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
        self.base_url = "https://community.example.org"
        self.connect_url = f"{self.base_url}/connect/"
        self.feed_dir = Path(feed_dir)
        self.storage = storage or AnnouncementStorage()
        self.whatsapp_bridge_enabled = False
        self.matrix_auth: dict[str, str] | None = None

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
        social_platforms: set[str] = set()
        self.whatsapp_bridge_enabled = False
        self.matrix_auth = None
        if self.config_path.is_file():
            cfg = load_config(self.config_path)
            domain = cfg.get("domain", {}).get("web", "community.example.org")
            base_url = f"https://{domain}"
            self.base_url = base_url
            community_url = cfg.get("community", {}).get("url") or base_url
            self.connect_url = f"{community_url.rstrip('/')}/connect/"
            community_name = cfg.get("project", {}).get("name", "Community")
            social_platforms = {
                item["platform"] for item in (cfg.get("community") or {}).get("social", [])
            }
            announcement_config = cfg["services"]["announcements"]
            if not announcement_config["enabled"]:
                return adapters
            channels = announcement_config.get("channels", {})
            self.whatsapp_bridge_enabled = (
                cfg.get("services", {}).get("whatsapp_bridge", {}).get("enabled", False)
            )

        web_config = channels.get("web_feed", {})
        if (
            not web_config.get("enabled", True)
            and "web_feed" not in self.storage.get_channel_settings()
        ):
            self.storage.set_channel_enabled("web_feed", False)
        connections = self.storage.list_connections()
        matrix_connection = connections.get("matrix_room")
        matrix_config = matrix_connection or channels.get("matrix_room", {})
        channel_links = {}
        if matrix_connection or matrix_config.get("enabled", False):
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

        if matrix_connection:
            self.matrix_auth = {
                "homeserver_url": matrix_connection["homeserver_url"],
                "access_token": matrix_connection["access_token"],
            }
        elif matrix_config.get("enabled", False):
            token_env = matrix_config["access_token_env"]
            env_path = self.config_path.parent / ".env"
            env_values = parse_env_file(env_path)
            access_token = os.environ.get(token_env) or env_values.get(token_env)
            if not access_token:
                raise ValueError(
                    "Matrix announcements are enabled, but the access token "
                    f"environment variable {token_env} is not set."
                )
            self.matrix_auth = {
                "homeserver_url": matrix_config["homeserver_url"],
                "access_token": access_token,
            }
            adapters.append(
                MatrixRoomAdapter(
                    homeserver_url=matrix_config["homeserver_url"],
                    room_id=matrix_config["room_id"],
                    access_token=access_token,
                )
            )
        connected = self._connected_adapters()
        configured_names = {adapter.name for adapter in (*adapters, *connected)}
        manual = [
            ManualChannelAdapter(platform)
            for platform in PLATFORM_CATALOG
            if platform.identifier in social_platforms
            and platform.identifier not in configured_names
        ]
        return adapters + connected + manual

    def _connected_adapters(self) -> list[ChannelAdapter]:
        """Adapters for channels organizers connected in the console."""
        adapters = []
        connections = self.storage.list_connections()
        for kind, values in connections.items():
            connector = CONNECTORS.get(kind)
            if connector is None:
                continue
            if kind == "whatsapp":
                if not self.whatsapp_bridge_enabled or self.matrix_auth is None:
                    continue
                try:
                    adapters.append(build_connector(kind, values, self.matrix_auth))
                except (KeyError, ValueError):
                    logger.warning(
                        "Skipping the %s connection: its saved settings are invalid.", kind
                    )
                continue
            if kind == "email":
                try:
                    adapters.append(
                        build_email_adapter(
                            values,
                            SubscriberManager(self.storage.db_path.parent / "subscribers.db"),
                            self._unsubscribe_secret(),
                        )
                    )
                except (KeyError, ValueError):
                    logger.warning("Skipping the email connection: its saved settings are invalid.")
                continue
            if kind == "sms":
                adapters.append(
                    SmsGatewayAdapter(
                        values["base_url"],
                        values["username"],
                        values["password"],
                        SubscriberManager(self.storage.db_path.parent / "subscribers.db"),
                        stop_url=f"{self.base_url}/subscribe/stop",
                    )
                )
                continue
            if kind == "fediverse":
                adapters.append(
                    FediverseAdapter(Fediverse(self.storage, self.base_url, values["username"]))
                )
                continue
            try:
                adapters.append(connector.build(values))
            except (KeyError, ValueError):
                logger.warning("Skipping the %s connection: its saved settings are invalid.", kind)
        return adapters

    def _unsubscribe_secret(self) -> bytes:
        """The key that signs unsubscribe links, created once and kept in the announcements database."""
        secret = self.storage.get_setting("subscriber_token_secret", "")
        if not secret:
            secret = new_secret()
            self.storage.set_setting("subscriber_token_secret", secret)
        return secret.encode()

    def reload_connections(self) -> None:
        """Pick up channels connected or disconnected since the publisher started."""
        self.adapters = self._load_default_adapters()

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
            adapter.check(self.channel_variant(announcement, adapter.name))
            for adapter in self.adapters
            if channel_settings.get(adapter.name, True)
        ]

    def canonical_url_for(self, announcement: Announcement) -> str:
        """Return the public feed URL, including for drafts not published yet."""
        if announcement.canonical_url:
            return announcement.canonical_url
        web_adapter = next(
            (adapter for adapter in self.adapters if isinstance(adapter, WebFeedAdapter)),
            None,
        )
        return web_adapter.canonical_url_for(announcement) if web_adapter else ""

    def channel_variant(self, announcement: Announcement, channel_name: str) -> Announcement:
        """Return an announcement copy with generated text and channel media applied."""
        platform = PLATFORMS.get(channel_name)
        has_media_override = channel_name in announcement.channel_media
        if not has_media_override and (
            platform is None or channel_name in announcement.channel_text
        ):
            return announcement
        variant = copy(announcement)
        if has_media_override:
            variant.image = announcement.channel_media[channel_name]
            variant.image_id = announcement.channel_media_ids[channel_name]
        if platform is not None and channel_name not in announcement.channel_text:
            variant.channel_text = dict(announcement.channel_text)
            variant.channel_text[channel_name] = generate_platform_text(
                title=announcement.title,
                body=announcement.body,
                canonical_url=self.canonical_url_for(announcement),
                tags=announcement.tags,
                connect_url=self.connect_url,
                platform=platform,
            )
        return variant

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
        # Edits and withdrawals only go where the announcement was sent, so a channel
        # never gets a correction for a post it never had.
        prior_states = (
            {state["adapter_name"]: state for state in self.storage.get_delivery_states(ann.id)}
            if operation != "publish"
            else {}
        )
        reached = set(prior_states)
        for adapter in self.adapters:
            manual_update = isinstance(adapter, ManualChannelAdapter) and operation == "update"
            if (
                not channel_settings.get(adapter.name, True)
                and operation != "withdraw"
                and not manual_update
            ):
                continue
            if operation == "publish" and ann.channels and adapter.name not in ann.channels:
                continue
            if operation != "publish" and adapter.name not in reached:
                continue
            if isinstance(adapter, ManualChannelAdapter) and operation != "publish":
                previous = prior_states[adapter.name]
                if previous["status"] == "withdrawn" or (
                    previous["status"] == "failed"
                    and previous["operation"] in ("publish", "manual_publish")
                ):
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
        # The /connect page follows canonical state, so refresh it even if no channel took the job.
        self.refresh_connect_page()
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
            elif not self.storage.get_channel_settings().get(adapter.name, True) and not (
                isinstance(adapter, ManualChannelAdapter) and operation == "update"
            ):
                reason = "Channel was disabled before delivery."
            if reason:
                self.storage.finish_job(job_id, "cancelled", reason)
                return DeliveryResult(success=False, error=reason)

        try:
            external_id = self.storage.get_external_id(ann.id, adapter.name)
            channel_announcement = self.channel_variant(ann, adapter.name)
            if operation == "publish":
                res = adapter.publish(channel_announcement)
            elif operation == "update":
                res = adapter.update_post(channel_announcement, external_id)
            else:
                res = adapter.withdraw_post(ann.id, external_id)
        except Exception as exc:
            res = DeliveryResult(success=False, error=str(exc), retryable=False)

        previous_manual_state = (
            next(
                (
                    state
                    for state in self.storage.get_delivery_states(ann.id)
                    if state["adapter_name"] == adapter.name
                ),
                None,
            )
            if isinstance(adapter, ManualChannelAdapter)
            else None
        )
        self.storage.record_delivery(
            announcement_id=ann.id,
            adapter_name=adapter.name,
            status=(
                "prepared"
                if res.success and res.prepared
                else "withdrawn"
                if operation == "withdraw" and res.success
                else "delivered"
                if res.success
                else "failed"
            ),
            error=res.error,
            external_id=res.external_id,
            operation=(
                f"manual_{operation}" if isinstance(adapter, ManualChannelAdapter) else operation
            ),
            retryable=res.retryable,
            idempotent=res.idempotent,
            post_url=(
                previous_manual_state["post_url"]
                if previous_manual_state and operation in ("update", "withdraw")
                else None
            ),
        )
        if res.success:
            self.storage.finish_job(job_id, "succeeded")
        elif res.retryable and res.idempotent and job["attempts"] < job["max_attempts"]:
            retry_at = datetime.now(timezone.utc) + retry_delay(job["attempts"])
            self.storage.finish_job(job_id, "queued", res.error, retry_at=retry_at)
        else:
            self.storage.finish_job(job_id, "failed", res.error)
        return res

    def refresh_connect_page(self) -> None:
        """Rebuild the public /connect page. A failure here must never block delivery."""
        try:
            profile, resolution, announcements, logo, cover = load_identity(
                self.config_path, self.storage
            )
            files = build_site(profile, resolution, announcements, logo, cover=cover)
            write_files(self.feed_dir.parent / "connect", files)
        except Exception:
            logger.warning("Could not refresh the /connect page.", exc_info=True)

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

    def record_manual_outcome(
        self,
        announcement_id: str,
        channel: str,
        outcome: str,
        recorded_by: str,
        post_url: str | None = None,
        note: str | None = None,
    ) -> tuple[str, str, str | None]:
        """Record a manual publish, update, or withdrawal result."""
        if channel not in PLATFORMS:
            raise KeyError(f"Unknown platform '{channel}'.")
        recorded_by = recorded_by.strip()
        if not recorded_by:
            raise ValueError("The person recording the outcome is required.")

        announcement = self.preview(announcement_id)
        adapter = next(
            (
                item
                for item in self.adapters
                if item.name == channel and isinstance(item, ManualChannelAdapter)
            ),
            None,
        )
        delivery = next(
            (
                item
                for item in self.storage.get_delivery_states(announcement_id)
                if item["adapter_name"] == channel
            ),
            None,
        )
        if delivery is None or delivery["status"] not in ("prepared", "failed", "unknown"):
            raise ManualOutcomeConflict("This manual delivery is not awaiting an outcome.")

        operation = delivery["operation"]
        manual_action = {
            "manual_publish": "publish",
            "manual_update": "update",
            "manual_withdraw": "withdraw",
        }.get(operation)
        if manual_action is None and adapter is not None:
            manual_action = operation if operation in ("publish", "update", "withdraw") else None
        if manual_action is None or (adapter is None and not operation.startswith("manual_")):
            raise ManualOutcomeConflict("This platform is not set up for manual posting.")

        if manual_action == "withdraw":
            valid_announcement_state = announcement.status == AnnouncementStatus.WITHDRAWN
        else:
            valid_announcement_state = announcement.status in (
                AnnouncementStatus.PUBLISHED,
                AnnouncementStatus.UPDATED,
            )
        if not valid_announcement_state:
            raise ManualOutcomeConflict("This manual action does not match the announcement state.")

        outcome_statuses = {
            "publish": {"published": "delivered", "failed": "failed", "unknown": "unknown"},
            "update": {"updated": "updated", "failed": "failed", "unknown": "unknown"},
            "withdraw": {"withdrawn": "withdrawn", "failed": "failed", "unknown": "unknown"},
        }
        status_by_outcome = outcome_statuses[manual_action]
        if outcome not in status_by_outcome:
            choices = ", ".join(label.capitalize() for label in status_by_outcome)
            raise ValueError(f"Choose {choices}.")

        post_url = (post_url or "").strip()
        note = " ".join((note or "").split())[:1000]
        if outcome in ("published", "updated"):
            try:
                parsed_url = urlsplit(post_url)
                valid_url = (
                    len(post_url) <= 2048
                    and not any(character.isspace() for character in post_url)
                    and parsed_url.scheme == "https"
                    and parsed_url.hostname is not None
                    and parsed_url.path not in ("", "/")
                    and parsed_url.username is None
                    and parsed_url.password is None
                )
                _ = parsed_url.port
            except ValueError:
                valid_url = False
            if not valid_url:
                raise ValueError("Enter the HTTPS URL of the published or updated post.")
            recorded_post_url = post_url
            error = None
        else:
            recorded_post_url = (
                delivery["post_url"]
                if manual_action in ("update", "withdraw") and outcome in ("failed", "unknown")
                else None
            )
            action_label = "post" if manual_action == "publish" else manual_action
            error = note or (
                f"Administrator recorded that the manual {action_label} failed."
                if outcome == "failed"
                else f"Administrator could not confirm whether the manual {action_label} was completed."
            )

        self.storage.record_delivery(
            announcement_id,
            channel,
            status_by_outcome[outcome],
            error=error,
            operation=f"manual_{manual_action}",
            recorded_by=recorded_by,
            post_url=recorded_post_url,
        )
        return manual_action, outcome, recorded_post_url

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
