"""The community's own ActivityPub account: WebFinger, signatures, follows and delivery."""

import json
import re
from email.utils import formatdate

import pytest
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.announcements.adapters import http
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.fediverse import Fediverse, FediverseAdapter
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage
from fastapi.testclient import TestClient

PASSWORD = "a-long-safe-test-password"


@pytest.fixture
def ours(tmp_path):
    return Fediverse(AnnouncementStorage(tmp_path / "ours.db"), "https://ourtown.org")


@pytest.fixture
def remote(tmp_path):
    return Fediverse(
        AnnouncementStorage(tmp_path / "remote.db"), "https://social.example", username="alice"
    )


class Network:
    """Serves the remote actor document and records everything POSTed."""

    def __init__(self, *servers: Fediverse):
        self.servers = servers
        self.posts: list[tuple[str, dict, bytes]] = []
        self.inbox_status = 202

    def __call__(self, method, url, headers, payload):
        if method == "GET":
            for server in self.servers:
                if url == server.actor_id:
                    return 200, server.actor("Alice", "")
            return 404, {}
        self.posts.append((url, headers, payload.content))
        return self.inbox_status, {}


def signed_follow(remote: Fediverse, ours: Fediverse, activity: dict | None = None):
    activity = activity or {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{remote.actor_id}/follow/1",
        "type": "Follow",
        "actor": remote.actor_id,
        "object": ours.actor_id,
    }
    body = json.dumps(activity).encode()
    headers = remote.sign("POST", f"{ours.base_url}/ap/inbox", body)
    return headers, body


def test_webfinger_and_actor_describe_the_account(ours):
    finger = ours.webfinger("acct:announcements@ourtown.org")
    assert finger["links"][0]["href"] == "https://ourtown.org/ap/actor"
    assert ours.webfinger("acct:someone@ourtown.org") is None
    actor = ours.actor("Our Town", "News")
    assert actor["preferredUsername"] == "announcements"
    assert "BEGIN PUBLIC KEY" in actor["publicKey"]["publicKeyPem"]
    assert ours.public_key_pem() == actor["publicKey"]["publicKeyPem"]


def test_a_signed_follow_is_accepted_and_answered(ours, remote):
    network = Network(remote)
    headers, body = signed_follow(remote, ours)

    status, _ = ours.handle_inbox("/ap/inbox", headers, body, network)

    assert status == 202
    assert ours.follower_count() == 1
    inbox, accept_headers, accept_body = network.posts[0]
    assert inbox == remote.actor("Alice", "")["inbox"]
    accept = json.loads(accept_body)
    assert accept["type"] == "Accept" and accept["object"]["type"] == "Follow"
    # The remote server can verify our Accept with our published key.
    remote.verify("POST", "/ap/inbox", accept_headers, accept_body, Network(ours))

    undo = {
        "id": f"{remote.actor_id}/undo/1",
        "type": "Undo",
        "actor": remote.actor_id,
        "object": {"type": "Follow", "object": ours.actor_id},
    }
    headers, body = signed_follow(remote, ours, undo)
    assert ours.handle_inbox("/ap/inbox", headers, body, network)[0] == 202
    assert ours.follower_count() == 0


def test_forged_tampered_stale_or_unsigned_requests_are_refused(ours, remote, tmp_path):
    network = Network(remote)
    headers, body = signed_follow(remote, ours)

    tampered = body.replace(b"Follow", b"Folloz")
    assert ours.handle_inbox("/ap/inbox", headers, tampered, network)[0] == 401

    stale = {**headers, "Date": formatdate(timeval=1_000_000_000, usegmt=True)}
    assert ours.handle_inbox("/ap/inbox", stale, body, network)[0] == 401

    unsigned = {k: v for k, v in headers.items() if k != "Signature"}
    assert ours.handle_inbox("/ap/inbox", unsigned, body, network)[0] == 401

    # Mallory signs a Follow that claims to come from Alice.
    mallory = Fediverse(AnnouncementStorage(tmp_path / "m.db"), "https://evil.example", "mallory")
    forged = {"type": "Follow", "actor": remote.actor_id, "object": ours.actor_id}
    body = json.dumps(forged).encode()
    headers = mallory.sign("POST", "https://ourtown.org/ap/inbox", body)
    assert ours.handle_inbox("/ap/inbox", headers, body, Network(remote, mallory))[0] == 401
    assert ours.follower_count() == 0


def test_publication_reaches_each_server_once_and_failures_are_retryable(ours, tmp_path):
    ours.add_follower(
        "https://a.example/users/1", "https://a.example/users/1/inbox", "https://a.example/inbox"
    )
    ours.add_follower(
        "https://a.example/users/2", "https://a.example/users/2/inbox", "https://a.example/inbox"
    )
    ours.add_follower("https://b.example/users/3", "https://b.example/users/3/inbox", None)
    network = Network()
    storage = ours.storage
    publisher = AnnouncementPublisher(
        storage=storage, adapters=[FediverseAdapter(ours, transport=network)]
    )

    announcement, results = publisher.publish("Street party", "**Saturday** from noon")

    assert results["fediverse"].success
    assert sorted(url for url, _, _ in network.posts) == [
        "https://a.example/inbox",
        "https://b.example/users/3/inbox",
    ]
    create = json.loads(network.posts[0][2])
    assert create["type"] == "Create"
    assert "<strong>Saturday</strong>" in create["object"]["content"]
    assert "Signature" in network.posts[0][1]

    network.inbox_status = 500
    _, edit = publisher.update(announcement.id, body="Sunday from noon")
    assert not edit["fediverse"].success and edit["fediverse"].retryable

    network.inbox_status = 410
    _, removed = publisher.withdraw(announcement.id)
    assert removed["fediverse"].success
    assert json.loads(network.posts[-1][2])["type"] == "Delete"


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "resolve", lambda host: ["93.184.216.34"])
    AdminStore(tmp_path / "announcements.db").create_admin("olga", PASSWORD)
    app = create_app(
        db_path=tmp_path / "announcements.db",
        feed_dir=tmp_path / "feed",
        config_path=tmp_path / "missing.yaml",
        setup_token="unused",
        run_worker=False,
    )
    return app, TestClient(app, base_url="https://community.example.org", follow_redirects=False)


def test_public_endpoints_exist_only_once_the_account_is_connected(console):
    app, client = console
    assert client.get("/ap/actor").status_code == 404

    page = client.get("/admin/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    client.post(
        "/admin/login", data={"csrf_token": token, "username": "olga", "password": PASSWORD}
    )
    channels = client.get("/admin/channels")
    token = re.search(r'name="csrf_token" value="([^"]+)"', channels.text).group(1)
    client.post("/admin/channels/fediverse/save", data={"csrf_token": token, "username": "news"})

    finger = client.get("/.well-known/webfinger?resource=acct:news@community.example.org")
    assert finger.headers["content-type"].startswith("application/jrd+json")
    actor = client.get("/ap/actor")
    assert actor.headers["content-type"].startswith("application/activity+json")
    assert actor.json()["preferredUsername"] == "news"

    app.state.publisher.adapters = [
        a for a in app.state.publisher.adapters if a.name != "fediverse"
    ] + [WebFeedAdapter(output_dir=app.state.publisher.feed_dir, storage=app.state.storage)]
    app.state.publisher.reload_connections()
    announcement, _ = app.state.publisher.publish("Hello fediverse", "First post")
    outbox = client.get("/ap/outbox").json()
    assert outbox["orderedItems"][0]["object"]["id"].endswith(announcement.id)
    assert client.get(f"/ap/notes/{announcement.id}").status_code == 200
    app.state.publisher.withdraw(announcement.id)
    assert client.get(f"/ap/notes/{announcement.id}").status_code == 410

    unsigned = client.post("/ap/inbox", content=b'{"type": "Follow"}')
    assert unsigned.status_code == 401
