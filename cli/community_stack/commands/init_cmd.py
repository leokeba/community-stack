"""Implementation of the 'init' command."""

from __future__ import annotations

import shutil
from pathlib import Path

from community_stack.secrets import create_env_file

DEFAULT_CONFIG_TEMPLATE = """# Community Communications Stack Configuration
# Documentation: docs/architecture.md
# Schema: config/schema.json

project:
  name: "community"
  profile: "starter" # starter | community | accessible | resilient

domain:
  base: "example.org"
  matrix: "matrix.example.org"
  web: "community.example.org"

services:
  matrix:
    enabled: true
    federation: false
    allow_registration: false
    database_type: "postgres"
    max_upload_size_mb: 50
  element:
    enabled: true
  announcements:
    enabled: true
  whatsapp_bridge:
    enabled: false
    mode: "none" # none | announcements_only | selective_bridge
    mapped_rooms: []

operations:
  backups:
    enabled: true
    schedule: "daily"
    target: "local"
    encryption: "restic" # restic (client-side encrypted) | none (owner-only archives)
    retention_days: 14
    storage_path: "./backups"
  monitoring:
    enabled: true
"""


def run_init(
    dest_dir: Path | str = ".",
    force: bool = False,
    example: bool = False,
) -> int:
    """Initialize a local directory with configuration, secrets, and data folders."""
    base = Path(dest_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)

    config_path = base / "community-stack.yaml"
    env_path = base / ".env"
    env_example_path = base / ".env.example"

    print(f"Initializing Community Stack in: {base}")

    # 1. Create community-stack.yaml
    if config_path.exists() and not force:
        print(f"  [SKIP] {config_path.name} already exists (use --force to overwrite)")
    else:
        # Check if example config exists to copy from
        repo_example = (
            Path(__file__).resolve().parent.parent.parent.parent / "config" / "example.yaml"
        )
        if repo_example.is_file() and example:
            shutil.copy(repo_example, config_path)
        else:
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(DEFAULT_CONFIG_TEMPLATE)
        print(f"  [CREATE] {config_path.name}")

    # 2. Copy or create .env.example
    repo_env_example = Path(__file__).resolve().parent.parent.parent.parent / ".env.example"
    if repo_env_example.is_file() and not env_example_path.exists():
        shutil.copy(repo_env_example, env_example_path)
        print(f"  [CREATE] {env_example_path.name}")

    # 3. Create .env with generated secrets
    if env_path.exists() and not force:
        print(f"  [SKIP] {env_path.name} already exists (use --force to overwrite)")
    else:
        create_env_file(env_path, force=force)
        print(f"  [CREATE] {env_path.name} (permissions 0600, generated secrets)")

    # 4. Create standard data and backup directories
    dirs_to_create = [
        base / "data",
        base / "data" / "matrix",
        base / "data" / "postgres",
        base / "data" / "landing",
        base / "data" / "element",
        base / "backups",
    ]
    for d in dirs_to_create:
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            print(f"  [CREATE] {d.relative_to(base)}/")

    print("\nInitialization complete! Next steps:")
    print("  1. Edit 'community-stack.yaml' with your domain and settings.")
    print("  2. Verify your configuration with: community-stack check")
    print("  3. Preview deployment changes with: community-stack plan")
    return 0
