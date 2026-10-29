"""Implementation of the 'agent' command."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path


def run_agent(args: argparse.Namespace) -> int:
    if getattr(args, "agent_action", None) != "serve":
        print("Usage: community-stack agent serve [--project-dir DIR]")
        return 2

    from community_stack.agent.server import serve

    project_dir = Path(args.project_dir).resolve()
    if not (project_dir / "community-stack.yaml").is_file():
        print(f"[ERROR] No community-stack.yaml in {project_dir}.")
        return 1
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        serve(
            project_dir,
            socket_path=Path(args.socket) if args.socket else None,
            allowed_uids=set(args.console_uid) if args.console_uid else None,
            socket_gid=args.console_gid,
            run_scheduler=not getattr(args, "no_scheduler", False),
        )
    except KeyboardInterrupt:
        return 0
    return 0
