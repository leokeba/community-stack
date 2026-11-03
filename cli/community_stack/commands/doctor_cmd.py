"""Diagnostic doctor command implementation for community-stack CLI."""

from __future__ import annotations

import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from community_stack.backup import BackupEngine, BackupError
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
        # Flag if a database exposes host ports directly.
        exposed_port = "5432" if "postgres" in name else "3306" if "facilmap_db" in name else None
        if exposed_port and f"{exposed_port}->" in c.get("status", ""):
            findings.append(
                f"[CRITICAL] Container {name} is exposing database port {exposed_port} publicly!"
            )
    if not findings:
        findings.append("[OK]   Internal network isolation verified (no database ports exposed).")
    return findings


def audit_vikunja(
    config: dict, config_path: Path | str, env_path: Path | str
) -> tuple[bool, list[str]]:
    """Check optional Vikunja containers, storage, HTTPS/API, database and backup."""
    settings = config.get("services", {}).get("vikunja", {})
    if not settings.get("enabled", False):
        return True, ["[INFO] Vikunja is disabled; optional-service checks skipped."]

    messages: list[str] = []
    healthy = True
    containers = {item.get("name"): item for item in list_community_containers()}
    for container_name, label in (
        ("community_vikunja", "application"),
        ("community_vikunja_db", "database"),
    ):
        container = containers.get(container_name)
        if not container:
            messages.append(f"[FAIL] Vikunja {label} container is not deployed.")
            healthy = False
        elif container.get("state") != "running":
            messages.append(f"[FAIL] Vikunja {label} container is not running.")
            healthy = False
        elif "(healthy)" in container.get("status", ""):
            messages.append(f"[OK] Vikunja {label} container is healthy.")
        elif container_name == "community_vikunja":
            messages.append(
                "[INFO] Vikunja application is running; its official scratch image "
                "is checked through the public /health endpoint below."
            )
        else:
            messages.append(f"[WARN] Vikunja {label} container is running without healthy status.")
            healthy = False

    project_root = Path(config_path).resolve().parent
    files_path = project_root / settings.get("files_path", "data/vikunja/files")
    try:
        database_path = project_root / settings.get("database", {}).get(
            "data_path", "data/vikunja/postgres"
        )
        if (
            files_path.is_dir()
            and database_path.is_dir()
            and os.access(files_path, os.W_OK)
            and os.access(database_path, os.W_OK)
            and shutil.disk_usage(files_path).free > 0
        ):
            messages.append(f"[OK] Vikunja persistent files are available at {files_path}.")
        else:
            messages.append(
                f"[FAIL] Vikunja files or database storage are unavailable/writable: "
                f"{files_path}, {database_path}."
            )
            healthy = False
    except OSError as exc:
        messages.append(f"[FAIL] Vikunja persistent storage check failed: {exc}")
        healthy = False

    hostname = settings.get("hostname") or f"tasks.{config['domain']['base']}"
    if hostname.endswith(("example.org", ".test", ".local", "localhost")):
        messages.append(f"[WARN] Skipping HTTPS reachability for placeholder host {hostname}.")
        healthy = False
    else:
        try:
            with urllib.request.urlopen(f"https://{hostname}/health", timeout=5) as response:
                body = response.read(64).decode("utf-8", errors="replace").strip()
                if response.status == 200 and body == "OK":
                    messages.append(
                        "[OK] Vikunja /health is reachable through HTTPS (database included)."
                    )
                else:
                    messages.append(
                        f"[FAIL] Vikunja HTTPS health endpoint returned {response.status}: {body}"
                    )
                    healthy = False
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            messages.append(f"[FAIL] Vikunja is not reachable through HTTPS: {exc}")
            healthy = False

    try:
        engine = BackupEngine(config_path=config_path, env_path=env_path)
        backup_ok, backup_messages = engine.verify_backup()
        if backup_ok:
            messages.append("[OK] A Vikunja-inclusive backup is present and structurally valid.")
        else:
            messages.append("[FAIL] Vikunja backup check failed: " + "; ".join(backup_messages))
            healthy = False
    except (BackupError, ConfigError, OSError) as exc:
        messages.append(f"[FAIL] Vikunja backup check failed: {exc}")
        healthy = False
    return healthy, messages


def audit_facilmap(
    config: dict, config_path: Path | str, env_path: Path | str
) -> tuple[bool, list[str]]:
    """Check optional FacilMap containers, storage, HTTPS Socket.IO, database, and backup."""
    settings = config.get("services", {}).get("facilmap", {})
    if not settings.get("enabled", False):
        return True, ["[INFO] FacilMap is disabled; optional-service checks skipped."]

    messages: list[str] = []
    healthy = True
    containers = {item.get("name"): item for item in list_community_containers()}
    for container_name, label in (
        ("community_facilmap", "application"),
        ("community_facilmap_db", "database"),
    ):
        container = containers.get(container_name)
        if not container:
            messages.append(f"[FAIL] FacilMap {label} container is not deployed.")
            healthy = False
        elif container.get("state") != "running":
            messages.append(f"[FAIL] FacilMap {label} container is not running.")
            healthy = False
        elif "(healthy)" in container.get("status", ""):
            messages.append(f"[OK] FacilMap {label} container is healthy.")
        else:
            messages.append(f"[FAIL] FacilMap {label} container is running but not healthy.")
            healthy = False

    project_root = Path(config_path).resolve().parent
    cache_path = project_root / settings.get("cache_path", "data/facilmap/cache")
    database_path = project_root / settings.get("database", {}).get(
        "data_path", "data/facilmap/mariadb"
    )
    try:
        if cache_path.is_dir() and database_path.is_dir():
            if shutil.disk_usage(cache_path).free > 0 and shutil.disk_usage(database_path).free > 0:
                messages.append(
                    f"[OK] FacilMap cache and database storage are present at "
                    f"{cache_path} and {database_path}."
                )
            else:
                messages.append("[FAIL] FacilMap persistent storage has no free space.")
                healthy = False
        else:
            messages.append(
                f"[FAIL] FacilMap persistent storage is missing: {cache_path}, {database_path}."
            )
            healthy = False
    except OSError as exc:
        messages.append(f"[FAIL] FacilMap persistent storage check failed: {exc}")
        healthy = False

    hostname = settings.get("hostname") or f"maps.{config['domain']['base']}"
    if hostname.endswith(("example.org", ".test", ".local", "localhost")):
        messages.append(f"[WARN] Skipping HTTPS reachability for placeholder host {hostname}.")
        healthy = False
    else:
        try:
            with urllib.request.urlopen(f"https://{hostname}/", timeout=10) as response:
                if response.status == 200:
                    messages.append("[OK] FacilMap application is reachable through HTTPS.")
                else:
                    messages.append(
                        f"[FAIL] FacilMap HTTPS application endpoint returned {response.status}."
                    )
                    healthy = False
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            messages.append(f"[FAIL] FacilMap is not reachable through HTTPS: {exc}")
            healthy = False
        try:
            url = f"https://{hostname}/socket.io/?EIO=4&transport=polling"
            with urllib.request.urlopen(url, timeout=10) as response:
                body = response.read(512).decode("utf-8", errors="replace")
                if response.status == 200 and body.startswith("0{"):
                    messages.append(
                        "[OK] FacilMap Socket.IO polling handshake is reachable through HTTPS."
                    )
                else:
                    messages.append(
                        f"[FAIL] FacilMap HTTPS Socket.IO endpoint returned "
                        f"{response.status}: {body[:160]}"
                    )
                    healthy = False
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            messages.append(f"[FAIL] FacilMap Socket.IO is not reachable through HTTPS: {exc}")
            healthy = False

    try:
        engine = BackupEngine(config_path=config_path, env_path=env_path)
        backup_ok, backup_messages = engine.verify_backup()
        if backup_ok:
            messages.append("[OK] A FacilMap-inclusive backup is present and structurally valid.")
        else:
            messages.append("[FAIL] FacilMap backup check failed: " + "; ".join(backup_messages))
            healthy = False
    except (BackupError, ConfigError, OSError) as exc:
        messages.append(f"[FAIL] FacilMap backup check failed: {exc}")
        healthy = False
    return healthy, messages


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
        cfg = {}

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

    vikunja_ok = True
    facilmap_ok = True
    if cfg:
        print("\n--- VIKUNJA SERVICE DIAGNOSTICS ---")
        vikunja_ok, messages = audit_vikunja(cfg, config_path, env_path)
        for message in messages:
            print(f"  {message}")
        print("\n--- FACILMAP SERVICE DIAGNOSTICS ---")
        facilmap_ok, messages = audit_facilmap(cfg, config_path, env_path)
        for message in messages:
            print(f"  {message}")

    print("\n================================================================================")
    print("Doctor report complete. All sensitive tokens and passwords have been redacted.")
    print("================================================================================")
    return 0 if checks.ok and vikunja_ok and facilmap_ok else 1
