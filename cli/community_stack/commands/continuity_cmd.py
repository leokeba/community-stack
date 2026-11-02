"""CLI controls for channel states and continuity mode."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from community_stack.admin.store import AdminStore
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.continuity.offline_exporter import OfflineArchiveExporter
from community_stack.continuity.package import DEFAULT_GUIDE, build_continuity_package
from community_stack.continuity.readiness import build_readiness_report, format_readiness_report
from community_stack.continuity.service import ContinuityService
from community_stack.continuity.store import ContinuityStore


def _translations(path: str | None) -> dict[str, dict[str, str]] | None:
    if not path:
        return None
    try:
        data: Any = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read the translations file: {exc}") from exc
    if not isinstance(data, dict) or any(
        not isinstance(language, str) or not isinstance(item, dict)
        for language, item in data.items()
    ):
        raise ValueError("Translations must be a JSON object keyed by language.")
    return data


def _audit(
    storage: AnnouncementStorage,
    actor: str,
    action: str,
    target: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    AdminStore(storage.db_path).record_event(
        actor=actor,
        action=action,
        target=target,
        details=details,
        client="local-cli",
    )


def _print_steps(steps: dict[str, str]) -> bool:
    failed = False
    for step, status in steps.items():
        print(f"  {status.upper():<10} {step.replace('_', ' ')}")
        failed = failed or status == "failed"
    return failed


def run_continuity(args: argparse.Namespace) -> int:
    """Run a continuity CLI action."""
    action = getattr(args, "continuity_action", None) or "status"
    config_path = getattr(args, "config", "community-stack.yaml")
    storage = AnnouncementStorage()

    if action == "status":
        continuity = ContinuityStore(storage.db_path)
        current = continuity.get_current()
        _audit(
            storage,
            getattr(args, "actor", "local operator"),
            "continuity.status",
            current["event_id"] if current else None,
        )
        if current:
            print(f"Continuity mode: {'active' if current['active'] else 'inactive'}")
            print(f"Last event:      {current['event_id']}")
            print(f"Reason:          {current['reason']}")
            print(f"Affected:        {', '.join(current['affected_channels']) or '(none)'}")
        else:
            print("Continuity mode: inactive (never activated)")
        states = continuity.list_channel_states()
        print("\nChannel states:")
        for channel, state in states.items():
            print(f"  {channel}: {state['status']} — {state['reason']} ({state['actor']})")
        if not states:
            print("  No manually recorded channel states.")
        if current:
            print("\nRecent operation steps:")
            for step in continuity.list_steps(current["event_id"]):
                detail = f": {step['error']}" if step["error"] else ""
                print(f"  {step['status']}: {step['step']} (attempts: {step['attempts']}){detail}")
        return 0

    if action == "package":
        target = getattr(args, "output", None) or DEFAULT_GUIDE
        path = build_continuity_package(config_path, storage, target)
        valid, messages = OfflineArchiveExporter.verify_archive(path)
        if not valid:
            print("[ERROR] The continuity guide failed verification: " + "; ".join(messages))
            return 1
        _audit(storage, getattr(args, "actor", "local operator"), "continuity.package", str(path))
        print(f"Continuity guide written and verified at {path}.")
        return 0

    publisher = AnnouncementPublisher(storage=storage, config_path=config_path)
    service = ContinuityService(storage, publisher, config_path)

    try:
        if action in ("check", "report"):
            report = build_readiness_report(storage, publisher, config_path, service.guide_path)
            print(format_readiness_report(report))
            _audit(storage, getattr(args, "actor", "local operator"), f"continuity.{action}")
            return 1 if action == "check" and report["findings"] else 0
        if action == "plan":
            actor = args.actor
            plan = service.plan(args.reason, args.affected, _translations(args.translations))
            print(f"Continuity mode already active: {'yes' if plan['active'] else 'no'}")
            print(f"Affected channels: {', '.join(plan['affected_channels'])}")
            print(
                "Activation will mark affected channels unavailable, publish the notice, "
                "send migration messages only to unaffected enabled destinations, "
                "and rebuild the offline guide."
            )
            print(f"Unaffected destinations: {', '.join(plan['target_channels']) or '(none)'}")
            for language, message in plan["messages"].items():
                print(f"\n[{language}] {message['title']}\n{message['body']}")
            print(f"\nOffline guide: {plan['guide_path']}")
            _audit(
                storage,
                actor,
                "continuity.plan",
                plan["event_id"],
                {"affected_channels": plan["affected_channels"]},
            )
            return 0
        if action == "activate":
            result = service.activate(
                args.actor,
                args.reason,
                args.affected,
                _translations(args.translations),
            )
            _audit(
                storage,
                args.actor,
                "continuity.activated",
                result["event_id"],
                {"affected_channels": args.affected, "steps": result["steps"]},
            )
            print(
                f"Continuity operation {result['event_id']} "
                f"({'resumed' if not result['created'] else 'started'}):"
            )
            return 1 if _print_steps(result["steps"]) else 0
        if action == "deactivate":
            result = service.deactivate(args.actor, args.reason)
            _audit(
                storage,
                args.actor,
                "continuity.deactivated",
                result["event_id"],
                {"steps": result["steps"]},
            )
            print(
                f"Continuity operation {result['event_id']} "
                f"({'resumed' if not result['created'] else 'started'}):"
            )
            return 1 if _print_steps(result["steps"]) else 0
        if action == "channel":
            service.set_channel_state(args.channel, args.status, args.reason, args.actor)
            _audit(
                storage,
                args.actor,
                "continuity.channel_state",
                args.channel,
                {"status": args.status, "reason": args.reason},
            )
            print(f"{args.channel} is now marked {args.status}.")
            return 0
        if action == "drill":
            drill_action = getattr(args, "continuity_drill_action", None)
            if drill_action == "start":
                if not args.confirm:
                    raise ValueError(
                        "A drill is sent to live channels; repeat the command with --confirm."
                    )
                exercise = service.start_drill(args.actor, args.scenario, args.channel)
                _audit(
                    storage,
                    args.actor,
                    "continuity.drill_started",
                    exercise["exercise_id"],
                    {"channels": exercise["channels"], "delivery": exercise["delivery"]},
                )
                print(
                    f"Exercise {exercise['exercise_id']} published; "
                    "verify access on each selected channel, then record the review with "
                    "`continuity drill review`."
                )
                for channel, status in exercise["delivery"].items():
                    print(f"  {status.upper():<10} {channel}")
                return 1 if "failed" in exercise["delivery"].values() else 0
            if drill_action == "review":
                exercise = service.review_drill(
                    args.exercise_id,
                    args.actor,
                    args.access_verified == "yes",
                    args.problems,
                )
                _audit(
                    storage,
                    args.actor,
                    "continuity.drill_reviewed",
                    exercise["exercise_id"],
                    {
                        "status": exercise["status"],
                        "access_verified": exercise["access_verified"],
                        "problems": exercise["problems"],
                    },
                )
                print(
                    f"Exercise {exercise['exercise_id']}: {exercise['status']}. "
                    f"Problems: {exercise['problems'] or 'none recorded'}"
                )
                return 0
            if drill_action == "report":
                exercise = service.store.get_exercise(args.exercise_id)
                if exercise is None:
                    raise ValueError(f"Unknown continuity exercise: {args.exercise_id}.")
                _audit(
                    storage,
                    "local operator",
                    "continuity.drill_report",
                    exercise["exercise_id"],
                )
                print(f"Exercise: {exercise['exercise_id']} ({exercise['status']})")
                print(f"Scenario: {exercise['scenario']}")
                print(f"Channels: {', '.join(exercise['channels'])}")
                print(f"Started: {exercise['started_at']}")
                print(f"Delivery: {json.dumps(exercise['delivery'], sort_keys=True)}")
                print(f"Access verified: {exercise['access_verified']}")
                print(f"Problems: {exercise['problems'] or 'none recorded'}")
                return 0
            raise ValueError("Choose drill start, review or report.")
    except (KeyError, ValueError, OSError, RuntimeError) as exc:
        print(f"[ERROR] {exc}")
        return 1

    print(f"Unknown continuity action: {action}")
    return 2
