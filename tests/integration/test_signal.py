"""Experimental Signal delivery through the stack's own signal-cli service."""

import re
from pathlib import Path

import pytest
import yaml
from community_stack.admin import app as app_module
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.announcements.adapters.signal import SIGNAL_API, SignalAdapter, signal_send
from community_stack.announcements.connectors import CONNECTORS
from community_stack.announcements.models import Announcement
from fastapi.testclient import TestClient

GROUP = "group.dGhpcyBpcyBhIGdyb3VwIGlk"


class FakeSignal:
    def __init__(self, accounts=None, groups=None):
        self.calls = []
        self.accounts = accounts if accounts is not None else []
        self.groups = groups or []

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url, payload))
        if url.endswith("/v1/accounts"):
            return 200, self.accounts
        if "/v1/groups/" in url:
            return 200, self.groups
        if "/v1/qrcodelink" in url:
            return 200, b"\x89PNG fake"
        if url.endswith("/v2/send"):
            return 201, [{"timestamp": "1793300000000"}]
        if "/v1/remote-delete/" in url:
            return 201, {"timestamp": "1"}
        return 404, {}


def test_posts_edits_and_deletes_in_the_group():
    fake = FakeSignal(accounts=[{"number": "+33612345678", "aci": "x"}])
    adapter = SignalAdapter("+33612345678", GROUP, transport=fake)
    announcement = Announcement(title="Assembly", body="Tonight at **8**")

    posted = adapter.publish(announcement)
    assert posted.success and posted.external_id == "1793300000000"
    assert fake.calls[0][2] == {
        "message": "Assembly\n\nTonight at 8",
        "number": "+33612345678",
        "recipients": [GROUP],
    }
    assert adapter.update_post(announcement, posted.external_id).success
    assert fake.calls[1][2]["edit_timestamp"] == 1793300000000
    assert adapter.withdraw_post(announcement.id, posted.external_id).success
    assert fake.calls[2][2] == {"recipient": GROUP, "timestamp": 1793300000000}
    assert adapter.health_check().ok


def test_the_client_only_talks_to_the_internal_signal_service():
    status, body = signal_send("GET", "http://169.254.169.254/latest/meta-data", {}, None)
    assert status == 0 and "Refusing" in body["error"]
    assert SIGNAL_API == "http://community_signal:8080"


def test_connector_values_must_come_from_linking():
    with pytest.raises(ValueError, match="Link a Signal account"):
        CONNECTORS["signal"].validate({"number": "nobody", "group_id": GROUP})
    with pytest.raises(ValueError, match="Choose a group"):
        CONNECTORS["signal"].validate({"number": "+33612345678", "group_id": "x"})


def test_signal_api_is_isolated_on_an_internal_network():
    compose = yaml.safe_load(Path("services/signal/docker-compose.yml").read_text())
    signal = compose["services"]["signal"]
    assert "ports" not in signal
    assert compose["networks"]["signal_net"]["external"] is True
    announcements = yaml.safe_load(Path("services/announcements/docker-compose.yml").read_text())
    assert announcements["networks"]["signal_net"]["internal"] is True


@pytest.fixture
def console(tmp_path, monkeypatch):
    fake = FakeSignal()
    monkeypatch.setattr(app_module, "signal_send", fake)
    AdminStore(tmp_path / "announcements.db").create_admin("olga", "a-long-safe-test-password")
    app = create_app(
        db_path=tmp_path / "announcements.db",
        feed_dir=tmp_path / "feed",
        config_path=tmp_path / "missing.yaml",
        setup_token="unused",
        run_worker=False,
    )
    client = TestClient(app, base_url="https://community.test", follow_redirects=False)
    page = client.get("/admin/login")
    client.post(
        "/admin/login",
        data={
            "csrf_token": csrf(page),
            "username": "olga",
            "password": "a-long-safe-test-password",
        },
    )
    return app, client, fake


def csrf(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def test_linking_shows_a_qr_code_then_lets_you_pick_a_group(console):
    app, client, fake = console
    page = client.get("/admin/channels/signal/link")
    assert 'src="/admin/channels/signal/qr"' in page.text
    assert client.get("/admin/channels/signal/qr").content == b"\x89PNG fake"

    fake.accounts = [{"number": "+33612345678", "aci": "x"}]
    fake.groups = [{"id": GROUP, "name": "Neighbours", "internal_id": "i"}]
    page = client.get("/admin/channels/signal/link")
    assert "Neighbours" in page.text
    response = client.post(
        "/admin/channels/signal/use",
        data={
            "csrf_token": csrf(page),
            "number": "+33612345678",
            "group_id": GROUP,
            "group_name": "Neighbours",
        },
    )
    assert response.status_code == 303
    assert app.state.storage.list_connections()["signal"]["group_id"] == GROUP
    assert any(a.name == "signal" for a in app.state.publisher.adapters)
