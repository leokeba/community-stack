"""Unit tests for announcement models and storage."""

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
    assert ann.status == AnnouncementStatus.PUBLISHED
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

    # Check failed query
    failed = storage.get_failed_deliveries()
    assert len(failed) == 1
    assert failed[0]["adapter_name"] == "matrix_room"
