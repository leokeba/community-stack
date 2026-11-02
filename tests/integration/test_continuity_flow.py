"""Integration tests validating full offline archive creation, distribution, and verification flow."""

import json
import zipfile

from community_stack.announcements.adapters.manual import ManualChannelAdapter
from community_stack.announcements.adapters.matrix_room import MatrixRoomAdapter
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.models import Announcement, AnnouncementStatus
from community_stack.announcements.platforms import PLATFORMS
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.continuity.offline_exporter import OfflineArchiveExporter
from community_stack.continuity.package import build_continuity_package
from community_stack.continuity.service import ContinuityService
from community_stack.subscriptions.manager import SubscriberManager


def test_offline_continuity_end_to_end_flow(tmp_path):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")

    # 1. Create active and withdrawn announcements
    ann1 = Announcement(
        title="Active Medical Aid Station Open",
        body="First aid and medical advice available at North High School.",
        author="Health Working Group",
        tags=["medical", "health"],
        status=AnnouncementStatus.PUBLISHED,
    )
    ann2 = Announcement(
        title="Outdated Curfew Notice",
        body="This notice has been superseded.",
        status=AnnouncementStatus.WITHDRAWN,
    )
    draft = Announcement(title="Unreleased plan", body="Not public yet.")
    storage.save_announcement(ann1)
    storage.save_announcement(ann2)
    storage.save_announcement(draft)

    # 2. Export offline archive bundle
    exporter = OfflineArchiveExporter(storage=storage, community_name="City Mutual Aid")
    export_bundle = tmp_path / "emergency_broadcast.zip"
    exporter.export_archive(export_bundle)

    assert export_bundle.is_file()

    # 3. Verify bundle cryptographically
    is_valid, _messages = OfflineArchiveExporter.verify_archive(export_bundle)
    assert is_valid is True

    # 4. Extract into an offline web root (simulating USB stick or captive portal)
    extract_root = tmp_path / "captive_portal_www"
    extract_root.mkdir()
    with zipfile.ZipFile(export_bundle, "r") as zf:
        zf.extractall(extract_root)

    # Verify extracted offline files
    html_file = extract_root / "index.html"
    json_file = extract_root / "announcements.json"
    manifest_file = extract_root / "manifest.json"

    assert html_file.is_file()
    assert json_file.is_file()
    assert manifest_file.is_file()

    html_content = html_file.read_text(encoding="utf-8")
    assert "Active Medical Aid Station Open" in html_content
    assert "North High School" in html_content
    assert "City Mutual Aid" in html_content

    # Withdrawn announcements and unpublished drafts must be excluded
    assert "Outdated Curfew Notice" not in html_content
    assert "Unreleased plan" not in html_content

    # Structured JSON file must contain exact 1 active announcement
    json_data = json.loads(json_file.read_text(encoding="utf-8"))
    assert len(json_data) == 1
    assert json_data[0]["title"] == "Active Medical Aid Station Open"


def test_activation_targets_unaffected_channels_and_isolates_provider_failure(tmp_path):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    web_feed = WebFeedAdapter(output_dir=tmp_path / "feed", storage=storage)
    matrix = MatrixRoomAdapter(
        transport=lambda method, path, headers, payload: (503, {"error": "temporarily down"})
    )
    manual = ManualChannelAdapter(PLATFORMS["facebook"])
    publisher = AnnouncementPublisher(
        storage=storage,
        adapters=[web_feed, matrix, manual],
        config_path=tmp_path / "missing.yaml",
        feed_dir=tmp_path / "feed",
    )
    service = ContinuityService(storage, publisher, tmp_path / "missing.yaml")
    connections_before = storage.list_connections()

    result = service.activate(
        "olga", "Instagram account access has been restricted.", ["instagram"]
    )

    assert result["steps"] == {
        "public_notice": "completed",
        "migration_announcements": "completed",
        "offline_guide": "completed",
        "refresh_connect_page": "completed",
    }
    assert "instagram" not in result["target_channels"]
    assert {"web_feed", "matrix_room", "facebook"} <= set(result["target_channels"])
    [announcement] = storage.list_announcements(limit=10)
    states = {
        state["adapter_name"]: state for state in storage.get_delivery_states(announcement.id)
    }
    assert states["web_feed"]["status"] == "delivered"
    assert states["matrix_room"]["status"] != "delivered"
    assert states["facebook"]["status"] == "prepared"
    assert service.store.list_channel_states()["instagram"]["status"] == "unavailable"
    assert storage.list_connections() == connections_before


def test_activation_resumes_failed_step_without_duplicating_or_staling_guide(tmp_path, monkeypatch):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    web_feed = WebFeedAdapter(output_dir=tmp_path / "feed", storage=storage)
    publisher = AnnouncementPublisher(
        storage=storage,
        adapters=[web_feed],
        config_path=tmp_path / "missing.yaml",
        feed_dir=tmp_path / "feed",
    )
    service = ContinuityService(storage, publisher, tmp_path / "missing.yaml")
    original = service._publish_migration_messages
    calls = 0

    def fail_once(current):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected migration-step failure")
        return original(current)

    monkeypatch.setattr(service, "_publish_migration_messages", fail_once)
    first = service.activate("olga", "A social network is unavailable.", ["instagram"])
    assert first["steps"]["migration_announcements"] == "failed"
    [event] = service.store.list_events()

    second = service.activate("olga", "A social network is unavailable.", ["instagram"])

    assert second["event_id"] == first["event_id"] == event["event_id"]
    assert second["steps"]["migration_announcements"] == "completed"
    [announcement] = storage.list_announcements(limit=10)
    with zipfile.ZipFile(service.guide_path) as archive:
        bundled = archive.read("announcements.json").decode()
    assert announcement.title in bundled
    assert len(service.store.list_events()) == 1
    step_attempts = {step["step"]: step["attempts"] for step in service.status()["steps"]}
    assert step_attempts["migration_announcements"] == 2
    assert step_attempts["offline_guide"] == 2
    assert step_attempts["refresh_connect_page"] == 2


def test_deactivation_clears_notice_but_keeps_history_and_channel_connections(tmp_path):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    publisher = AnnouncementPublisher(
        storage=storage,
        adapters=[WebFeedAdapter(output_dir=tmp_path / "feed", storage=storage)],
        config_path=tmp_path / "missing.yaml",
        feed_dir=tmp_path / "feed",
    )
    service = ContinuityService(storage, publisher, tmp_path / "missing.yaml")
    before = storage.list_connections()
    service.activate("olga", "A social network is unavailable.", ["instagram"])

    result = service.deactivate("olga", "Normal service has resumed.")

    assert result["steps"] == {"offline_guide": "completed", "refresh_connect_page": "completed"}
    assert service.store.active_notice() is None
    assert service.store.list_channel_states()["instagram"]["status"] == "unavailable"
    assert [event["action"] for event in reversed(service.store.list_events())] == [
        "activated",
        "deactivated",
    ]
    assert storage.list_connections() == before
    assert "Continuity notice" not in (tmp_path / "connect" / "index.html").read_text()


def test_continuity_package_is_verified_and_excludes_subscriber_addresses(tmp_path):
    data = tmp_path / "data"
    storage = AnnouncementStorage(db_path=data / "announcements/announcements.db")
    subscribers = SubscriberManager(data / "announcements/subscribers.db")
    subscribers.add_subscriber("email", "private-subscriber@example.org")
    storage.save_announcement(
        Announcement(
            title="Public update",
            body="The community contact page remains available.",
            status=AnnouncementStatus.PUBLISHED,
        )
    )

    archive_path = build_continuity_package(
        tmp_path / "missing.yaml", storage, data / "landing/continuity/guide.zip"
    )

    valid, messages = OfflineArchiveExporter.verify_archive(archive_path)
    assert valid, messages
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert {
            "index.html",
            "guide.html",
            "community.json",
            "announcements.json",
            "emergency-procedures.txt",
            "qr.svg",
            "qr.png",
            "manifest.json",
        } <= names
        contents = b"".join(archive.read(name) for name in names)
    assert b"Public update" in contents
    assert b"private-subscriber@example.org" not in contents


def test_drill_labels_every_public_renderer_without_changing_continuity_state(tmp_path):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    feed_dir = tmp_path / "feed"
    publisher = AnnouncementPublisher(
        storage=storage,
        adapters=[WebFeedAdapter(output_dir=feed_dir, storage=storage)],
        config_path=tmp_path / "missing.yaml",
        feed_dir=feed_dir,
    )
    service = ContinuityService(storage, publisher, tmp_path / "missing.yaml")
    states_before = service.store.list_channel_states()

    exercise = service.start_drill(
        "olga", "The primary social account is temporarily unavailable.", ["website"]
    )

    label = "[SIMULATION EXERCISE - NOT A REAL EMERGENCY]"
    announcement = storage.get_announcement(exercise["announcement_id"])
    assert announcement is not None
    assert announcement.title == "Community communication drill"
    assert label in (feed_dir / "announcements.html").read_text(encoding="utf-8")
    assert label in (feed_dir / "rss.xml").read_text(encoding="utf-8")
    assert label in (feed_dir / "atom.xml").read_text(encoding="utf-8")
    assert label in (feed_dir / "feed.json").read_text(encoding="utf-8")
    assert label in (tmp_path / "connect" / "index.html").read_text(encoding="utf-8")
    archive_path = OfflineArchiveExporter(storage=storage).export_archive(
        tmp_path / "drill-archive.zip"
    )
    guide_path = build_continuity_package(
        tmp_path / "missing.yaml", storage, tmp_path / "guide.zip"
    )
    for path in (archive_path, guide_path):
        with zipfile.ZipFile(path) as archive:
            contents = b"".join(archive.read(name) for name in archive.namelist())
        assert label.encode() in contents
    assert service.store.list_channel_states() == states_before
    assert service.store.active_notice() is None

    announcement.title = "An edited drill announcement"
    announcement.body = "The original label is not stored in these fields."
    storage.save_announcement(announcement)
    publisher.adapters[0].rebuild()
    assert label in (feed_dir / "announcements.html").read_text(encoding="utf-8")

    reviewed = service.review_drill(exercise["exercise_id"], "pat", True, "")
    assert reviewed["status"] == "verified"
    assert service.store.get_current() is None
