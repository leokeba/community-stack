"""Unit tests for WebFeedAdapter and MatrixRoomAdapter."""

from xml.etree import ElementTree as ET

from community_stack.announcements.adapters.matrix_room import MatrixRoomAdapter
from community_stack.announcements.adapters.web_feed import WebFeedAdapter
from community_stack.announcements.models import Announcement, AnnouncementStatus


def test_web_feed_adapter_generates_all_formats(tmp_path):
    output_dir = tmp_path / "feed"
    adapter = WebFeedAdapter(
        output_dir=output_dir,
        base_url="https://community.test",
        community_name="Test Community",
        channel_links={"Matrix announcement room": "https://matrix.to/#/!public:community.test"},
    )

    ann = Announcement(
        title="Public Rally Scheduled",
        body="Join us at the city square on Saturday at 2pm.",
        author="Organizing Committee",
        status=AnnouncementStatus.PUBLISHED,
    )

    res = adapter.publish(ann)
    assert res.success is True
    assert f"/feed/announcements/{ann.id}/" in (res.external_id or "")

    # Check that all four feed files were generated
    html_file = output_dir / "announcements.html"
    rss_file = output_dir / "rss.xml"
    atom_file = output_dir / "atom.xml"
    json_file = output_dir / "feed.json"

    assert html_file.is_file()
    detail_file = output_dir / "announcements" / ann.id / "index.html"
    assert detail_file.is_file()
    assert rss_file.is_file()
    assert atom_file.is_file()
    assert json_file.is_file()

    # Verify content in generated feeds
    html_content = html_file.read_text(encoding="utf-8")
    assert "Public Rally Scheduled" in html_content
    assert "Organizing Committee" in html_content
    assert f"/feed/announcements/{ann.id}/" in detail_file.read_text(encoding="utf-8")
    assert "Matrix announcement room" in detail_file.read_text(encoding="utf-8")

    rss_content = rss_file.read_text(encoding="utf-8")
    assert "<title>Public Rally Scheduled</title>" in rss_content

    atom_content = atom_file.read_text(encoding="utf-8")
    assert "<title>Public Rally Scheduled</title>" in atom_content
    atom_root = ET.parse(atom_file).getroot()
    assert atom_root.find("{http://www.w3.org/2005/Atom}updated") is not None

    json_content = json_file.read_text(encoding="utf-8")
    assert "Public Rally Scheduled" in json_content


def test_web_feed_adapter_withdrawal(tmp_path):
    output_dir = tmp_path / "feed"
    adapter = WebFeedAdapter(output_dir=output_dir)

    ann = Announcement(
        title="Draft Notice",
        body="This will be retracted.",
        status=AnnouncementStatus.PUBLISHED,
    )
    adapter.publish(ann)

    # Now withdraw it
    with_res = adapter.withdraw(ann.id)
    assert with_res.success is True

    # Check that withdrawn announcement is omitted from active feeds
    html_content = (output_dir / "announcements.html").read_text(encoding="utf-8")
    assert "Draft Notice" not in html_content
    assert not (output_dir / "announcements" / ann.id / "index.html").exists()


def test_matrix_room_adapter_publish_success():
    sent_requests = []

    def mock_transport(method, path, headers, payload):
        sent_requests.append(
            {"method": method, "path": path, "headers": headers, "payload": payload}
        )
        return 200, {"event_id": "$mock_event_12345"}

    adapter = MatrixRoomAdapter(
        homeserver_url="http://mock-matrix:8008",
        room_id="!public:community.test",
        access_token="syt_secret_token",
        transport=mock_transport,
    )

    ann = Announcement(title="Important Community Notice", body="Matrix message body.")
    res = adapter.publish(ann)

    assert res.success is True
    assert res.external_id == "$mock_event_12345"
    assert len(sent_requests) == 1
    req = sent_requests[0]
    assert req["method"] == "PUT"
    assert "!public:community.test" in req["path"]
    assert req["headers"]["Authorization"] == "Bearer syt_secret_token"
    assert "Important Community Notice" in req["payload"]["body"]


def test_matrix_room_adapter_retryable_error():
    def mock_transport(method, path, headers, payload):
        return 502, {"error": "Bad Gateway - Synapse restarting"}

    adapter = MatrixRoomAdapter(transport=mock_transport)
    ann = Announcement(title="Retry Test", body="Should be retryable.")
    res = adapter.publish(ann)

    assert res.success is False
    assert res.retryable is True
    assert "Bad Gateway" in (res.error or "")


def test_matrix_room_adapter_non_retryable_error():
    def mock_transport(method, path, headers, payload):
        return 403, {"error": "User not permitted to post in this room"}

    adapter = MatrixRoomAdapter(transport=mock_transport)
    ann = Announcement(title="Forbidden Test", body="Non-retryable permission error.")
    res = adapter.publish(ann)

    assert res.success is False
    assert res.retryable is False
    assert "User not permitted" in (res.error or "")
