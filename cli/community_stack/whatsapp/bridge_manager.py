"""WhatsApp bridge manager, room allowlist validation, and emergency killswitch."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from community_stack import docker
from community_stack.config import load_config


class BridgeError(Exception):
    """Raised when bridge operations encounter an error."""


class WhatsAppBridgeManager:
    """Manages the optional mautrix-whatsapp bridge service."""

    def __init__(
        self,
        config_path: Path | str = "community-stack.yaml",
        env_path: Path | str = ".env",
    ) -> None:
        self.config_path = Path(config_path)
        self.env_path = Path(env_path)
        self.config = load_config(self.config_path) if self.config_path.is_file() else {}
        self.bridge_cfg = self.config.get("services", {}).get("whatsapp_bridge", {})

    def is_enabled(self) -> bool:
        """Return True if whatsapp_bridge is enabled in configuration."""
        return bool(self.bridge_cfg.get("enabled", False))

    def get_mode(self) -> str:
        """Return operating mode: 'none', 'announcements_only', or 'selective_bridge'."""
        return str(self.bridge_cfg.get("mode", "none"))

    def get_mapped_rooms(self) -> list[dict[str, Any]]:
        """Return strictly allowlisted room mappings."""
        return list(self.bridge_cfg.get("mapped_rooms", []))

    def is_room_allowlisted(self, matrix_room_id: str) -> bool:
        """Verify that a given Matrix room ID is explicitly allowlisted for bridging."""
        if not self.is_enabled() or self.get_mode() != "selective_bridge":
            return False
        for mapping in self.get_mapped_rooms():
            if mapping.get("matrix_room_id") == matrix_room_id:
                return True
        return False

    def check_health(self) -> dict[str, Any]:
        """Inspect running container status and linkage state."""
        if not self.is_enabled():
            return {
                "enabled": False,
                "status": "disabled",
                "mode": "none",
                "message": "WhatsApp bridge is disabled in configuration.",
            }

        if not docker.is_docker_available():
            return {
                "enabled": True,
                "status": "unknown",
                "mode": self.get_mode(),
                "message": "Docker is not available to inspect bridge container.",
            }

        containers = docker.list_community_containers()
        bridge_c = next((c for c in containers if c["name"] == "community_whatsapp_bridge"), None)
        if not bridge_c:
            return {
                "enabled": True,
                "status": "stopped",
                "mode": self.get_mode(),
                "message": "Bridge container is not running. Start with 'community-stack install'.",
            }

        return {
            "enabled": True,
            "status": bridge_c.get("state", "running"),
            "mode": self.get_mode(),
            "details": bridge_c.get("status", ""),
            "mapped_rooms_count": len(self.get_mapped_rooms()),
        }

    def emergency_disable(self, confirm: bool = False) -> bool:
        """Emergency killswitch: immediately stops bridge container without touching Matrix."""
        if not confirm:
            raise BridgeError(
                "Emergency bridge disable requires explicit confirmation flag (--confirm)."
            )

        print("[KILLSWITCH] Halting community_whatsapp_bridge container immediately...")
        services_dir = Path("services/whatsapp_bridge")
        if services_dir.is_dir() and docker.is_docker_available():
            res = docker.run_compose(
                project_dir=services_dir,
                args=["stop"],
            )
            return res.returncode == 0
        return True
