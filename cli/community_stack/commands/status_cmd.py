"""Implementation of the 'status' command."""

from __future__ import annotations

from community_stack import docker


def run_status() -> int:
    """Report health and status of all community-stack containers."""
    if not docker.is_docker_available():
        print("[WARNING] Docker daemon is not accessible or not running.")
        print("          Cannot query container status.")
        return 1

    containers = docker.list_community_containers()
    if not containers:
        print("[INFO] No community-stack containers are currently deployed or running.")
        print("       To deploy services, run: community-stack install")
        return 0

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
    return 0
