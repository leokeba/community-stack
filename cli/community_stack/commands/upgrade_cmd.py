"""Implementation of the 'upgrade' command."""

from __future__ import annotations

from pathlib import Path

from community_stack import docker
from community_stack.backup import BackupEngine, BackupError
from community_stack.commands.logs_cmd import SERVICE_DIR_MAP, get_services_root


def run_upgrade(
    service: str,
    config_path: Path | str = "community-stack.yaml",
    env_path: Path | str = ".env",
) -> int:
    """Perform a safe, service-specific upgrade with pre-upgrade backup."""
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
        print(f"[ERROR] Unknown service '{service}'. Known services: {known}")
        return 1

    print(f"Initiating safe upgrade for service '{service}' in {svc_dir.name}...\n")

    # Step 1: Pre-upgrade snapshot
    print("[1/3] Creating pre-upgrade safety backup snapshot...")
    try:
        engine = BackupEngine(config_path=config_path, env_path=env_path)
        snapshot = engine.create_snapshot()
        print(f"      Pre-upgrade snapshot created: {snapshot.id}")
    except BackupError as exc:
        print(f"[WARN] Pre-upgrade backup failed: {exc}. Continuing with caution.")

    if not docker.is_docker_available():
        print("[ERROR] Docker daemon is not accessible. Cannot perform container upgrade.")
        return 1

    env_p = Path(env_path).resolve()

    # Step 2: Pull registry images or build the local publishing application.
    is_local_app = dir_name == "announcements"
    if is_local_app:
        print("[2/3] Building the local announcements application...")
    else:
        print(f"[2/3] Pulling updated container image for {service}...")
        pull_res = docker.run_compose(project_dir=svc_dir, args=["pull"], env_file=env_p)
        if pull_res.returncode != 0:
            print(f"[WARN] 'docker compose pull' exited with {pull_res.returncode}.")

    # Step 3: Recreate container with new image
    print(f"[3/3] Recreating and restarting {service}...")
    up_res = docker.run_compose(
        project_dir=svc_dir,
        args=[
            "up",
            "-d",
            *(["--build"] if is_local_app else []),
            "--no-deps",
            "--remove-orphans",
        ],
        env_file=env_p,
    )
    if up_res.returncode != 0:
        print(f"[ERROR] Failed to restart {service} (exit code {up_res.returncode}).")
        return 1

    print(f"\n  [SUCCESS] Service '{service}' upgraded and restarted successfully!")
    print("            Check status: community-stack status")
    print(f"            Check logs:   community-stack logs {service}")
    return 0
