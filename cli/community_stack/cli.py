"""Command line interface entry point for community-stack."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from community_stack import __version__
from community_stack.commands.access_cmd import run_access
from community_stack.commands.announce_cmd import run_announce
from community_stack.commands.archive_cmd import run_archive
from community_stack.commands.backup_cmd import run_backup, run_backup_verify
from community_stack.commands.bridge_cmd import run_bridge
from community_stack.commands.check_cmd import run_check
from community_stack.commands.connect_cmd import run_connect
from community_stack.commands.continuity_cmd import run_continuity
from community_stack.commands.doctor_cmd import run_doctor
from community_stack.commands.init_cmd import run_init
from community_stack.commands.install_cmd import run_install
from community_stack.commands.logs_cmd import run_logs
from community_stack.commands.plan_cmd import run_plan
from community_stack.commands.restore_cmd import run_restore
from community_stack.commands.service_cmd import run_service
from community_stack.commands.status_cmd import run_status
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
    p_init.add_argument("--project-name", help="Short identifier, e.g. ourtown")
    p_init.add_argument("--base-domain", help="Community's main domain, e.g. ourtown.org")
    p_init.add_argument("--web-domain", help="Domain for the website and console")
    p_init.add_argument("--matrix-domain", help="Domain for the Matrix chat server")
    p_init.add_argument("--timezone", help="IANA time zone, e.g. Europe/Paris")
    p_init.add_argument("--acme-email", help="Email for HTTPS certificate notices")
    p_init.add_argument("--console-uid", type=int, help="User ID the console container runs as")
    p_init.add_argument("--console-gid", type=int, help="Group ID the console container runs as")
    p_init.add_argument(
        "--setup-token", action="store_true", help="Generate a first-administrator setup token"
    )

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
    p_status = subparsers.add_parser("status", help="Report service health and version information")
    p_status.add_argument(
        "-c", "--config", default="community-stack.yaml", help="Path to config file"
    )

    # service lifecycle
    p_service = subparsers.add_parser(
        "service", help="Enable, disable, start, stop, restart, or inspect a service"
    )
    p_service.add_argument(
        "action", choices=["enable", "disable", "start", "stop", "restart", "status"]
    )
    p_service.add_argument("service", help="Service name (for example vikunja or facilmap)")
    p_service.add_argument(
        "-c", "--config", default="community-stack.yaml", help="Path to config file"
    )
    p_service.add_argument("-e", "--env", default=".env", help="Path to secrets .env file")

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

    p_ann_deliveries = announce_subs.add_parser(
        "deliveries", help="List channel publication states and attempt history"
    )
    p_ann_deliveries.add_argument("id", help="Announcement ID")

    p_ann_outcome = announce_subs.add_parser(
        "outcome", help="Record a manual publication, update, or withdrawal result"
    )
    p_ann_outcome.add_argument("id", help="Announcement ID")
    p_ann_outcome.add_argument("channel", help="Platform identifier, e.g. instagram")
    p_ann_outcome.add_argument(
        "outcome",
        choices=["published", "updated", "withdrawn", "failed", "unknown"],
        help="What happened to the manual post",
    )
    p_ann_outcome.add_argument("--actor", required=True, help="Name of the person recording it")
    p_ann_outcome.add_argument("--post-url", help="HTTPS URL for a published or updated post")
    p_ann_outcome.add_argument("--note", help="Optional explanation for a failed or unknown result")

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

    # admin recovery
    p_admin = subparsers.add_parser(
        "admin", help="Recover admin console accounts from the server (when nobody can log in)"
    )
    p_admin.add_argument(
        "--db", help="Admin database (default data/announcements/announcements.db)"
    )
    admin_subs = p_admin.add_subparsers(dest="admin_action", help="Recovery actions")
    admin_subs.add_parser("list", help="List console accounts")
    p_admin_reset = admin_subs.add_parser(
        "reset-password", help="Give an account a new temporary password"
    )
    p_admin_reset.add_argument("username", help="Account to reset")
    p_admin_reset.add_argument(
        "--disable-two-step", action="store_true", help="Also turn off two-step login"
    )
    p_admin_reset.add_argument(
        "--password-stdin", action="store_true", help="Read the new password from standard input"
    )
    p_admin_2fa = admin_subs.add_parser(
        "reset-two-step", help="Turn off an account's two-step login"
    )
    p_admin_2fa.add_argument("username", help="Account to reset")

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

    bridge_subs.add_parser("status", help="Show what is installed, running and registered")
    bridge_subs.add_parser("doctor", help="Check each prerequisite and say how to fix it")
    bridge_subs.add_parser("rooms", help="List Matrix rooms the bridge created for WhatsApp chats")
    p_b_logs = bridge_subs.add_parser("logs", help="Show the bridge container's recent logs")
    p_b_logs.add_argument("--lines", type=int, default=100, help="How many lines to show")
    p_b_disable = bridge_subs.add_parser(
        "disable", help="Emergency killswitch: stop the bridge, keep its data and links"
    )
    p_b_disable.add_argument("--confirm", action="store_true", help="Explicit confirmation")

    # access (Safe Mode)
    p_access = subparsers.add_parser(
        "access", help="Safe Mode: protected rooms, enrolled accounts and recovery codes"
    )
    p_access.add_argument("--dir", default=".", help="Project directory (default: current)")
    access_subs = p_access.add_subparsers(dest="access_action", help="Access actions")

    access_subs.add_parser("rooms", help="List protected rooms")
    p_a_protect = access_subs.add_parser(
        "protect", help="Protect a room if it is compatible (runs in the Safe Mode container)"
    )
    p_a_protect.add_argument("room_id")
    p_a_protect.add_argument("--name", required=True, help="Human-readable room name")
    p_a_unprotect = access_subs.add_parser("unprotect", help="Stop protecting a room")
    p_a_unprotect.add_argument("room_id")
    p_a_enroll = access_subs.add_parser("enroll", help="Enroll an account in Safe Mode")
    p_a_enroll.add_argument("user_id")
    p_a_code = access_subs.add_parser(
        "recovery-code", help="Issue a one-time recovery code (shown once)"
    )
    p_a_code.add_argument("user_id")
    p_a_status = access_subs.add_parser("status", help="Show one account's Safe Mode state")
    p_a_status.add_argument("user_id")
    p_a_restore = access_subs.add_parser(
        "restore", help="Administrative restore, without the member's recovery code"
    )
    p_a_restore.add_argument("user_id")
    p_a_restore.add_argument("--confirm", action="store_true", help="Explicit confirmation")
    access_subs.add_parser("doctor", help="Check every protected room against the rules")
    access_subs.add_parser("bot", help="Show whether the Safe Mode bot container is running")
    p_a_logs = access_subs.add_parser("logs", help="Show the Safe Mode bot's recent logs")
    p_a_logs.add_argument("--lines", type=int, default=200)

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
    p_s_add.add_argument(
        "--consent-source",
        help="Where and how the person agreed, e.g. 'paper_form' or 'event_signup'. "
        "Required for email.",
    )

    p_s_unsub = subs_subparsers.add_parser("unsubscribe", help="Unsubscribe a contact")
    p_s_unsub.add_argument(
        "--channel", required=True, choices=["email", "sms"], help="Channel type"
    )
    p_s_unsub.add_argument("--dest", required=True, help="Email address or phone number")

    p_s_export = subs_subparsers.add_parser(
        "export", help="Export every record held about subscribers, without secrets"
    )
    p_s_export.add_argument(
        "-o", "--output", help="Target JSON file (default: backups/subscribers-export.json)"
    )

    p_s_test = subs_subparsers.add_parser(
        "test-email", help="Send one test email to an address you choose, to check delivery"
    )
    p_s_test.add_argument("--to", required=True, help="Address to send the test to")
    p_s_test.add_argument(
        "-c", "--config", default="community-stack.yaml", help="Path to config file"
    )

    p_s_delete = subs_subparsers.add_parser(
        "delete", help="Erase everything held about one address (permanent)"
    )
    p_s_delete.add_argument("--channel", required=True, choices=["email", "sms"])
    p_s_delete.add_argument("--dest", required=True, help="Email address or phone number")
    p_s_delete.add_argument("--confirm", action="store_true", help="Required to erase the record")

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

    # connect
    p_connect = subparsers.add_parser(
        "connect", help="Show the community's stable /connect page, QR codes and identity package"
    )
    p_connect.add_argument(
        "-c", "--config", default="community-stack.yaml", help="Path to config file"
    )
    connect_subs = p_connect.add_subparsers(dest="connect_action", help="Connect actions")
    connect_subs.add_parser("show", help="Show the identity and which channels are listed")
    p_con_qr = connect_subs.add_parser("generate-qr", help="Write printable QR codes (SVG and PNG)")
    p_con_qr.add_argument(
        "--format", choices=("svg", "png", "both"), default="both", help="Image format"
    )
    p_con_qr.add_argument(
        "--destination", help="Address the QR code opens (default: the /connect page)"
    )
    p_con_qr.add_argument("--output-dir", help="Output folder (default: backups/connect-qr)")
    p_con_export = connect_subs.add_parser(
        "export", help="Export the identity package as a checksummed zip"
    )
    p_con_export.add_argument(
        "-o", "--output", help="Target path (default: backups/community-identity.zip)"
    )
    p_con_render = connect_subs.add_parser("render", help="Rebuild the live /connect page now")
    p_con_render.add_argument(
        "--output-dir", help="Folder served as /connect (default: data/landing/connect)"
    )

    # continuity
    p_continuity = subparsers.add_parser(
        "continuity", help="Manage outage notices, channel states and offline guides"
    )
    continuity_subs = p_continuity.add_subparsers(
        dest="continuity_action", help="Continuity actions"
    )
    continuity_subs.add_parser("status", help="Show continuity mode, channel states and history")

    p_cont_plan = continuity_subs.add_parser("plan", help="Preview a continuity activation")
    p_cont_plan.add_argument("--actor", required=True, help="Responsible administrator")
    p_cont_plan.add_argument("--reason", required=True, help="Why continuity mode is needed")
    p_cont_plan.add_argument(
        "--affected", action="append", required=True, help="Affected channel (repeatable)"
    )
    p_cont_plan.add_argument("--translations", help="JSON file with translated migration templates")
    p_cont_plan.add_argument(
        "-c", "--config", default="community-stack.yaml", help="Path to config file"
    )

    p_cont_activate = continuity_subs.add_parser(
        "activate", help="Activate or resume continuity mode"
    )
    p_cont_activate.add_argument("--actor", required=True, help="Responsible administrator")
    p_cont_activate.add_argument("--reason", required=True, help="Why continuity mode is needed")
    p_cont_activate.add_argument(
        "--affected", action="append", required=True, help="Affected channel (repeatable)"
    )
    p_cont_activate.add_argument(
        "--translations", help="JSON file with translated migration templates"
    )
    p_cont_activate.add_argument(
        "-c", "--config", default="community-stack.yaml", help="Path to config file"
    )

    p_cont_deactivate = continuity_subs.add_parser(
        "deactivate", help="Remove the active continuity notice"
    )
    p_cont_deactivate.add_argument("--actor", required=True, help="Responsible administrator")
    p_cont_deactivate.add_argument("--reason", required=True, help="Why continuity mode ended")
    p_cont_deactivate.add_argument(
        "-c", "--config", default="community-stack.yaml", help="Path to config file"
    )

    p_cont_channel = continuity_subs.add_parser(
        "channel", help="Manually record one channel's operational state"
    )
    p_cont_channel.add_argument("channel", help="Channel identifier")
    p_cont_channel.add_argument(
        "status", choices=["operational", "degraded", "unavailable", "retired"]
    )
    p_cont_channel.add_argument("--actor", required=True, help="Responsible administrator")
    p_cont_channel.add_argument("--reason", required=True, help="Reason for the state")
    p_cont_channel.add_argument(
        "-c", "--config", default="community-stack.yaml", help="Path to config file"
    )

    p_cont_package = continuity_subs.add_parser(
        "package", help="Build and verify the public offline contact guide"
    )
    p_cont_package.add_argument("-o", "--output", help="Output zip (default: public guide path)")
    p_cont_package.add_argument("--actor", default="local operator", help="Responsible operator")
    p_cont_package.add_argument(
        "-c", "--config", default="community-stack.yaml", help="Path to config file"
    )

    for action, description in (
        ("check", "Check continuity readiness; exit nonzero when findings exist"),
        ("report", "Print aggregate continuity metrics and recommendations"),
    ):
        p_readiness = continuity_subs.add_parser(action, help=description)
        p_readiness.add_argument("--actor", default="local operator", help="Responsible operator")
        p_readiness.add_argument(
            "-c", "--config", default="community-stack.yaml", help="Path to config file"
        )

    p_drill = continuity_subs.add_parser("drill", help="Run and review a simulated outage drill")
    drill_subs = p_drill.add_subparsers(dest="continuity_drill_action", help="Drill actions")
    p_drill_start = drill_subs.add_parser(
        "start", help="Publish a clearly labeled simulation to independent channels"
    )
    p_drill_start.add_argument("--actor", required=True, help="Responsible administrator")
    p_drill_start.add_argument("--scenario", required=True, help="Short simulated outage scenario")
    p_drill_start.add_argument(
        "--channel", action="append", required=True, help="Independent channel key (repeatable)"
    )
    p_drill_start.add_argument(
        "--confirm", action="store_true", help="Confirm that the simulation will be publicly sent"
    )
    p_drill_start.add_argument(
        "-c", "--config", default="community-stack.yaml", help="Path to config file"
    )

    p_drill_review = drill_subs.add_parser(
        "review", help="Record whether members could access the drill message"
    )
    p_drill_review.add_argument("exercise_id", help="Exercise identifier returned by start")
    p_drill_review.add_argument("--actor", required=True, help="Administrator recording results")
    p_drill_review.add_argument(
        "--access-verified", choices=("yes", "no"), required=True, help="Was access verified?"
    )
    p_drill_review.add_argument(
        "--problems", default="", help="Brief description of problems or follow-up needed"
    )

    p_drill_report = drill_subs.add_parser("report", help="Show a drill and its recorded results")
    p_drill_report.add_argument("exercise_id", help="Exercise identifier")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = create_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "init":
        return run_init(
            dest_dir=args.dir,
            force=args.force,
            example=args.example,
            project_name=args.project_name,
            base_domain=args.base_domain,
            web_domain=args.web_domain,
            matrix_domain=args.matrix_domain,
            timezone=args.timezone,
            acme_email=args.acme_email,
            console_uid=args.console_uid,
            console_gid=args.console_gid,
            setup_token=args.setup_token,
        )
    elif args.command == "check":
        return run_check(config_path=args.config, env_path=args.env, skip_dns=args.skip_dns)
    elif args.command == "plan":
        return run_plan(config_path=args.config, env_path=args.env)
    elif args.command == "install":
        return run_install(config_path=args.config, env_path=args.env)
    elif args.command == "status":
        return run_status(config_path=args.config)
    elif args.command == "service":
        return run_service(
            action=args.action,
            service=args.service,
            config_path=args.config,
            env_path=args.env,
        )
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
    elif args.command == "admin":
        from community_stack.commands.admin_cmd import run_admin

        return run_admin(args)
    elif args.command == "agent":
        from community_stack.commands.agent_cmd import run_agent

        return run_agent(args)
    elif args.command == "bridge":
        return run_bridge(args=args)
    elif args.command == "access":
        return run_access(args=args)
    elif args.command == "subscribers":
        return run_subscribers(args=args)
    elif args.command == "archive":
        return run_archive(args=args)
    elif args.command == "connect":
        return run_connect(args=args)
    elif args.command == "continuity":
        return run_continuity(args=args)
    else:
        parser.print_help()
        return 2


if __name__ == "__main__":
    sys.exit(main())
