"""Markdown formatting, per-channel custom text, and the editor's live preview."""

import re

import pytest
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.announcements.adapters import http
from community_stack.announcements.adapters.matrix_room import MatrixRoomAdapter
from community_stack.announcements.adapters.social import MastodonAdapter
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.formatting import render_html, to_plain_text
from community_stack.announcements.models import Announcement
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage
from fastapi.testclient import TestClient

PASSWORD = "a-long-safe-test-password"
BODY = "## Food bank\n\nOpen **Saturday**.\n\n- Rice\n- Beans\n\nSee [the map](https://example.org/map)."


def test_markdown_renders_safely():
    html = render_html("<script>alert(1)</script> [x](javascript:alert(1)) **ok**")
    assert "<script>" not in html
    assert 'href="javascript' not in html
    assert "<strong>ok</strong>" in html
    assert "<br />" in render_html("line one\nline two")


def test_plain_text_drops_markdown_syntax():
    assert to_plain_text(BODY) == (
        "Food bank\n\nOpen Saturday.\n\n• Rice\n• Beans\n\nSee the map (https://example.org/map)."
    )


def test_feed_and_matrix_render_formatting(tmp_path):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    payloads = []
    matrix = MatrixRoomAdapter(
        transport=lambda method, path, headers, payload: (
            payloads.append(payload) or (200, {"event_id": "$e"})
        )
    )
    feed = WebFeedAdapter(output_dir=tmp_path / "feed", storage=storage)
    publisher = AnnouncementPublisher(storage=storage, adapters=[feed, matrix])

    announcement, _ = publisher.publish("Weekly update", BODY)

    page = (tmp_path / "feed" / "announcements" / announcement.id / "index.html").read_text()
    assert "<h2>Food bank</h2>" in page and "<strong>Saturday</strong>" in page
    assert "<strong>Saturday</strong>" in payloads[0]["formatted_body"]
    assert "**" not in payloads[0]["body"]


def test_connectors_send_custom_text_or_plain_text():
    calls = []

    def transport(method, url, headers, payload):
        calls.append(payload)
        return 200, {"id": "1"}

    adapter = MastodonAdapter("https://social.example", "token-123456", transport=transport)
    plain = Announcement(title="Weekly update", body=BODY)
    adapter.publish(plain)
    assert calls[-1]["status"].startswith("Weekly update\n\nFood bank")
    assert "**" not in calls[-1]["status"]

    custom = Announcement(
        title="Weekly update",
        body=BODY,
        channel_text={"mastodon": "Food bank Saturday! #mutualaid"},
    )
    adapter.publish(custom)
    assert calls[-1]["status"] == "Food bank Saturday! #mutualaid"
    assert adapter.check(custom).preview == "Food bank Saturday! #mutualaid"


def test_invalid_custom_text_is_rejected():
    with pytest.raises(Exception, match="5,000"):
        Announcement(title="t", body="b", channel_text={"mastodon": "x" * 5001})


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "resolve", lambda host: ["93.184.216.34"])
    AdminStore(tmp_path / "announcements.db").create_admin("olga", PASSWORD)
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    storage.save_connection(
        "mastodon", {"instance_url": "https://social.example", "access_token": "tok-12345678"}, None
    )
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
    return app, client


def csrf(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def test_preview_endpoint_returns_rendered_html_and_channel_text(console):
    _, client = console
    editor = client.get("/admin/announcements/new")
    assert "Custom text per channel" in editor.text
    assert "/admin/static/editor.js" in editor.text
    assert client.get("/admin/static/editor.js").status_code == 200

    preview = client.post(
        "/admin/preview",
        data={
            "csrf_token": csrf(editor),
            "title": "Weekly update",
            "body": "x" * 600,
            "channel_text:mastodon": "",
        },
    )
    data = preview.json()
    assert data["html"].startswith("<p>")
    mastodon = next(channel for channel in data["channels"] if channel["name"] == "mastodon")
    assert mastodon["shortened"] and len(mastodon["preview"]) == 500

    assert client.post("/admin/preview", data={"title": "x"}).status_code == 403


def test_editor_saves_custom_text_and_detail_shows_formatting(console):
    app, client = console
    editor = client.get("/admin/announcements/new")
    created = client.post(
        "/admin/announcements/new",
        data={
            "csrf_token": csrf(editor),
            "title": "Weekly update",
            "body": BODY,
            "channel_text:mastodon": "Short version for Mastodon",
        },
    )
    announcement_id = created.headers["location"].rsplit("/", 1)[-1]
    stored = app.state.storage.get_announcement(announcement_id)
    assert stored.channel_text == {"mastodon": "Short version for Mastodon"}

    detail = client.get(created.headers["location"])
    assert "<h2>Food bank</h2>" in detail.text
    assert "Short version for Mastodon" in detail.text

    edit = client.get(f"{created.headers['location']}/edit")
    assert "Short version for Mastodon" in edit.text
    client.post(
        f"{created.headers['location']}/update",
        data={"csrf_token": csrf(edit), "title": "Weekly update", "body": BODY},
    )
    assert app.state.storage.get_announcement(announcement_id).channel_text == {}
