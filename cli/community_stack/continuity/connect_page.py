"""The /connect page: a static, no-JavaScript hub for every official channel.

The page is plain HTML written to disk and served by the landing nginx, so it keeps
working when the announcements app is down. The same files also go into offline
packages, and QR codes point at the page's stable address.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import segno
from community_stack.announcements.media import MediaError, process_image
from community_stack.announcements.models import Announcement, AnnouncementStatus
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.community import (
    ChannelResolution,
    CommunityProfile,
    load_community,
    profile_image_path,
    resolve_independent,
)
from community_stack.config import ConfigError

RECENT_LIMIT = 10
MAX_LOGO_BYTES = 2 * 1024 * 1024
PAGE_CSS = """
:root { font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; line-height: 1.55; color: #17212b; background: #f7f8f9; }
body { margin: 0; }
main, header, footer { max-width: 42rem; margin: 0 auto; padding: 1rem; }
header { padding-top: 1.5rem; }
.cover img { display: block; width: 100%; max-height: 12rem; object-fit: cover; border-radius: .5rem; }
.identity { display: flex; gap: 1rem; align-items: center; padding-top: .75rem; }
.identity img { width: 4.5rem; height: 4.5rem; object-fit: contain; flex: none; }
h1 { font-size: 1.6rem; margin: 0 0 .25rem; }
h2 { font-size: 1.2rem; margin: 0 0 .5rem; }
section { background: #fff; border: 1px solid #d5dde2; border-radius: .5rem; padding: 1rem 1.2rem; margin: 1rem 0; }
ul { padding-left: 1.2rem; margin: .5rem 0 0; }
li { margin: .4rem 0; }
a { color: #174f71; font-weight: 600; }
.address { font-size: 1.05rem; overflow-wrap: anywhere; }
.muted { color: #4a5963; }
.warning { border-left: .3rem solid #b42318; background: #fff1f0; padding: .75rem 1rem; }
footer { font-size: .9rem; color: #4a5963; }
@media print {
  body { background: #fff; color: #000; }
  section { border: 1px solid #000; break-inside: avoid; }
  a { color: #000; text-decoration: none; }
  a::after { content: " (" attr(href) ")"; font-weight: normal; font-size: .85em; }
}
"""


class ConnectError(ValueError):
    """Raised when the connect page cannot be built, such as a bad logo file."""


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _paragraphs(text: str) -> str:
    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    return "".join(f"<p>{_e(part).replace(chr(10), '<br>')}</p>" for part in parts)


def recent_announcements(storage: AnnouncementStorage) -> list[Announcement]:
    """Published or updated public announcements, newest first. Drafts never appear."""
    public = (AnnouncementStatus.PUBLISHED, AnnouncementStatus.UPDATED)
    items = [a for a in storage.list_announcements(limit=None) if a.status in public]
    return items[:RECENT_LIMIT]


def prepare_logo(path: Path) -> tuple[str, bytes]:
    """Re-encode the logo so that metadata such as GPS location is removed."""
    return prepare_profile_image(path, "logo")


def prepare_cover(path: Path) -> tuple[str, bytes]:
    """Re-encode the cover image so that metadata such as GPS location is removed."""
    return prepare_profile_image(path, "cover")


def prepare_profile_image(path: Path, kind: str) -> tuple[str, bytes]:
    if kind not in {"logo", "cover"}:
        raise ValueError(f"Unknown profile image type: {kind}.")
    if not path.is_file():
        raise ConnectError(f"The {kind} file {path.name} was not found.")
    data = path.read_bytes()
    if len(data) > MAX_LOGO_BYTES:
        raise ConnectError(f"The {kind} must be 2 MB or smaller.")
    try:
        processed = process_image(data)
    except MediaError as exc:
        raise ConnectError(f"The {kind} could not be used: {exc}") from exc
    return f"{kind}.{processed.extension}", processed.content


def store_profile_image(data: bytes, kind: str, directory: Path) -> tuple[str, Path, bool]:
    """Sanitize and persist a console-uploaded image in the backed-up media directory."""
    if kind not in {"logo", "cover"}:
        raise ValueError(f"Unknown profile image type: {kind}.")
    if len(data) > MAX_LOGO_BYTES:
        raise ConnectError(f"The {kind} must be 2 MB or smaller.")
    try:
        processed = process_image(data)
    except MediaError as exc:
        raise ConnectError(f"The {kind} could not be used: {exc}") from exc
    if len(processed.content) > MAX_LOGO_BYTES:
        raise ConnectError(f"The processed {kind} must be 2 MB or smaller.")

    digest = hashlib.sha256(processed.content).hexdigest()[:16]
    filename = f"{kind}-{digest}.{processed.extension}"
    target = directory / filename
    existed = target.is_file()
    if existed:
        return filename, target, False

    temporary: Path | None = None
    try:
        directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=f".{kind}-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(processed.content)
        temporary.chmod(0o640)
        os.replace(temporary, target)
    except OSError as exc:
        raise ConnectError(f"The {kind} could not be saved.") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return filename, target, True


def render_page(
    profile: CommunityProfile,
    resolution: ChannelResolution,
    announcements: list[Announcement],
    logo_name: str | None,
    generated_at: str,
    cover_name: str | None = None,
) -> str:
    """Return the complete page. Every profile value is escaped, and nothing is loaded from elsewhere."""
    channels = "".join(
        f'<li><a href="{_e(ch.url)}">{_e(ch.label)}</a><br>'
        f'<span class="muted">{_e(ch.description)}</span></li>'
        if ch.url
        else f"<li><strong>{_e(ch.label)}</strong><br>"
        f'<span class="muted">{_e(ch.description)}</span>'
        f'<br>Address: <span class="address">{_e(ch.detail)}</span></li>'
        for ch in resolution.shown
    )
    independent_html = (
        f"<ul>{channels}</ul>"
        if channels
        else '<p class="muted">No independent channels are set up yet.</p>'
    )

    social_items = "".join(
        f'<li><a href="{_e(acc.url)}">{_e(acc.label)}</a>'
        + (f' <span class="muted">{_e(acc.handle)}</span>' if acc.handle else "")
        + "</li>"
        for acc in profile.social
    )
    social_html = (
        f"<ul>{social_items}</ul>"
        if social_items
        else '<p class="muted">No social network accounts are listed.</p>'
    )

    recent_items = "".join(
        f'<li><a href="{_e(a.canonical_url or f"{profile.url}/feed/announcements/{a.id}/")}">'
        f'{_e(a.title)}</a> <time datetime="{_e(a.published_at)}" class="muted">'
        f"{_e(a.published_at[:10])}</time></li>"
        for a in announcements
    )
    recent_html = (
        f"<ul>{recent_items}</ul>"
        if recent_items
        else '<p class="muted">No announcements have been published yet.</p>'
    )

    logo_html = (
        f'<img src="{_e(logo_name)}" alt="{_e(profile.name)} logo" width="72" height="72">'
        if logo_name
        else ""
    )
    cover_html = (
        f'<div class="cover"><img src="{_e(cover_name)}" alt=""></div>' if cover_name else ""
    )
    description_html = f"<p>{_e(profile.description)}</p>" if profile.description else ""
    contact_html = (
        f'<p>Contact: <a href="mailto:{_e(profile.contact_email)}">{_e(profile.contact_email)}</a></p>'
        if profile.contact_email
        else ""
    )
    emergency_html = (
        f'<section aria-labelledby="emergency"><h2 id="emergency">If social networks are unavailable</h2>'
        f"{_paragraphs(profile.emergency_instructions)}</section>"
        if profile.emergency_instructions.strip()
        else ""
    )
    generated_date = generated_at[:10]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_e(profile.name)} · Stay in touch</title>
  <meta name="description" content="{_e(profile.description or profile.name)}">
  <link rel="canonical" href="{_e(profile.connect_url)}">
  <style>{PAGE_CSS}</style>
</head>
<body>
  <header>
    {cover_html}
    <div class="identity">
      {logo_html}
      <div>
        <h1>{_e(profile.name)}</h1>
        {description_html}
        {contact_html}
      </div>
    </div>
  </header>
  <main>
    <section aria-labelledby="address">
      <h2 id="address">Bookmark this page</h2>
      <p>This address stays the same, even if we change social networks:</p>
      <p class="address"><a href="{_e(profile.connect_url)}">{_e(profile.connect_url)}</a></p>
      <p class="muted">Social networks can change their rules or stop working. Our own channels below do not depend on them.</p>
    </section>
    <section aria-labelledby="independent">
      <h2 id="independent">Our independent channels</h2>
      <p>These belong to the community. They keep working if a social network becomes unavailable.</p>
      {independent_html}
    </section>
    <section aria-labelledby="choose">
      <h2 id="choose">Which should I choose?</h2>
      <ul>
        <li><strong>Email</strong>: announcements arrive in your inbox. You confirm once, and you can stop with one click.</li>
        <li><strong>Feeds</strong>: no sign-up at all. A feed reader app shows new announcements for you.</li>
        <li><strong>Chat</strong>: follow announcements in a Matrix app such as Element, if you already use one.</li>
        <li><strong>Fediverse</strong>: follow us from Mastodon or a similar app, like any other account.</li>
      </ul>
      <p class="muted">Pick whichever you already use. You can change your mind at any time.</p>
    </section>
    <section aria-labelledby="social">
      <h2 id="social">Find us on social networks</h2>
      <p>Social networks are good for news and conversation. Use the channels above to make sure you do not miss anything.</p>
      {social_html}
    </section>
    {emergency_html}
    <section aria-labelledby="recent">
      <h2 id="recent">Recent announcements</h2>
      {recent_html}
    </section>
  </main>
  <footer>
    <p>Last updated {_e(generated_date)}.</p>
  </footer>
</body>
</html>
"""


def qr_svg(url: str) -> bytes:
    buffer = io.BytesIO()
    segno.make(url, error="m").save(buffer, kind="svg", scale=8, border=4)
    return buffer.getvalue()


def qr_png(url: str) -> bytes:
    buffer = io.BytesIO()
    segno.make(url, error="m").save(buffer, kind="png", scale=12, border=4)
    return buffer.getvalue()


def build_site(
    profile: CommunityProfile,
    resolution: ChannelResolution,
    announcements: list[Announcement],
    logo: tuple[str, bytes] | None,
    generated_at: str | None = None,
    cover: tuple[str, bytes] | None = None,
) -> dict[str, bytes]:
    """The files the live site needs: the page and its logo."""
    stamp = generated_at or datetime.now(timezone.utc).isoformat()
    page = render_page(
        profile,
        resolution,
        announcements,
        logo[0] if logo else None,
        stamp,
        cover[0] if cover else None,
    )
    files = {"index.html": page.encode("utf-8")}
    if logo:
        files[logo[0]] = logo[1]
    if cover:
        files[cover[0]] = cover[1]
    return files


def build_bundle(
    profile: CommunityProfile,
    resolution: ChannelResolution,
    announcements: list[Announcement],
    logo: tuple[str, bytes] | None,
    generated_at: str | None = None,
    cover: tuple[str, bytes] | None = None,
) -> dict[str, bytes]:
    """Everything for an identity package: page, logo, QR codes and machine-readable data."""
    stamp = generated_at or datetime.now(timezone.utc).isoformat()
    files = build_site(profile, resolution, announcements, logo, stamp, cover)
    data: dict[str, Any] = {
        "format": "community-stack/connect",
        "format_version": 1,
        "generated_at": stamp,
        "name": profile.name,
        "description": profile.description,
        "url": profile.url,
        "connect_url": profile.connect_url,
        "logo": logo[0] if logo else None,
        "cover": cover[0] if cover else None,
        "contact_email": profile.contact_email,
        "emergency_instructions": profile.emergency_instructions,
        "social": [
            {"platform": acc.platform, "label": acc.label, "url": acc.url, "handle": acc.handle}
            for acc in profile.social
        ],
        "independent_channels": [
            {"key": ch.key, "label": ch.label, "url": ch.url, "detail": ch.detail}
            for ch in resolution.shown
        ],
    }
    files["community.json"] = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()
    files["qr.svg"] = qr_svg(profile.connect_url)
    files["qr.png"] = qr_png(profile.connect_url)
    return files


def load_identity(
    config_path: Path | str,
    storage: AnnouncementStorage,
    require_images: bool = False,
) -> tuple[
    CommunityProfile,
    ChannelResolution,
    list[Announcement],
    tuple[str, bytes] | None,
    tuple[str, bytes] | None,
]:
    """Everything the page needs, read from the configuration and the canonical store.

    Images that cannot be read are left out of the page, so it still updates. The command-line
    tools pass ``require_images=True`` so the operator is told.
    """
    profile, config = load_community(config_path)
    connections = storage.list_connections()
    resolution = resolve_independent(profile, connections, config)
    announcements = recent_announcements(storage)
    images: dict[str, tuple[str, bytes] | None] = {"logo": None, "cover": None}
    for kind in images:
        path = profile_image_path(config_path, getattr(profile, kind))
        if path is None:
            continue
        try:
            images[kind] = prepare_profile_image(path, kind)
        except ConnectError:
            if require_images:
                raise
    return profile, resolution, announcements, images["logo"], images["cover"]


def offline_connect_files(
    config_path: Path | str, storage: AnnouncementStorage
) -> tuple[dict[str, bytes] | None, str | None]:
    """Identity files for an offline archive. A problem here returns a warning, never fails the archive."""
    try:
        profile, resolution, announcements, logo, cover = load_identity(config_path, storage)
    except (ConfigError, ConnectError) as exc:
        return None, f"Community contacts were left out of the archive: {exc}"
    return build_bundle(profile, resolution, announcements, logo, cover=cover), None


def write_files(directory: Path | str, files: dict[str, bytes]) -> None:
    """Write files atomically, and remove logo files left over from an earlier logo."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        final = target / name
        temp = target / f".{name}.tmp"
        temp.write_bytes(content)
        os.replace(temp, final)
    for kind in ("logo", "cover"):
        for stale in target.glob(f"{kind}.*"):
            if stale.name not in files:
                stale.unlink(missing_ok=True)
