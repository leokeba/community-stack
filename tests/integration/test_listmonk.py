"""Email newsletter through listmonk: idempotent campaigns, follow-ups, and validation."""

import pytest
from community_stack.announcements.adapters import http
from community_stack.announcements.adapters.listmonk import ListmonkAdapter
from community_stack.announcements.connectors import CONNECTORS
from community_stack.announcements.models import Announcement


class FakeListmonk:
    """A tiny in-memory listmonk: campaigns can be created, searched and started."""

    def __init__(self, fail_start: int = 0):
        self.campaigns: list[dict] = []
        self.calls: list[tuple[str, str, dict, object]] = []
        self.fail_start = fail_start

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url, headers, payload))
        path = url.split("https://lists.example.org", 1)[1]
        if method == "GET" and path.startswith("/api/campaigns?"):
            return 200, {"data": {"results": [dict(c) for c in self.campaigns]}}
        if method == "POST" and path == "/api/campaigns":
            campaign = {"id": len(self.campaigns) + 1, "status": "draft", **payload}
            self.campaigns.append(campaign)
            return 200, {"data": campaign}
        if method == "PUT" and path.endswith("/status"):
            if self.fail_start:
                self.fail_start -= 1
                return 503, {"message": "busy"}
            campaign_id = int(path.split("/")[3])
            self.campaigns[campaign_id - 1]["status"] = payload["status"]
            return 200, {"data": True}
        if method == "GET" and path.startswith("/api/lists"):
            return 200, {"data": {"results": [{"id": 1, "name": "Neighbours"}]}}
        raise AssertionError(f"unexpected {method} {path}")


def adapter(fake: FakeListmonk) -> ListmonkAdapter:
    return ListmonkAdapter(
        "https://lists.example.org", "api", "secret-token-1", [1], transport=fake
    )


def announcement() -> Announcement:
    return Announcement(
        title="Garden party",
        body="**Saturday** at the park.",
        canonical_url="https://community.example.org/feed/announcements/ann_1/",
        author="Organizers",
    )


def test_publish_creates_a_markdown_campaign_and_starts_it():
    fake = FakeListmonk()
    ann = announcement()

    result = adapter(fake).publish(ann)

    assert result.success and result.external_id == "1"
    campaign = fake.campaigns[0]
    assert campaign["status"] == "running"
    assert campaign["content_type"] == "markdown"
    assert campaign["lists"] == [1]
    assert campaign["name"].endswith(f"[{ann.id}]")
    assert "[Read this announcement online](https://community.example.org/" in campaign["body"]
    assert fake.calls[0][2]["Authorization"] == "token api:secret-token-1"


def test_retry_after_a_failed_start_does_not_create_a_second_campaign():
    fake = FakeListmonk(fail_start=1)
    ann = announcement()
    first = adapter(fake).publish(ann)
    assert not first.success and first.retryable and first.idempotent

    second = adapter(fake).publish(ann)
    assert second.success
    assert len(fake.campaigns) == 1
    assert adapter(fake).publish(ann).success
    assert len(fake.campaigns) == 1


def test_edits_and_withdrawals_send_one_follow_up_each():
    fake = FakeListmonk()
    ann = announcement()
    adapter(fake).publish(ann)
    adapter(fake).update(ann)
    adapter(fake).update(ann)
    adapter(fake).withdraw(ann.id)

    subjects = [campaign["subject"] for campaign in fake.campaigns]
    assert subjects == [
        "Garden party",
        "Update: Garden party",
        "Correction: an announcement was withdrawn",
    ]


def test_health_check_reports_missing_lists():
    fake = FakeListmonk()
    assert adapter(fake).health_check().ok
    missing = ListmonkAdapter("https://lists.example.org", "api", "t" * 12, [1, 7], transport=fake)
    assert "7" in missing.health_check().details


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"base_url": "http://lists.example.org"}, "https"),
        (
            {"base_url": "https://lists.example.org", "api_user": "api", "list_ids": "one"},
            "list IDs",
        ),
    ],
)
def test_listmonk_settings_are_validated(monkeypatch, values, message):
    monkeypatch.setattr(http, "resolve", lambda host: ["93.184.216.34"])
    with pytest.raises(ValueError, match=message):
        CONNECTORS["listmonk"].validate({"api_token": "secret-token-1", **values})


def test_listmonk_connector_builds_with_list_ids(monkeypatch):
    monkeypatch.setattr(http, "resolve", lambda host: ["93.184.216.34"])
    values = CONNECTORS["listmonk"].validate(
        {
            "base_url": "https://lists.example.org/",
            "api_user": "api",
            "api_token": "secret-token-1",
            "list_ids": "1, 3",
        }
    )
    built = CONNECTORS["listmonk"].build(values)
    assert built.list_ids == [1, 3]
    assert built.base_url == "https://lists.example.org"
