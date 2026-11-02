"""Email sign-up: confirmation links, abuse limits, and one-click unsubscribe through the web app."""

import re
from email.message import EmailMessage

import community_stack.admin.app as admin_app
import pytest
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.subscriptions.email_transport import SmtpTransport
from fastapi.testclient import TestClient

CONNECTION = {
    "host": "smtp.example.org",
    "port": "587",
    "username": "announcements",
    "password": "a-long-password",
    "from_address": "announcements@ourtown.org",
    "public_url": "https://ourtown.org",
}


@pytest.fixture
def mail(monkeypatch):
    sent: list[EmailMessage] = []

    def fake_send(self, message):
        sent.append(message)

    monkeypatch.setattr(SmtpTransport, "send", fake_send)
    return sent


@pytest.fixture
def site(tmp_path, mail, monkeypatch):
    monkeypatch.setattr(admin_app, "PRIVACY_NOTICE_REVIEWED", True)
    db = tmp_path / "announcements.db"
    AdminStore(db).create_admin("olga", "a-long-safe-test-password")
    AnnouncementStorage(db).save_connection("email", dict(CONNECTION), None)
    app = create_app(
        db_path=db,
        feed_dir=tmp_path / "feed",
        config_path=tmp_path / "missing.yaml",
        setup_token="unused",
        run_worker=False,
    )
    client = TestClient(app, base_url="https://ourtown.org", follow_redirects=False)
    return app, client, mail, db


def csrf(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def link_in(message: EmailMessage) -> str:
    body = message.get_body(preferencelist=("plain",)).get_content()
    return re.search(r"https://\S+", body).group(0)


def sign_up(client, address="ann@example.org", **extra):
    page = client.get("/subscribe/email")
    data = {"csrf_token": csrf(page), "email": address, "consent": "yes", **extra}
    return client.post("/subscribe/email", data=data)


def subscriber_manager(db):
    from community_stack.subscriptions.manager import SubscriberManager

    return SubscriberManager(db.parent / "subscribers.db")


def test_signup_confirm_and_appear_on_the_recipient_list(site):
    _app, client, mail, db = site
    page = client.get("/subscribe/email")
    assert "Email updates" in page.text and "I agree to receive" in page.text

    response = sign_up(client)
    assert response.status_code == 200
    assert "we have sent it a confirmation link" in response.text
    assert len(mail) == 1 and mail[0]["To"] == "ann@example.org"

    confirm_url = link_in(mail[0])
    path = confirm_url.replace("https://ourtown.org", "")
    assert client.get(path).status_code == 200
    manager = subscriber_manager(db)
    assert manager.get_active_recipients("email") == []  # GET changes nothing

    token = re.search(r"t=([^\s&]+)", path).group(1)
    confirmed = client.post(
        "/subscribe/email/activate", data={"csrf_token": csrf(page), "t": token}
    )
    assert "You are subscribed" in confirmed.text
    assert manager.get_active_recipients("email") == ["ann@example.org"]


def test_confirmation_link_works_once(site):
    _app, client, mail, _db = site
    sign_up(client)
    token = re.search(r"t=([^\s&]+)", link_in(mail[0])).group(1)
    page = client.get("/subscribe/email")
    assert (
        "You are subscribed"
        in client.post(
            "/subscribe/email/activate", data={"csrf_token": csrf(page), "t": token}
        ).text
    )
    again = client.post("/subscribe/email/activate", data={"csrf_token": csrf(page), "t": token})
    assert again.status_code == 400
    assert "already used" in again.text


def test_bots_that_fill_the_hidden_field_get_the_same_answer_and_nothing_is_sent(site):
    _app, client, mail, db = site
    response = sign_up(client, website="http://spam.example")
    assert "we have sent it a confirmation link" in response.text
    assert mail == []
    assert subscriber_manager(db).export_records() == []


def test_consent_is_required(site):
    _app, client, mail, _db = site
    page = client.get("/subscribe/email")
    response = client.post(
        "/subscribe/email", data={"csrf_token": csrf(page), "email": "ann@example.org"}
    )
    assert response.status_code == 400
    assert "Please tick the consent box" in response.text
    assert mail == []


def test_invalid_address_is_refused(site):
    _app, client, mail, _db = site
    response = sign_up(client, address="not-an-address")
    assert response.status_code == 400
    assert "valid email address" in response.text
    assert mail == []


def test_forms_need_the_csrf_token(site):
    _app, client, mail, _db = site
    client.get("/subscribe/email")
    response = client.post(
        "/subscribe/email",
        data={"email": "ann@example.org", "consent": "yes", "csrf_token": "forged"},
    )
    assert response.status_code == 403
    assert mail == []


def test_one_client_is_limited_per_hour(site):
    _app, client, _mail, _db = site
    for n in range(5):
        assert "we have sent it" in sign_up(client, f"p{n}@example.org").text
    limited = sign_up(client, "p6@example.org")
    assert limited.status_code == 429
    assert "Too many sign-up requests" in limited.text


def test_one_address_is_limited_per_day(site):
    _app, client, _mail, _db = site
    for _ in range(3):
        assert "we have sent it" in sign_up(client, "same@example.org").text
    assert sign_up(client, "same@example.org").status_code == 429


def test_unsubscribe_needs_a_button_press_and_a_valid_token(site):
    _app, client, mail, db = site
    sign_up(client)
    token = re.search(r"t=([^\s&]+)", link_in(mail[0])).group(1)
    page = client.get("/subscribe/email")
    client.post("/subscribe/email/activate", data={"csrf_token": csrf(page), "t": token})

    # Opening a personal unsubscribe link changes nothing (mail scanners follow links).
    from community_stack.subscriptions.email_signup import make_unsubscribe_token

    secret = AnnouncementStorage(db).get_setting("subscriber_token_secret", "").encode()
    unsub = make_unsubscribe_token(secret, "ann@example.org")
    opened = client.get(f"/subscribe/email/unsubscribe?t={unsub}")
    assert opened.status_code == 200 and "Yes, unsubscribe me" in opened.text
    assert subscriber_manager(db).get_active_recipients("email") == ["ann@example.org"]

    # The one-click POST needs no cookie, only the signed token.
    client.cookies.clear()
    done = client.post(f"/subscribe/email/unsubscribe?t={unsub}")
    assert "will not receive any more" in done.text
    assert subscriber_manager(db).get_active_recipients("email") == []


def test_tampered_or_foreign_unsubscribe_tokens_are_refused(site):
    _app, client, _mail, db = site
    sign_up(client)
    from community_stack.subscriptions.email_signup import make_unsubscribe_token

    secret = AnnouncementStorage(db).get_setting("subscriber_token_secret", "").encode()
    real = make_unsubscribe_token(secret, "ann@example.org")
    forged_for_other = make_unsubscribe_token(b"not-the-secret", "ann@example.org")
    tampered = real[:-2] + ("AA" if not real.endswith("AA") else "BB")
    for bad in (forged_for_other, tampered, "garbage"):
        assert client.post(f"/subscribe/email/unsubscribe?t={bad}").status_code == 400


def test_every_announcement_email_carries_a_working_personal_link(site, tmp_path):
    _app, client, mail, db = site
    sign_up(client)
    token = re.search(r"t=([^\s&]+)", link_in(mail[0])).group(1)
    page = client.get("/subscribe/email")
    client.post("/subscribe/email/activate", data={"csrf_token": csrf(page), "t": token})
    mail.clear()

    # Built the way production builds it: from the stored connection and secret.
    publisher = AnnouncementPublisher(
        storage=AnnouncementStorage(db),
        config_path=tmp_path / "missing.yaml",
        feed_dir=tmp_path / "feed",
    )
    publisher.publish("Notice", "Hello.")
    assert len(mail) == 1
    url = mail[0]["List-Unsubscribe"].strip("<>")
    assert url.startswith("https://ourtown.org/subscribe/email/unsubscribe?t=")
    assert mail[0]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    client.cookies.clear()
    done = client.post(url.replace("https://ourtown.org", ""))
    assert "will not receive any more" in done.text
    assert subscriber_manager(db).get_active_recipients("email") == []


def test_without_a_mail_server_the_page_says_so_and_refuses_sign_ups(tmp_path, mail):
    db = tmp_path / "announcements.db"
    AdminStore(db).create_admin("olga", "a-long-safe-test-password")
    app = create_app(
        db_path=db,
        feed_dir=tmp_path / "feed",
        config_path=tmp_path / "missing.yaml",
        setup_token="unused",
        run_worker=False,
    )
    client = TestClient(app, base_url="https://ourtown.org", follow_redirects=False)
    assert "not available yet" in client.get("/subscribe/email").text
    # Without a mail server there is no form, so a forged POST is refused outright.
    refused = client.post("/subscribe/email", data={"email": "a@example.org", "consent": "yes"})
    assert refused.status_code in (403, 404)
    assert mail == []


def test_signup_stays_closed_until_privacy_notice_is_reviewed(site, monkeypatch):
    _app, client, mail, _db = site
    monkeypatch.setattr(admin_app, "PRIVACY_NOTICE_REVIEWED", False)
    page = client.get("/subscribe/email")
    assert "not available until the privacy and consent wording has been reviewed" in page.text
    assert '<form method="post" action="/subscribe/email">' not in page.text

    response = client.post(
        "/subscribe/email",
        data={
            "csrf_token": client.cookies.get(admin_app.PREAUTH_CSRF_COOKIE),
            "email": "ann@example.org",
            "consent": "yes",
        },
    )
    assert response.status_code == 404
    assert mail == []


def test_a_failed_confirmation_email_is_reported_and_not_retried_silently(site, monkeypatch):
    from community_stack.subscriptions.email_transport import TransportError

    _app, client, _mail, db = site

    def refuse(self, message):
        raise TransportError("busy", retryable=True)

    monkeypatch.setattr(SmtpTransport, "send", refuse)
    response = sign_up(client)
    assert response.status_code == 503
    assert "could not send the confirmation" in response.text
    assert subscriber_manager(db).get_active_recipients("email") == []


def test_important_only_sign_up_stores_the_preference(site):
    _app, client, mail, db = site
    response = sign_up(client, "vip@example.org", preference="important")
    assert "we have sent it a confirmation link" in response.text
    token = re.search(r"t=([^\s&]+)", link_in(mail[-1])).group(1)
    page = client.get("/subscribe/email")
    client.post("/subscribe/email/activate", data={"csrf_token": csrf(page), "t": token})
    record = next(
        r for r in subscriber_manager(db).export_records() if r["destination"] == "vip@example.org"
    )
    assert record["topics"] == ["important"]


def test_unknown_preference_is_refused(site):
    _app, client, mail, _db = site
    response = sign_up(client, "x@example.org", preference="everything-and-more")
    assert response.status_code == 400
    assert mail == []
