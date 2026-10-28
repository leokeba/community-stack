"""Announcement images: processing, the public feed, channel uploads, and the console."""

import io
import json
import re

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


def test_images_cannot_change_after_publication(console):
    _, client, url = console
    detail = client.get(url)
    client.post(f"{url}/publish", data={"csrf_token": csrf(detail), "confirm": "publish"})
    assert upload(client, url, csrf(detail)).status_code == 409
