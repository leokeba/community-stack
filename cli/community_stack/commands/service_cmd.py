"""Generic service lifecycle commands."""

from __future__ import annotations

from pathlib import Path

import yaml

from community_stack import docker
from community_stack.collaborative_apps import COLLABORATIVE_APPS_BY_KEY
from community_stack.commands.logs_cmd import SERVICE_DIR_MAP, get_services_root
from community_stack.commands.status_cmd import run_status
from community_stack.config import ConfigError, load_config, validate_config
from community_stack.config_edit import (
    ensure_facilmap_config,
    ensure_field_map_config,
    ensure_librebooking_config,
    ensure_vikunja_config,
    set_value,
    write_in_place,
)
from community_stack.continuity.connect_page import write_service_directory


def _set_enabled(config_path: Path, service: str, enabled: bool) -> None:
    original = config_path.read_text(encoding="utf-8")
    if service == "vikunja":
        candidate = ensure_vikunja_config(original)
    elif service == "facilmap":
        candidate = ensure_facilmap_config(original)
    elif service == "librebooking":
        candidate = ensure_librebooking_config(original)
    elif service == "field_map":
        candidate = ensure_field_map_config(original)
    else:
        raise ValueError(f"Enable/disable is not supported for {service}.")
    candidate = set_value(candidate, ("services", service, "enabled"), enabled)
    errors = validate_config(yaml.safe_load(candidate))
    if errors:
        raise ValueError("Updated configuration is invalid: " + "; ".join(errors))
    write_in_place(config_path, candidate)


def run_service(
    action: str,
    service: str,
    config_path: Path | str = "community-stack.yaml",
    env_path: Path | str = ".env",
) -> int:
    """Enable/disable a service or control its Compose lifecycle."""
    service_key = service.lower().strip()
    if service_key not in SERVICE_DIR_MAP:
        known = ", ".join(sorted(SERVICE_DIR_MAP))
        print(f"[ERROR] Unknown service '{service}'. Known services: {known}")
        return 1
    directory = SERVICE_DIR_MAP[service_key]
    config_file = Path(config_path)
    env_file = Path(env_path).resolve()

    if action in ("enable", "disable"):
        if directory not in COLLABORATIVE_APPS_BY_KEY:
            print("[ERROR] Enable/disable via this command is supported for collaborative apps.")
            return 1
        if action == "disable" and not docker.is_docker_available():
            print(f"[ERROR] Docker is unavailable; {service} was not disabled.")
            return 1
        try:
            _set_enabled(config_file, directory, action == "enable")
        except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
            print(f"[ERROR] Could not update configuration: {exc}")
            return 1
        if action == "enable":
            try:
                write_service_directory(config_file)
            except (ConfigError, OSError, ValueError) as exc:
                print(
                    "[ERROR] The service is enabled, but the landing-page directory "
                    f"could not update: {exc}"
                )
                return 1
            print(f"{service} is enabled in configuration. Deploy it with: community-stack install")
            return 0
        try:
            services_root = get_services_root()
        except FileNotFoundError as exc:
            print(f"[ERROR] {exc}")
            return 1
        route_path = config_file.resolve().parent / "data" / "proxy" / f"{directory}.caddy"
        previous_route = route_path.read_text(encoding="utf-8") if route_path.is_file() else None
        try:
            from community_stack.provision import prepare_service

            config = load_config(config_file)
            prepare_service(
                config_file.resolve().parent,
                directory,
                config,
                env_file,
                services_root,
            )
        except (ConfigError, OSError, ValueError) as exc:
            print(f"[ERROR] Could not update {service}'s public route: {exc}")
            return 1
        result = docker.run_compose(
            services_root / directory,
            ["down", "--remove-orphans"],
            env_file=env_file,
            capture_output=True,
        )
        if result.returncode != 0:
            try:
                _set_enabled(config_file, directory, True)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                print(f"[ERROR] {service} shutdown failed and configuration rollback failed: {exc}")
                return 1
            print(f"[ERROR] {service} could not be stopped; configuration was restored to enabled.")
            return 1
        if previous_route != route_path.read_text(encoding="utf-8"):
            proxy_result = docker.run_compose(
                services_root / "proxy",
                ["restart", "caddy"],
                env_file=env_file,
                capture_output=True,
            )
            if proxy_result.returncode != 0:
                print(f"[ERROR] {service} is stopped, but Caddy could not reload its route.")
                return 1
        try:
            write_service_directory(config_file)
        except (ConfigError, OSError, ValueError) as exc:
            print(
                f"[ERROR] {service} is stopped, but the landing-page directory "
                f"could not update: {exc}"
            )
            return 1
        print(
            f"{service} is disabled and its containers are stopped. Persistent data was preserved."
        )
        return 0

    if action == "status":
        return run_status(config_path=config_file)
    if directory in COLLABORATIVE_APPS_BY_KEY and action in ("start", "restart"):
        try:
            config = load_config(config_file)
        except (ConfigError, OSError) as exc:
            print(f"[ERROR] Could not load configuration: {exc}")
            return 1
        if not config.get("services", {}).get(directory, {}).get("enabled", False):
            print(f"[ERROR] {service} is disabled in community-stack.yaml.")
            return 1

    if not docker.is_docker_available():
        print("[ERROR] Docker daemon is not accessible or not running.")
        return 1
    try:
        services_root = get_services_root()
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}")
        return 1

    result = docker.run_compose(
        services_root / directory,
        [action],
        env_file=env_file,
        capture_output=False,
    )
    if result.returncode != 0:
        print(f"[ERROR] Could not {action} service '{service}'.")
        return 1
    print(f"[SUCCESS] Service '{service}' {action} completed.")
    return 0
