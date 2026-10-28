"""Secrets loading, validation, generation, and permission verification."""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from typing import Any

PLACEHOLDER_PREFIXES = ("replace_with_", "your_", "change_me", "example_")


def generate_secret(num_bytes: int = 32) -> str:
    """Generate a cryptographically secure hex secret."""
    return secrets.token_hex(num_bytes)


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple .env file into key-value pairs."""
    if not path.is_file():
        return {}

    env_vars: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            key, val = stripped.split("=", 1)
            key = key.strip()
            val = val.strip().strip("'\"")
            env_vars[key] = val
    return env_vars


def check_file_permissions(path: Path) -> list[str]:
    """Check that sensitive file permissions are sufficiently restricted (e.g. 0600 on POSIX)."""
    issues: list[str] = []
    if os.name != "posix" or not path.exists():
        return issues

    file_stat = path.stat()
    mode = file_stat.st_mode

    # Check if group or others have read/write/execute permissions
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        issues.append(
            f"Permissions on {path} are too open ({oct(stat.S_IMODE(mode))}). "
            "Run 'chmod 600 .env' to secure credentials."
        )
    return issues


def get_required_secrets(config: dict[str, Any]) -> list[str]:
    """Determine required environment secret keys based on enabled services in config."""
    required: list[str] = []
    services = config.get("services", {})
    operations = config.get("operations", {})

    if services.get("matrix", {}).get("enabled", False):
        required.extend(
            [
                "POSTGRES_PASSWORD",
                "SYNAPSE_SECRET_KEY",
                "SYNAPSE_REGISTRATION_SHARED_SECRET",
            ]
        )

    backups = operations.get("backups", {})
    if backups.get("enabled", False):
        required.append("RESTIC_PASSWORD")
        if backups.get("target") == "s3":
            required.extend(["S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET_URL"])

    if services.get("whatsapp_bridge", {}).get("enabled", False):
        required.extend(["WHATSAPP_AS_TOKEN", "WHATSAPP_HS_TOKEN"])

    return required


def validate_secrets(
    config: dict[str, Any], env_path: Path | str | None = None
) -> tuple[bool, list[str]]:
    """Validate that all required secrets are present, non-empty, and not default placeholders.

    Returns (is_valid, list_of_messages). Does NOT reveal secret values.
    """
    path = Path(env_path or ".env")
    messages: list[str] = []
    is_valid = True

    if not path.is_file():
        messages.append(f"Secrets file '{path}' is missing.")
        return False, messages

    # Check file permissions
    perm_issues = check_file_permissions(path)
    if perm_issues:
        messages.extend(perm_issues)

    env_vars = parse_env_file(path)
    required = get_required_secrets(config)

    for key in required:
        if key not in env_vars:
            messages.append(f"Missing required secret: {key}")
            is_valid = False
            continue

        val = env_vars[key]
        if not val or not val.strip():
            messages.append(f"Secret '{key}' is empty in {path}")
            is_valid = False
            continue

        lower_val = val.lower()
        if any(lower_val.startswith(p) for p in PLACEHOLDER_PREFIXES):
            messages.append(f"Secret '{key}' still contains a placeholder value")
            is_valid = False

    return is_valid, messages


def create_env_file(target_path: Path | str, force: bool = False) -> None:
    """Generate a fresh .env file with secure generated random secrets."""
    path = Path(target_path)
    if path.exists() and not force:
        raise FileExistsError(f"Secrets file {path} already exists. Use force=True to overwrite.")

    postgres_pw = generate_secret(24)
    synapse_secret = generate_secret(32)
    synapse_reg_secret = generate_secret(32)
    restic_pw = generate_secret(32)

    content = f"""# Community Communications Stack Secrets
# Generated securely by 'community-stack init'
# Permissions: 0600

POSTGRES_DB=synapse
POSTGRES_USER=synapse_user
POSTGRES_PASSWORD={postgres_pw}

SYNAPSE_SECRET_KEY={synapse_secret}
SYNAPSE_REGISTRATION_SHARED_SECRET={synapse_reg_secret}

RESTIC_PASSWORD={restic_pw}

# Optional S3 offsite backup credentials (if operations.backups.target is 's3')
# S3_ACCESS_KEY=
# S3_SECRET_KEY=
# S3_BUCKET_URL=
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    # Set 0600 permissions on POSIX
    if os.name == "posix":
        path.chmod(0o600)
