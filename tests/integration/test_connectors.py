"""Wave-1 connectors (Mastodon, Bluesky, Telegram, Discord, Slack) and the Channels page."""

import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.announcements.adapters import http
from community_stack.announcements.adapters.messaging import (
    DiscordWebhookAdapter,
    SlackWebhookAdapter,
    TelegramAdapter,
    slack_escape,
)
from community_stack.announcements.adapters.social import (
    BlueskyAdapter,
    MastodonAdapter,
    deterministic_tid,
)
from community_stack.announcements.connectors import CONNECTORS
from community_stack.announcements.models import Announcement
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage
from fastapi.testclient import TestClient

PASSWORD = "a-long-safe-test-password"
TELEGRAM_TOKEN = "123456789:AAbbccddeeffgghhiijjkkllmmnnoopp_qq"


class FakeTransport:
    """Records requests and answers each with the first matching scripted reply."""

    def __init__(self, replies: list[tuple[str, str, int, dict]]):
        self.replies = replies
        self.calls: list[tuple[str, str, dict, object]] = []

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url, headers, payload))
        for index, (want_method, fragment, status, body) in enumerate(self.replies):
            if want_method == method and fragment in url:
                self.replies.pop(index)
                return status, body
        raise AssertionError(f"Unexpected request {method} {url}")


def announcement(body: str = "Bring water and snacks.") -> Announcement:
    return Announcement(
        title="Cleanup day",
        body=body,
        canonical_url="https://community.example.org/feed/announcements/ann_1/",
        published_at="2026-11-01T10:00:00+00:00",
    )


def test_compose_post_shortens_with_a_link_to_the_full_text():
    long = announcement("x" * 1000)
    text = http.compose_post(long, 300)
    assert len(text) == 300
    assert text.endswith("… https://community.example.org/feed/announcements/ann_1/")
    assert http.compose_post(announcement(), 300) == "Cleanup day\n\nBring water and snacks."


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://mastodon.example", "https"),
        ("https://user:pw@mastodon.example", "user name"),
        ("https://internal.example", "private network"),
    ],
)
def test_unsafe_addresses_are_refused(monkeypatch, url, message):
    monkeypatch.setattr(http, "resolve", lambda host: ["10.0.0.5"])
    with pytest.raises(http.UnsafeURLError, match=message):
        http.assert_public_https(url)


def test_default_transport_will_not_contact_private_addresses(monkeypatch):
    monkeypatch.setattr(http, "resolve", lambda host: ["127.0.0.1"])
    status, body = http.send("GET", "https://looks-public.example/api", {}, None)
    assert status == 0
    assert "private network" in body["error"]


def test_mastodon_posts_edits_and_deletes_with_the_saved_status_id():
    transport = FakeTransport(
        [
            ("POST", "/api/v1/statuses", 200, {"id": "111"}),
            ("PUT", "/api/v1/statuses/111", 200, {"id": "111"}),
            ("DELETE", "/api/v1/statuses/111", 404, {}),
        ]
    )
    adapter = MastodonAdapter("https://social.example", "token-123456", transport=transport)

    ann = announcement()
    posted = adapter.publish(ann)
    assert posted.success and posted.external_id == "111"
    assert transport.calls[0][2]["Idempotency-Key"] == "community-stack-" + ann.id
    assert transport.calls[0][2]["Authorization"] == "Bearer token-123456"
    assert adapter.update_post(announcement(), "111").success
    assert adapter.withdraw_post("ann_1", "111").success


def test_mastodon_server_errors_are_retryable_and_auth_errors_are_not():
    transport = FakeTransport(
        [
            ("POST", "/api/v1/statuses", 503, {"error": "busy"}),
            ("POST", "/api/v1/statuses", 401, {"error": "The access token is invalid"}),
        ]
    )
    adapter = MastodonAdapter("https://social.example", "token-123456", transport=transport)
    busy = adapter.publish(announcement())
    denied = adapter.publish(announcement())
    assert busy.retryable and busy.idempotent
    assert not denied.retryable
    assert "access token is invalid" in denied.error


def test_bluesky_uses_a_stable_record_key_and_link_facets():
    long = announcement("é" * 400)
    transport = FakeTransport(
        [
            ("POST", "createSession", 200, {"accessJwt": "jwt", "did": "did:plc:abc"}),
            ("POST", "putRecord", 200, {"uri": "at://did:plc:abc/app.bsky.feed.post/3k"}),
            ("POST", "createSession", 200, {"accessJwt": "jwt", "did": "did:plc:abc"}),
            ("POST", "deleteRecord", 200, {}),
        ]
    )
    adapter = BlueskyAdapter("community.bsky.social", "abcd-efgh-ijkl-mnop", transport=transport)

    result = adapter.publish(long)
    assert result.success
    payload = transport.calls[1][3]
    assert payload["rkey"] == deterministic_tid(long)
    assert len(payload["rkey"]) == 13
    record = payload["record"]
    assert len(record["text"]) <= 300
    link = long.canonical_url.encode()
    facet = record["facets"][0]["index"]
    assert record["text"].encode()[facet["byteStart"] : facet["byteEnd"]] == link

    assert adapter.withdraw_post(long.id, result.external_id).success
    assert transport.calls[3][3]["rkey"] == "3k"


def test_bluesky_record_key_is_stable_and_distinct_per_announcement():
    first = announcement()
    second = Announcement(
        title="Other", body="Other", id="ann_other", published_at="2026-11-01T10:00:00.000001+00:00"
    )
    assert deterministic_tid(first) == deterministic_tid(first)
    # Keys come from the publication time (microseconds) plus 10 bits of the ID.
    assert deterministic_tid(first) != deterministic_tid(second)


def test_telegram_round_trip_and_token_never_appears_in_errors():
    transport = FakeTransport(
        [
            ("POST", "/sendMessage", 200, {"ok": True, "result": {"message_id": 42}}),
            (
                "POST",
                "/editMessageText",
                400,
                {"ok": False, "description": "message is not modified"},
            ),
            ("POST", "/deleteMessage", 403, {"ok": False, "description": f"bad {TELEGRAM_TOKEN}"}),
        ]
    )
    adapter = TelegramAdapter(TELEGRAM_TOKEN, "@ourcommunity", transport=transport)

    assert adapter.publish(announcement()).external_id == "42"
    assert adapter.update_post(announcement(), "42").success
    failed = adapter.withdraw_post("ann_1", "42")
    assert not failed.success
    assert TELEGRAM_TOKEN not in failed.error


def test_telegram_failed_posts_are_only_retried_when_rate_limited():
    transport = FakeTransport(
        [
            ("POST", "/sendMessage", 0, {"error": "Network error: timed out"}),
            ("POST", "/sendMessage", 429, {"ok": False, "description": "Too Many Requests"}),
        ]
    )
    adapter = TelegramAdapter(TELEGRAM_TOKEN, "@ourcommunity", transport=transport)
    assert not adapter.publish(announcement()).idempotent
    assert adapter.publish(announcement()).idempotent


def test_discord_never_pings_and_slack_escapes_markup():
    discord_transport = FakeTransport([("POST", "?wait=true", 200, {"id": "9"})])
    discord = DiscordWebhookAdapter(
        "https://discord.com/api/webhooks/1/abc", transport=discord_transport
    )
    assert discord.publish(announcement("@everyone come")).external_id == "9"
    assert discord_transport.calls[0][3]["allowed_mentions"] == {"parse": []}

    slack_transport = FakeTransport([("POST", "hooks.slack.com", 200, {"raw": "ok"})])
    slack = SlackWebhookAdapter("https://hooks.slack.com/services/T/B/x", transport=slack_transport)
    assert slack.publish(announcement("<!channel> & more")).success
    assert slack_transport.calls[0][3]["text"].endswith("&lt;!channel&gt; &amp; more")
    assert slack_escape("a<b") == "a&lt;b"


def test_publisher_skips_unsupported_edits_and_passes_post_ids(tmp_path):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    slack_transport = FakeTransport([("POST", "hooks.slack.com", 200, {})])
    mastodon_transport = FakeTransport(
        [
            ("POST", "/api/v1/statuses", 200, {"id": "77"}),
            ("PUT", "/api/v1/statuses/77", 200, {"id": "77"}),
        ]
    )
    publisher = AnnouncementPublisher(
        storage=storage,
        adapters=[
            SlackWebhookAdapter(
                "https://hooks.slack.com/services/T/B/x", transport=slack_transport
            ),
            MastodonAdapter("https://social.example", "token-123456", transport=mastodon_transport),
        ],
    )
    published, _ = publisher.publish("Notice", "Body")
    _, results = publisher.update(published.id, body="Corrected")

    assert set(results) == {"mastodon"}
    assert mastodon_transport.calls[-1][1].endswith("/api/v1/statuses/77")


@pytest.mark.parametrize(
    ("kind", "values", "message"),
    [
        (
            "bluesky",
            {"handle": "us.bsky.social", "app_password": "my-real-password"},
            "app password",
        ),
        ("telegram", {"chat_id": "ourcommunity", "bot_token": TELEGRAM_TOKEN}, "@channelname"),
        (
            "telegram",
            {"chat_id": "1004464970877", "bot_token": TELEGRAM_TOKEN},
            "leading minus sign",
        ),
        ("discord", {"webhook_url": "https://evil.example/api/webhooks/1/x"}, "discord.com"),
        ("slack", {"webhook_url": "https://hooks.slack.com/other/x"}, "Slack webhook"),
    ],
)
def test_connector_validation_explains_mistakes(monkeypatch, kind, values, message):
    monkeypatch.setattr(http, "resolve", lambda host: ["93.184.216.34"])
    with pytest.raises(ValueError, match=message):
        CONNECTORS[kind].validate(values)


def test_telegram_accepts_private_channel_id_with_minus_100_prefix():
    values = CONNECTORS["telegram"].validate(
        {"chat_id": "-1004464970877", "bot_token": TELEGRAM_TOKEN}
    )

    assert values["chat_id"] == "-1004464970877"


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "resolve", lambda host: ["93.184.216.34"])
    transport = FakeTransport([])
    monkeypatch.setattr(http, "send", transport)
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
        "/admin/login",
        data={"csrf_token": csrf(page), "username": "olga", "password": PASSWORD},
    )
    return app, client, transport


def csrf(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def test_console_connects_tests_and_publishes_to_a_channel(console):
    app, client, transport = console
    page = client.get("/admin/channels")
    assert "Mastodon" in page.text and "Not connected" in page.text

    saved = client.post(
        "/admin/channels/mastodon/save",
        data={
            "csrf_token": csrf(page),
            "instance_url": "https://social.example/",
            "access_token": "secret-token-value",
        },
    )
    assert saved.status_code == 200
    assert "secret-token-value" not in saved.text
    assert "https://social.example" in saved.text

    transport.replies.append(("GET", "verify_credentials", 200, {"acct": "community"}))
    tested = client.post("/admin/channels/mastodon/test", data={"csrf_token": csrf(page)})
    assert "Connected as @community." in tested.text

    transport.replies.append(("POST", "/api/v1/statuses", 200, {"id": "5"}))
    _, results = app.state.publisher.publish("Hello fediverse", "First post")
    assert results["mastodon"].success
    assert transport.calls[-1][2]["Authorization"] == "Bearer secret-token-value"

    actions = [event["action"] for event in app.state.auth_store.list_events()]
    assert {"channel.connected", "channel.tested"} <= set(actions)
    assert all("secret-token-value" not in str(e) for e in app.state.auth_store.list_events())


def test_saving_without_a_new_secret_keeps_the_old_one(console):
    app, client, _ = console
    page = client.get("/admin/channels")
    client.post(
        "/admin/channels/telegram/save",
        data={"csrf_token": csrf(page), "chat_id": "@ourcommunity", "bot_token": TELEGRAM_TOKEN},
    )
    client.post(
        "/admin/channels/telegram/save",
        data={"csrf_token": csrf(page), "chat_id": "@renamedchannel", "bot_token": ""},
    )
    saved = app.state.storage.list_connections()["telegram"]
    assert saved == {"chat_id": "@renamedchannel", "bot_token": TELEGRAM_TOKEN}


def test_invalid_settings_are_explained_and_disconnect_removes_credentials(console):
    app, client, _ = console
    page = client.get("/admin/channels")
    refused = client.post(
        "/admin/channels/discord/save",
        data={"csrf_token": csrf(page), "webhook_url": "https://evil.example/api/webhooks/1/x"},
    )
    assert refused.status_code == 400
    assert "discord.com" in refused.text

    client.post(
        "/admin/channels/discord/save",
        data={"csrf_token": csrf(page), "webhook_url": "https://discord.com/api/webhooks/1/abc"},
    )
    assert any(a.name == "discord" for a in app.state.publisher.adapters)
    client.post("/admin/channels/discord/disconnect", data={"csrf_token": csrf(page)})
    assert "discord" not in app.state.storage.list_connections()
    assert not any(a.name == "discord" for a in app.state.publisher.adapters)


def test_matrix_connector_enables_mirroring_and_whatsapp_forwarding(console, tmp_path):
    app, client, _ = console
    config = yaml.safe_load(Path("config/example.yaml").read_text(encoding="utf-8"))
    config["domain"]["matrix"] = "community.test"
    config["services"]["whatsapp_bridge"]["enabled"] = True
    config_path = tmp_path / "missing.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    app.state.publisher.reload_connections()

    page = client.get("/admin/channels")
    saved = client.post(
        "/admin/channels/matrix_room/save",
        data={
            "csrf_token": csrf(page),
            "homeserver_url": "http://community_synapse:8008",
            "room_id": "!announcements:community.test",
            "access_token": "matrix-access-token",
        },
    )
    assert saved.status_code == 200
    assert "matrix-access-token" not in saved.text

    mirror_page = client.get("/admin/mirroring")
    assert "Mirror a room" in mirror_page.text
    assert "not available in the admin UI yet" not in mirror_page.text

    wa_page = client.get("/admin/whatsapp")
    assert "login qr" in wa_page.text
    assert "Linked devices" in wa_page.text
    assert "@whatsappbot:community.test" in wa_page.text

    saved_wa = client.post(
        "/admin/channels/whatsapp/save",
        data={"csrf_token": csrf(page), "room_id": "!portal:community.test"},
    )
    assert saved_wa.status_code == 200
    assert any(adapter.name == "whatsapp" for adapter in app.state.publisher.adapters)


def test_channel_credentials_database_is_private_on_posix(console):
    app, _, _ = console

    if os.name == "posix":
        assert app.state.storage.db_path.stat().st_mode & 0o777 == 0o600


def test_whatsapp_channel_requires_bridge_and_matrix_prerequisites(console):
    _, client, _ = console
    page = client.get("/admin/channels")

    response = client.post(
        "/admin/channels/whatsapp/save",
        data={"csrf_token": csrf(page), "room_id": "!portal:community.test"},
    )

    assert response.status_code == 409
    assert "Enable the WhatsApp bridge first" in response.text


def test_unknown_channel_types_are_rejected(console):
    _, client, _ = console
    page = client.get("/admin/channels")
    assert (
        client.post("/admin/channels/myspace/save", data={"csrf_token": csrf(page)}).status_code
        == 404
    )


def test_tid_uses_publication_time():
    ann = announcement()
    later = Announcement(
        title=ann.title,
        body=ann.body,
        id=ann.id,
        published_at=datetime(2027, 1, 1, tzinfo=timezone.utc).isoformat(),
    )
    assert deterministic_tid(ann) != deterministic_tid(later)
