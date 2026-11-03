"""Unit tests for email delivery: the SMTP transport, the adapter's failure rules, and the connector."""

import smtplib
from email.message import EmailMessage
from unittest import mock

import pytest
from community_stack.announcements.adapters import http
from community_stack.announcements.adapters.email_newsletter import EmailNewsletterAdapter
from community_stack.announcements.connectors import (
    CONNECTORS,
    build_email_adapter,
)
from community_stack.announcements.models import Announcement
from community_stack.subscriptions.email_transport import (
    MailgunTransport,
    SmtpTransport,
    TransportError,
    UnconfiguredTransport,
)
from community_stack.subscriptions.manager import SubscriberManager

SECRET = b"unit-test-unsubscribe-secret-0123456789"


def _message(to: str = "ann@example.org") -> EmailMessage:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = "Hello"
    msg.set_content("Body")
    return msg


class FakeSMTP:
    """Stands in for smtplib.SMTP and records what was called."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []
        self.sent: list[EmailMessage] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        self.calls.append("close")
        return False

    def starttls(self, context=None) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login:{user}")

    def send_message(self, message: EmailMessage) -> None:
        if self.error:
            raise self.error
        self.sent.append(message)


def _factory(fake: FakeSMTP):
    return lambda host, port, timeout: fake


# SMTP transport


def test_smtp_sends_with_starttls_and_login():
    fake = FakeSMTP()
    SmtpTransport("smtp.example.org", 587, "sender", "secret", smtp_factory=_factory(fake)).send(
        _message()
    )
    assert fake.calls == ["starttls", "login:sender", "close"]
    assert fake.sent[0]["To"] == "ann@example.org"


def test_smtp_skips_login_without_a_user_name():
    fake = FakeSMTP()
    SmtpTransport("smtp.example.org", 587, smtp_factory=_factory(fake)).send(_message())
    assert "login:" not in " ".join(fake.calls)


def test_port_465_is_implicit_tls_so_no_starttls():
    fake = FakeSMTP()
    SmtpTransport("smtp.example.org", 465, smtp_factory=_factory(fake)).send(_message())
    assert "starttls" not in fake.calls


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (smtplib.SMTPResponseException(451, b"try later"), True),
        (smtplib.SMTPResponseException(421, b"busy"), True),
        (smtplib.SMTPResponseException(550, b"no such user"), False),
        (smtplib.SMTPAuthenticationError(535, b"bad login"), False),
        (smtplib.SMTPServerDisconnected("gone"), True),
        (OSError("network unreachable"), True),
    ],
)
def test_smtp_errors_say_whether_to_retry(error, retryable):
    transport = SmtpTransport("smtp.example.org", smtp_factory=_factory(FakeSMTP(error)))
    with pytest.raises(TransportError) as info:
        transport.send(_message())
    assert info.value.retryable is retryable


def test_error_text_never_contains_the_password():
    transport = SmtpTransport(
        "smtp.example.org",
        username="u",
        password="hunter2-secret",
        smtp_factory=_factory(FakeSMTP(OSError("x"))),
    )
    with pytest.raises(TransportError) as info:
        transport.send(_message())
    assert "hunter2" not in str(info.value)


def test_mailgun_sends_message_parts_and_unsubscribe_headers():
    calls = []

    def fake_transport(method, url, headers, payload):
        calls.append((method, url, headers, payload))
        return 200, {"id": "message-id"}

    message = _message()
    message["From"] = "Community <announcements@example.org>"
    message["List-Unsubscribe"] = "<https://community.example.org/unsubscribe?t=token>"
    message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message.add_alternative("<p>Hello</p>", subtype="html")

    MailgunTransport(
        "mg.example.org",
        "key-mailgun-api-secret",
        transport=fake_transport,
    ).send(message)

    method, url, headers, payload = calls[0]
    assert method == "POST"
    assert url == "https://api.mailgun.net/v3/mg.example.org/messages"
    assert headers["Authorization"].startswith("Basic ")
    assert payload.fields == {
        "from": "announcements@example.org",
        "to": "ann@example.org",
        "subject": "Hello",
        "o:tracking": "no",
        "o:tracking-clicks": "no",
        "o:tracking-opens": "no",
        "text": "Body\n",
        "html": "<p>Hello</p>\n",
        "h:List-Unsubscribe": "<https://community.example.org/unsubscribe?t=token>",
        "h:List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def test_mailgun_uses_the_eu_endpoint_for_verification():
    calls = []

    def fake_transport(method, url, headers, payload):
        calls.append((method, url, headers, payload))
        return 200, {"domain": {"name": "mg.example.org"}}

    transport = MailgunTransport(
        "mg.example.org", "key-mailgun-api-secret", region="eu", transport=fake_transport
    )

    assert transport.verify() == "Connected to Mailgun sending domain mg.example.org."
    assert calls[0][0:2] == (
        "GET",
        "https://api.eu.mailgun.net/v4/domains/mg.example.org",
    )


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(400, False), (401, False), (429, True), (503, True), (0, True)],
)
def test_mailgun_errors_are_classified_without_leaking_credentials(status, retryable):
    transport = MailgunTransport(
        "mg.example.org",
        "key-mailgun-api-secret",
        transport=lambda *_args: (status, {"message": "key-mailgun-api-secret"}),
    )
    message = _message()
    message["From"] = "announcements@example.org"
    with pytest.raises(TransportError) as info:
        transport.send(message)
    assert info.value.retryable is retryable
    assert "key-mailgun-api-secret" not in str(info.value)


def test_unconfigured_transport_refuses_without_retry():
    with pytest.raises(TransportError) as info:
        UnconfiguredTransport().send(_message())
    assert info.value.retryable is False


# Adapter failure rules


class ScriptedMail(SmtpTransport):
    """Accepts a set number of messages, then fails. Recipient order is not fixed, so count instead."""

    def __init__(self, accept: int, error: TransportError) -> None:
        self.accept = accept
        self.error = error
        self.accepted: list[str] = []

    def send(self, message) -> None:
        if len(self.accepted) >= self.accept:
            raise self.error
        self.accepted.append(message["To"])


@pytest.fixture
def subscribers(tmp_path):
    manager = SubscriberManager(db_path=tmp_path / "subs.db")
    for address in ("a@example.org", "b@example.org"):
        manager.add_subscriber("email", address)
    return manager


def _ann() -> Announcement:
    return Announcement(title="Notice", body="Water is off.", id="ann_test_0001")


def test_adapter_refuses_to_report_success_without_a_mail_server(subscribers):
    result = EmailNewsletterAdapter(
        unsubscribe_secret=SECRET, subscriber_manager=subscribers
    ).publish(_ann())
    assert result.success is False
    assert result.retryable is False
    assert "No email server" in result.error


def test_each_person_gets_their_own_message(subscribers):
    mail = ScriptedMail(accept=10, error=TransportError("unused", retryable=False))
    result = EmailNewsletterAdapter(
        unsubscribe_secret=SECRET, subscriber_manager=subscribers, transport=mail
    ).publish(_ann())
    assert result.success is True
    assert sorted(mail.accepted) == ["a@example.org", "b@example.org"]


def test_messages_include_a_personal_unsubscribe_link(subscribers):
    adapter = EmailNewsletterAdapter(
        subscriber_manager=subscribers,
        public_base_url="https://community.example.org",
        unsubscribe_secret=SECRET,
    )
    link = adapter.unsubscribe_url("a@example.org")
    assert link.startswith("https://community.example.org/subscribe/email/unsubscribe?t=")
    message = adapter.build_email_message(_ann(), link)
    assert message["List-Unsubscribe"] == f"<{link}>"
    assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert link in message.get_body(preferencelist=("plain",)).get_content()


def test_each_person_gets_a_different_unsubscribe_link(subscribers):
    adapter = EmailNewsletterAdapter(subscriber_manager=subscribers, unsubscribe_secret=SECRET)
    assert adapter.unsubscribe_url("a@example.org") != adapter.unsubscribe_url("b@example.org")


def test_temporary_failure_before_any_send_is_retryable(subscribers):
    mail = ScriptedMail(accept=0, error=TransportError("busy", retryable=True))
    result = EmailNewsletterAdapter(
        unsubscribe_secret=SECRET, subscriber_manager=subscribers, transport=mail
    ).publish(_ann())
    assert result.success is False
    assert result.retryable is True


def test_retry_after_partial_send_mails_only_the_rest(subscribers):
    """People already reached are not mailed again when the job is retried."""
    first = ScriptedMail(accept=1, error=TransportError("busy", retryable=True))
    result = EmailNewsletterAdapter(
        unsubscribe_secret=SECRET, subscriber_manager=subscribers, transport=first
    ).publish(_ann())
    assert result.success is False
    assert result.retryable is True
    assert "after 1 of 2" in result.error

    second = ScriptedMail(accept=10, error=TransportError("unused", retryable=False))
    result = EmailNewsletterAdapter(
        unsubscribe_secret=SECRET, subscriber_manager=subscribers, transport=second
    ).publish(_ann())
    assert result.success is True
    assert len(second.accepted) == 1
    assert sorted(first.accepted + second.accepted) == ["a@example.org", "b@example.org"]


# Connector


GOOD_EMAIL = {
    "provider": "smtp",
    "host": "smtp.example.org",
    "port": "587",
    "username": "announcements",
    "password": "a-long-password",
    "from_address": "announcements@example.org",
    "public_url": "https://community.example.org",
}


def test_email_connector_is_registered_with_its_fields():
    connector = CONNECTORS["email"]
    names = [f.name for f in connector.fields]
    assert names == [
        "provider",
        "host",
        "port",
        "username",
        "password",
        "mailgun_domain",
        "mailgun_api_key",
        "mailgun_region",
        "from_address",
        "public_url",
    ]
    assert next(f for f in connector.fields if f.name == "password").secret is True
    assert next(f for f in connector.fields if f.name == "mailgun_api_key").secret is True


PUBLIC = ["93.184.216.34"]


def test_email_connector_builds_an_adapter_with_a_public_base(tmp_path):
    with mock.patch.object(http, "resolve", return_value=PUBLIC):
        values = CONNECTORS["email"].validate(dict(GOOD_EMAIL))
    adapter = build_email_adapter(values, SubscriberManager(tmp_path / "subs.db"), SECRET)
    assert isinstance(adapter, EmailNewsletterAdapter)
    assert adapter.public_base_url == "https://community.example.org"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host", "localhost"),
        ("host", "not a host"),
        ("port", "70000"),
        ("port", "smtp"),
        ("from_address", "nobody"),
        ("public_url", "http://community.example.org"),
    ],
)
def test_email_connector_rejects_bad_values(field, value):
    values = {**GOOD_EMAIL, field: value}
    with mock.patch.object(http, "resolve", return_value=PUBLIC), pytest.raises(ValueError):
        CONNECTORS["email"].validate(values)


def test_email_host_must_be_public():
    values = {**GOOD_EMAIL, "host": "mail.internal.example"}
    with (
        mock.patch.object(http, "resolve", return_value=["10.0.0.5"]),
        pytest.raises(http.UnsafeURLError, match="private network"),
    ):
        CONNECTORS["email"].validate(values)


def test_email_port_defaults_to_submission_port():
    with mock.patch.object(http, "resolve", return_value=PUBLIC):
        values = CONNECTORS["email"].validate({**GOOD_EMAIL, "port": ""})
    assert values["port"] == "587"


def test_mailgun_connector_validates_and_builds_adapter(tmp_path):
    values = {
        "provider": "mailgun",
        "mailgun_domain": "MG.Example.org",
        "mailgun_api_key": "key-mailgun-api-secret",
        "mailgun_region": "eu",
        "from_address": "announcements@mg.example.org",
        "public_url": "https://community.example.org",
    }
    with mock.patch.object(http, "resolve", return_value=PUBLIC):
        validated = CONNECTORS["email"].validate(values)
    adapter = build_email_adapter(validated, SubscriberManager(tmp_path / "subs.db"), SECRET)

    assert validated["mailgun_domain"] == "mg.example.org"
    assert validated["mailgun_region"] == "eu"
    assert isinstance(adapter.transport, MailgunTransport)
    assert adapter.transport.domain == "mg.example.org"
    assert adapter.transport.region == "eu"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mailgun_domain", "https://mg.example.org"),
        ("mailgun_api_key", "short"),
        ("mailgun_region", "ap"),
    ],
)
def test_mailgun_connector_rejects_bad_values(field, value):
    values = {
        "provider": "mailgun",
        "mailgun_domain": "mg.example.org",
        "mailgun_api_key": "key-mailgun-api-secret",
        "mailgun_region": "us",
        "from_address": "announcements@mg.example.org",
        "public_url": "https://community.example.org",
        field: value,
    }
    with mock.patch.object(http, "resolve", return_value=PUBLIC), pytest.raises(ValueError):
        CONNECTORS["email"].validate(values)
