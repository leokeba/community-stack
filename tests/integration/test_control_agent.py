"""The privileged control agent: catalog checks, socket transport, and the console System page."""

import json
import re
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.agent import operations
from community_stack.agent.client import AgentClient, AgentRefused, AgentUnavailable
from community_stack.agent.operations import AgentContext
from community_stack.agent.server import (
    PASSWORD_FAILURE_LIMIT,
    AgentService,
    AgentSocketServer,
    peer_uid,
)
from fastapi.testclient import TestClient

PASSWORD = "a-long-safe-test-password"


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    (root / "data" / "matrix").mkdir(parents=True)
    (root / "data" / "matrix" / "homeserver.yaml").write_text("server_name: x\n", encoding="utf-8")
    (root / "community-stack.yaml").write_text(
        Path("config/example.yaml")
        .read_text(encoding="utf-8")
        .replace('encryption: "restic"', 'encryption: "none"'),
        encoding="utf-8",
    )
    (root / ".env").write_text("RESTIC_PASSWORD=unused\n", encoding="utf-8")
    store = AdminStore(root / "data" / "announcements" / "announcements.db")
    store.create_admin("olga", PASSWORD)
    store.create_account("pat", PASSWORD, "publisher")
    store.create_account("ada", PASSWORD, "admin")
    store.set_disabled("ada", True)
    monkeypatch.setattr(operations.docker, "is_docker_available", lambda: True)
    monkeypatch.setattr(
        operations.docker,
        "list_community_containers",
        lambda: [{"name": "community_synapse", "image": "s", "status": "Up", "state": "running"}],
    )
    return root


@pytest.fixture
def service(project):
    return AgentService(AgentContext(project_dir=project))


@pytest.fixture
def socket_path(service):
    # Unix socket paths are limited to about 100 bytes, shorter than pytest's tmp_path.
    directory = Path(tempfile.mkdtemp(prefix="csa-"))
    path = directory / "agent.sock"
    server = AgentSocketServer(path, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield path
    server.shutdown()
    server.server_close()
    shutil.rmtree(directory, ignore_errors=True)


def audit_entries(project: Path) -> list[dict]:
    path = project / "data" / "agent" / "audit.log"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize(
    ("request_body", "error"),
    [
        ({"op": "shell.run", "actor": "olga"}, "Unknown operation"),
        ({"op": "status.get"}, "acting account"),
        ({"op": "backup.verify", "actor": "olga", "params": {"path": "/etc"}}, "Unknown parameter"),
        (
            {"op": "backup.verify", "actor": "olga", "params": {"snapshot_id": "../../etc"}},
            "Invalid value",
        ),
        ({"op": "status.get", "actor": "pat"}, "may not run"),
        ({"op": "status.get", "actor": "ada"}, "may not run"),
        ({"op": "status.get", "actor": "nobody"}, "may not run"),
        (["not", "an", "object"], "JSON object"),
    ],
)
def test_requests_outside_the_catalog_rules_are_refused(service, project, request_body, error):
    response = service.handle(request_body)
    assert response["ok"] is False
    assert error in response["error"]


def test_status_and_backup_operations_run_for_an_owner(service, project):
    status = service.handle({"op": "status.get", "actor": "olga"})
    assert status["ok"]
    assert status["result"]["containers"][0]["name"] == "community_synapse"

    created = service.handle({"op": "backup.create", "actor": "olga"})
    assert created["ok"], created
    snapshot_id = created["result"]["id"]
    verified = service.handle(
        {"op": "backup.verify", "actor": "olga", "params": {"snapshot_id": snapshot_id}}
    )
    assert verified["ok"] and verified["result"]["ok"], verified

    outcomes = [(entry["op"], entry["outcome"]) for entry in audit_entries(project)]
    assert outcomes[-3:] == [
        ("status.get", "succeeded"),
        ("backup.create", "succeeded"),
        ("backup.verify", "succeeded"),
    ]


def test_restore_drill_needs_the_password_and_cleans_up(service, project):
    service.handle({"op": "backup.create", "actor": "olga"})

    assert "Password" in service.handle({"op": "restore.drill", "actor": "olga"})["error"]
    wrong = service.handle({"op": "restore.drill", "actor": "olga", "password": "wrong"})
    assert wrong["ok"] is False

    drill = service.handle({"op": "restore.drill", "actor": "olga", "password": PASSWORD})
    assert drill["ok"], drill
    assert "Configuration restored and valid." in drill["result"]["checks"]
    drills = project / "backups" / ".restore-drills"
    assert not any(drills.iterdir())

    log = (project / "data" / "agent" / "audit.log").read_text(encoding="utf-8")
    assert PASSWORD not in log
    assert "wrong" not in log


def test_repeated_wrong_passwords_lock_out_dangerous_operations(service):
    for _ in range(PASSWORD_FAILURE_LIMIT):
        service.handle({"op": "restore.drill", "actor": "olga", "password": "wrong"})
    locked = service.handle({"op": "restore.drill", "actor": "olga", "password": PASSWORD})
    assert "Too many" in locked["error"]


def test_only_one_change_runs_at_a_time(service):
    service._change_lock.acquire()
    try:
        busy = service.handle({"op": "backup.create", "actor": "olga"})
        assert "in progress" in busy["error"]
        assert service.handle({"op": "status.get", "actor": "olga"})["ok"]
    finally:
        service._change_lock.release()


def test_agent_never_creates_the_admin_database(tmp_path):
    root = tmp_path / "empty-stack"
    root.mkdir()
    service = AgentService(AgentContext(project_dir=root))
    assert service.handle({"op": "status.get", "actor": "olga"})["ok"] is False
    assert not (root / "data" / "announcements" / "announcements.db").exists()


def test_client_and_server_talk_over_the_socket(socket_path):
    assert socket_path.stat().st_mode & 0o777 == 0o660
    client = AgentClient(socket_path)
    assert client.call("status.get", "olga")["docker_available"] is True
    with pytest.raises(AgentRefused, match="may not run"):
        client.call("status.get", "pat")
    with pytest.raises(AgentUnavailable):
        AgentClient(socket_path.parent / "missing.sock").call("status.get", "olga")


def raw_request(path: Path, payload: bytes) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(str(path))
        try:
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        data = b""
        while chunk := connection.recv(65536):
            data += chunk
    return json.loads(data)


def test_malformed_and_oversized_requests_are_rejected(socket_path):
    assert "Malformed" in raw_request(socket_path, b"{not json")["error"]
    assert "too large" in raw_request(socket_path, b"x" * (70 * 1024))["error"]


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="peer credentials need Linux")
def test_processes_outside_the_allowed_uids_are_refused(service):
    directory = Path(tempfile.mkdtemp(prefix="csa-"))
    path = directory / "agent.sock"
    server = AgentSocketServer(path, service, allowed_uids={-12345})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with socket.socket(socket.AF_UNIX) as probe:
            probe.connect(str(path))
            assert peer_uid(probe) is not None
        with pytest.raises(AgentRefused, match="may not use the agent"):
            AgentClient(path).call("status.get", "olga")
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(directory, ignore_errors=True)


def console(project: Path, agent_socket: Path) -> TestClient:
    app = create_app(
        db_path=project / "data" / "announcements" / "announcements.db",
        feed_dir=project / "feed",
        config_path=project / "missing.yaml",
        setup_token="unused",
        run_worker=False,
        agent_socket=agent_socket,
    )
    return TestClient(app, base_url="https://community.test", follow_redirects=False)


def signed_in(client: TestClient, username: str) -> TestClient:
    page = client.get("/admin/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    client.post(
        "/admin/login", data={"csrf_token": token, "username": username, "password": PASSWORD}
    )
    return client


def csrf(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def test_system_page_explains_a_missing_agent(project, tmp_path):
    owner = signed_in(console(project, tmp_path / "absent.sock"), "olga")
    page = owner.get("/admin/system")
    assert page.status_code == 200
    assert "The system agent is not running" in page.text


def test_system_page_runs_operations_through_the_agent(project, socket_path):
    owner = signed_in(console(project, socket_path), "olga")
    page = owner.get("/admin/system")
    assert "community_synapse" in page.text

    backup = owner.post("/admin/system/backup", data={"csrf_token": csrf(page)})
    assert backup.status_code == 200
    assert "Backup finished." in backup.text
    assert "not encrypted" in backup.text

    drill = owner.post("/admin/system/drill", data={"csrf_token": csrf(page), "password": "wrong"})
    assert drill.status_code == 400
    assert "Password confirmation failed." in drill.text

    actions = [event["action"] for event in owner.app.state.auth_store.list_events()]
    assert actions[:2] == ["system.restore.drill", "system.backup.create"]


def test_system_page_is_limited_to_owners_and_admins(project, socket_path):
    publisher = signed_in(console(project, socket_path), "pat")
    assert publisher.get("/admin/system").status_code == 403
    assert "System" not in publisher.get("/admin").text
