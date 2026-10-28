"""Console-side client for the control agent."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 15 * 60


class AgentUnavailable(Exception):
    """The agent is not installed, not running, or not reachable from here."""


class AgentRefused(Exception):
    """The agent refused the request or the operation failed; the message is user-facing."""


class AgentClient:
    def __init__(self, socket_path: Path | str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout

    def available(self) -> bool:
        return self.socket_path.is_socket()

    def call(
        self,
        op: str,
        actor: str,
        params: dict[str, str] | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {"op": op, "actor": actor, "params": params or {}}
        if password is not None:
            request["password"] = password
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(str(self.socket_path))
                connection.sendall(json.dumps(request).encode("utf-8"))
                connection.shutdown(socket.SHUT_WR)
                chunks = []
                while chunk := connection.recv(65536):
                    chunks.append(chunk)
        except OSError as exc:
            raise AgentUnavailable("The system agent is not reachable.") from exc
        try:
            response = json.loads(b"".join(chunks))
        except ValueError as exc:
            raise AgentUnavailable("The system agent sent an unreadable reply.") from exc
        if not response.get("ok"):
            raise AgentRefused(response.get("error") or "The system agent refused the request.")
        return response.get("result") or {}
