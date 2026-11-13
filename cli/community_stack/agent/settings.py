"""Settings the console may change, and the plan/backup/apply/check/rollback pipeline."""

from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from community_stack import docker
from community_stack.backup import BackupError
from community_stack.bridges.catalogue import BRIDGE_SERVICES
from community_stack.config import ConfigError, validate_config
from community_stack.config_edit import (
    ensure_facilmap_config,
    ensure_field_map_config,
    ensure_librebooking_config,
    ensure_vikunja_config,
    get_value,
    set_value,
    write_in_place,
)
from community_stack.continuity.connect_page import write_service_directory
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
    kind: str  # "bool", "int", "choice", "hostname", "email", "version", or "timezone"
    default: Any
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    service: str | None = None
    restart_service: str | None = None
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
        if self.kind == "hostname":
            if not re.fullmatch(r"[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text):
                raise SettingsError(f"{self.label}: enter a valid public hostname.")
            return text
        if self.kind == "email":
            if not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9.-]{1,189}\.[A-Za-z]{2,63}",
                text,
            ):
                raise SettingsError(f"{self.label}: enter a valid email address.")
            return text
        if self.kind == "version":
            if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-[0-9]+\.[0-9]+\.[0-9]+", text):
                raise SettingsError(f"{self.label}: enter a pinned app-container release tag.")
            return text
        if self.kind == "git_ref":
            if not re.fullmatch(r"[a-f0-9]{40}", text):
                raise SettingsError(f"{self.label}: enter a full lowercase Git commit SHA.")
            return text
        if self.kind == "tile_url":
            try:
                parsed = urlsplit(text)
                hostname = parsed.hostname
            except ValueError:
                parsed = None
                hostname = None
            if (
                parsed is None
                or parsed.scheme != "https"
                or not hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
                or any(char.isspace() for char in text)
                or not all(f"{{{axis}}}" in text for axis in ("z", "x", "y"))
            ):
                raise SettingsError(
                    f"{self.label}: enter an HTTPS tile URL containing {{z}}, {{x}}, and {{y}}."
                )
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
            "The browser chat app at /app/. Turning it off does not affect Matrix itself.",
            "bool",
            True,
            service="element",
        ),
        Setting(
            "services.signal.enabled",
            "Signal (experimental)",
            "Runs signal-cli so announcements can go to a Signal group. Link an account on "
            "the Channels page afterwards. Signal may block accounts that look automated.",
            "bool",
            False,
            service="signal",
        ),
        Setting(
            "services.whatsapp_bridge.enabled",
            "WhatsApp bridge (unofficial)",
            "Starts mautrix-whatsapp and registers it with Matrix. It links a phone to this "
            "server and is bidirectional; only bridge a group intended for public announcements.",
            "bool",
            False,
            service="whatsapp_bridge",
            dangerous=True,
        ),
        Setting(
            "services.signal_bridge.enabled",
            "Signal bridge (experimental)",
            "Starts mautrix-signal and registers it with Matrix. It links a Signal account to "
            "this server and is bidirectional; Signal may restrict accounts it considers automated.",
            "bool",
            False,
            service="signal_bridge",
            dangerous=True,
        ),
        Setting(
            "services.meta_bridge.enabled",
            "Facebook Messenger bridge (experimental)",
            "Starts mautrix-meta and registers it with Matrix for Messenger. It signs an account "
            "in to this server and is bidirectional; Meta may restrict automated accounts.",
            "bool",
            False,
            service="meta_bridge",
            dangerous=True,
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
        Setting(
            "services.vikunja.enabled",
            "Vikunja task management",
            "Deploy Vikunja at tasks.<community domain> with its own accounts. Add the "
            "hostname's DNS record before enabling it.",
            "bool",
            False,
            service="vikunja",
            dangerous=True,
        ),
        Setting(
            "services.vikunja.allow_registration",
            "Vikunja self-registration",
            "Allow anyone to create a Vikunja account. Existing accounts are unaffected; "
            "turn this off to keep account creation admin-managed.",
            "bool",
            False,
            restart_service="vikunja",
            dangerous=True,
        ),
        Setting(
            "services.vikunja.enable_link_sharing",
            "Vikunja public project sharing",
            "Allow project owners to create anonymous share links. Anyone with a link gets "
            "its selected permission; use Read for public to-do lists.",
            "bool",
            False,
            restart_service="vikunja",
            dangerous=True,
        ),
        Setting(
            "services.facilmap.enabled",
            "FacilMap collaborative maps",
            "Deploy maps.<community domain>. FacilMap has no user accounts or OIDC; anyone "
            "who can reach it can create maps, and shared map URLs are bearer credentials. "
            "Do not store sensitive locations.",
            "bool",
            False,
            service="facilmap",
            dangerous=True,
        ),
        Setting(
            "services.facilmap.hostname",
            "FacilMap public hostname",
            "The HTTPS hostname routed to FacilMap. Add its DNS record before enabling the service.",
            "hostname",
            "",
            restart_service="facilmap",
        ),
        Setting(
            "services.field_map.enabled",
            "Field Map mobile mapping",
            "Deploy Field Map at its public hostname. Maps are private by default, but share "
            "URLs are bearer credentials. Confirm the anonymous-map setting before enabling it.",
            "bool",
            False,
            service="field_map",
            dangerous=True,
        ),
        Setting(
            "services.field_map.hostname",
            "Field Map public hostname",
            "The HTTPS hostname routed to Field Map. Add its DNS record before enabling the service.",
            "hostname",
            "",
            restart_service="field_map",
        ),
        Setting(
            "services.field_map.tile_url",
            "Field Map tile URL",
            "HTTPS tile template containing {z}, {x}, and {y}. The URL is embedded in the "
            "public frontend; use a provider that permits your expected traffic.",
            "tile_url",
            "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            restart_service="field_map",
        ),
        Setting(
            "services.field_map.anonymous_links_enabled",
            "Field Map anonymous map creation",
            "Anyone who can reach Field Map can create maps when this is on. Share links are "
            "bearer credentials; disable this setting unless anonymous creation is intended.",
            "bool",
            True,
            restart_service="field_map",
            dangerous=True,
        ),
        Setting(
            "services.field_map.version",
            "Field Map source commit",
            "Pinned full Git commit SHA from leokeba/field-map. Changing it rebuilds the app "
            "after a safety backup.",
            "git_ref",
            "c9646113b2817d838e340f649a6445d0381fb702",
            restart_service="field_map",
            dangerous=True,
        ),
        Setting(
            "services.librebooking.enabled",
            "LibreBooking space reservations",
            "Deploy booking.<community domain> with its own accounts and approval calendar. "
            "Add the hostname's DNS record before enabling it.",
            "bool",
            False,
            service="librebooking",
            dangerous=True,
        ),
        Setting(
            "services.librebooking.hostname",
            "LibreBooking public hostname",
            "The HTTPS hostname routed to LibreBooking. Add its DNS record before enabling it.",
            "hostname",
            "",
            restart_service="librebooking",
        ),
        Setting(
            "services.librebooking.admin_email",
            "LibreBooking administrator email",
            "The email address LibreBooking grants administrator privileges to during first setup.",
            "email",
            "admin@example.org",
            restart_service="librebooking",
        ),
        Setting(
            "services.librebooking.allow_registration",
            "LibreBooking self-registration",
            "Allow anyone to create a LibreBooking account. Enable temporarily for initial admin "
            "registration, then turn it off.",
            "bool",
            False,
            restart_service="librebooking",
            dangerous=True,
        ),
        Setting(
            "services.librebooking.version",
            "LibreBooking container version",
            "Pinned app-container release tag (app version-container version). Review upstream "
            "releases before changing it; "
            "the service is rebuilt and a safety backup is taken.",
            "version",
            "7.2.0-3.5.2",
            restart_service="librebooking",
            dangerous=True,
        ),
    )
}


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
    values = {key: get_value(config, s.path, s.default) for key, s in SETTINGS.items()}
    if values["services.librebooking.admin_email"] == "admin@example.org":
        values["services.librebooking.admin_email"] = (env or {}).get(
            "ACME_EMAIL", "admin@example.org"
        )
    values["services.facilmap.hostname"] = get_value(
        config,
        ("services", "facilmap", "hostname"),
        f"maps.{get_value(config, ('domain', 'base'), 'example.org')}",
    )
    values["services.librebooking.hostname"] = get_value(
        config,
        ("services", "librebooking", "hostname"),
        f"booking.{get_value(config, ('domain', 'base'), 'example.org')}",
    )
    values["services.field_map.hostname"] = get_value(
        config,
        ("services", "field_map", "hostname"),
        f"field-map.{get_value(config, ('domain', 'base'), 'example.org')}",
    )
    return values


def build_plan(context, raw_changes: Any) -> dict[str, Any]:
    """What the change would do, without doing it."""
    changes = parse_changes(raw_changes)
    old_text = context.config_path.read_text(encoding="utf-8")
    config = yaml.safe_load(old_text) or {}
    current = current_values(config, parse_env_file(context.env_path))
    effective = {key: value for key, value in changes.items() if current[key] != value}

    new_text = old_text
    if any(key.startswith("services.vikunja.") for key in effective):
        try:
            new_text = ensure_vikunja_config(new_text)
        except (TypeError, ValueError) as exc:
            raise SettingsError(str(exc)) from exc
    if any(key.startswith("services.facilmap.") for key in effective):
        try:
            new_text = ensure_facilmap_config(new_text)
        except (TypeError, ValueError) as exc:
            raise SettingsError(str(exc)) from exc
    if any(key.startswith("services.librebooking.") for key in effective):
        try:
            new_text = ensure_librebooking_config(new_text)
        except (TypeError, ValueError) as exc:
            raise SettingsError(str(exc)) from exc
    if any(key.startswith("services.field_map.") for key in effective):
        try:
            new_text = ensure_field_map_config(new_text)
        except (TypeError, ValueError) as exc:
            raise SettingsError(str(exc)) from exc
    for key, value in effective.items():
        try:
            new_text = set_value(new_text, SETTINGS[key].path, value)
        except ValueError as exc:
            raise SettingsError(str(exc)) from exc
    errors = validate_config(yaml.safe_load(new_text))
    if errors:
        raise SettingsError(f"The new configuration would be invalid: {errors[0]}")

    warnings = []
    if effective.get("services.whatsapp_bridge.enabled") is True:
        if not get_value(yaml.safe_load(new_text), ("services", "matrix", "enabled"), True):
            raise SettingsError("Enable the Matrix server before enabling the WhatsApp bridge.")
        if get_value(
            config, ("services", "whatsapp_bridge", "mode"), "none"
        ) == "none" and "mode" in config.get("services", {}).get("whatsapp_bridge", {}):
            new_text = set_value(
                new_text,
                ("services", "whatsapp_bridge", "mode"),
                "announcements_only",
            )
        warnings.append(
            "WhatsApp is an unofficial, bidirectional bridge. Link only a phone and group "
            "that the community intends to bridge."
        )
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
    proposed_config = yaml.safe_load(new_text)
    field_map = proposed_config.get("services", {}).get("field_map", {})
    facilmap = proposed_config.get("services", {}).get("facilmap", {})
    field_map_host = field_map.get("hostname") or (
        f"field-map.{proposed_config.get('domain', {}).get('base', 'example.org')}"
    )
    facilmap_host = facilmap.get("hostname") or (
        f"maps.{proposed_config.get('domain', {}).get('base', 'example.org')}"
    )
    if (
        field_map.get("enabled")
        and facilmap.get("enabled")
        and field_map_host.casefold() == facilmap_host.casefold()
    ):
        raise SettingsError(
            "Field Map and FacilMap cannot use the same hostname. Disable FacilMap before "
            "moving its hostname to Field Map."
        )
    if effective.get("services.vikunja.enabled") is True:
        hostname = get_value(
            proposed_config,
            ("services", "vikunja", "hostname"),
            f"tasks.{get_value(config, ('domain', 'base'), 'example.org')}",
        )
        warnings.append(
            f"Vikunja will be deployed at https://{hostname}/. Its accounts are separate "
            f"from Matrix; self-registration is "
            f"{'enabled' if get_value(proposed_config, ('services', 'vikunja', 'allow_registration'), False) else 'disabled'} "
            f"and public link sharing is "
            f"{'enabled' if get_value(proposed_config, ('services', 'vikunja', 'enable_link_sharing'), False) else 'disabled'}."
        )
    if effective.get("services.vikunja.allow_registration") is True:
        warnings.append("Anyone can create a Vikunja account while self-registration is enabled.")
    if effective.get("services.vikunja.enable_link_sharing") is True:
        warnings.append(
            "Project owners can create anonymous Vikunja share links with Read, Read & Write, "
            "or Admin permission. Review each project's contents and choose Read for public lists."
        )
    if effective.get("services.facilmap.enabled") is True or (
        "services.facilmap.hostname" in effective
        and get_value(proposed_config, ("services", "facilmap", "enabled"), False)
    ):
        hostname = get_value(
            proposed_config,
            ("services", "facilmap", "hostname"),
            f"maps.{get_value(config, ('domain', 'base'), 'example.org')}",
        )
        warnings.append(
            f"FacilMap will be deployed at https://{hostname}/. It has no accounts or OIDC; "
            "map admin/edit/read-only URLs are bearer credentials, anyone who reaches the "
            "service can create maps, and uploaded photos are not supported. Do not store "
            "sensitive locations."
        )
    if effective.get("services.librebooking.enabled") is True or (
        any(key.startswith("services.librebooking.") for key in effective)
        and get_value(proposed_config, ("services", "librebooking", "enabled"), False)
    ):
        hostname = get_value(
            proposed_config,
            ("services", "librebooking", "hostname"),
            f"booking.{get_value(config, ('domain', 'base'), 'example.org')}",
        )
        warnings.append(
            f"LibreBooking will be deployed at https://{hostname}/Web/. Configure each space's "
            "approval rules in LibreBooking. For first setup, briefly enable self-registration "
            "to create the administrator account, then turn it off."
        )
    if effective.get("services.librebooking.allow_registration") is True:
        warnings.append(
            "Anyone can create a LibreBooking account while self-registration is enabled. "
            "Turn it off immediately after registering the initial administrator."
        )
    if effective.get("services.field_map.enabled") is True or (
        any(key.startswith("services.field_map.") for key in effective) and field_map.get("enabled")
    ):
        warnings.append(
            f"Field Map will be deployed at https://{field_map_host}/. Map share URLs are "
            "bearer credentials. Its photos and locations may be sensitive; do not add "
            "sensitive observations to this test instance."
        )
    if effective.get("services.field_map.anonymous_links_enabled") is True or (
        field_map.get("enabled") and field_map.get("anonymous_links_enabled", True)
    ):
        warnings.append(
            "Anyone who can reach Field Map can create maps and consume storage while "
            "anonymous map creation is enabled."
        )
    if field_map.get("tile_url") == "https://tile.openstreetmap.org/{z}/{x}/{y}.png" and (
        effective.get("services.field_map.enabled") is True
        or "services.field_map.tile_url" in effective
    ):
        warnings.append(
            "The OpenStreetMap tile URL is for low-volume testing only; use an appropriate "
            "tile provider before production."
        )

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
    route_path = context.project_dir / "data" / "proxy" / f"{service}.caddy"
    previous_route = (
        route_path.read_text(encoding="utf-8")
        if service in ("vikunja", "facilmap", "librebooking", "field_map") and route_path.is_file()
        else None
    )
    if running:
        if not docker.ensure_network("proxy_net"):
            raise SettingsError("Docker could not create the proxy network.")
        if (service == "matrix" or service in BRIDGE_SERVICES) and not docker.ensure_network(
            "bridge_net"
        ):
            raise SettingsError("Docker could not create the isolated Matrix appservice network.")
    if running or service in ("vikunja", "facilmap", "librebooking", "field_map"):
        try:
            config = yaml.safe_load(context.config_path.read_text(encoding="utf-8")) or {}
            prepare_service(context.project_dir, service, config, context.env_path)
        except (OSError, ValueError, ProvisionError) as exc:
            raise SettingsError(str(exc)) from exc
    if service in BRIDGE_SERVICES and running:
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
        if service in ("librebooking", "field_map"):
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
    if service in ("vikunja", "facilmap", "librebooking", "field_map"):
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
        service_changes = {
            SETTINGS[key].service: (key, value, SETTINGS[key].service)
            for key, value in effective.items()
            if SETTINGS[key].service
        }
        proposed_config = yaml.safe_load(plan["_new_text"])
        for key in effective:
            service = SETTINGS[key].restart_service
            if service and get_value(proposed_config, ("services", service, "enabled"), False):
                service_changes.setdefault(service, (key, True, service))
        ordered_service_changes = sorted(
            service_changes.values(),
            key=lambda item: (
                0
                if item[2] == "matrix" and item[1]
                else 1
                if item[2] == "whatsapp_bridge" and item[1]
                else 0
                if item[2] == "whatsapp_bridge" and not item[1]
                else 2
                if item[2] in ("facilmap", "field_map") and not item[1]
                else 3
                if item[2] in ("facilmap", "field_map") and item[1]
                else 1
            ),
        )
        previous_config = yaml.safe_load(plan["_old_text"]) or {}
        for key, value, service in ordered_service_changes:
            if service:
                was_enabled = get_value(previous_config, ("services", service, "enabled"), False)
                attempted.append((service, bool(was_enabled)))
                set_service(context, service, bool(value))
                steps.append(f"{SETTINGS[key].label} is now {'running' if value else 'stopped'}.")
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
