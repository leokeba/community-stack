"""Offline archive exporter and local continuity generator."""

from __future__ import annotations

import hashlib
import html
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from community_stack.announcements.formatting import render_html
from community_stack.announcements.models import Announcement, AnnouncementStatus
from community_stack.announcements.storage import AnnouncementStorage

OFFLINE_CSS = """
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  max-width: 800px;
  margin: 0 auto;
  padding: 1.5rem;
  line-height: 1.6;
  color: #222;
  background-color: #fff;
}
header { border-bottom: 2px solid #0056b3; padding-bottom: 1rem; margin-bottom: 2rem; }
h1 { margin: 0 0 0.5rem 0; color: #0056b3; font-size: 1.8rem; }
.badge { display: inline-block; padding: 0.2rem 0.5rem; background: #e9ecef; border-radius: 4px; font-size: 0.85rem; }
.card { border: 1px solid #dee2e6; border-radius: 6px; padding: 1.25rem; margin-bottom: 1.5rem; }
.card h2 { margin-top: 0; font-size: 1.3rem; color: #333; }
.meta { font-size: 0.85rem; color: #666; margin-bottom: 1rem; }
.offline-banner { background: #fff3cd; border: 1px solid #ffeeba; color: #856404; padding: 0.75rem 1rem; border-radius: 4px; margin-bottom: 1.5rem; }
"""


def write_checksummed_zip(
    target: Path,
    files: dict[str, bytes],
    community_name: str,
    exported_at: str,
    announcements_count: int,
) -> Path:
    """Write files and a manifest of SHA-256 checksums into one zip, as ``verify_archive`` expects."""
    manifest = {
        "community_name": community_name,
        "exported_at": exported_at,
        "announcements_count": announcements_count,
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
    }
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return target


class OfflineArchiveExporter:
    """Exports self-contained offline HTML archives for USB or local Wi-Fi distribution."""

    def __init__(
        self,
        storage: AnnouncementStorage | None = None,
        community_name: str = "Community Communications",
    ) -> None:
        self.storage = storage or AnnouncementStorage()
        self.community_name = community_name

    def generate_html(
        self, announcements: list[Announcement], export_time: str, has_connect: bool = False
    ) -> str:
        """Render self-contained HTML page requiring zero external fonts, scripts, or assets."""
        items_html = ""
        for a in announcements:
            author_html = f"<span class='badge'>{html.escape(a.author)}</span>" if a.author else ""
            items_html += f"""
            <article class="card">
              <h2>{html.escape(a.title)}</h2>
              <div class="meta">
                <span>Date: {html.escape(a.published_at[:10])}</span> &bull;
                <span>ID: {html.escape(a.id)}</span> {author_html}
              </div>
              <div class="body">
                {render_html(a.body)}
              </div>
            </article>
            """

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Offline Archive - {html.escape(self.community_name)}</title>
  <style>{OFFLINE_CSS}</style>
</head>
<body>
  <header>
    <h1>{html.escape(self.community_name)}</h1>
    <p>Self-Contained Offline Public Announcement Archive</p>
  </header>
  <div class="offline-banner">
    <strong>📦 OFFLINE CONTINUITY COPY</strong><br>
    Exported at: {html.escape(export_time)} UTC. No internet connection required.
  </div>
  {'<p><a href="connect/index.html">Community contacts and channels</a></p>' if has_connect else ""}
  <main>
    {items_html if items_html else "<p>No active announcements in this archive.</p>"}
  </main>
  <footer>
    <p><small>Exported by Community Communications Stack &bull; File checksums are listed in manifest.json</small></p>
  </footer>
</body>
</html>
"""

    def export_archive(
        self, output_path: Path | str, connect_files: dict[str, bytes] | None = None
    ) -> Path:
        """Export complete offline bundle as a standalone .zip archive.

        ``connect_files`` (from ``connect_page.build_bundle``) are stored under ``connect/``.
        """
        target = Path(output_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)

        if not str(target).endswith(".zip"):
            target = target.with_suffix(".zip")

        announcements = self.storage.list_announcements(status=None, limit=200)
        # Only what is public: drafts and scheduled announcements must never leak.
        active = [
            a
            for a in announcements
            if a.status in (AnnouncementStatus.PUBLISHED, AnnouncementStatus.UPDATED)
        ]
        now_iso = datetime.now(timezone.utc).isoformat()

        html_content = self.generate_html(active, now_iso, has_connect=bool(connect_files))
        json_data = [a.to_dict() for a in active]
        json_content = json.dumps(json_data, indent=2)

        files: dict[str, bytes] = {
            "index.html": html_content.encode("utf-8"),
            "announcements.json": json_content.encode("utf-8"),
        }
        for name, content in (connect_files or {}).items():
            files[f"connect/{name}"] = content
        return write_checksummed_zip(
            target, files, self.community_name, now_iso, announcements_count=len(active)
        )

    @staticmethod
    def verify_archive(archive_path: Path | str) -> tuple[bool, list[str]]:
        """Verify checksums and structural integrity of an exported offline bundle."""
        path = Path(archive_path).resolve()
        messages: list[str] = []

        if not path.is_file():
            messages.append(f"Archive file not found: {path}")
            return False, messages

        try:
            with zipfile.ZipFile(path, "r") as zf:
                names = zf.namelist()
                if "manifest.json" not in names or "index.html" not in names:
                    messages.append("Archive missing critical files (manifest.json or index.html).")
                    return False, messages

                manifest_raw = zf.read("manifest.json").decode("utf-8")
                manifest = json.loads(manifest_raw)

                for fname, expected_hash in manifest.get("files", {}).items():
                    if fname not in names:
                        messages.append(f"Missing file in archive: {fname}")
                        return False, messages
                    content = zf.read(fname)
                    actual_hash = hashlib.sha256(content).hexdigest()
                    if actual_hash != expected_hash:
                        messages.append(f"Checksum mismatch for {fname}")
                        return False, messages

                messages.append(f"Archive '{path.name}' verified successfully.")
                messages.append(f"Bulletins count: {manifest.get('announcements_count')}")
                messages.append(f"Exported at: {manifest.get('exported_at')}")
                return True, messages
        except (zipfile.BadZipFile, json.JSONDecodeError, OSError) as exc:
            messages.append(f"Failed to verify archive: {exc}")
            return False, messages
