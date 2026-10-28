"""Command line interface entry point for community-stack."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from community_stack import __version__
from community_stack.commands.check_cmd import run_check
from community_stack.commands.init_cmd import run_init
from community_stack.commands.plan_cmd import run_plan
from community_stack.commands.stubs import (
    run_backup,
    run_backup_verify,
    run_doctor,
    run_install,
    run_logs,
    run_restore,
    run_status,
    run_upgrade,
)


def create_parser() -> argparse.ArgumentParser:
    """Build the argument parser for community-stack CLI."""
    parser = argparse.ArgumentParser(
        prog="community-stack",
        description="Resilient Community Communications Stack management CLI.",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # init
    p_init = subparsers.add_parser("init", help="Initialize local configuration and directories")
    p_init.add_argument("--dir", default=".", help="Target directory (default: current)")
    p_init.add_argument(
        "--force", action="store_true", help="Overwrite existing configuration and secrets"
    )
    p_init.add_argument("--example", action="store_true", help="Use repository example config")

    # check
    p_check = subparsers.add_parser(
        "check", help="Validate configuration, secrets, and dependencies"
    )
    p_check.add_argument(
        "-c", "--config", default="community-stack.yaml", help="Path to config file"
    )
    p_check.add_argument("-e", "--env", default=".env", help="Path to secrets .env file")
    p_check.add_argument("--skip-dns", action="store_true", help="Skip public DNS verification")

    # plan
    p_plan = subparsers.add_parser("plan", help="Dry-run preview of intended deployment changes")
    p_plan.add_argument(
        "-c", "--config", default="community-stack.yaml", help="Path to config file"
    )
    p_plan.add_argument("-e", "--env", default=".env", help="Path to secrets .env file")

    # install
    p_install = subparsers.add_parser("install", help="Deploy enabled services to the host")
    p_install.add_argument(
        "-c", "--config", default="community-stack.yaml", help="Path to config file"
    )
    p_install.add_argument("-e", "--env", default=".env", help="Path to secrets .env file")

    # status
    subparsers.add_parser("status", help="Report service health and version information")

    # logs
    p_logs = subparsers.add_parser("logs", help="View or stream service logs")
    p_logs.add_argument("service", help="Name of service (e.g. matrix, element, landing, caddy)")
    p_logs.add_argument("-f", "--follow", action="store_true", help="Follow log output")
    p_logs.add_argument("-n", "--tail", type=int, default=100, help="Number of lines to show")

    # backup
    p_backup = subparsers.add_parser("backup", help="Manage encrypted backups")
    p_backup.add_argument(
        "action", nargs="?", default="create", choices=["create", "verify"], help="Backup action"
    )
    p_backup.add_argument(
        "-c", "--config", default="community-stack.yaml", help="Path to config file"
    )

    # upgrade
    p_upgrade = subparsers.add_parser("upgrade", help="Upgrade an individual service")
    p_upgrade.add_argument("service", help="Name of service to upgrade")

    # restore
    p_restore = subparsers.add_parser("restore", help="Restore backup to isolated directory")
    p_restore.add_argument("--target", required=True, help="Target directory for restored data")
    p_restore.add_argument(
        "--confirm", action="store_true", help="Explicit confirmation to proceed"
    )

    # doctor
    p_doctor = subparsers.add_parser(
        "doctor", help="Run non-destructive diagnostics with secret redaction"
    )
    p_doctor.add_argument(
        "-c", "--config", default="community-stack.yaml", help="Path to config file"
    )
    p_doctor.add_argument("-e", "--env", default=".env", help="Path to secrets .env file")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = create_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "init":
        return run_init(dest_dir=args.dir, force=args.force, example=args.example)
    elif args.command == "check":
        return run_check(config_path=args.config, env_path=args.env, skip_dns=args.skip_dns)
    elif args.command == "plan":
        return run_plan(config_path=args.config, env_path=args.env)
    elif args.command == "install":
        return run_install(config_path=args.config, env_path=args.env)
    elif args.command == "status":
        return run_status()
    elif args.command == "logs":
        return run_logs(service=args.service, follow=args.follow, tail=args.tail)
    elif args.command == "backup":
        if args.action == "verify":
            return run_backup_verify()
        return run_backup(config_path=args.config)
    elif args.command == "upgrade":
        return run_upgrade(service=args.service)
    elif args.command == "restore":
        return run_restore(target=args.target, confirm=args.confirm)
    elif args.command == "doctor":
        return run_doctor(config_path=args.config, env_path=args.env)
    else:
        parser.print_help()
        return 2


if __name__ == "__main__":
    sys.exit(main())
