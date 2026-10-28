"""Durable delivery jobs: automatic retries, superseding, cancellation, and the worker."""

import sqlite3
import time
from argparse import Namespace
from datetime import datetime, timedelta, timezone

from community_stack.admin.app import create_app
from community_stack.announcements.adapters.matrix_room import MatrixRoomAdapter
from community_stack.announcements.publisher import AnnouncementPublisher, retry_delay
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.commands import announce_cmd
from fastapi.testclient import TestClient


def scripted_matrix(responses: list[tuple[int, dict]], calls: list[str]) -> MatrixRoomAdapter:
    replies = iter(responses)

    def transport(method, path, headers, payload):
        calls.append(path)
        return next(replies)

    return MatrixRoomAdapter(transport=transport)


def make_jobs_due(db_path) -> None:
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE delivery_jobs SET run_after = ? WHERE status = 'queued'", (past,))


def test_retry_delay_backs_off_exponentially_up_to_one_hour():
    assert [retry_delay(n).total_seconds() for n in range(1, 7)] == [
        30,
        120,
        480,
        1920,
        3600,
        3600,
    ]


def test_temporary_failure_is_queued_and_retried_automatically(tmp_path):
    db_path = tmp_path / "announcements.db"
    storage = AnnouncementStorage(db_path=db_path)
    calls: list[str] = []
    matrix = scripted_matrix([(503, {"error": "down"}), (200, {"event_id": "$ok"})], calls)
    publisher = AnnouncementPublisher(storage=storage, adapters=[matrix])

    announcement, results = publisher.publish("Road closed", "Use the north entrance.")

    assert not results["matrix_room"].success
    [job] = storage.get_active_jobs(announcement.id)
    assert job["status"] == "queued"
    assert job["attempts"] == 1
    assert datetime.fromisoformat(job["run_after"]) > datetime.now(timezone.utc)
    assert publisher.process_due_jobs() == 0

    make_jobs_due(db_path)
    assert publisher.process_due_jobs() == 1

    assert len(calls) == 2
    assert storage.get_active_jobs(announcement.id) == []
    [state] = storage.get_delivery_states(announcement.id)
    assert state["status"] == "delivered"
    assert state["attempts"] == 2


def test_permanent_failure_is_not_retried(tmp_path):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    calls: list[str] = []
    matrix = scripted_matrix([(403, {"error": "forbidden"})], calls)
    publisher = AnnouncementPublisher(storage=storage, adapters=[matrix])

    announcement, _ = publisher.publish("Notice", "Body")

    assert storage.get_active_jobs(announcement.id) == []
    assert storage.get_delivery_states(announcement.id)[0]["status"] == "failed"


def test_retries_stop_after_max_attempts(tmp_path):
    db_path = tmp_path / "announcements.db"
    storage = AnnouncementStorage(db_path=db_path)
    calls: list[str] = []
    matrix = scripted_matrix([(503, {"error": "down"})] * 3, calls)
    publisher = AnnouncementPublisher(storage=storage, adapters=[matrix], max_attempts=2)

    announcement, _ = publisher.publish("Notice", "Body")
    make_jobs_due(db_path)
    publisher.process_due_jobs()
    make_jobs_due(db_path)

    assert publisher.process_due_jobs() == 0
    assert len(calls) == 2
    assert storage.get_active_jobs(announcement.id) == []
    assert storage.get_delivery_states(announcement.id)[0]["status"] == "failed"


def test_update_supersedes_a_pending_publish_and_still_publishes(tmp_path):
    db_path = tmp_path / "announcements.db"
    storage = AnnouncementStorage(db_path=db_path)
    calls: list[str] = []
    matrix = scripted_matrix([(503, {"error": "down"}), (503, {"error": "down"})], calls)
    publisher = AnnouncementPublisher(storage=storage, adapters=[matrix])

    announcement, _ = publisher.publish("Original", "Body")
    publisher.update(announcement.id, title="Corrected")

    [job] = storage.get_active_jobs(announcement.id)
    assert job["operation"] == "publish"
    assert "/send/m.room.message/pub_" in calls[1]


def test_withdrawal_cancels_a_pending_publish_retry(tmp_path):
    db_path = tmp_path / "announcements.db"
    storage = AnnouncementStorage(db_path=db_path)
    calls: list[str] = []
    matrix = scripted_matrix([(503, {"error": "down"}), (200, {"event_id": "$w"})], calls)
    publisher = AnnouncementPublisher(storage=storage, adapters=[matrix])

    announcement, _ = publisher.publish("Mistake", "Sent too early")
    publisher.withdraw(announcement.id)
    make_jobs_due(db_path)

    assert publisher.process_due_jobs() == 0
    assert len(calls) == 2
    assert "/withdraw_" in calls[1]
    assert storage.get_active_jobs(announcement.id) == []


def test_disabling_a_channel_cancels_its_pending_retry(tmp_path):
    db_path = tmp_path / "announcements.db"
    storage = AnnouncementStorage(db_path=db_path)
    calls: list[str] = []
    matrix = scripted_matrix([(503, {"error": "down"})], calls)
    publisher = AnnouncementPublisher(storage=storage, adapters=[matrix])

    announcement, _ = publisher.publish("Notice", "Body")
    storage.set_channel_enabled("matrix_room", False)
    make_jobs_due(db_path)

    assert publisher.process_due_jobs() == 1
    assert len(calls) == 1
    assert storage.get_active_jobs(announcement.id) == []


def test_job_left_running_by_a_crashed_worker_is_reclaimed(tmp_path):
    db_path = tmp_path / "announcements.db"
    storage = AnnouncementStorage(db_path=db_path)
    calls: list[str] = []
    matrix = scripted_matrix([(200, {"event_id": "$ok"})], calls)
    publisher = AnnouncementPublisher(storage=storage, adapters=[matrix])
    announcement = publisher.create_draft("Notice", "Body")
    storage.enqueue_delivery_job(
        announcement.id, "matrix_room", "publish", 5, claim_for=timedelta(minutes=5)
    )
    assert publisher.process_due_jobs() == 0

    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE delivery_jobs SET lease_expires_at = ?", (expired,))

    assert publisher.process_due_jobs() == 1
    assert len(calls) == 1


def test_admin_app_runs_due_jobs_in_the_background(tmp_path):
    db_path = tmp_path / "announcements.db"
    app = create_app(
        db_path=db_path,
        feed_dir=tmp_path / "feed",
        config_path=tmp_path / "missing.yaml",
        setup_token="unused",
        run_worker=True,
        worker_interval=0.05,
    )
    calls: list[str] = []
    app.state.publisher.adapters.append(
        scripted_matrix([(503, {"error": "down"}), (200, {"event_id": "$ok"})], calls)
    )
    announcement, _ = app.state.publisher.publish("Notice", "Body")
    make_jobs_due(db_path)

    with TestClient(app):
        deadline = time.monotonic() + 5
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)

    assert len(calls) == 2
    assert app.state.storage.get_active_jobs(announcement.id) == []


def test_cli_worker_once_runs_due_jobs(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    storage = AnnouncementStorage()
    calls: list[str] = []
    matrix = scripted_matrix([(503, {"error": "down"}), (200, {"event_id": "$ok"})], calls)
    AnnouncementPublisher(storage=storage, adapters=[matrix]).publish("Notice", "Body")
    make_jobs_due(storage.db_path)
    monkeypatch.setattr(
        announce_cmd,
        "AnnouncementPublisher",
        lambda storage, config_path: AnnouncementPublisher(storage=storage, adapters=[matrix]),
    )

    exit_code = announce_cmd.run_announce(
        Namespace(announce_action="worker", once=True), config_path="missing.yaml"
    )

    assert exit_code == 0
    assert "Ran 1 due delivery job(s)." in capsys.readouterr().out
    assert len(calls) == 2
