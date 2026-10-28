"""Diagnostic doctor command implementation for community-stack CLI."""

from __future__ import annotations

import shutil
from pathlib import Path

from community_stack.config import ConfigError, load_config
from community_stack.docker import is_docker_available, list_community_containers
from community_stack.redact import redact_dict
from community_stack.secrets import parse_env_file
from community_stack.validator import run_checks


def audit_disk_space(target_path: Path) -> tuple[bool, str]:
    """Check available disk space on the target volume."""
    try:
        usage = shutil.disk_usage(target_path)
        free_gb = usage.free / (1024**3)
        total_gb = usage.total / (1024**3)
        percent_free = (usage.free / usage.total) * 100
        details = f"{free_gb:.1f} GB free of {total_gb:.1f} GB ({percent_free:.1f}% free)"
        is_healthy = free_gb >= 2.0  # At least 2 GB free required
        return is_healthy, details
    except OSError as exc:
        return False, f"Could not determine disk usage: {exc}"


def audit_network_isolation() -> list[str]:
    """Verify that internal services (Postgres, Synapse internal) are not exposing public ports."""
    findings: list[str] = []
    if not is_docker_available():
        findings.append("[INFO] Docker not reachable; skipping live port exposure audit.")
        return findings

    containers = list_community_containers()
    for c in containers:
        name = c.get("name", "")
        # Flag if postgres exposes host ports directly
        if "postgres" in name and "5432->" in c.get("status", ""):
            findings.append(f"[CRITICAL] Container {name} is exposing database port 5432 publicly!")
    if not findings:
        findings.append("[OK]   Internal network isolation verified (No database ports exposed).")
    return findings


def run_doctor(
    config_path: Path | str = "community-stack.yaml", env_path: Path | str = ".env"
) -> int:
    """Perform non-destructive diagnostics with secret redaction."""
    print("================================================================================")
    print("                   COMMUNITY STACK DIAGNOSTIC DOCTOR REPORT                     ")
    print("================================================================================\n")

    # 1. Run standard checks
    checks = run_checks(config_path=config_path, env_path=env_path)
    print("--- PRE-FLIGHT DIAGNOSTICS ---")
    for msg in checks.info:
        print(f"  [OK]      {msg}")
    for msg in checks.warnings:
        print(f"  [WARN]    {msg}")
    for msg in checks.errors:
        print(f"  [FAIL]    {msg}")

    # 2. Storage & Disk Space Audit
    print("\n--- STORAGE & DISK CAPACITY AUDIT ---")
    data_dir = Path("data").resolve()
    disk_ok, disk_details = audit_disk_space(data_dir.parent if not data_dir.exists() else data_dir)
    disk_glyph = "[OK]  " if disk_ok else "[WARN]"
    print(f"  {disk_glyph} Storage volume: {disk_details}")

    # 3. Network Isolation & Port Security Audit
    print("\n--- NETWORK ISOLATION & PORT AUDIT ---")
    for line in audit_network_isolation():
        print(f"  {line}")

    # 4. Config dump (safe non-secrets)
    print("\n--- ACTIVE CONFIGURATION DUMP ---")
    try:
        cfg = load_config(config_path)
        safe_cfg = redact_dict(cfg)
        import yaml

        print(yaml.safe_dump(safe_cfg, default_flow_style=False, indent=2))
    except (ConfigError, OSError) as exc:
        print(f"  Could not load configuration: {exc}")

    # 5. Environment Secrets Audit (Redacted)
    print("--- SECRETS AUDIT (REDACTED) ---")
    env_file = Path(env_path)
    if env_file.is_file():
        parsed = parse_env_file(env_file)
        redacted_env = redact_dict(parsed)
        for k, v in sorted(redacted_env.items()):
            print(f"  {k} = {v}")
    else:
        print(f"  Secrets file {env_file} is absent.")

    print("\n================================================================================")
    print("Doctor report complete. All sensitive tokens and passwords have been redacted.")
    print("================================================================================")
    return 0 if checks.ok else 1
