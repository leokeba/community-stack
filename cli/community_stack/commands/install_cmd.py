"""Implementation of the 'install' command."""

from __future__ import annotations

from pathlib import Path

from community_stack import docker
from community_stack.config import ConfigError, load_config
from community_stack.provision import ProvisionError, prepare_service
from community_stack.validator import run_checks


def get_services_dir() -> Path:
    """Locate the services/ directory."""
    cwd_services = Path.cwd() / "services"
    if cwd_services.is_dir():
        return cwd_services

    repo_services = Path(__file__).resolve().parent.parent.parent.parent / "services"
    if repo_services.is_dir():
        return repo_services

    raise FileNotFoundError("Could not locate services/ directory.")


def run_install(
    config_path: Path | str = "community-stack.yaml",
    env_path: Path | str = ".env",
) -> int:
    """Deploy enabled services according to configuration."""
    print("Executing pre-flight checks before deployment...\n")
    checks = run_checks(config_path=config_path, env_path=env_path)
    if not checks.ok:
        print("[ERROR] Pre-flight checks failed. Fix the issues below before installing:")
        for err in checks.errors:
            print(f"  - {err}")
        return 1

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"[ERROR] Failed to load configuration: {exc}")
        return 1

    if not docker.is_docker_available():
        print("[ERROR] Docker daemon is not reachable or not running.")
        print(
            "        Ensure Docker is installed and the daemon is active (e.g. 'systemctl start docker')."
        )
        return 1

    print("[1/4] Ensuring Docker networks are configured...")
    if not docker.ensure_network("proxy_net"):
        print("[ERROR] Failed to create or inspect Docker network 'proxy_net'.")
        return 1
    print("      Network 'proxy_net' is ready.")

    services_root = get_services_dir()
    env_p = Path(env_path).resolve()
    services_to_deploy: list[str] = []

    # Proxy is always deployed as ingress
    services_to_deploy.append("proxy")

    services_cfg = config.get("services", {})
    # Landing page
    services_to_deploy.append("landing")

    # Matrix
    if services_cfg.get("matrix", {}).get("enabled", True):
        services_to_deploy.append("matrix")

    # Element
    if services_cfg.get("element", {}).get("enabled", True):
        services_to_deploy.append("element")

    # Public announcement portal and authenticated publishing interface
    if services_cfg.get("announcements", {}).get("enabled", True):
        services_to_deploy.append("announcements")

    # WhatsApp bridge
    if services_cfg.get("signal", {}).get("enabled", False):
        services_to_deploy.append("signal")

    if services_cfg.get("whatsapp_bridge", {}).get("enabled", False):
        services_to_deploy.append("whatsapp_bridge")

    print(f"\n[2/4] Deploying enabled services: {', '.join(services_to_deploy)}")

    for svc in services_to_deploy:
        svc_dir = services_root / svc
        if not svc_dir.is_dir():
            print(f"[WARN] Service directory {svc_dir} not found. Skipping.")
            continue

        print(f"\n--- Deploying {svc} ({svc_dir.name}) ---")
        try:
            prepared = prepare_service(
                Path(config_path).resolve().parent,
                svc,
                config,
                Path(env_path).resolve(),
                services_root,
            )
        except ProvisionError as exc:
            print(f"[ERROR] {exc}")
            return 1
        if prepared:
            print(f"  {prepared}")
        res = docker.run_compose(
            project_dir=svc_dir,
            args=["up", "-d", "--remove-orphans"],
            env_file=env_p,
            capture_output=False,
        )
        if res.returncode != 0:
            print(f"[ERROR] Deployment of service '{svc}' failed (exit code {res.returncode}).")
            return 1
        print(f"  [SUCCESS] {svc} deployed successfully.")

    print("\n[3/4] Deployment finished. Verifying container status...")
    from community_stack.commands.status_cmd import run_status

    run_status()

    print("\n[4/4] Next steps:")
    print("  - Check logs: community-stack logs <service>")
    print("  - Run diagnostics: community-stack doctor")
    return 0
