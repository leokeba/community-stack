"""Unit tests for WhatsApp announcement adapter and bridge manager."""

import pytest
from community_stack.announcements.models import Announcement
from community_stack.whatsapp.announcement_forwarder import WhatsAppAnnouncementAdapter
from community_stack.whatsapp.bridge_manager import BridgeError, WhatsAppBridgeManager


def test_whatsapp_adapter_posts_to_matrix_portal():
    dispatched = []

    def mock_transport(method, path, payload):
        dispatched.append({"method": method, "path": path, "payload": payload})
        return 200, {"event_id": "$portal-event"}

    adapter = WhatsAppAnnouncementAdapter(
        homeserver_url="http://matrix.test",
        room_id="!portal:matrix.test",
        access_token="matrix-token-value",
        transport=mock_transport,
    )
    announcement = Announcement(title="Public notice", body="The event starts at noon.")

    result = adapter.publish(announcement)

    assert result.success
    assert result.external_id == "$portal-event"
    assert dispatched[0]["method"] == "PUT"
    assert "%21portal%3Amatrix.test" in dispatched[0]["path"]
    assert dispatched[0]["payload"]["body"] == "📢 Public notice\n\nThe event starts at noon."


def test_whatsapp_adapter_propagates_matrix_failures():
    def mock_transport(method, path, payload):
        return 503, {"error": "Homeserver restarting"}

    adapter = WhatsAppAnnouncementAdapter(
        homeserver_url="http://matrix.test",
        room_id="!portal:matrix.test",
        access_token="matrix-token-value",
        transport=mock_transport,
    )

    result = adapter.publish(Announcement(title="Notice", body="Body"))

    assert not result.success
    assert result.retryable
    assert "Homeserver restarting" in (result.error or "")


def test_bridge_manager_allowlist(tmp_path):
    cfg_file = tmp_path / "community-stack.yaml"
    cfg_file.write_text(
        """
project:
  name: "test"
  profile: "accessible"
domain:
  base: "example.org"
services:
  matrix:
    enabled: true
  element:
    enabled: true
  announcements:
    enabled: true
  whatsapp_bridge:
    enabled: true
    mode: "selective_bridge"
    mapped_rooms:
      - matrix_room_id: "!allowed_room:example.org"
        whatsapp_chat_jid: "123456@g.us"
""",
        encoding="utf-8",
    )

    manager = WhatsAppBridgeManager(config_path=cfg_file)
    assert manager.is_enabled() is True
    assert manager.get_mode() == "selective_bridge"
    assert manager.is_room_allowlisted("!allowed_room:example.org") is True
    assert manager.is_room_allowlisted("!secret_room:example.org") is False


def test_bridge_manager_emergency_disable(tmp_path):
    cfg_file = tmp_path / "community-stack.yaml"
    cfg_file.write_text(
        """
project:
  name: "test"
  profile: "accessible"
domain:
  base: "example.org"
services:
  matrix:
    enabled: true
  element:
    enabled: true
  announcements:
    enabled: true
  whatsapp_bridge:
    enabled: true
""",
        encoding="utf-8",
    )

    manager = WhatsAppBridgeManager(config_path=cfg_file)

    # Without confirmation should raise BridgeError
    with pytest.raises(BridgeError, match="requires explicit confirmation"):
        manager.emergency_disable(confirm=False)

    # With confirmation should succeed
    success = manager.emergency_disable(confirm=True)
    assert success is True
