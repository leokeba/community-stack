"""Implementation of the 'bridge' CLI command and subcommands."""

from __future__ import annotations

import argparse
from pathlib import Path

from community_stack.whatsapp.bridge_manager import BridgeError, WhatsAppBridgeManager


def run_bridge(
    args: argparse.Namespace,
    config_path: Path | str = "community-stack.yaml",
    env_path: Path | str = ".env",
) -> int:
    """Entry point for the bridge command."""
    manager = WhatsAppBridgeManager(config_path=config_path, env_path=env_path)
    action = getattr(args, "bridge_action", None)

    if not action or action == "status":
        health = manager.check_health()
        print("================================================================================")
        print("                       WHATSAPP BRIDGE STATUS                                   ")
        print("================================================================================\n")
        print(
            f"Enabled in Config:  {'Yes' if health.get('enabled') else 'No (Disabled by default)'}"
        )
        print(f"Operating Mode:     {health.get('mode')}")
        print(f"Container Status:   {health.get('status')}")
        if health.get("details"):
            print(f"Container Details:  {health['details']}")
        if "message" in health:
            print(f"Notes:              {health['message']}")
        if "mapped_rooms_count" in health:
            print(f"Allowlisted Rooms:  {health['mapped_rooms_count']}")
        print("\nNote: mautrix-whatsapp is an unofficial reverse-engineered integration.")
        print("      Core Matrix and public announcement operations continue if bridge drops.")
        return 0

    elif action == "disable":
        confirm = getattr(args, "confirm", False)
        if not confirm:
            print("[ERROR] Emergency disable requires confirmation flag: --confirm")
            print("        Run: community-stack bridge disable --confirm")
            return 1
        try:
            manager.emergency_disable(confirm=confirm)
            print("  [SUCCESS] WhatsApp bridge container has been halted.")
            print("            Outgoing and incoming bridge traffic is blocked.")
            print("            Matrix homeserver remains completely operational.")
            return 0
        except BridgeError as exc:
            print(f"[ERROR] {exc}")
            return 1

    elif action == "list-rooms":
        rooms = manager.get_mapped_rooms()
        if not rooms:
            print("No Matrix rooms are currently allowlisted for WhatsApp bridging.")
            return 0

        print(f"{'MATRIX ROOM ID':<35} {'WHATSAPP JID':<30} {'DESCRIPTION'}")
        print("-" * 80)
        for r in rooms:
            m_id = r.get("matrix_room_id", "")
            w_id = r.get("whatsapp_chat_jid", "")
            desc = r.get("description", "")
            print(f"{m_id:<35} {w_id:<30} {desc}")
        return 0

    else:
        print(f"Unknown bridge action: {action}")
        return 2
