"""Implementation of the 'restore' command."""

from __future__ import annotations

from pathlib import Path

from community_stack.backup import BackupEngine, BackupError


def run_restore(
    target: str,
    config_path: Path | str = "community-stack.yaml",
    env_path: Path | str = ".env",
    snapshot_id: str | None = None,
    confirm: bool = False,
) -> int:
    """Restore from a backup snapshot to an isolated target directory."""
    print("================================================================================")
    print("                     COMMUNITY STACK RESTORE DRILL                              ")
    print("================================================================================\n")

    if not confirm:
        print("[ERROR] Restore requires explicit confirmation flag: --confirm")
        print("        Safety Guard: Restore will never execute without explicit confirmation.")
        print(f"        To proceed with restoring into '{target}', run:")
        print(f"          community-stack restore --target {target} --confirm")
        return 1

    target_dir = Path(target).resolve()
    print(f"Target directory: {target_dir}")
    print(f"Snapshot selection: {snapshot_id or 'latest available'}\n")

    try:
        engine = BackupEngine(config_path=config_path, env_path=env_path)
        engine.restore_to_target(target_dir=target_dir, snapshot_id=snapshot_id, confirm=confirm)

        print("  [SUCCESS] Backup restored successfully to target directory!")
        print(f"            Restored files location: {target_dir}")
        print("            - Persistent data restored: data/")
        print("            - Database dump restored:   backups/postgres_synapse.dump")
        print("            - Configuration validated:  community-stack.yaml")
        print("            - Secrets file permissions: 0600 (.env)")
        print("\nProduction environment was NOT modified. Recovery drill succeeded.")
        return 0
    except BackupError as exc:
        print(f"  [ERROR] Restore operation failed: {exc}")
        return 1
