"""Implementation of the 'subscribers' CLI command and subcommands."""

from __future__ import annotations

import argparse

from community_stack.subscriptions.manager import SubscriberManager


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

        try:
            sub = manager.add_subscriber(channel=channel, destination=destination, topics=topics)
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

    else:
        print(f"Unknown subscribers action: {action}")
        return 2
