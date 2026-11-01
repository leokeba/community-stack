"""Console subscribers page: counts, masked records, export, erasure, test send, and the privacy notice."""

import json
import re

import pytest
from community_stack import backup as backup_module
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.agent import client as agent_client
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.subscriptions.email_transport import SmtpTransport
from community_stack.subscriptions.manager import SubscriberManager
from fastapi.testclient import TestClient

PASSWORD = "a-long-safe-test-password"
CONNECTION = {
    "host": "smtp.example.org",
    "port": "587",
    "from_address": "announcements@ourtown.org",
    "public_url": "https://ourtown.org",
}


def csrf(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


@pytest.fixture
def site(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(SmtpTransport, "send", lambda self, message: sent.append(message))
    monkeypatch.setattr(backup_module, "is_docker_available", lambda: False)
    monkeypatch.setattr(agent_client.AgentClient, "available", lambda self: False)
    (tmp_path / "data" / "announcements").mkdir(parents=True)
    db = tmp_path / "data" / "announcements" / "announcements.db"
    store = AdminStore(db)
    store.create_admin("olga", PASSWORD)
    store.create_account("pat", PASSWORD, "publisher")
    subscribers = SubscriberManager(tmp_path / "data" / "announcements" / "subscribers.db")
    subscribers.register_consent_text("2026-11-email", "I agree.")
    _sub, token = subscribers.request_subscription(
        "email",
        "resident@example.org",
        consent_version="2026-11-email",
        consent_source="web_form",
    )
    subscribers.confirm_subscription(token)

    def build_client(connected: bool = False) -> TestClient:
        if connected:
            AnnouncementStorage(db).save_connection("email", dict(CONNECTION), None)
        app = create_app(
            db_path=db,
            feed_dir=tmp_path / "feed",
            config_path=tmp_path / "community-stack.yaml",
            setup_token="unused",
            run_worker=False,
        )
        return TestClient(app, base_url="https://community.test", follow_redirects=False)

    return build_client, tmp_path, sent, db


def sign_in(client, username):
    page = client.get("/admin/login")
    client.post(
        "/admin/login",
        data={"csrf_token": csrf(page), "username": username, "password": PASSWORD},
    )


def test_owner_sees_masked_records_counts_and_the_review_warning(site):
    build_client, _root, _sent, _db = site
    client = build_client()
    sign_in(client, "olga")
    page = client.get("/admin/subscribers")
    assert page.status_code == 200
    assert "Email, active: 1" in page.text
    assert "r***@example.org" in page.text
    visible = re.sub(r'value="[^"]*"', "", page.text)
    assert "resident@example.org" not in visible
    assert "not yet been reviewed" in page.text


def test_publishers_cannot_open_the_subscribers_page(site):
    build_client, _root, _sent, _db = site
    client = build_client()
    sign_in(client, "pat")
    assert client.get("/admin/subscribers").status_code == 403


def test_export_downloads_records_and_is_audited(site):
    build_client, _root, _sent, db = site
    client = build_client()
    sign_in(client, "olga")
    response = client.get("/admin/subscribers/export")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    records = json.loads(response.text)["records"]
    assert records[0]["destination"] == "resident@example.org"
    assert "hash" not in response.text.lower()
    assert AdminStore(db).has_event("subscribers.exported")


def test_erasure_needs_confirmation_then_removes_the_record(site):
    build_client, _root, _sent, db = site
    client = build_client()
    sign_in(client, "olga")
    page = client.get("/admin/subscribers")
    refused = client.post(
        "/admin/subscribers/erase",
        data={"csrf_token": csrf(page), "address": "resident@example.org"},
    )
    assert refused.status_code == 400

    erased = client.post(
        "/admin/subscribers/erase",
        data={"csrf_token": csrf(page), "address": "resident@example.org", "confirm": "yes"},
    )
    assert erased.status_code == 303
    assert SubscriberManager(db.parent / "subscribers.db").export_records() == []


def test_the_audit_log_never_stores_a_full_address(site):
    build_client, _root, _sent, db = site
    client = build_client()
    sign_in(client, "olga")
    page = client.get("/admin/subscribers")
    client.post(
        "/admin/subscribers/erase",
        data={"csrf_token": csrf(page), "address": "resident@example.org", "confirm": "yes"},
    )
    events = AdminStore(db).list_events()
    text = json.dumps(events)
    assert "resident@example.org" not in text
    assert "r***@example.org" in text


def test_test_send_without_a_mail_server_says_so(site):
    build_client, _root, sent, _db = site
    client = build_client()
    sign_in(client, "olga")
    page = client.get("/admin/subscribers")
    response = client.post(
        "/admin/subscribers/test-email",
        data={"csrf_token": csrf(page), "to": "me@example.org"},
    )
    assert response.status_code == 303
    follow = client.get("/admin/subscribers")
    assert "No email server is connected" in follow.text
    assert sent == []


def test_test_send_mails_once_and_subscribes_nobody(site):
    build_client, root, sent, _db = site
    client = build_client(connected=True)
    sign_in(client, "olga")
    page = client.get("/admin/subscribers")
    response = client.post(
        "/admin/subscribers/test-email",
        data={"csrf_token": csrf(page), "to": "me@example.org"},
    )
    assert response.status_code == 303
    assert len(sent) == 1
    assert sent[0]["To"] == "me@example.org"
    assert "List-Unsubscribe" in sent[0]
    records = SubscriberManager(root / "data" / "announcements" / "subscribers.db").export_records()
    assert [r["destination"] for r in records] == ["resident@example.org"]


def test_privacy_notice_is_public_and_marked_as_a_draft(site):
    build_client, _root, _sent, _db = site
    client = build_client(connected=True)
    page = client.get("/subscribe/email/privacy")
    assert page.status_code == 200
    assert "How we use your email address" in page.text
    assert "not yet been reviewed" in page.text
    assert "CNIL" in page.text
    assert "/subscribe/email/privacy" in client.get("/subscribe/email").text
