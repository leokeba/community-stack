"""Unix-socket server for the control agent (see docs/decisions/0007-control-agent.md)."""

from __future__ import annotations

import json
import logging
import os
import socket
import socketserver
import struct
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from community_stack.admin.store import account_password_matches, can, read_account
from community_stack.agent import backups
from community_stack.agent.operations import (
    CATALOG,
    DANGEROUS,
    READ,
    AgentContext,
    OperationError,
)
from community_stack.backup import BackupError
from community_stack.redact import is_sensitive_key

MAX_REQUEST_BYTES = 64 * 1024
READ_TIMEOUT_SECONDS = 10
PASSWORD_FAILURE_LIMIT = 5
PASSWORD_FAILURE_WINDOW_SECONDS = 15 * 60

logger = logging.getLogger(__name__)


def peer_uid(connection: socket.socket) -> int | None:
    """UID of the process on the other end of a Unix socket, where the OS reports it."""
    if not hasattr(socket, "SO_PEERCRED"):
        return None
    creds = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


class AgentService:
    """Checks and runs one request; independent of the socket so it can be tested directly."""

    def __init__(self, context: AgentContext) -> None:
        self.context = context
        self._change_lock = threading.Lock()
        self._password_failures: dict[str, deque[float]] = defaultdict(deque)
        self._failures_lock = threading.Lock()

    def handle(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            return {"ok": False, "error": "Request must be a JSON object."}
        op_name = request.get("op")
        actor = request.get("actor")
        operation = CATALOG.get(op_name) if isinstance(op_name, str) else None
        if operation is None:
            return self._finish(request, "refused", "Unknown operation.")
        if not isinstance(actor, str) or not actor:
            return self._finish(request, "refused", "Requests must name the acting account.")
        try:
            params = operation.validate(request.get("params", {}))
        except OperationError as exc:
            return self._finish(request, "refused", str(exc))

        account = read_account(self.context.admin_db_path, actor)
        if account is None or not can(account["role"], operation.permission):
            return self._finish(request, "refused", "This account may not run this operation.")
        if operation.risk == DANGEROUS or (
            operation.needs_password is not None and operation.needs_password(params)
        ):
            refusal = self._check_password(actor, account, request.get("password"))
            if refusal:
                return self._finish(request, "refused", refusal)

        if operation.risk == READ:
            return self._run(request, operation, params)
        if not self._change_lock.acquire(blocking=False):
            return self._finish(request, "refused", "Another change is in progress; try again.")
        try:
            return self._run(request, operation, params)
        finally:
            self._change_lock.release()

    def run_scheduled_backup_if_due(self) -> None:
        """Called by the scheduler thread; skips quietly when another change is running."""
        if not backups.backup_due(self.context, datetime.now(timezone.utc)):
            return
        if not self._change_lock.acquire(blocking=False):
            return
        try:
            state = backups.run_scheduled_backup(self.context)
        finally:
            self._change_lock.release()
        request = {"op": "backup.scheduled", "actor": "scheduler", "params": {}}
        self._audit(request, state["last_result"], state.get("last_error"))

    def _check_password(self, actor: str, account: dict, password: Any) -> str | None:
        now = time.monotonic()
        with self._failures_lock:
            failures = self._password_failures[actor]
            while failures and now - failures[0] > PASSWORD_FAILURE_WINDOW_SECONDS:
                failures.popleft()
            if len(failures) >= PASSWORD_FAILURE_LIMIT:
                return "Too many wrong passwords. Try again later."
        if isinstance(password, str) and account_password_matches(account, password):
            return None
        with self._failures_lock:
            self._password_failures[actor].append(now)
        return "Password confirmation failed."

    def _run(self, request: dict, operation, params: dict[str, str]) -> dict[str, Any]:
        try:
            result = operation.handler(self.context, params)
        except (OperationError, BackupError) as exc:
            return self._finish(request, "failed", str(exc))
        except Exception:
            logger.exception("Agent operation %s failed", operation.name)
            return self._finish(request, "failed", "The operation failed; see the agent log.")
        self._audit(request, "succeeded")
        return {"ok": True, "result": result}

    def _finish(self, request: Any, outcome: str, error: str) -> dict[str, Any]:
        self._audit(request, outcome, error)
        return {"ok": False, "error": error}

    def _audit(self, request: Any, outcome: str, error: str | None = None) -> None:
        request = request if isinstance(request, dict) else {}
        params = request.get("params")
        # Bounded so a hostile request cannot bloat the log; passwords are never logged.
        safe_params = (
            {
                str(k)[:40]: "[REDACTED]" if is_sensitive_key(str(k)) else str(v)[:200]
                for k, v in list(params.items())[:10]
            }
            if isinstance(params, dict)
            else {}
        )
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "actor": str(request.get("actor", ""))[:80],
            "op": str(request.get("op", ""))[:80],
            "params": safe_params,
            "outcome": outcome,
        }
        if error:
            entry["error"] = error
        path = self.context.audit_path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        line = (json.dumps(entry) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)


class _Handler(socketserver.BaseRequestHandler):
    server: AgentSocketServer

    def handle(self) -> None:
        connection: socket.socket = self.request
        connection.settimeout(READ_TIMEOUT_SECONDS)
        # Read the whole request before answering, even to refuse it: closing a
        # socket with unread data resets the connection and the client would
        # see "unreachable" instead of the reason.
        try:
            payload, too_large = self._read_request(connection)
        except OSError:
            return
        allowed = self.server.allowed_uids
        uid = peer_uid(connection)
        if allowed is not None and uid is not None and uid not in allowed:
            self._reply({"ok": False, "error": "This process may not use the agent."})
            return
        if too_large:
            self._reply({"ok": False, "error": "Request is too large."})
            return
        try:
            request = json.loads(payload or b"null")
        except ValueError:
            self._reply({"ok": False, "error": "Malformed request."})
            return
        self._reply(self.server.service.handle(request))

    @staticmethod
    def _read_request(connection: socket.socket) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        size = 0
        while chunk := connection.recv(65536):
            size += len(chunk)
            if size <= MAX_REQUEST_BYTES:
                chunks.append(chunk)
            elif size > 16 * MAX_REQUEST_BYTES:
                break
        return b"".join(chunks), size > MAX_REQUEST_BYTES

    def _reply(self, response: dict[str, Any]) -> None:
        try:
            self.request.sendall(json.dumps(response, default=str).encode("utf-8"))
        except OSError:
            pass


class AgentSocketServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(
        self,
        socket_path: Path,
        service: AgentService,
        allowed_uids: set[int] | None = None,
        socket_gid: int | None = None,
    ) -> None:
        self.service = service
        self.allowed_uids = allowed_uids
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.is_socket():
            socket_path.unlink()
        super().__init__(str(socket_path), _Handler)
        # The console reaches the socket through its group; nobody else can.
        os.chmod(socket_path, 0o660)
        os.chmod(socket_path.parent, 0o750)
        if socket_gid is not None:
            os.chown(socket_path, -1, socket_gid)
            os.chown(socket_path.parent, -1, socket_gid)


def default_socket_path(project_dir: Path) -> Path:
    return project_dir / "data" / "agent" / "run" / "agent.sock"


def serve(
    project_dir: Path,
    socket_path: Path | None = None,
    allowed_uids: set[int] | None = None,
    socket_gid: int | None = None,
    run_scheduler: bool = True,
) -> None:
    context = AgentContext(project_dir=project_dir.resolve())
    path = socket_path or default_socket_path(context.project_dir)
    service = AgentService(context)
    if run_scheduler:
        backups.BackupScheduler(service).start()
    with AgentSocketServer(path, service, allowed_uids, socket_gid) as server:
        logger.info("Control agent listening on %s", path)
        server.serve_forever()
