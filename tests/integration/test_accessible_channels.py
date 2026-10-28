"""Integration tests for multi-channel accessible distribution (Web, Matrix, Fediverse, Email)."""

from community_stack.announcements.adapters.activitypub import ActivityPubAdapter
from community_stack.announcements.adapters.email_newsletter import EmailNewsletterAdapter
from community_stack.announcements.adapters.matrix_room import MatrixRoomAdapter
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.subscriptions.manager import SubscriberManager


def test_multi_channel_accessible_broadcast(tmp_path):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    sub_mgr = SubscriberManager(db_path=tmp_path / "subscribers.db")

    # Add active email subscribers
    sub_mgr.add_subscriber("email", "resident1@example.org")
    sub_mgr.add_subscriber("email", "resident2@example.org")

    email_dispatched = []

    def mock_email_transport(msg, recipients):
        email_dispatched.append({"subject": msg["Subject"], "recipients": list(recipients)})
        return True, None

    activitypub_dispatched = []

    def mock_ap_transport(method, url, headers, data):
        activitypub_dispatched.append(data)
        return 201, {"id": data["id"]}

    web_adapter = WebFeedAdapter(output_dir=tmp_path / "web_feed")
    matrix_adapter = MatrixRoomAdapter(
        transport=lambda method, path, headers, payload: (200, {"event_id": "$m_event_1"}),
    )
    ap_adapter = ActivityPubAdapter(transport=mock_ap_transport)
    email_adapter = EmailNewsletterAdapter(
        subscriber_manager=sub_mgr,
        transport=mock_email_transport,
    )

    publisher = AnnouncementPublisher(
        storage=storage,
        adapters=[web_adapter, matrix_adapter, ap_adapter, email_adapter],
    )

    # 1. Publish multi-channel announcement
    _ann, results = publisher.publish(
        title="Public Transportation Route Update",
        body="Bus lines 12 and 14 will operate on holiday schedule tomorrow.",
        author="Transit Coordinator",
        tags=["transit", "schedule"],
    )

    # All channels must succeed
    assert results["web_feed"].success is True
    assert results["matrix_room"].success is True
    assert results["activitypub"].success is True
    assert results["email"].success is True

    # Verify Email recipients
    assert len(email_dispatched) == 1
    assert "resident1@example.org" in email_dispatched[0]["recipients"]
    assert "resident2@example.org" in email_dispatched[0]["recipients"]

    # Verify ActivityPub
    assert len(activitypub_dispatched) == 1
    assert "Public Transportation Route Update" in activitypub_dispatched[0]["object"]["content"]

    # 2. Test opt-out flow: resident1 unsubscribes
    sub_mgr.unsubscribe("email", "resident1@example.org")

    # Second announcement: only resident2 receives email
    _ann2, results2 = publisher.publish(
        title="Emergency Route Clearance",
        body="Main Street has reopened to public traffic.",
    )
    assert results2["email"].success is True
    assert len(email_dispatched) == 2
    assert "resident1@example.org" not in email_dispatched[1]["recipients"]
    assert "resident2@example.org" in email_dispatched[1]["recipients"]


def test_activitypub_failure_does_not_block_email_or_web(tmp_path):
    """Network failure on Fediverse must never block Email or Web publishing."""
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    sub_mgr = SubscriberManager(db_path=tmp_path / "subscribers.db")
    sub_mgr.add_subscriber("email", "alert_subscriber@example.org")

    email_sent = []

    def mock_email_transport(msg, recipients):
        email_sent.append(list(recipients))
        return True, None

    # ActivityPub fails
    def failing_ap_transport(method, url, headers, data):
        return 503, {"error": "Federation remote server offline"}

    web_adapter = WebFeedAdapter(output_dir=tmp_path / "web_feed")
    ap_adapter = ActivityPubAdapter(transport=failing_ap_transport)
    email_adapter = EmailNewsletterAdapter(
        subscriber_manager=sub_mgr,
        transport=mock_email_transport,
    )

    publisher = AnnouncementPublisher(
        storage=storage,
        adapters=[web_adapter, ap_adapter, email_adapter],
    )

    _ann, results = publisher.publish(
        title="Emergency Flood Warning",
        body="Move to high ground immediately.",
    )

    # Web and Email must succeed despite ActivityPub failure
    assert results["web_feed"].success is True
    assert results["email"].success is True
    assert results["activitypub"].success is False
    assert len(email_sent) == 1
