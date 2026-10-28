"""Integration tests validating WhatsApp bridge integration and failure decoupling."""

from pathlib import Path

import yaml
from community_stack.announcements.adapters.matrix_room import MatrixRoomAdapter
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
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
    assert "healthcheck" in wa_service


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
        enabled=True,
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
