"""Implementation of the 'status' command."""

from __future__ import annotations

from pathlib import Path

from community_stack import docker
from community_stack.config import ConfigError, load_config


def run_status(config_path: Path | str = "community-stack.yaml") -> int:
    """Report health and status of all community-stack containers."""
    apps = {}
    config_valid = True
    try:
        config = load_config(config_path)
        services = config.get("services", {})
        for service, label, default_host in (
            ("vikunja", "Vikunja", "tasks"),
            ("facilmap", "FacilMap", "maps"),
        ):
            settings = services.get(service, {})
            hostname = settings.get("hostname") or f"{default_host}.{config['domain']['base']}"
            apps[service] = {
                "label": label,
                "enabled": settings.get("enabled", False),
                "url": f"https://{hostname}/",
            }
    except (ConfigError, OSError) as exc:
        config_valid = False
        print(f"[WARNING] Could not load configuration for optional-service status: {exc}")

    if not docker.is_docker_available():
        print("[WARNING] Docker daemon is not accessible or not running.")
        print("          Cannot query container status.")
        if not config_valid:
            print("Vikunja: status unknown because configuration could not be loaded")
            print("FacilMap: status unknown because configuration could not be loaded")
        else:
            for app in apps.values():
                state = "enabled but unavailable" if app["enabled"] else "disabled"
                print(f"{app['label']}: {state}" + (f" — {app['url']}" if app["enabled"] else ""))
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

    if not config_valid:
        print("Vikunja: status unknown because configuration could not be loaded")
        print("FacilMap: status unknown because configuration could not be loaded")
        return 1

    for service, app in apps.items():
        container = next(
            (item for item in containers if item.get("name") == f"community_{service}"), None
        )
        if not app["enabled"]:
            state = "disabled"
        elif container:
            state = f"enabled — {container.get('status', 'deployed')}"
        else:
            state = "enabled but not deployed"
        print(f"{app['label']}: {state}" + (f" — {app['url']}" if app["enabled"] else ""))
    return 0
