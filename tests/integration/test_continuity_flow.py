"""Integration tests validating full offline archive creation, distribution, and verification flow."""

import json
import zipfile

from community_stack.announcements.models import Announcement, AnnouncementStatus
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.continuity.offline_exporter import OfflineArchiveExporter


def test_offline_continuity_end_to_end_flow(tmp_path):
    storage = AnnouncementStorage(db_path=tmp_path / "announcements.db")

    # 1. Create active and withdrawn announcements
    ann1 = Announcement(
        title="Active Medical Aid Station Open",
        body="First aid and medical advice available at North High School.",
        author="Health Working Group",
        tags=["medical", "health"],
        status=AnnouncementStatus.PUBLISHED,
    )
    ann2 = Announcement(
        title="Outdated Curfew Notice",
        body="This notice has been superseded.",
        status=AnnouncementStatus.WITHDRAWN,
    )
    draft = Announcement(title="Unreleased plan", body="Not public yet.")
    storage.save_announcement(ann1)
    storage.save_announcement(ann2)
    storage.save_announcement(draft)

    # 2. Export offline archive bundle
    exporter = OfflineArchiveExporter(storage=storage, community_name="City Mutual Aid")
    export_bundle = tmp_path / "emergency_broadcast.zip"
    exporter.export_archive(export_bundle)

    assert export_bundle.is_file()

    # 3. Verify bundle cryptographically
    is_valid, _messages = OfflineArchiveExporter.verify_archive(export_bundle)
    assert is_valid is True

    # 4. Extract into an offline web root (simulating USB stick or captive portal)
    extract_root = tmp_path / "captive_portal_www"
    extract_root.mkdir()
    with zipfile.ZipFile(export_bundle, "r") as zf:
        zf.extractall(extract_root)

    # Verify extracted offline files
    html_file = extract_root / "index.html"
    json_file = extract_root / "announcements.json"
    manifest_file = extract_root / "manifest.json"

    assert html_file.is_file()
    assert json_file.is_file()
    assert manifest_file.is_file()

    html_content = html_file.read_text(encoding="utf-8")
    assert "Active Medical Aid Station Open" in html_content
    assert "North High School" in html_content
    assert "City Mutual Aid" in html_content

    # Withdrawn announcements and unpublished drafts must be excluded
    assert "Outdated Curfew Notice" not in html_content
    assert "Unreleased plan" not in html_content

    # Structured JSON file must contain exact 1 active announcement
    json_data = json.loads(json_file.read_text(encoding="utf-8"))
    assert len(json_data) == 1
    assert json_data[0]["title"] == "Active Medical Aid Station Open"
