"""Matrix account management through the agent, with Synapse's admin API kept private."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.agent import matrix_admin
from community_stack.agent.operations import AgentContext
from community_stack.agent.server import AgentService, AgentSocketServer
from fastapi.testclient import TestClient

PASSWORD = "a-long-safe-test-password"


class FakeSynapse:
    def __init__(self):
        self.users = {"@alice:matrix.ourtown.org": {"displayname": "Alice"}}
        self.calls = []

    def __call__(self, method, path, body=None, token=None):
        self.calls.append((method, path, body, token))
        if path == "/_synapse/admin/v1/register":
            if method == "GET":
                return 200, {"nonce": "n0nce"}
            return 200, {
                "user_id": "@community-stack-admin:matrix.ourtown.org",
                "access_token": "adm",
            }
        if path.startswith("/_synapse/admin/v2/users?"):
            users = [
                {
                    "name": u,
                    "displayname": d.get("displayname"),
                    "admin": False,
                    "deactivated": d.get("deactivated", False),
                }
                for u, d in self.users.items()
            ]
            users.append({"name": "@community-stack-admin:matrix.ourtown.org", "admin": True})
            return 200, {"users": users}
        if path.startswith("/_synapse/admin/v2/users/"):
            user = path.rsplit("/", 1)[-1].replace("%40", "@").replace("%3A", ":")
            if method == "GET":
                return (
                    (200, self.users[user]) if user in self.users else (404, {"error": "not found"})
                )
            self.users[user] = {"displayname": (body or {}).get("displayname")}
            return 201, {}
        if "/reset_password/" in path or "/deactivate/" in path:
            return 200, {}
        return 404, {}


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    (root / "data" / "matrix").mkdir(parents=True)
    (root / "data" / "matrix" / "homeserver.yaml").write_text(
        'server_name: "matrix.ourtown.org"\nregistration_shared_secret: "shh"\n', encoding="utf-8"
    )
    (root / "community-stack.yaml").write_text(
        Path("config/example.yaml").read_text(), encoding="utf-8"
    )
    AdminStore(root / "data" / "announcements" / "announcements.db").create_admin("olga", PASSWORD)
    fake = FakeSynapse()
    monkeypatch.setattr(matrix_admin, "synapse_request", fake)
    return root, fake


def test_the_admin_account_is_created_once_with_a_valid_mac(project):
    root, fake = project
    context = AgentContext(root)
    assert matrix_admin.admin_token(context) == "adm"
    register = fake.calls[1][2]
    import hashlib
    import hmac

    expected = hmac.new(
        b"shh",
        b"\x00".join([b"n0nce", b"community-stack-admin", register["password"].encode(), b"admin"]),
        hashlib.sha1,
    ).hexdigest()
    assert register["mac"] == expected and register["admin"] is True
    credentials = root / "data" / "agent" / "matrix-admin.json"
    assert os.stat(credentials).st_mode & 0o777 == 0o600
    assert matrix_admin.admin_token(context) == "adm"
    assert len(fake.calls) == 2


def test_listing_hides_the_stacks_own_admin_account(project):
    root, _ = project
    users = matrix_admin.list_users(AgentContext(root), {})["users"]
    assert [u["user_id"] for u in users] == ["@alice:matrix.ourtown.org"]


def test_create_reset_and_deactivate(project):
    root, fake = project
    context = AgentContext(root)
    created = matrix_admin.create_user(context, {"username": "bob", "display_name": "Bob"})
    assert (
        created["user_id"] == "@bob:matrix.ourtown.org" and len(created["temporary_password"]) >= 12
    )
    with pytest.raises(matrix_admin.MatrixAdminError, match="already exists"):
        matrix_admin.create_user(context, {"username": "bob"})

    reset = matrix_admin.reset_password(context, {"user_id": "@alice:matrix.ourtown.org"})
    assert reset["temporary_password"]
    assert fake.calls[-1][2]["logout_devices"] is True
    matrix_admin.deactivate_user(context, {"user_id": "@alice:matrix.ourtown.org"})
    assert fake.calls[-1][2] == {"erase": False}


@pytest.mark.parametrize(
    "user_id",
    ["@mallory:other.server", "@community-stack-admin:matrix.ourtown.org", "not-a-user"],
)
def test_only_this_servers_ordinary_accounts_can_be_changed(project, user_id):
    root, _ = project
    with pytest.raises(matrix_admin.MatrixAdminError):
        matrix_admin.reset_password(AgentContext(root), {"user_id": user_id})


def test_resets_and_deactivation_need_the_password_and_passwords_stay_out_of_logs(project):
    root, _ = project
    service = AgentService(AgentContext(root))
    request = {
        "op": "matrix.users.reset_password",
        "actor": "olga",
        "params": {"user_id": "@alice:matrix.ourtown.org"},
    }
    assert "Password" in service.handle(request)["error"]
    reply = service.handle({**request, "password": PASSWORD})
    assert reply["ok"]
    log = (root / "data" / "agent" / "audit.log").read_text()
    assert reply["result"]["temporary_password"] not in log


def test_console_page_shows_accounts_and_temporary_passwords_once(project):
    root, _ = project
    directory = Path(tempfile.mkdtemp(prefix="csmx-"))
    server = AgentSocketServer(directory / "agent.sock", AgentService(AgentContext(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        app = create_app(
            db_path=root / "data" / "announcements" / "announcements.db",
            feed_dir=root / "feed",
            config_path=root / "community-stack.yaml",
            setup_token="unused",
            run_worker=False,
            agent_socket=directory / "agent.sock",
        )
        client = TestClient(app, base_url="https://community.test", follow_redirects=False)
        page = client.get("/admin/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        client.post(
            "/admin/login", data={"csrf_token": token, "username": "olga", "password": PASSWORD}
        )
        page = client.get("/admin/matrix-users")
        assert "@alice:matrix.ourtown.org" in page.text
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        created = client.post(
            "/admin/matrix-users/create", data={"csrf_token": token, "username": "carol"}
        )
        password = re.search(r"shown only now: <code>([^<]+)</code>", created.text).group(1)
        assert password not in json.dumps(app.state.auth_store.list_events())
        assert password not in client.get("/admin/matrix-users").text
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.skipif(
    not os.environ.get("COMMUNITY_STACK_DOCKER_TESTS"),
    reason="set COMMUNITY_STACK_DOCKER_TESTS=1 to run against a real Synapse container",
)
def test_against_a_real_synapse(tmp_path, monkeypatch):
    name, volume = "csmxadmin_synapse", "csmxadmin_data"
    image = "matrixdotorg/synapse:v1.163.2"
    run = lambda *a: subprocess.run(list(a), check=False, capture_output=True, text=True)
    run("docker", "rm", "-f", name)
    run("docker", "volume", "rm", volume)
    run(
        "docker",
        "run",
        "--rm",
        "-v",
        f"{volume}:/data",
        "-e",
        "SYNAPSE_SERVER_NAME=mx.test",
        "-e",
        "SYNAPSE_REPORT_STATS=no",
        image,
        "generate",
    )
    run("docker", "run", "-d", "--name", name, "-v", f"{volume}:/data", image)
    try:
        for _ in range(60):
            if (
                run(
                    "docker", "inspect", name, "--format", "{{.State.Health.Status}}"
                ).stdout.strip()
                == "healthy"
            ):
                break
            time.sleep(2)
        root = tmp_path / "stack"
        (root / "data" / "matrix").mkdir(parents=True)
        run(
            "docker",
            "cp",
            f"{name}:/data/homeserver.yaml",
            str(root / "data" / "matrix" / "homeserver.yaml"),
        )
        monkeypatch.setattr(matrix_admin, "SYNAPSE_CONTAINER", name)
        context = AgentContext(root)
        created = matrix_admin.create_user(context, {"username": "dana", "display_name": "Dana"})
        assert created["user_id"] == "@dana:mx.test"
        assert "@dana:mx.test" in [
            u["user_id"] for u in matrix_admin.list_users(context, {})["users"]
        ]
        matrix_admin.reset_password(context, {"user_id": "@dana:mx.test"})
        matrix_admin.deactivate_user(context, {"user_id": "@dana:mx.test"})
        users = {u["user_id"]: u for u in matrix_admin.list_users(context, {})["users"]}
        assert users["@dana:mx.test"]["deactivated"]
    finally:
        run("docker", "rm", "-f", name)
        run("docker", "volume", "rm", volume)
