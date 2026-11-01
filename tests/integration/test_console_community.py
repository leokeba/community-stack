"""The console's community profile page: login, role checks, saving through the agent, and refusals."""

import re
from pathlib import Path

import pytest
import yaml
from community_stack import backup as backup_module
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.agent import client as agent_client
from community_stack.agent.operations import AgentContext
from community_stack.agent.server import AgentService
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
PASSWORD = "a-long-safe-test-password"


def csrf(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


@pytest.fixture
def site(tmp_path, monkeypatch):
    (tmp_path / "data" / "matrix").mkdir(parents=True)
    (tmp_path / "data" / "matrix" / "homeserver.yaml").write_text(
        "server_name: x\n", encoding="utf-8"
    )
    (tmp_path / "community-stack.yaml").write_text(
        (REPO / "config/example.yaml")
        .read_text(encoding="utf-8")
        .replace('encryption: "restic"', 'encryption: "none"'),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("RESTIC_PASSWORD=unused\n", encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "data" / "announcements").mkdir(parents=True)
    db = tmp_path / "data" / "announcements" / "announcements.db"
    store = AdminStore(db)
    store.create_admin("olga", PASSWORD)
    store.create_account("pat", PASSWORD, "publisher")

    service = AgentService(AgentContext(project_dir=tmp_path))

    def in_process_call(self, op, actor, params=None, password=None):
        reply = service.handle({"op": op, "actor": actor, "params": params or {}})
        if not reply["ok"]:
            raise agent_client.AgentRefused(reply["error"])
        return reply["result"]

    monkeypatch.setattr(backup_module, "is_docker_available", lambda: False)
    monkeypatch.setattr(agent_client.AgentClient, "call", in_process_call)
    monkeypatch.setattr(agent_client.AgentClient, "available", lambda self: True)
    app = create_app(
        db_path=db,
        feed_dir=tmp_path / "feed",
        config_path=tmp_path / "community-stack.yaml",
        setup_token="unused",
        run_worker=False,
    )
    return TestClient(app, base_url="https://community.test", follow_redirects=False), tmp_path


def sign_in(client, username):
    page = client.get("/admin/login")
    client.post(
        "/admin/login",
        data={"csrf_token": csrf(page), "username": username, "password": PASSWORD},
    )


def form(**overrides):
    data = {
        "name": "Riverside Collective",
        "description": "Neighbours helping each other.",
        "url": "https://community.example.org",
        "logo": "logo.png",
        "contact_email": "hello@example.org",
        "emergency_instructions": "Use the feed if chat is down.",
        "social_platform_0": "instagram",
        "social_url_0": "https://instagram.com/riverside",
        "social_handle_0": "@riverside",
        "independent_website": "on",
        "independent_rss": "on",
        "independent_email": "on",
    }
    data.update(overrides)
    return data


def test_owner_sees_the_profile_form_with_the_current_values(site):
    client, _root = site
    sign_in(client, "olga")
    page = client.get("/admin/community")
    assert page.status_code == 200
    assert "Community profile" in page.text
    assert 'name="social_platform_4"' in page.text  # five social rows


def test_owner_saves_the_profile_through_the_agent(site):
    client, root = site
    sign_in(client, "olga")
    response = client.post(
        "/admin/community", data={"csrf_token": csrf(client.get("/admin/community")), **form()}
    )
    assert response.status_code == 303
    config = yaml.safe_load((root / "community-stack.yaml").read_text(encoding="utf-8"))
    assert config["community"]["name"] == "Riverside Collective"
    assert config["community"]["social"][0]["handle"] == "@riverside"
    assert config["community"]["independent"]["email"] is True
    assert config["community"]["independent"]["matrix"] is False

    # The saved profile is used on the next page load.
    page = client.get("/admin/community")
    assert "Riverside Collective" in page.text


def test_a_publisher_cannot_change_the_profile(site):
    client, root = site
    sign_in(client, "pat")
    before = (root / "community-stack.yaml").read_text(encoding="utf-8")
    assert client.get("/admin/community").status_code == 403
    response = client.post("/admin/community", data={"csrf_token": "forged", **form()})
    assert response.status_code == 403
    assert (root / "community-stack.yaml").read_text(encoding="utf-8") == before


def test_a_forged_form_is_refused(site):
    client, root = site
    sign_in(client, "olga")
    before = (root / "community-stack.yaml").read_text(encoding="utf-8")
    response = client.post("/admin/community", data={"csrf_token": "forged", **form()})
    assert response.status_code == 403
    assert (root / "community-stack.yaml").read_text(encoding="utf-8") == before


def test_invalid_values_show_the_reason_and_change_nothing(site):
    client, root = site
    sign_in(client, "olga")
    before = (root / "community-stack.yaml").read_text(encoding="utf-8")
    page = client.get("/admin/community")
    response = client.post(
        "/admin/community",
        data={"csrf_token": csrf(page), **form(url="http://insecure.example.org")},
    )
    assert response.status_code == 400
    assert "not valid" in response.text
    assert (root / "community-stack.yaml").read_text(encoding="utf-8") == before


def test_the_channel_menu_is_on_the_profile_page(site):
    client, _root = site
    sign_in(client, "olga")
    text = client.get("/admin/community").text
    assert "Matrix chat room" in text
    assert "Fediverse account" in text
