"""Review before publishing: submission, approval by someone else, edits, and urgent alerts."""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.announcements.models import AnnouncementStatus
from fastapi.testclient import TestClient

PASSWORD = "a-long-safe-test-password"


@pytest.fixture
def app(tmp_path):
    store = AdminStore(tmp_path / "announcements.db")
    store.create_admin("olga", PASSWORD)
    store.create_account("pat", PASSWORD, "publisher")
    store.create_account("rita", PASSWORD, "reviewer")
    return create_app(
        db_path=tmp_path / "announcements.db",
        feed_dir=tmp_path / "feed",
        config_path=tmp_path / "missing.yaml",
        setup_token="unused",
        run_worker=False,
    )


def csrf(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def login(app, username: str) -> TestClient:
    client = TestClient(app, base_url="https://community.test", follow_redirects=False)
    page = client.get("/admin/login")
    client.post(
        "/admin/login", data={"csrf_token": csrf(page), "username": username, "password": PASSWORD}
    )
    return client


def post(client: TestClient, path: str, **data):
    return client.post(path, data={"csrf_token": csrf(client.get("/admin/account")), **data})


@pytest.fixture
def review_on(app):
    owner = login(app, "olga")
    assert post(owner, "/admin/settings/review", review_required="true").status_code == 303
    return owner


def draft(client: TestClient, title: str = "Rent strike meeting") -> str:
    response = post(client, "/admin/announcements/new", title=title, body="Thursday 7pm")
    return response.headers["location"]


def status(app, url: str) -> AnnouncementStatus:
    return app.state.storage.get_announcement(url.rsplit("/", 1)[-1]).status


def test_submitted_announcements_need_someone_elses_approval(app, review_on):
    pat, rita = login(app, "pat"), login(app, "rita")
    url = draft(pat)

    assert post(pat, f"{url}/publish", confirm="publish").status_code == 409
    assert post(pat, f"{url}/submit").status_code == 303
    assert status(app, url) == AnnouncementStatus.IN_REVIEW
    assert post(pat, f"{url}/approve").status_code == 403
    assert "Someone else needs to approve it." in pat.get(url).text

    assert "Waiting for your review" in rita.get("/admin").text
    assert "Waiting for your review" not in pat.get("/admin").text
    assert post(rita, f"{url}/approve", note="Looks good").status_code == 303
    assert status(app, url) == AnnouncementStatus.APPROVED

    assert post(pat, f"{url}/publish", confirm="publish").status_code == 303
    assert status(app, url) == AnnouncementStatus.PUBLISHED
    history = [e["action"] for e in app.state.storage.review_history(url.rsplit("/", 1)[-1])]
    assert history == ["submitted", "approved"]


def test_reviewers_can_send_drafts_back_with_a_note(app, review_on):
    pat, rita = login(app, "pat"), login(app, "rita")
    url = draft(pat)
    post(pat, f"{url}/submit")

    assert post(rita, f"{url}/request-changes", note=" ").status_code == 400
    assert post(rita, f"{url}/request-changes", note="Add the address").status_code == 303
    assert status(app, url) == AnnouncementStatus.DRAFT
    assert "Add the address" in pat.get(url).text


def test_editing_after_approval_or_scheduling_requires_review_again(app, review_on):
    pat, rita = login(app, "pat"), login(app, "rita")
    url = draft(pat)
    post(pat, f"{url}/submit")
    post(rita, f"{url}/approve")
    when = (datetime.now(ZoneInfo("UTC")) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    assert post(pat, f"{url}/schedule", scheduled_for=when).status_code == 303
    assert status(app, url) == AnnouncementStatus.SCHEDULED

    post(pat, f"{url}/update", title="Rent strike meeting", body="Friday 7pm")
    stored = app.state.storage.get_announcement(url.rsplit("/", 1)[-1])
    assert stored.status == AnnouncementStatus.DRAFT
    assert stored.scheduled_at is None
    assert post(pat, f"{url}/schedule", scheduled_for=when).status_code == 409


def test_urgent_publication_needs_an_owner_or_admin_a_reason_and_a_password(app, review_on):
    pat = login(app, "pat")
    url = draft(pat, "Gas leak on Main Street")
    post(pat, f"{url}/submit")

    assert (
        post(pat, f"{url}/publish-urgently", reason="Evacuate now", password=PASSWORD).status_code
        == 403
    )
    assert (
        post(review_on, f"{url}/publish-urgently", reason="short", password=PASSWORD).status_code
        == 400
    )
    assert (
        post(
            review_on, f"{url}/publish-urgently", reason="Evacuate the block now", password="wrong"
        )
    ).status_code == 403

    done = post(
        review_on, f"{url}/publish-urgently", reason="Evacuate the block now", password=PASSWORD
    )
    assert done.status_code == 303
    assert status(app, url) == AnnouncementStatus.PUBLISHED
    event = app.state.auth_store.list_events()[0]
    assert event["action"] == "announcement.published_urgently"
    assert event["details"]["reason"] == "Evacuate the block now"


def test_with_review_off_publishers_publish_directly(app):
    pat = login(app, "pat")
    url = draft(pat)
    assert "Submit for review" not in pat.get(url).text
    assert post(pat, f"{url}/publish", confirm="publish").status_code == 303


def test_announcements_in_review_are_not_public(app, review_on, tmp_path):
    pat = login(app, "pat")
    url = draft(pat, "Not yet public")
    post(pat, f"{url}/submit")
    feed = tmp_path / "feed" / "announcements.html"
    assert not feed.exists() or "Not yet public" not in feed.read_text(encoding="utf-8")
