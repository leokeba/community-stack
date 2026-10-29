"""Service updates to the image versions pinned in the installed release."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

import yaml

from community_stack import docker
from community_stack.agent import settings as service_control
from community_stack.backup import BackupError

UPDATABLE = ("proxy", "landing", "matrix", "element", "signal", "whatsapp_bridge")


class UpdateError(Exception):
    """An update the agent refuses or could not finish; shown to people."""


def release_notes(image: str) -> str | None:
    repository, _, tag = image.rpartition(":")
    version = tag.removeprefix("v").split("-")[0]
    if repository.endswith("synapse"):
        return f"https://github.com/element-hq/synapse/releases/tag/v{version}"
    if repository.endswith("element-web"):
        return f"https://github.com/element-hq/element-web/releases/tag/v{version}"
    if repository == "caddy":
        return f"https://github.com/caddyserver/caddy/releases/tag/v{version}"
    if repository == "nginx":
        return f"https://nginx.org/en/CHANGES-{'.'.join(version.split('.')[:2])}"
    if repository == "postgres":
        return f"https://www.postgresql.org/docs/release/{version}/"
    if repository.endswith("signal-cli-rest-api"):
        return f"https://github.com/bbernhard/signal-cli-rest-api/releases/tag/{tag}"
    if repository.endswith("mautrix/whatsapp"):
        return f"https://github.com/mautrix/whatsapp/releases/tag/v{version}"
    return None


def _pins(context, service: str) -> dict[str, dict[str, str]]:
    path = context.services_dir / service / "docker-compose.yml"
    if not path.is_file():
        return {}
    compose = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        name: {"image": spec["image"], "container": spec.get("container_name", name)}
        for name, spec in (compose.get("services") or {}).items()
        if spec.get("image")
    }


def _running(container: str) -> dict[str, str] | None:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Config.Image}} {{json .Image}}", container],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    image, image_id = result.stdout.split()
    return {"image": json.loads(image), "image_id": json.loads(image_id)}


def _major(image: str) -> str:
    return re.split(r"[.-]", image.rpartition(":")[2], maxsplit=1)[0]


def versions(context, params: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = []
    for service in UPDATABLE:
        for name, pin in _pins(context, service).items():
            running = _running(pin["container"])
            blocked = None
            if running and name == "postgres" and _major(running["image"]) != _major(pin["image"]):
                blocked = (
                    "A new PostgreSQL major version needs a database migration; "
                    "do it from the command line."
                )
            rows.append(
                {
                    "service": service,
                    "component": name,
                    "running": running["image"] if running else None,
                    "pinned": pin["image"],
                    "update_available": bool(running) and running["image"] != pin["image"],
                    "blocked": blocked,
                    "release_notes": release_notes(pin["image"]),
                }
            )
    return {"components": rows}


def update(context, params: dict[str, Any]) -> dict[str, Any]:
    """Back up, pull and restart one service on its pinned images, rolling back on failure."""
    service = params["service"]
    rows = [row for row in versions(context)["components"] if row["service"] == service]
    pending = [row for row in rows if row["update_available"]]
    if not pending:
        return {"steps": [f"{service} is already on the versions of this release."]}
    blocked = [row["blocked"] for row in pending if row["blocked"]]
    if blocked:
        raise UpdateError(blocked[0])

    steps = []
    engine = context.backup_engine()
    if engine.backups_cfg.get("enabled", True):
        try:
            snapshot = engine.create_snapshot()
        except BackupError as exc:
            raise UpdateError(f"The safety backup failed, so nothing was updated: {exc}") from exc
        steps.append(f"Safety backup {snapshot.id} created.")

    previous = {
        pin["container"]: (_running(pin["container"]) or {}).get("image_id")
        for pin in _pins(context, service).values()
    }
    service_dir = context.services_dir / service
    pull = docker.run_compose(service_dir, ["pull"], env_file=context.env_path, capture_output=True)
    if pull.returncode != 0:
        raise UpdateError(f"The new images for {service} could not be downloaded.")
    steps.append("New images downloaded.")
    try:
        service_control.set_service(context, service, True)
    except service_control.SettingsError as exc:
        # Run the previous images again under the new tags, then restart.
        for pin in _pins(context, service).values():
            image_id = previous.get(pin["container"])
            if image_id:
                subprocess.run(["docker", "tag", image_id, pin["image"]], check=False)
        try:
            service_control.set_service(context, service, True)
            restored = "The previous version was restored."
        except service_control.SettingsError:
            restored = "The previous version could not be restarted; check the Health page."
        raise UpdateError(f"{exc} {restored}") from exc
    steps.append(f"{service} is running the new version and is healthy.")
    return {"steps": steps}
