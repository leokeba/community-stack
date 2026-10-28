"""Implementation of the 'backup' and 'backup verify' commands."""

from __future__ import annotations

from pathlib import Path

from community_stack.backup import BackupEngine, BackupError


def run_backup(
    config_path: Path | str = "community-stack.yaml",
    env_path: Path | str = ".env",
) -> int:
    """Create an encrypted, timestamped backup snapshot."""
    print("Initiating Community Stack backup snapshot...\n")
    try:
        engine = BackupEngine(config_path=config_path, env_path=env_path)
        snapshot = engine.create_snapshot()
        print("  [SUCCESS] Backup snapshot created successfully!")
        print(f"            Snapshot ID:   {snapshot.id}")
        print(f"            Timestamp:     {snapshot.timestamp}")
        print(f"            Target:        {snapshot.target}")
        print(f"            Archive Size:  {snapshot.size_bytes / 1024:.2f} KB")
        print(
            f"            Database Dump: {'Included' if snapshot.db_included else 'Offline marker'}"
        )
        print("\nNext step: verify integrity with: community-stack backup verify")
        return 0
    except BackupError as exc:
        print(f"  [ERROR] Backup failed: {exc}")
        return 1


def run_backup_verify(
    config_path: Path | str = "community-stack.yaml",
    env_path: Path | str = ".env",
    snapshot_id: str | None = None,
) -> int:
    """Verify integrity and completeness of the latest or specified backup snapshot."""
    print("Verifying backup snapshot integrity...\n")
    try:
        engine = BackupEngine(config_path=config_path, env_path=env_path)
        is_valid, messages = engine.verify_backup(snapshot_id=snapshot_id)

        for msg in messages:
            if msg.startswith("ERROR"):
                print(f"  [FAIL] {msg}")
            elif msg.startswith("WARNING"):
                print(f"  [WARN] {msg}")
            else:
                print(f"  [OK]   {msg}")

        if is_valid:
            print("\n  [SUCCESS] Backup verification passed! Archive is intact and recoverable.")
            return 0
        else:
            print("\n  [FAIL] Backup verification failed. Review errors above.")
            return 1
    except BackupError as exc:
        print(f"  [ERROR] Verification failed: {exc}")
        return 1
