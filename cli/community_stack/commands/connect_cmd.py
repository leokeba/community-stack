"""Implementation of the 'connect' CLI command: the community's stable contact page."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from community_stack.announcements.storage import AnnouncementStorage
from community_stack.community import address_warnings, connection_warnings
from community_stack.config import ConfigError
from community_stack.continuity.connect_page import (
    ConnectError,
    build_bundle,
    build_site,
    load_identity,
    qr_png,
    qr_svg,
    write_files,
)
from community_stack.continuity.offline_exporter import write_checksummed_zip

DEFAULT_QR_DIR = Path("backups/connect-qr")
DEFAULT_EXPORT = Path("backups/community-identity.zip")
DEFAULT_SITE_DIR = Path("data/landing/connect")


def run_connect(args: argparse.Namespace) -> int:
    """Entry point for the connect command."""
    action = getattr(args, "connect_action", None) or "show"
    config_path = getattr(args, "config", "community-stack.yaml")
    try:
        storage = AnnouncementStorage()
        if action == "show":
            return _show(config_path, storage)
        if action == "generate-qr":
            return _generate_qr(args, config_path, storage)
        if action == "export":
            return _export(args, config_path, storage)
        if action == "render":
            return _render(args, config_path, storage)
    except (ConfigError, ConnectError) as exc:
        print(f"[ERROR] {exc}")
        return 1
    print(f"Unknown connect action: {action}")
    return 2


def _show(config_path: Path | str, storage: AnnouncementStorage) -> int:
    profile, resolution, announcements, _logo, _cover = load_identity(
        config_path, storage, require_images=True
    )
    print(f"Community:    {profile.name}")
    print(f"Address:      {profile.url}")
    print(f"Stable page:  {profile.connect_url}")
    print(f"Logo:         {profile.logo or 'none'}")
    print(f"Announcements on the page: {len(announcements)}")

    print("\nIndependent channels shown:")
    for channel in resolution.shown:
        print(f"  - {channel.label}: {channel.url or channel.detail}")
    if not resolution.shown:
        print("  (none)")
    print("\nIndependent channels not shown:")
    for key, reason in resolution.hidden:
        print(f"  - {key}: {reason}")

    print("\nSocial networks:")
    for account in profile.social:
        print(f"  - {account.label}: {account.url}")
    if not profile.social:
        print("  (none listed)")

    for warning in address_warnings(profile.url, profile):
        print(f"\n[WARN] {warning}")
    for warning in connection_warnings(storage.list_connections()):
        print(f"\n[WARN] {warning}")
    return 0


def _generate_qr(args: argparse.Namespace, config_path: Path | str, storage) -> int:
    profile, _resolution, _announcements, _logo, _cover = load_identity(
        config_path, storage, require_images=True
    )
    destination = (getattr(args, "destination", None) or profile.connect_url).rstrip("/")
    if not destination.startswith("https://"):
        print("[ERROR] The QR destination must start with https://")
        return 1
    fmt = getattr(args, "format", "both")
    output_dir = Path(getattr(args, "output_dir", None) or DEFAULT_QR_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    if fmt in ("svg", "both"):
        written.append(_write(output_dir / "connect-qr.svg", qr_svg(destination)))
    if fmt in ("png", "both"):
        written.append(_write(output_dir / "connect-qr.png", qr_png(destination)))

    print(f"QR code for {destination}")
    for path in written:
        print(f"  Written: {path}")
    print("Print at least 3 cm (1.2 in) square, with its white border, for a phone camera to read.")
    for warning in address_warnings(destination, profile):
        print(f"[WARN] {warning}")
    return 0


def _export(args: argparse.Namespace, config_path: Path | str, storage) -> int:
    profile, resolution, announcements, logo, cover = load_identity(
        config_path, storage, require_images=True
    )
    now = datetime.now(timezone.utc).isoformat()
    files = build_bundle(profile, resolution, announcements, logo, now, cover)
    target = Path(getattr(args, "output", None) or DEFAULT_EXPORT)
    if target.suffix != ".zip":
        target = target.with_suffix(".zip")
    target.parent.mkdir(parents=True, exist_ok=True)
    write_checksummed_zip(target, files, profile.name, now, len(announcements))
    print("Identity package exported.")
    print(f"  Archive path: {target.resolve()}")
    print("  Contents: index.html (the page), community.json, qr.svg, qr.png, profile images")
    print("  Check it with: community-stack archive verify <path>")
    return 0


def _render(args: argparse.Namespace, config_path: Path | str, storage) -> int:
    profile, resolution, announcements, logo, cover = load_identity(
        config_path, storage, require_images=True
    )
    output_dir = Path(getattr(args, "output_dir", None) or DEFAULT_SITE_DIR)
    write_files(output_dir, build_site(profile, resolution, announcements, logo, cover=cover))
    print(f"Wrote the public /connect page to {output_dir.resolve()}")
    return 0


def _write(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path.resolve()
