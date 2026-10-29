"""The control agent's operation catalog: the only actions the console can request."""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from community_stack import docker
from community_stack.agent import backups, health, updates
from community_stack.agent import settings as settings_catalog
from community_stack.backup import BackupEngine
from community_stack.config import ConfigError, load_config

READ = "read"
CHANGE = "change"
DANGEROUS = "dangerous"

SNAPSHOT_ID = re.compile(r"^snap_[0-9]{8}_[0-9]{6}Z$")


class OperationError(Exception):
    """A request the agent refuses or could not complete; the message is shown to people."""


@dataclass(frozen=True)
class Param:
    """A string parameter: requests may only carry values that match its pattern."""

    name: str
    pattern: re.Pattern[str]
    required: bool = False


@dataclass(frozen=True)
class AgentContext:
    """Paths the agent derives from its own configuration, never from requests."""

    project_dir: Path

    @property
    def config_path(self) -> Path:
        return self.project_dir / "community-stack.yaml"

    @property
    def env_path(self) -> Path:
        return self.project_dir / ".env"

    @property
    def admin_db_path(self) -> Path:
        return self.project_dir / "data" / "announcements" / "announcements.db"

    @property
    def services_dir(self) -> Path:
        return self.project_dir / "services"

    @property
    def audit_path(self) -> Path:
        return self.project_dir / "data" / "agent" / "audit.log"

    def backup_engine(self) -> BackupEngine:
        return BackupEngine(config_path=self.config_path, env_path=self.env_path)


@dataclass(frozen=True)
class Operation:
    name: str
    risk: str
    permission: str
    handler: Callable[[AgentContext, dict[str, str]], dict[str, Any]]
    params: tuple[Param, ...] = field(default_factory=tuple)
    summary: str = ""
    # For structured parameters: validates and returns them, raising OperationError.
    custom_validate: Callable[[dict], dict] | None = None
    # For operations whose danger depends on the request, such as stopping Matrix.
    needs_password: Callable[[dict], bool] | None = None

    def validate(self, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise OperationError("Parameters must be an object.")
        if self.custom_validate is not None:
            return self.custom_validate(params)
        allowed = {param.name: param for param in self.params}
        unknown = sorted(set(params) - set(allowed))
        if unknown:
            raise OperationError(f"Unknown parameter: {', '.join(unknown)}.")
        clean: dict[str, str] = {}
        for param in self.params:
            value = params.get(param.name)
            if value is None or value == "":
                if param.required:
                    raise OperationError(f"Missing parameter: {param.name}.")
                continue
            if not isinstance(value, str) or not param.pattern.fullmatch(value):
                raise OperationError(f"Invalid value for {param.name}.")
            clean[param.name] = value
        return clean


def status_get(context: AgentContext, params: dict[str, str]) -> dict[str, Any]:
    available = docker.is_docker_available()
    return {
        "docker_available": available,
        "containers": docker.list_community_containers() if available else [],
    }


def backup_create(context: AgentContext, params: dict[str, str]) -> dict[str, Any]:
    return asdict(context.backup_engine().create_snapshot())


def backup_verify(context: AgentContext, params: dict[str, str]) -> dict[str, Any]:
    ok, messages = context.backup_engine().verify_backup(snapshot_id=params.get("snapshot_id"))
    return {"ok": ok, "messages": messages}


def restore_drill(context: AgentContext, params: dict[str, str]) -> dict[str, Any]:
    """Restore a snapshot into a scratch directory, check it, then delete the copy."""
    engine = context.backup_engine()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    target = engine.backup_dir / ".restore-drills" / stamp
    checks: list[str] = []
    try:
        engine.restore_to_target(target, snapshot_id=params.get("snapshot_id"), confirm=True)
        try:
            load_config(target / "community-stack.yaml")
            checks.append("Configuration restored and valid.")
        except ConfigError as exc:
            raise OperationError(f"Restored configuration is invalid: {exc}") from exc
        checks.append(
            "Secrets file restored." if (target / ".env").is_file() else "No secrets file found."
        )
        checks.append(
            "Service data restored."
            if (target / "data").is_dir()
            else "No service data in the snapshot."
        )
        dump = target / "backups" / "postgres_synapse.dump"
        checks.append(
            "Database dump restored."
            if dump.is_file() and not dump.read_bytes().startswith(b"# Offline backup marker")
            else "Snapshot has no database dump (the database was offline)."
        )
        return {"ok": True, "checks": checks}
    finally:
        shutil.rmtree(target, ignore_errors=True)


def _changes_param(params: dict) -> dict[str, Any]:
    if set(params) != {"changes"}:
        raise OperationError("Send the settings to change as 'changes'.")
    try:
        settings_catalog.parse_changes(params["changes"])
    except settings_catalog.SettingsError as exc:
        raise OperationError(str(exc)) from exc
    return {"changes": params["changes"]}


def _backup_settings_call(handler):
    def run(context: AgentContext, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return handler(context, params)
        except backups.BackupSettingsError as exc:
            raise OperationError(str(exc)) from exc

    return run


def _update_call(context: AgentContext, params: dict[str, Any]) -> dict[str, Any]:
    try:
        return updates.update(context, params)
    except updates.UpdateError as exc:
        raise OperationError(str(exc)) from exc


def config_get(context: AgentContext, params: dict[str, Any]) -> dict[str, Any]:
    config = yaml.safe_load(context.config_path.read_text(encoding="utf-8")) or {}
    return {"values": settings_catalog.current_values(config)}


def config_plan(context: AgentContext, params: dict[str, Any]) -> dict[str, Any]:
    try:
        plan = settings_catalog.build_plan(context, params["changes"])
    except settings_catalog.SettingsError as exc:
        raise OperationError(str(exc)) from exc
    return settings_catalog.public_plan(plan)


def config_apply(context: AgentContext, params: dict[str, Any]) -> dict[str, Any]:
    try:
        return settings_catalog.apply_changes(context, params["changes"])
    except settings_catalog.SettingsError as exc:
        raise OperationError(str(exc)) from exc


CATALOG: dict[str, Operation] = {
    op.name: op
    for op in (
        Operation(
            "status.get",
            READ,
            "system",
            status_get,
            summary="List the stack's containers and their state.",
        ),
        Operation(
            "backup.create",
            CHANGE,
            "system",
            backup_create,
            summary="Create a backup snapshot.",
        ),
        Operation(
            "backup.verify",
            READ,
            "system",
            backup_verify,
            params=(Param("snapshot_id", SNAPSHOT_ID),),
            summary="Check that a snapshot is intact and complete.",
        ),
        Operation(
            "health.get",
            READ,
            "system",
            health.health_get,
            summary="Check disk space, backups, certificates and services.",
        ),
        Operation(
            "logs.get",
            READ,
            "system",
            health.logs_get,
            params=(
                Param("service", re.compile("|".join(health.LOG_SERVICES)), required=True),
                Param("lines", re.compile("|".join(health.LOG_LINES))),
            ),
            summary="Read a service's recent logs, with secrets removed.",
        ),
        Operation(
            "backups.status",
            READ,
            "system",
            backups.backups_status,
            summary="Where backups go, how often, and how the last automatic one went.",
        ),
        Operation(
            "backups.use_offsite",
            DANGEROUS,
            "system",
            _backup_settings_call(backups.use_offsite),
            params=(
                Param("endpoint", backups.ENDPOINT, required=True),
                Param("bucket", backups.BUCKET, required=True),
                Param("prefix", backups.PREFIX),
                Param("region", backups.REGION),
                Param("access_key_id", backups.ACCESS_KEY, required=True),
                Param("secret_access_key", backups.SECRET_KEY, required=True),
            ),
            summary="Send encrypted backups to an S3-compatible bucket, after testing it.",
        ),
        Operation(
            "backups.use_local",
            DANGEROUS,
            "system",
            _backup_settings_call(backups.use_local),
            summary="Send backups to the server's own disk again.",
        ),
        Operation(
            "services.versions",
            READ,
            "system",
            updates.versions,
            summary="Compare running service versions with this release's pinned versions.",
        ),
        Operation(
            "services.update",
            CHANGE,
            "system",
            _update_call,
            params=(Param("service", re.compile("|".join(updates.UPDATABLE)), required=True),),
            summary="Back up, update one service to its pinned version, roll back on failure.",
        ),
        Operation(
            "config.get",
            READ,
            "system",
            config_get,
            summary="Read the settings the console may change.",
        ),
        Operation(
            "config.plan",
            READ,
            "system",
            config_plan,
            custom_validate=_changes_param,
            summary="Show what a settings change would do, without doing it.",
        ),
        Operation(
            "config.apply",
            CHANGE,
            "system",
            config_apply,
            custom_validate=_changes_param,
            needs_password=lambda params: settings_catalog.requires_password(params["changes"]),
            summary="Back up, change settings, start or stop services, and roll back on failure.",
        ),
        Operation(
            "restore.drill",
            DANGEROUS,
            "system",
            restore_drill,
            params=(Param("snapshot_id", SNAPSHOT_ID),),
            summary="Restore a snapshot into a scratch directory, check it, and delete it.",
        ),
    )
}
