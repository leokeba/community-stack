"""Announcement images: processing, the public feed, channel uploads, and the console."""

import io
import json
import re
import sqlite3

import pytest
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.announcements import media
from community_stack.announcements.adapters import http
from community_stack.announcements.adapters.messaging import (
    DiscordWebhookAdapter,
    SlackWebhookAdapter,
)
from community_stack.announcements.adapters.social import BlueskyAdapter, MastodonAdapter
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage
from fastapi.testclient import TestClient
from PIL import Image

PASSWORD = "a-long-safe-test-password"
ALT = "Volunteers sorting food donations on tables"


def jpeg_with_location(size=(2400, 1200), orientation: int | None = None) -> bytes:
    image = Image.new("RGB", size, (200, 120, 40))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"
    exif[0x8825] = {2: (48.0, 51.0, 24.0)}
    if orientation:
        exif[0x0112] = orientation
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif.tobytes())
    return buffer.getvalue()


def test_processing_strips_metadata_applies_rotation_and_resizes():
    processed = media.process_image(jpeg_with_location(orientation=6))

    with Image.open(io.BytesIO(processed.content)) as result:
        assert not result.getexif()
        assert "exif" not in result.info
        # Orientation 6 means the camera was rotated: the stored image is upright.
        assert (result.width, result.height) == (800, 1600)
    assert processed.content_type == "image/jpeg"


def test_transparent_images_stay_png():
    buffer = io.BytesIO()
    Image.new("RGBA", (40, 40), (0, 0, 0, 0)).save(buffer, "PNG")
    assert media.process_image(buffer.getvalue()).content_type == "image/png"


@pytest.mark.parametrize(
    ("data", "message"),
    [(b"<?php echo 1;", "not an image"), (b"x" * (11 * 1024 * 1024), "10 MB")],
)
def test_unusable_uploads_are_refused(data, message):
    with pytest.raises(media.MediaError, match=message):
        media.process_image(data)


def test_decompression_bombs_are_refused(monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(media.MediaError, match="too large in pixels"):
        media.process_image(jpeg_with_location(size=(200, 200)))


def test_alt_text_is_required():
    with pytest.raises(media.MediaError, match="Describe the image"):
        media.validate_alt("   ")


@pytest.fixture
def setup(tmp_path):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    feed = WebFeedAdapter(output_dir=tmp_path / "feed", storage=storage)
    publisher = AnnouncementPublisher(storage=storage, adapters=[feed])
    draft = publisher.create_draft("Food drive", "Thank you, everyone!")
    image = storage.save_image(draft.id, media.process_image(jpeg_with_location()), ALT, "olga")
    return storage, publisher, draft, image, tmp_path


def test_images_are_public_only_while_the_announcement_is(setup):
    _, publisher, draft, image, tmp_path = setup
    public = tmp_path / "feed" / "media" / image.filename
    assert not public.exists()

    publisher.publish_draft(draft.id)
    assert public.read_bytes() == image.read_bytes()
    page = (tmp_path / "feed" / "announcements" / draft.id / "index.html").read_text()
    assert f'alt="{ALT}"' in page
    assert 'property="og:image"' in page
    feed = json.loads((tmp_path / "feed" / "feed.json").read_text())
    assert feed["items"][0]["image"].endswith(image.filename)

    publisher.withdraw(draft.id)
    assert not public.exists()


def test_replacing_an_image_deletes_the_old_file(setup):
    storage, _, draft, image, _ = setup
    replacement = storage.save_image(draft.id, media.process_image(jpeg_with_location()), ALT, None)
    assert not image.path.exists()
    assert storage.get_announcement(draft.id).image.id == replacement.id


def test_channel_image_override_persists_dispatches_and_cleans_up(setup):
    storage, publisher, draft, shared, tmp_path = setup
    override = storage.save_channel_image(
        draft.id, "mastodon", media.process_image(jpeg_with_location()), ALT, "olga"
    )
    loaded = storage.get_announcement(draft.id)

    assert loaded.image.id == shared.id
    assert loaded.channel_media["mastodon"].id == override.id
    assert publisher.channel_variant(loaded, "mastodon").image.id == override.id
    assert publisher.channel_variant(loaded, "bluesky").image.id == shared.id

    publisher.publish_draft(draft.id)
    public_override = tmp_path / "feed" / "media" / override.filename
    assert public_override.read_bytes() == override.read_bytes()

    replacement = storage.save_channel_image(
        draft.id, "mastodon", media.process_image(jpeg_with_location()), ALT, "olga"
    )
    assert not override.path.exists()
    assert storage.remove_channel_image(draft.id, "mastodon")
    assert not replacement.path.exists()
    assert storage.get_announcement(draft.id).channel_media == {}
    publisher.withdraw(draft.id)
    assert not public_override.exists()


def test_social_adapter_receives_channel_image_instead_of_shared_image(tmp_path):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    feed = WebFeedAdapter(output_dir=tmp_path / "feed", storage=storage)
    shared = media.process_image(jpeg_with_location())
    specific = media.process_image(jpeg_with_location(size=(640, 480)))
    calls = []
    replies = [(200, {"id": "media-1"}), (200, {"id": "status-1"})]

    def transport(method, url, headers, payload):
        calls.append((method, url, headers, payload))
        return replies.pop(0)

    mastodon = MastodonAdapter("https://social.example", "token-12345678", transport=transport)
    publisher = AnnouncementPublisher(
        storage=storage,
        adapters=[feed, mastodon],
        config_path=tmp_path / "missing.yaml",
        feed_dir=tmp_path / "feed",
    )
    draft = publisher.create_draft("Food drive", "Bring supplies.")
    shared_image = storage.save_image(draft.id, shared, ALT, None)
    specific_image = storage.save_channel_image(draft.id, "mastodon", specific, ALT, None)

    publisher.publish_draft(draft.id)

    upload = calls[0][3]
    assert upload.files[0][3] == specific_image.read_bytes()
    assert upload.files[0][3] != shared_image.read_bytes()
    assert calls[1][3]["media_ids"] == ["media-1"]


def test_existing_database_migrates_channel_media_column(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("""
            CREATE TABLE announcements (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
                published_at TEXT NOT NULL, language TEXT NOT NULL, status TEXT NOT NULL,
                canonical_url TEXT NOT NULL, expires_at TEXT, audience TEXT NOT NULL,
                author TEXT, tags_json TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, scheduled_at TEXT,
                channel_text_json TEXT NOT NULL DEFAULT '{}', image_id TEXT,
                channels_json TEXT NOT NULL DEFAULT '[]'
            )
        """)
        connection.execute(
            """
            INSERT INTO announcements (
                id, title, body, published_at, language, status, canonical_url, audience,
                tags_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ann_20260101_000000_abcd1234",
                "Legacy notice",
                "Existing content.",
                "2026-01-01T00:00:00+00:00",
                "en",
                "draft",
                "",
                "public",
                "[]",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    storage = AnnouncementStorage(db_path=db_path)

    assert storage.get_announcement("ann_20260101_000000_abcd1234").channel_media_ids == {}


def test_bytes_within_shrinks_for_channel_limits(setup):
    _, _, _, image, _ = setup
    content, content_type = image.bytes_within(20_000)
    assert len(content) <= 20_000
    assert content_type == "image/jpeg"


class Recorder:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url, headers, payload))
        return self.replies.pop(0)


def published(setup):
    storage, publisher, draft, _, _ = setup
    publisher.publish_draft(draft.id)
    return storage.get_announcement(draft.id)


def test_mastodon_uploads_the_image_with_its_description(setup):
    announcement = published(setup)
    transport = Recorder((200, {"id": "m1"}), (200, {"id": "s1"}))
    MastodonAdapter("https://social.example", "token-123456", transport=transport).publish(
        announcement
    )
    upload = transport.calls[0][3]
    assert isinstance(upload, http.Multipart)
    assert upload.fields == {"description": ALT}
    assert transport.calls[1][3]["media_ids"] == ["m1"]


def test_bluesky_embeds_the_image_with_alt_text(setup):
    announcement = published(setup)
    transport = Recorder(
        (200, {"accessJwt": "jwt", "did": "did:plc:a"}),
        (200, {"blob": {"ref": "cid"}}),
        (200, {"uri": "at://did:plc:a/app.bsky.feed.post/x"}),
    )
    BlueskyAdapter("us.bsky.social", "abcd-efgh-ijkl-mnop", transport=transport).publish(
        announcement
    )
    assert isinstance(transport.calls[1][3], http.RawBody)
    assert len(transport.calls[1][3].content) <= 1_000_000
    embed = transport.calls[2][3]["record"]["embed"]
    assert embed["images"][0]["alt"] == ALT


def test_discord_attaches_and_slack_links_the_image(setup):
    announcement = published(setup)
    discord = Recorder((200, {"id": "9"}))
    DiscordWebhookAdapter("https://discord.com/api/webhooks/1/x", transport=discord).publish(
        announcement
    )
    payload = discord.calls[0][3]
    assert json.loads(payload.fields["payload_json"])["attachments"][0]["description"] == ALT

    slack = Recorder((200, {}))
    SlackWebhookAdapter("https://hooks.slack.com/services/T/B/x", transport=slack).publish(
        announcement
    )
    image_block = slack.calls[0][3]["blocks"][1]
    assert image_block["alt_text"] == ALT
    assert image_block["image_url"].endswith(f"/feed/media/{announcement.image.filename}")


def test_multipart_encoding_contains_fields_and_file():
    body, content_type = http.Multipart(
        {"description": "hi"}, (("file", "a.jpg", "image/jpeg", b"\xff\xd8"),)
    ).encode()
    boundary = content_type.split("boundary=")[1]
    assert body.startswith(f"--{boundary}".encode()) and b'name="description"' in body
    assert b'filename="a.jpg"' in body and b"\xff\xd8" in body


@pytest.fixture
def console(tmp_path):
    AdminStore(tmp_path / "announcements.db").create_admin("olga", PASSWORD)
    app = create_app(
        db_path=tmp_path / "announcements.db",
        feed_dir=tmp_path / "feed",
        config_path=tmp_path / "missing.yaml",
        setup_token="unused",
        run_worker=False,
    )
    client = TestClient(app, base_url="https://community.test", follow_redirects=False)
    page = client.get("/admin/login")
    client.post(
        "/admin/login", data={"csrf_token": csrf(page), "username": "olga", "password": PASSWORD}
    )
    editor = client.get("/admin/announcements/new")
    created = client.post(
        "/admin/announcements/new",
        data={"csrf_token": csrf(editor), "title": "Food drive", "body": "Thanks!"},
    )
    return app, client, created.headers["location"]


def csrf(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def upload(client, url, token, alt=ALT):
    return client.post(
        f"{url}/image",
        data={"csrf_token": token, "alt": alt},
        files={"image": ("photo.jpg", jpeg_with_location(), "image/jpeg")},
    )


def test_editor_uploads_previews_replaces_and_removes_shared_image(console):
    app, client, _ = console
    editor = client.get("/admin/announcements/new")
    created = client.post(
        "/admin/announcements/new",
        data={
            "csrf_token": csrf(editor),
            "title": "Food drive",
            "body": "Bring supplies.",
            "image_alt": ALT,
        },
        files={"image": ("photo.jpg", jpeg_with_location(), "image/jpeg")},
    )
    assert created.status_code == 303
    announcement_id = created.headers["location"].rsplit("/", 1)[-1]
    original = app.state.storage.get_announcement(announcement_id).image
    assert original is not None

    edit_url = f"{created.headers['location']}/edit"
    edit = client.get(edit_url)
    assert f'src="/admin/media/{original.id}"' in edit.text
    assert f">{ALT}</textarea>" in edit.text

    saved = client.post(
        f"{created.headers['location']}/update",
        data={
            "csrf_token": csrf(edit),
            "title": "Food drive update",
            "body": "Bring canned food.",
            "image_alt": ALT,
        },
        files={"image": ("", b"", "application/octet-stream")},
    )
    assert saved.status_code == 303
    unchanged = app.state.storage.get_announcement(announcement_id).image
    assert unchanged is not None and unchanged.id == original.id

    edit = client.get(edit_url)
    replacement = client.post(
        f"{created.headers['location']}/update",
        data={
            "csrf_token": csrf(edit),
            "title": "Food drive update",
            "body": "Bring canned food.",
            "image_alt": "Canned food arranged on a table",
        },
        files={"image": ("replacement.jpg", jpeg_with_location(), "image/jpeg")},
    )
    assert replacement.status_code == 303
    replaced_image = app.state.storage.get_announcement(announcement_id).image
    assert replaced_image is not None
    assert replaced_image.id != original.id
    assert not original.path.exists()

    edit = client.get(edit_url)
    removed = client.post(
        f"{created.headers['location']}/update",
        data={
            "csrf_token": csrf(edit),
            "title": "Food drive update",
            "body": "Bring canned food.",
            "image_alt": "Canned food arranged on a table",
            "remove_image": "true",
        },
        files={"image": ("", b"", "application/octet-stream")},
    )
    assert removed.status_code == 303
    assert app.state.storage.get_announcement(announcement_id).image is None


def test_console_upload_describe_and_remove(console):
    app, client, url = console
    detail = client.get(url)
    assert upload(client, url, "wrong").status_code == 403
    assert upload(client, url, csrf(detail), alt=" ").status_code == 400
    assert upload(client, url, csrf(detail)).status_code == 303

    detail = client.get(url)
    image_url = re.search(r'src="(/admin/media/[^"]+)"', detail.text).group(1)
    assert client.get(image_url).headers["content-type"] == "image/jpeg"
    assert (
        TestClient(app, base_url="https://community.test", follow_redirects=False)
        .get(image_url)
        .status_code
        == 303
    )

    described = client.post(f"{url}/image/alt", data={"csrf_token": csrf(detail), "alt": "New"})
    assert described.status_code == 303
    assert "Description: New" in client.get(url).text

    client.post(f"{url}/image/remove", data={"csrf_token": csrf(detail)})
    assert "/admin/media/" not in client.get(url).text
    actions = [event["action"] for event in app.state.auth_store.list_events()]
    assert {"announcement.image_added", "announcement.image_removed"} <= set(actions)


def test_console_channel_image_override_upload_and_remove(console):
    app, client, url = console
    detail = client.get(url)
    upload_form = {
        "csrf_token": csrf(detail),
        "channel": "instagram",
        "alt": ALT,
    }

    assert (
        client.post(
            f"{url}/channel-image",
            data={**upload_form, "channel": "unknown"},
            files={"image": ("photo.jpg", jpeg_with_location(), "image/jpeg")},
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"{url}/channel-image",
            data={**upload_form, "csrf_token": "wrong"},
            files={"image": ("photo.jpg", jpeg_with_location(), "image/jpeg")},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{url}/channel-image",
            data={**upload_form, "alt": " "},
            files={"image": ("photo.jpg", jpeg_with_location(), "image/jpeg")},
        ).status_code
        == 400
    )

    saved = client.post(
        f"{url}/channel-image",
        data=upload_form,
        files={"image": ("photo.jpg", jpeg_with_location(), "image/jpeg")},
    )
    assert saved.status_code == 303
    announcement_id = url.rsplit("/", 1)[-1]
    announcement = app.state.storage.get_announcement(announcement_id)
    image = announcement.channel_media["instagram"]
    assert image.path.is_file()
    assert "Instagram" in client.get(url).text

    removed = client.post(
        f"{url}/channel-image/instagram/remove",
        data={"csrf_token": csrf(client.get(url))},
    )
    assert removed.status_code == 303
    assert app.state.storage.get_announcement(announcement_id).channel_media == {}
    actions = [event["action"] for event in app.state.auth_store.list_events()]
    assert {
        "announcement.channel_image_added",
        "announcement.channel_image_removed",
    } <= set(actions)


def test_images_cannot_change_after_publication(console):
    _, client, url = console
    detail = client.get(url)
    client.post(f"{url}/publish", data={"csrf_token": csrf(detail), "confirm": "publish"})
    assert upload(client, url, csrf(detail)).status_code == 409
