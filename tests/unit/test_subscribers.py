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


# CR2.2: consent records


def _old_schema_db(path):
    """A subscriber table as it was before consent was recorded."""
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE subscribers (
            id TEXT PRIMARY KEY, channel TEXT NOT NULL, destination TEXT NOT NULL,
            status TEXT NOT NULL, topics_json TEXT NOT NULL, created_at TEXT NOT NULL,
            unsubscribed_at TEXT, UNIQUE(channel, destination))"""
    )
    conn.execute(
        "INSERT INTO subscribers VALUES ('sub_old', 'email', 'old@example.org', 'active', '[]', "
        "'2026-01-01T00:00:00+00:00', NULL)"
    )
    conn.commit()
    conn.close()


def test_migration_adds_consent_columns_and_marks_old_rows_legacy(tmp_path):
    db = tmp_path / "subs.db"
    _old_schema_db(db)
    mgr = SubscriberManager(db_path=db)

    row = mgr.list_subscribers()[0]
    assert row.consent_source == "legacy"
    assert row.consent_at is None  # never invent a consent time
    assert row.consent_version is None
    assert row.created_at == "2026-01-01T00:00:00+00:00"


def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "subs.db"
    _old_schema_db(db)
    SubscriberManager(db_path=db)
    mgr = SubscriberManager(db_path=db)
    assert [s.consent_source for s in mgr.list_subscribers()] == ["legacy"]


def test_new_rows_without_consent_are_not_relabelled_on_restart(tmp_path):
    db = tmp_path / "subs.db"
    mgr = SubscriberManager(db_path=db)
    mgr.add_subscriber("email", "ann@example.org")
    SubscriberManager(db_path=db)  # restart
    row = mgr.list_subscribers()[0]
    assert row.consent_source is None


def test_consent_text_is_stored_once_and_never_changes(tmp_path):
    mgr = SubscriberManager(db_path=tmp_path / "subs.db")
    mgr.register_consent_text("2026-11", "I agree to receive announcements by email.")
    mgr.register_consent_text("2026-11", "I agree to receive announcements by email.")
    assert mgr.consent_text("2026-11") == "I agree to receive announcements by email."
    with pytest.raises(ValueError, match="different text"):
        mgr.register_consent_text("2026-11", "Changed after people agreed.")


def test_add_records_version_source_and_time(tmp_path):
    mgr = SubscriberManager(db_path=tmp_path / "subs.db")
    mgr.register_consent_text("2026-11", "I agree.")
    sub = mgr.add_subscriber(
        "email", "ann@example.org", consent_version="2026-11", consent_source="web_form"
    )
    stored = mgr.list_subscribers()[0]
    assert stored.consent_version == "2026-11"
    assert stored.consent_source == "web_form"
    assert stored.consent_at is not None
    assert sub.consent_version == "2026-11"


def test_consent_needs_a_known_version_and_a_source(tmp_path):
    mgr = SubscriberManager(db_path=tmp_path / "subs.db")
    with pytest.raises(ValueError, match="no stored text"):
        mgr.add_subscriber("email", "a@example.org", consent_version="2099", consent_source="x")
    mgr.register_consent_text("2026-11", "I agree.")
    with pytest.raises(ValueError, match="both a version and a source"):
        mgr.add_subscriber("email", "a@example.org", consent_version="2026-11")
    with pytest.raises(ValueError, match="1 to 40"):
        mgr.add_subscriber("email", "a@example.org", consent_version="2026-11", consent_source="")
    assert mgr.list_subscribers() == []


def test_resubscribing_keeps_the_original_consent_when_none_is_given(tmp_path):
    mgr = SubscriberManager(db_path=tmp_path / "subs.db")
    mgr.register_consent_text("2026-11", "I agree.")
    mgr.add_subscriber(
        "email", "ann@example.org", consent_version="2026-11", consent_source="web_form"
    )
    first = mgr.list_subscribers()[0]
    mgr.add_subscriber("email", "ann@example.org")
    kept = mgr.list_subscribers()[0]
    assert kept.consent_at == first.consent_at
    assert kept.consent_version == "2026-11"


def test_legacy_cli_import_is_marked_legacy(tmp_path, monkeypatch):
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
    mgr = SubscriberManager()
    assert mgr.list_subscribers()[0].consent_source == "legacy"
