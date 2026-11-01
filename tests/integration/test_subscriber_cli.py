"""Subscriber CLI: consent for email, export without secrets, and permanent erasure (CR2.10)."""

import json
import os

import pytest
from community_stack.cli import main
from community_stack.subscriptions.manager import SubscriberManager


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def manager(workdir) -> SubscriberManager:
    return SubscriberManager(workdir / "data/announcements/subscribers.db")


def test_email_add_without_a_consent_source_is_refused(workdir, capsys):
    rc = main(["subscribers", "add", "--channel", "email", "--dest", "ann@example.org"])
    assert rc == 1
    assert "--consent-source" in capsys.readouterr().out
    assert manager(workdir).export_records() == []


def test_email_add_records_consent_and_activates(workdir):
    rc = main(
        [
            "subscribers",
            "add",
            "--channel",
            "email",
            "--dest",
            "ann@example.org",
            "--consent-source",
            "paper_form",
        ]
    )
    assert rc == 0
    record = manager(workdir).export_records()[0]
    assert record["status"] == "active"
    assert record["consent_source"] == "paper_form"
    assert record["consent_version"]


def test_export_contains_records_but_no_secrets(workdir, capsys):
    main(
        [
            "subscribers",
            "add",
            "--channel",
            "email",
            "--dest",
            "ann@example.org",
            "--consent-source",
            "paper_form",
        ]
    )
    out = workdir / "exports" / "all.json"
    assert main(["subscribers", "export", "-o", str(out)]) == 0
    payload = json.loads(out.read_text())
    assert payload["format"] == "community-stack/subscribers"
    assert payload["records"][0]["destination"] == "ann@example.org"
    text = out.read_text().lower()
    assert "hash" not in text and "token" not in text
    if os.name == "posix":
        assert out.stat().st_mode & 0o777 == 0o600


def test_delete_needs_confirmation(workdir, capsys):
    main(
        [
            "subscribers",
            "add",
            "--channel",
            "email",
            "--dest",
            "ann@example.org",
            "--consent-source",
            "paper_form",
        ]
    )
    assert main(["subscribers", "delete", "--channel", "email", "--dest", "ann@example.org"]) == 1
    assert "--confirm" in capsys.readouterr().out
    assert len(manager(workdir).export_records()) == 1


def test_delete_erases_the_address_from_every_table(workdir):
    mgr = manager(workdir)
    mgr.register_consent_text("2026-11", "I agree.")
    _sub, token = mgr.request_subscription(
        "email", "ann@example.org", consent_version="2026-11", consent_source="web_form"
    )
    mgr.confirm_subscription(token)
    mgr.queue_email_send("ann_test_0001", "publish", ["ann@example.org"])
    mgr.allow_signup("client-hash", "ann@example.org")

    rc = main(
        ["subscribers", "delete", "--channel", "email", "--dest", "ann@example.org", "--confirm"]
    )
    assert rc == 0
    assert mgr.export_records() == []
    assert mgr.email_send_progress("ann_test_0001", "publish") == (0, 0)
    import sqlite3

    with sqlite3.connect(mgr.db_path) as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM signup_requests WHERE destination = ?", ("ann@example.org",)
        ).fetchone()[0]
    assert remaining == 0


def test_test_email_sends_one_message_and_subscribes_nobody(workdir, monkeypatch, capsys):
    import shutil
    from pathlib import Path

    from community_stack.announcements.storage import AnnouncementStorage
    from community_stack.subscriptions.email_transport import SmtpTransport

    sent = []
    monkeypatch.setattr(SmtpTransport, "send", lambda self, message: sent.append(message))
    shutil.copy(
        Path(__file__).resolve().parents[2] / "config/example.yaml",
        workdir / "community-stack.yaml",
    )
    AnnouncementStorage(workdir / "data/announcements/announcements.db").save_connection(
        "email",
        {
            "host": "smtp.example.org",
            "port": "587",
            "from_address": "announcements@ourtown.org",
            "public_url": "https://ourtown.org",
        },
        None,
    )

    rc = main(["subscribers", "test-email", "--to", "me@example.org"])
    assert rc == 0, capsys.readouterr().out
    assert len(sent) == 1
    assert sent[0]["To"] == "me@example.org"
    assert "List-Unsubscribe" in sent[0]
    assert manager(workdir).export_records() == []


def test_test_email_without_a_mail_server_says_so(workdir, capsys):
    import shutil
    from pathlib import Path

    shutil.copy(
        Path(__file__).resolve().parents[2] / "config/example.yaml",
        workdir / "community-stack.yaml",
    )
    rc = main(["subscribers", "test-email", "--to", "me@example.org"])
    assert rc == 1
    assert "No email server is connected" in capsys.readouterr().out


def test_test_email_rejects_a_bad_address(workdir, capsys):
    rc = main(["subscribers", "test-email", "--to", "not-an-address"])
    assert rc == 1
