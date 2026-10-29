"""Announcement templates: create, use, edit, delete, and save from an announcement."""

import re

import pytest
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from fastapi.testclient import TestClient

PASSWORD = "a-long-safe-test-password"


@pytest.fixture
def client(tmp_path):
    store = AdminStore(tmp_path / "announcements.db")
    store.create_admin("olga", PASSWORD)
    store.create_account("rita", PASSWORD, "reviewer")
    app = create_app(
        db_path=tmp_path / "announcements.db",
        feed_dir=tmp_path / "feed",
        config_path=tmp_path / "missing.yaml",
        setup_token="unused",
        run_worker=False,
    )
    return login(app, "olga")


def login(app, username):
    client = TestClient(app, base_url="https://community.test", follow_redirects=False)
    page = client.get("/admin/login")
    client.post(
        "/admin/login", data={"csrf_token": csrf(page), "username": username, "password": PASSWORD}
    )
    return client


def csrf(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def post(client, path, **data):
    return client.post(path, data={"csrf_token": csrf(client.get("/admin/account")), **data})


def test_create_use_edit_and_delete_a_template(client):
    created = post(
        client,
        "/admin/templates/new",
        name="Weekly meeting",
        title="Weekly meeting",
        body="Thursday at **7pm**, community hall.",
        tags="meetings",
    )
    assert created.status_code == 303
    page = client.get("/admin/templates")
    template_id = re.search(r"/admin/templates/(tpl_[A-Za-z0-9_-]+)/edit", page.text).group(1)

    editor = client.get(f"/admin/announcements/new?template={template_id}")
    assert "Thursday at **7pm**, community hall." in editor.text
    assert 'value="meetings"' in editor.text
    assert "Start from a template" in editor.text

    post(
        client,
        f"/admin/templates/{template_id}/update",
        name="Weekly meeting",
        title="Weekly meeting",
        body="Friday now.",
    )
    assert "Friday now." in client.get(f"/admin/announcements/new?template={template_id}").text

    post(client, f"/admin/templates/{template_id}/delete")
    assert client.get(f"/admin/announcements/new?template={template_id}").status_code == 404


def test_templates_follow_announcement_rules(client):
    refused = post(client, "/admin/templates/new", name="Empty", title="", body="x")
    assert refused.status_code == 400
    assert post(client, "/admin/templates/new", name="", title="t", body="b").status_code == 400


def test_save_an_announcement_as_a_template(client):
    created = post(client, "/admin/announcements/new", title="Food bank", body="Saturday 10am")
    post(client, f"{created.headers['location']}/save-as-template", name="Food bank")
    assert "Food bank" in client.get("/admin/templates").text


def test_reviewers_cannot_manage_templates(client):
    reviewer = login(client.app, "rita")
    assert reviewer.get("/admin/templates").status_code == 403
    assert post(reviewer, "/admin/templates/new", name="x", title="t", body="b").status_code == 403
