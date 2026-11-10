"""Request body limits for unauthenticated and form-post endpoints."""

from __future__ import annotations

from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

MAX_INBOX_BYTES = 256 * 1024
MAX_ADMIN_BODY_BYTES = 12 * 1024 * 1024
MAX_SUBSCRIBE_BODY_BYTES = 1024 * 1024


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == "/ap/inbox":
            max_bytes = MAX_INBOX_BYTES
        elif path.startswith("/admin"):
            max_bytes = MAX_ADMIN_BODY_BYTES
        elif path.startswith("/subscribe"):
            max_bytes = MAX_SUBSCRIBE_BODY_BYTES
        else:
            await self.app(scope, receive, send)
            return

        lengths = [
            value for key, value in scope.get("headers", []) if key.lower() == b"content-length"
        ]
        if lengths:
            if len(lengths) != 1 or not lengths[0].isdigit():
                await Response(status_code=400)(scope, receive, send)
                return
            try:
                if int(lengths[0]) > max_bytes:
                    await Response(status_code=413)(scope, receive, send)
                    return
            except ValueError:
                await Response(status_code=400)(scope, receive, send)
                return

        size = 0

        async def limited_receive():
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > max_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await Response(status_code=413)(scope, receive, send)
