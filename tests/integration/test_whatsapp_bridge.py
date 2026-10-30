"""Integration tests validating WhatsApp bridge integration and failure decoupling."""

from pathlib import Path

import yaml
from community_stack.announcements.adapters.matrix_room import MatrixRoomAdapter
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.models import Announcement
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.whatsapp.announcement_forwarder import WhatsAppAnnouncementAdapter


def test_whatsapp_compose_configuration():
    compose_path = Path("services/whatsapp_bridge/docker-compose.yml")
    assert compose_path.is_file()
    with open(compose_path, "r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream)

    assert "whatsapp_bridge" in data.get("services", {})
    wa_service = data["services"]["whatsapp_bridge"]
    assert wa_service.get("image") == "dock.mau.dev/mautrix/whatsapp:v0.11.2"
    assert "bridge_net" in wa_service["networks"]
    assert "ports" not in wa_service
    assert data["networks"]["bridge_net"]["external"] is True
    matrix_compose = yaml.safe_load(
        Path("services/matrix/docker-compose.yml").read_text(encoding="utf-8")
    )
    assert "bridge_net" in matrix_compose["services"]["synapse"]["networks"]


def test_whatsapp_failure_does_not_block_core_announcements(tmp_path):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")
    web_adapter = WebFeedAdapter(output_dir=tmp_path / "web_feed")

    # Mock matrix adapter: succeeds
    matrix_adapter = MatrixRoomAdapter(
        transport=lambda method, path, headers, payload: (200, {"event_id": "$m_123"}),
    )

    # Mock whatsapp adapter: fails (simulating account ban or disconnected WhatsApp Web session)
    def failing_whatsapp_transport(method, path, payload):
        return 503, {"error": "WhatsApp Web session disconnected. Account re-linking required."}

    wa_adapter = WhatsAppAnnouncementAdapter(
        homeserver_url="http://matrix.test",
        room_id="!portal:matrix.test",
        access_token="matrix-token-value",
        transport=failing_whatsapp_transport,
    )

    publisher = AnnouncementPublisher(
        storage=storage,
        adapters=[web_adapter, matrix_adapter, wa_adapter],
    )

    ann, results = publisher.publish(
        title="High-Priority Weather Warning",
        body="Flood alert in Lowland district.",
        author="Emergency Team",
    )

    # Web and Matrix MUST succeed
    assert results["web_feed"].success is True
    assert results["matrix_room"].success is True

    # WhatsApp failure is isolated
    assert results["whatsapp"].success is False
    assert "WhatsApp Web session disconnected" in (results["whatsapp"].error or "")

    # Canonical storage has the announcement
    stored = storage.get_announcement(ann.id)
    assert stored is not None
    assert stored.title == "High-Priority Weather Warning"

    # Static web feed was generated and contains the announcement
    html = (tmp_path / "web_feed" / "announcements.html").read_text(encoding="utf-8")
    assert "High-Priority Weather Warning" in html

    # Delivery states reflect failure accurately
    states = storage.get_delivery_states(ann.id)
    state_map = {s["adapter_name"]: s["status"] for s in states}
    assert state_map["web_feed"] == "delivered"
    assert state_map["matrix_room"] == "delivered"
    assert state_map["whatsapp"] == "failed"


def test_whatsapp_posts_to_the_saved_matrix_portal_room():
    calls = []

    def transport(method, path, payload):
        calls.append((method, path, payload))
        return 200, {"event_id": "$whatsapp-portal"}

    adapter = WhatsAppAnnouncementAdapter(
        homeserver_url="http://matrix.test",
        room_id="!public:matrix.test",
        access_token="matrix-token-value",
        transport=transport,
    )
    announcement = Announcement(title="Water cut", body="Tomorrow morning.")

    result = adapter.publish(announcement)

    assert result.success
    assert calls[0][0] == "PUT"
    assert "/rooms/%21public%3Amatrix.test/send/m.room.message/" in calls[0][1]
    assert calls[0][2]["body"] == "📢 Water cut\n\nTomorrow morning."


def test_matrix_requests_use_the_saved_access_token():
    calls = []

    def transport(method, path, headers, payload):
        calls.append(headers)
        return 200, {"event_id": "$matrix"}

    adapter = MatrixRoomAdapter(
        homeserver_url="http://matrix.test",
        room_id="!announcements:matrix.test",
        access_token="matrix-secret-token",
        transport=transport,
    )

    assert adapter.publish(Announcement(title="Notice", body="Details")).success
    assert calls[0]["Authorization"] == "Bearer matrix-secret-token"


def test_matrix_health_check_requires_public_unencrypted_room_membership():
    def transport(method, path, headers, payload):
        if path.endswith("/account/whoami"):
            return 200, {"user_id": "@announcements:matrix.test"}
        if path.endswith("/joined_rooms"):
            return 200, {"joined_rooms": ["!announcements:matrix.test"]}
        if path.endswith("/state/m.room.encryption"):
            return 404, {"errcode": "M_NOT_FOUND"}
        if path.endswith("/directory/list/room/%21announcements%3Amatrix.test"):
            return 200, {"visibility": "public"}
        raise AssertionError(f"Unexpected Matrix request: {method} {path}")

    adapter = MatrixRoomAdapter(
        homeserver_url="http://matrix.test",
        room_id="!announcements:matrix.test",
        access_token="matrix-secret-token",
        transport=transport,
    )

    assert adapter.health_check().ok
