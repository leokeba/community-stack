"""The bot's runtime configuration, written by provisioning into ``data/safemode/config.yaml``.

The file holds the appservice tokens, so it is created with mode 0600 and mounted only into
the bot container. The host CLI reads the database path from the project layout instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

BOT_LOCALPART = "safemode"
LISTEN_PORT = 8090
CONTAINER_DATA_DIR = "/data"


class BotConfigError(Exception):
    """The bot configuration is missing or incomplete."""


@dataclass(frozen=True)
class BotConfig:
    homeserver_url: str
    server_name: str
    bot_user_id: str
    as_token: str
    hs_token: str
    listen_port: int
    db_path: str

    @classmethod
    def load(cls, path: Path | str) -> BotConfig:
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise BotConfigError(f"Safe Mode is not provisioned: {path} is missing.") from exc
        required = (
            "homeserver_url",
            "server_name",
            "bot_user_id",
            "as_token",
            "hs_token",
            "db_path",
        )
        missing = [key for key in required if not raw.get(key)]
        if missing:
            raise BotConfigError(f"Safe Mode configuration is missing: {', '.join(missing)}")
        return cls(
            homeserver_url=str(raw["homeserver_url"]),
            server_name=str(raw["server_name"]),
            bot_user_id=str(raw["bot_user_id"]),
            as_token=str(raw["as_token"]),
            hs_token=str(raw["hs_token"]),
            listen_port=int(raw.get("listen_port", LISTEN_PORT)),
            db_path=str(raw["db_path"]),
        )


def write_config(path: Path, values: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        yaml.safe_dump(values, handle, sort_keys=True)
    os.chmod(path, 0o600)
