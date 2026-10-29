"""Implementation of the 'init' command."""

from __future__ import annotations

import re
import secrets
import shutil
from pathlib import Path

from community_stack.config_edit import set_env_values, set_value, write_in_place
from community_stack.secrets import create_env_file

DEFAULT_CONFIG_TEMPLATE = """# Community Communications Stack Configuration
# Documentation: docs/architecture.md
# Schema: config/schema.json

project:
  name: "community"
  profile: "starter" # starter | community | accessible | resilient
  timezone: "UTC" # IANA name used for scheduling, e.g. Europe/Paris

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


DOMAIN = re.compile(r"^(?=.{3,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def run_init(
    dest_dir: Path | str = ".",
    force: bool = False,
    example: bool = False,
    project_name: str | None = None,
    base_domain: str | None = None,
    web_domain: str | None = None,
    matrix_domain: str | None = None,
    timezone: str | None = None,
    acme_email: str | None = None,
    console_uid: int | None = None,
    console_gid: int | None = None,
    setup_token: bool = False,
) -> int:
    """Initialize a local directory with configuration, secrets, and data folders."""
    web_domain, matrix_domain, base_domain = (
        value.lower() if value else value for value in (web_domain, matrix_domain, base_domain)
    )
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

    # 3b. Apply what the installer was told about this community
    for name, value in (("web", web_domain), ("matrix", matrix_domain), ("base", base_domain)):
        if value and not DOMAIN.fullmatch(value.lower()):
            print(f"[ERROR] '{value}' is not a valid domain name for {name}.")
            return 1
    if project_name and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,62}", project_name):
        print("[ERROR] The project name must be lowercase letters, digits, - or _.")
        return 1
    config_changes = {
        ("project", "name"): project_name,
        ("project", "timezone"): timezone,
        ("domain", "base"): base_domain,
        ("domain", "web"): web_domain,
        ("domain", "matrix"): matrix_domain,
    }
    text = config_path.read_text(encoding="utf-8")
    for path, value in config_changes.items():
        if value:
            text = set_value(text, path, value)
    write_in_place(config_path, text)
    env_values = {
        "WEB_DOMAIN": web_domain,
        "MATRIX_DOMAIN": matrix_domain,
        "ACME_EMAIL": acme_email,
        "COMMUNITY_STACK_UID": str(console_uid) if console_uid is not None else None,
        "COMMUNITY_STACK_GID": str(console_gid) if console_gid is not None else None,
    }
    token = secrets.token_urlsafe(24) if setup_token else None
    if token:
        env_values["COMMUNITY_STACK_ADMIN_SETUP_TOKEN"] = token
    env_values = {key: value for key, value in env_values.items() if value}
    if env_values:
        set_env_values(env_path, env_values)

    # 4. Create standard data and backup directories
    dirs_to_create = [
        base / "data",
        base / "data" / "matrix",
        base / "data" / "postgres",
        base / "data" / "landing",
        base / "data" / "element",
        base / "data" / "announcements",
        base / "data" / "landing" / "feed",
        base / "backups",
    ]
    for d in dirs_to_create:
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            print(f"  [CREATE] {d.relative_to(base)}/")

    if token:
        print(f"\n  First-administrator setup token (also in .env): {token}")
    print("\nInitialization complete! Next steps:")
    print("  1. Edit 'community-stack.yaml' with your domain and settings.")
    print("  2. Verify your configuration with: community-stack check")
    print("  3. Preview deployment changes with: community-stack plan")
    return 0
