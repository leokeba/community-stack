"""First-run setup for Safe Mode: appservice tokens, bot config, registration and Synapse entry.

Tokens live in ``.env`` (``SAFEMODE_AS_TOKEN``, ``SAFEMODE_HS_TOKEN``), the same place the
bridges keep theirs. The bot's runtime config is rendered once into ``data/safemode``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from community_stack import docker
from community_stack.provision import (
    SYNAPSE_UID,
    ProvisionError,
    _add_appservice_registration,
    _domain,
    synapse_image,
)
from community_stack.safemode.settings import BOT_LOCALPART, LISTEN_PORT, write_config
from community_stack.secrets import generate_secret, parse_env_file

SERVICE = "safemode"
CONTAINER_URL = "http://community-safemode:8090"
HOMESERVER_URL = "http://matrix-synapse:8008"
REGISTRATION_ID = "community-stack-safemode"
AS_KEY = "SAFEMODE_AS_TOKEN"
HS_KEY = "SAFEMODE_HS_TOKEN"


def _ensure_tokens(env_path: Path) -> dict[str, str]:
    values = parse_env_file(env_path)
    missing = [key for key in (AS_KEY, HS_KEY) if not values.get(key)]
    if not missing:
        return {AS_KEY: values[AS_KEY], HS_KEY: values[HS_KEY]}
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    for key in missing:
        value = generate_secret()
        for index, line in enumerate(lines):
            if line.partition("=")[0].strip() == key:
                lines[index] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
        values[key] = value
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    env_path.chmod(0o600)
    return {AS_KEY: values[AS_KEY], HS_KEY: values[HS_KEY]}


def registration_document(server_name: str, as_token: str, hs_token: str) -> dict[str, Any]:
    bot_user = f"@{BOT_LOCALPART}:{server_name}"
    return {
        "id": REGISTRATION_ID,
        "url": CONTAINER_URL,
        "as_token": as_token,
        "hs_token": hs_token,
        "sender_localpart": BOT_LOCALPART,
        "rate_limited": False,
        "namespaces": {
            "users": [{"exclusive": True, "regex": re.escape(bot_user)}],
            "aliases": [],
            "rooms": [],
        },
    }


def prepare_safemode(
    project_dir: Path, config: dict[str, Any], env_path: Path, services_dir: Path
) -> str | None:
    services = config.get("services", {})
    if not services.get("matrix", {}).get("enabled", True):
        raise ProvisionError("Enable the Matrix server before enabling Safe Mode.")
    matrix_dir = project_dir / "data" / "matrix"
    homeserver_path = matrix_dir / "homeserver.yaml"
    if not homeserver_path.is_file():
        raise ProvisionError("Matrix has not been provisioned yet. Start Matrix before Safe Mode.")

    server_name = _domain(config, "matrix")
    tokens = _ensure_tokens(env_path)
    data_dir = project_dir / "data" / SERVICE
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    data_dir.chmod(0o700)

    config_path = data_dir / "config.yaml"
    bot_user = f"@{BOT_LOCALPART}:{server_name}"
    if config_path.is_file():
        existing = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if existing.get("as_token") != tokens[AS_KEY] or existing.get("hs_token") != tokens[HS_KEY]:
            raise ProvisionError(
                "Safe Mode credentials in .env do not match its configuration. "
                "Restore the original values before starting the service."
            )
    else:
        write_config(
            config_path,
            {
                "homeserver_url": HOMESERVER_URL,
                "server_name": server_name,
                "bot_user_id": bot_user,
                "as_token": tokens[AS_KEY],
                "hs_token": tokens[HS_KEY],
                "listen_port": LISTEN_PORT,
                "db_path": "/data/safemode.db",
            },
        )

    appservices_dir = matrix_dir / "appservices"
    appservices_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    registration_path = appservices_dir / "safemode.yaml"
    document = registration_document(server_name, tokens[AS_KEY], tokens[HS_KEY])
    registration_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    registration_path.chmod(0o640)

    ownership = docker.run_container(
        [
            "--rm",
            "-v",
            f"{matrix_dir.resolve()}:/data",
            "--entrypoint",
            "chown",
            synapse_image(services_dir),
            "-R",
            f"{SYNAPSE_UID}:{SYNAPSE_UID}",
            "/data/appservices",
        ]
    )
    if ownership.returncode != 0:
        raise ProvisionError("Synapse could not read the Safe Mode appservice registration.")

    registered = _add_appservice_registration(homeserver_path, "/data/appservices/safemode.yaml")
    if registered:
        return "Configured Safe Mode and registered its bot with Synapse."
    return None
