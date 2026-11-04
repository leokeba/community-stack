"""Safe Mode in the admin console, through the control agent's socket."""

import re
import shutil
import tempfile
import threading
from pathlib import Path

import pytest
import yaml
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.agent import safemode as safemode_ops
from community_stack.agent.operations import CATALOG, AgentContext, OperationError
from community_stack.agent.server import AgentService, AgentSocketServer
from community_stack.safemode.store import ProtectedRoom, Store
from fastapi.testclient import TestClient

PASSWORD = "a-long-safe-test-password"
BOT = "@safemode:matrix.ourtown.org"
MEMBER = "@member:matrix.ourtown.org"
ROOM = "!ops:matrix.ourtown.org"


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    (root / "data" / "matrix").mkdir(parents=True)
    (root / "data" / "matrix" / "homeserver.yaml").write_text(
        'server_name: "matrix.ourtown.org"\n', encoding="utf-8"
    )
    (root / "data" / "safemode").mkdir(parents=True)
    (root / "data" / "safemode" / "config.yaml").write_text(
        yaml.safe_dump({"bot_user_id": BOT}), encoding="utf-8"
    )
    config = yaml.safe_load(Path("config/example.yaml").read_text(encoding="utf-8"))
    config["services"]["safemode"] = {"enabled": True}
    (root / "community-stack.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    AdminStore(root / "data" / "announcements" / "announcements.db").create_admin("olga", PASSWORD)
    store = Store(root / "data" / "safemode" / "safemode.db")
    store.upsert_room(ProtectedRoom(ROOM, "Ops group", True, True, "joined", "11"))
    store.close()
    # Docker is not used by these tests: the bot container is reported as absent.
    monkeypatch.setattr(safemode_ops, "_bot_status", lambda: "running")
    return root


@pytest.fixture
def console(project):
    directory = Path(tempfile.mkdtemp(prefix="csmsm-"))
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
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    client.post(
        "/admin/login", data={"csrf_token": token, "username": "olga", "password": PASSWORD}
    )
    yield client, project
    server.shutdown()
    server.server_close()
    shutil.rmtree(directory, ignore_errors=True)


def _token(client, path):
    page = client.get(path)
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1), page


def test_the_page_lists_protected_rooms_without_membership(console):
    client, _ = console
    _, page = _token(client, "/admin/safe-mode")
    assert page.status_code == 200
    assert "Ops group" in page.text
    assert MEMBER not in page.text


def test_enrolling_and_issuing_a_recovery_code_shows_the_code_once(console, project):
    client, root = console
    token, _ = _token(client, "/admin/safe-mode")
    client.post("/admin/safe-mode/enroll", data={"csrf_token": token, "user_id": MEMBER})

    token, _ = _token(client, "/admin/safe-mode")
    issued = client.post(
        "/admin/safe-mode/recovery-code", data={"csrf_token": token, "user_id": MEMBER}
    )
    code = re.search(
        r"shown only now \(give it to the member privately\): <code>([^<]+)</code>", issued.text
    )
    assert code, "the page should show the new code once"
    secret = code.group(1).strip()
    assert secret.count("-") == 3

    later = client.get("/admin/safe-mode").text
    assert secret not in later
    assert secret not in (root / "data" / "agent" / "audit.log").read_text(encoding="utf-8")
    database = (root / "data" / "safemode" / "safemode.db").read_bytes()
    assert secret.replace("-", "").encode() not in database


def test_restore_is_refused_without_the_admin_password(console):
    client, _ = console
    token, _ = _token(client, "/admin/safe-mode")
    refused = client.post(
        "/admin/safe-mode/restore",
        data={
            "csrf_token": token,
            "user_id": MEMBER,
            "confirm": "yes",
            "password": "wrong-password",
        },
    )
    assert "password" in refused.text.lower()


def test_restore_needs_the_confirmation_box(console):
    client, _ = console
    token, _ = _token(client, "/admin/safe-mode")
    page = client.post(
        "/admin/safe-mode/restore",
        data={"csrf_token": token, "user_id": MEMBER, "password": PASSWORD},
    )
    assert page.status_code == 400
    assert "Confirm the change" in page.text


def test_unknown_room_ids_are_refused_by_the_agent_before_any_write(console, project):
    client, root = console
    token, _ = _token(client, "/admin/safe-mode")
    client.post("/admin/safe-mode/unprotect", data={"csrf_token": token, "room_id": "not-a-room"})
    store = Store(root / "data" / "safemode" / "safemode.db")
    assert [room.room_id for room in store.enabled_rooms()] == [ROOM]
    store.close()


# Agent catalogue -----------------------------------------------------------


def test_every_safe_mode_operation_is_system_permission_and_restore_is_dangerous():
    names = [name for name in CATALOG if name.startswith("safemode.")]
    assert len(names) == 8
    assert all(CATALOG[name].permission == "system" for name in names)
    assert CATALOG["safemode.restore"].risk == "dangerous"
    assert CATALOG["safemode.overview"].risk == "read"


def test_the_catalogue_rejects_malformed_identifiers():
    with pytest.raises(OperationError):
        CATALOG["safemode.enroll"].validate({"user_id": "member"})
    with pytest.raises(OperationError):
        CATALOG["safemode.protect"].validate({"room_id": "!a:b", "name": "x\ny"})


def test_overview_reports_not_set_up_without_creating_a_database(tmp_path):
    (tmp_path / "community-stack.yaml").write_text(
        Path("config/example.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    result = safemode_ops.overview(AgentContext(tmp_path), {})
    assert result["set_up"] is False
    assert not (tmp_path / "data" / "safemode" / "safemode.db").exists()
