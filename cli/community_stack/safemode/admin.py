"""Operator commands that need Matrix. They run inside the Safe Mode container.

The host CLI calls these with ``docker exec community_safemode python -m
community_stack.safemode.admin ...``, so the homeserver is never reached from the host and
no credentials leave the container. Commands that only touch the database run on the host.
Membership changes are never made from here: ``restore`` records the change and asks the
running bot to carry it out.
"""

from __future__ import annotations

import argparse
import sys

from community_stack.safemode.bot import ask_bot_to_drive, build
from community_stack.safemode.manager import GatewayError, SafeModeError, compatibility_problems
from community_stack.safemode.settings import BotConfig, BotConfigError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="safemode-admin")
    parser.add_argument("--config", default="/data/config.yaml")
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("doctor", help="Check every protected room against the compatibility rules")

    protect = sub.add_parser("protect", help="Mark a room protected if it is compatible")
    protect.add_argument("room_id")
    protect.add_argument("--name", required=True)

    create = sub.add_parser("create-room", help="Create a protected room owned by the bot")
    create.add_argument("--name", required=True)
    create.add_argument("--invite", action="append", default=[], metavar="USER_ID")

    unprotect = sub.add_parser("unprotect", help="Stop protecting a room")
    unprotect.add_argument("room_id")

    restore = sub.add_parser("restore", help="Restore an account without its recovery code")
    restore.add_argument("user_id")
    restore.add_argument("--confirm", action="store_true", required=True)

    args = parser.parse_args(argv)
    try:
        config = BotConfig.load(args.config)
    except BotConfigError as exc:
        print(f"[ERROR] {exc}")
        return 1
    bot, gateway, store = build(config)
    manager = bot.manager
    try:
        if args.action == "doctor":
            return _doctor(manager, gateway, store)
        if args.action == "protect":
            problems = manager.protect_room(args.room_id, args.name)
            if problems:
                print("[REFUSED] This room cannot be protected:")
                for problem in problems:
                    print(f"  - {problem}")
                return 1
            print("Protected. Members are removed when Safe Mode is turned on.")
            return 0
        if args.action == "create-room":
            room_id = gateway.create_room(args.name, args.invite)
            problems = manager.protect_room(room_id, args.name)
            if problems:
                print(f"[REFUSED] Created {room_id}, but it cannot be protected:")
                for problem in problems:
                    print(f"  - {problem}")
                return 1
            print(f"Created and protected {room_id}")
            if args.invite:
                print(f"Invited {len(args.invite)} account(s). They must accept in their client.")
            return 0
        if args.action == "unprotect":
            changed = manager.unprotect_room(args.room_id)
            print(
                "Stopped protecting the room." if changed else "That room is not in the registry."
            )
            return 0 if changed else 1
        if args.action == "restore":
            # Record the restore here, then let the running bot send the invitations, so all
            # membership changes for the account come from one process.
            refused = manager.start_admin_restore(args.user_id)
            if refused:
                print(refused)
                return 0
            described = ask_bot_to_drive(config, args.user_id)
            print(
                described
                or "Restore recorded. The Safe Mode bot sends the invitations within 5 minutes."
            )
            return 0
    except (GatewayError, SafeModeError) as exc:
        print(f"[ERROR] {exc}")
        return 1
    finally:
        store.close()
    return 1


def _doctor(manager, gateway, store) -> int:
    rooms = store.enabled_rooms()
    failures = 0
    print(f"Protected rooms: {len(rooms)}")
    for room in rooms:
        try:
            facts = gateway.room_facts(room.room_id)
        except GatewayError as exc:
            failures += 1
            print(f"  [FAIL] {room.name} ({room.room_id}): cannot read the room: {exc}")
            continue
        problems = compatibility_problems(facts, manager.bot_user_id)
        if problems:
            failures += 1
            print(f"  [FAIL] {room.name} ({room.room_id}): " + "; ".join(problems))
        else:
            print(f"  [ OK ] {room.name} ({room.room_id})")
    print(f"Problems: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
