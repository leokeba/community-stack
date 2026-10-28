"""Static Web, RSS, and Atom feed generator adapter."""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from community_stack.announcements.adapters.base import (
    ChannelAdapter,
    ChannelCapabilities,
    DeliveryResult,
    HealthResult,
    UpdateMode,
    WithdrawMode,
)
from community_stack.announcements.models import Announcement, AnnouncementStatus
from community_stack.announcements.storage import AnnouncementStorage


class WebFeedAdapter(ChannelAdapter):
    """Generates public HTML archive, Atom 1.0, RSS 2.0, and JSON Feed files."""

    def __init__(
        self,
        output_dir: Path | str = "data/landing/feed",
        base_url: str = "https://community.example.org",
        community_name: str = "Community Communications",
        storage: AnnouncementStorage | None = None,
        channel_links: dict[str, str] | None = None,
    ) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.base_url = base_url.rstrip("/")
        self.community_name = community_name
        self.channel_links = channel_links or {}
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.storage = storage or AnnouncementStorage(
            db_path=self.output_dir.parent / "announcements.db"
        )

    @property
    def name(self) -> str:
        return "web_feed"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label="Public web feed",
            update_mode=UpdateMode.IN_PLACE,
            withdraw_mode=WithdrawMode.DELETE,
        )

    def canonical_url_for(self, announcement: Announcement) -> str:
        return f"{self.base_url}/feed/announcements/{announcement.id}/"

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def rebuild(self) -> None:
        """Rebuild all public HTML and feed outputs from canonical SQLite records."""
        self._regenerate_all(self.storage.list_announcements(limit=None))

    def _regenerate_all(self, announcements: list[Announcement]) -> None:
        """Regenerate HTML, Atom, RSS, and JSON feed files."""
        active = [
            a
            for a in announcements
            if a.status in (AnnouncementStatus.PUBLISHED, AnnouncementStatus.UPDATED)
            and a.audience == "public"
        ]
        for announcement in active:
            if not announcement.canonical_url:
                announcement.canonical_url = self.canonical_url_for(announcement)
        active.sort(key=lambda a: a.published_at, reverse=True)

        self._generate_html(active)
        active_ids = {announcement.id for announcement in active}
        for announcement in announcements:
            if announcement.id not in active_ids:
                withdrawn_page = self.output_dir / "announcements" / announcement.id / "index.html"
                if withdrawn_page.is_file():
                    withdrawn_page.unlink()
        self._generate_rss(active)
        self._generate_atom(active)
        self._generate_json_feed(active)

    def _generate_html(self, announcements: list[Announcement]) -> None:
        items_html = ""
        for a in announcements:
            author_badge = f"<span class='badge'>{html.escape(a.author)}</span>" if a.author else ""
            detail_url = a.canonical_url or f"announcements/{a.id}/"
            items_html += f"""
            <article class="announcement-item card">
              <header>
                <h2><a href="{detail_url}">{html.escape(a.title)}</a></h2>
                <div class="meta">
                  <time datetime="{html.escape(a.published_at)}">{html.escape(a.published_at[:10])}</time>
                  {f"<span>Updated {html.escape(a.updated_at[:10])}</span>" if a.updated_at != a.published_at else ""}
                  {author_badge}
                </div>
              </header>
              <div class="body">
                <p>{html.escape(a.body).replace(chr(10), "<br>")}</p>
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
  <link rel="alternate" type="application/feed+json" title="JSON Feed" href="feed.json">
</head>
<body>
  <div class="container">
    <header>
      <h1>Public Announcements</h1>
      <p><a href="/">&larr; Back to Portal</a> &bull; <a href="rss.xml">RSS Feed</a> &bull; <a href="atom.xml">Atom Feed</a> &bull; <a href="feed.json">JSON Feed</a></p>
    </header>
    <main>
      {items_html if items_html else "<p>No active public announcements at this time.</p>"}
    </main>
  </div>
</body>
</html>
"""
        (self.output_dir / "announcements.html").write_text(full_html, encoding="utf-8")
        for announcement in announcements:
            author = (
                f"<p class='author'>By {html.escape(announcement.author)}</p>"
                if announcement.author
                else ""
            )
            body = html.escape(announcement.body).replace("\n", "<br>\n")
            channel_links = " ".join(
                f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'
                for label, url in self.channel_links.items()
            )
            page = f"""<!DOCTYPE html>
<html lang="{html.escape(announcement.language)}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(announcement.title)} - {html.escape(self.community_name)}</title>
  <link rel="canonical" href="{html.escape(self.canonical_url_for(announcement))}">
</head>
<body>
  <main>
    <p><a href="/feed/announcements.html">&larr; All announcements</a></p>
    <article>
      <h1>{html.escape(announcement.title)}</h1>
      <time datetime="{html.escape(announcement.published_at)}">{html.escape(announcement.published_at[:10])}</time>
      {"<p>Updated <time datetime='" + html.escape(announcement.updated_at) + "'>" + html.escape(announcement.updated_at[:10]) + "</time></p>" if announcement.updated_at != announcement.published_at else ""}
      {author}
      <div>{body}</div>
    </article>
    <nav aria-label="Announcement feeds">
      <a href="/feed/rss.xml">RSS</a> ·
      <a href="/feed/atom.xml">Atom</a> ·
      <a href="/feed/feed.json">JSON Feed</a>
    </nav>
    {"<nav aria-label='Other community channels'>" + channel_links + "</nav>" if channel_links else ""}
  </main>
</body>
</html>
"""
            detail_path = self.output_dir / "announcements" / announcement.id / "index.html"
            detail_path.parent.mkdir(parents=True, exist_ok=True)
            detail_path.write_text(page, encoding="utf-8")

    def _generate_rss(self, announcements: list[Announcement]) -> None:
        rss = ET.Element("rss", version="2.0")
        channel = ET.SubElement(rss, "channel")

        title = ET.SubElement(channel, "title")
        title.text = f"{self.community_name} Announcements"

        link = ET.SubElement(channel, "link")
        link.text = f"{self.base_url}/feed/announcements.html"

        desc = ET.SubElement(channel, "description")
        desc.text = "Authoritative public updates and community advisories."

        last_build = ET.SubElement(channel, "lastBuildDate")
        latest_update = max(
            (self._parse_datetime(item.updated_at) for item in announcements),
            default=datetime.now(timezone.utc),
        )
        last_build.text = format_datetime(latest_update)

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
            published = self._parse_datetime(a.published_at)
            item_pub.text = format_datetime(published)

        tree = ET.ElementTree(rss)
        tree.write(self.output_dir / "rss.xml", encoding="utf-8", xml_declaration=True)

    def _generate_atom(self, announcements: list[Announcement]) -> None:
        feed = ET.Element("feed", xmlns="http://www.w3.org/2005/Atom")

        title = ET.SubElement(feed, "title")
        title.text = f"{self.community_name} Announcements"

        feed_id = ET.SubElement(feed, "id")
        feed_id.text = f"{self.base_url}/feed/atom.xml"
        ET.SubElement(feed, "link", href=f"{self.base_url}/feed/atom.xml", rel="self")
        ET.SubElement(
            feed,
            "link",
            href=f"{self.base_url}/feed/announcements.html",
            rel="alternate",
        )
        updated = max(
            (self._parse_datetime(item.updated_at) for item in announcements),
            default=datetime.now(timezone.utc),
        )
        feed_updated = ET.SubElement(feed, "updated")
        feed_updated.text = updated.isoformat()

        for a in announcements:
            entry = ET.SubElement(feed, "entry")
            e_title = ET.SubElement(entry, "title")
            e_title.text = a.title

            e_id = ET.SubElement(entry, "id")
            e_id.text = a.canonical_url or self.canonical_url_for(a)
            ET.SubElement(
                entry,
                "link",
                href=a.canonical_url or self.canonical_url_for(a),
                rel="alternate",
            )

            e_updated = ET.SubElement(entry, "updated")
            e_updated.text = a.updated_at

            e_published = ET.SubElement(entry, "published")
            e_published.text = a.published_at

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
                    "date_modified": a.updated_at,
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
            if not announcement.canonical_url:
                announcement.canonical_url = self.canonical_url_for(announcement)
            all_anns = self.storage.list_announcements(limit=None)
            self._regenerate_all(all_anns)

            return DeliveryResult(
                success=True,
                external_id=announcement.canonical_url,
                idempotent=True,
            )
        except Exception as exc:
            return DeliveryResult(success=False, error=str(exc), retryable=True, idempotent=True)

    def update(self, announcement: Announcement) -> DeliveryResult:
        return self.publish(announcement)

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        try:
            announcement = self.storage.get_announcement(announcement_id)
            if announcement is None:
                return DeliveryResult(
                    success=False,
                    error=f"Announcement '{announcement_id}' not found in canonical storage.",
                )
            self._regenerate_all(self.storage.list_announcements(limit=None))
            return DeliveryResult(success=True, idempotent=True)
        except Exception as exc:
            return DeliveryResult(success=False, error=str(exc), retryable=True, idempotent=True)

    def health_check(self) -> HealthResult:
        try:
            test_file = self.output_dir / ".healthcheck_probe"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink()
            return HealthResult(ok=True, details="Output directory writable.")
        except Exception as exc:
            return HealthResult(ok=False, details=f"Cannot write to output dir: {exc}")
