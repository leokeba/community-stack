"""Unit tests for WhatsApp announcement adapter and bridge manager."""

import pytest
from community_stack.announcements.models import Announcement
from community_stack.whatsapp.announcement_forwarder import WhatsAppAnnouncementAdapter
from community_stack.whatsapp.bridge_manager import BridgeError, WhatsAppBridgeManager


def test_whatsapp_adapter_disabled():
    adapter = WhatsAppAnnouncementAdapter(enabled=False)
    ann = Announcement(title="Disabled Test", body="Body")
    res = adapter.publish(ann)
    assert res.success is False
    assert "disabled" in (res.error or "")


def test_whatsapp_adapter_enabled_mock_transport():
    dispatched = []

    def mock_transport(method, path, payload):
        dispatched.append({"method": method, "path": path, "payload": payload})
        return 200, {"message_id": "wa_msg_999"}

    adapter = WhatsAppAnnouncementAdapter(
        enabled=True,
        target_jid="123456789-group@g.us",
        transport=mock_transport,
    )

    ann = Announcement(
        title="Public Rally Announcement",
        body="Join the rally this Sunday.",
        canonical_url="https://example.org/feed#ann1",
        author="Coordination Hub",
    )

    res = adapter.publish(ann)
    assert res.success is True
    assert res.external_id == "wa_msg_999"
    assert len(dispatched) == 1
    req = dispatched[0]
    assert req["payload"]["recipient"] == "123456789-group@g.us"
    assert "Public Rally Announcement" in req["payload"]["message"]
    assert "Coordination Hub" in req["payload"]["message"]


def test_whatsapp_adapter_error_masks_phone_numbers():
    def mock_transport(method, path, payload):
        return 500, {"error": "Failed sending to recipient +1234567890123: Timeout"}

    adapter = WhatsAppAnnouncementAdapter(enabled=True, transport=mock_transport)
    ann = Announcement(title="Fail Test", body="Body")
    res = adapter.publish(ann)

    assert res.success is False
    assert res.retryable is True
    # Verify phone number was redacted from error message
    assert "1234567890123" not in (res.error or "")
    assert "[PHONE_REDACTED]" in (res.error or "")


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
