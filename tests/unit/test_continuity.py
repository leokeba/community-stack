"""Unit tests for offline archive exporter and verification."""

import zipfile

from community_stack.announcements.models import Announcement
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.continuity.offline_exporter import OfflineArchiveExporter


def test_generate_offline_html():
    exporter = OfflineArchiveExporter(community_name="Mutual Aid Network")
    ann = Announcement(
        title="Offline Water Distribution",
        body="Clean water available at Main Firehouse from 8am to 6pm.",
        author="Logistics",
    )
    html_out = exporter.generate_html([ann], "2026-10-28T12:00:00Z")

    assert "Mutual Aid Network" in html_out
    assert "Offline Water Distribution" in html_out
    assert "Main Firehouse" in html_out
    assert "OFFLINE CONTINUITY COPY" in html_out
    assert "http://" not in html_out or "localhost" in html_out or "w3.org" in html_out
    assert "<script" not in html_out


def test_export_and_verify_archive(tmp_path):
    db_file = tmp_path / "announcements.db"
    storage = AnnouncementStorage(db_path=db_file)
    storage.save_announcement(Announcement(title="Notice 1", body="First offline bulletin."))
    storage.save_announcement(Announcement(title="Notice 2", body="Second offline bulletin."))

    exporter = OfflineArchiveExporter(storage=storage)
    zip_path = tmp_path / "offline_archive.zip"
    exported_file = exporter.export_archive(zip_path)

    assert exported_file.is_file()
    assert exported_file == zip_path

    # Verify structural contents of ZIP
    with zipfile.ZipFile(exported_file, "r") as zf:
        names = zf.namelist()
        assert "index.html" in names
        assert "announcements.json" in names
        assert "manifest.json" in names

    # Verify via static verifier
    is_valid, messages = OfflineArchiveExporter.verify_archive(exported_file)
    assert is_valid is True
    assert any("verified successfully" in m for m in messages)


def test_verify_archive_detects_tampering(tmp_path):
    db_file = tmp_path / "announcements.db"
    storage = AnnouncementStorage(db_path=db_file)
    storage.save_announcement(Announcement(title="Original", body="Body"))

    exporter = OfflineArchiveExporter(storage=storage)
    zip_path = tmp_path / "tamper_test.zip"
    exporter.export_archive(zip_path)

    # Tamper with archive content: rewrite index.html with modified bytes
    tampered_zip = tmp_path / "tampered.zip"
    with zipfile.ZipFile(zip_path, "r") as z_in, zipfile.ZipFile(tampered_zip, "w") as z_out:
        for item in z_in.infolist():
            if item.filename == "index.html":
                z_out.writestr(item, b"Tampered HTML Content!")
            else:
                z_out.writestr(item, z_in.read(item.filename))

    is_valid, messages = OfflineArchiveExporter.verify_archive(tampered_zip)
    assert is_valid is False
    assert any("Checksum mismatch" in m for m in messages)
