"""Diagnostic doctor command implementation for community-stack CLI."""

from __future__ import annotations

from pathlib import Path

from community_stack.config import ConfigError, load_config
from community_stack.redact import redact_dict
from community_stack.secrets import parse_env_file
from community_stack.validator import run_checks


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

    # 2. Config dump (safe non-secrets)
    print("\n--- ACTIVE CONFIGURATION DUMP ---")
    try:
        cfg = load_config(config_path)
        safe_cfg = redact_dict(cfg)
        import yaml

        print(yaml.safe_dump(safe_cfg, default_flow_style=False, indent=2))
    except (ConfigError, OSError) as exc:
        print(f"  Could not load configuration: {exc}")

    # 3. Environment Secrets Audit (Redacted)
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
