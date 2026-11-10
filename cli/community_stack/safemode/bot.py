"""The Safe Mode bot: a Matrix application service that answers ``!safe`` in private rooms.

Synapse pushes events to ``PUT /_matrix/app/v1/transactions/{txnId}``, authenticated with
the homeserver token. The bot acts only in direct conversations (exactly two joined
members, one of them the bot) and only for enrolled accounts. The sender is taken from the
event that Synapse delivers, never from message text. Message bodies are never logged.

Synapse waits for each transaction to be acknowledged before sending the next one, so the
request thread only records commands in the store (and reads the room's members once).
Kicks, invites and replies run on a small worker pool. A recorded ``!safe on`` is finished by
the reconciler even if the bot stops before its worker runs.

The bot also watches protected rooms: a suspended account that is invited back or rejoins is
removed again.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from community_stack.safemode.commands import CommandError, parse
from community_stack.safemode.manager import HELP, AccessManager, GatewayError, Submitted
from community_stack.safemode.settings import BotConfig, BotConfigError
from community_stack.safemode.store import Store
from community_stack.safemode.synapse import SynapseGateway

log = logging.getLogger("community_stack.safemode")

MIN_SECONDS_BETWEEN_COMMANDS = 3.0
MAX_BODY_BYTES = 4 * 1024 * 1024
RECONCILE_INTERVAL_SECONDS = 300
SEEN_TRANSACTIONS = 512
TRANSACTION_PATH = re.compile(r"^/_matrix/app/v1/transactions/([^/?]+)$")
DRIVE_PATH = "/_safemode/v1/drive"
ENCRYPTED_NOTICE = (
    "I cannot read encrypted messages, so I did not act on your last message. "
    "Safe Mode has NOT changed. Start a new direct chat with me with encryption turned off, "
    "or ask an administrator."
)
WORKERS = 4


class SafetyBot:
    def __init__(
        self,
        manager: AccessManager,
        gateway: SynapseGateway,
        clock: Callable[[], float] = time.monotonic,
        executor: Executor | None = None,
    ) -> None:
        self.manager = manager
        self.gateway = gateway
        self.clock = clock
        self._pool = executor or ThreadPoolExecutor(
            max_workers=WORKERS, thread_name_prefix="safemode"
        )
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._last_command: dict[str, float] = {}
        self._lock = threading.Lock()

    # Transactions ----------------------------------------------------------

    def handle_transaction(self, txn_id: str, events: list[dict[str, Any]]) -> None:
        with self._lock:
            if txn_id in self._seen:
                return
        for event in events:
            try:
                self._handle_event(event)
            except (GatewayError, CommandError, KeyError, TypeError, ValueError) as exc:
                # Log the kind of failure only; never the event content.
                log.warning("safe mode could not process an event: %s", type(exc).__name__)
        with self._lock:
            self._seen[txn_id] = None
            while len(self._seen) > SEEN_TRANSACTIONS:
                self._seen.popitem(last=False)

    def _handle_event(self, event: dict[str, Any]) -> None:
        sender = event.get("sender", "")
        room_id = event.get("room_id", "")
        if not sender or not room_id or sender == self.gateway.bot_user_id:
            return
        event_type = event.get("type")
        content = event.get("content") or {}
        if event_type == "m.room.member":
            target = event.get("state_key") or ""
            membership = content.get("membership")
            if target == self.gateway.bot_user_id:
                if membership == "invite":
                    self._later(self._on_invite, room_id, sender)
                return
            if membership in ("join", "invite") and self.manager.is_suspended(target):
                # Someone let a suspended account back in; protected rooms only (checked in enforce).
                self._later(self.manager.enforce, target, room_id)
            return
        if event_type == "m.room.message":
            self._on_message(event, room_id, sender, content)
        elif (
            event_type == "m.room.encrypted"
            and self.manager.store.is_enrolled(sender)
            and self._allow_now(sender)
        ):
            # The bot has no encryption keys. Say so, rather than leave the member believing a
            # command worked; the content is unreadable here and never inspected.
            self._later(self._encrypted_notice, room_id)

    def _on_invite(self, room_id: str, inviter: str) -> None:
        if not self.manager.store.is_enrolled(inviter):
            self.gateway.leave(room_id)  # declining the invitation; unknown accounts get no reply
            return
        self.gateway.join(room_id)
        if not self._is_direct(room_id):
            self.gateway.leave(room_id)
            return
        self._reply(room_id, HELP)

    def _on_message(
        self, event: dict[str, Any], room_id: str, sender: str, content: dict[str, Any]
    ) -> None:
        if not self.manager.store.is_enrolled(sender):
            return  # unknown accounts get no reply at all
        if content.get("msgtype") not in (None, "m.text"):
            return
        body = content.get("body")
        if not isinstance(body, str) or not body.lstrip().lower().startswith("!safe"):
            return
        if not self._is_direct(room_id):
            return
        try:
            command = parse(body, content)
        except CommandError as exc:
            self._later(self._reply, room_id, str(exc))
            return
        if command is None:
            return
        if not self._allow_now(sender):
            self._later(
                self._reply,
                room_id,
                "Please wait a few seconds before sending another Safe Mode command.",
            )
            return
        # Recorded in the store before the transaction is acknowledged; Matrix work comes later.
        submitted = self.manager.submit(sender, command)
        if submitted.reply is not None:
            self._later(self._reply, room_id, submitted.reply)
        else:
            self._later(self._finish, sender, room_id, submitted)

    def _encrypted_notice(self, room_id: str) -> None:
        if self._is_direct(room_id):
            self._reply(room_id, ENCRYPTED_NOTICE)

    def _finish(self, sender: str, room_id: str, submitted: Submitted) -> None:
        # A newer command from the same account replaces this one and sends its own reply.
        if self.manager.drive(sender, submitted.operation_id):
            self._reply(room_id, self.manager.outcome_text(sender, submitted.verb))

    def _is_direct(self, room_id: str) -> bool:
        members = self.gateway.joined_members(room_id)
        return len(members) == 2 and self.gateway.bot_user_id in members

    def _allow_now(self, sender: str) -> bool:
        now = self.clock()
        with self._lock:
            last = self._last_command.get(sender)
            if last is not None and now - last < MIN_SECONDS_BETWEEN_COMMANDS:
                return False
            self._last_command[sender] = now
            return True

    def _reply(self, room_id: str, text: str) -> None:
        self.gateway.send_notice(room_id, text, uuid.uuid4().hex)

    def _later(self, action: Callable[..., object], *args: object) -> None:
        self._pool.submit(self._safely, action, *args)

    @staticmethod
    def _safely(action: Callable[..., object], *args: object) -> None:
        try:
            action(*args)
        except Exception as exc:  # a worker must never die silently or log event content
            log.warning("safe mode could not finish a task: %s", type(exc).__name__)

    # Maintenance -----------------------------------------------------------

    def drive_now(self, user_id: str) -> str:
        """Finish the account's current operation now (operator tools) and describe the state."""
        self.manager.drive(user_id)
        return self.manager.status_text(user_id)

    def reconcile(self) -> list[str]:
        try:
            return self.manager.reconcile()
        except GatewayError as exc:
            log.warning("safe mode reconciliation could not reach Matrix: %s", type(exc).__name__)
            return []


def make_handler(bot: SafetyBot, hs_token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "community-safemode"
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return  # request lines can carry tokens in query strings; keep them out of logs

        def _reply(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authorised(self) -> bool:
            header = self.headers.get("Authorization", "")
            expected = f"Bearer {hs_token}"
            return hmac.compare_digest(header.encode(), expected.encode())

        def do_GET(self) -> None:
            if self.path == "/health":
                self._reply(200, {"status": "ok"})
            else:
                self._reply(404, {"errcode": "M_NOT_FOUND"})

        def do_POST(self) -> None:
            if self.path.startswith("/_matrix/app/v1/ping"):
                if not self._authorised():
                    self._reply(403, {"errcode": "M_FORBIDDEN"})
                    return
                self._reply(200, {})
                return
            if self.path == DRIVE_PATH:
                # Operator tools record a change in the store, then ask the bot to carry it out,
                # so every membership change for an account comes from this one process.
                if not self._authorised():
                    self._reply(403, {"errcode": "M_FORBIDDEN"})
                    return
                payload = self._json_body()
                user_id = payload.get("user_id") if isinstance(payload, dict) else None
                if not isinstance(user_id, str) or not user_id:
                    self._reply(400, {"errcode": "M_BAD_JSON"})
                    return
                try:
                    self._reply(200, {"text": bot.drive_now(user_id)})
                except GatewayError:
                    self._reply(502, {"errcode": "M_UNKNOWN", "error": "Matrix unreachable"})
                return
            self._reply(404, {"errcode": "M_NOT_FOUND"})

        def _json_body(self) -> object:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > MAX_BODY_BYTES:
                return None
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return None

        def do_PUT(self) -> None:
            match = TRANSACTION_PATH.match(self.path.split("?", 1)[0])
            if not match:
                self._reply(404, {"errcode": "M_NOT_FOUND"})
                return
            if not self._authorised():
                self._reply(403, {"errcode": "M_FORBIDDEN"})
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > MAX_BODY_BYTES:
                self._reply(413, {"errcode": "M_TOO_LARGE"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._reply(400, {"errcode": "M_NOT_JSON"})
                return
            events = payload.get("events") or []
            if isinstance(events, list):
                bot.handle_transaction(match.group(1), [e for e in events if isinstance(e, dict)])
            self._reply(200, {})

    return Handler


def serve(config: BotConfig, bot: SafetyBot) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(
        ("0.0.0.0", config.listen_port), make_handler(bot, config.hs_token)
    )
    server.daemon_threads = True
    return server


def build(config: BotConfig) -> tuple[SafetyBot, SynapseGateway, Store]:
    store = Store(config.db_path)
    gateway = SynapseGateway(config.homeserver_url, config.bot_user_id, config.as_token)
    manager = AccessManager(store, gateway, config.bot_user_id)
    return SafetyBot(manager, gateway), gateway, store


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    config_path = args[args.index("--config") + 1] if "--config" in args else "/data/config.yaml"
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        config = BotConfig.load(config_path)
    except BotConfigError as exc:
        log.error("%s", exc)
        return 1
    bot, gateway, _store = build(config)
    _register_with_retries(gateway)
    _start_reconciler(bot)  # finishes anything interrupted, without delaying the listener
    server = serve(config, bot)
    log.info("safe mode bot listening on port %s", config.listen_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def ask_bot_to_drive(config: BotConfig, user_id: str, timeout: float = 110) -> str | None:
    """From an operator command in the bot container: ask the running bot to carry out the
    account's recorded operation. Returns the bot's description, or None if it cannot be reached."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{config.listen_port}{DRIVE_PATH}",
        data=json.dumps({"user_id": user_id}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {config.hs_token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return str(json.loads(response.read() or b"{}").get("text", ""))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def _register_with_retries(gateway: SynapseGateway, attempts: int = 30) -> None:
    for attempt in range(attempts):
        try:
            gateway.register_bot()
            return
        except GatewayError:
            log.info("waiting for the homeserver (attempt %s)", attempt + 1)
            time.sleep(min(10, 1 + attempt))
    raise SystemExit("Safe Mode could not register its account with the homeserver.")


def _start_reconciler(bot: SafetyBot) -> None:
    def loop() -> None:
        while True:
            bot.reconcile()
            time.sleep(RECONCILE_INTERVAL_SECONDS)

    threading.Thread(target=loop, name="safemode-reconcile", daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
