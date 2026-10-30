"""Meta social publishing and connector validation."""

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from community_stack.announcements.adapters import http
from community_stack.announcements.adapters.meta import (
    FACEBOOK_GRAPH,
    INSTAGRAM_GRAPH,
    THREADS_GRAPH,
    FacebookPageAdapter,
    InstagramAdapter,
    ThreadsAdapter,
)
from community_stack.announcements.connectors import CONNECTORS, build_connector
from community_stack.announcements.media import MediaImage
from community_stack.announcements.models import Announcement

TOKEN = "long-lived-token-value"


class FakeTransport:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url, headers, payload))
        for index, (want_method, fragment, status, body) in enumerate(self.replies):
            if want_method == method and fragment in url:
                self.replies.pop(index)
                return status, body
        raise AssertionError(f"Unexpected request {method} {url}")


def announcement(image=None, channel_text=None) -> Announcement:
    return Announcement(
        title="Cleanup day",
        body="Bring water and snacks.",
        canonical_url="https://community.example.org/feed/announcements/ann_1/",
        published_at="2026-11-01T10:00:00+00:00",
        image=image,
        channel_text=channel_text or {},
    )


def image() -> MediaImage:
    return MediaImage(
        id="img_test_1234567890123456",
        path=Path("cleanup.jpg"),
        alt="Volunteers cleaning the park",
        content_type="image/jpeg",
        width=1200,
        height=800,
    )


def form_fields(payload: http.Form) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(payload.encode()[0].decode()).items()}


def test_http_transport_sends_form_encoded_meta_requests(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self):
            return b'{"id":"post-1"}'

    def open_request(request, timeout):
        assert timeout == http.TIMEOUT_SECONDS
        assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
        assert request.data == b"message=Hello+world"
        return Response()

    monkeypatch.setattr(http, "assert_public_https", lambda url: url)
    monkeypatch.setattr(http.urllib.request, "urlopen", open_request)

    status, body = http.send(
        "POST",
        "https://graph.facebook.com/v26.0/123/feed",
        {},
        http.Form({"message": "Hello world"}),
    )

    assert status == 200 and body == {"id": "post-1"}


@pytest.mark.parametrize(
    ("kind", "values"),
    [
        ("facebook", {"page_id": "123456789", "access_token": TOKEN}),
        ("instagram", {"account_id": "123456789", "access_token": TOKEN}),
        ("threads", {"user_id": "123456789", "access_token": TOKEN}),
    ],
)
def test_meta_connectors_validate_and_build(kind, values):
    assert CONNECTORS[kind].validate(values) == values
    adapter = build_connector(kind, values)
    assert adapter.name == kind


@pytest.mark.parametrize(
    ("kind", "values"),
    [
        ("facebook", {"page_id": "page", "access_token": TOKEN}),
        ("instagram", {"account_id": "123456789", "access_token": "short"}),
        ("threads", {"user_id": "123456789", "access_token": "token with spaces"}),
    ],
)
def test_meta_connectors_reject_invalid_credentials(kind, values):
    with pytest.raises(ValueError):
        CONNECTORS[kind].validate(values)


def test_facebook_publishes_edits_and_withdraws_page_posts():
    transport = FakeTransport(
        [
            ("POST", "/123456789/feed", 200, {"id": "123456789_987"}),
            ("POST", "/123456789_987", 200, {"success": True}),
            ("DELETE", "/123456789_987", 200, {"success": True}),
        ]
    )
    adapter = FacebookPageAdapter("123456789", TOKEN, transport)

    published = adapter.publish(announcement(channel_text={"facebook": "Custom post"}))
    edited = adapter.update_post(announcement(), published.external_id)
    withdrawn = adapter.withdraw_post("ann_1", published.external_id)

    assert published.success and published.external_id == "123456789_987"
    assert edited.success and edited.external_id == published.external_id
    assert withdrawn.success
    post = transport.calls[0][3]
    assert isinstance(post, http.Form)
    assert form_fields(post) == {"message": "Custom post", "access_token": TOKEN}
    assert transport.calls[1][3].fields["message"] == "Cleanup day\n\nBring water and snacks."
    assert parse_qs(urlsplit(transport.calls[2][1]).query)["access_token"] == [TOKEN]
    assert adapter.capabilities.update_mode.value == "in_place"


def test_facebook_image_uses_public_url_and_health_check_redacts_token():
    transport = FakeTransport(
        [
            ("POST", "/123456789/photos", 200, {"id": "photo-4", "post_id": "post-4"}),
            ("GET", "/123456789?", 200, {"id": "123456789", "name": "Community"}),
        ]
    )
    adapter = FacebookPageAdapter("123456789", TOKEN, transport)

    result = adapter.publish(announcement(image=image()))
    health = adapter.health_check()

    assert result.success and result.external_id == "post-4"
    assert form_fields(transport.calls[0][3])["url"] == (
        "https://community.example.org/feed/media/cleanup.jpg"
    )
    assert health.ok and health.details == "Connected to Facebook Page Community."
    assert TOKEN not in health.details
    assert FACEBOOK_GRAPH in transport.calls[0][1]


def test_instagram_requires_public_image_and_publishes_container():
    transport = FakeTransport(
        [
            ("POST", "/17841400000000000/media", 200, {"id": "container-1"}),
            ("POST", "/17841400000000000/media_publish", 200, {"id": "ig-post-1"}),
            ("GET", "/me?", 200, {"id": "17841400000000000", "username": "community"}),
        ]
    )
    adapter = InstagramAdapter("17841400000000000", TOKEN, transport)

    missing = adapter.publish(announcement())
    published = adapter.publish(announcement(image=image()))
    health = adapter.health_check()

    assert not missing.success and "requires an image" in missing.error
    assert published.success and published.external_id == "ig-post-1"
    media = form_fields(transport.calls[0][3])
    assert media["image_url"] == "https://community.example.org/feed/media/cleanup.jpg"
    assert media["alt_text"] == "Volunteers cleaning the park"
    assert media["caption"] == "Cleanup day\n\nBring water and snacks."
    assert health.ok and health.details == "Connected as @community."
    assert INSTAGRAM_GRAPH in transport.calls[0][1]
    assert adapter.capabilities.update_mode.value == "none"
    assert adapter.capabilities.withdraw_mode.value == "none"


def test_threads_publishes_text_and_images_then_deletes_post():
    transport = FakeTransport(
        [
            ("POST", "/123456789/threads", 200, {"id": "text-container"}),
            ("POST", "/123456789/threads_publish", 200, {"id": "threads-post-1"}),
            ("POST", "/123456789/threads", 200, {"id": "image-container"}),
            ("POST", "/123456789/threads_publish", 200, {"id": "threads-post-2"}),
            ("DELETE", "/threads-post-2?", 200, {"success": True}),
            ("GET", "/me?", 200, {"id": "123456789", "username": "community"}),
        ]
    )
    adapter = ThreadsAdapter("123456789", TOKEN, transport)

    text_post = adapter.publish(announcement())
    image_post = adapter.publish(announcement(image=image()))
    withdrawn = adapter.withdraw_post("ann_1", image_post.external_id)
    health = adapter.health_check()

    assert text_post.success and text_post.external_id == "threads-post-1"
    assert image_post.success and image_post.external_id == "threads-post-2"
    assert form_fields(transport.calls[0][3])["media_type"] == "TEXT"
    assert form_fields(transport.calls[0][3])["text"] == "Cleanup day\n\nBring water and snacks."
    image_fields = form_fields(transport.calls[2][3])
    assert image_fields["media_type"] == "IMAGE"
    assert image_fields["alt_text"] == "Volunteers cleaning the park"
    assert withdrawn.success and health.ok
    assert THREADS_GRAPH in transport.calls[0][1]
    assert adapter.capabilities.update_mode.value == "none"
    assert adapter.capabilities.withdraw_mode.value == "delete"


def test_meta_errors_do_not_expose_access_tokens():
    transport = FakeTransport(
        [
            (
                "GET",
                "/me?",
                401,
                {"error": {"message": f"Invalid token {TOKEN}"}},
            )
        ]
    )
    health = ThreadsAdapter("123456789", TOKEN, transport).health_check()

    assert not health.ok
    assert TOKEN not in health.details
    assert "[redacted]" in health.details


@pytest.mark.parametrize(
    ("adapter_type", "values", "fragment", "body", "message"),
    [
        (
            FacebookPageAdapter,
            ("123456789", TOKEN),
            "/123456789?",
            {"id": "987654321", "name": "Other Page"},
            "configured Facebook Page",
        ),
        (
            InstagramAdapter,
            ("17841400000000000", TOKEN),
            "/me?",
            {"id": "17841400000000001", "username": "other"},
            "configured Instagram account",
        ),
        (
            ThreadsAdapter,
            ("123456789", TOKEN),
            "/me?",
            {"id": "987654321", "username": "other"},
            "configured Threads account",
        ),
    ],
)
def test_meta_health_checks_reject_a_token_for_another_account(
    adapter_type, values, fragment, body, message
):
    transport = FakeTransport([("GET", fragment, 200, body)])
    health = adapter_type(*values, transport=transport).health_check()

    assert not health.ok
    assert message in health.details
