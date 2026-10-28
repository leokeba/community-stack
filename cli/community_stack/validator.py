"""System and environment validation for community-stack."""

from __future__ import annotations

import shutil
import socket
from pathlib import Path

from community_stack.config import ConfigError, load_config
from community_stack.secrets import validate_secrets


class ValidationResult:
    """Encapsulates the results of a validation check."""

    def __init__(self) -> None:
        self.ok: bool = True
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.info: list[str] = []

    def add_error(self, message: str) -> None:
        self.errors.append(message)
        self.ok = False

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

    def add_info(self, message: str) -> None:
        self.info.append(message)


def check_tool_dependencies(result: ValidationResult) -> None:
    """Check availability of required external CLI tools."""
    docker_bin = shutil.which("docker")
    if docker_bin:
        result.add_info(f"Docker binary found: {docker_bin}")
    else:
        result.add_warning("Docker is not installed or not in PATH.")

    # Check for docker compose
    compose_v2 = shutil.which("docker") is not None
    # We can also check if docker-compose v1 exists
    compose_v1 = shutil.which("docker-compose") is not None
    if not (compose_v2 or compose_v1):
        result.add_warning("Docker Compose is not detected.")


def check_dns_resolution(domain: str, result: ValidationResult) -> None:
    """Check DNS resolution for configured domains."""
    if domain.endswith(("example.org", ".test", ".local")):
        result.add_info(f"Domain '{domain}' is a test/example domain; skipping public DNS check.")
        return

    try:
        ip = socket.gethostbyname(domain)
        result.add_info(f"Domain '{domain}' resolves to {ip}")
    except socket.gaierror:
        result.add_warning(
            f"Domain '{domain}' does not resolve via DNS. Ensure DNS A/AAAA records point to this host before requesting TLS."
        )


def check_local_directories(base_dir: Path, result: ValidationResult) -> None:
    """Verify local state and data directories exist."""
    expected = [base_dir / "data", base_dir / "backups"]
    for path in expected:
        if path.is_dir():
            result.add_info(f"Directory exists: {path}")
        else:
            result.add_warning(
                f"Directory '{path}' does not exist yet. Run 'community-stack init' to create."
            )


def run_checks(
    config_path: Path | str = "community-stack.yaml",
    env_path: Path | str = ".env",
    skip_dns: bool = False,
) -> ValidationResult:
    """Run comprehensive configuration, secrets, and system checks."""
    result = ValidationResult()

    # 1. Configuration loading and schema validation
    config_p = Path(config_path)
    if not config_p.is_file():
        result.add_error(f"Configuration file not found: {config_p}. Run 'community-stack init'.")
        return result

    try:
        config = load_config(config_p)
        result.add_info(f"Configuration '{config_p}' loaded and passed JSON Schema validation.")
    except ConfigError as exc:
        result.add_error(str(exc))
        return result

    # 2. Check local directories relative to configuration base
    base_dir = config_p.parent.resolve()
    check_local_directories(base_dir, result)

    # 3. Secrets validation
    secrets_ok, secret_messages = validate_secrets(config, env_path=env_path)
    if secrets_ok:
        result.add_info("All required secrets are configured.")
    else:
        for msg in secret_messages:
            if "too open" in msg:
                result.add_warning(msg)
            else:
                result.add_error(msg)

    # 4. Tool dependencies
    check_tool_dependencies(result)

    # 5. DNS checks
    if not skip_dns:
        base_domain = config.get("domain", {}).get("base")
        if base_domain:
            check_dns_resolution(base_domain, result)
        matrix_domain = config.get("domain", {}).get("matrix")
        if matrix_domain and matrix_domain != base_domain:
            check_dns_resolution(matrix_domain, result)
        web_domain = config.get("domain", {}).get("web")
        if web_domain and web_domain != base_domain:
            check_dns_resolution(web_domain, result)

    return result
