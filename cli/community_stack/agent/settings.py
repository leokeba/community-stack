"""The plan/backup/apply/check/rollback pipeline behind the console's service settings.

Which settings exist, and each service's checks, warnings and defaults, are in
`service_catalogue`; this module only applies them, the same way for every service.
"""

from __future__ import annotations

import difflib
import json
import subprocess
import time
from typing import Any

import yaml

from community_stack import docker
from community_stack.agent.service_catalogue import (
    SERVICES,
    SETTINGS,
    SPEC_FOR_SETTING,
    SPECS_BY_SERVICE,
    PlanContext,
    ServiceSpec,
    Setting,
    SettingsError,
)
from community_stack.backup import BackupError
from community_stack.config import ConfigError, validate_config
from community_stack.config_edit import (
    ensure_service_config,
    get_value,
    set_value,
    write_in_place,
)
from community_stack.continuity.connect_page import write_service_directory
from community_stack.provision import ProvisionError, prepare_service
from community_stack.secrets import parse_env_file

__all__ = [
    "SERVICES",
    "SETTINGS",
    "ServiceSpec",
    "Setting",
    "SettingsError",
    "apply_changes",
    "build_plan",
    "current_values",
    "parse_changes",
    "public_plan",
    "requires_password",
    "set_service",
]

HEALTH_TIMEOUT_SECONDS = 180
HEALTH_POLL_SECONDS = 3


def parse_changes(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        raise SettingsError("No changes were sent.")
    changes = {}
    for key, value in raw.items():
        setting = SETTINGS.get(key)
        if setting is None:
            raise SettingsError("That setting cannot be changed from the console.")
        changes[key] = setting.parse(value)
    return changes


def current_values(config: dict, env: dict[str, str] | None = None) -> dict[str, Any]:
    return {key: setting.current(config, env or {}) for key, setting in SETTINGS.items()}


def build_plan(context, raw_changes: Any) -> dict[str, Any]:
    """What the change would do, without doing it."""
    changes = parse_changes(raw_changes)
    old_text = context.config_path.read_text(encoding="utf-8")
    config = yaml.safe_load(old_text) or {}
    env = parse_env_file(context.env_path)
    current = current_values(config, env)
    effective = {key: value for key, value in changes.items() if current[key] != value}
    touched = [spec for spec in SERVICES if any(SPEC_FOR_SETTING[key] is spec for key in effective)]

    new_text = old_text
    try:
        for spec in touched:
            if spec.config_defaults:
                new_text = ensure_service_config(new_text, spec.service)
        for key, value in effective.items():
            new_text = set_value(new_text, SETTINGS[key].path, value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(str(exc)) from exc
    errors = validate_config(yaml.safe_load(new_text))
    if errors:
        raise SettingsError(f"The new configuration would be invalid: {errors[0]}")

    ctx = PlanContext(
        current=config, proposed=yaml.safe_load(new_text), effective=effective, env=env
    )
    for spec in touched:
        if spec.adjust:
            new_text = spec.adjust(ctx, new_text)
    # Every service checks every change: one service's change can break another's rules.
    for spec in SERVICES:
        if spec.checks:
            spec.checks(ctx)
    warnings = [warning for spec in touched if spec.warnings for warning in spec.warnings(ctx)]

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
    # Updates also restart services the console has no settings for, such as the proxy.
    spec = SPECS_BY_SERVICE.get(service) or ServiceSpec(service, service, "", (), service=service)
    route_path = context.project_dir / "data" / "proxy" / f"{service}.caddy"
    previous_route = (
        route_path.read_text(encoding="utf-8") if spec.routed and route_path.is_file() else None
    )
    if running:
        if not docker.ensure_network("proxy_net"):
            raise SettingsError("Docker could not create the proxy network.")
        if spec.uses_bridge_net and not docker.ensure_network("bridge_net"):
            raise SettingsError("Docker could not create the isolated Matrix appservice network.")
    if running or spec.routed:
        try:
            config = yaml.safe_load(context.config_path.read_text(encoding="utf-8")) or {}
            prepare_service(context.project_dir, service, config, context.env_path)
        except (OSError, ValueError, ProvisionError) as exc:
            raise SettingsError(str(exc)) from exc
    if spec.appservice and running:
        try:
            result = docker.run_compose(
                context.services_dir / service,
                ["down"],
                env_file=context.env_path,
                capture_output=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SettingsError(f"Could not stop {service} before reloading Synapse.") from exc
        if result.returncode != 0:
            raise SettingsError(f"Docker could not stop {service} before reloading Synapse.")
        wait_for(context, service, False)
        try:
            result = docker.run_compose(
                context.services_dir / "matrix",
                ["restart", "synapse"],
                env_file=context.env_path,
                capture_output=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SettingsError(f"Could not reload the {service} appservice registration.") from exc
        if result.returncode != 0:
            raise SettingsError(f"Synapse could not reload the {service} appservice registration.")
        wait_for(context, "matrix", True)
    if running:
        args = ["up", "-d", "--remove-orphans"]
        if spec.build:
            args.insert(-1, "--build")
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
    if spec.routed:
        current_route = route_path.read_text(encoding="utf-8") if route_path.is_file() else None
        if previous_route != current_route:
            try:
                result = docker.run_compose(
                    context.services_dir / "proxy",
                    ["restart", "caddy"],
                    env_file=context.env_path,
                    capture_output=True,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise SettingsError(f"Could not reload Caddy's {service} route.") from exc
            if result.returncode != 0:
                raise SettingsError(f"Caddy could not reload the {service} route.")


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
        # A spec's switch starts or stops it; its other settings restart it while it is on.
        service_changes: dict[str, tuple[ServiceSpec, bool]] = {}
        for key, value in effective.items():
            spec = SPEC_FOR_SETTING[key]
            if spec.service and key == spec.enable_key:
                service_changes[spec.service] = (spec, bool(value))
        proposed_config = yaml.safe_load(plan["_new_text"])
        for key in effective:
            spec = SPEC_FOR_SETTING[key]
            if spec.service and get_value(
                proposed_config, ("services", spec.service, "enabled"), False
            ):
                service_changes.setdefault(spec.service, (spec, True))
        ordered = sorted(
            service_changes.values(),
            key=lambda item: item[0].rank_on if item[1] else item[0].rank_off,
        )
        previous_config = yaml.safe_load(plan["_old_text"]) or {}
        for spec, running in ordered:
            was_enabled = get_value(previous_config, ("services", spec.service, "enabled"), False)
            attempted.append((spec.service, bool(was_enabled)))
            set_service(context, spec.service, running)
            steps.append(f"{spec.label} is now {'running' if running else 'stopped'}.")
    except SettingsError as exc:
        write_in_place(context.config_path, plan["_old_text"])
        restore_errors = []
        # Put every touched service back, the one that failed included, newest first.
        for service, was_enabled in reversed(attempted):
            try:
                set_service(context, service, was_enabled)
            except SettingsError as restore_exc:
                restore_errors.append(str(restore_exc))
        message = f"{exc} The previous configuration was restored."
        if restore_errors:
            message += " Some services could not be put back: " + " ".join(restore_errors)
        raise SettingsError(message) from exc
    try:
        write_service_directory(context.config_path)
    except (ConfigError, OSError, ValueError) as exc:
        steps.append(
            "Warning: settings were applied, but the landing-page service directory "
            f"could not refresh: {exc}"
        )
    else:
        steps.append("Landing-page service directory refreshed.")
    return {"applied": True, "steps": steps}
