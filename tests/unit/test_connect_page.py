"""Unit tests for the /connect page renderer, logo handling and QR codes."""

import io
import json
import xml.etree.ElementTree as ET

import pytest
from community_stack.announcements.models import Announcement, AnnouncementStatus
from community_stack.community import (
    ChannelResolution,
    CommunityProfile,
    IndependentChannel,
    SocialAccount,
)
from community_stack.continuity.connect_page import (
    ConnectError,
    build_bundle,
    build_site,
    prepare_logo,
    qr_png,
    qr_svg,
    render_page,
    store_profile_image,
    write_files,
)
from PIL import Image
from PIL.PngImagePlugin import PngInfo

GENERATED = "2026-11-01T10:00:00+00:00"


def _profile(**overrides) -> CommunityProfile:
    values = {
        "name": "Example Community",
        "url": "https://community.example.org",
        "domain": "community.example.org",
        "base_domain": "example.org",
        "description": "A community working together.",
        "logo": "logo.png",
        "contact_email": "hello@example.org",
        "emergency_instructions": "Use the feed.\n\nIf the chat is down, call the coordinator.",
        "social": (SocialAccount("instagram", "https://instagram.com/example", "@example"),),
        "independent": {"website": True, "rss": True},
    }
    values.update(overrides)
    return CommunityProfile(**values)


def _resolution() -> ChannelResolution:
    return ChannelResolution(
        shown=[
            IndependentChannel(
                key="website",
                label="Our website",
                description="Read on the website.",
                url="https://community.example.org/feed/announcements.html",
            ),
            IndependentChannel(
                key="activitypub",
                label="Fediverse",
                description="Follow us.",
                detail="@announcements@community.example.org",
            ),
        ],
        hidden=[],
    )


def _announcement(title: str, status: AnnouncementStatus, ann_id: str) -> Announcement:
    return Announcement(
        title=title,
        body="Body text",
        id=ann_id,
        status=status,
        published_at="2026-10-20T09:00:00+00:00",
        canonical_url=f"https://community.example.org/feed/announcements/{ann_id}/",
    )


def test_page_escapes_every_profile_value():
    profile = _profile(
        name='<script>alert("x")</script>',
        description="<img src=x onerror=alert(1)>",
        social=(SocialAccount("x", 'https://x.example/"><script>', "<b>@a</b>"),),
    )
    page = render_page(profile, _resolution(), [], None, GENERATED)
    assert "<script>alert" not in page
    assert "<img src=x" not in page
    assert "&lt;script&gt;alert" in page


def test_page_uses_no_javascript_and_no_outside_resources():
    page = render_page(_profile(), _resolution(), [], "logo.png", GENERATED)
    assert "<script" not in page.lower()
    assert 'src="http' not in page
    assert 'href="https://fonts' not in page
    assert '<link rel="stylesheet" href=' not in page


def test_page_includes_cover_image():
    page = render_page(_profile(cover="cover.jpg"), _resolution(), [], None, GENERATED, "cover.jpg")
    assert '<div class="cover"><img src="cover.jpg" alt=""></div>' in page


def test_page_is_printable_and_declares_its_stable_address():
    page = render_page(_profile(), _resolution(), [], None, GENERATED)
    assert "@media print" in page
    assert 'rel="canonical" href="https://community.example.org/connect/"' in page
    assert 'lang="en"' in page


def test_page_links_announcements_to_their_canonical_address():
    announcements = [_announcement("Public one", AnnouncementStatus.PUBLISHED, "ann_pub_0001")]
    page = render_page(_profile(), _resolution(), announcements, None, GENERATED)
    assert "Public one" in page
    assert 'href="https://community.example.org/feed/announcements/ann_pub_0001/"' in page


def test_page_shows_empty_states_and_emergency_text():
    page = render_page(
        _profile(social=(), independent={}), ChannelResolution([], []), [], None, GENERATED
    )
    assert "No independent channels are set up yet." in page
    assert "No social network accounts are listed." in page
    assert "No announcements have been published yet." in page
    assert "If social networks are unavailable" in page
    assert "<p>If the chat is down, call the coordinator.</p>" in page


def test_page_omits_emergency_section_when_not_configured():
    page = render_page(_profile(emergency_instructions=""), _resolution(), [], None, GENERATED)
    assert "If social networks are unavailable" not in page


def test_page_gives_the_address_as_plain_text_for_print():
    page = render_page(_profile(), _resolution(), [], None, GENERATED)
    assert "https://community.example.org/connect/" in page
    assert "Social networks can change their rules" in page


def test_prepare_logo_removes_metadata(tmp_path):
    path = tmp_path / "logo.png"
    info = PngInfo()
    info.add_text("Comment", "home address 12 rue secrete")
    Image.new("RGB", (64, 64), "red").save(path, pnginfo=info)

    # Opaque images are re-encoded as JPEG; the name follows the content.
    name, content = prepare_logo(path)
    assert name == "logo.jpg"
    assert content[:2] == b"\xff\xd8"
    assert b"rue secrete" not in content
    assert Image.open(io.BytesIO(content)).size == (64, 64)


def test_prepare_logo_rejects_non_images(tmp_path):
    path = tmp_path / "logo.png"
    path.write_text("not really a picture")
    with pytest.raises(ConnectError):
        prepare_logo(path)


def test_prepare_logo_reports_a_missing_file(tmp_path):
    with pytest.raises(ConnectError, match="not found"):
        prepare_logo(tmp_path / "logo.png")


def test_store_profile_image_reencodes_and_deduplicates(tmp_path):
    source = io.BytesIO()
    info = PngInfo()
    info.add_text("Comment", "private metadata")
    Image.new("RGB", (64, 64), "blue").save(source, format="PNG", pnginfo=info)

    directory = tmp_path / "data" / "announcements" / "media" / "community"
    filename, path, created = store_profile_image(source.getvalue(), "cover", directory)
    assert filename.startswith("cover-") and filename.endswith(".jpg")
    assert path.parent == directory
    assert created is True
    assert b"private metadata" not in path.read_bytes()

    repeated_name, repeated_path, repeated_created = store_profile_image(
        source.getvalue(), "cover", directory
    )
    assert repeated_name == filename
    assert repeated_path == path
    assert repeated_created is False


def test_qr_svg_is_well_formed_and_qr_png_decodes_as_image():
    svg = qr_svg("https://community.example.org/connect/")
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")

    png = qr_png("https://community.example.org/connect/")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert Image.open(io.BytesIO(png)).width > 100


def test_bundle_contains_identity_files_and_no_private_data():
    profile = _profile()
    bundle = build_bundle(profile, _resolution(), [], None, GENERATED)
    assert set(bundle) == {"index.html", "community.json", "qr.svg", "qr.png"}

    data = json.loads(bundle["community.json"])
    assert data["connect_url"] == "https://community.example.org/connect/"
    assert data["contact_email"] == "hello@example.org"
    assert [c["key"] for c in data["independent_channels"]] == ["website", "activitypub"]
    text = bundle["community.json"].decode()
    assert "subscriber" not in text.lower()


def test_bundle_contains_cover_image_and_metadata():
    bundle = build_bundle(
        _profile(cover="cover.jpg"),
        _resolution(),
        [],
        None,
        GENERATED,
        ("cover.jpg", b"cover bytes"),
    )
    assert bundle["cover.jpg"] == b"cover bytes"
    assert b'"cover": "cover.jpg"' in bundle["community.json"]
    assert b'src="cover.jpg"' in bundle["index.html"]


def test_build_site_includes_logo_when_given(tmp_path):
    profile = _profile()
    files = build_site(profile, _resolution(), [], ("logo.jpg", b"jpegbytes"), GENERATED)
    assert set(files) == {"index.html", "logo.jpg"}
    assert b'src="logo.jpg"' in files["index.html"]


def test_write_files_replaces_stale_logo(tmp_path):
    write_files(tmp_path, {"index.html": b"old", "logo.png": b"old-logo"})
    write_files(tmp_path, {"index.html": b"new", "logo.jpg": b"new-logo"})
    assert (tmp_path / "index.html").read_bytes() == b"new"
    assert not (tmp_path / "logo.png").exists()
    assert (tmp_path / "logo.jpg").read_bytes() == b"new-logo"
    assert not list(tmp_path.glob(".*.tmp"))
