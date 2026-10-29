"""Docker and Docker Compose orchestration utilities for community-stack."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any


def is_docker_available() -> bool:
    """Return True if docker CLI is installed and responsive."""
    if not shutil.which("docker"):
        return False
    try:
        res = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return res.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def get_compose_command() -> list[str]:
    """Determine the appropriate Docker Compose command."""
    if shutil.which("docker"):
        # Test if `docker compose` plugin works
        try:
            res = subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if res.returncode == 0:
                return ["docker", "compose"]
        except (subprocess.SubprocessError, OSError):
            pass

    if shutil.which("docker-compose"):
        return ["docker-compose"]

    return ["docker", "compose"]


def run_compose(
    project_dir: Path | str,
    args: list[str],
    env_file: Path | str | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Execute a docker compose command in a given service directory."""
    base_cmd = get_compose_command()
    cmd = list(base_cmd)

    if env_file:
        env_p = Path(env_file).resolve()
        if env_p.is_file():
            cmd.extend(["--env-file", str(env_p)])

    cmd.extend(args)

    return subprocess.run(
        cmd,
        cwd=str(project_dir),
        capture_output=capture_output,
        text=True,
        check=False,
    )


def run_container(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a one-off `docker run` and capture its output."""
    return subprocess.run(["docker", "run", *args], capture_output=True, text=True, check=False)


def ensure_network(network_name: str = "proxy_net") -> bool:
    """Ensure that an external Docker network exists."""
    if not is_docker_available():
        return False

    # Check if network exists
    res = subprocess.run(
        ["docker", "network", "inspect", network_name],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode == 0:
        return True

    # Create network
    create_res = subprocess.run(
        ["docker", "network", "create", network_name],
        capture_output=True,
        text=True,
        check=False,
    )
    return create_res.returncode == 0


def list_community_containers() -> list[dict[str, Any]]:
    """List running or stopped community-stack containers."""
    if not is_docker_available():
        return []

    res = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "name=community_",
            "--format",
            "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.State}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return []

    containers: list[dict[str, Any]] = []
    for line in res.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            containers.append(
                {
                    "name": parts[0],
                    "image": parts[1],
                    "status": parts[2],
                    "state": parts[3],
                }
            )
    return containers
