"""Per-person email ledger: retries, audience snapshots, revisions and time budgets (CR2.5)."""

import pytest
from community_stack.announcements.adapters.email_newsletter import EmailNewsletterAdapter
from community_stack.announcements.models import Announcement
from community_stack.subscriptions.email_transport import EmailTransport, TransportError
from community_stack.subscriptions.manager import SubscriberManager

SECRET = b"unit-test-unsubscribe-secret-0123456789"


class Mail(EmailTransport):
    def __init__(self, fail_after: int | None = None, retryable: bool = True) -> None:
        self.fail_after = fail_after
        self.retryable = retryable
        self.sent: list[str] = []

    def send(self, message) -> None:
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise TransportError("server busy", retryable=self.retryable)
        self.sent.append(message["To"])


@pytest.fixture
def subs(tmp_path):
    manager = SubscriberManager(db_path=tmp_path / "subs.db")
    manager.register_consent_text("2026-11", "I agree.")
    for address in ("a@example.org", "b@example.org", "c@example.org"):
        _sub, token = manager.request_subscription(
            "email", address, consent_version="2026-11", consent_source="web_form"
        )
        manager.confirm_subscription(token)
    return manager


def _ann(title="Notice", body="Water is off.") -> Announcement:
    return Announcement(id="ann_ledger_0001", title=title, body=body)


def _adapter(subs, mail, budget=200):
    return EmailNewsletterAdapter(
        unsubscribe_secret=SECRET,
        subscriber_manager=subs,
        transport=mail,
        time_budget_seconds=budget,
    )


def test_full_send_marks_everyone(subs):
    mail = Mail()
    result = _adapter(subs, mail).publish(_ann())
    assert result.success and result.idempotent
    assert sorted(mail.sent) == ["a@example.org", "b@example.org", "c@example.org"]
    assert subs.email_send_progress("ann_ledger_0001", "publish") == (3, 3)


def test_retry_never_remails_anyone(subs):
    first = Mail(fail_after=2)
    assert _adapter(subs, first).publish(_ann()).success is False
    second = Mail()
    assert _adapter(subs, second).publish(_ann()).success is True
    assert len(first.sent) + len(second.sent) == 3
    assert set(first.sent).isdisjoint(second.sent)


def test_someone_who_unsubscribed_mid_send_is_skipped(subs):
    first = Mail(fail_after=1)
    _adapter(subs, first).publish(_ann())
    subs.unsubscribe("email", "b@example.org")
    second = Mail()
    _adapter(subs, second).publish(_ann())
    assert "b@example.org" not in second.sent


def test_signup_after_the_first_attempt_is_not_mailed_on_retry(subs):
    first = Mail(fail_after=1)
    _adapter(subs, first).publish(_ann())
    _sub, token = subs.request_subscription(
        "email", "late@example.org", consent_version="2026-11", consent_source="web_form"
    )
    subs.confirm_subscription(token)
    second = Mail()
    _adapter(subs, second).publish(_ann())
    assert "late@example.org" not in second.sent


def test_a_new_revision_is_sent_once_to_current_subscribers(subs):
    assert _adapter(subs, Mail()).publish(_ann()).success
    fixed = Mail()
    result = _adapter(subs, fixed).update(_ann(body="Water is back on."))
    assert result.success
    assert sorted(fixed.sent) == ["a@example.org", "b@example.org", "c@example.org"]
    again = Mail()
    _adapter(subs, again).update(_ann(body="Water is back on."))
    assert again.sent == []


def test_time_budget_pauses_retryably_and_resumes(subs):
    paused = _adapter(subs, Mail(), budget=-1).publish(_ann())
    assert paused.success is False
    assert paused.retryable is True
    assert "Paused after 0 of 3" in paused.error
    resumed = Mail()
    assert _adapter(subs, resumed).publish(_ann()).success is True
    assert len(resumed.sent) == 3


def test_permanent_refusal_is_not_retried(subs):
    result = _adapter(subs, Mail(fail_after=0, retryable=False)).publish(_ann())
    assert result.success is False
    assert result.retryable is False


def test_empty_audience_is_a_clean_success(tmp_path):
    empty = SubscriberManager(db_path=tmp_path / "empty.db")
    result = _adapter(empty, Mail()).publish(_ann())
    assert result.success is True
    assert result.external_id == "email_no_subscribers"
