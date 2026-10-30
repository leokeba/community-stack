"""Mirroring Matrix rooms into announcements on other channels."""

import re
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
        self.sent: list[str] = []
        self.syncs = 0

    def __call__(self, method, path, headers, payload):
        if path.endswith("/account/whoami"):
            return 200, {"user_id": "@bot:hs.example"}
        if "/join/" in path:
            alias = path.rsplit("/", 1)[-1]
            return 200, {"room_id": ANNOUNCEMENTS_ROOM if "announcements" in alias else ROOM}
        if path.endswith("/state/m.room.power_levels"):
            return 200, {
                "users": {"@mod:hs.example": 50, "@bot:hs.example": 100},
                "users_default": 0,
            }
        if path.startswith("/_matrix/client/v3/sync"):
            self.syncs += 1
            since = parse_qs(urlsplit(path).query).get("since")
            events = self.batches.pop(0) if self.batches else []
            if not since:
                events = [message("$old", "@mod:hs.example", "Old history\nshould be skipped")]
            return 200, {
                "next_batch": f"s{self.syncs}",
                "rooms": {"join": {ROOM: {"timeline": {"events": events}}}},
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
