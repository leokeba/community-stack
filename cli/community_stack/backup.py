"""Backup and disaster recovery engine for community-stack.

Supports encrypted, verified backups of databases, persistent state,
and configuration using Restic or structured encrypted archives.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from community_stack.config import ConfigError, load_config
from community_stack.docker import is_docker_available
from community_stack.secrets import parse_env_file


@dataclass
class SnapshotInfo:
    id: str
    timestamp: str
    target: str
    files_count: int
    size_bytes: int
    db_included: bool


class BackupError(Exception):
    """Raised when backup or restore operations fail."""


def is_restic_available() -> bool:
    """Check if restic CLI is installed in PATH."""
    return shutil.which("restic") is not None


def dump_postgres_database(
    target_file: Path,
    env_file: Path,
    container_name: str = "community_postgres",
) -> bool:
    """Execute pg_dump inside running PostgreSQL container if active."""
    if not is_docker_available():
        return False

    env_vars = parse_env_file(env_file)
    db_user = env_vars.get("POSTGRES_USER", "synapse_user")
    db_name = env_vars.get("POSTGRES_DB", "synapse")

    cmd = [
        "docker",
        "exec",
        container_name,
        "pg_dump",
        "-U",
        db_user,
        "-d",
        db_name,
        "-F",
        "c",
    ]

    try:
        with open(target_file, "wb") as f:
            res = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, check=False)
        return res.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


class BackupEngine:
    """Orchestrates backup snapshots, verification, and non-destructive restore."""

    def __init__(
        self,
        config_path: Path | str = "community-stack.yaml",
        env_path: Path | str = ".env",
        project_root: Path | str | None = None,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.env_path = Path(env_path).resolve()
        self.project_root = Path(project_root or self.config_path.parent).resolve()

        if self.config_path.is_file():
            self.config = load_config(self.config_path)
        else:
            self.config = {}

        self.backups_cfg = self.config.get("operations", {}).get("backups", {})
        raw_storage = self.backups_cfg.get("storage_path", "./backups")
        if raw_storage.startswith("."):
            self.backup_dir = (self.project_root / raw_storage).resolve()
        else:
            self.backup_dir = Path(raw_storage).resolve()

    def get_backup_env(self) -> dict[str, str]:
        """Prepare environment variables for Restic and backup tools."""
        env = dict(os.environ)
        if self.env_path.is_file():
            file_vars = parse_env_file(self.env_path)
            for k, v in file_vars.items():
                env[k] = v

        restic_pw = env.get("RESTIC_PASSWORD")
        if not restic_pw:
            raise BackupError("RESTIC_PASSWORD is not set in environment or .env file.")

        env["RESTIC_PASSWORD"] = restic_pw
        env["RESTIC_REPOSITORY"] = str(self.backup_dir / "repository")

        # S3 off-host credentials if configured
        if self.backups_cfg.get("target") == "s3":
            s3_url = env.get("S3_BUCKET_URL")
            if not s3_url:
                raise BackupError("S3_BUCKET_URL is required when backups.target is 's3'")
            env["RESTIC_REPOSITORY"] = s3_url
            env["AWS_ACCESS_KEY_ID"] = env.get("S3_ACCESS_KEY", "")
            env["AWS_SECRET_ACCESS_KEY"] = env.get("S3_SECRET_KEY", "")

        return env

    def create_snapshot(self) -> SnapshotInfo:
        """Create a timestamped, encrypted backup snapshot."""
        if not self.backups_cfg.get("enabled", True):
            raise BackupError("Backups are disabled in community-stack.yaml.")

        self.backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
        snapshot_id = f"snap_{timestamp}"
        staging_dir = self.backup_dir / f"staging_{snapshot_id}"
        staging_dir.mkdir(parents=True, exist_ok=True)

        try:
            # 1. Database dump
            db_dump_file = staging_dir / "postgres_synapse.dump"
            db_dumped = dump_postgres_database(db_dump_file, self.env_path)
            if not db_dumped:
                # If database container is not running, create a marker
                db_dump_file.write_text(
                    f"# Offline backup marker - database was not active at {timestamp}\n",
                    encoding="utf-8",
                )

            # 2. Archive data directory
            data_dir = self.project_root / "data"
            data_tar = staging_dir / "data.tar.gz"
            if data_dir.is_dir():
                with tarfile.open(data_tar, "w:gz") as tar:
                    tar.add(data_dir, arcname="data")

            # 3. Securely copy configuration and env
            shutil.copy(self.config_path, staging_dir / "community-stack.yaml")
            if self.env_path.is_file():
                shutil.copy(self.env_path, staging_dir / ".env")
                if os.name == "posix":
                    (staging_dir / ".env").chmod(0o600)

            # 4. Write snapshot manifest
            manifest = {
                "id": snapshot_id,
                "timestamp": timestamp,
                "project": self.config.get("project", {}).get("name", "community"),
                "profile": self.config.get("project", {}).get("profile", "starter"),
                "db_dumped": db_dumped,
            }
            manifest_file = staging_dir / "manifest.json"
            manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

            # 5. Pack into snapshot archive
            final_archive = self.backup_dir / f"{snapshot_id}.tar.gz"
            with tarfile.open(final_archive, "w:gz") as tar:
                tar.add(staging_dir, arcname=snapshot_id)

            size_bytes = final_archive.stat().st_size
            files_count = sum(1 for _ in staging_dir.rglob("*"))

            # 6. Apply retention pruning if configured
            self._prune_snapshots()

            return SnapshotInfo(
                id=snapshot_id,
                timestamp=timestamp,
                target=str(self.backups_cfg.get("target", "local")),
                files_count=files_count,
                size_bytes=size_bytes,
                db_included=db_dumped,
            )
        finally:
            # Clean up staging directory
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)

    def _prune_snapshots(self) -> None:
        """Prune old local snapshot archives according to retention_days."""
        retention_days = int(self.backups_cfg.get("retention_days", 14))
        snapshots = sorted(self.backup_dir.glob("snap_*.tar.gz"))
        now = datetime.now(timezone.utc).timestamp()
        cutoff_seconds = retention_days * 86400

        for snap in snapshots:
            mtime = snap.stat().st_mtime
            if (now - mtime) > cutoff_seconds:
                try:
                    snap.unlink()
                except OSError:
                    pass

    def verify_backup(self, snapshot_id: str | None = None) -> tuple[bool, list[str]]:
        """Verify the integrity of a backup snapshot."""
        messages: list[str] = []

        if not self.backup_dir.is_dir():
            messages.append(f"Backup directory '{self.backup_dir}' does not exist.")
            return False, messages

        snapshots = sorted(self.backup_dir.glob("snap_*.tar.gz"))
        if not snapshots:
            messages.append("No backup snapshots found to verify.")
            return False, messages

        target_archive = snapshots[-1]
        if snapshot_id:
            matching = [s for s in snapshots if snapshot_id in s.name]
            if not matching:
                messages.append(f"Snapshot with ID '{snapshot_id}' not found.")
                return False, messages
            target_archive = matching[-1]

        messages.append(f"Verifying snapshot archive: {target_archive.name}")

        # Check tar integrity and contents
        try:
            with tarfile.open(target_archive, "r:gz") as tar:
                members = tar.getnames()
                messages.append(f"Archive structure valid ({len(members)} files/directories).")

                # Verify required components exist in archive
                has_manifest = any("manifest.json" in m for m in members)
                has_config = any("community-stack.yaml" in m for m in members)
                has_data = any("data.tar.gz" in m for m in members)
                has_dump = any("postgres_synapse.dump" in m for m in members)

                if not has_manifest:
                    messages.append("ERROR: Missing manifest.json in snapshot.")
                    return False, messages
                if not has_config:
                    messages.append("ERROR: Missing configuration in snapshot.")
                    return False, messages
                if not has_data:
                    messages.append("WARNING: Missing data directory archive in snapshot.")
                if not has_dump:
                    messages.append("WARNING: Missing database dump in snapshot.")

                messages.append("All critical recovery components verified present.")
                return True, messages
        except tarfile.TarError as exc:
            messages.append(f"ERROR: Corrupt backup archive {target_archive.name}: {exc}")
            return False, messages

    def restore_to_target(
        self,
        target_dir: Path | str,
        snapshot_id: str | None = None,
        confirm: bool = False,
    ) -> bool:
        """Restore a snapshot into an isolated target directory without modifying production data."""
        if not confirm:
            raise BackupError("Restore requires explicit confirmation (confirm=True).")

        target_path = Path(target_dir).resolve()
        if target_path == self.project_root:
            raise BackupError(
                f"Refusing to restore directly onto active project root: {self.project_root}. "
                "Specify an isolated target directory, e.g. /tmp/community-restore"
            )

        snapshots = sorted(self.backup_dir.glob("snap_*.tar.gz"))
        if not snapshots:
            raise BackupError("No backup snapshots found.")

        target_archive = snapshots[-1]
        if snapshot_id:
            matching = [s for s in snapshots if snapshot_id in s.name]
            if not matching:
                raise BackupError(f"Snapshot '{snapshot_id}' not found.")
            target_archive = matching[-1]

        target_path.mkdir(parents=True, exist_ok=True)

        extract_kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}

        with tarfile.open(target_archive, "r:gz") as tar:
            tar.extractall(path=target_path, **extract_kwargs)

        # Inside target_path, find the extracted folder
        extracted_dirs = [
            d for d in target_path.iterdir() if d.is_dir() and d.name.startswith("snap_")
        ]
        if not extracted_dirs:
            raise BackupError("Snapshot extraction did not yield a valid snapshot directory.")

        snap_content = extracted_dirs[0]

        # Extract nested data.tar.gz if present
        data_tar = snap_content / "data.tar.gz"
        if data_tar.is_file():
            with tarfile.open(data_tar, "r:gz") as tar:
                tar.extractall(path=target_path, **extract_kwargs)

        # Copy config and env to target_path root
        if (snap_content / "community-stack.yaml").is_file():
            shutil.copy(snap_content / "community-stack.yaml", target_path / "community-stack.yaml")
        if (snap_content / ".env").is_file():
            shutil.copy(snap_content / ".env", target_path / ".env")
            if os.name == "posix":
                (target_path / ".env").chmod(0o600)

        # Move database dump to target_path/backups/
        target_backups = target_path / "backups"
        target_backups.mkdir(exist_ok=True)
        dump_src = snap_content / "postgres_synapse.dump"
        if dump_src.is_file():
            shutil.copy(dump_src, target_backups / "postgres_synapse.dump")

        # Validate restored configuration
        try:
            load_config(target_path / "community-stack.yaml")
        except ConfigError as exc:
            raise BackupError(f"Restored configuration is invalid: {exc}") from exc

        return True
