"""Implementation of the 'check' command."""

from __future__ import annotations

from pathlib import Path

from community_stack.validator import run_checks


def run_check(
    config_path: Path | str = "community-stack.yaml",
    env_path: Path | str = ".env",
    skip_dns: bool = False,
) -> int:
    """Run configuration, secret, permission, and environment health checks."""
    print("Running Community Stack pre-flight checks...\n")

    result = run_checks(config_path=config_path, env_path=env_path, skip_dns=skip_dns)

    # Print info items
    for item in result.info:
        print(f"  [OK]      {item}")

    # Print warnings
    for item in result.warnings:
        print(f"  [WARNING] {item}")

    # Print errors
    for item in result.errors:
        print(f"  [ERROR]   {item}")

    print("\nSummary:")
    if result.ok:
        if result.warnings:
            print("  Check passed with warnings. Review warnings above before deploying.")
        else:
            print("  All checks passed successfully! System is ready for deployment.")
        return 0
    else:
        print(f"  Check failed with {len(result.errors)} error(s). Please fix issues above.")
        return 1
