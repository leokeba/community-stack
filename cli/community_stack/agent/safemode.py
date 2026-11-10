"""Safe Mode operations for the control agent.

The console never touches the Safe Mode database or Docker. It asks the agent, which runs on
the host with the project's own files. Store-only work happens here directly; work that needs
Matrix runs inside the Safe Mode container, which holds the bot's credentials. Replies carry
room IDs and counts, never member lists, and recovery codes appear only in the one response
that issues them.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from community_stack import matrix_ids
from community_stack.config import ConfigError, load_config
from community_stack.safemode.manager import AccessManager, GatewayError, SafeModeError
from community_stack.safemode.store import Store, StoreMissing

CONTAINER = "community_safemode"
USER_ID = re.compile(r"@[a-z0-9._=/-]{1,64}:[A-Za-z0-9.-]{1,253}(?::\d{1,5})?")
ROOM_ID = matrix_ids.ROOM_ID
ROOM_NAME = re.compile(r"[^\n\r]{1,100}")
# Up to 20 Matrix IDs, separated by commas or spaces.
INVITES = re.compile(rf"{USER_ID.pattern}(?:[ ,]+{USER_ID.pattern}){{0,19}}")


class SafeModeOperationError(Exception):
    """Shown to the administrator. Kept separate so the operations module can convert it."""


class _HostGateway:
    """The host cannot reach the homeserver. Store-only operations never call it."""

    def __getattr__(self, name: str):
        def refuse(*_args, **_kwargs):
            raise GatewayError("Matrix is reached from the Safe Mode container.")

        return refuse


def _paths(project_dir: Path) -> tuple[Path, Path]:
    return (
        project_dir / "data" / "safemode" / "config.yaml",
        project_dir / "data" / "safemode" / "safemode.db",
    )


def _open(project_dir: Path) -> tuple[Store, AccessManager]:
    config_path, db_path = _paths(project_dir)
    if not config_path.is_file():
        raise SafeModeOperationError(
            "Safe Mode is not set up. Enable it in the configuration and run install."
        )
    try:
        bot_user = str(
            (yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}).get("bot_user_id", "")
        )
    except (OSError, yaml.YAMLError) as exc:
        raise SafeModeOperationError("Safe Mode configuration cannot be read.") from exc
    try:
        # Never create the file: the agent runs as root, and the bot could not write to a
        # database root created. The bot creates it when it first starts.
        store = Store(db_path, create=False)
    except StoreMissing as exc:
        raise SafeModeOperationError(
            "The Safe Mode database does not exist yet. Start the Safe Mode service first."
        ) from exc
    return store, AccessManager(store, _HostGateway(), bot_user)


def _in_store(project_dir: Path, action) -> dict[str, Any]:
    store, manager = _open(project_dir)
    try:
        return action(store, manager)
    except SafeModeError as exc:
        raise SafeModeOperationError(str(exc)) from exc
    finally:
        store.close()


def _container(args: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        ["docker", "exec", CONTAINER, "python", "-m", "community_stack.safemode.admin", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def _bot_status() -> str:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", CONTAINER],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "not running"


def overview(context, params: dict[str, str]) -> dict[str, Any]:
    try:
        config = load_config(context.config_path)
        enabled = bool(config.get("services", {}).get("safemode", {}).get("enabled", False))
    except ConfigError:
        enabled = False

    def read(store: Store, manager: AccessManager) -> dict[str, Any]:
        states: dict[str, int] = {}
        for _user, state in store.pending_accounts(
            ("SUSPENDING", "SUSPENDED", "PARTIAL", "RESTORING")
        ):
            states[state] = states.get(state, 0) + 1
        rooms = store.enabled_rooms()
        return {
            "enabled": enabled,
            "bot_status": _bot_status(),
            "rooms": [{"room_id": r.room_id, "name": r.name} for r in rooms],
            "enrolled_count": store.enrolled_count(),
            "states": states,
        }

    config_path, db_path = _paths(context.project_dir)
    if not config_path.is_file() or not db_path.is_file():
        return {
            "enabled": enabled,
            "bot_status": _bot_status(),
            "rooms": [],
            "enrolled_count": 0,
            "states": {},
            "set_up": False,
        }
    result = _in_store(context.project_dir, read)
    result["set_up"] = True
    return result


def status(context, params: dict[str, str]) -> dict[str, Any]:
    def read(store: Store, manager: AccessManager) -> dict[str, Any]:
        if not store.is_enrolled(params["user_id"]):
            raise SafeModeError("This account is not enrolled in Safe Mode.")
        current = store.state(params["user_id"])
        if current is None:
            return {"user_id": params["user_id"], "state": "ACTIVE", "outstanding": 0}
        outstanding = sum(
            1 for m in store.memberships(params["user_id"]) if m.result not in ("done", "skipped")
        )
        return {"user_id": params["user_id"], "state": current[0], "outstanding": outstanding}

    return _in_store(context.project_dir, read)


def enroll(context, params: dict[str, str]) -> dict[str, Any]:
    def write(store: Store, manager: AccessManager) -> dict[str, Any]:
        store.enroll(params["user_id"])
        return {"user_id": params["user_id"]}

    return _in_store(context.project_dir, write)


def recovery_code(context, params: dict[str, str]) -> dict[str, Any]:
    def issue(store: Store, manager: AccessManager) -> dict[str, Any]:
        return {
            "user_id": params["user_id"],
            "code": manager.issue_recovery_code(params["user_id"]),
        }

    return _in_store(context.project_dir, issue)


def unprotect(context, params: dict[str, str]) -> dict[str, Any]:
    def write(store: Store, manager: AccessManager) -> dict[str, Any]:
        if not manager.unprotect_room(params["room_id"]):
            raise SafeModeError("That room is not in the protected registry.")
        return {"room_id": params["room_id"], "protected": False}

    return _in_store(context.project_dir, write)


def protect(context, params: dict[str, str]) -> dict[str, Any]:
    code, output = _container(["protect", params["room_id"], "--name", params["name"]])
    if code != 0:
        raise SafeModeOperationError(output or "The room could not be protected.")
    return {"room_id": params["room_id"], "name": params["name"], "protected": True}


def create_room(context, params: dict[str, str]) -> dict[str, Any]:
    args = ["create-room", "--name", params["name"]]
    for user_id in re.split(r"[ ,]+", params.get("invite", "").strip()):
        if user_id:
            args += ["--invite", user_id]
    code, output = _container(args)
    match = re.search(r"Created and protected (\S+)", output)
    if code != 0 or not match:
        raise SafeModeOperationError(output or "The room could not be created.")
    return {"room_id": match.group(1), "name": params["name"], "protected": True}


def doctor(context, params: dict[str, str]) -> dict[str, Any]:
    code, output = _container(["doctor"])
    return {"ok": code == 0, "report": output}


def restore(context, params: dict[str, str]) -> dict[str, Any]:
    code, output = _container(["restore", params["user_id"], "--confirm"])
    if code != 0:
        raise SafeModeOperationError(output or "The account could not be restored.")
    return {"user_id": params["user_id"], "report": output}
