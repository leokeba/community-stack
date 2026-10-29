"""Scheduled backups and off-site storage, run by the control agent."""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import yaml

from community_stack.agent.health import latest_backup_time, read_state, state_path
from community_stack.announcements.adapters.http import UnsafeURLError, assert_public_https
from community_stack.backup import BackupEngine, BackupError
from community_stack.config_edit import get_value, set_env_values, set_value, write_in_place
from community_stack.secrets import parse_env_file

logger = logging.getLogger(__name__)

INTERVALS = {
    "hourly": timedelta(hours=1),
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
}
RETRY_AFTER_FAILURE = timedelta(hours=1)
CHECK_EVERY_SECONDS = 300
BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
PREFIX = re.compile(r"[A-Za-z0-9._/-]{0,100}")
ACCESS_KEY = re.compile(r"[A-Za-z0-9]{8,128}")
SECRET_KEY = re.compile(r"[A-Za-z0-9/+=_-]{16,128}")
REGION = re.compile(r"[a-z0-9-]{2,32}")
ENDPOINT = re.compile(r"https://[A-Za-z0-9.-]{3,200}(:\d{2,5})?")


class BackupSettingsError(Exception):
    """A backup change the agent refuses or could not apply; shown to people."""


def _write_state(context, state: dict[str, Any]) -> None:
    path = state_path(context)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")
    path.chmod(0o600)


def _config(context) -> dict:
    return yaml.safe_load(context.config_path.read_text(encoding="utf-8")) or {}


def schedule_interval(config: dict) -> timedelta | None:
    backups = config.get("operations", {}).get("backups", {})
    if not backups.get("enabled", False):
        return None
    return INTERVALS.get(backups.get("schedule", "daily"), INTERVALS["daily"])


def backup_due(context, now: datetime) -> bool:
    interval = schedule_interval(_config(context))
    if interval is None:
        return False
    state = read_state(context)
    if state.get("last_result") == "failed":
        retry_at = datetime.fromisoformat(state["last_attempt"]) + RETRY_AFTER_FAILURE
        if now < retry_at:
            return False
    latest = latest_backup_time(context)
    return latest is None or now - latest >= interval


def run_scheduled_backup(context) -> dict[str, Any]:
    """Create and verify one backup, recording the outcome for the Health page."""
    now = datetime.now(timezone.utc)
    state = {"last_attempt": now.isoformat()}
    engine = context.backup_engine()
    try:
        snapshot = engine.create_snapshot()
        ok, messages = engine.verify_backup(snapshot_id=snapshot.id)
        if not ok:
            raise BackupError(next((m for m in messages if m.startswith("ERROR")), "check failed"))
    except BackupError as exc:
        state.update(last_result="failed", last_error=str(exc)[:500])
        _write_state(context, state)
        return state
    state.update(last_result="succeeded", last_snapshot=snapshot.id, last_error=None)
    _write_state(context, state)
    return state


class BackupScheduler(threading.Thread):
    """Checks every few minutes whether a scheduled backup is due, and runs it."""

    def __init__(self, service) -> None:
        super().__init__(name="backup-scheduler", daemon=True)
        self.service = service
        self.stopped = threading.Event()

    def run(self) -> None:
        while not self.stopped.wait(CHECK_EVERY_SECONDS):
            try:
                self.service.run_scheduled_backup_if_due()
            except Exception:
                logger.exception("Scheduled backup check failed.")


def describe_repository(engine: BackupEngine, env_values: dict[str, str]) -> str:
    """Where backups go, without credentials."""
    if engine.encryption != "restic":
        return f"Unencrypted archives on the server ({engine.backup_dir})"
    if engine.target == "s3":
        url = env_values.get("S3_BUCKET_URL", "").removeprefix("s3:")
        parts = urlsplit(url)
        return f"Encrypted, off-site: {parts.hostname or '?'}{parts.path}"
    if engine.target == "sftp":
        return "Encrypted, off-site over SFTP"
    return f"Encrypted, on the server's own disk ({engine.backup_dir / 'repository'})"


def backups_status(context, params: dict[str, Any]) -> dict[str, Any]:
    config = _config(context)
    engine = context.backup_engine()
    latest = None
    try:
        found = latest_backup_time(context)
        latest = found.isoformat() if found else None
    except BackupError as exc:
        latest = f"unknown ({exc})"
    return {
        "enabled": bool(get_value(config, ("operations", "backups", "enabled"), False)),
        "schedule": get_value(config, ("operations", "backups", "schedule"), "daily"),
        "target": engine.target,
        "encryption": engine.encryption,
        "where": describe_repository(engine, parse_env_file(context.env_path)),
        "latest_backup": latest,
        "scheduler": read_state(context),
    }


def _replace_settings(context, env_values: dict[str, str], config_values: dict) -> tuple[str, str]:
    """Write .env and configuration changes; return the previous texts for rollback."""
    old_env = context.env_path.read_text(encoding="utf-8")
    old_config = context.config_path.read_text(encoding="utf-8")
    new_config = old_config
    for path, value in config_values.items():
        new_config = set_value(new_config, path, value)
    if env_values:
        set_env_values(context.env_path, env_values)
    write_in_place(context.config_path, new_config)
    return old_env, old_config


def _restore_settings(context, old_env: str, old_config: str) -> None:
    write_in_place(context.env_path, old_env)
    write_in_place(context.config_path, old_config)


def use_offsite(context, params: dict[str, Any]) -> dict[str, Any]:
    """Point encrypted backups at an S3-compatible bucket, after proving it works."""
    endpoint = params["endpoint"].rstrip("/")
    try:
        assert_public_https(endpoint)
    except UnsafeURLError as exc:
        raise BackupSettingsError(str(exc)) from exc
    if shutil.which("restic") is None:
        raise BackupSettingsError("Install restic on the server first (sudo apt install restic).")
    if not parse_env_file(context.env_path).get("RESTIC_PASSWORD"):
        raise BackupSettingsError("RESTIC_PASSWORD must be set in the server's .env first.")
    prefix = params.get("prefix") or "community-stack"
    repository = f"s3:{endpoint}/{params['bucket']}/{prefix.strip('/')}"
    env_values = {
        "S3_BUCKET_URL": repository,
        "S3_ACCESS_KEY": params["access_key_id"],
        "S3_SECRET_KEY": params["secret_access_key"],
    }
    if params.get("region"):
        env_values["S3_REGION"] = params["region"]

    old_env, old_config = _replace_settings(
        context,
        env_values,
        {
            ("operations", "backups", "target"): "s3",
            ("operations", "backups", "encryption"): "restic",
        },
    )
    steps = ["Settings saved; checking the bucket."]
    try:
        engine = context.backup_engine()
        env = engine.get_backup_env()
        engine._ensure_restic_repository(env)
        steps.append("The bucket accepted an encrypted backup repository.")
        snapshot = engine.create_snapshot()
        ok, messages = engine.verify_backup(snapshot_id=snapshot.id)
        if not ok:
            raise BackupError(next((m for m in messages if m.startswith("ERROR")), "check failed"))
        steps.append(f"First off-site backup {snapshot.id} created and verified.")
    except BackupError as exc:
        _restore_settings(context, old_env, old_config)
        raise BackupSettingsError(
            f"The bucket could not be used, so backups stay where they were: {exc}"
        ) from exc
    _write_state(
        context,
        {
            "last_attempt": datetime.now(timezone.utc).isoformat(),
            "last_result": "succeeded",
            "last_snapshot": snapshot.id,
            "last_error": None,
        },
    )
    return {"steps": steps}


def use_local(context, params: dict[str, Any]) -> dict[str, Any]:
    """Go back to the encrypted repository on the server's own disk."""
    _replace_settings(context, {}, {("operations", "backups", "target"): "local"})
    return {"steps": ["Backups now go to the server's own disk. The bucket is left as it was."]}
