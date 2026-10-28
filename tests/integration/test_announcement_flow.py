"""Integration tests for canonical public announcement workflow and adapter isolation."""

from community_stack.announcements.adapters.matrix_room import MatrixRoomAdapter
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.models import AnnouncementStatus
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage


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
