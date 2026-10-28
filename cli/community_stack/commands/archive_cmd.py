"""Implementation of the 'archive' CLI command for offline continuity."""

from __future__ import annotations

import argparse
from pathlib import Path

from community_stack.continuity.offline_exporter import OfflineArchiveExporter


def run_archive(
    args: argparse.Namespace,
    config_path: Path | str = "community-stack.yaml",
) -> int:
    """Entry point for the archive command."""
    action = getattr(args, "archive_action", None)

    if not action or action == "export":
        default_out = Path("backups/offline_announcements.zip")
        raw_out = getattr(args, "output", None) or default_out
        output_path = Path(raw_out)

        exporter = OfflineArchiveExporter()
        print("Exporting self-contained offline announcement archive...\n")
        final_path = exporter.export_archive(output_path)

        size_kb = final_path.stat().st_size / 1024
        print("  [SUCCESS] Offline archive exported successfully!")
        print(f"            Archive path: {final_path}")
        print(f"            File size:    {size_kb:.2f} KB")
        print("\nDistribution options:")
        print("  - Copy to USB flash drives for distribution")
        print("  - Host on local captive portal Wi-Fi access points (Raspberry Pi / OpenWrt)")
        print("  - Host on local LAN web server during internet blackouts")
        return 0

    elif action == "verify":
        target = getattr(args, "path", None)
        if not target:
            print(
                "[ERROR] Please provide archive path to verify: community-stack archive verify <path>"
            )
            return 1

        is_valid, messages = OfflineArchiveExporter.verify_archive(target)
        for msg in messages:
            glyph = "[OK]  " if is_valid else "[FAIL]"
            print(f"  {glyph} {msg}")

        return 0 if is_valid else 1

    else:
        print(f"Unknown archive action: {action}")
        return 2
