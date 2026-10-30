"""Authentication, CSRF, and publishing integration tests for the admin application."""

import re
import sqlite3
from pathlib import Path

from community_stack.admin.app import LOGIN_IP_LIMIT, AdminStore, create_app
from community_stack.announcements.adapters.matrix_room import MatrixRoomAdapter
from fastapi.testclient import TestClient


def extract_csrf(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def make_client(tmp_path: Path) -> tuple[TestClient, Path]:
    feed_dir = tmp_path / "feed"
    app = create_app(
        db_path=tmp_path / "announcements.db",
        feed_dir=feed_dir,
        config_path=tmp_path / "no-config.yaml",
        setup_token="one-time-secret",
    )
    return TestClient(app, base_url="https://community.test", follow_redirects=False), feed_dir


def setup_admin(client: TestClient) -> None:
    setup_page = client.get("/admin/setup")
    assert setup_page.status_code == 200
    csrf_token = extract_csrf(setup_page)
    response = client.post(
        "/admin/setup",
        data={
            "csrf_token": csrf_token,
            "setup_token": "one-time-secret",
            "username": "organizer",
            "password": "a-long-safe-test-password",
        },
    )
    assert response.status_code == 303
    assert "secure" in response.headers["set-cookie"].lower()


def test_admin_setup_auth_csrf_and_publish_flow(tmp_path):
    client, feed_dir = make_client(tmp_path)
    assert client.get("/admin").status_code == 303

    setup_admin(client)
    dashboard = client.get("/admin")
    assert dashboard.status_code == 200
    assert "Needs attention" in dashboard.text
    csrf_token = extract_csrf(dashboard)

    new_page = client.get("/admin/announcements/new")
    create_response = client.post(
        "/admin/announcements/new",
        data={
            "csrf_token": extract_csrf(new_page),
            "title": "Community update",
            "body": "Bring supplies.\nMeet at the library.",
            "author": "Organizers",
        },
    )
    assert create_response.status_code == 303
    detail_url = create_response.headers["location"]
    draft_id = detail_url.rsplit("/", 1)[-1]
    detail = client.get(detail_url)
    assert "Draft" in detail.text
    assert "Public web feed" in detail.text
    assert "Edits change the published post." in detail.text
    assert "Bring supplies." in detail.text
    assert not (feed_dir / "announcements.html").exists()

    blocked = client.post(f"{detail_url}/publish", data={"confirm": "publish"})
    assert blocked.status_code == 403
    not_confirmed = client.post(
        f"{detail_url}/publish",
        data={"csrf_token": csrf_token},
    )
    assert not_confirmed.status_code == 400
    published = client.post(
        f"{detail_url}/publish",
        data={"csrf_token": csrf_token, "confirm": "publish"},
    )
    assert published.status_code == 303
    assert f"/feed/announcements/{draft_id}/" in (feed_dir / "announcements.html").read_text(
        encoding="utf-8"
    )

    edit_page = client.get(f"{detail_url}/edit")
    edit_response = client.post(
        f"{detail_url}/update",
        data={
            "csrf_token": csrf_token,
            "title": "Community update revised",
            "body": "Bring supplies tomorrow.",
            "author": "Organizers",
        },
    )
    assert edit_page.status_code == 200
    assert edit_response.status_code == 303
    assert "Community update revised" in (feed_dir / "announcements.html").read_text(
        encoding="utf-8"
    )

    public_page = feed_dir / "announcements" / draft_id / "index.html"
    assert public_page.is_file()
    withdrawn = client.post(
        f"{detail_url}/withdraw",
        data={"csrf_token": csrf_token, "confirm": "withdraw"},
    )
    assert withdrawn.status_code == 303
    assert not public_page.exists()
    assert "Community update revised" not in (feed_dir / "feed.json").read_text(encoding="utf-8")

    logout = client.post("/admin/logout", data={"csrf_token": csrf_token})
    assert logout.status_code == 303
    assert client.get("/admin/announcements").status_code == 303

    login_page = client.get("/admin/login")
    login = client.post(
        "/admin/login",
        data={
            "csrf_token": extract_csrf(login_page),
            "username": "organizer",
            "password": "a-long-safe-test-password",
        },
    )
    assert login.status_code == 303
    assert client.get("/admin").status_code == 200


def test_login_rate_limit_and_single_admin_bootstrap(tmp_path):
    db_path = tmp_path / "announcements.db"
    auth_store = AdminStore(db_path)
    auth_store.create_admin("organizer", "a-long-safe-test-password")
    client = TestClient(
        create_app(
            db_path=db_path,
            feed_dir=tmp_path / "feed",
            config_path=tmp_path / "missing.yaml",
            setup_token="unused",
        ),
        base_url="https://community.test",
        follow_redirects=False,
    )
    login_page = client.get("/admin/login")
    csrf_token = extract_csrf(login_page)
    for attempt in range(5):
        response = client.post(
            "/admin/login",
            data={
                "csrf_token": csrf_token,
                "username": "organizer",
                "password": f"wrong-password-{attempt}",
            },
        )
        assert response.status_code == 401
    limited = client.post(
        "/admin/login",
        data={"csrf_token": csrf_token, "username": "organizer", "password": "wrong"},
    )
    assert limited.status_code == 429

    setup_page = client.get("/admin/setup")
    assert setup_page.status_code == 303


def test_destination_settings_are_csrf_protected_and_rebuild_from_sqlite(tmp_path):
    client, feed_dir = make_client(tmp_path)
    setup_admin(client)
    csrf_token = extract_csrf(client.get("/admin"))

    settings_page = client.get("/admin/settings")
    assert client.post("/admin/settings", data={"enabled_channel": "web_feed"}).status_code == 403
    disabled = client.post("/admin/settings", data={"csrf_token": csrf_token})
    assert settings_page.status_code == 200
    assert disabled.status_code == 303

    response = client.post(
        "/admin/announcements/new",
        data={
            "csrf_token": csrf_token,
            "title": "Stored while feed is off",
            "body": "Canonical content.",
        },
    )
    announcement_url = response.headers["location"]
    announcement_id = announcement_url.rsplit("/", 1)[-1]
    client.post(
        f"{announcement_url}/publish",
        data={"csrf_token": csrf_token, "confirm": "publish"},
    )
    assert not (feed_dir / "announcements.html").exists()

    enabled = client.post(
        "/admin/settings",
        data={"csrf_token": csrf_token, "enabled_channel": "web_feed"},
    )
    assert enabled.status_code == 303
    assert announcement_id in (feed_dir / "feed.json").read_text(encoding="utf-8")


def test_delivery_error_and_safe_retry_are_visible_to_admin(tmp_path):
    app = create_app(
        db_path=tmp_path / "announcements.db",
        feed_dir=tmp_path / "feed",
        config_path=tmp_path / "missing.yaml",
        setup_token="one-time-secret",
    )
    attempts = []

    def transport(method, path, headers, payload):
        attempts.append(path)
        if len(attempts) == 1:
            return 503, {"error": "temporary Matrix outage"}
        return 200, {"event_id": "$recovered"}

    app.state.publisher.adapters.append(MatrixRoomAdapter(transport=transport))
    client = TestClient(app, base_url="https://community.test", follow_redirects=False)
    setup_admin(client)
    csrf_token = extract_csrf(client.get("/admin"))
    editor = client.get("/admin/announcements/new")
    created = client.post(
        "/admin/announcements/new",
        data={
            "csrf_token": extract_csrf(editor),
            "title": "Public alert",
            "body": "A test alert.",
        },
    )
    detail_url = created.headers["location"]
    client.post(
        f"{detail_url}/publish",
        data={"csrf_token": csrf_token, "confirm": "publish"},
    )
    failed_detail = client.get(detail_url)
    assert "temporary Matrix outage" in failed_detail.text
    assert "Retry safe, idempotent failed deliveries" in failed_detail.text
    assert "Scheduled deliveries" in failed_detail.text
    assert "1 of 5" in failed_detail.text

    retried = client.post(f"{detail_url}/retry", data={"csrf_token": csrf_token})
    assert retried.status_code == 303
    assert len(attempts) == 2
    healthy_detail = client.get(detail_url)
    assert "temporary Matrix outage" in healthy_detail.text
    assert "Delivered" in healthy_detail.text
    assert "Delivery attempt history" in healthy_detail.text


def test_unauthenticated_pages_do_not_create_sessions(tmp_path):
    client, _feed_dir = make_client(tmp_path)
    for _ in range(3):
        assert client.get("/admin/login").status_code == 200
        client.cookies.clear()
    with sqlite3.connect(tmp_path / "announcements.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0] == 0


def test_login_rate_limit_applies_per_client_across_usernames(tmp_path):
    db_path = tmp_path / "announcements.db"
    AdminStore(db_path).create_admin("organizer", "a-long-safe-test-password")
    client = TestClient(
        create_app(
            db_path=db_path,
            feed_dir=tmp_path / "feed",
            config_path=tmp_path / "missing.yaml",
            setup_token="unused",
        ),
        base_url="https://community.test",
        follow_redirects=False,
    )
    csrf_token = extract_csrf(client.get("/admin/login"))
    for attempt in range(LOGIN_IP_LIMIT):
        response = client.post(
            "/admin/login",
            data={"csrf_token": csrf_token, "username": f"guess-{attempt}", "password": "x"},
        )
        assert response.status_code == 401
    limited = client.post(
        "/admin/login",
        data={
            "csrf_token": csrf_token,
            "username": "organizer",
            "password": "a-long-safe-test-password",
        },
    )
    assert limited.status_code == 429


def test_setup_rejects_non_ascii_token_without_server_error(tmp_path):
    client, _feed_dir = make_client(tmp_path)
    csrf_token = extract_csrf(client.get("/admin/setup"))
    response = client.post(
        "/admin/setup",
        data={
            "csrf_token": csrf_token,
            "setup_token": "jeton-é",
            "username": "organizer",
            "password": "a-long-safe-test-password",
        },
    )
    assert response.status_code == 403


def test_console_errors_are_pages_and_public_errors_stay_json(tmp_path):
    client, _feed_dir = make_client(tmp_path)
    setup_admin(client)
    csrf_token = extract_csrf(client.get("/admin"))

    refused = client.post(
        "/admin/announcements/missing/publish",
        data={"csrf_token": csrf_token, "confirm": "publish"},
    )
    assert refused.status_code == 404
    assert refused.headers["content-type"].startswith("text/html")
    assert "Announcement not found." in refused.text
    assert 'aria-label="Administration"' in refused.text

    unknown = client.get("/admin/no-such-page")
    assert unknown.status_code == 404
    assert "This page does not exist." in unknown.text

    assert client.get("/ap/notes/missing").headers["content-type"] == "application/json"


def test_action_results_are_shown_once_on_the_next_page(tmp_path):
    client, _feed_dir = make_client(tmp_path)
    setup_admin(client)
    assert "Welcome, organizer." in client.get("/admin").text

    page = client.get("/admin/accounts")
    created = client.post(
        "/admin/accounts/create",
        data={
            "csrf_token": extract_csrf(page),
            "username": "camille",
            "password": "another-long-password",
            "role": "publisher",
        },
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/admin/accounts"
    assert "Created the account camille." in client.get("/admin/accounts").text
    assert "Created the account camille." not in client.get("/admin/accounts").text

    refused = client.post(
        "/admin/accounts/create",
        data={
            "csrf_token": extract_csrf(page),
            "username": "sami",
            "password": "short",
            "role": "publisher",
        },
    )
    assert refused.status_code == 400
    assert 'class="toast toast-error"' in refused.text


def test_signed_out_pages_use_the_full_width_layout(tmp_path):
    client, _feed_dir = make_client(tmp_path)
    for path in ("/admin/setup", "/admin/login"):
        page = client.get(path)
        assert 'class="auth-shell"' in page.text
        assert 'aria-label="Administration"' not in page.text
        assert "data-console" not in page.text


def test_sessions_from_before_messages_existed_gain_the_column(tmp_path):
    db_path = tmp_path / "announcements.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE admin_sessions (token_hash TEXT PRIMARY KEY, username TEXT,"
            " csrf_token TEXT NOT NULL, expires_at TEXT NOT NULL)"
        )
    store = AdminStore(db_path)
    token, _ = store.new_session(None)
    store.set_flash(token, {"tone": "success", "text": "Saved."})
    assert store.pop_flash(token) == {"tone": "success", "text": "Saved."}
    assert store.pop_flash(token) is None


def test_console_pages_load_nothing_from_other_sites(tmp_path):
    client, _feed_dir = make_client(tmp_path)
    login = client.get("/admin/login")
    assert "default-src 'self'" in login.headers["content-security-policy"]
    assert "style=" not in login.text
    setup_admin(client)
    dashboard = client.get("/admin")
    assert "script-src 'self'" in dashboard.headers["content-security-policy"]
    assert "<style" not in dashboard.text and "style=" not in dashboard.text
    font = client.get("/admin/static/fonts/source-sans-3-latin.woff2")
    assert font.status_code == 200
    assert font.headers["content-type"] == "font/woff2"
    assert "content-security-policy" not in client.get("/health").headers


def test_announcement_list_filters_by_status(tmp_path):
    client, _feed_dir = make_client(tmp_path)
    setup_admin(client)
    for title in ("Water outage", "Assembly"):
        page = client.get("/admin/announcements/new")
        client.post(
            "/admin/announcements/new",
            data={"csrf_token": extract_csrf(page), "title": title, "body": "Details."},
        )
    drafts = client.get("/admin/announcements?status=draft")
    assert "Water outage" in drafts.text and "Assembly" in drafts.text
    published = client.get("/admin/announcements?status=published")
    assert "Water outage" not in published.text
    assert "No announcements with this status." in published.text
    assert "Water outage" in client.get("/admin/announcements?status=nonsense").text
