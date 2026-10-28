"""Unit tests for announcement models and storage."""

import sqlite3

import pytest
from community_stack.announcements.models import (
    Announcement,
    AnnouncementStatus,
    ModelValidationError,
)
from community_stack.announcements.storage import AnnouncementStorage


def test_announcement_model_valid():
    ann = Announcement(
        title="Emergency Weather Alert",
        body="Heavy rainfall expected. Safe locations available at Community Center.",
        author="Coordination Team",
        tags=["alert", "weather"],
    )
    assert ann.title == "Emergency Weather Alert"
    assert ann.status == AnnouncementStatus.DRAFT
    assert ann.id.startswith("ann_")


def test_announcement_model_validation_failures():
    # Empty title
    with pytest.raises(ModelValidationError, match="title cannot be empty"):
        Announcement(title="", body="Valid body")

    # Excessive title length
    with pytest.raises(ModelValidationError, match="title exceeds maximum length"):
        Announcement(title="A" * 201, body="Valid body")

    # Empty body
    with pytest.raises(ModelValidationError, match="body cannot be empty"):
        Announcement(title="Valid title", body="")

    # Invalid language code
    with pytest.raises(ModelValidationError, match="Language code"):
        Announcement(title="Valid", body="Valid", language="12345")

    # Invalid canonical URL
    with pytest.raises(ModelValidationError, match="Canonical URL must start with"):
        Announcement(title="Valid", body="Valid", canonical_url="ftp://invalid.url")

    with pytest.raises(ModelValidationError, match="must be public"):
        Announcement(title="Private", body="Not for publication", audience="private")


def test_announcement_storage_crud(tmp_path):
    db_file = tmp_path / "announcements.db"
    storage = AnnouncementStorage(db_path=db_file)

    ann = Announcement(
        title="First Public Update",
        body="Welcome to the independent announcement feed.",
        author="Admin",
        tags=["welcome"],
    )
    storage.save_announcement(ann)

    # Read back
    fetched = storage.get_announcement(ann.id)
    assert fetched is not None
    assert fetched.id == ann.id
    assert fetched.title == "First Public Update"
    assert fetched.tags == ["welcome"]

    # List
    all_anns = storage.list_announcements()
    assert len(all_anns) == 1
    assert all_anns[0].id == ann.id

    # Update
    ann.title = "Updated Public Update"
    ann.status = AnnouncementStatus.UPDATED
    storage.save_announcement(ann)

    updated_fetch = storage.get_announcement(ann.id)
    assert updated_fetch is not None
    assert updated_fetch.title == "Updated Public Update"
    assert updated_fetch.status == AnnouncementStatus.UPDATED
    assert updated_fetch.updated_at == ann.updated_at


def test_storage_migrates_legacy_timestamp_and_delivery_state(tmp_path):
    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_file)
    conn.executescript("""
        CREATE TABLE announcements (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            published_at TEXT NOT NULL,
            language TEXT NOT NULL,
            status TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            expires_at TEXT,
            audience TEXT NOT NULL,
            author TEXT,
            tags_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE delivery_states (
            announcement_id TEXT NOT NULL,
            adapter_name TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT NOT NULL,
            last_error TEXT,
            external_id TEXT,
            PRIMARY KEY (announcement_id, adapter_name)
        );
        INSERT INTO announcements VALUES (
            'ann_legacy_1', 'Legacy notice', 'Body', '2024-01-02T00:00:00+00:00',
            'en', 'published', '', NULL, 'public', NULL, '[]',
            '2024-01-01T00:00:00+00:00'
        );
        INSERT INTO delivery_states VALUES (
            'ann_legacy_1', 'matrix_room', 'failed', 4,
            '2024-01-03T00:00:00+00:00', 'Old failure', NULL
        );
    """)
    conn.commit()
    conn.close()

    storage = AnnouncementStorage(db_path=db_file)
    announcement = storage.get_announcement("ann_legacy_1")
    assert announcement is not None
    assert announcement.updated_at == "2024-01-01T00:00:00+00:00"

    legacy_attempt = storage.get_delivery_attempts("ann_legacy_1")
    assert len(legacy_attempt) == 1
    assert legacy_attempt[0]["attempt_number"] == 4
    assert legacy_attempt[0]["legacy_snapshot"] == 1
    assert legacy_attempt[0]["error"] == "Old failure"

    storage.record_delivery(
        "ann_legacy_1",
        "matrix_room",
        status="delivered",
        operation="update",
        idempotent=True,
    )
    history = storage.get_delivery_attempts("ann_legacy_1")
    assert [attempt["attempt_number"] for attempt in history] == [4, 5]
    assert [attempt["status"] for attempt in history] == ["failed", "delivered"]
    assert history[0]["error"] == "Old failure"
    assert history[1]["legacy_snapshot"] == 0
    current = storage.get_delivery_states("ann_legacy_1")[0]
    assert current["attempts"] == 5
    assert current["status"] == "delivered"


def test_storage_delivery_state_tracking(tmp_path):
    db_file = tmp_path / "announcements.db"
    storage = AnnouncementStorage(db_path=db_file)

    ann = Announcement(title="Testing Deliveries", body="Delivery body")
    storage.save_announcement(ann)

    # Record successful web delivery
    storage.record_delivery(
        announcement_id=ann.id,
        adapter_name="web_feed",
        status="delivered",
        external_id="https://example.org/feed#ann1",
    )

    # Record failed matrix delivery
    storage.record_delivery(
        announcement_id=ann.id,
        adapter_name="matrix_room",
        status="failed",
        error="Connection refused",
    )

    states = storage.get_delivery_states(ann.id)
    assert len(states) == 2
    state_map = {s["adapter_name"]: s for s in states}
    assert state_map["web_feed"]["status"] == "delivered"
    assert state_map["matrix_room"]["status"] == "failed"
    assert state_map["matrix_room"]["attempts"] == 1
    attempt_history = storage.get_delivery_attempts(ann.id, "matrix_room")
    assert len(attempt_history) == 1
    assert attempt_history[0]["error"] == "Connection refused"

    # Check failed query
    failed = storage.get_failed_deliveries()
    assert len(failed) == 1
    assert failed[0]["adapter_name"] == "matrix_room"
