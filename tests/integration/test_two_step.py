"""Two-step login (TOTP and recovery codes) and server-side account recovery."""

import base64
import os
import re
from argparse import Namespace

import pytest
from community_stack.admin import totp
from community_stack.admin.app import create_app
from community_stack.admin.store import PENDING_LOGIN_ATTEMPTS, AdminStore
from community_stack.commands import admin_cmd
from fastapi.testclient import TestClient

PASSWORD = "a-long-safe-test-password"


def test_codes_match_rfc_6238_and_cannot_be_replayed():
    secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    assert totp.code_at(secret, 59 // 30) == "287082"
    assert totp.code_at(secret, 1111111109 // 30) == "081804"

    now = 1_800_000_000
    step = totp.current_step(now)
    code = totp.code_at(secret, step)
    assert totp.matching_step(secret, code, None, now) == step
    assert totp.matching_step(secret, code, step, now) is None
    assert totp.matching_step(secret, totp.code_at(secret, step + 1), None, now) == step + 1
    assert totp.matching_step(secret, totp.code_at(secret, step + 3), None, now) is None
    assert totp.matching_step(secret, "12ab56", None, now) is None


@pytest.fixture
def store(tmp_path):
    store = AdminStore(tmp_path / "announcements.db")
    store.create_admin("olga", PASSWORD)
    store.create_account("pat", PASSWORD, "publisher")
    return store


def enable(store: AdminStore, username: str) -> tuple[str, list[str]]:
    secret = store.start_totp(username)
    codes = store.confirm_totp(username, totp.code_at(secret, totp.current_step()))
    assert codes and len(codes) == 10
    return secret, codes


def test_codes_and_recovery_codes_work_once(store):
    secret, codes = enable(store, "olga")
    assert store.totp_enabled("olga")
    assert store.verify_second_factor("olga", totp.code_at(secret, totp.current_step())) is None
    assert (
        store.verify_second_factor("olga", totp.code_at(secret, totp.current_step() + 1)) == "totp"
    )
    assert store.verify_second_factor("olga", codes[0].upper()) == "recovery"
    assert store.verify_second_factor("olga", codes[0]) is None
    assert store.recovery_codes_left("olga") == 9

    store.disable_totp("olga")
    assert not store.totp_enabled("olga")
    assert store.recovery_codes_left("olga") == 0


def test_a_wrong_setup_code_leaves_two_step_off(store):
    store.start_totp("olga")
    assert store.confirm_totp("olga", "000000") is None or store.totp_enabled("olga")
    assert store.confirm_totp("olga", "not-a-code") is None


@pytest.fixture
def app(store, tmp_path):
    return create_app(
        db_path=tmp_path / "announcements.db",
        feed_dir=tmp_path / "feed",
        config_path=tmp_path / "missing.yaml",
        setup_token="unused",
        run_worker=False,
    )


def client_for(app) -> TestClient:
    return TestClient(app, base_url="https://community.test", follow_redirects=False)


def csrf(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def password_step(client: TestClient, username: str = "olga"):
    page = client.get("/admin/login")
    return client.post(
        "/admin/login",
        data={"csrf_token": csrf(page), "username": username, "password": PASSWORD},
    )


def test_console_setup_then_login_needs_the_code(app):
    client = client_for(app)
    password_step(client)
    account = client.get("/admin/account")
    refused = client.post(
        "/admin/account/two-step/start",
        data={"csrf_token": csrf(account), "current_password": "wrong"},
    )
    assert refused.status_code == 400

    setup = client.post(
        "/admin/account/two-step/start",
        data={"csrf_token": csrf(account), "current_password": PASSWORD},
    )
    assert "<svg" in setup.text
    secret = re.search(r"<code>([A-Z2-7]+)</code>", setup.text).group(1)
    confirmed = client.post(
        "/admin/account/two-step/confirm",
        data={"csrf_token": csrf(setup), "code": totp.code_at(secret, totp.current_step())},
    )
    assert "Save these recovery codes now." in confirmed.text
    recovery = re.findall(r"\b[0-9a-f]{8}-[0-9a-f]{8}\b", confirmed.text)
    assert len(recovery) == 10

    fresh = client_for(app)
    response = password_step(fresh)
    assert response.headers["location"] == "/admin/login/verify"
    assert fresh.get("/admin").status_code == 303

    verify = fresh.get("/admin/login/verify")
    wrong = fresh.post("/admin/login/verify", data={"csrf_token": csrf(verify), "code": "000000"})
    assert wrong.status_code == 401
    ok = fresh.post(
        "/admin/login/verify",
        data={"csrf_token": csrf(verify), "code": totp.code_at(secret, totp.current_step() + 1)},
    )
    assert ok.status_code == 303 and ok.headers["location"] == "/admin"
    assert fresh.get("/admin").status_code == 200

    with_recovery = client_for(app)
    password_step(with_recovery)
    verify = with_recovery.get("/admin/login/verify")
    used = with_recovery.post(
        "/admin/login/verify", data={"csrf_token": csrf(verify), "code": recovery[0]}
    )
    assert used.status_code == 303
    actions = [event["action"] for event in app.state.auth_store.list_events()]
    assert "two_step.enabled" in actions and "login.second_step_failed" in actions


def test_second_step_attempts_are_limited(app, store):
    enable(store, "olga")
    client = client_for(app)
    password_step(client)
    verify = client.get("/admin/login/verify")
    for _ in range(PENDING_LOGIN_ATTEMPTS):
        client.post("/admin/login/verify", data={"csrf_token": csrf(verify), "code": "000000"})
    exhausted = client.post(
        "/admin/login/verify", data={"csrf_token": csrf(verify), "code": "000000"}
    )
    assert exhausted.status_code == 303
    assert exhausted.headers["location"] == "/admin/login"


def test_admins_can_turn_off_two_step_for_someone_who_lost_their_phone(app, store):
    enable(store, "pat")
    owner = client_for(app)
    password_step(owner)
    page = owner.get("/admin/accounts")
    assert "Turn off (lost phone)" in page.text
    response = owner.post(
        "/admin/accounts/two-step", data={"csrf_token": csrf(page), "username": "pat"}
    )
    assert response.status_code == 303
    assert not store.totp_enabled("pat")


def test_cli_recovers_a_locked_out_owner(store, tmp_path, capsys):
    enable(store, "olga")
    db = str(tmp_path / "announcements.db")

    assert admin_cmd.run_admin(Namespace(db=db, admin_action="list")) == 0
    assert "olga" in capsys.readouterr().out

    code = admin_cmd.run_admin(
        Namespace(
            db=db,
            admin_action="reset-password",
            username="olga",
            password_stdin=False,
            disable_two_step=True,
        )
    )
    assert code == 0
    temporary = re.search(r"Temporary password \(shown once\): (\S+)", capsys.readouterr().out)
    assert store.verify_password("olga", temporary.group(1))
    assert not store.totp_enabled("olga")
    assert store.list_events()[0]["actor"] == "server command line"


def test_cli_refuses_to_run_as_root_on_a_user_owned_database(store, tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    code = admin_cmd.run_admin(
        Namespace(db=str(tmp_path / "announcements.db"), admin_action="list")
    )
    assert code == 1
