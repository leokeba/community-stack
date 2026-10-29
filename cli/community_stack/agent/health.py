"""Health checks and redacted service logs for the console's Health page."""

from __future__ import annotations

import json
import re
import shutil
import socket
import sqlite3
import ssl
from datetime import datetime, timezone
from typing import Any

import yaml

from community_stack import docker
from community_stack.backup import BackupError
from community_stack.redact import redact_text
from community_stack.secrets import parse_env_file

OK, WARNING, PROBLEM = "ok", "warning", "problem"
LOG_SERVICES = ("proxy", "landing", "matrix", "element", "announcements", "whatsapp_bridge")
LOG_LINES = ("50", "200", "500")
MAX_LOG_BYTES = 200_000
BACKUP_WARNING_HOURS = 36
CERT_WARNING_DAYS = 14
TLS_TIMEOUT_SECONDS = 5


def _check(name: str, status: str, details: str) -> dict[str, str]:
    return {"name": name, "status": status, "details": details}


def disk_check(context) -> dict[str, str]:
    usage = shutil.disk_usage(context.project_dir)
    free_gb = usage.free / 1024**3
    share = usage.free / usage.total
    details = f"{free_gb:.1f} GB free of {usage.total / 1024**3:.0f} GB ({share:.0%})."
    if free_gb < 2 or share < 0.05:
        return _check("Disk space", PROBLEM, details + " Services may stop when the disk fills.")
    if free_gb < 5 or share < 0.15:
        return _check("Disk space", WARNING, details)
    return _check("Disk space", OK, details)


def container_checks() -> list[dict[str, str]]:
    if not docker.is_docker_available():
        return [_check("Docker", PROBLEM, "Docker is not reachable on the server.")]
    containers = docker.list_community_containers()
    if not containers:
        return [_check("Services", WARNING, "No stack services are deployed.")]
    checks = []
    for container in containers:
        status_text = container.get("status", "")
        if container.get("state") != "running" or "unhealthy" in status_text:
            status = PROBLEM
        elif "health: starting" in status_text:
            status = WARNING
        else:
            status = OK
        checks.append(_check(container["name"].removeprefix("community_"), status, status_text))
    return checks


def certificate_days_left(host: str) -> float:
    context = ssl.create_default_context()
    with (
        socket.create_connection((host, 443), timeout=TLS_TIMEOUT_SECONDS) as raw,
        context.wrap_socket(raw, server_hostname=host) as tls,
    ):
        not_after = tls.getpeercert()["notAfter"]
    expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), tz=timezone.utc)
    return (expires - datetime.now(timezone.utc)).total_seconds() / 86400


def certificate_checks(config: dict) -> list[dict[str, str]]:
    domains = config.get("domain", {})
    hosts = sorted({domains[key] for key in ("web", "matrix") if domains.get(key)})
    checks = []
    for host in hosts:
        if host.endswith(("example.org", ".test", ".local")):
            continue
        name = f"Certificate for {host}"
        try:
            days = certificate_days_left(host)
        except (OSError, ssl.SSLError, KeyError, ValueError) as exc:
            checks.append(_check(name, PROBLEM, f"Could not check it: {exc}"))
            continue
        if days < 3:
            checks.append(_check(name, PROBLEM, f"Expires in {days:.0f} days."))
        elif days < CERT_WARNING_DAYS:
            checks.append(
                _check(name, WARNING, f"Expires in {days:.0f} days; Caddy should renew it soon.")
            )
        else:
            checks.append(_check(name, OK, f"Valid for {days:.0f} more days."))
    return checks


def parse_restic_time(value: str) -> datetime:
    """restic writes nanoseconds and any UTC offset; fromisoformat on 3.10 accepts neither."""
    match = re.fullmatch(r"(.+T\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?", value)
    if not match:
        raise ValueError(f"Unrecognised restic time: {value}")
    base, fraction, offset = match.groups()
    micros = (fraction or "0")[:6].ljust(6, "0")
    offset = "+00:00" if offset in (None, "Z") else offset
    return datetime.fromisoformat(f"{base}.{micros}{offset}").astimezone(timezone.utc)


def latest_backup_time(context) -> datetime | None:
    engine = context.backup_engine()
    if engine.encryption == "restic":
        snapshots = engine._restic_snapshots(engine.get_backup_env())
        if not snapshots:
            return None
        return parse_restic_time(snapshots[-1]["time"])
    archives = sorted(engine.backup_dir.glob("snap_*.tar.gz"))
    if not archives:
        return None
    return datetime.fromtimestamp(archives[-1].stat().st_mtime, tz=timezone.utc)


def state_path(context):
    return context.project_dir / "data" / "agent" / "backup-state.json"


def read_state(context) -> dict[str, Any]:
    """The scheduler's record of its last automatic backup."""
    try:
        return json.loads(state_path(context).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


SCHEDULE_HOURS = {"hourly": 1, "daily": 24, "weekly": 168}


def backup_check(context) -> dict[str, str]:
    state = read_state(context)
    if state.get("last_result") == "failed":
        return _check(
            "Backups",
            PROBLEM,
            f"The last automatic backup failed: {state.get('last_error')}. "
            "It is retried every hour.",
        )
    try:
        latest = latest_backup_time(context)
    except BackupError as exc:
        return _check("Backups", PROBLEM, f"Could not read backups: {exc}")
    if latest is None:
        return _check("Backups", PROBLEM, "No backup exists yet.")
    hours = (datetime.now(timezone.utc) - latest).total_seconds() / 3600
    details = f"Latest backup {hours:.0f} hours ago."
    config = yaml.safe_load(context.config_path.read_text(encoding="utf-8")) or {}
    schedule = config.get("operations", {}).get("backups", {}).get("schedule", "daily")
    allowed = SCHEDULE_HOURS.get(schedule, 24) * 1.5 + 1
    if hours > max(allowed, BACKUP_WARNING_HOURS if schedule == "hourly" else 0):
        return _check(
            "Backups",
            WARNING,
            details + f" That is older than the {schedule} schedule; is the agent running?",
        )
    return _check("Backups", OK, details)


def health_get(context, params: dict[str, Any]) -> dict[str, Any]:
    config = yaml.safe_load(context.config_path.read_text(encoding="utf-8")) or {}
    checks = [disk_check(context), backup_check(context)]
    checks += certificate_checks(config)
    checks += container_checks()
    order = {PROBLEM: 0, WARNING: 1, OK: 2}
    worst = min((c["status"] for c in checks), key=order.__getitem__, default=OK)
    return {"overall": worst, "checks": checks}


def _known_secrets(context) -> list[str]:
    secrets = list(parse_env_file(context.env_path).values())
    if context.admin_db_path.is_file():
        conn = sqlite3.connect(f"file:{context.admin_db_path}?mode=ro", uri=True)
        try:
            for (values_json,) in conn.execute("SELECT values_json FROM channel_connections"):
                secrets.extend(str(v) for v in json.loads(values_json).values())
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return [s for s in secrets if len(s) >= 8]


def redact_logs(text: str, known_secrets: list[str]) -> str:
    text = redact_text(text, known_secrets)
    text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    return re.sub(r"\bbot\d{5,15}:[A-Za-z0-9_-]{30,}", "bot[REDACTED]", text)


def logs_get(context, params: dict[str, Any]) -> dict[str, Any]:
    service = params["service"]
    lines = params.get("lines", "200")
    result = docker.run_compose(
        context.services_dir / service,
        ["logs", "--no-color", "--tail", lines],
        env_file=context.env_path,
        capture_output=True,
    )
    output = (result.stdout or "") + (result.stderr or "")
    output = output[-MAX_LOG_BYTES:]
    return {
        "service": service,
        "lines": int(lines),
        "text": redact_logs(output, _known_secrets(context)),
    }
