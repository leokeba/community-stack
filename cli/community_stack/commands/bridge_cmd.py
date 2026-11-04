"""The 'bridge' CLI command: inspect, diagnose and operate bridges from the host.

Linking and room selection are done in the admin console, where the bridge's Matrix
bot is driven for you. The CLI covers what an operator needs at a shell.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from community_stack import docker
from community_stack.agent import bridges
from community_stack.agent.bridges import BridgeError
from community_stack.agent.operations import AgentContext
from community_stack.bridges.catalogue import WHATSAPP
from community_stack.whatsapp.bridge_manager import WhatsAppBridgeManager


def _context(project_dir: Path) -> AgentContext:
    return AgentContext(project_dir=project_dir)


def _print_checks(checks: list[dict]) -> None:
    for check in checks:
        marker = "OK  " if check["ok"] else "FAIL"
        print(f"  [{marker}] {check['message']}")
        if not check["ok"] and check.get("fix"):
            print(f"         -> {check['fix']}")


def _status(project_dir: Path) -> int:
    checks = bridges.diagnose(_context(project_dir))
    print(f"WhatsApp bridge ({WHATSAPP.upstream})")
    _print_checks(checks)
    print("\nA running container is not the same as a linked account. Check linking in")
    print("the admin console (WhatsApp bridge page, 'Check linked accounts').")
    print("\nmautrix-whatsapp is an unofficial integration. Core Matrix and announcements")
    print("keep working if the bridge stops.")
    return 0


def _doctor(project_dir: Path) -> int:
    checks = bridges.diagnose(_context(project_dir))
    print(f"Diagnosing WhatsApp bridge ({WHATSAPP.container})")
    _print_checks(checks)
    failing = [check["id"] for check in checks if not check["ok"]]
    if failing:
        print(f"\n{len(failing)} check(s) need attention.")
        return 1
    print("\nAll local checks pass. Linking is confirmed separately in the admin console.")
    return 0


def _rooms(project_dir: Path) -> int:
    try:
        rooms = bridges.portal_rooms(_context(project_dir))
    except BridgeError as exc:
        print(f"[ERROR] {exc}")
        return 1
    if not rooms:
        print("No bridged WhatsApp rooms yet. A room appears after a chat's first message.")
        return 0
    print(f"{'MATRIX ROOM ID':<50} {'MEMBERS':>7}  NAME")
    print("-" * 80)
    for room in rooms:
        print(f"{room['room_id']:<50} {room['members']:>7}  {room['name']}")
    return 0


def _logs(lines: int) -> int:
    if not docker.is_docker_available():
        print("[ERROR] Docker is not available.")
        return 1
    result = subprocess.run(
        ["docker", "logs", "--tail", str(lines), WHATSAPP.container],
        capture_output=True,
        text=True,
        check=False,
    )
    output = (result.stdout + result.stderr).strip()
    print(output or "(no log output)")
    return result.returncode


def run_bridge(
    args: argparse.Namespace,
    config_path: Path | str = "community-stack.yaml",
    env_path: Path | str = ".env",
) -> int:
    """Entry point for the bridge command."""
    action = getattr(args, "bridge_action", None) or "status"
    project_dir = Path(config_path).resolve().parent

    if action == "status":
        return _status(project_dir)
    if action == "doctor":
        return _doctor(project_dir)
    if action == "rooms":
        return _rooms(project_dir)
    if action == "logs":
        return _logs(getattr(args, "lines", 100))
    if action == "disable":
        if not getattr(args, "confirm", False):
            print("[ERROR] Emergency disable requires confirmation flag: --confirm")
            print("        Run: community-stack bridge disable --confirm")
            return 1
        manager = WhatsAppBridgeManager(config_path=config_path, env_path=env_path)
        try:
            manager.emergency_disable(confirm=True)
        except BridgeError as exc:
            print(f"[ERROR] {exc}")
            return 1
        print("  [SUCCESS] WhatsApp bridge container has been halted.")
        print("            Its data, credentials and room mappings are kept.")
        print("            Matrix homeserver remains completely operational.")
        return 0

    print(f"Unknown bridge action: {action}")
    return 2
