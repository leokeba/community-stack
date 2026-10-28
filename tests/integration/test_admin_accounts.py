"""Accounts, roles, permission checks and the audit log in the admin application."""

import re
import sqlite3

import pytest
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from fastapi.testclient import TestClient

PASSWORD = "a-long-safe-test-password"


def csrf(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


@pytest.fixture
def app(tmp_path):
    store = AdminStore(tmp_path / "announcements.db")
    store.create_admin("owner", PASSWORD)
    return create_app(
        db_path=tmp_path / "announcements.db",
        feed_dir=tmp_path / "feed",
        config_path=tmp_path / "missing.yaml",
        setup_token="unused",
        run_worker=False,
    )


def client_for(app) -> TestClient:
    return TestClient(app, base_url="https://community.test", follow_redirects=False)


def login(app, username: str, password: str = PASSWORD) -> TestClient:
    client = client_for(app)
    page = client.get("/admin/login")
    response = client.post(
        "/admin/login",
        data={"csrf_token": csrf(page), "username": username, "password": password},
    )
    assert response.status_code == 303, response.text
    return client


def post(client: TestClient, path: str, **data):
    token = csrf(client.get("/admin/account"))
    return client.post(path, data={"csrf_token": token, **data})


def add_account(owner: TestClient, username: str, role: str):
    return post(owner, "/admin/accounts/create", username=username, password=PASSWORD, role=role)


def test_account_created_before_roles_existed_becomes_owner(tmp_path):
    db_path = tmp_path / "announcements.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE admin_users (username TEXT PRIMARY KEY, password_salt TEXT NOT NULL,"
            " password_hash TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO admin_users VALUES ('legacy', '00', '00', '2026-01-01')")
    assert AdminStore(db_path).get_account("legacy")["role"] == "owner"


def test_reviewer_can_read_but_not_change_anything(app):
    owner = login(app, "owner")
    assert add_account(owner, "rita", "reviewer").status_code == 303
    reviewer = login(app, "rita")

    assert reviewer.get("/admin").status_code == 200
    assert reviewer.get("/admin/announcements").status_code == 200
    assert "Create a draft" not in reviewer.get("/admin").text
    for path in ("/admin/announcements/new", "/admin/settings", "/admin/accounts", "/admin/audit"):
        assert reviewer.get(path).status_code == 403, path
    denied = post(reviewer, "/admin/announcements/new", title="Nope", body="Not allowed")
    assert denied.status_code == 403


def test_publisher_can_publish_but_not_manage_accounts_or_settings(app):
    owner = login(app, "owner")
    add_account(owner, "pat", "publisher")
    publisher = login(app, "pat")

    created = post(publisher, "/admin/announcements/new", title="Market day", body="Saturday")
    assert created.status_code == 303
    published = post(publisher, f"{created.headers['location']}/publish", confirm="publish")
    assert published.status_code == 303
    assert publisher.get("/admin/accounts").status_code == 403
    assert publisher.get("/admin/settings").status_code == 403
    assert "Accounts" not in publisher.get("/admin").text


def test_admin_cannot_create_or_manage_owners(app):
    owner = login(app, "owner")
    add_account(owner, "ada", "admin")
    admin = login(app, "ada")

    assert add_account(admin, "sneaky", "owner").status_code == 403
    assert post(admin, "/admin/accounts/role", username="owner", role="reviewer").status_code == 403
    assert (
        post(admin, "/admin/accounts/access", username="owner", action="disable").status_code == 403
    )
    assert add_account(admin, "pat", "publisher").status_code == 303
    assert post(admin, "/admin/accounts/role", username="pat", role="reviewer").status_code == 303


def test_last_owner_and_own_account_are_protected(app):
    owner = login(app, "owner")
    assert post(owner, "/admin/accounts/role", username="owner", role="admin").status_code == 400

    add_account(owner, "olga", "owner")
    olga = login(app, "olga")
    post(olga, "/admin/accounts/access", username="owner", action="disable")
    response = post(olga, "/admin/accounts/role", username="olga", role="admin")
    assert response.status_code == 400

    store = app.state.auth_store
    with pytest.raises(ValueError, match="active owner"):
        store.set_role("olga", "admin")


def test_role_changes_and_disabling_apply_to_existing_sessions(app):
    owner = login(app, "owner")
    add_account(owner, "pat", "publisher")
    publisher = login(app, "pat")
    assert publisher.get("/admin/announcements/new").status_code == 200

    post(owner, "/admin/accounts/role", username="pat", role="reviewer")
    assert publisher.get("/admin/announcements/new").status_code == 403

    post(owner, "/admin/accounts/access", username="pat", action="disable")
    assert publisher.get("/admin").status_code == 303
    failed = client_for(app)
    page = failed.get("/admin/login")
    response = failed.post(
        "/admin/login", data={"csrf_token": csrf(page), "username": "pat", "password": PASSWORD}
    )
    assert response.status_code == 401


def test_password_change_requires_current_password_and_ends_other_sessions(app):
    first = login(app, "owner")
    second = login(app, "owner")

    wrong = post(first, "/admin/account", current_password="wrong", new_password="x" * 12)
    assert wrong.status_code == 400
    assert "not correct" in wrong.text

    changed = post(
        first, "/admin/account", current_password=PASSWORD, new_password="another-long-password"
    )
    assert changed.status_code == 303
    assert first.get("/admin").status_code == 200
    assert second.get("/admin").status_code == 303
    login(app, "owner", "another-long-password")


def test_password_reset_signs_the_account_out(app):
    owner = login(app, "owner")
    add_account(owner, "pat", "publisher")
    publisher = login(app, "pat")

    reset = post(owner, "/admin/accounts/password", username="pat", password="fresh-temp-password")
    assert reset.status_code == 303
    assert publisher.get("/admin").status_code == 303
    login(app, "pat", "fresh-temp-password")


def test_audit_log_records_actions_and_escapes_untrusted_values(app):
    attacker = client_for(app)
    page = attacker.get("/admin/login")
    attacker.post(
        "/admin/login",
        data={"csrf_token": csrf(page), "username": "<script>x</script>", "password": "nope"},
    )
    owner = login(app, "owner")
    created = post(owner, "/admin/announcements/new", title="Clinic hours", body="Open Monday")
    post(owner, f"{created.headers['location']}/publish", confirm="publish")

    actions = [event["action"] for event in app.state.auth_store.list_events()]
    assert actions[:4] == [
        "announcement.published",
        "announcement.created",
        "login.succeeded",
        "login.failed",
    ]
    log = owner.get("/admin/audit")
    assert log.status_code == 200
    assert "&lt;script&gt;x&lt;/script&gt;" in log.text
    assert "<script>x</script>" not in log.text
    assert "Clinic hours" in log.text
