"""Scheduled backups, off-site storage, .env editing, and the console Backups page."""

import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.agent import backups, health
from community_stack.agent.operations import AgentContext
from community_stack.agent.server import AgentService, AgentSocketServer
from community_stack.announcements.adapters import http
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.backup import BackupEngine, BackupError
from community_stack.config_edit import set_env_values
from community_stack.secrets import parse_env_file
from fastapi.testclient import TestClient

PASSWORD = "a-long-safe-test-password"
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "stack"
    (root / "data" / "matrix").mkdir(parents=True)
    (root / "community-stack.yaml").write_text(
        Path("config/example.yaml")
        .read_text(encoding="utf-8")
        .replace('encryption: "restic"', 'encryption: "none"'),
        encoding="utf-8",
    )
    (root / ".env").write_text("# secrets\nRESTIC_PASSWORD=correct-horse\n", encoding="utf-8")
    AdminStore(root / "data" / "announcements" / "announcements.db").create_admin("olga", PASSWORD)
    return root


def test_env_values_are_replaced_or_appended_in_place(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# keep\nA=1\nB=2", encoding="utf-8")
    inode = os.stat(env).st_ino
    set_env_values(env, {"B": "x/y+z=", "C": "3"})
    assert env.read_text(encoding="utf-8") == "# keep\nA=1\nB=x/y+z=\nC=3\n"
    assert os.stat(env).st_ino == inode
    for bad in ("has space", "new\nline", "", 'quo"te'):
        with pytest.raises(ValueError):
            set_env_values(env, {"B": bad})


def test_backups_are_due_by_schedule(project):
    context = AgentContext(project)
    now = datetime.now(timezone.utc)
    assert backups.backup_due(context, now)

    context.backup_engine().create_snapshot()
    assert not backups.backup_due(context, now)
    assert backups.backup_due(context, now + timedelta(days=1, minutes=1))


def test_a_failed_backup_is_retried_after_an_hour(project, monkeypatch):
    context = AgentContext(project)
    monkeypatch.setattr(backups, "latest_backup_time", lambda context: None)
    failed_at = datetime.now(timezone.utc)
    backups._write_state(
        context, {"last_result": "failed", "last_attempt": failed_at.isoformat(), "last_error": "x"}
    )
    assert not backups.backup_due(context, failed_at + timedelta(minutes=30))
    assert backups.backup_due(context, failed_at + timedelta(minutes=61))


def test_disabled_backups_are_never_due(project):
    config = project / "community-stack.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "  backups:\n    enabled: true", "  backups:\n    enabled: false"
        ),
        encoding="utf-8",
    )
    assert not backups.backup_due(AgentContext(project), datetime.now(timezone.utc))


def test_scheduled_run_records_success_and_failure(project, monkeypatch):
    service = AgentService(AgentContext(project))
    service.run_scheduled_backup_if_due()
    state = health.read_state(AgentContext(project))
    assert state["last_result"] == "succeeded"
    assert health.backup_check(AgentContext(project))["status"] == health.OK

    def broken(self):
        raise BackupError("bucket unreachable")

    monkeypatch.setattr(BackupEngine, "create_snapshot", broken)
    monkeypatch.setattr(backups, "backup_due", lambda context, now: True)
    service.run_scheduled_backup_if_due()
    check = health.backup_check(AgentContext(project))
    assert check["status"] == health.PROBLEM
    assert "bucket unreachable" in check["details"]
    log = (project / "data" / "agent" / "audit.log").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["outcome"] for line in log] == ["succeeded", "failed"]


def test_scheduled_backup_waits_while_another_change_runs(project, monkeypatch):
    service = AgentService(AgentContext(project))
    calls = []
    monkeypatch.setattr(backups, "run_scheduled_backup", lambda context: calls.append(1))
    service._change_lock.acquire()
    try:
        service.run_scheduled_backup_if_due()
    finally:
        service._change_lock.release()
    assert calls == []


@pytest.fixture
def offsite(project, monkeypatch):
    monkeypatch.setattr(http, "resolve", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(backups.shutil, "which", lambda name, *a, **k: "/usr/bin/restic")
    created = []

    def fake_ensure(self, env):
        created.append(dict(env))

    monkeypatch.setattr(BackupEngine, "_ensure_restic_repository", fake_ensure)
    monkeypatch.setattr(
        BackupEngine,
        "create_snapshot",
        lambda self: type("Snap", (), {"id": "snap_20261029_120000Z"})(),
    )
    monkeypatch.setattr(BackupEngine, "verify_backup", lambda self, snapshot_id=None: (True, []))
    monkeypatch.setattr(backups, "latest_backup_time", lambda context: None)
    return project, created


PARAMS = {
    "endpoint": "https://s3.eu-central-003.backblazeb2.com",
    "bucket": "community-backups",
    "access_key_id": "0031a2b3c4d5e6f",
    "secret_access_key": SECRET,
}


def test_offsite_switch_tests_the_bucket_then_saves_settings(offsite):
    project, created = offsite
    result = backups.use_offsite(AgentContext(project), dict(PARAMS))

    assert "First off-site backup" in result["steps"][-1]
    env = parse_env_file(project / ".env")
    assert env["S3_BUCKET_URL"] == (
        "s3:https://s3.eu-central-003.backblazeb2.com/community-backups/community-stack"
    )
    assert env["RESTIC_PASSWORD"] == "correct-horse"
    assert created[0]["AWS_SECRET_ACCESS_KEY"] == SECRET
    config = yaml.safe_load((project / "community-stack.yaml").read_text(encoding="utf-8"))
    assert config["operations"]["backups"]["target"] == "s3"
    assert config["operations"]["backups"]["encryption"] == "restic"
    status = backups.backups_status(AgentContext(project), {})
    assert status["where"].startswith("Encrypted, off-site: s3.eu-central-003.backblazeb2.com")
    assert SECRET not in json.dumps(status)


def test_a_failing_bucket_leaves_everything_as_it_was(offsite, monkeypatch):
    project, _ = offsite
    env_before = (project / ".env").read_text(encoding="utf-8")
    config_before = (project / "community-stack.yaml").read_text(encoding="utf-8")

    def denied(self, env):
        raise BackupError("restic could not initialize the backup repository: Access Denied")

    monkeypatch.setattr(BackupEngine, "_ensure_restic_repository", denied)
    with pytest.raises(backups.BackupSettingsError, match="stay where they were"):
        backups.use_offsite(AgentContext(project), dict(PARAMS))
    assert (project / ".env").read_text(encoding="utf-8") == env_before
    assert (project / "community-stack.yaml").read_text(encoding="utf-8") == config_before


def test_offsite_needs_the_password_and_never_logs_the_secret(offsite):
    project, _ = offsite
    service = AgentService(AgentContext(project))
    request = {"op": "backups.use_offsite", "actor": "olga", "params": dict(PARAMS)}
    assert "Password" in service.handle(request)["error"]
    assert service.handle({**request, "password": PASSWORD})["ok"]
    log = (project / "data" / "agent" / "audit.log").read_text(encoding="utf-8")
    assert SECRET not in log
    assert "[REDACTED]" in log


def test_private_or_plain_http_endpoints_are_refused(offsite, monkeypatch):
    project, _ = offsite
    monkeypatch.setattr(http, "resolve", lambda host: ["10.0.0.8"])
    with pytest.raises(backups.BackupSettingsError, match="private network"):
        backups.use_offsite(AgentContext(project), dict(PARAMS))
    service = AgentService(AgentContext(project))
    plain = {**PARAMS, "endpoint": "http://s3.example.org"}
    response = service.handle(
        {"op": "backups.use_offsite", "actor": "olga", "params": plain, "password": PASSWORD}
    )
    assert "Invalid value for endpoint" in response["error"]


@pytest.fixture
def console(project):
    storage = AnnouncementStorage(project / "data" / "announcements" / "announcements.db")
    publisher_ann = storage.list_announcements()
    assert publisher_ann == []
    directory = Path(tempfile.mkdtemp(prefix="csb-"))
    server = AgentSocketServer(directory / "agent.sock", AgentService(AgentContext(project)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    app = create_app(
        db_path=project / "data" / "announcements" / "announcements.db",
        feed_dir=project / "feed",
        config_path=project / "community-stack.yaml",
        setup_token="unused",
        run_worker=False,
        agent_socket=directory / "agent.sock",
    )
    client = TestClient(app, base_url="https://community.test", follow_redirects=False)
    page = client.get("/admin/login")
    client.post(
        "/admin/login", data={"csrf_token": csrf(page), "username": "olga", "password": PASSWORD}
    )
    yield app, client
    server.shutdown()
    server.server_close()
    shutil.rmtree(directory, ignore_errors=True)


def csrf(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def test_backups_page_warns_about_local_only_backups(console):
    _, client = console
    page = client.get("/admin/backups")
    assert "same disk as the data" in page.text
    assert "Daily, each checked after it is made" in page.text
    refused = client.post(
        "/admin/backups/offsite", data={"csrf_token": csrf(page), **PARAMS, "password": "wrong"}
    )
    assert refused.status_code == 400
    assert "Password confirmation failed." in refused.text


def test_offline_archive_download_contains_only_published_announcements(console):
    app, client = console
    app.state.publisher.publish("Water point open", "At the square")
    app.state.publisher.create_draft("Secret plan", "Not yet")
    response = client.get("/admin/backups/offline-archive.zip")
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        html = archive.read("index.html").decode()
    assert "Water point open" in html and "Secret plan" not in html


def test_scheduler_thread_stops_cleanly(project, monkeypatch):
    monkeypatch.setattr(backups, "CHECK_EVERY_SECONDS", 0.01)
    calls = []
    service = type("S", (), {"run_scheduled_backup_if_due": lambda self: calls.append(1)})()
    scheduler = backups.BackupScheduler(service)
    scheduler.start()
    time.sleep(0.1)
    scheduler.stopped.set()
    scheduler.join(timeout=1)
    assert calls and not scheduler.is_alive()
