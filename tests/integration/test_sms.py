"""SMS alerts: gateway delivery, confirmation codes, and the public sign-up pages."""

import re

import pytest
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.announcements.adapters import http
from community_stack.announcements.models import Announcement
from community_stack.announcements.sms import (
    CODE_ATTEMPTS,
    CODES_PER_NUMBER_PER_DAY,
    SmsGatewayAdapter,
    SmsSignup,
    normalize_phone,
)
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.subscriptions.manager import SubscriberManager
from fastapi.testclient import TestClient


class Gateway:
    def __init__(self):
        self.messages = []
        self.status = 200

    def __call__(self, method, url, headers, payload):
        if method == "GET":
            return 200, [{"id": "phone-1"}]
        self.messages.append((headers, payload))
        return self.status, {"id": payload["id"], "state": "Pending"}


def test_phone_numbers_are_normalized_to_international_format():
    assert normalize_phone("+33 6 12 34 56 78") == "+33612345678"
    assert normalize_phone("0033612345678") == "+33612345678"
    with pytest.raises(ValueError, match="international format"):
        normalize_phone("0612345678")


def test_publishing_texts_subscribers_in_batches_with_stable_ids(tmp_path):
    subscribers = SubscriberManager(tmp_path / "subs.db")
    for n in range(60):
        subscribers.add_subscriber("sms", f"+3361234{n:04d}")
    gateway = Gateway()
    adapter = SmsGatewayAdapter(
        "https://api.sms-gate.app",
        "user",
        "secret-pass",
        subscribers,
        stop_url="https://ourtown.org/subscribe/stop",
        transport=gateway,
    )
    announcement = Announcement(title="Boil water notice", body="Until Friday " * 40)

    result = adapter.publish(announcement)

    assert result.success
    assert [len(p["phoneNumbers"]) for _, p in gateway.messages] == [50, 10]
    text = gateway.messages[0][1]["textMessage"]["text"]
    assert text.endswith("Stop: https://ourtown.org/subscribe/stop")
    assert gateway.messages[0][0]["Authorization"].startswith("Basic ")
    first_ids = [p["id"] for _, p in gateway.messages]
    adapter.publish(announcement)
    assert [p["id"] for _, p in gateway.messages[2:]] == first_ids

    gateway.status = 409
    assert adapter.publish(announcement).success
    gateway.status = 503
    failed = adapter.publish(announcement)
    assert not failed.success and failed.retryable and failed.idempotent

    gateway.status = 200
    adapter.withdraw(announcement.id)
    assert gateway.messages[-1][1]["textMessage"]["text"].startswith("Correction:")


def test_codes_are_limited_single_use_and_expire_after_failed_tries(tmp_path):
    signup = SmsSignup(tmp_path / "subs.db", "server-secret")
    code = signup.new_code("+33612345678", "join", "203.0.113.1")
    assert not signup.confirm("+33612345678", "leave", code)
    assert signup.confirm("+33612345678", "join", code)
    assert not signup.confirm("+33612345678", "join", code)

    for _ in range(CODES_PER_NUMBER_PER_DAY - 1):
        signup.new_code("+33612345678", "join", "203.0.113.1")
    with pytest.raises(ValueError, match="Too many codes"):
        signup.new_code("+33612345678", "join", "203.0.113.2")

    code = signup.new_code("+33699999999", "join", "203.0.113.3")
    for _ in range(CODE_ATTEMPTS):
        signup.confirm("+33699999999", "join", "000000")
    assert not signup.confirm("+33699999999", "join", code)


@pytest.fixture
def site(tmp_path, monkeypatch):
    gateway = Gateway()
    monkeypatch.setattr(http, "send", gateway)
    db = tmp_path / "announcements.db"
    AdminStore(db).create_admin("olga", "a-long-safe-test-password")
    AnnouncementStorage(db).save_connection(
        "sms",
        {"base_url": "https://api.sms-gate.app", "username": "user", "password": "secret-pass"},
        None,
    )
    app = create_app(
        db_path=db,
        feed_dir=tmp_path / "feed",
        config_path=tmp_path / "missing.yaml",
        setup_token="unused",
        run_worker=False,
    )
    return app, TestClient(app, base_url="https://ourtown.org", follow_redirects=False), gateway


def csrf(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def texted_code(gateway) -> str:
    return re.match(r"(\d{6}) is your code", gateway.messages[-1][1]["textMessage"]["text"]).group(
        1
    )


def test_people_sign_up_confirm_receive_alerts_and_leave(site):
    app, client, gateway = site
    page = client.get("/subscribe")
    assert "text alerts" in page.text

    no_consent = client.post(
        "/subscribe/join/code", data={"csrf_token": csrf(page), "number": "+33612345678"}
    )
    assert no_consent.status_code == 400 and not gateway.messages

    sent = client.post(
        "/subscribe/join/code",
        data={"csrf_token": csrf(page), "number": "+33 6 12 34 56 78", "consent": "yes"},
    )
    assert "We sent a 6-digit code" in sent.text
    wrong = client.post(
        "/subscribe/join/confirm",
        data={"csrf_token": csrf(sent), "number": "+33612345678", "code": "000000"},
    )
    assert wrong.status_code == 400
    joined = client.post(
        "/subscribe/join/confirm",
        data={"csrf_token": csrf(sent), "number": "+33612345678", "code": texted_code(gateway)},
    )
    assert "You are signed up" in joined.text

    app.state.publisher.publish("Storm warning", "Stay indoors tonight.")
    assert gateway.messages[-1][1]["phoneNumbers"] == ["+33612345678"]

    stop = client.get("/subscribe/stop")
    client.post("/subscribe/leave/code", data={"csrf_token": csrf(stop), "number": "+33612345678"})
    left = client.post(
        "/subscribe/leave/confirm",
        data={"csrf_token": csrf(stop), "number": "+33612345678", "code": texted_code(gateway)},
    )
    assert "You will not receive any more text alerts." in left.text
    count = len(gateway.messages)
    app.state.publisher.publish("Second alert", "Nobody left to text.")
    assert len(gateway.messages) == count


def test_bots_and_forged_forms_get_no_texts(site):
    _, client, gateway = site
    page = client.get("/subscribe")
    client.post(
        "/subscribe/join/code",
        data={"csrf_token": csrf(page), "number": "+33612345678", "consent": "yes", "website": "x"},
    )
    assert not gateway.messages
    forged = client.post(
        "/subscribe/join/code", data={"csrf_token": "forged", "number": "+33612345678"}
    )
    assert forged.status_code == 403


def test_sign_up_is_unavailable_without_an_sms_connection(tmp_path):
    app = create_app(
        db_path=tmp_path / "a.db",
        feed_dir=tmp_path / "feed",
        config_path=tmp_path / "missing.yaml",
        setup_token="unused",
        run_worker=False,
    )
    page = TestClient(app, base_url="https://ourtown.org").get("/subscribe")
    assert "not available yet" in page.text
