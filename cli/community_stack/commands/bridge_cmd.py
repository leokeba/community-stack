"""The 'bridge' CLI command: inspect, diagnose and stop a bridge from the host.

Linking accounts and choosing rooms are done in the admin console (Chat → Bridges), where
the bridge's Matrix bot is driven for you. The CLI covers what an operator needs at a
shell, for any supported bridge (WhatsApp by default).
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from community_stack import docker
from community_stack.agent import bridges
from community_stack.agent.bridges import BridgeError
from community_stack.agent.operations import AgentContext
from community_stack.bridges.catalogue import BridgeSpec, get, supported


def _context(project_dir: Path) -> AgentContext:
    return AgentContext(project_dir=project_dir)


def _print_checks(checks: list[dict]) -> None:
    for check in checks:
        marker = "OK  " if check["ok"] else "FAIL"
        print(f"  [{marker}] {check['message']}")
        if not check["ok"] and check.get("fix"):
            print(f"         -> {check['fix']}")


def _status(project_dir: Path, spec: BridgeSpec) -> int:
    checks = bridges.diagnose(_context(project_dir), spec)
    print(f"{spec.name} bridge ({spec.upstream})")
    _print_checks(checks)
    print("\nA running container is not the same as a linked account. Check linking in")
    print(f"the admin console: Chat → Bridges → {spec.name}.")
    print(f"\nThe {spec.name} bridge is optional. Core Matrix and announcements keep working")
    print("if it stops.")
    return 0


def _doctor(project_dir: Path, spec: BridgeSpec) -> int:
    checks = bridges.diagnose(_context(project_dir), spec)
    print(f"Diagnosing the {spec.name} bridge ({spec.container})")
    _print_checks(checks)
    failing = [check["id"] for check in checks if not check["ok"]]
    if failing:
        print(f"\n{len(failing)} check(s) need attention.")
        return 1
    print("\nAll local checks pass. Linking is confirmed separately in the admin console.")
    return 0


def _rooms(project_dir: Path, spec: BridgeSpec) -> int:
    try:
        rooms = bridges.portal_rooms(_context(project_dir), spec)
    except BridgeError as exc:
        print(f"[ERROR] {exc}")
        return 1
    if not rooms:
        print(f"No Matrix rooms are linked to {spec.name} {spec.chat_noun}s yet.")
        return 0
    print(f"{'MATRIX ROOM ID':<50} {'MEMBERS':>7}  RELAY  NAME")
    print("-" * 86)
    for room in rooms:
        relay = "on " if room["relay"] else "off"
        print(f"{room['room_id']:<50} {room['members']:>7}  {relay}    {room['name']}")
    return 0


def _logs(spec: BridgeSpec, lines: int) -> int:
    if not docker.is_docker_available():
        print("[ERROR] Docker is not available.")
        return 1
    result = subprocess.run(
        ["docker", "logs", "--tail", str(lines), spec.container],
        capture_output=True,
        text=True,
        check=False,
    )
    output = (result.stdout + result.stderr).strip()
    print(output or "(no log output)")
    return result.returncode


def _disable(project_dir: Path, spec: BridgeSpec) -> int:
    """Emergency stop: halt the container now. Data, login and room links are kept."""
    if not docker.is_docker_available():
        print("[ERROR] Docker is not available.")
        return 1
    result = docker.run_compose(project_dir / "services" / spec.service, ["stop"])
    if result.returncode != 0:
        print(f"[ERROR] The {spec.name} bridge container could not be stopped.")
        return 1
    print(f"  [SUCCESS] The {spec.name} bridge container has been stopped.")
    print("            Its data, login and room links are kept; Matrix keeps running.")
    print("            Start it again under Services and updates in the admin console.")
    return 0


def run_bridge(
    args: argparse.Namespace,
    config_path: Path | str = "community-stack.yaml",
    env_path: Path | str = ".env",
) -> int:
    """Entry point for the bridge command."""
    action = getattr(args, "bridge_action", None) or "status"
    project_dir = Path(config_path).resolve().parent
    key = getattr(args, "bridge", None) or "whatsapp"
    try:
        spec = get(key)
    except KeyError:
        names = ", ".join(spec.key for spec in supported())
        print(f"Unknown bridge: {key}. Choose one of: {names}.")
        return 2

    if action == "status":
        return _status(project_dir, spec)
    if action == "doctor":
        return _doctor(project_dir, spec)
    if action == "rooms":
        return _rooms(project_dir, spec)
    if action == "logs":
        return _logs(spec, getattr(args, "lines", 100))
    if action == "disable":
        if not getattr(args, "confirm", False):
            print("[ERROR] Emergency disable requires confirmation flag: --confirm")
            print(f"        Run: community-stack bridge --bridge {spec.key} disable --confirm")
            return 1
        return _disable(project_dir, spec)

    print(f"Unknown bridge action: {action}")
    return 2
