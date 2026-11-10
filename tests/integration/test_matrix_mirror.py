"""Mirroring Matrix rooms into announcements on other channels."""

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.announcements.adapters.matrix_room import MatrixRoomAdapter
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.matrix_mirror import (
    MAX_PER_ROOM_PER_HOUR,
    MatrixMirror,
    split_message,
)
from community_stack.announcements.models import AnnouncementStatus
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage
from fastapi.testclient import TestClient

ROOM = "!news:hs.example"
ANNOUNCEMENTS_ROOM = "!announcements:hs.example"


class Homeserver:
    """A scripted Synapse: queued sync batches, power levels, and recorded sends."""

    def __init__(self):
        self.batches: list[list[dict]] = []
        self.message_pages: list[dict] = []
        self.sent: list[str] = []
        self.syncs = 0
        self.limited = False

    def __call__(self, method, path, headers, payload):
        if path.endswith("/account/whoami"):
            return 200, {"user_id": "@bot:hs.example"}
        if "/join/" in path:
            alias = path.rsplit("/", 1)[-1]
            return 200, {"room_id": ANNOUNCEMENTS_ROOM if "announcements" in alias else ROOM}
        if path.endswith("/state/m.room.create"):
            return 200, {
                "type": "m.room.create",
                "sender": "@creator:hs.example",
                "content": {"room_version": "12", "creator": "@creator:hs.example"},
            }
        if path.endswith("/state/m.room.power_levels"):
            return 200, {
                "users": {
                    "@mod:hs.example": 50,
                    "@other:hs.example": 50,
                    "@bot:hs.example": 100,
                },
                "users_default": 0,
            }
        if "/messages?" in path:
            return (200, self.message_pages.pop(0)) if self.message_pages else (404, {})
        if path.startswith("/_matrix/client/v3/sync"):
            self.syncs += 1
            since = parse_qs(urlsplit(path).query).get("since")
            events = self.batches.pop(0) if self.batches else []
            if not since:
                events = [message("$old", "@mod:hs.example", "Old history\nshould be skipped")]
            timeline = {"events": events}
            if self.limited:
                timeline.update(limited=True, prev_batch="page-0")
            return 200, {
                "next_batch": f"s{self.syncs}",
                "rooms": {"join": {ROOM: {"timeline": timeline}}},
            }
        if "/send/m.room.message/" in path:
            self.sent.append(payload["body"])
            return 200, {"event_id": "$sent"}
        return 404, {"error": "unknown"}


def message(event_id, sender, body):
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "content": {"msgtype": "m.text", "body": body},
    }


@pytest.fixture
def setup(tmp_path):
    storage = AnnouncementStorage(tmp_path / "announcements.db")
    homeserver = Homeserver()
    matrix = MatrixRoomAdapter(room_id=ANNOUNCEMENTS_ROOM, access_token="t", transport=homeserver)
    feed = WebFeedAdapter(output_dir=tmp_path / "feed", storage=storage)
    publisher = AnnouncementPublisher(storage=storage, adapters=[feed, matrix])
    review = {"on": False}
    mirror = MatrixMirror(storage, publisher, matrix, lambda: review["on"])
    mirror.add_room("#news:hs.example", "publish", 50, ["web_feed"], "olga")
    mirror.sync_once()  # first sync only records where "now" is
    return mirror, homeserver, storage, review, tmp_path


def test_split_message_uses_the_first_line_as_title():
    assert split_message("## Water cut\nFrom 9 to 12 tomorrow.") == (
        "Water cut",
        "From 9 to 12 tomorrow.",
    )
    title, body = split_message("x" * 300)
    assert len(title) == 120 and body == "x" * 300


def test_moderator_messages_publish_to_the_chosen_channels_only(setup):
    mirror, homeserver, storage, _, tmp_path = setup
    homeserver.batches.append(
        [
            message("$1", "@mod:hs.example", "Water cut\nFrom 9 to 12 tomorrow."),
            message("$2", "@visitor:hs.example", "Spam\nBuy now"),
            message("$3", "@bot:hs.example", "Our own post\nignored"),
        ]
    )

    assert mirror.sync_once() == 1

    [announcement] = storage.list_announcements()
    assert announcement.title == "Water cut"
    assert announcement.status == AnnouncementStatus.PUBLISHED
    assert announcement.channels == ["web_feed"]
    assert "Water cut" in (tmp_path / "feed" / "announcements.html").read_text()
    assert homeserver.sent == []
    assert "Old history" not in (tmp_path / "feed" / "announcements.html").read_text()


def test_room_version_12_creator_messages_are_mirrored(setup):
    mirror, homeserver, storage, _, _ = setup
    homeserver.batches.append([message("$creator", "@creator:hs.example", "Notice\nDetails")])

    assert mirror.sync_once() == 1
    [announcement] = storage.list_announcements()
    assert announcement.title == "Notice"
    assert announcement.status == AnnouncementStatus.PUBLISHED


def test_edits_update_and_deletions_withdraw(setup):
    mirror, homeserver, storage, _, _ = setup
    homeserver.batches.append([message("$1", "@mod:hs.example", "Water cut\nFrom 9 to 12.")])
    mirror.sync_once()
    edit = {
        "type": "m.room.message",
        "event_id": "$1-edit",
        "sender": "@mod:hs.example",
        "content": {
            "msgtype": "m.text",
            "body": "* Water cut\nFrom 10 to 12.",
            "m.new_content": {"msgtype": "m.text", "body": "Water cut\nFrom 10 to 12."},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$1"},
        },
    }
    homeserver.batches.append([edit])
    mirror.sync_once()
    [announcement] = storage.list_announcements()
    assert announcement.body == "From 10 to 12."
    assert announcement.status == AnnouncementStatus.UPDATED

    homeserver.batches.append(
        [
            {
                "type": "m.room.redaction",
                "event_id": "$r",
                "sender": "@mod:hs.example",
                "redacts": "$1",
            }
        ]
    )
    mirror.sync_once()
    assert storage.list_announcements()[0].status == AnnouncementStatus.WITHDRAWN


def test_only_the_original_sender_can_edit_a_mirrored_message(setup):
    mirror, homeserver, storage, _, _ = setup
    homeserver.batches.append([message("$1", "@mod:hs.example", "Water cut\nFrom 9 to 12.")])
    mirror.sync_once()
    homeserver.batches.append(
        [
            {
                "type": "m.room.message",
                "event_id": "$1-forged-edit",
                "sender": "@other:hs.example",
                "content": {
                    "msgtype": "m.text",
                    "m.new_content": {"msgtype": "m.text", "body": "Water cut\nFrom 10 to 12."},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$1"},
                },
            }
        ]
    )

    assert mirror.sync_once() == 0
    [announcement] = storage.list_announcements()
    assert announcement.body == "From 9 to 12."
    assert announcement.status == AnnouncementStatus.PUBLISHED


def test_reviewed_edits_return_approved_announcements_to_review(setup):
    mirror, homeserver, storage, review, _ = setup
    review["on"] = True
    homeserver.batches.append([message("$1", "@mod:hs.example", "Water cut\nFrom 9 to 12.")])
    mirror.sync_once()
    [announcement] = storage.list_announcements()
    mirror.publisher.set_review_status(
        announcement.id, (AnnouncementStatus.IN_REVIEW,), AnnouncementStatus.APPROVED
    )
    mirror.publisher.schedule(announcement.id, datetime.now(timezone.utc) + timedelta(days=1))
    homeserver.batches.append(
        [
            {
                "type": "m.room.message",
                "event_id": "$1-edit",
                "sender": "@mod:hs.example",
                "content": {
                    "msgtype": "m.text",
                    "m.new_content": {"msgtype": "m.text", "body": "Water cut\nFrom 10 to 12."},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$1"},
                },
            }
        ]
    )

    assert mirror.sync_once() == 1
    announcement = storage.get_announcement(announcement.id)
    assert announcement.body == "From 10 to 12."
    assert announcement.status == AnnouncementStatus.IN_REVIEW
    assert announcement.scheduled_at is None


def test_reviewed_edits_do_not_change_already_published_announcements(setup):
    mirror, homeserver, storage, review, _ = setup
    homeserver.batches.append([message("$1", "@mod:hs.example", "Water cut\nFrom 9 to 12.")])
    mirror.sync_once()
    [announcement] = storage.list_announcements()
    review["on"] = True
    homeserver.batches.append(
        [
            {
                "type": "m.room.message",
                "event_id": "$1-edit",
                "sender": "@mod:hs.example",
                "content": {
                    "msgtype": "m.text",
                    "m.new_content": {"msgtype": "m.text", "body": "Water cut\nFrom 10 to 12."},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$1"},
                },
            }
        ]
    )

    assert mirror.sync_once() == 0
    announcement = storage.get_announcement(announcement.id)
    assert announcement.body == "From 9 to 12."
    assert announcement.status == AnnouncementStatus.PUBLISHED


def test_review_mode_creates_a_submitted_draft(setup):
    mirror, homeserver, storage, review, _ = setup
    review["on"] = True
    homeserver.batches.append([message("$1", "@mod:hs.example", "Meeting\nThursday")])
    mirror.sync_once()
    [announcement] = storage.list_announcements()
    assert announcement.status == AnnouncementStatus.IN_REVIEW
    assert storage.last_submitter(announcement.id) == "matrix:@mod:hs.example"


def test_each_room_is_rate_limited(setup):
    mirror, homeserver, storage, _, _ = setup
    homeserver.batches.append(
        [
            message(f"${n}", "@mod:hs.example", f"Alert {n}\nbody")
            for n in range(MAX_PER_ROOM_PER_HOUR + 3)
        ]
    )
    mirror.sync_once()
    assert len(storage.list_announcements(limit=None)) == MAX_PER_ROOM_PER_HOUR


def test_limited_timeline_is_backfilled_before_advancing_sync(setup):
    mirror, homeserver, storage, _, _ = setup
    homeserver.limited = True
    homeserver.message_pages.extend(
        [
            {"chunk": [message("$2", "@mod:hs.example", "Two\nbody")], "end": "page-1"},
            {
                "chunk": [
                    message("$1", "@mod:hs.example", "One\nbody"),
                    message("$old", "@mod:hs.example", "Old history\nshould be skipped"),
                    message("$0", "@mod:hs.example", "Old\nbody"),
                ],
                "end": "page-2",
            },
        ]
    )
    homeserver.batches.append(
        [
            message("$3", "@mod:hs.example", "Three\nbody"),
            message("$4", "@mod:hs.example", "Four\nbody"),
        ]
    )

    assert mirror.sync_once() == 4
    announcements = storage.list_announcements(limit=None)
    assert [announcement.title for announcement in announcements] == [
        "Four",
        "Three",
        "Two",
        "One",
    ]
    assert storage.get_setting("mirror_sync_token", "") == "s2"


def test_failed_timeline_backfill_does_not_advance_the_sync_token(setup):
    mirror, homeserver, _, _, _ = setup
    homeserver.limited = True
    homeserver.batches.append([message("$new", "@mod:hs.example", "New\nbody")])

    assert mirror.sync_once() == 0
    assert mirror.storage.get_setting("mirror_sync_token", "") == "s1"


def test_existing_mirror_event_table_is_migrated_with_sender(tmp_path):
    storage = AnnouncementStorage(tmp_path / "announcements.db")
    with storage._get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE mirror_events (
                event_id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                announcement_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
    matrix = MatrixRoomAdapter(room_id=ANNOUNCEMENTS_ROOM, access_token="t", transport=Homeserver())
    feed = WebFeedAdapter(output_dir=tmp_path / "feed", storage=storage)
    publisher = AnnouncementPublisher(storage=storage, adapters=[feed])

    MatrixMirror(storage, publisher, matrix, lambda: False)

    with storage._get_connection() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(mirror_events)")}
    assert "sender" in columns


def test_the_announcements_room_itself_cannot_be_mirrored(setup):
    mirror, _, _, _, _ = setup
    with pytest.raises(ValueError, match="announcements room itself"):
        mirror.add_room("#announcements:hs.example", "publish", 50, ["web_feed"], None)


def test_console_adds_and_removes_mirrored_rooms(tmp_path):
    AdminStore(tmp_path / "announcements.db").create_admin("olga", "a-long-safe-test-password")
    app = create_app(
        db_path=tmp_path / "announcements.db",
        feed_dir=tmp_path / "feed",
        config_path=tmp_path / "missing.yaml",
        setup_token="unused",
        run_worker=False,
    )
    client = TestClient(app, base_url="https://community.test", follow_redirects=False)
    page = client.get("/admin/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    client.post(
        "/admin/login",
        data={"csrf_token": token, "username": "olga", "password": "a-long-safe-test-password"},
    )
    assert (
        "Mirroring needs a Matrix announcements connection" in client.get("/admin/mirroring").text
    )

    app.state.publisher.adapters.append(
        MatrixRoomAdapter(room_id=ANNOUNCEMENTS_ROOM, access_token="t", transport=Homeserver())
    )
    page = client.get("/admin/mirroring")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    none_chosen = client.post(
        "/admin/mirroring/add", data={"csrf_token": token, "room": "#news:hs.example"}
    )
    assert none_chosen.status_code == 400
    added = client.post(
        "/admin/mirroring/add",
        data={
            "csrf_token": token,
            "room": "#news:hs.example",
            "who": "moderators",
            "mode": "review",
            "channel": ["web_feed"],
        },
    )
    assert added.status_code == 303
    assert "#news:hs.example" in client.get("/admin/mirroring").text
    client.post("/admin/mirroring/remove", data={"csrf_token": token, "room_id": ROOM})
    assert "Stop mirroring" not in client.get("/admin/mirroring").text
