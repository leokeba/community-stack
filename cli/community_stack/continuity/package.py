"""Printable, checksummed contact and communication guide for offline use."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from community_stack.announcements.formatting import with_simulation_label
from community_stack.announcements.models import AnnouncementStatus
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.continuity.connect_page import build_bundle, load_identity
from community_stack.continuity.offline_exporter import write_checksummed_zip
from community_stack.continuity.store import ContinuityStore

DEFAULT_GUIDE = Path("data/landing/continuity/guide.zip")


def build_continuity_package(
    config_path: Path | str,
    storage: AnnouncementStorage,
    output_path: Path | str = DEFAULT_GUIDE,
) -> Path:
    """Create an offline guide from public community data, never subscriber records."""
    profile, resolution, announcements, logo, cover = load_identity(config_path, storage)
    continuity = ContinuityStore(storage.db_path)
    channel_states = continuity.list_channel_states()
    notice = continuity.active_notice()
    stamp = datetime.now(timezone.utc).isoformat()
    bundle = build_bundle(
        profile,
        resolution,
        announcements,
        logo,
        stamp,
        cover,
        channel_states,
        notice,
    )
    public_announcements = [
        with_simulation_label(item)
        for item in storage.list_announcements(status=None, limit=200)
        if item.status in (AnnouncementStatus.PUBLISHED, AnnouncementStatus.UPDATED)
    ]
    emergency = profile.emergency_instructions
    if notice:
        emergency = "\n\n".join(part for part in (emergency, notice["reason"]) if part)
    files = {
        "index.html": bundle["index.html"],
        "guide.html": bundle["index.html"],
        "community.json": bundle["community.json"],
        "announcements.json": (
            json.dumps(
                [item.to_dict() for item in public_announcements], indent=2, ensure_ascii=False
            )
            + "\n"
        ).encode("utf-8"),
        "emergency-procedures.txt": emergency.encode("utf-8"),
        "qr.svg": bundle["qr.svg"],
        "qr.png": bundle["qr.png"],
    }
    files.update(
        {name: content for name, content in bundle.items() if name.startswith(("logo.", "cover."))}
    )
    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".continuity-guide-", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        write_checksummed_zip(
            temporary,
            files,
            profile.name,
            stamp,
            len(public_announcements),
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
