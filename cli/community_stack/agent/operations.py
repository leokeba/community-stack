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
from community_stack.agent import (
    backups,
    bridges,
    health,
    matrix_admin,
    updates,
    vikunja_users,
)
from community_stack.agent import community as community_profile
from community_stack.agent import safemode as safemode_ops
from community_stack.agent import settings as settings_catalog
from community_stack.backup import BackupEngine
from community_stack.bridges.catalogue import LOGIN_COMMAND, WHATSAPP, BridgeSpec, supported
from community_stack.collaborative_apps import COLLABORATIVE_APPS
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


def _community_param(params: dict) -> dict[str, Any]:
    if set(params) != {"community"} or not isinstance(params["community"], dict):
        raise OperationError("Send the community profile as 'community'.")
    return {"community": params["community"]}


def community_apply(context: AgentContext, params: dict[str, Any]) -> dict[str, Any]:
    try:
        return community_profile.apply(context, params["community"])
    except community_profile.CommunityError as exc:
        raise OperationError(str(exc)) from exc


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


def _matrix_call(handler):
    def run(context: AgentContext, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return handler(context, params)
        except matrix_admin.MatrixAdminError as exc:
            raise OperationError(str(exc)) from exc

    return run


def _safemode_call(handler):
    def run(context: AgentContext, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return handler(context, params)
        except safemode_ops.SafeModeOperationError as exc:
            raise OperationError(str(exc)) from exc

    return run


def _vikunja_call(handler):
    def run(context: AgentContext, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return handler(context, params)
        except vikunja_users.VikunjaUserError as exc:
            raise OperationError(str(exc)) from exc

    return run


def config_get(context: AgentContext, params: dict[str, Any]) -> dict[str, Any]:
    config = yaml.safe_load(context.config_path.read_text(encoding="utf-8")) or {}
    result = {"values": settings_catalog.current_values(config)}
    for app in COLLABORATIVE_APPS:
        result[f"{app.key}_url"] = app.public_url(config) if app.is_enabled(config) else None
    return result


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


def _bridge_call(handler):
    def run(context: AgentContext, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return handler(context, params)
        except bridges.BridgeError as exc:
            raise OperationError(str(exc)) from exc

    return run


def _bridge_call(handler):
    def run(context: AgentContext, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return handler(context, params)
        except bridges.BridgeError as exc:
            raise OperationError(str(exc)) from exc

    return run


def _bridge_operations(spec: BridgeSpec) -> tuple[Operation, ...]:
    """The console's operations for one bridge; every name is the bridge key, then the action."""
    key = spec.key
    command_pattern = re.compile(
        "|".join(
            [re.escape(command) for command in spec.commands]
            + ([bridges.LOGIN_FLOW_PATTERN.pattern] if not spec.login_flows else [])
            + [re.escape(f"{LOGIN_COMMAND} {flow}") for flow in spec.login_flows]
        )
    )
    room_pattern = re.compile(r"![A-Za-z0-9._=~-]{1,255}:[A-Za-z0-9.:-]{1,255}")
    operations = (
        Operation(
            f"bridge.{key}.status",
            READ,
            "system",
            _bridge_call(lambda context, params: bridges.status(context, spec)),
            summary=f"Whether the {spec.name} bridge is enabled, registered and running.",
        ),
        Operation(
            f"bridge.{key}.command",
            CHANGE,
            "system",
            _bridge_call(
                lambda context, params: bridges.run_command(context, params["command"], spec)
            ),
            params=(Param("command", command_pattern, required=True),),
            summary=f"Send a {spec.name} bridge bot command and show its replies.",
        ),
        Operation(
            f"bridge.{key}.rooms",
            READ,
            "system",
            _bridge_call(lambda context, params: {"rooms": bridges.portal_rooms(context, spec)}),
            summary=f"List the Matrix rooms the {spec.name} bridge created for chats.",
        ),
        Operation(
            f"bridge.{key}.login_state",
            CHANGE,
            "system",
            _bridge_call(lambda context, params: bridges.login_state(context, spec)),
            summary=f"Ask the {spec.name} bridge whether an account is linked.",
        ),
        Operation(
            f"bridge.{key}.room_relay",
            CHANGE,
            "system",
            _bridge_call(
                lambda context, params: bridges.set_room_relay(context, params["room_id"], spec)
            ),
            params=(Param("room_id", room_pattern, required=True),),
            summary=f"Let the linked account relay messages into one bridged {spec.name} room.",
        ),
        Operation(
            f"bridge.{key}.room_invite",
            CHANGE,
            "system",
            _bridge_call(
                lambda context, params: bridges.invite_portal_user(
                    context, params["room_id"], params["user_id"], spec
                )
            ),
            params=(
                Param("room_id", room_pattern, required=True),
                Param("user_id", matrix_admin.USER_ID, required=True),
            ),
            summary=f"Invite a Matrix account to one {spec.name} portal room.",
        ),
    )
    if key == WHATSAPP.key:
        operations += (
            Operation(
                "bridge.whatsapp.stale_portals",
                READ,
                "system",
                _bridge_call(lambda context, params: {"portals": bridges.stale_portals(context)}),
                summary="List allowlisted WhatsApp groups whose saved Matrix portal rooms were deleted.",
            ),
            Operation(
                "bridge.whatsapp.group_add",
                CHANGE,
                "system",
                _bridge_call(
                    lambda context, params: bridges.add_whatsapp_group(
                        context, params["invite_link"]
                    )
                ),
                params=(Param("invite_link", bridges.INVITE_LINK_PATTERN, required=True),),
                summary="Allow and join one WhatsApp group from an invite link.",
            ),
            Operation(
                "bridge.whatsapp.orphan_portal_remove",
                CHANGE,
                "system",
                _bridge_call(
                    lambda context, params: bridges.remove_orphaned_portal(
                        context, params["group_id"], params["confirmation"]
                    )
                ),
                params=(
                    Param("group_id", bridges.GROUP_ID_PATTERN, required=True),
                    Param("confirmation", re.compile("yes"), required=True),
                ),
                summary="Remove a saved WhatsApp group record that has no Matrix room.",
            ),
            Operation(
                "bridge.whatsapp.portal_mapping_repair",
                CHANGE,
                "system",
                _bridge_call(
                    lambda context, params: bridges.repair_stale_portal(
                        context,
                        params["group_id"],
                        params["confirmation"],
                        params.get("room_id", ""),
                    )
                ),
                params=(
                    Param("group_id", bridges.GROUP_ID_PATTERN, required=True),
                    Param("confirmation", re.compile("yes"), required=True),
                    Param("room_id", room_pattern),
                ),
                summary="Repair a deleted Matrix room mapping for one allowlisted WhatsApp group.",
            ),
            Operation(
                "bridge.whatsapp.group_disallow",
                CHANGE,
                "system",
                _bridge_call(
                    lambda context, params: bridges.disallow_whatsapp_group(
                        context, params["group_id"]
                    )
                ),
                params=(Param("group_id", bridges.GROUP_ID_PATTERN, required=True),),
                summary="Revoke one WhatsApp group's portal allowlist permission.",
            ),
            Operation(
                "bridge.whatsapp.portal_remove",
                CHANGE,
                "system",
                _bridge_call(
                    lambda context, params: bridges.remove_portal_room(
                        context, params["room_id"], params["confirmation"]
                    )
                ),
                params=(
                    Param("room_id", room_pattern, required=True),
                    Param("confirmation", re.compile("yes"), required=True),
                ),
                summary="Remove a WhatsApp group portal and its Matrix room.",
            ),
        )
    return operations


BRIDGE_OPERATIONS: tuple[Operation, ...] = tuple(
    op for spec in supported() for op in _bridge_operations(spec)
)


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
            "matrix.users.list",
            READ,
            "system",
            _matrix_call(matrix_admin.list_users),
            params=(
                Param("from", re.compile(r"\d{1,8}")),
                Param("deactivated", re.compile(r"(?:true|false)")),
            ),
            summary="List the Matrix accounts on this server.",
        ),
        Operation(
            "matrix.users.create",
            CHANGE,
            "system",
            _matrix_call(matrix_admin.create_user),
            params=(
                Param("username", re.compile(r"[a-z0-9._=/-]{1,64}"), required=True),
                Param("display_name", re.compile(r"[^\n\r]{1,100}")),
            ),
            summary="Create a Matrix account with a temporary password.",
        ),
        Operation(
            "matrix.users.reset_password",
            DANGEROUS,
            "system",
            _matrix_call(matrix_admin.reset_password),
            params=(Param("user_id", matrix_admin.USER_ID, required=True),),
            summary="Give a Matrix account a new temporary password and sign it out.",
        ),
        Operation(
            "matrix.users.deactivate",
            DANGEROUS,
            "system",
            _matrix_call(matrix_admin.deactivate_user),
            params=(Param("user_id", matrix_admin.USER_ID, required=True),),
            summary="Deactivate a Matrix account.",
        ),
        Operation(
            "safemode.overview",
            READ,
            "system",
            _safemode_call(safemode_ops.overview),
            summary="Show Safe Mode's bot state, protected rooms and aggregate account states.",
        ),
        Operation(
            "safemode.status",
            READ,
            "system",
            _safemode_call(safemode_ops.status),
            params=(Param("user_id", safemode_ops.USER_ID, required=True),),
            summary="Show one account's Safe Mode state, without room names.",
        ),
        Operation(
            "safemode.doctor",
            READ,
            "system",
            _safemode_call(safemode_ops.doctor),
            summary="Check every protected room against the compatibility rules.",
        ),
        Operation(
            "safemode.enroll",
            CHANGE,
            "system",
            _safemode_call(safemode_ops.enroll),
            params=(Param("user_id", safemode_ops.USER_ID, required=True),),
            summary="Enroll a Matrix account in Safe Mode.",
        ),
        Operation(
            "safemode.recovery_code",
            CHANGE,
            "system",
            _safemode_call(safemode_ops.recovery_code),
            params=(Param("user_id", safemode_ops.USER_ID, required=True),),
            summary="Issue a one-time Safe Mode recovery code, shown once.",
        ),
        Operation(
            "safemode.protect",
            CHANGE,
            "system",
            _safemode_call(safemode_ops.protect),
            params=(
                Param("room_id", safemode_ops.ROOM_ID, required=True),
                Param("name", safemode_ops.ROOM_NAME, required=True),
            ),
            summary="Protect a Matrix room if it is compatible.",
        ),
        Operation(
            "safemode.unprotect",
            CHANGE,
            "system",
            _safemode_call(safemode_ops.unprotect),
            params=(Param("room_id", safemode_ops.ROOM_ID, required=True),),
            summary="Stop protecting a Matrix room.",
        ),
        Operation(
            "safemode.restore",
            DANGEROUS,
            "system",
            _safemode_call(safemode_ops.restore),
            params=(Param("user_id", safemode_ops.USER_ID, required=True),),
            summary="Restore an account's Safe Mode access without its recovery code.",
        ),
        Operation(
            "vikunja.users.create",
            CHANGE,
            "system",
            _vikunja_call(vikunja_users.create_user),
            params=(
                Param("username", re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"), required=True),
                Param(
                    "email",
                    re.compile(r"[A-Za-z0-9][^\s@]{0,63}@[A-Za-z0-9.-]{1,189}\.[A-Za-z]{2,63}"),
                    required=True,
                ),
            ),
            summary="Create a Vikunja account and return its temporary password once.",
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
            "community.apply",
            CHANGE,
            "settings",
            community_apply,
            custom_validate=_community_param,
            summary="Save the community profile: name, contact, social accounts and channel choices.",
        ),
        Operation(
            "restore.drill",
            DANGEROUS,
            "system",
            restore_drill,
            params=(Param("snapshot_id", SNAPSHOT_ID),),
            summary="Restore a snapshot into a scratch directory, check it, and delete it.",
        ),
        *BRIDGE_OPERATIONS,
    )
}
