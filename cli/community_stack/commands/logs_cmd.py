"""Implementation of the 'logs' command."""

from __future__ import annotations

from pathlib import Path

from community_stack import docker

SERVICE_DIR_MAP = {
    "matrix": "matrix",
    "synapse": "matrix",
    "postgres": "matrix",
    "element": "element",
    "announcements": "announcements",
    "publishing": "announcements",
    "web": "landing",
    "landing": "landing",
    "caddy": "proxy",
    "proxy": "proxy",
    "whatsapp": "whatsapp_bridge",
    "whatsapp_bridge": "whatsapp_bridge",
}


def get_services_root() -> Path:
    """Find services/ directory."""
    cwd_services = Path.cwd() / "services"
    if cwd_services.is_dir():
        return cwd_services

    repo_services = Path(__file__).resolve().parent.parent.parent.parent / "services"
    if repo_services.is_dir():
        return repo_services

    raise FileNotFoundError("Could not locate services/ directory.")


def run_logs(service: str, follow: bool = False, tail: int = 100) -> int:
    """Stream or display logs for a specific community service."""
    if not docker.is_docker_available():
        print("[ERROR] Docker daemon is not accessible or not running.")
        return 1

    svc_key = service.lower().strip()
    dir_name = SERVICE_DIR_MAP.get(svc_key, svc_key)

    try:
        services_root = get_services_root()
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}")
        return 1

    svc_dir = services_root / dir_name
    if not svc_dir.is_dir():
        known = ", ".join(sorted(set(SERVICE_DIR_MAP.keys())))
        print(f"[ERROR] Unknown service '{service}'. Known services are: {known}")
        return 1

    args = ["logs", "--tail", str(tail)]
    if follow:
        args.append("-f")

    print(f"Fetching logs for service '{service}' in {svc_dir.name}...")
    res = docker.run_compose(project_dir=svc_dir, args=args, capture_output=False)
    return res.returncode
