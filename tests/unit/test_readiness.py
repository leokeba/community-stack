import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.models import Announcement, AnnouncementStatus
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.community import ChannelResolution, CommunityProfile, IndependentChannel
from community_stack.continuity import readiness
from community_stack.continuity.offline_exporter import write_checksummed_zip
from community_stack.subscriptions.manager import SubscriberManager


def profile(**overrides):
    values = {
        "name": "Community",
        "url": "https://community.example.org",
        "domain": "community.example.org",
        "base_domain": "community.example.org",
        "emergency_instructions": "Use the public radio desk.",
    }
    values.update(overrides)
    return CommunityProfile(**values)


def test_readiness_reports_missing_infrastructure_with_recommendations(tmp_path, monkeypatch):
    storage = AnnouncementStorage(tmp_path / "announcements.db")
    publisher = AnnouncementPublisher(storage=storage, adapters=[])
    community = profile(url="https://hosted.example.net", emergency_instructions="")
    monkeypatch.setattr(readiness, "load_community", lambda _path: (community, {}))
    monkeypatch.setattr(
        readiness,
        "resolve_independent",
        lambda *_args: ChannelResolution(shown=[], hidden=[]),
    )

    report = readiness.build_readiness_report(
        storage, publisher, tmp_path / "config.yaml", tmp_path / "guide.zip"
    )

    finding_keys = {item["key"] for item in report["findings"]}
    assert {
        "stable_url",
        "independent_channels",
        "subscription_workflow",
        "offline_archive",
        "emergency_instructions",
    } <= finding_keys
    assert all(item["recommendation"] for item in report["findings"])
    assert "score" not in report


def test_readiness_metrics_are_aggregate_and_validate_the_offline_guide(tmp_path, monkeypatch):
    storage = AnnouncementStorage(tmp_path / "announcements.db")
    announcement = Announcement(
        id="ann_readiness_test",
        title="Public update",
        body="Public information.",
        status=AnnouncementStatus.PUBLISHED,
    )
    storage.save_announcement(announcement)
    storage.record_delivery(announcement.id, "web_feed", "delivered")
    drill = Announcement(
        id="ann_drill_metric",
        title="Simulation",
        body="Simulation only.",
        status=AnnouncementStatus.PUBLISHED,
    )
    storage.save_announcement(drill)
    storage.record_delivery(drill.id, "web_feed", "delivered")
    distribution = storage.independent_delivery_metrics({"web_feed"})
    assert distribution["total"] == 1
    assert distribution["distributed"] == 1
    manager = SubscriberManager(tmp_path / "subscribers.db")
    manager.register_consent_text("test-v1", "Test consent text.")
    _subscriber, token = manager.request_subscription(
        "email", "private@example.org", "test-v1", "website"
    )
    assert token is not None
    manager.confirm_subscription(token)

    publisher = AnnouncementPublisher(
        storage=storage,
        adapters=[WebFeedAdapter(output_dir=tmp_path / "feed", storage=storage)],
    )
    community = profile()
    channel = IndependentChannel(
        key="website",
        label="Community website",
        description="Public announcements.",
        url="https://community.example.org/feed/announcements.html",
    )
    monkeypatch.setattr(readiness, "load_community", lambda _path: (community, {}))
    monkeypatch.setattr(
        readiness,
        "resolve_independent",
        lambda *_args: ChannelResolution(shown=[channel], hidden=[]),
    )

    guide = tmp_path / "guide.zip"
    files = {
        "index.html": b"guide",
        "guide.html": b"guide",
        "community.json": b"{}",
        "announcements.json": json.dumps([announcement.to_dict(), drill.to_dict()]).encode(),
        "emergency-procedures.txt": b"Use the public radio desk.",
        "qr.svg": b"<svg></svg>",
        "qr.png": b"png",
    }
    write_checksummed_zip(guide, files, "Community", "now", 2)

    report = readiness.build_readiness_report(storage, publisher, tmp_path / "config.yaml", guide)

    metrics = report["metrics"]
    assert metrics["confirmed_independent_subscribers"] == 1
    assert metrics["active_independent_channels"] == 1
    assert metrics["independently_distributed_announcements"] == 1
    assert metrics["independent_distribution_percent"] == 100.0
    assert metrics["announcements_in_offline_archive"] == 2
    assert "private@example.org" not in json.dumps(metrics)


def test_readiness_flags_untested_subscription_and_stale_independent_channel(tmp_path, monkeypatch):
    storage = AnnouncementStorage(tmp_path / "announcements.db")
    announcement = Announcement(
        id="ann_stale_delivery",
        title="Public update",
        body="Public information.",
        status=AnnouncementStatus.PUBLISHED,
    )
    storage.save_announcement(announcement)
    storage.record_delivery(announcement.id, "web_feed", "delivered")
    stale = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    with sqlite3.connect(storage.db_path) as connection:
        connection.execute(
            "UPDATE delivery_states SET last_attempt_at = ? WHERE announcement_id = ?",
            (stale, announcement.id),
        )
    publisher = AnnouncementPublisher(
        storage=storage,
        adapters=[
            WebFeedAdapter(output_dir=tmp_path / "feed", storage=storage),
            SimpleNamespace(name="email"),
        ],
    )
    community = profile()
    channel = IndependentChannel(
        key="website",
        label="Community website",
        description="Public announcements.",
        url="https://community.example.org/feed/announcements.html",
    )
    monkeypatch.setattr(readiness, "load_community", lambda _path: (community, {}))
    monkeypatch.setattr(
        readiness,
        "resolve_independent",
        lambda *_args: ChannelResolution(shown=[channel], hidden=[]),
    )

    report = readiness.build_readiness_report(
        storage, publisher, tmp_path / "config.yaml", tmp_path / "missing-guide.zip"
    )

    finding_keys = {item["key"] for item in report["findings"]}
    assert "subscription_workflow" in finding_keys
    assert "publication:website" in finding_keys
