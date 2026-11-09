"""The 'access' command: Safe Mode rooms, accounts, recovery codes and the bot."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

import yaml

from community_stack import matrix_ids
from community_stack.safemode.manager import AccessManager, GatewayError, SafeModeError
from community_stack.safemode.store import Store

CONTAINER = "community_safemode"
USER_ID = re.compile(r"^@[a-z0-9._=/-]{1,64}:[A-Za-z0-9.-]{1,253}(:\d{1,5})?$")
ROOM_ID = matrix_ids.ROOM_ID


class _OfflineGateway:
    """Used on the host, which cannot reach the homeserver. Only store operations run here."""

    def __getattr__(self, name: str):
        def refuse(*_args, **_kwargs):
            raise GatewayError("Matrix is reached from the Safe Mode container, not the host.")

        return refuse


def run_access(args: argparse.Namespace) -> int:
    project = Path(getattr(args, "dir", ".") or ".").resolve()
    action = getattr(args, "access_action", None)
    if not action:
        print(
            "Usage: community-stack access {rooms,protect,unprotect,enroll,recovery-code,status,restore,doctor,bot,logs}"
        )
        return 1
    if action == "bot":
        return _bot_status(project)
    if action == "logs":
        return _run(["docker", "logs", "--tail", str(args.lines), CONTAINER])
    if action in ("protect", "restore", "doctor"):
        try:
            return _admin_exec(project, args)
        except SafeModeError as exc:
            print(f"[ERROR] {exc}")
            return 1

    try:
        store, manager = _open(project)
    except SafeModeError as exc:
        print(f"[ERROR] {exc}")
        return 1
    try:
        return _store_action(action, args, store, manager)
    except SafeModeError as exc:
        print(f"[ERROR] {exc}")
        return 1
    finally:
        store.close()


def _store_action(
    action: str, args: argparse.Namespace, store: Store, manager: AccessManager
) -> int:
    if action == "rooms":
        rooms = store.enabled_rooms()
        if not rooms:
            print(
                "No protected rooms. Add one with: community-stack access protect <room-id> --name <name>"
            )
            return 0
        for room in rooms:
            print(f"{room.room_id}  {room.name}")
        print(f"\nTotal: {len(rooms)} protected room(s)")
        return 0
    if action == "unprotect":
        _check(args.room_id, ROOM_ID, "room ID")
        if manager.unprotect_room(args.room_id):
            print("Stopped protecting the room. Existing suspensions are unchanged.")
            return 0
        print("That room is not in the protected registry.")
        return 1
    if action == "enroll":
        _check(args.user_id, USER_ID, "Matrix user ID")
        store.enroll(args.user_id)
        print(f"Enrolled {args.user_id} in Safe Mode.")
        print(
            "Issue their recovery code with: community-stack access recovery-code " + args.user_id
        )
        return 0
    if action == "recovery-code":
        _check(args.user_id, USER_ID, "Matrix user ID")
        code = manager.issue_recovery_code(args.user_id)
        print("Recovery code (shown once; give it to the person and keep no copy here):")
        print(f"  {code}")
        print("It is needed to turn Safe Mode off. Issuing a new code replaces this one.")
        return 0
    if action == "status":
        _check(args.user_id, USER_ID, "Matrix user ID")
        if not store.is_enrolled(args.user_id):
            print("This account is not enrolled in Safe Mode.")
            return 1
        print(_state_summary(store, args.user_id))
        return 0
    raise SafeModeError(f"Unknown access action: {action}")


def _state_summary(store: Store, user_id: str) -> str:
    current = store.state(user_id)
    if current is None:
        return "State: ACTIVE (Safe Mode off)"
    memberships = store.memberships(user_id)
    failed = sum(1 for m in memberships if m.result not in ("done", "skipped"))
    return f"State: {current[0]}. Rooms in snapshot: {len(memberships)}. Outstanding: {failed}."


def _admin_exec(project: Path, args: argparse.Namespace) -> int:
    command = ["docker", "exec", CONTAINER, "python", "-m", "community_stack.safemode.admin"]
    if args.access_action == "protect":
        _check(args.room_id, ROOM_ID, "room ID")
        command += ["protect", args.room_id, "--name", args.name]
    elif args.access_action == "restore":
        _check(args.user_id, USER_ID, "Matrix user ID")
        if not args.confirm:
            print(
                "[REFUSED] Administrative restore needs --confirm. Members normally use their recovery code."
            )
            return 1
        command += ["restore", args.user_id, "--confirm"]
    else:
        command += ["doctor"]
    return _run(command)


def _bot_status(project: Path) -> int:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", CONTAINER],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print("Safe Mode bot: not installed or not running.")
        return 1
    print(f"Safe Mode bot container: {result.stdout.strip()}")
    return 0


def _open(project: Path) -> tuple[Store, AccessManager]:
    config_path = project / "data" / "safemode" / "config.yaml"
    db_path = project / "data" / "safemode" / "safemode.db"
    if not config_path.is_file():
        raise SafeModeError(
            "Safe Mode is not set up. Enable it in community-stack.yaml and run install."
        )
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    store = Store(db_path)
    return store, AccessManager(store, _OfflineGateway(), str(config.get("bot_user_id", "")))


def _check(value: str, pattern: re.Pattern[str], label: str) -> None:
    if not pattern.fullmatch(value or ""):
        raise SafeModeError(f"That is not a valid {label}.")


def _run(command: list[str]) -> int:
    return subprocess.run(command, check=False).returncode
