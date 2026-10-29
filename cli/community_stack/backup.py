"""Backup and disaster recovery engine for community-stack.

Snapshots databases, persistent state, and configuration. With
``operations.backups.encryption: restic`` they go into a client-side encrypted
Restic repository (local, S3 or SFTP); otherwise into owner-only, unencrypted
local archives.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
from dataclasses import dataclass, field
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
    encrypted: bool = False
    warnings: list[str] = field(default_factory=list)


class BackupError(Exception):
    """Raised when backup or restore operations fail."""


RESTIC_TAG = "community-stack"
# Only these host variables reach restic, so other secrets in .env stay out of it.
RESTIC_ENV_PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "XDG_CACHE_HOME")
RESTIC_SETTINGS = ("RESTIC_CACHE_DIR", "RESTIC_REPOSITORY")
REQUIRED_SNAPSHOT_FILES = ("manifest.json", "community-stack.yaml")
EXPECTED_SNAPSHOT_FILES = ("data.tar.gz", "postgres_synapse.dump")


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
        self.target = self.backups_cfg.get("target", "local")
        self.encryption = self.backups_cfg.get("encryption", "none")

    def get_backup_env(self) -> dict[str, str]:
        """Prepare the minimal environment restic needs for the configured target."""
        file_vars = parse_env_file(self.env_path) if self.env_path.is_file() else {}

        def setting(key: str) -> str | None:
            return file_vars.get(key) or os.environ.get(key)

        env = {key: os.environ[key] for key in RESTIC_ENV_PASSTHROUGH if key in os.environ}
        for key in RESTIC_SETTINGS:
            if setting(key):
                env[key] = setting(key)

        restic_pw = setting("RESTIC_PASSWORD")
        if not restic_pw:
            raise BackupError("RESTIC_PASSWORD is not set in environment or .env file.")
        env["RESTIC_PASSWORD"] = restic_pw

        if self.target == "s3":
            s3_url = setting("S3_BUCKET_URL")
            if not s3_url:
                raise BackupError("S3_BUCKET_URL is required when backups.target is 's3'")
            env["RESTIC_REPOSITORY"] = s3_url
            env["AWS_ACCESS_KEY_ID"] = setting("S3_ACCESS_KEY") or ""
            env["AWS_SECRET_ACCESS_KEY"] = setting("S3_SECRET_KEY") or ""
            if setting("S3_REGION"):
                env["AWS_DEFAULT_REGION"] = setting("S3_REGION")
        elif self.target == "sftp":
            if not env.get("RESTIC_REPOSITORY"):
                raise BackupError(
                    "RESTIC_REPOSITORY (for example sftp:user@host:/srv/restic) is required "
                    "when backups.target is 'sftp'"
                )
        else:
            env["RESTIC_REPOSITORY"] = str(self.backup_dir / "repository")
        return env

    def _restic(self, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        binary = shutil.which("restic", path=env.get("PATH"))
        if binary is None:
            raise BackupError(
                "Backups are set to 'encryption: restic' but restic is not installed. "
                "Install it (for example 'apt install restic') and try again."
            )
        return subprocess.run([binary, *args], env=env, capture_output=True, text=True, check=False)

    def _restic_ok(self, args: list[str], env: dict[str, str], action: str) -> str:
        result = self._restic(args, env)
        if result.returncode != 0:
            detail = (result.stderr.strip().splitlines() or ["no error output"])[-1]
            raise BackupError(f"restic could not {action}: {detail}")
        return result.stdout

    def _ensure_restic_repository(self, env: dict[str, str]) -> None:
        if self._restic(["cat", "config"], env).returncode == 0:
            return
        if self.target == "local":
            self.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._restic_ok(["init"], env, "initialize the backup repository")

    def _restic_snapshots(self, env: dict[str, str]) -> list[dict]:
        output = self._restic_ok(
            ["snapshots", "--json", "--tag", RESTIC_TAG], env, "list snapshots"
        )
        snapshots = json.loads(output or "[]") or []
        for snapshot in snapshots:
            snapshot["snapshot_tag"] = next(
                (tag for tag in snapshot.get("tags") or [] if tag.startswith("snap_")), ""
            )
        return sorted(snapshots, key=lambda s: s["snapshot_tag"])

    def _find_restic_snapshot(self, env: dict[str, str], snapshot_id: str | None) -> dict:
        snapshots = self._restic_snapshots(env)
        if snapshot_id:
            snapshots = [s for s in snapshots if snapshot_id in s["snapshot_tag"]]
            if not snapshots:
                raise BackupError(f"Snapshot '{snapshot_id}' not found.")
        if not snapshots:
            raise BackupError("No backup snapshots found.")
        return snapshots[-1]

    def create_snapshot(self) -> SnapshotInfo:
        """Create a timestamped, owner-only (0600) backup snapshot.

        Local snapshots are gzip archives, not encrypted: they contain .env, the
        database dump, and TLS keys, so they must stay on protected storage.
        """
        if not self.backups_cfg.get("enabled", True):
            raise BackupError("Backups are disabled in community-stack.yaml.")

        self.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
        snapshot_id = f"snap_{timestamp}"
        staging_dir = self.backup_dir / f"staging_{snapshot_id}"
        staging_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

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
                announcement_db = data_dir / "announcements" / "announcements.db"
                sqlite_snapshot = staging_dir / "announcements.db.snapshot"
                if announcement_db.is_file():
                    try:
                        with (
                            sqlite3.connect(announcement_db) as source,
                            sqlite3.connect(sqlite_snapshot) as destination,
                        ):
                            source.backup(destination)
                    except sqlite3.Error as exc:
                        raise BackupError(
                            f"Could not create a consistent announcement database snapshot: {exc}"
                        ) from exc

                def exclude_live_sqlite_files(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
                    relative_name = member.name.removeprefix("data/")
                    if (
                        relative_name == "announcements/announcements.db"
                        or relative_name.startswith("announcements/announcements.db-")
                    ):
                        return None
                    return member

                with tarfile.open(data_tar, "w:gz") as tar:
                    tar.add(data_dir, arcname="data", filter=exclude_live_sqlite_files)
                    if sqlite_snapshot.is_file():
                        tar.add(
                            sqlite_snapshot,
                            arcname="data/announcements/announcements.db",
                        )
                if sqlite_snapshot.is_file():
                    sqlite_snapshot.unlink()

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

            files_count = sum(1 for _ in staging_dir.rglob("*"))
            if self.encryption == "restic":
                return self._store_in_restic(
                    staging_dir, snapshot_id, timestamp, files_count, db_dumped
                )

            # 5. Pack into snapshot archive
            final_archive = self.backup_dir / f"{snapshot_id}.tar.gz"
            fd = os.open(final_archive, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as archive_file:
                if os.name == "posix":
                    os.fchmod(archive_file.fileno(), 0o600)
                with tarfile.open(fileobj=archive_file, mode="w:gz") as tar:
                    tar.add(staging_dir, arcname=snapshot_id)

            size_bytes = final_archive.stat().st_size

            # 6. Apply retention pruning if configured
            self._prune_snapshots()

            warnings = ["Snapshot is not encrypted. Set operations.backups.encryption: restic."]
            if self.target != "local":
                warnings.append(
                    f"Target '{self.target}' needs encryption: restic; "
                    "this snapshot was only stored locally."
                )
            return SnapshotInfo(
                id=snapshot_id,
                timestamp=timestamp,
                target="local",
                files_count=files_count,
                size_bytes=size_bytes,
                db_included=db_dumped,
                warnings=warnings,
            )
        finally:
            # Clean up staging directory
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)

    def _store_in_restic(
        self,
        staging_dir: Path,
        snapshot_id: str,
        timestamp: str,
        files_count: int,
        db_dumped: bool,
    ) -> SnapshotInfo:
        """Back up the staged snapshot into the restic repository and apply retention."""
        env = self.get_backup_env()
        self._ensure_restic_repository(env)
        output = self._restic_ok(
            ["backup", "--json", "--tag", RESTIC_TAG, "--tag", snapshot_id, str(staging_dir)],
            env,
            "create the backup",
        )
        summary = next(
            (
                line
                for line in map(json.loads, filter(None, output.splitlines()))
                if line.get("message_type") == "summary"
            ),
            {},
        )
        warnings = []
        retention_days = int(self.backups_cfg.get("retention_days", 14))
        forget = self._restic(
            ["forget", "--tag", RESTIC_TAG, "--keep-within", f"{retention_days}d", "--prune"],
            env,
        )
        if forget.returncode != 0:
            warnings.append("Old snapshots could not be pruned; the new snapshot is safe.")
        return SnapshotInfo(
            id=snapshot_id,
            timestamp=timestamp,
            target=self.target,
            files_count=files_count,
            size_bytes=int(summary.get("data_added", 0)),
            db_included=db_dumped,
            encrypted=True,
            warnings=warnings,
        )

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
        if self.encryption == "restic":
            return self._verify_restic(snapshot_id)
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

    def _verify_restic(self, snapshot_id: str | None) -> tuple[bool, list[str]]:
        messages: list[str] = []
        env = self.get_backup_env()
        try:
            snapshot = self._find_restic_snapshot(env, snapshot_id)
        except BackupError as exc:
            return False, [f"ERROR: {exc}"]
        messages.append(
            f"Verifying encrypted snapshot {snapshot['snapshot_tag']} ({snapshot['short_id']})."
        )
        check = self._restic(["check"], env)
        if check.returncode != 0:
            messages.append("ERROR: restic repository check failed; the repository is damaged.")
            return False, messages
        messages.append("Repository structure and encryption verified by restic.")

        listing = self._restic_ok(["ls", "--json", snapshot["id"]], env, "list the snapshot")
        root = snapshot["paths"][0].rstrip("/")
        names = {
            node["path"][len(root) + 1 :]
            for node in map(json.loads, filter(None, listing.splitlines()))
            # Older restic releases mark nodes only with struct_type.
            if "node" in (node.get("message_type"), node.get("struct_type"))
            and node.get("path", "").startswith(root + "/")
        }
        missing = [name for name in REQUIRED_SNAPSHOT_FILES if name not in names]
        if missing:
            messages.append(f"ERROR: Missing {', '.join(missing)} in snapshot.")
            return False, messages
        for name in EXPECTED_SNAPSHOT_FILES:
            if name not in names:
                messages.append(f"WARNING: Missing {name} in snapshot.")
        messages.append("All critical recovery components verified present.")
        return True, messages

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

        if self.encryption == "restic":
            env = self.get_backup_env()
            snapshot = self._find_restic_snapshot(env, snapshot_id)
            target_path.mkdir(mode=0o700, parents=True, exist_ok=True)
            scratch = target_path / ".restic-restore"
            self._restic_ok(
                ["restore", snapshot["id"], "--target", str(scratch)], env, "restore the snapshot"
            )
            restored = scratch / snapshot["paths"][0].lstrip("/")
            snap_content = target_path / (snapshot["snapshot_tag"] or snapshot["short_id"])
            shutil.move(str(restored), str(snap_content))
            shutil.rmtree(scratch, ignore_errors=True)
            return self._finish_restore(snap_content, target_path)

        snapshots = sorted(self.backup_dir.glob("snap_*.tar.gz"))
        if not snapshots:
            raise BackupError("No backup snapshots found.")

        target_archive = snapshots[-1]
        if snapshot_id:
            matching = [s for s in snapshots if snapshot_id in s.name]
            if not matching:
                raise BackupError(f"Snapshot '{snapshot_id}' not found.")
            target_archive = matching[-1]

        target_path.mkdir(mode=0o700, parents=True, exist_ok=True)

        extract_kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}

        with tarfile.open(target_archive, "r:gz") as tar:
            tar.extractall(path=target_path, **extract_kwargs)

        # Inside target_path, find the extracted folder
        extracted_dirs = [
            d for d in target_path.iterdir() if d.is_dir() and d.name.startswith("snap_")
        ]
        if not extracted_dirs:
            raise BackupError("Snapshot extraction did not yield a valid snapshot directory.")

        return self._finish_restore(extracted_dirs[0], target_path)

    def _finish_restore(self, snap_content: Path, target_path: Path) -> bool:
        """Unpack a snapshot directory's data, config, secrets and dump into the target."""
        extract_kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}

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
