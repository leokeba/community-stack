"""Aggregate-only continuity metrics and actionable readiness findings."""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from community_stack.announcements.models import AnnouncementStatus
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.community import load_community, resolve_independent
from community_stack.continuity.offline_exporter import OfflineArchiveExporter
from community_stack.subscriptions.manager import SubscriberManager

INDEPENDENT_ADAPTERS = {
    "website": "web_feed",
    "rss": "web_feed",
    "email": "email",
    "matrix": "matrix_room",
    "activitypub": "fediverse",
}
ARCHIVE_FILES = {
    "index.html",
    "guide.html",
    "community.json",
    "announcements.json",
    "emergency-procedures.txt",
    "qr.svg",
    "qr.png",
    "manifest.json",
}
PUBLIC_STATUSES = (AnnouncementStatus.PUBLISHED, AnnouncementStatus.UPDATED)
RECENT_PUBLICATION_WINDOW = timedelta(days=90)


def _finding(key: str, title: str, detail: str, recommendation: str) -> dict[str, str]:
    return {
        "key": key,
        "title": title,
        "detail": detail,
        "recommendation": recommendation,
    }


def _archive_announcement_count(path: Path, expected_ids: set[str]) -> tuple[int, str | None]:
    if not path.is_file():
        return 0, "No offline continuity guide has been generated."
    valid, messages = OfflineArchiveExporter.verify_archive(path)
    if not valid:
        return 0, "; ".join(messages)
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            missing = ARCHIVE_FILES - names
            if missing:
                return 0, f"The guide is missing files: {', '.join(sorted(missing))}."
            items = json.loads(archive.read("announcements.json"))
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        return 0, f"The guide could not be read: {exc}"
    if not isinstance(items, list):
        return 0, "The guide's announcements.json is not a list."
    archived_ids = {
        item.get("id")
        for item in items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if not expected_ids <= archived_ids:
        return len(items), "The guide does not contain all of the latest public announcements."
    return len(items), None


def build_readiness_report(
    storage: AnnouncementStorage,
    publisher: AnnouncementPublisher,
    config_path: Path | str,
    guide_path: Path | str,
) -> dict:
    """Build metrics and findings without exposing subscriber records or a score."""
    profile, config = load_community(config_path)
    connections = storage.list_connections()
    resolution = resolve_independent(profile, connections, config)
    adapter_names = {adapter.name for adapter in publisher.adapters}
    settings = storage.get_channel_settings()
    active_channels = [
        channel
        for channel in resolution.shown
        if (adapter := INDEPENDENT_ADAPTERS[channel.key]) in adapter_names
        and settings.get(adapter, True)
    ]
    active_adapter_names = {INDEPENDENT_ADAPTERS[channel.key] for channel in active_channels}
    delivery = storage.independent_delivery_metrics(active_adapter_names)
    total = int(delivery["total"] or 0)
    distributed = int(delivery["distributed"] or 0)
    percent = round(distributed * 100 / total, 1) if total else 0.0
    subscriber_manager = SubscriberManager(storage.db_path.parent / "subscribers.db")
    confirmed_subscribers = subscriber_manager.count_confirmed_active()
    subscribers_by_channel = {
        channel: subscriber_manager.count_confirmed_active(channel) for channel in ("email", "sms")
    }

    public_announcements = [
        item for item in storage.list_announcements(limit=200) if item.status in PUBLIC_STATUSES
    ]
    guide_count, archive_error = _archive_announcement_count(
        Path(guide_path), {item.id for item in public_announcements}
    )
    metrics = {
        "confirmed_independent_subscribers": confirmed_subscribers,
        "active_independent_channels": len(active_channels),
        "active_channel_labels": [channel.label for channel in active_channels],
        "independent_distribution_percent": percent,
        "independently_distributed_announcements": distributed,
        "public_announcements": total,
        "last_successful_independent_publication": delivery["last_success_at"],
        "announcements_in_offline_archive": guide_count,
    }
    findings: list[dict[str, str]] = []

    url = urlsplit(profile.connect_url)
    if url.scheme != "https" or not url.hostname:
        findings.append(
            _finding(
                "stable_url",
                "Stable community URL is missing or not HTTPS",
                profile.connect_url,
                "Configure the canonical community URL on a domain the community controls.",
            )
        )
    elif url.hostname != profile.base_domain and not url.hostname.endswith(
        f".{profile.base_domain}"
    ):
        findings.append(
            _finding(
                "stable_url",
                "Community URL depends on an external host name",
                profile.connect_url,
                "Use a community-controlled domain so the address can survive a host move.",
            )
        )

    if not active_channels:
        findings.append(
            _finding(
                "independent_channels",
                "No independent channels are active",
                "No enabled, configured independent channel is available on /connect.",
                "Enable the public website or feed and connect at least one community-owned channel.",
            )
        )

    configured_subscription_channels = adapter_names & {"email", "sms"}
    if not configured_subscription_channels:
        findings.append(
            _finding(
                "subscription_workflow",
                "No subscription workflow is configured",
                "Neither email nor SMS subscriptions are connected.",
                "Configure an opt-in subscription channel, then test signup, confirmation and unsubscribe.",
            )
        )
    else:
        untested = [
            channel
            for channel in sorted(configured_subscription_channels)
            if subscribers_by_channel[channel] == 0
        ]
        if untested:
            findings.append(
                _finding(
                    "subscription_workflow",
                    "Subscription workflow has no confirmed-user evidence",
                    f"No confirmed active subscriber is recorded for: {', '.join(untested)}.",
                    "Run an end-to-end signup, confirmation, delivery and unsubscribe test for each listed channel.",
                )
            )

    now = datetime.now(timezone.utc)
    for channel in active_channels:
        adapter = INDEPENDENT_ADAPTERS[channel.key]
        result = storage.independent_delivery_metrics({adapter})
        last_success = result["last_success_at"]
        recent = False
        if isinstance(last_success, str):
            try:
                published_at = datetime.fromisoformat(last_success.replace("Z", "+00:00"))
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=timezone.utc)
                recent = now - published_at.astimezone(timezone.utc) <= RECENT_PUBLICATION_WINDOW
            except ValueError:
                recent = False
        if not recent:
            findings.append(
                _finding(
                    f"publication:{channel.key}",
                    f"No recent successful publication on {channel.label}",
                    f"No successful delivery is recorded in the last {RECENT_PUBLICATION_WINDOW.days} days.",
                    "Publish a routine test update to this channel and confirm it is publicly accessible.",
                )
            )

    if archive_error:
        findings.append(
            _finding(
                "offline_archive",
                "Offline continuity guide is missing or incomplete",
                archive_error,
                "Run `community-stack continuity package`, verify it with `community-stack archive verify`, and distribute the new copy.",
            )
        )
    if not profile.emergency_instructions.strip():
        findings.append(
            _finding(
                "emergency_instructions",
                "Emergency instructions are missing",
                "The community profile has no emergency instructions for members.",
                "Add concise, reviewed emergency instructions to the community profile and rebuild the offline guide.",
            )
        )

    return {"metrics": metrics, "findings": findings}


def format_readiness_report(report: dict) -> str:
    metrics = report["metrics"]
    lines = [
        "Continuity readiness metrics (aggregates only):",
        f"  Confirmed independent subscribers: {metrics['confirmed_independent_subscribers']}",
        f"  Active independent channels: {metrics['active_independent_channels']}",
        (
            f"  Independently distributed announcements: "
            f"{metrics['independently_distributed_announcements']}/"
            f"{metrics['public_announcements']} ({metrics['independent_distribution_percent']}%)"
        ),
        (
            "  Last successful independent publication: "
            f"{metrics['last_successful_independent_publication'] or 'never'}"
        ),
        (
            f"  Announcements in verified offline guide: "
            f"{metrics['announcements_in_offline_archive']}"
        ),
        "",
    ]
    findings = report["findings"]
    if not findings:
        lines.append("No readiness findings.")
    else:
        lines.append("Readiness findings and recommendations:")
        for finding in findings:
            lines.extend(
                [
                    f"- {finding['title']}",
                    f"  {finding['detail']}",
                    f"  Recommendation: {finding['recommendation']}",
                ]
            )
    return "\n".join(lines)
