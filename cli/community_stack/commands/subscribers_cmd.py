"""Implementation of the 'subscribers' CLI command and subcommands."""

from __future__ import annotations

import argparse
import json
from email.message import EmailMessage
from pathlib import Path

from community_stack.subscriptions.email_signup import EMAIL_CONSENT_TEXT, EMAIL_CONSENT_VERSION
from community_stack.subscriptions.manager import (
    Subscriber,
    SubscriberManager,
    validate_destination,
)


def run_subscribers(args: argparse.Namespace) -> int:
    """Entry point for the subscribers command."""
    manager = SubscriberManager()
    action = getattr(args, "subscriber_action", None)

    if not action or action == "list":
        channel = getattr(args, "channel", None)
        active_only = not getattr(args, "all", False)
        subscribers = manager.list_subscribers(channel=channel, active_only=active_only)

        if not subscribers:
            print("No subscribers found matching criteria.")
            print("To add: community-stack subscribers add --channel email --dest user@example.org")
            return 0

        print(f"{'ID':<16} {'CHANNEL':<10} {'STATUS':<12} {'DESTINATION':<28} {'TOPICS'}")
        print("-" * 80)
        for s in subscribers:
            topics_str = ", ".join(s.topics) if s.topics else "all"
            print(
                f"{s.id:<16} {s.channel:<10} {s.status:<12} {s.mask_destination():<28} {topics_str}"
            )
        print(f"\nTotal: {len(subscribers)} subscriber(s)")
        return 0

    elif action == "add":
        channel = getattr(args, "channel", None)
        destination = getattr(args, "dest", None)
        raw_topics = getattr(args, "topics", "") or ""
        topics = [t.strip() for t in raw_topics.split(",") if t.strip()]

        if not channel or not destination:
            print("[ERROR] Both --channel and --dest are required.")
            return 1

        consent_source = getattr(args, "consent_source", None)
        if channel == "email" and not consent_source:
            print(
                "[ERROR] Email subscribers need --consent-source: say where and how they agreed, "
                "for example 'paper_form' or 'event_signup'."
            )
            return 1

        try:
            consent: dict[str, str] = {}
            if channel == "email":
                manager.register_consent_text(EMAIL_CONSENT_VERSION, EMAIL_CONSENT_TEXT)
                consent = {
                    "consent_version": EMAIL_CONSENT_VERSION,
                    "consent_source": consent_source,
                }
            sub = manager.add_subscriber(
                channel=channel, destination=destination, topics=topics, **consent
            )
            print("  [SUCCESS] Subscriber registered successfully!")
            print(f"            ID:          {sub.id}")
            print(f"            Channel:     {sub.channel}")
            print(f"            Destination: {sub.mask_destination()}")
            print(f"            Status:      {sub.status}")
            return 0
        except ValueError as exc:
            print(f"[ERROR] {exc}")
            return 1

    elif action == "unsubscribe":
        channel = getattr(args, "channel", None)
        destination = getattr(args, "dest", None)

        if not channel or not destination:
            print("[ERROR] Both --channel and --dest are required.")
            return 1

        unsub_ok = manager.unsubscribe(channel=channel, destination=destination)
        if unsub_ok:
            print(f"  [SUCCESS] Destination successfully unsubscribed from {channel} broadcasts.")
            return 0
        else:
            print(f"[WARN] No active subscription found for destination in channel '{channel}'.")
            return 0

    elif action == "test-email":
        return _test_email(args)

    elif action == "export":
        output = Path(getattr(args, "output", None) or "backups/subscribers-export.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        records = manager.export_records()
        output.write_text(
            json.dumps({"format": "community-stack/subscribers", "records": records}, indent=2),
            encoding="utf-8",
        )
        output.chmod(0o600)
        print(f"  [SUCCESS] Exported {len(records)} record(s) to {output.resolve()}")
        print(
            "            The file contains personal data. Share it only with the person concerned."
        )
        return 0

    elif action == "delete":
        channel = getattr(args, "channel", None)
        destination = getattr(args, "dest", None)
        if not getattr(args, "confirm", False):
            print("[ERROR] Deleting is permanent. Add --confirm to erase this record.")
            return 1
        if manager.delete_subscriber(channel=channel, destination=destination):
            print(f"  [SUCCESS] Erased everything held about this {channel} address.")
            return 0
        print(f"[WARN] No {channel} record was found for that destination.")
        return 0

    else:
        print(f"Unknown subscribers action: {action}")
        return 2


def _test_email(args: argparse.Namespace) -> int:
    """Send one test message through the connected mail server. Nobody is subscribed by this."""
    from community_stack.announcements.adapters.email_newsletter import EmailNewsletterAdapter
    from community_stack.announcements.publisher import AnnouncementPublisher
    from community_stack.announcements.storage import AnnouncementStorage
    from community_stack.config import ConfigError
    from community_stack.continuity.connect_page import load_identity
    from community_stack.subscriptions.email_transport import TransportError

    address = (getattr(args, "to", "") or "").strip()
    try:
        validate_destination("email", address)
    except ValueError as exc:
        print(f"[ERROR] {exc}")
        return 1

    config_path = getattr(args, "config", "community-stack.yaml")
    try:
        publisher = AnnouncementPublisher(storage=AnnouncementStorage(), config_path=config_path)
        profile, _res, _anns, _logo = load_identity(config_path, publisher.storage)
    except ConfigError as exc:
        print(f"[ERROR] {exc}")
        return 1
    adapter = next((a for a in publisher.adapters if isinstance(a, EmailNewsletterAdapter)), None)
    if adapter is None or adapter.unsubscribe_secret is None:
        print("[ERROR] No email server is connected. Connect one on the Channels page first.")
        return 1

    message = EmailMessage()
    message["Subject"] = f"Test message from {profile.name}"
    message["From"] = adapter.from_address
    message["To"] = address
    message["List-Unsubscribe"] = f"<{adapter.unsubscribe_url(address)}>"
    message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message.set_content(
        f"This is a test from {profile.name}. Nothing else was sent, and you have not been "
        "subscribed.\n\n"
        "If you can read this, email delivery is working. Check that it did not land in spam.\n\n"
        f"To stop any future emails: {adapter.unsubscribe_url(address)}\n"
    )
    try:
        adapter.transport.send(message)
    except TransportError as exc:
        print(f"[FAIL] The test was not sent: {exc}")
        return 1
    print(
        f"  [SUCCESS] Test email sent to {Subscriber(channel='email', destination=address).mask_destination()}."
    )
    print(
        "            Check the inbox and spam folder. Then check the message headers for SPF, DKIM and DMARC results."
    )
    return 0
