"""Email preferences (CR2.8): who receives tagged announcements, and who receives a correction."""

import pytest
from community_stack.announcements.adapters.email_newsletter import EmailNewsletterAdapter
from community_stack.announcements.models import Announcement
from community_stack.subscriptions.email_transport import EmailTransport
from community_stack.subscriptions.manager import SubscriberManager

SECRET = b"unit-test-unsubscribe-secret-0123456789"


class Mail(EmailTransport):
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, message) -> None:
        self.sent.append(message["To"])


def _subscribe(mgr, address, topics=None):
    mgr.register_consent_text("2026-11", "I agree.")
    _sub, token = mgr.request_subscription(
        "email",
        address,
        consent_version="2026-11",
        consent_source="web_form",
        topics=topics or [],
    )
    mgr.confirm_subscription(token)


@pytest.fixture
def mgr(tmp_path):
    manager = SubscriberManager(db_path=tmp_path / "subs.db")
    _subscribe(manager, "everything@example.org")
    _subscribe(manager, "important@example.org", topics=["important"])
    return manager


def _adapter(mgr, mail):
    return EmailNewsletterAdapter(subscriber_manager=mgr, transport=mail, unsubscribe_secret=SECRET)


def _ann(ann_id, tags=None, body="Body.") -> Announcement:
    return Announcement(id=ann_id, title="Notice", body=body, tags=tags or [])


def test_untagged_announcement_skips_important_only_subscribers(mgr):
    mail = Mail()
    _adapter(mgr, mail).publish(_ann("ann_plain_0001"))
    assert mail.sent == ["everything@example.org"]


def test_important_announcement_reaches_both(mgr):
    mail = Mail()
    _adapter(mgr, mail).publish(_ann("ann_imp_0001", tags=["Important"]))
    assert sorted(mail.sent) == ["everything@example.org", "important@example.org"]


def test_correction_goes_only_to_people_who_were_sent_the_original(mgr):
    _adapter(mgr, Mail()).publish(_ann("ann_plain_0002"))  # only the everything subscriber got this

    _subscribe(mgr, "late@example.org")  # signed up after, never received it
    notice = Mail()
    _adapter(mgr, notice).withdraw("ann_plain_0002")
    assert notice.sent == ["everything@example.org"]


def test_correction_reaches_everyone_who_got_the_original(mgr):
    adapter_mail = Mail()
    _adapter(mgr, adapter_mail).publish(_ann("ann_imp_0002", tags=["important"]))
    notice = Mail()
    _adapter(mgr, notice).withdraw("ann_imp_0002")
    assert sorted(notice.sent) == ["everything@example.org", "important@example.org"]


def test_correction_for_an_announcement_nobody_received_sends_nothing(mgr):
    mail = Mail()
    result = _adapter(mgr, mail).withdraw("ann_never_sent_0001")
    assert result.success is True
    assert mail.sent == []


def test_preferences_are_stored_with_the_subscriber(mgr):
    records = {r["destination"]: r for r in mgr.export_records()}
    assert records["important@example.org"]["topics"] == ["important"]
    assert records["everything@example.org"]["topics"] == []
