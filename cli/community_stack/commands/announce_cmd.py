"""Implementation of the 'announce' CLI command and subcommands."""

from __future__ import annotations

import argparse
from pathlib import Path

from community_stack.announcements.models import ModelValidationError
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage


def run_announce(
    args: argparse.Namespace,
    config_path: Path | str = "community-stack.yaml",
) -> int:
    """Entry point for the announce command."""
    storage = AnnouncementStorage()
    publisher = AnnouncementPublisher(storage=storage, config_path=config_path)

    action = getattr(args, "announce_action", None)
    if not action or action == "list":
        announcements = storage.list_announcements(limit=getattr(args, "limit", 20))
        if not announcements:
            print("No public announcements found in local storage.")
            print(
                "To publish an announcement: community-stack announce create --title '...' --body '...'"
            )
            return 0

        print(f"{'ID':<26} {'DATE':<12} {'STATUS':<10} {'TITLE'}")
        print("-" * 80)
        for a in announcements:
            print(f"{a.id:<26} {a.published_at[:10]:<12} {a.status.value:<10} {a.title[:30]}")
        return 0

    elif action == "create":
        title = getattr(args, "title", None)
        body = getattr(args, "body", None)
        author = getattr(args, "author", None)
        raw_tags = getattr(args, "tags", "") or ""
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

        if not title or not body:
            print("[ERROR] Both --title and --body are required to publish an announcement.")
            return 1

        try:
            ann, results = publisher.publish(title=title, body=body, author=author, tags=tags)
            failures = sum(not result.success for result in results.values())
            if failures:
                print("  [PARTIAL] Announcement saved; one or more channel deliveries failed.")
            elif results:
                print("  [SUCCESS] Announcement created and dispatched!")
            else:
                print("  [PARTIAL] Announcement saved, but no publishing destinations are enabled.")
            print(f"            ID:           {ann.id}")
            print(f"            Title:        {ann.title}")
            print(f"            Published At: {ann.published_at}")
            print("\n  Channel Delivery Results:")
            for adapter_name, res in results.items():
                status_glyph = "[OK]  " if res.success else "[FAIL]"
                detail = res.external_id or res.error or "completed"
                print(f"    {status_glyph} {adapter_name:<14} -> {detail}")
            return 1 if failures or not results else 0
        except ModelValidationError as exc:
            print(f"[ERROR] Validation failed: {exc}")
            return 1
        except ValueError as exc:
            print(f"[ERROR] {exc}")
            return 1

    elif action == "draft":
        title = getattr(args, "title", None)
        body = getattr(args, "body", None)
        author = getattr(args, "author", None)
        raw_tags = getattr(args, "tags", "") or ""
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

        try:
            ann = publisher.create_draft(title=title, body=body, author=author, tags=tags)
            print(f"Draft saved. Preview it with: community-stack announce preview {ann.id}")
            print(f"ID: {ann.id}")
            return 0
        except ModelValidationError as exc:
            print(f"[ERROR] Validation failed: {exc}")
            return 1

    elif action == "preview":
        ann_id = getattr(args, "id", None)
        try:
            ann = publisher.preview(ann_id)
        except KeyError as exc:
            print(f"[ERROR] {exc}")
            return 1

        print(f"PREVIEW ({ann.status.value}): {ann.title}")
        if ann.author:
            print(f"By: {ann.author}")
        print()
        print(ann.body)
        return 0

    elif action == "publish":
        ann_id = getattr(args, "id", None)
        try:
            ann, results = publisher.publish_draft(ann_id)
            failed = sum(not result.success for result in results.values())
            print(
                f"Announcement '{ann.id}' published to {len(results) - failed}/{len(results)} channels."
            )
            for adapter_name, result in results.items():
                outcome = "OK" if result.success else f"FAILED: {result.error}"
                print(f"  - {adapter_name}: {outcome}")
            return 1 if failed else 0
        except (KeyError, ValueError) as exc:
            print(f"[ERROR] {exc}")
            return 1
        except ModelValidationError as exc:
            print(f"[ERROR] Validation failed: {exc}")
            return 1

    elif action == "show":
        ann_id = getattr(args, "id", None)
        if not ann_id:
            print("[ERROR] Please provide announcement ID to show.")
            return 1
        ann = storage.get_announcement(ann_id)
        if not ann:
            print(f"[ERROR] Announcement '{ann_id}' not found.")
            return 1

        print("================================================================================")
        print(f"ANNOUNCEMENT: {ann.title}")
        print("================================================================================")
        print(f"ID:           {ann.id}")
        print(f"Status:       {ann.status.value}")
        print(f"Published At: {ann.published_at}")
        if ann.author:
            print(f"Author:       {ann.author}")
        if ann.tags:
            print(f"Tags:         {', '.join(ann.tags)}")
        print("\n--- CONTENT ---\n")
        print(ann.body)
        print("\n--- ADAPTER DELIVERY STATE ---")
        states = storage.get_delivery_states(ann.id)
        for s in states:
            print(
                f"  - {s['adapter_name']:<14}: {s['status']} (attempts: {s['attempts']}, last: {s['last_attempt_at']})"
            )
        return 0

    elif action == "update":
        ann_id = getattr(args, "id", None)
        title = getattr(args, "title", None)
        body = getattr(args, "body", None)
        author = getattr(args, "author", None)

        if not ann_id:
            print("[ERROR] Please provide announcement ID to update.")
            return 1

        try:
            ann, results = publisher.update(ann_id, title=title, body=body, author=author)
            failures = sum(not result.success for result in results.values())
            print(f"  [{'PARTIAL' if failures else 'SUCCESS'}] Announcement '{ann.id}' updated.")
            for adapter_name, res in results.items():
                status_glyph = "[OK]  " if res.success else "[FAIL]"
                print(
                    f"    {status_glyph} {adapter_name:<14} -> {res.external_id or res.error or 'ok'}"
                )
            return 1 if failures else 0
        except KeyError as exc:
            print(f"[ERROR] {exc}")
            return 1
        except ValueError as exc:
            print(f"[ERROR] {exc}")
            return 1
        except ModelValidationError as exc:
            print(f"[ERROR] {exc}")
            return 1

    elif action == "withdraw":
        ann_id = getattr(args, "id", None)
        if not ann_id:
            print("[ERROR] Please provide announcement ID to withdraw.")
            return 1

        try:
            ann, results = publisher.withdraw(ann_id)
            failures = sum(not result.success for result in results.values())
            print(
                f"  [{'PARTIAL' if failures else 'SUCCESS'}] "
                f"Announcement '{ann.id}' marked WITHDRAWN."
            )
            for adapter_name, res in results.items():
                print(f"    - {adapter_name:<14}: {'Withdrawn' if res.success else res.error}")
            return 1 if failures else 0
        except KeyError as exc:
            print(f"[ERROR] {exc}")
            return 1
        except ValueError as exc:
            print(f"[ERROR] {exc}")
            return 1

    elif action == "retry":
        ann_id = getattr(args, "id", None)
        results = publisher.retry_failed(announcement_id=ann_id)
        if not results:
            if storage.get_failed_deliveries():
                print(
                    "Failed deliveries remain, but none are safely retryable with the "
                    "currently configured adapters."
                )
                return 1
            print("No failed deliveries to retry.")
            return 0

        failed = sum(not result.success for result in results.values())
        print(f"Retried {len(results)} failed deliveries; {failed} still failing:")
        for key, res in results.items():
            print(f"  - {key}: {'Success' if res.success else res.error}")
        return 1 if failed else 0

    else:
        print(f"Unknown announce action: {action}")
        return 2
