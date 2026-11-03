"""Implementation of the 'status' command."""

from __future__ import annotations

from pathlib import Path

from community_stack import docker
from community_stack.config import ConfigError, load_config


def run_status(config_path: Path | str = "community-stack.yaml") -> int:
    """Report health and status of all community-stack containers."""
    vikunja_enabled = False
    vikunja_url = ""
    config_valid = True
    try:
        config = load_config(config_path)
        vikunja = config.get("services", {}).get("vikunja", {})
        vikunja_enabled = vikunja.get("enabled", False)
        if vikunja_enabled:
            hostname = vikunja.get("hostname") or f"tasks.{config['domain']['base']}"
            vikunja_url = f"https://{hostname}/"
    except (ConfigError, OSError) as exc:
        config_valid = False
        print(f"[WARNING] Could not load configuration for optional-service status: {exc}")

    if not docker.is_docker_available():
        print("[WARNING] Docker daemon is not accessible or not running.")
        print("          Cannot query container status.")
        if not config_valid:
            print("Vikunja: status unknown because configuration could not be loaded")
        elif vikunja_enabled:
            print(f"Vikunja: enabled but unavailable — {vikunja_url}")
        else:
            print("Vikunja: disabled")
        return 1

    containers = docker.list_community_containers()
    if not containers:
        print("[INFO] No community-stack containers are currently deployed or running.")
        print("       To deploy services, run: community-stack install")
    else:
        print("================================================================================")
        print("                    COMMUNITY STACK SERVICE STATUS                             ")
        print("================================================================================\n")
        print(f"{'CONTAINER NAME':<26} {'STATE':<10} {'STATUS':<26} {'IMAGE'}")
        print("-" * 80)

        for c in containers:
            name = c.get("name", "")
            state = c.get("state", "")
            status = c.get("status", "")
            image = c.get("image", "")
            print(f"{name:<26} {state:<10} {status:<26} {image}")

        print("-" * 80)
        print(f"Total managed containers: {len(containers)}")

    vikunja_container = next(
        (item for item in containers if item.get("name") == "community_vikunja"), None
    )
    if not config_valid:
        print("Vikunja: status unknown because configuration could not be loaded")
    elif vikunja_enabled and vikunja_container:
        print(f"Vikunja: enabled — {vikunja_container.get('status', 'deployed')} — {vikunja_url}")
    elif vikunja_enabled:
        print(f"Vikunja: enabled but not deployed — {vikunja_url}")
    else:
        print("Vikunja: disabled")
    return 0
