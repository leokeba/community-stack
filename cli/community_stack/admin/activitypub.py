"""ActivityPub and WebFinger endpoints that let the announcement feed be followed from the fediverse."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from community_stack.announcements.adapters import http
from community_stack.announcements.fediverse import ACTIVITY_JSON, CONTEXT, FediverseAdapter
from community_stack.announcements.models import Announcement, AnnouncementStatus
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage

MAX_INBOX_BYTES = 256 * 1024


def register_activitypub(
    app: FastAPI,
    *,
    storage: AnnouncementStorage,
    publisher: AnnouncementPublisher,
    community_name: Callable[[], str],
) -> None:
    def fediverse():
        adapter = next((a for a in publisher.adapters if isinstance(a, FediverseAdapter)), None)
        if adapter is None:
            raise HTTPException(status_code=404)
        return adapter.fediverse

    def activity_json(data: dict, status_code: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status_code, media_type=ACTIVITY_JSON)

    @app.get("/.well-known/webfinger")
    async def webfinger(request: Request) -> Response:
        document = fediverse().webfinger(request.query_params.get("resource", ""))
        if document is None:
            raise HTTPException(status_code=404)
        return JSONResponse(document, media_type="application/jrd+json")

    @app.get("/ap/actor")
    async def ap_actor() -> Response:
        name = community_name()
        return activity_json(fediverse().actor(name, f"Public announcements from {name}."))

    @app.get("/ap/followers")
    async def ap_followers() -> Response:
        account = fediverse()
        return activity_json(
            {
                "@context": "https://www.w3.org/ns/activitystreams",
                "id": f"{account.base_url}/ap/followers",
                "type": "OrderedCollection",
                "totalItems": account.follower_count(),
            }
        )

    def public_announcements() -> list[Announcement]:
        return [
            a
            for a in storage.list_announcements(limit=20)
            if a.status in (AnnouncementStatus.PUBLISHED, AnnouncementStatus.UPDATED)
        ]

    @app.get("/ap/outbox")
    async def ap_outbox() -> Response:
        account = fediverse()
        items = [account.activity("Create", a) for a in public_announcements()]
        return activity_json(
            {
                "@context": "https://www.w3.org/ns/activitystreams",
                "id": f"{account.base_url}/ap/outbox",
                "type": "OrderedCollection",
                "totalItems": len(items),
                "orderedItems": items,
            }
        )

    @app.get("/ap/notes/{announcement_id}")
    async def ap_note(announcement_id: str) -> Response:
        account = fediverse()
        announcement = storage.get_announcement(announcement_id)
        if announcement and announcement.status == AnnouncementStatus.WITHDRAWN:
            return activity_json(
                {"id": account.note_id(announcement_id), "type": "Tombstone"}, status_code=410
            )
        if not announcement or announcement.status not in (
            AnnouncementStatus.PUBLISHED,
            AnnouncementStatus.UPDATED,
        ):
            raise HTTPException(status_code=404)
        return activity_json({"@context": CONTEXT, **account.note(announcement)})

    @app.post("/ap/inbox")
    async def ap_inbox(request: Request) -> Response:
        account = fediverse()
        if int(request.headers.get("content-length") or 0) > MAX_INBOX_BYTES:
            raise HTTPException(status_code=413)
        body = await request.body()
        if len(body) > MAX_INBOX_BYTES:
            raise HTTPException(status_code=413)
        status, message = await run_in_threadpool(
            account.handle_inbox, request.url.path, dict(request.headers), body, http.send
        )
        return JSONResponse({"result": message}, status_code=status)
