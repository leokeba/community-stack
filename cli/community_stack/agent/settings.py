"""Settings the console may change, and the plan/backup/apply/check/rollback pipeline."""

from __future__ import annotations

import difflib
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from community_stack import docker
from community_stack.backup import BackupError
from community_stack.config import validate_config
from community_stack.config_edit import get_value, set_value, write_in_place
from community_stack.provision import ProvisionError, prepare_service
from community_stack.secrets import parse_env_file

HEALTH_TIMEOUT_SECONDS = 180
HEALTH_POLL_SECONDS = 3


class SettingsError(Exception):
    """A change the agent refuses or could not apply; the message is shown to people."""


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    help: str
    kind: str  # "bool", "int", "choice" or "timezone"
    default: Any
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    service: str | None = None
    dangerous: bool = False

    @property
    def path(self) -> tuple[str, ...]:
        return tuple(self.key.split("."))

    def parse(self, raw: Any) -> Any:
        text = str(raw).strip()
        if self.kind == "bool":
            if text not in ("true", "false"):
                raise SettingsError(f"{self.label}: choose on or off.")
            return text == "true"
        if self.kind == "int":
            if not text.isdigit():
                raise SettingsError(f"{self.label}: enter a whole number.")
            value = int(text)
            if (self.minimum is not None and value < self.minimum) or (
                self.maximum is not None and value > self.maximum
            ):
                raise SettingsError(
                    f"{self.label}: choose between {self.minimum} and {self.maximum}."
                )
            return value
        if self.kind == "choice":
            if text not in self.choices:
                raise SettingsError(f"{self.label}: choose one of {', '.join(self.choices)}.")
            return text
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError):
            raise SettingsError(f"{self.label}: '{text[:64]}' is not a known time zone.") from None
        return text


# Only settings that take effect when changed belong here. Matrix registration,
# federation and upload size are not listed because install does not render
# them into Synapse's configuration yet.
SETTINGS: dict[str, Setting] = {
    setting.key: setting
    for setting in (
        Setting(
            "project.timezone",
            "Community time zone",
            "Scheduled announcements use this time zone.",
            "timezone",
            "UTC",
        ),
        Setting(
            "operations.backups.schedule",
            "Automatic backups",
            "How often the server makes and checks a backup on its own.",
            "choice",
            "daily",
            choices=("hourly", "daily", "weekly"),
        ),
        Setting(
            "operations.backups.retention_days",
            "Keep backups for (days)",
            "Older snapshots are deleted after each new backup.",
            "int",
            14,
            minimum=1,
            maximum=3650,
        ),
        Setting(
            "operations.backups.encryption",
            "Backup encryption",
            "restic encrypts backups on the server; none keeps unencrypted, owner-only archives.",
            "choice",
            "none",
            choices=("restic", "none"),
        ),
        Setting(
            "services.element.enabled",
            "Element web chat",
            "The browser chat app at /app. Turning it off does not affect Matrix itself.",
            "bool",
            True,
            service="element",
        ),
        Setting(
            "services.matrix.enabled",
            "Matrix chat server",
            "Turning it off stops all community chat, and Matrix announcements, until it is "
            "turned back on. Messages and accounts are kept.",
            "bool",
            True,
            service="matrix",
            dangerous=True,
        ),
    )
}


def parse_changes(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        raise SettingsError("No changes were sent.")
    if len(raw) > len(SETTINGS):
        raise SettingsError("Too many changes in one request.")
    changes = {}
    for key, value in raw.items():
        setting = SETTINGS.get(key)
        if setting is None:
            raise SettingsError("That setting cannot be changed from the console.")
        changes[key] = setting.parse(value)
    return changes


def current_values(config: dict) -> dict[str, Any]:
    return {key: get_value(config, s.path, s.default) for key, s in SETTINGS.items()}


def build_plan(context, raw_changes: Any) -> dict[str, Any]:
    """What the change would do, without doing it."""
    changes = parse_changes(raw_changes)
    old_text = context.config_path.read_text(encoding="utf-8")
    config = yaml.safe_load(old_text) or {}
    current = current_values(config)
    effective = {key: value for key, value in changes.items() if current[key] != value}

    new_text = old_text
    for key, value in effective.items():
        try:
            new_text = set_value(new_text, SETTINGS[key].path, value)
        except ValueError as exc:
            raise SettingsError(str(exc)) from exc
    errors = validate_config(yaml.safe_load(new_text))
    if errors:
        raise SettingsError(f"The new configuration would be invalid: {errors[0]}")

    warnings = []
    if effective.get("operations.backups.encryption") == "restic":
        if shutil.which("restic") is None:
            raise SettingsError("Install restic on the server first (sudo apt install restic).")
        if not parse_env_file(context.env_path).get("RESTIC_PASSWORD"):
            raise SettingsError("RESTIC_PASSWORD must be set in the server's .env first.")
        warnings.append(
            "Keep a copy of RESTIC_PASSWORD offline: encrypted backups cannot be restored "
            "without it."
        )
    if effective.get("services.matrix.enabled") is False and get_value(
        config, ("services", "announcements", "channels", "matrix_room", "enabled"), False
    ):
        warnings.append("Matrix announcements will fail while Matrix is off.")

    diff = "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            "current",
            "proposed",
        )
    )
    return {
        "changes": [
            {
                "key": key,
                "label": SETTINGS[key].label,
                "from": current[key],
                "to": value,
                "dangerous": SETTINGS[key].dangerous,
            }
            for key, value in effective.items()
        ],
        "needs_password": any(SETTINGS[key].dangerous for key in effective),
        "warnings": warnings,
        "diff": diff,
        "_new_text": new_text,
        "_old_text": old_text,
        "_effective": effective,
    }


def public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if not key.startswith("_")}


def requires_password(raw_changes: Any) -> bool:
    try:
        return any(SETTINGS[key].dangerous for key in parse_changes(raw_changes))
    except SettingsError:
        return False


def _compose_states(context, service: str) -> list[dict[str, str]]:
    result = docker.run_compose(
        context.services_dir / service,
        ["ps", "--all", "--format", "json"],
        env_file=context.env_path,
        capture_output=True,
    )
    if result.returncode != 0:
        raise SettingsError(f"Could not read the state of {service}.")
    text = result.stdout.strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def wait_for(context, service: str, running: bool) -> None:
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    while True:
        states = _compose_states(context, service)
        if not running and not any(s.get("State") == "running" for s in states):
            return
        if running and states:
            if any(s.get("State") in ("exited", "dead") for s in states):
                raise SettingsError(f"{service} stopped while starting.")
            if all(
                s.get("State") == "running" and s.get("Health", "") in ("", "healthy")
                for s in states
            ):
                return
        if time.monotonic() > deadline:
            raise SettingsError(f"{service} did not become healthy in time.")
        time.sleep(HEALTH_POLL_SECONDS)


def set_service(context, service: str, running: bool) -> None:
    if running:
        try:
            config = yaml.safe_load(context.config_path.read_text(encoding="utf-8")) or {}
            prepare_service(context.project_dir, service, config, context.env_path)
        except ProvisionError as exc:
            raise SettingsError(str(exc)) from exc
        docker.ensure_network("proxy_net")
        args = ["up", "-d", "--remove-orphans"]
    else:
        args = ["down"]
    try:
        result = docker.run_compose(
            context.services_dir / service, args, env_file=context.env_path, capture_output=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SettingsError(f"Could not run Docker for {service}.") from exc
    if result.returncode != 0:
        raise SettingsError(f"Docker could not {'start' if running else 'stop'} {service}.")
    wait_for(context, service, running)


def apply_changes(context, raw_changes: Any) -> dict[str, Any]:
    """Back up, write the configuration, start or stop services, and roll back on failure."""
    plan = build_plan(context, raw_changes)
    effective = plan["_effective"]
    if not effective:
        return {"applied": False, "steps": ["Nothing to change."]}

    steps = []
    engine = context.backup_engine()
    if engine.backups_cfg.get("enabled", True):
        try:
            snapshot = engine.create_snapshot()
        except BackupError as exc:
            raise SettingsError(f"The safety backup failed, so nothing was changed: {exc}") from exc
        steps.append(f"Safety backup {snapshot.id} created.")
    else:
        steps.append("Backups are turned off, so no safety backup was made.")

    previous = context.config_path.with_name("community-stack.yaml.previous")
    previous.write_text(plan["_old_text"], encoding="utf-8")
    previous.chmod(0o600)
    write_in_place(context.config_path, plan["_new_text"])
    steps.append("Configuration saved.")

    attempted: list[tuple[str, bool]] = []
    try:
        for key, value in effective.items():
            service = SETTINGS[key].service
            if service:
                attempted.append((service, bool(value)))
                set_service(context, service, bool(value))
                steps.append(f"{SETTINGS[key].label} is now {'running' if value else 'stopped'}.")
    except SettingsError as exc:
        write_in_place(context.config_path, plan["_old_text"])
        restore_errors = []
        # Put every touched service back, the one that failed included, newest first.
        for service, value in reversed(attempted):
            try:
                set_service(context, service, not value)
            except SettingsError as restore_exc:
                restore_errors.append(str(restore_exc))
        message = f"{exc} The previous configuration was restored."
        if restore_errors:
            message += " Some services could not be put back: " + " ".join(restore_errors)
        raise SettingsError(message) from exc
    return {"applied": True, "steps": steps}
