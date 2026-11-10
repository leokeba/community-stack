import asyncio

from community_stack.admin.request_limits import (
    MAX_ADMIN_BODY_BYTES,
    MAX_INBOX_BYTES,
    MAX_SUBSCRIBE_BODY_BYTES,
    RequestBodyLimitMiddleware,
)
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


def _client():
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware)

    @app.post("/{path:path}")
    async def echo_body(request: Request):
        return {"size": len(await request.body())}

    return TestClient(app)


def test_request_body_caps_reject_oversized_content_lengths():
    client = _client()
    for path, limit in (
        ("/ap/inbox", MAX_INBOX_BYTES),
        ("/admin/login", MAX_ADMIN_BODY_BYTES),
        ("/subscribe", MAX_SUBSCRIBE_BODY_BYTES),
    ):
        response = client.post(path, content=b"x" * (limit + 1))
        assert response.status_code == 413


def test_request_body_cap_rejects_chunked_oversize_body():
    client = _client()
    response = client.post(
        "/ap/inbox",
        content=iter((b"x" * MAX_INBOX_BYTES, b"x")),
    )
    assert response.status_code == 413


def test_invalid_content_length_returns_bad_request():
    app = RequestBodyLimitMiddleware(FastAPI())
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/ap/inbox",
        "raw_path": b"/ap/inbox",
        "query_string": b"",
        "headers": [(b"content-length", b"not-a-number")],
        "client": ("127.0.0.1", 12345),
        "server": ("example.test", 443),
    }

    asyncio.run(app(scope, receive, send))

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 400
