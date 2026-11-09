"""Unit tests for the WhatsApp announcement adapter."""

from community_stack.announcements.adapters.whatsapp import WhatsAppAnnouncementAdapter
from community_stack.announcements.models import Announcement


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
