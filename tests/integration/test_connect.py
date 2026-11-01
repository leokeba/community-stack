"""Integration tests for the /connect page: CLI, publisher refresh, archives and deployment wiring."""

import copy
import logging
from pathlib import Path

import pytest
import yaml
from community_stack.announcements.adapters.base import ChannelAdapter, DeliveryResult, HealthResult
from community_stack.announcements.models import Announcement, AnnouncementStatus
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.cli import main
from community_stack.continuity.connect_page import offline_connect_files
from community_stack.continuity.offline_exporter import OfflineArchiveExporter
from PIL import Image

REPO = Path(__file__).resolve().parents[2]


class RecordingAdapter(ChannelAdapter):
    """Stands in for a real channel. It only records what it was asked to publish."""

    def __init__(self, succeed: bool = True) -> None:
        self.succeed = succeed
        self.published: list[str] = []

    @property
    def name(self) -> str:
        return "test_channel"

    def publish(self, announcement: Announcement) -> DeliveryResult:
        self.published.append(announcement.id)
        if self.succeed:
            return DeliveryResult(success=True, external_id=announcement.id, idempotent=True)
        return DeliveryResult(success=False, error="offline", retryable=False)

    def update(self, announcement: Announcement) -> DeliveryResult:
        return self.publish(announcement)

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        return DeliveryResult(success=True, idempotent=True)

    def health_check(self) -> HealthResult:
        return HealthResult(ok=True, details="ok")


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A temporary deployment: a configuration with a community block, a logo and an empty store."""
    monkeypatch.chdir(tmp_path)
    with open(REPO / "config/example.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["community"] = {
        "name": "Example Community",
        "description": "A community working together.",
        "url": "https://community.example.org",
        "logo": "logo.png",
        "contact": {"email": "hello@example.org"},
        "emergency_instructions": "Use the feed if the chat is down.",
        "social": [{"platform": "instagram", "url": "https://instagram.com/example"}],
        "independent": {"website": True, "rss": True, "email": True, "matrix": True},
    }
    (tmp_path / "community-stack.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    Image.new("RGB", (48, 48), "navy").save(tmp_path / "logo.png")
    return tmp_path


def _storage(site: Path) -> AnnouncementStorage:
    return AnnouncementStorage(db_path=site / "data/announcements/announcements.db")


def _announcement(ann_id: str, status: AnnouncementStatus, title: str) -> Announcement:
    return Announcement(
        id=ann_id,
        title=title,
        body="Body text",
        status=status,
        canonical_url=f"https://community.example.org/feed/announcements/{ann_id}/",
    )


def test_recent_list_excludes_drafts_and_withdrawn(site):
    from community_stack.continuity.connect_page import recent_announcements

    storage = _storage(site)
    storage.save_announcement(_announcement("ann_draft_0001", AnnouncementStatus.DRAFT, "Draft"))
    storage.save_announcement(
        _announcement("ann_pub_0001", AnnouncementStatus.PUBLISHED, "Published")
    )
    storage.save_announcement(
        _announcement("ann_wd_0001", AnnouncementStatus.WITHDRAWN, "Withdrawn")
    )
    storage.save_announcement(_announcement("ann_upd_0001", AnnouncementStatus.UPDATED, "Updated"))

    titles = {a.title for a in recent_announcements(storage)}
    assert titles == {"Published", "Updated"}


def test_cli_show_lists_channels_and_reasons(site, capsys):
    assert main(["connect", "show"]) == 0
    out = capsys.readouterr().out
    assert "Stable page:  https://community.example.org/connect/" in out
    assert "Our website" in out
    not_shown = out.split("not shown:")[1]
    assert "email: No email server is connected." in not_shown
    assert "matrix: No public Matrix room is connected." in not_shown


def test_cli_generate_qr_writes_both_formats(site, capsys):
    assert main(["connect", "generate-qr", "--output-dir", "out"]) == 0
    assert (site / "out/connect-qr.svg").read_bytes().startswith(b"<?xml")
    assert (site / "out/connect-qr.png").read_bytes().startswith(b"\x89PNG")
    assert "Print at least 3 cm" in capsys.readouterr().out


def test_cli_generate_qr_warns_for_an_address_off_the_domain(site, capsys):
    rc = main(
        [
            "connect",
            "generate-qr",
            "--output-dir",
            "out",
            "--destination",
            "https://elsewhere.net/x",
        ]
    )
    assert rc == 0
    assert "[WARN]" in capsys.readouterr().out


def test_cli_generate_qr_rejects_plain_http(site, capsys):
    rc = main(["connect", "generate-qr", "--destination", "http://community.example.org/connect/"])
    assert rc == 1
    assert "must start with https" in capsys.readouterr().out


def test_cli_export_writes_a_package_that_verifies(site):
    assert main(["connect", "export", "-o", "pkg/identity.zip"]) == 0
    ok, messages = OfflineArchiveExporter.verify_archive(site / "pkg/identity.zip")
    assert ok, messages

    import zipfile

    with zipfile.ZipFile(site / "pkg/identity.zip") as zf:
        names = set(zf.namelist())
    assert {"index.html", "community.json", "qr.svg", "qr.png", "logo.jpg"} <= names


def test_cli_render_writes_the_live_page(site):
    assert main(["connect", "render"]) == 0
    page = (site / "data/landing/connect/index.html").read_text(encoding="utf-8")
    assert "<h1>Example Community</h1>" in page
    assert (site / "data/landing/connect/logo.jpg").is_file()


def test_publisher_refreshes_page_after_successful_delivery(site):
    adapter = RecordingAdapter()
    publisher = AnnouncementPublisher(
        storage=_storage(site),
        adapters=[adapter],
        config_path=site / "community-stack.yaml",
        feed_dir=site / "data/landing/feed",
    )
    ann, results = publisher.publish("Water shutdown tomorrow", "Taps off from 08:00.")

    assert results["test_channel"].success
    page = (site / "data/landing/connect/index.html").read_text(encoding="utf-8")
    assert "Water shutdown tomorrow" in page
    assert f"/feed/announcements/{ann.id}/" in page


def test_withdrawn_announcement_leaves_the_page(site):
    publisher = AnnouncementPublisher(
        storage=_storage(site),
        adapters=[RecordingAdapter()],
        config_path=site / "community-stack.yaml",
        feed_dir=site / "data/landing/feed",
    )
    ann, _ = publisher.publish("Meeting moved", "New room.")
    publisher.withdraw(ann.id)

    page = (site / "data/landing/connect/index.html").read_text(encoding="utf-8")
    assert "Meeting moved" not in page


def test_failed_page_refresh_never_blocks_delivery(site, caplog):
    cfg = yaml.safe_load((site / "community-stack.yaml").read_text())
    cfg["community"]["url"] = "http://not-secure.example.org"
    (site / "community-stack.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    adapter = RecordingAdapter()
    publisher = AnnouncementPublisher(
        storage=_storage(site),
        adapters=[adapter],
        config_path=site / "community-stack.yaml",
        feed_dir=site / "data/landing/feed",
    )
    with caplog.at_level(logging.WARNING):
        _ann, results = publisher.publish("Still delivered", "Body.")

    assert results["test_channel"].success
    assert adapter.published
    assert "Could not refresh the /connect page." in caplog.text
    assert not (site / "data/landing/connect/index.html").exists()


def test_page_follows_canonical_state_even_when_a_channel_fails(site):
    publisher = AnnouncementPublisher(
        storage=_storage(site),
        adapters=[RecordingAdapter(succeed=False)],
        config_path=site / "community-stack.yaml",
        feed_dir=site / "data/landing/feed",
    )
    publisher.publish("Saved but not sent", "Body.")
    page = (site / "data/landing/connect/index.html").read_text(encoding="utf-8")
    assert "Saved but not sent" in page


def test_offline_archive_includes_identity_files_and_verifies(site):
    storage = _storage(site)
    storage.save_announcement(
        _announcement("ann_pub_0002", AnnouncementStatus.PUBLISHED, "Public notice")
    )
    files, warning = offline_connect_files(site / "community-stack.yaml", storage)
    assert warning is None

    target = site / "archive/offline.zip"
    OfflineArchiveExporter(storage=storage).export_archive(target, connect_files=files)
    ok, messages = OfflineArchiveExporter.verify_archive(target)
    assert ok, messages

    import zipfile

    with zipfile.ZipFile(target) as zf:
        names = set(zf.namelist())
        index = zf.read("index.html").decode()
    assert "connect/index.html" in names
    assert "connect/community.json" in names
    assert 'href="connect/index.html"' in index


def test_bad_logo_leaves_contacts_out_but_still_exports(site):
    cfg = yaml.safe_load((site / "community-stack.yaml").read_text())
    cfg["community"]["logo"] = "missing.png"
    (site / "community-stack.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    storage = _storage(site)
    files, warning = offline_connect_files(site / "community-stack.yaml", storage)
    assert files is None
    assert "left out of the archive" in warning

    target = site / "archive/offline.zip"
    OfflineArchiveExporter(storage=storage).export_archive(target, connect_files=files)
    assert OfflineArchiveExporter.verify_archive(target)[0]


def test_deployment_mounts_connect_directory_read_only_for_landing():
    landing = yaml.safe_load((REPO / "services/landing/docker-compose.yml").read_text())
    volumes = landing["services"]["landing"]["volumes"]
    assert "../../data/landing/connect:/usr/share/nginx/html/connect:ro" in volumes


def test_deployment_mounts_connect_directory_writable_for_announcements():
    app = yaml.safe_load((REPO / "services/announcements/docker-compose.yml").read_text())
    volumes = app["services"]["announcements"]["volumes"]
    assert "../../data/landing/connect:/app/data/landing/connect" in volumes


def test_connect_mountpoint_exists_in_read_only_html_volume():
    assert (REPO / "services/landing/html/connect").is_dir()


def test_portal_pages_link_to_the_connect_page():
    for name in ("index.html", "lite.html"):
        assert "/connect/" in (REPO / "services/landing/html" / name).read_text()


def test_example_config_still_produces_a_page_without_community_block(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = copy.deepcopy(yaml.safe_load((REPO / "config/example.yaml").read_text()))
    (tmp_path / "community-stack.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert main(["connect", "render"]) == 0
    page = (tmp_path / "data/landing/connect/index.html").read_text(encoding="utf-8")
    assert "<h1>community</h1>" in page
