"""Scheduled publication: storage, the worker, time zones, the console and the CLI."""

import re
import sqlite3
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.models import AnnouncementStatus
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.commands import announce_cmd
from fastapi.testclient import TestClient

PASSWORD = "a-long-safe-test-password"


def make_publisher(tmp_path):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    feed = WebFeedAdapter(output_dir=tmp_path / "feed", storage=storage)
    return AnnouncementPublisher(storage=storage, adapters=[feed]), storage


def make_due(db_path) -> None:
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE announcements SET scheduled_at = ? WHERE status = 'scheduled'", (past,)
        )


def soon() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=2)


def test_scheduled_draft_is_published_by_the_worker_when_due(tmp_path):
    publisher, storage = make_publisher(tmp_path)
    draft = publisher.create_draft("Food bank", "Open Saturday at 10")

    scheduled = publisher.schedule(draft.id, soon())
    assert scheduled.status == AnnouncementStatus.SCHEDULED
    assert publisher.process_due_jobs() == 0
    assert not (tmp_path / "feed" / "announcements.html").exists()

    make_due(tmp_path / "announcements.db")
    assert publisher.process_due_jobs() == 1

    stored = storage.get_announcement(draft.id)
    assert stored.status == AnnouncementStatus.PUBLISHED
    assert stored.scheduled_at is None
    assert "Food bank" in (tmp_path / "feed" / "announcements.html").read_text(encoding="utf-8")
    assert publisher.process_due_jobs() == 0


def test_only_one_process_can_claim_a_publication(tmp_path):
    publisher, storage = make_publisher(tmp_path)
    draft = publisher.create_draft("Once", "Only once")
    publisher.schedule(draft.id, soon())

    assert storage.transition_status(draft.id, ("scheduled",), "published")
    assert not storage.transition_status(draft.id, ("scheduled",), "published")
    with pytest.raises(ValueError, match="not a draft"):
        publisher.publish_draft(draft.id)


@pytest.mark.parametrize(
    "when",
    [
        datetime.now(timezone.utc) - timedelta(minutes=5),
        datetime.now() + timedelta(hours=1),
    ],
    ids=["past", "no-time-zone"],
)
def test_invalid_schedule_times_are_rejected(tmp_path, when):
    publisher, _ = make_publisher(tmp_path)
    draft = publisher.create_draft("Notice", "Body")
    with pytest.raises(ValueError):
        publisher.schedule(draft.id, when)


def test_published_announcements_cannot_be_scheduled(tmp_path):
    publisher, _ = make_publisher(tmp_path)
    published, _ = publisher.publish("Notice", "Body")
    with pytest.raises(ValueError, match="Only drafts"):
        publisher.schedule(published.id, soon())


def test_editing_keeps_the_schedule_and_withdrawal_cancels_it(tmp_path):
    publisher, storage = make_publisher(tmp_path)
    draft = publisher.create_draft("Meeting", "Tuesday")
    publisher.schedule(draft.id, soon())

    edited, results = publisher.update(draft.id, body="Wednesday")
    assert results == {}
    assert edited.status == AnnouncementStatus.SCHEDULED

    publisher.withdraw(draft.id)
    make_due(tmp_path / "announcements.db")
    assert publisher.process_due_jobs() == 0
    assert storage.get_announcement(draft.id).status == AnnouncementStatus.WITHDRAWN


def test_unschedule_returns_to_draft(tmp_path):
    publisher, storage = make_publisher(tmp_path)
    draft = publisher.create_draft("Notice", "Body")
    publisher.schedule(draft.id, soon())

    publisher.unschedule(draft.id)

    stored = storage.get_announcement(draft.id)
    assert stored.status == AnnouncementStatus.DRAFT
    assert stored.scheduled_at is None
    with pytest.raises(ValueError, match="not scheduled"):
        publisher.unschedule(draft.id)


def configured_app(tmp_path):
    config = tmp_path / "community-stack.yaml"
    config.write_text(
        Path("config/example.yaml")
        .read_text(encoding="utf-8")
        .replace('timezone: "UTC"', 'timezone: "Europe/Paris"'),
        encoding="utf-8",
    )
    AdminStore(tmp_path / "announcements.db").create_admin("olga", PASSWORD)
    app = create_app(
        db_path=tmp_path / "announcements.db",
        feed_dir=tmp_path / "feed",
        config_path=config,
        setup_token="unused",
        run_worker=False,
    )
    client = TestClient(app, base_url="https://community.test", follow_redirects=False)
    page = client.get("/admin/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    client.post(
        "/admin/login", data={"csrf_token": token, "username": "olga", "password": PASSWORD}
    )
    return app, client


def csrf(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def test_console_schedules_in_the_community_time_zone(tmp_path):
    app, client = configured_app(tmp_path)
    editor = client.get("/admin/announcements/new")
    created = client.post(
        "/admin/announcements/new",
        data={"csrf_token": csrf(editor), "title": "Assembly", "body": "In the square"},
    )
    detail_url = created.headers["location"]
    detail = client.get(detail_url)
    assert "Europe/Paris" in detail.text

    local = (datetime.now(ZoneInfo("Europe/Paris")) + timedelta(days=2)).replace(
        hour=9, minute=30, second=0, microsecond=0
    )
    response = client.post(
        f"{detail_url}/schedule",
        data={"csrf_token": csrf(detail), "scheduled_for": local.strftime("%Y-%m-%dT%H:%M")},
    )
    assert response.status_code == 303

    stored = app.state.storage.get_announcement(detail_url.rsplit("/", 1)[-1])
    assert datetime.fromisoformat(stored.scheduled_at) == local
    page = client.get(detail_url)
    assert "Scheduled to publish on" in page.text
    assert "09:30" in page.text

    cancelled = client.post(f"{detail_url}/unschedule", data={"csrf_token": csrf(page)})
    assert cancelled.status_code == 303
    actions = [event["action"] for event in app.state.auth_store.list_events()]
    assert actions[:2] == ["announcement.unscheduled", "announcement.scheduled"]


def test_console_rejects_a_past_time(tmp_path):
    _, client = configured_app(tmp_path)
    editor = client.get("/admin/announcements/new")
    created = client.post(
        "/admin/announcements/new",
        data={"csrf_token": csrf(editor), "title": "Late", "body": "Too late"},
    )
    detail = client.get(created.headers["location"])
    response = client.post(
        f"{created.headers['location']}/schedule",
        data={"csrf_token": csrf(detail), "scheduled_for": "2020-01-01T10:00"},
    )
    assert response.status_code == 409


def test_cli_schedule_uses_the_community_time_zone(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "community-stack.yaml"
    config.write_text(
        Path(__file__)
        .resolve()
        .parents[2]
        .joinpath("config/example.yaml")
        .read_text(encoding="utf-8")
        .replace('timezone: "UTC"', 'timezone: "America/New_York"'),
        encoding="utf-8",
    )
    draft = AnnouncementPublisher(storage=AnnouncementStorage(), adapters=[]).create_draft(
        "Rally", "Bring signs"
    )
    when = datetime.now(ZoneInfo("America/New_York")) + timedelta(days=1)

    code = announce_cmd.run_announce(
        Namespace(announce_action="schedule", id=draft.id, at=when.strftime("%Y-%m-%d %H:%M")),
        config_path=config,
    )

    assert code == 0
    assert "America/New_York" in capsys.readouterr().out
    stored = AnnouncementStorage().get_announcement(draft.id)
    assert stored.status == AnnouncementStatus.SCHEDULED
