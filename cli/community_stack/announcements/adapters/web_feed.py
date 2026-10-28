"""Static Web, RSS, and Atom feed generator adapter."""

from __future__ import annotations

import html
import json
from pathlib import Path
from xml.etree import ElementTree as ET

from community_stack.announcements.adapters.base import (
    ChannelAdapter,
    DeliveryResult,
    HealthResult,
)
from community_stack.announcements.models import Announcement, AnnouncementStatus


class WebFeedAdapter(ChannelAdapter):
    """Generates public HTML archive, Atom 1.0, RSS 2.0, and JSON Feed files."""

    def __init__(
        self,
        output_dir: Path | str = "data/landing/feed",
        base_url: str = "https://community.example.org",
        community_name: str = "Community Communications",
    ) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.base_url = base_url.rstrip("/")
        self.community_name = community_name
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._data_store_file = self.output_dir / "announcements_store.json"

    @property
    def name(self) -> str:
        return "web_feed"

    def _load_announcements(self) -> dict[str, dict]:
        if not self._data_store_file.is_file():
            return {}
        try:
            return json.loads(self._data_store_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_announcements(self, data: dict[str, dict]) -> None:
        self._data_store_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _regenerate_all(self, announcements: list[Announcement]) -> None:
        """Regenerate HTML, Atom, RSS, and JSON feed files."""
        # Only include non-withdrawn announcements in public feeds
        active = [a for a in announcements if a.status != AnnouncementStatus.WITHDRAWN]
        active.sort(key=lambda a: a.published_at, reverse=True)

        self._generate_html(active)
        self._generate_rss(active)
        self._generate_atom(active)
        self._generate_json_feed(active)

    def _generate_html(self, announcements: list[Announcement]) -> None:
        items_html = ""
        for a in announcements:
            author_badge = f"<span class='badge'>{html.escape(a.author)}</span>" if a.author else ""
            items_html += f"""
            <article class="announcement-item card">
              <header>
                <h2>{html.escape(a.title)}</h2>
                <div class="meta">
                  <time datetime="{html.escape(a.published_at)}">{html.escape(a.published_at[:10])}</time>
                  {author_badge}
                </div>
              </header>
              <div class="body">
                <p>{html.escape(a.body)}</p>
              </div>
            </article>
            """

        full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Public Announcements - {html.escape(self.community_name)}</title>
  <link rel="stylesheet" href="../style.css">
  <link rel="alternate" type="application/rss+xml" title="RSS Feed" href="rss.xml">
  <link rel="alternate" type="application/atom+xml" title="Atom Feed" href="atom.xml">
</head>
<body>
  <div class="container">
    <header>
      <h1>Public Announcements</h1>
      <p><a href="/">&larr; Back to Portal</a> &bull; <a href="rss.xml">RSS Feed</a> &bull; <a href="atom.xml">Atom Feed</a></p>
    </header>
    <main>
      {items_html if items_html else "<p>No active public announcements at this time.</p>"}
    </main>
  </div>
</body>
</html>
"""
        (self.output_dir / "announcements.html").write_text(full_html, encoding="utf-8")

    def _generate_rss(self, announcements: list[Announcement]) -> None:
        rss = ET.Element("rss", version="2.0")
        channel = ET.SubElement(rss, "channel")

        title = ET.SubElement(channel, "title")
        title.text = f"{self.community_name} Announcements"

        link = ET.SubElement(channel, "link")
        link.text = f"{self.base_url}/feed/announcements.html"

        desc = ET.SubElement(channel, "description")
        desc.text = "Authoritative public updates and community advisories."

        for a in announcements:
            item = ET.SubElement(channel, "item")
            item_title = ET.SubElement(item, "title")
            item_title.text = a.title

            item_link = ET.SubElement(item, "link")
            item_link.text = a.canonical_url or f"{self.base_url}/feed/announcements.html#{a.id}"

            item_guid = ET.SubElement(item, "guid", isPermaLink="false")
            item_guid.text = a.id

            item_desc = ET.SubElement(item, "description")
            item_desc.text = a.body

            item_pub = ET.SubElement(item, "pubDate")
            item_pub.text = a.published_at

        tree = ET.ElementTree(rss)
        tree.write(self.output_dir / "rss.xml", encoding="utf-8", xml_declaration=True)

    def _generate_atom(self, announcements: list[Announcement]) -> None:
        feed = ET.Element("feed", xmlns="http://www.w3.org/2005/Atom")

        title = ET.SubElement(feed, "title")
        title.text = f"{self.community_name} Announcements"

        feed_id = ET.SubElement(feed, "id")
        feed_id.text = f"{self.base_url}/feed/atom.xml"

        for a in announcements:
            entry = ET.SubElement(feed, "entry")
            e_title = ET.SubElement(entry, "title")
            e_title.text = a.title

            e_id = ET.SubElement(entry, "id")
            e_id.text = f"urn:uuid:{a.id}"

            e_updated = ET.SubElement(entry, "updated")
            e_updated.text = a.published_at

            content = ET.SubElement(entry, "content", type="text")
            content.text = a.body

        tree = ET.ElementTree(feed)
        tree.write(self.output_dir / "atom.xml", encoding="utf-8", xml_declaration=True)

    def _generate_json_feed(self, announcements: list[Announcement]) -> None:
        feed_data = {
            "version": "https://jsonfeed.org/version/1.1",
            "title": f"{self.community_name} Announcements",
            "home_page_url": f"{self.base_url}/feed/announcements.html",
            "feed_url": f"{self.base_url}/feed/feed.json",
            "items": [
                {
                    "id": a.id,
                    "title": a.title,
                    "content_text": a.body,
                    "url": a.canonical_url or f"{self.base_url}/feed/announcements.html#{a.id}",
                    "date_published": a.published_at,
                    "author": {"name": a.author} if a.author else None,
                }
                for a in announcements
            ],
        }
        (self.output_dir / "feed.json").write_text(
            json.dumps(feed_data, indent=2), encoding="utf-8"
        )

    def publish(self, announcement: Announcement) -> DeliveryResult:
        try:
            store = self._load_announcements()
            store[announcement.id] = announcement.to_dict()
            self._save_announcements(store)

            all_anns = [Announcement.from_dict(v) for v in store.values()]
            self._regenerate_all(all_anns)

            return DeliveryResult(
                success=True,
                external_id=f"{self.base_url}/feed/announcements.html#{announcement.id}",
            )
        except Exception as exc:
            return DeliveryResult(success=False, error=str(exc), retryable=False)

    def update(self, announcement: Announcement) -> DeliveryResult:
        return self.publish(announcement)

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        try:
            store = self._load_announcements()
            if announcement_id in store:
                store[announcement_id]["status"] = AnnouncementStatus.WITHDRAWN.value
                self._save_announcements(store)
                all_anns = [Announcement.from_dict(v) for v in store.values()]
                self._regenerate_all(all_anns)
            return DeliveryResult(success=True)
        except Exception as exc:
            return DeliveryResult(success=False, error=str(exc), retryable=False)

    def health_check(self) -> HealthResult:
        try:
            test_file = self.output_dir / ".healthcheck_probe"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink()
            return HealthResult(ok=True, details="Output directory writable.")
        except Exception as exc:
            return HealthResult(ok=False, details=f"Cannot write to output dir: {exc}")
