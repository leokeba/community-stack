"""Unit tests for SubscriberManager, validation, and opt-out flows."""

import pytest
from community_stack.subscriptions.manager import SubscriberManager


def test_subscriber_add_and_masking(tmp_path):
    mgr = SubscriberManager(db_path=tmp_path / "subscribers.db")

    sub_email = mgr.add_subscriber("email", "organizer@example.org", topics=["alerts", "news"])
    assert sub_email.channel == "email"
    assert sub_email.status == "active"
    assert sub_email.mask_destination() == "o***@example.org"

    sub_sms = mgr.add_subscriber("sms", "+15551234567", topics=["emergency"])
    assert sub_sms.channel == "sms"
    assert sub_sms.mask_destination() == "+15****567"


def test_subscriber_validation_failures(tmp_path):
    mgr = SubscriberManager(db_path=tmp_path / "subscribers.db")

    with pytest.raises(ValueError, match="Invalid email address"):
        mgr.add_subscriber("email", "not-an-email")

    with pytest.raises(ValueError, match="Invalid phone number format"):
        mgr.add_subscriber("sms", "abc1234")

    with pytest.raises(ValueError, match="Unsupported subscription channel"):
        mgr.add_subscriber("carrier_pigeon", "bird_1")


def test_unsubscribe_immediate_opt_out(tmp_path):
    mgr = SubscriberManager(db_path=tmp_path / "subscribers.db")

    mgr.add_subscriber("email", "member@example.org", topics=["general"])
    active_before = mgr.get_active_recipients("email")
    assert "member@example.org" in active_before

    # Immediate opt-out
    unsub_ok = mgr.unsubscribe("email", "member@example.org")
    assert unsub_ok is True

    # Active recipients must immediately exclude unsubscribed contact
    active_after = mgr.get_active_recipients("email")
    assert "member@example.org" not in active_after

    # Listing active-only subscribers should not show them
    active_list = mgr.list_subscribers(channel="email", active_only=True)
    assert len(active_list) == 0

    # Listing all subscribers shows them as unsubscribed
    all_list = mgr.list_subscribers(channel="email", active_only=False)
    assert len(all_list) == 1
    assert all_list[0].status == "unsubscribed"
    assert all_list[0].unsubscribed_at is not None


def test_topic_filtering(tmp_path):
    mgr = SubscriberManager(db_path=tmp_path / "subscribers.db")

    mgr.add_subscriber("email", "emergency_only@example.org", topics=["emergency"])
    mgr.add_subscriber("email", "general_only@example.org", topics=["general"])
    mgr.add_subscriber("email", "all_topics@example.org", topics=[])

    emergency_recipients = mgr.get_active_recipients("email", topic="emergency")
    assert "emergency_only@example.org" in emergency_recipients
    assert "all_topics@example.org" in emergency_recipients
    assert "general_only@example.org" not in emergency_recipients
