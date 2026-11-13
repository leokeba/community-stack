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
from community_stack.announcements.formatting import with_simulation_label
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
:root { color-scheme: light; font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; line-height: 1.6; color: #17212b; background: #f4f7fa; }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark; color: #edf4f8; background: #111820; } }
* { box-sizing: border-box; }
body { margin: 0; }
main, header, footer { max-width: 54rem; margin: 0 auto; padding: 1rem; }
header { padding-top: 1rem; }
header nav { display: flex; flex-wrap: wrap; gap: 1rem; padding-bottom: 1.2rem; }
.cover img { display: block; width: 100%; max-height: 14rem; object-fit: cover; border-radius: .9rem; }
.identity { display: flex; gap: 1rem; align-items: center; padding: .75rem 0 1rem; }
.identity img { width: 4.5rem; height: 4.5rem; object-fit: contain; flex: none; }
h1 { font-size: clamp(1.8rem, 5vw, 2.5rem); line-height: 1.12; margin: 0 0 .4rem; letter-spacing: -.025em; }
h2 { font-size: 1.3rem; line-height: 1.25; margin: 0 0 .5rem; }
section { background: #fff; border: 1px solid #d3dee6; border-radius: .85rem; padding: 1.25rem clamp(1rem, 4vw, 1.6rem); margin: 1rem 0; }
@media (prefers-color-scheme: dark) { section { background: #1a2530; border-color: #354754; } }
ul { padding-left: 1.2rem; margin: .6rem 0 0; }
li { margin: .55rem 0; }
a { color: #075b8d; font-weight: 650; text-underline-offset: .2em; }
a:focus-visible { outline: 3px solid #e2a72e; outline-offset: 3px; border-radius: .15rem; }
@media (prefers-color-scheme: dark) { a { color: #80c9f2; } }
.site-nav { display: flex; flex-wrap: wrap; gap: 1rem; }
.site-nav a { color: #52616d; font-size: .95rem; }
@media (prefers-color-scheme: dark) { .site-nav a { color: #bccbd5; } }
.channel-list, .announcement-list { padding: 0; list-style: none; }
.channel-list li { padding: .9rem 0; margin: 0; border-bottom: 1px solid #e4ebf0; }
.channel-list li:last-child { border-bottom: 0; }
@media (prefers-color-scheme: dark) { .channel-list li { border-color: #354754; } }
.address { font-size: 1.05rem; overflow-wrap: anywhere; }
.muted { color: #52616d; }
@media (prefers-color-scheme: dark) { .muted { color: #bccbd5; } }
.warning { border-left: .3rem solid #b42318; background: #fff1f0; color: #61201b; }
.warning h2 { color: #8e2018; }
@media (prefers-color-scheme: dark) { .warning { background: #351f22; color: #ffd9d4; } .warning h2 { color: #ffb4a9; } }
footer { color: #52616d; font-size: .9rem; }
@media (prefers-color-scheme: dark) { footer { color: #bccbd5; } }
@media print {
  body { background: #fff; color: #000; }
  section { border: 1px solid #000; break-inside: avoid; }
  a { color: #000; text-decoration: none; }
  a::after { content: " (" attr(href) ")"; font-weight: normal; font-size: .85em; }
  header nav { display: none; }
}
"""


class ConnectError(ValueError):
    """Raised when the connect page cannot be built, such as a bad logo file."""


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _paragraphs(text: str) -> str:
    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    return "".join(f"<p>{_e(part).replace(chr(10), '<br>')}</p>" for part in parts)


def _channel_state(channel_states: dict[str, dict[str, str]], key: str) -> str:
    state = channel_states.get(key)
    if not state:
        return ""
    reason = f' <span class="muted">{_e(state["reason"])}</span>' if state.get("reason") else ""
    return (
        f'<br><span class="channel-state">{_e(state["status"].replace("_", " ").title())}</span>'
        f"{reason}"
    )


def render_continuity_notice(notice: dict[str, Any] | None) -> str:
    """Render the active notice fragment shared by /connect and the static homepage."""
    if not notice:
        return ""
    translations = notice.get("notice_translations") or notice.get("translations") or {}
    sections = "".join(
        f'<div lang="{_e(language)}"><h2>{_e(item["title"])}</h2>{_paragraphs(item["body"])}</div>'
        for language, item in sorted(translations.items())
    )
    if not sections:
        return ""
    return (
        '<section class="warning" id="continuity-notice" aria-labelledby="continuity-heading">'
        f'<h2 id="continuity-heading">Continuity notice</h2>{sections}</section>'
    )


def recent_announcements(storage: AnnouncementStorage) -> list[Announcement]:
    """Published or updated public announcements, newest first. Drafts never appear."""
    public = (AnnouncementStatus.PUBLISHED, AnnouncementStatus.UPDATED)
    items = [a for a in storage.list_announcements(limit=None) if a.status in public]
    return items[:RECENT_LIMIT]


def render_services_fragment(profile: CommunityProfile) -> str:
    if not profile.web_services:
        return '<p class="empty-state">No web apps are enabled right now.</p>'
    cards = "".join(
        f'<article class="service-card"><p class="service-kicker">Enabled</p>'
        f'<h3><a href="{_e(service.url)}">{_e(service.label)}</a></h3>'
        f"<p>{_e(service.description)}</p>"
        f'<a class="btn btn-primary" href="{_e(service.url)}">Open {_e(service.label)}</a></article>'
        for service in profile.web_services
    )
    return f'<div class="services-grid">{cards}</div>'


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
    channel_states: dict[str, dict[str, str]] | None = None,
    notice: dict[str, Any] | None = None,
    guide_available: bool = False,
) -> str:
    """Return the complete page. Every profile value is escaped, and nothing is loaded from elsewhere."""
    channel_states = channel_states or {}
    announcements = [with_simulation_label(item) for item in announcements]
    channels = "".join(
        f'<li><a href="{_e(ch.url)}">{_e(ch.label)}</a><br>'
        f'<span class="muted">{_e(ch.description)}</span>{_channel_state(channel_states, ch.key)}</li>'
        if ch.url
        else f"<li><strong>{_e(ch.label)}</strong><br>"
        f'<span class="muted">{_e(ch.description)}</span>'
        f'<br>Address: <span class="address">{_e(ch.detail)}</span>'
        f"{_channel_state(channel_states, ch.key)}</li>"
        for ch in resolution.shown
    )
    independent_html = (
        f'<ul class="channel-list">{channels}</ul>'
        if channels
        else '<p class="muted">No independent channels are set up yet.</p>'
    )

    social_items = "".join(
        f'<li><a href="{_e(acc.url)}">{_e(acc.label)}</a>'
        + (f' <span class="muted">{_e(acc.handle)}</span>' if acc.handle else "")
        + _channel_state(channel_states, acc.platform)
        + "</li>"
        for acc in profile.social
    )
    social_html = (
        f'<ul class="channel-list">{social_items}</ul>'
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
        f'<ul class="announcement-list">{recent_items}</ul>'
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
    notice_html = render_continuity_notice(notice)
    guide_html = (
        '<p><a href="/continuity/guide.zip">Download the contact and communication guide</a>'
        " (printable and usable offline).</p>"
        if guide_available
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
    <nav class="site-nav" aria-label="Primary navigation">
      <a href="/">Home</a>
      <a href="/feed/announcements.html">Announcements</a>
    </nav>
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
    {notice_html}
    <section aria-labelledby="address">
      <h2 id="address">Bookmark this page</h2>
      <p>Our stable address, even if social networks change:</p>
      <p class="address"><a href="{_e(profile.connect_url)}">{_e(profile.connect_url)}</a></p>
      <p class="muted">For the most reliable updates, use one of our community-owned channels below.</p>
    </section>
    <section aria-labelledby="independent">
      <h2 id="independent">Our independent channels</h2>
      {independent_html}
    </section>
    {emergency_html}
    <section aria-labelledby="social">
      <h2 id="social">Find us on social networks</h2>
      {social_html}
    </section>
    <section aria-labelledby="recent">
      <h2 id="recent">Recent announcements</h2>
      {recent_html}
      {guide_html}
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
    channel_states: dict[str, dict[str, str]] | None = None,
    notice: dict[str, Any] | None = None,
    guide_available: bool = False,
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
        channel_states,
        notice,
        guide_available,
    )
    files = {
        "index.html": page.encode("utf-8"),
        "continuity-notice.html": render_continuity_notice(notice).encode("utf-8"),
        "services.html": render_services_fragment(profile).encode("utf-8"),
    }
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
    channel_states: dict[str, dict[str, str]] | None = None,
    notice: dict[str, Any] | None = None,
) -> dict[str, bytes]:
    """Everything for an identity package: page, logo, QR codes and machine-readable data."""
    stamp = generated_at or datetime.now(timezone.utc).isoformat()
    files = build_site(
        profile,
        resolution,
        announcements,
        logo,
        stamp,
        cover,
        channel_states,
        notice,
    )
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
        "continuity_notice": notice,
        "channel_states": channel_states or {},
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
    from community_stack.continuity.store import ContinuityStore

    continuity = ContinuityStore(storage.db_path)
    return (
        build_bundle(
            profile,
            resolution,
            announcements,
            logo,
            cover=cover,
            channel_states=continuity.list_channel_states(),
            notice=continuity.active_notice(),
        ),
        None,
    )


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


def write_service_directory(
    config_path: Path | str,
    output_dir: Path | str | None = None,
) -> Path:
    """Refresh the landing-page web app list from the current enabled-service configuration."""
    profile, _ = load_community(config_path)
    target_dir = (
        Path(output_dir)
        if output_dir is not None
        else Path(config_path).resolve().parent / "data/landing/connect"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "services.html"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target_dir, prefix=".services-", delete=False
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(render_services_fragment(profile))
        assert temporary_path is not None
        temporary_path.chmod(0o644)
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return target
