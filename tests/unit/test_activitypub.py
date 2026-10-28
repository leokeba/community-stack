"""Unit tests for ActivityPub / ActivityStreams 2.0 adapter."""

from community_stack.announcements.adapters.activitypub import ActivityPubAdapter
from community_stack.announcements.models import Announcement


def test_build_create_activity():
    adapter = ActivityPubAdapter(
        actor_id="https://fediverse.example.org/actor",
        outbox_url="https://fediverse.example.org/outbox",
    )

    ann = Announcement(
        title="Public Rally Scheduled",
        body="Join us at the town square this Friday.",
        author="Community Action",
        tags=["action", "rally"],
    )

    activity = adapter.build_create_activity(ann)
    assert activity["type"] == "Create"
    assert activity["actor"] == "https://fediverse.example.org/actor"
    assert "object" in activity
    note = activity["object"]
    assert note["type"] == "Note"
    assert "Public Rally Scheduled" in note["content"]
    assert "Community Action" in note["content"]
    assert len(note["tag"]) == 2
    assert note["tag"][0]["name"] == "#action"


def test_activitypub_publish_success():
    posted_activities = []

    def mock_transport(method, url, headers, data):
        posted_activities.append({"url": url, "data": data})
        return 201, {"id": data["id"]}

    adapter = ActivityPubAdapter(
        outbox_url="https://fediverse.example.org/outbox",
        transport=mock_transport,
    )

    ann = Announcement(title="Fediverse Announcement", body="Hello Fediverse!")
    res = adapter.publish(ann)

    assert res.success is True
    assert res.external_id is not None
    assert len(posted_activities) == 1
    assert posted_activities[0]["url"] == "https://fediverse.example.org/outbox"


def test_activitypub_withdrawal():
    deleted_calls = []

    def mock_transport(method, url, headers, data):
        deleted_calls.append(data)
        return 200, {"status": "deleted"}

    adapter = ActivityPubAdapter(transport=mock_transport)
    res = adapter.withdraw("ann_test_123")

    assert res.success is True
    assert len(deleted_calls) == 1
    assert deleted_calls[0]["type"] == "Delete"
    assert "ann_test_123" in deleted_calls[0]["object"]


def test_activitypub_failure_retryable():
    def mock_transport(method, url, headers, data):
        return 503, {"error": "Fediverse instance overloaded"}

    adapter = ActivityPubAdapter(transport=mock_transport)
    ann = Announcement(title="Overload Test", body="Body")
    res = adapter.publish(ann)

    assert res.success is False
    assert res.retryable is True
    assert "overloaded" in (res.error or "")
