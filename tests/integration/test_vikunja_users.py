"""Vikunja account creation via the admin console and supported CLI."""

import json
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import pytest
import yaml
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.agent import vikunja_users
from community_stack.agent.operations import CATALOG, AgentContext, OperationError
from community_stack.agent.server import AgentService, AgentSocketServer
from fastapi.testclient import TestClient

PASSWORD = "a-long-safe-test-password"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "stack"
    root.mkdir()
    config = yaml.safe_load(Path("config/example.yaml").read_text(encoding="utf-8"))
    config["services"]["vikunja"]["enabled"] = True
    (root / "community-stack.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    AdminStore(root / "data" / "announcements" / "announcements.db").create_admin("olga", PASSWORD)
    return root


def test_vikunja_create_operation_validates_username_and_email():
    operation = CATALOG["vikunja.users.create"]
    assert operation.validate({"username": "community_admin", "email": "admin@example.org"}) == {
        "username": "community_admin",
        "email": "admin@example.org",
    }
    for params in (
        {"username": "--help", "email": "admin@example.org"},
        {"username": "valid", "email": "not-an-email"},
        {"username": "valid", "email": "--flag@example.org"},
    ):
        with pytest.raises(OperationError, match="Invalid value"):
            operation.validate(params)


def test_cli_prompt_interaction_does_not_log_password(tmp_path):
    secret = "temporary-password-that-must-not-leak"
    received = tmp_path / "received.json"
    script = tmp_path / "fake_vikunja.py"
    script.write_text(
        "import json, sys\n"
        "values = []\n"
        "for prompt in ('Password: ', 'Repeat password: '):\n"
        "    print(prompt, end='', flush=True)\n"
        "    values.append(sys.stdin.readline().rstrip('\\r\\n'))\n"
        "with open(sys.argv[1], 'w', encoding='utf-8') as output:\n"
        "    json.dump(values, output)\n",
        encoding="utf-8",
    )
    command = [sys.executable, str(script), str(received)]
    assert secret not in command

    vikunja_users._run_user_create(command, secret)

    assert json.loads(received.read_text(encoding="utf-8")) == [secret, secret]


def test_cli_error_redacts_password(tmp_path):
    secret = "temporary-password-that-must-not-leak"
    script = tmp_path / "fake_vikunja.py"
    script.write_text(
        "import sys\n"
        "for prompt in ('Password: ', 'Repeat password: '):\n"
        "    print(prompt, end='', flush=True)\n"
        "    value = sys.stdin.readline().rstrip('\\r\\n')\n"
        "print('password=' + value, file=sys.stderr)\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    with pytest.raises(vikunja_users.VikunjaUserError) as error:
        vikunja_users._run_user_create([sys.executable, str(script)], secret)
    assert secret not in str(error.value)


def test_admin_creates_vikunja_user_and_shows_password_once(project, monkeypatch):
    invoked = []

    def fake_run(command, password):
        invoked.append((command, password))

    monkeypatch.setattr(vikunja_users, "_run_user_create", fake_run)
    directory = Path(tempfile.mkdtemp(prefix="csvk-"))
    server = AgentSocketServer(directory / "agent.sock", AgentService(AgentContext(project)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        app = create_app(
            db_path=project / "data" / "announcements" / "announcements.db",
            feed_dir=project / "feed",
            config_path=project / "community-stack.yaml",
            setup_token="unused",
            run_worker=False,
            agent_socket=directory / "agent.sock",
        )
        client = TestClient(app, base_url="https://community.test", follow_redirects=False)
        login = client.get("/admin/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1)
        client.post(
            "/admin/login", data={"csrf_token": token, "username": "olga", "password": PASSWORD}
        )

        page = client.get("/admin/vikunja-users")
        assert "Create an account" in page.text
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = client.post(
            "/admin/vikunja-users/create",
            data={
                "csrf_token": token,
                "username": "organizer",
                "email": "organizer@example.org",
            },
        )

        temporary_password = re.search(
            r"shown only now: <code>([^<]+)</code>", response.text
        ).group(1)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert len(invoked) == 1
        command, supplied_password = invoked[0]
        assert supplied_password == temporary_password
        assert temporary_password not in command
        assert "organizer@example.org" in command

        events = app.state.auth_store.list_events()
        assert temporary_password not in json.dumps(events)
        assert "organizer@example.org" not in json.dumps(events)
        agent_log = (project / "data" / "agent" / "audit.log").read_text(encoding="utf-8")
        assert temporary_password not in agent_log
        assert "organizer@example.org" not in agent_log
        assert "[REDACTED]" in agent_log
        assert (
            temporary_password.encode()
            not in (project / "data" / "announcements" / "announcements.db").read_bytes()
        )
        assert temporary_password not in client.get("/admin/vikunja-users").text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        shutil.rmtree(directory, ignore_errors=True)
