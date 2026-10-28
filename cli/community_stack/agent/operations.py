"""The control agent's operation catalog: the only actions the console can request."""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from community_stack import docker
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

    def validate(self, params: Any) -> dict[str, str]:
        if not isinstance(params, dict):
            raise OperationError("Parameters must be an object.")
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
            "restore.drill",
            DANGEROUS,
            "system",
            restore_drill,
            params=(Param("snapshot_id", SNAPSHOT_ID),),
            summary="Restore a snapshot into a scratch directory, check it, and delete it.",
        ),
    )
}
