"""Command line interface entry point for community-stack."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from community_stack import __version__
from community_stack.commands.announce_cmd import run_announce
from community_stack.commands.archive_cmd import run_archive
from community_stack.commands.backup_cmd import run_backup, run_backup_verify
from community_stack.commands.bridge_cmd import run_bridge
from community_stack.commands.check_cmd import run_check
from community_stack.commands.init_cmd import run_init
from community_stack.commands.install_cmd import run_install
from community_stack.commands.logs_cmd import run_logs
from community_stack.commands.plan_cmd import run_plan
from community_stack.commands.restore_cmd import run_restore
from community_stack.commands.status_cmd import run_status
from community_stack.commands.stubs import run_doctor
from community_stack.commands.subscribers_cmd import run_subscribers
from community_stack.commands.upgrade_cmd import run_upgrade


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
    p_backup = subparsers.add_parser("backup", help="Manage backup snapshots")
    p_backup.add_argument(
        "action", nargs="?", default="create", choices=["create", "verify"], help="Backup action"
    )
    p_backup.add_argument(
        "-c", "--config", default="community-stack.yaml", help="Path to config file"
    )
    p_backup.add_argument("-e", "--env", default=".env", help="Path to secrets .env file")
    p_backup.add_argument("--snapshot", default=None, help="Specific snapshot ID to verify")

    # upgrade
    p_upgrade = subparsers.add_parser("upgrade", help="Upgrade an individual service")
    p_upgrade.add_argument("service", help="Name of service to upgrade")
    p_upgrade.add_argument(
        "-c", "--config", default="community-stack.yaml", help="Path to config file"
    )
    p_upgrade.add_argument("-e", "--env", default=".env", help="Path to secrets .env file")

    # restore
    p_restore = subparsers.add_parser("restore", help="Restore backup to isolated directory")
    p_restore.add_argument("--target", required=True, help="Target directory for restored data")
    p_restore.add_argument(
        "-c", "--config", default="community-stack.yaml", help="Path to config file"
    )
    p_restore.add_argument("-e", "--env", default=".env", help="Path to secrets .env file")
    p_restore.add_argument("--snapshot", default=None, help="Specific snapshot ID to restore")
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

    # announce
    p_announce = subparsers.add_parser(
        "announce", help="Manage and publish canonical community announcements"
    )
    announce_subs = p_announce.add_subparsers(dest="announce_action", help="Announcement actions")

    p_ann_list = announce_subs.add_parser("list", help="List recent announcements")
    p_ann_list.add_argument("--limit", type=int, default=20, help="Max items to list")

    p_ann_create = announce_subs.add_parser("create", help="Publish a new announcement")
    p_ann_create.add_argument("-t", "--title", required=True, help="Announcement title")
    p_ann_create.add_argument("-b", "--body", required=True, help="Announcement body content")
    p_ann_create.add_argument("-a", "--author", help="Author name or organization")
    p_ann_create.add_argument("--tags", help="Comma-separated tags")

    p_ann_draft = announce_subs.add_parser("draft", help="Save a draft without publishing")
    p_ann_draft.add_argument("-t", "--title", required=True, help="Announcement title")
    p_ann_draft.add_argument("-b", "--body", required=True, help="Announcement body content")
    p_ann_draft.add_argument("-a", "--author", help="Author name or organization")
    p_ann_draft.add_argument("--tags", help="Comma-separated tags")

    p_ann_preview = announce_subs.add_parser("preview", help="Preview a saved announcement")
    p_ann_preview.add_argument("id", help="Announcement ID")

    p_ann_publish = announce_subs.add_parser("publish", help="Publish a saved draft")
    p_ann_publish.add_argument("id", help="Draft announcement ID")

    p_ann_schedule = announce_subs.add_parser("schedule", help="Schedule a draft")
    p_ann_schedule.add_argument("id", help="Draft announcement ID")
    p_ann_schedule.add_argument(
        "--at", required=True, help="Local time in the community time zone, YYYY-MM-DD HH:MM"
    )

    p_ann_unschedule = announce_subs.add_parser("unschedule", help="Turn back into a draft")
    p_ann_unschedule.add_argument("id", help="Scheduled announcement ID")

    p_ann_show = announce_subs.add_parser("show", help="Show announcement details")
    p_ann_show.add_argument("id", help="Announcement ID")

    p_ann_update = announce_subs.add_parser("update", help="Update an announcement")
    p_ann_update.add_argument("id", help="Announcement ID to update")
    p_ann_update.add_argument("-t", "--title", help="New title")
    p_ann_update.add_argument("-b", "--body", help="New body content")
    p_ann_update.add_argument("-a", "--author", help="New author name")

    p_ann_withdraw = announce_subs.add_parser("withdraw", help="Withdraw an announcement")
    p_ann_withdraw.add_argument("id", help="Announcement ID to withdraw")

    p_ann_retry = announce_subs.add_parser("retry", help="Retry failed deliveries")
    p_ann_retry.add_argument("--id", help="Optional specific announcement ID to retry")

    p_ann_worker = announce_subs.add_parser(
        "worker", help="Run scheduled deliveries and automatic retries"
    )
    p_ann_worker.add_argument("--once", action="store_true", help="Run due jobs once and exit")
    p_ann_worker.add_argument(
        "--interval", type=float, default=15.0, help="Seconds between runs (default 15)"
    )

    # agent
    p_agent = subparsers.add_parser(
        "agent", help="Run the privileged control agent used by the admin console"
    )
    agent_subs = p_agent.add_subparsers(dest="agent_action", help="Agent actions")
    p_agent_serve = agent_subs.add_parser("serve", help="Listen for console requests")
    p_agent_serve.add_argument("--project-dir", default=".", help="Stack directory")
    p_agent_serve.add_argument("--socket", help="Socket path (default data/agent/run/agent.sock)")
    p_agent_serve.add_argument(
        "--console-uid",
        type=int,
        action="append",
        help="UID allowed to connect (repeatable; default: any process that can open the socket)",
    )
    p_agent_serve.add_argument(
        "--console-gid", type=int, help="Group that owns the socket so the console can reach it"
    )
    p_agent_serve.add_argument(
        "--no-scheduler", action="store_true", help="Do not run scheduled backups"
    )

    # bridge
    p_bridge = subparsers.add_parser("bridge", help="Manage optional WhatsApp bridge integration")
    bridge_subs = p_bridge.add_subparsers(dest="bridge_action", help="Bridge actions")

    bridge_subs.add_parser("status", help="Check bridge health and connection state")
    p_b_disable = bridge_subs.add_parser(
        "disable", help="Emergency killswitch to disconnect bridge"
    )
    p_b_disable.add_argument("--confirm", action="store_true", help="Explicit confirmation")
    bridge_subs.add_parser("list-rooms", help="List allowlisted Matrix rooms mapped to WhatsApp")

    # subscribers
    p_subs = subparsers.add_parser(
        "subscribers", help="Manage opt-in email and SMS broadcast subscriptions"
    )
    subs_subparsers = p_subs.add_subparsers(dest="subscriber_action", help="Subscriber actions")

    p_s_list = subs_subparsers.add_parser("list", help="List subscribers")
    p_s_list.add_argument("--channel", choices=["email", "sms"], help="Filter by channel")
    p_s_list.add_argument("--all", action="store_true", help="Include unsubscribed contacts")

    p_s_add = subs_subparsers.add_parser("add", help="Add an opt-in subscriber")
    p_s_add.add_argument("--channel", required=True, choices=["email", "sms"], help="Channel type")
    p_s_add.add_argument("--dest", required=True, help="Email address or phone number")
    p_s_add.add_argument("--topics", help="Comma-separated topic subscriptions")

    p_s_unsub = subs_subparsers.add_parser("unsubscribe", help="Unsubscribe a contact")
    p_s_unsub.add_argument(
        "--channel", required=True, choices=["email", "sms"], help="Channel type"
    )
    p_s_unsub.add_argument("--dest", required=True, help="Email address or phone number")

    # archive
    p_archive = subparsers.add_parser(
        "archive", help="Export and verify offline public announcement archives"
    )
    archive_subs = p_archive.add_subparsers(dest="archive_action", help="Archive actions")

    p_arc_export = archive_subs.add_parser("export", help="Export self-contained offline archive")
    p_arc_export.add_argument(
        "-o",
        "--output",
        help="Target archive path (default: backups/offline_announcements.zip)",
    )

    p_arc_verify = archive_subs.add_parser("verify", help="Verify offline archive bundle")
    p_arc_verify.add_argument("path", help="Path to offline archive file")

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
            return run_backup_verify(
                config_path=args.config, env_path=args.env, snapshot_id=args.snapshot
            )
        return run_backup(config_path=args.config, env_path=args.env)
    elif args.command == "upgrade":
        return run_upgrade(service=args.service, config_path=args.config, env_path=args.env)
    elif args.command == "restore":
        return run_restore(
            target=args.target,
            config_path=args.config,
            env_path=args.env,
            snapshot_id=args.snapshot,
            confirm=args.confirm,
        )
    elif args.command == "doctor":
        return run_doctor(config_path=args.config, env_path=args.env)
    elif args.command == "announce":
        return run_announce(args=args)
    elif args.command == "agent":
        from community_stack.commands.agent_cmd import run_agent

        return run_agent(args)
    elif args.command == "bridge":
        return run_bridge(args=args)
    elif args.command == "subscribers":
        return run_subscribers(args=args)
    elif args.command == "archive":
        return run_archive(args=args)
    else:
        parser.print_help()
        return 2


if __name__ == "__main__":
    sys.exit(main())
