"""Health checks, redacted logs through the agent, and the console Health page."""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import namedtuple
from pathlib import Path

import pytest
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.agent import health
from community_stack.agent.operations import AgentContext
from community_stack.agent.server import AgentService, AgentSocketServer
from community_stack.announcements.storage import AnnouncementStorage
from fastapi.testclient import TestClient

PASSWORD = "a-long-safe-test-password"
DiskUsage = namedtuple("DiskUsage", "total used free")
TELEGRAM_TOKEN = "123456789:AAbbccddeeffgghhiijjkkllmmnnoopp_qq"


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    root.mkdir()
    (root / "community-stack.yaml").write_text(
        Path("config/example.yaml")
        .read_text(encoding="utf-8")
        .replace('encryption: "restic"', 'encryption: "none"'),
        encoding="utf-8",
    )
    (root / ".env").write_text(
        "POSTGRES_PASSWORD=super-secret-db-password\nRESTIC_PASSWORD=unused-password\n",
        encoding="utf-8",
    )
    db = root / "data" / "announcements" / "announcements.db"
    AdminStore(db).create_admin("olga", PASSWORD)
    AdminStore(db).create_account("pat", PASSWORD, "publisher")
    AnnouncementStorage(db).save_connection(
        "telegram", {"chat_id": "@ourcommunity", "bot_token": TELEGRAM_TOKEN}, None
    )
    monkeypatch.setattr(health.docker, "is_docker_available", lambda: True)
    monkeypatch.setattr(
        health.docker,
        "list_community_containers",
        lambda: [
            {"name": "community_synapse", "state": "running", "status": "Up 2 hours (healthy)"},
            {"name": "community_element", "state": "running", "status": "Up 2 hours (unhealthy)"},
        ],
    )
    return root


def statuses(checks):
    return {check["name"]: check["status"] for check in checks}


@pytest.mark.parametrize(
    ("free_gb", "expected"), [(50, health.OK), (4, health.WARNING), (1, health.PROBLEM)]
)
def test_disk_thresholds(project, monkeypatch, free_gb, expected):
    monkeypatch.setattr(
        health.shutil, "disk_usage", lambda path: DiskUsage(60 * 1024**3, 0, free_gb * 1024**3)
    )
    assert health.disk_check(AgentContext(project))["status"] == expected


def test_unhealthy_containers_are_problems(project):
    assert statuses(health.container_checks()) == {
        "synapse": health.OK,
        "element": health.PROBLEM,
    }


def test_certificates_warn_before_expiry_and_report_failures(monkeypatch):
    days = {"community.real.org": 40, "matrix.real.org": 5}

    def fake(host):
        if host not in days:
            raise OSError("connection refused")
        return days[host]

    monkeypatch.setattr(health, "certificate_days_left", fake)
    config = {"domain": {"web": "community.real.org", "matrix": "matrix.real.org"}}
    assert statuses(health.certificate_checks(config)) == {
        "Certificate for community.real.org": health.OK,
        "Certificate for matrix.real.org": health.WARNING,
    }
    broken = health.certificate_checks({"domain": {"web": "down.real.org"}})
    assert broken[0]["status"] == health.PROBLEM
    assert health.certificate_checks({"domain": {"web": "community.example.org"}}) == []


def test_backup_age_is_checked(project):
    context = AgentContext(project)
    assert health.backup_check(context)["status"] == health.PROBLEM

    snapshot = context.backup_engine().create_snapshot()
    assert health.backup_check(context)["status"] == health.OK

    archive = project / "backups" / f"{snapshot.id}.tar.gz"
    three_days_ago = time.time() - 3 * 86400
    os.utime(archive, (three_days_ago, three_days_ago))
    check = health.backup_check(context)
    assert check["status"] == health.WARNING
    assert "not created automatically" in check["details"]


def test_restic_times_with_nanoseconds_and_offsets_parse():
    parsed = health.parse_restic_time("2026-10-28T19:40:29.401262123+01:00")
    assert parsed.isoformat() == "2026-10-28T18:40:29.401262+00:00"


def test_logs_are_redacted(project, monkeypatch):
    noisy = (
        "db connection with password super-secret-db-password\n"
        f"POST https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage\n"
        "Authorization: Bearer abc.def.ghi\n"
        "user logged in from 203.0.113.7\n"
    )
    monkeypatch.setattr(
        health.docker,
        "run_compose",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, noisy, ""),
    )
    text = health.logs_get(AgentContext(project), {"service": "matrix", "lines": "50"})["text"]
    assert "super-secret-db-password" not in text
    assert TELEGRAM_TOKEN not in text
    assert "abc.def.ghi" not in text
    assert "203.0.113.7" in text


@pytest.mark.parametrize(
    "params", [{"service": "../../etc"}, {"service": "matrix", "lines": "99999"}, {}]
)
def test_log_requests_are_validated(project, params):
    response = AgentService(AgentContext(project)).handle(
        {"op": "logs.get", "actor": "olga", "params": params}
    )
    assert response["ok"] is False


@pytest.fixture
def console(project, monkeypatch):
    monkeypatch.setattr(
        health.docker,
        "run_compose",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "synapse started\n", ""),
    )
    directory = Path(tempfile.mkdtemp(prefix="csh-"))
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
    yield app
    server.shutdown()
    server.server_close()
    shutil.rmtree(directory, ignore_errors=True)


def signed_in(app, username: str) -> TestClient:
    client = TestClient(app, base_url="https://community.test", follow_redirects=False)
    page = client.get("/admin/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    client.post(
        "/admin/login", data={"csrf_token": token, "username": username, "password": PASSWORD}
    )
    return client


def test_health_page_shows_checks_and_logs(console):
    owner = signed_in(console, "olga")
    page = owner.get("/admin/health")
    assert page.status_code == 200
    assert "Problem" in page.text and "element" in page.text
    logs = owner.get("/admin/health?service=matrix&lines=50")
    assert "synapse started" in logs.text


def test_health_page_is_for_owners_and_admins(console):
    assert signed_in(console, "pat").get("/admin/health").status_code == 403
