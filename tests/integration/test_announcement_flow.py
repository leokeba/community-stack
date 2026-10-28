"""Integration tests for canonical public announcement workflow and adapter isolation."""

from pathlib import Path

import pytest
from community_stack.announcements.adapters.matrix_room import MatrixRoomAdapter
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.models import AnnouncementStatus
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage


def test_draft_preview_and_publish_use_canonical_storage(tmp_path):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    web_adapter = WebFeedAdapter(output_dir=tmp_path / "feed", storage=storage)
    calls = []

    class CountingAdapter(MatrixRoomAdapter):
        def publish(self, announcement):
            calls.append(announcement.id)
            return super().publish(announcement)

    matrix = CountingAdapter(
        transport=lambda method, path, headers, payload: (200, {"event_id": "$event"})
    )
    publisher = AnnouncementPublisher(storage=storage, adapters=[web_adapter, matrix])

    draft = publisher.create_draft("Draft notice", "A first version.")
    assert draft.status == AnnouncementStatus.DRAFT
    assert publisher.preview(draft.id).body == "A first version."
    assert calls == []
    assert not (tmp_path / "feed" / "announcements.html").exists()

    published, results = publisher.publish_draft(draft.id)
    assert published.status == AnnouncementStatus.PUBLISHED
    assert all(result.success for result in results.values())
    assert calls == [draft.id]
    assert (tmp_path / "announcements.db").is_file()
    assert not (tmp_path / "feed" / "announcements_store.json").exists()
    assert "Draft notice" in (tmp_path / "feed" / "announcements.html").read_text(encoding="utf-8")
    assert (
        published.canonical_url == f"https://community.example.org/feed/announcements/{draft.id}/"
    )


def test_failed_update_retry_repeats_update_operation_idempotently(tmp_path):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    calls = []

    def matrix_transport(method, path, headers, payload):
        calls.append((path, payload))
        if len(calls) == 2:
            return 503, {"error": "temporarily unavailable"}
        return 200, {"event_id": "$event"}

    matrix = MatrixRoomAdapter(transport=matrix_transport)
    publisher = AnnouncementPublisher(storage=storage, adapters=[matrix])
    announcement, _ = publisher.publish("Original", "Initial content")
    _, update_results = publisher.update(announcement.id, title="Updated")

    assert not update_results["matrix_room"].success
    delivery = storage.get_delivery_states(announcement.id)[0]
    assert delivery["operation"] == "update"
    assert delivery["idempotent"] == 1

    retry_results = publisher.retry_failed(announcement.id)
    assert retry_results[f"{announcement.id}:matrix_room"].success
    assert calls[1][0] == calls[2][0]
    assert "Updated" in calls[2][1]["body"]
    assert storage.get_delivery_states(announcement.id)[0]["attempts"] == 3


def test_withdrawal_removes_public_page_even_when_web_channel_is_disabled(tmp_path):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    web_feed = WebFeedAdapter(output_dir=tmp_path / "feed", storage=storage)
    publisher = AnnouncementPublisher(storage=storage, adapters=[web_feed])
    announcement, _ = publisher.publish("Public", "Visible content.")
    detail_page = tmp_path / "feed" / "announcements" / announcement.id / "index.html"
    assert detail_page.is_file()

    storage.set_channel_enabled("web_feed", False)
    publisher.withdraw(announcement.id)

    assert not detail_page.exists()
    delivery = storage.get_delivery_states(announcement.id)[0]
    assert delivery["operation"] == "withdraw"
    assert delivery["status"] == "withdrawn"


def test_default_adapters_require_explicit_matrix_configuration(tmp_path):
    config = tmp_path / "community-stack.yaml"
    example = Path("config/example.yaml").read_text(encoding="utf-8")
    matrix_enabled = """      matrix_room:
        enabled: true
        homeserver_url: "https://matrix.test"
        room_id: "!public:matrix.test"
        access_token_env: "TEST_MATRIX_ANNOUNCEMENTS_TOKEN"
        public_announcement_room: true
"""
    config.write_text(
        example.replace(
            "        enabled: true\n      matrix_room:\n        enabled: false\n",
            "        enabled: false\n" + matrix_enabled,
        ),
        encoding="utf-8",
    )
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")

    with pytest.raises(ValueError, match="TEST_MATRIX_ANNOUNCEMENTS_TOKEN is not set"):
        AnnouncementPublisher(
            storage=storage,
            config_path=config,
            feed_dir=tmp_path / "feed",
        )

    (tmp_path / ".env").write_text("TEST_MATRIX_ANNOUNCEMENTS_TOKEN=test-token\n", encoding="utf-8")
    publisher = AnnouncementPublisher(
        storage=storage,
        config_path=config,
        feed_dir=tmp_path / "feed",
    )
    assert [adapter.name for adapter in publisher.adapters] == ["web_feed", "matrix_room"]
    assert storage.get_channel_settings()["web_feed"] is False


def test_announcement_end_to_end_delivery(tmp_path):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    web_adapter = WebFeedAdapter(output_dir=tmp_path / "web_feed")

    matrix_calls = []

    def mock_matrix_transport(method, path, headers, payload):
        matrix_calls.append({"method": method, "path": path, "payload": payload})
        return 200, {"event_id": f"$event_{len(matrix_calls)}"}

    matrix_adapter = MatrixRoomAdapter(transport=mock_matrix_transport)

    publisher = AnnouncementPublisher(
        storage=storage,
        adapters=[web_adapter, matrix_adapter],
    )

    # 1. Publish new announcement
    ann, results = publisher.publish(
        title="Community Food Drive",
        body="Donations accepted at City Hall until Friday evening.",
        author="Mutual Aid Group",
        tags=["food", "community"],
    )

    assert results["web_feed"].success is True
    assert results["matrix_room"].success is True
    assert len(matrix_calls) == 1

    # Check persistence
    stored = storage.get_announcement(ann.id)
    assert stored is not None
    assert stored.title == "Community Food Drive"

    # Check web feed files
    html = (tmp_path / "web_feed" / "announcements.html").read_text(encoding="utf-8")
    assert "Community Food Drive" in html

    # 2. Update announcement
    updated_ann, update_results = publisher.update(
        announcement_id=ann.id,
        title="Community Food Drive - Extended Hours",
    )
    assert updated_ann.status == AnnouncementStatus.UPDATED
    assert update_results["web_feed"].success is True
    assert update_results["matrix_room"].success is True

    # 3. Withdraw announcement
    withdrawn_ann, with_results = publisher.withdraw(ann.id)
    assert withdrawn_ann.status == AnnouncementStatus.WITHDRAWN
    assert with_results["web_feed"].success is True

    # Web feed should no longer show withdrawn item
    html_after = (tmp_path / "web_feed" / "announcements.html").read_text(encoding="utf-8")
    assert "Community Food Drive - Extended Hours" not in html_after


def test_adapter_failure_isolation(tmp_path):
    """Failure in Matrix adapter must NEVER prevent publication to the Web/RSS feed."""
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    web_adapter = WebFeedAdapter(output_dir=tmp_path / "web_feed")

    # Failing Matrix transport
    def failing_matrix_transport(method, path, headers, payload):
        return 503, {"error": "Matrix homeserver down for maintenance"}

    matrix_adapter = MatrixRoomAdapter(transport=failing_matrix_transport)

    publisher = AnnouncementPublisher(
        storage=storage,
        adapters=[web_adapter, matrix_adapter],
    )

    # Publish announcement
    ann, results = publisher.publish(
        title="Critical Security Advisory",
        body="Please ensure your accounts use hardware 2FA keys.",
        author="Security Team",
    )

    # Web feed MUST succeed despite Matrix failure
    assert results["web_feed"].success is True
    assert results["matrix_room"].success is False
    assert "homeserver down" in (results["matrix_room"].error or "")

    # Announcement is still saved durably
    stored = storage.get_announcement(ann.id)
    assert stored is not None

    # Web archive is still generated and accessible
    html = (tmp_path / "web_feed" / "announcements.html").read_text(encoding="utf-8")
    assert "Critical Security Advisory" in html

    # Retry failed delivery after Matrix recovers
    def recovered_matrix_transport(method, path, headers, payload):
        return 200, {"event_id": "$recovered_event_123"}

    matrix_adapter._custom_transport = recovered_matrix_transport

    retry_results = publisher.retry_failed(ann.id)
    key = f"{ann.id}:matrix_room"
    assert key in retry_results
    assert retry_results[key].success is True
    states = {item["adapter_name"]: item for item in storage.get_delivery_states(ann.id)}
    assert states["web_feed"]["attempts"] == 1
    assert states["matrix_room"]["attempts"] == 2
