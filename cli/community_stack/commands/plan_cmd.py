"""Implementation of the 'plan' command."""

from __future__ import annotations

from pathlib import Path

from community_stack.config import ConfigError, load_config
from community_stack.secrets import validate_secrets


def run_plan(
    config_path: Path | str = "community-stack.yaml",
    env_path: Path | str = ".env",
) -> int:
    """Print a dry-run execution plan detailing what services would be deployed or modified."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"Error loading configuration: {exc}")
        return 1

    project = config.get("project", {})
    domain = config.get("domain", {})
    services = config.get("services", {})
    operations = config.get("operations", {})

    print("================================================================================")
    print("                     COMMUNITY COMMUNICATIONS STACK PLAN")
    print("================================================================================")
    print("NOTE: This is a read-only plan. No changes will be applied to the host.")
    print("================================================================================\n")

    print(f"Project Name:    {project.get('name')}")
    print(f"Profile:         {project.get('profile')} (Milestone-based scope)")
    print(f"Base Domain:     {domain.get('base')}")
    print(f"Web Portal:      {domain.get('web')}")
    print(f"Matrix Server:   {domain.get('matrix')}")

    print("\n--- INGRESS & REVERSE PROXY (Caddy) ---")
    print("  Ports:         80/tcp (HTTP -> HTTPS redirect), 443/tcp (HTTPS)")
    print("  TLS Mode:      Automated Let's Encrypt / ZeroSSL ACME certificate issuance")
    print("  Routes:")
    print(f"    - {domain.get('web')}/*              -> service: landing (static website)")
    print(f"    - {domain.get('web')}/app/*          -> service: element (Element Web client)")
    print(f"    - {domain.get('web')}/feed/*         -> service: announcements (RSS/JSON feeds)")
    print(
        f"    - {domain.get('matrix')}/_matrix/*   -> service: matrix (Synapse Client-Server API)"
    )
    print(
        f"    - {domain.get('matrix')}/.well-known -> service: matrix (Client & Server delegation)"
    )

    print("\n--- APPLICATION SERVICES ---")
    matrix_cfg = services.get("matrix", {})
    if matrix_cfg.get("enabled"):
        print("  [ENABLED]  Matrix Homeserver (Synapse v1.119.0)")
        print(
            f"             - Federation: {'Enabled' if matrix_cfg.get('federation') else 'Disabled (isolated community)'}"
        )
        print(
            f"             - Registration: {'Open' if matrix_cfg.get('allow_registration') else 'Closed (Admin invite only)'}"
        )
        print("             - Database: PostgreSQL 16 (isolated internal network)")
        print(f"             - Max Upload: {matrix_cfg.get('max_upload_size_mb', 50)} MB")
    else:
        print("  [DISABLED] Matrix Homeserver")

    element_cfg = services.get("element", {})
    if element_cfg.get("enabled"):
        print("  [ENABLED]  Element Web Client (v1.11.70)")
        print(f"             - Preconfigured default homeserver: https://{domain.get('matrix')}")
    else:
        print("  [DISABLED] Element Web Client")

    ann_cfg = services.get("announcements", {})
    if ann_cfg.get("enabled"):
        print("  [ENABLED]  Canonical Public Announcement Feed")
        print("             - Formats: Static Web HTML, RSS 2.0, JSON Feed")
    else:
        print("  [DISABLED] Canonical Public Announcement Feed")

    wa_cfg = services.get("whatsapp_bridge", {})
    if wa_cfg.get("enabled"):
        print(
            f"  [ENABLED]  WhatsApp Bridge (mautrix-whatsapp, Mode: {wa_cfg.get('mode', 'none')})"
        )
        print("             - Caution: Unofficial integration subject to account/protocol changes.")
    else:
        print("  [DISABLED] WhatsApp Bridge (Not deployed)")

    print("\n--- INTERNAL NETWORKS ---")
    print("  - proxy_net:       Connects Caddy ingress to public HTTP endpoints")
    print(
        "  - matrix_internal: Isolated network between Synapse and PostgreSQL (no external access)"
    )
    if wa_cfg.get("enabled"):
        print("  - bridge_net:      Isolated Appservice link between Synapse and WhatsApp bridge")

    print("\n--- OPERATIONS & PERSISTENCE ---")
    backups_cfg = operations.get("backups", {})
    if backups_cfg.get("enabled"):
        print(
            f"  Backups:       Enabled (Target: {backups_cfg.get('target')}, Schedule: {backups_cfg.get('schedule')})"
        )
        print(f"                 Retention: {backups_cfg.get('retention_days')} days")
        print(f"                 Storage Path: {backups_cfg.get('storage_path')}")
        if backups_cfg.get("encryption", "none") == "restic":
            print("                 Encryption: Client-side encrypted repository (restic)")
        else:
            print("                 Encryption: None (owner-only archives; set encryption: restic)")
    else:
        print("  Backups:       Disabled")

    secrets_ok, _ = validate_secrets(config, env_path=env_path)
    print(
        f"\nSecrets Status:  {'Configured and verified' if secrets_ok else 'ATTENTION: Missing or incomplete secrets in .env'}"
    )

    print("\n================================================================================")
    print("End of execution plan. To deploy, run: community-stack install")
    print("================================================================================")
    return 0
