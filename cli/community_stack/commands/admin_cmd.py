"""Server-side recovery for admin accounts, for when nobody can log in."""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

DEFAULT_DB = Path("data/announcements/announcements.db")


def run_admin(args: argparse.Namespace) -> int:
    from community_stack.admin.store import AdminStore

    db_path = Path(args.db or DEFAULT_DB)
    if not db_path.is_file():
        print(f"[ERROR] No admin database at {db_path}. Run this from the stack directory.")
        return 1
    # A root-owned SQLite journal would leave the database unwritable for the console.
    if os.geteuid() == 0 and db_path.stat().st_uid != 0:
        print("[ERROR] Run this as the user that owns the database, not root, for example:")
        print(f"        sudo -u '#{db_path.stat().st_uid}' .venv/bin/community-stack admin ...")
        return 1
    store = AdminStore(db_path)
    action = getattr(args, "admin_action", None)

    if action == "list":
        accounts = store.list_accounts()
        print(f"{'USERNAME':<30} {'ROLE':<10} {'ACCESS':<9} TWO-STEP")
        for account in accounts:
            access = "disabled" if account["disabled_at"] else "active"
            two_step = "on" if account["totp_enabled_at"] else "off"
            print(f"{account['username']:<30} {account['role']:<10} {access:<9} {two_step}")
        return 0

    account = store.get_account(args.username) if getattr(args, "username", None) else None
    if action in ("reset-password", "reset-two-step") and account is None:
        print(f"[ERROR] No account named '{args.username}'. See: community-stack admin list")
        return 1

    if action == "reset-password":
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = secrets.token_urlsafe(18)
        try:
            store.set_password(account["username"], password)
        except ValueError as exc:
            print(f"[ERROR] {exc}")
            return 1
        if account["disabled_at"]:
            store.set_disabled(account["username"], False)
        if args.disable_two_step:
            store.disable_totp(account["username"])
        store.record_event(
            "server command line",
            "account.password_reset",
            account["username"],
            {"two_step_turned_off": bool(args.disable_two_step)},
        )
        print(f"Password reset for '{account['username']}'; all their sessions were signed out.")
        if not args.password_stdin:
            print(f"Temporary password (shown once): {password}")
            print("Log in and change it on the My account page.")
        return 0

    if action == "reset-two-step":
        store.disable_totp(account["username"])
        store.record_event("server command line", "two_step.reset", account["username"])
        print(f"Two-step login turned off for '{account['username']}'.")
        return 0

    print("Usage: community-stack admin {list,reset-password,reset-two-step}")
    return 2
