"""Routes for the mirroring pages of the admin console."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from community_stack.admin.common import flash
from community_stack.announcements.matrix_mirror import MODERATOR_POWER


def register_mirroring(
    app: FastAPI,
    *,
    audit: Any,
    checked_form: Any,
    mirror: Any,
    page: Any,
    publisher: Any,
    redirect_with: Any,
    render: Any,
    require: Any,
) -> None:
    def mirroring_page(
        request: Request, session: dict[str, Any], error: str | None = None, status_code: int = 200
    ):
        rooms = mirror()
        labels = {a.name: a.capabilities.label for a in publisher.adapters}
        return render(
            request,
            "mirroring.html",
            page(
                session,
                available=rooms is not None,
                rooms=rooms.rooms() if rooms else [],
                channel_labels=labels,
                **({"flash": flash(error, "error")} if error else {}),
            ),
            status_code=status_code,
        )

    @app.get("/admin/mirroring")
    async def mirroring(request: Request) -> Response:
        return mirroring_page(request, require(request, "settings"))

    @app.post("/admin/mirroring/add")
    async def add_mirrored_room(request: Request) -> Response:
        session, _ = await checked_form(request, "settings")
        raw = await request.form()
        rooms = mirror()
        if rooms is None:
            raise HTTPException(
                status_code=409, detail="Set up the Matrix announcements channel first."
            )
        known = {a.name for a in publisher.adapters}
        channels = [c for c in raw.getlist("channel") if c in known]
        if not channels:
            return mirroring_page(request, session, "Choose at least one channel.", 400)
        try:
            added = await run_in_threadpool(
                rooms.add_room,
                str(raw.get("room", "")).strip(),
                str(raw.get("mode", "review")),
                MODERATOR_POWER if raw.get("who") != "everyone" else 0,
                channels,
                session["username"],
            )
        except ValueError as exc:
            return mirroring_page(request, session, str(exc), 400)
        audit(
            request,
            session["username"],
            "mirroring.room_added",
            added["room_id"],
            mode=str(raw.get("mode")),
            channels=channels,
        )
        return redirect_with(
            request, "/admin/mirroring", flash(f"Mirroring {raw.get('room', '')}.".strip())
        )

    @app.post("/admin/mirroring/remove")
    async def remove_mirrored_room(request: Request) -> Response:
        session, form = await checked_form(request, "settings")
        rooms = mirror()
        if rooms is not None:
            rooms.remove_room(form.get("room_id", ""))
        audit(request, session["username"], "mirroring.room_removed", form.get("room_id", ""))
        return redirect_with(request, "/admin/mirroring", flash("Stopped mirroring that room."))
