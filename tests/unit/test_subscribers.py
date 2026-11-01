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


# CR2.1: one subscriber list for the CLI, publisher and sign-up pages.


def _legacy_db(path, rows):
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE subscribers (
            id TEXT PRIMARY KEY, channel TEXT NOT NULL, destination TEXT NOT NULL,
            status TEXT NOT NULL, topics_json TEXT NOT NULL, created_at TEXT NOT NULL,
            unsubscribed_at TEXT, UNIQUE(channel, destination))"""
    )
    conn.executemany("INSERT INTO subscribers VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_default_path_is_the_announcements_list(tmp_path, monkeypatch):
    from community_stack.subscriptions.manager import DEFAULT_DB_PATH

    monkeypatch.chdir(tmp_path)
    assert DEFAULT_DB_PATH == "data/announcements/subscribers.db"
    SubscriberManager()
    assert (tmp_path / "data/announcements/subscribers.db").is_file()


def test_legacy_cli_rows_are_imported_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _legacy_db(
        tmp_path / "data/subscriptions/subscribers.db",
        [
            (
                "sub_old_1",
                "email",
                "ann@example.org",
                "active",
                "[]",
                "2026-01-01T00:00:00+00:00",
                None,
            ),
            ("sub_old_2", "sms", "+33612345678", "active", "[]", "2026-01-02T00:00:00+00:00", None),
        ],
    )
    first = SubscriberManager()
    assert {s.destination for s in first.list_subscribers()} == {"ann@example.org", "+33612345678"}

    # A second start imports nothing new, and nothing is duplicated.
    again = SubscriberManager()
    assert len(again.list_subscribers()) == 2


def test_existing_rows_win_over_legacy_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    current = SubscriberManager()
    current.add_subscriber("email", "ann@example.org", topics=["events"])
    current.unsubscribe("email", "ann@example.org")
    _legacy_db(
        tmp_path / "data/subscriptions/subscribers.db",
        [
            (
                "sub_old_1",
                "email",
                "ann@example.org",
                "active",
                "[]",
                "2026-01-01T00:00:00+00:00",
                None,
            )
        ],
    )

    merged = SubscriberManager()
    row = next(
        s for s in merged.list_subscribers(active_only=False) if s.destination == "ann@example.org"
    )
    assert row.status == "unsubscribed"
    assert row.topics == ["events"]


def test_custom_paths_never_import_legacy_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _legacy_db(
        tmp_path / "data/subscriptions/subscribers.db",
        [
            (
                "sub_old_1",
                "email",
                "ann@example.org",
                "active",
                "[]",
                "2026-01-01T00:00:00+00:00",
                None,
            )
        ],
    )
    isolated = SubscriberManager(db_path=tmp_path / "isolated.db")
    assert isolated.list_subscribers() == []


def test_legacy_file_is_left_in_place(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / "data/subscriptions/subscribers.db"
    _legacy_db(
        legacy,
        [
            (
                "sub_old_1",
                "email",
                "ann@example.org",
                "active",
                "[]",
                "2026-01-01T00:00:00+00:00",
                None,
            )
        ],
    )
    SubscriberManager()
    assert legacy.is_file()
