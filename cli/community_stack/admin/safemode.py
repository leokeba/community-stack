"""Routes for the Safe Mode page of the admin console.

The console never reaches Docker or the Safe Mode database. Every read and change goes
through the control agent, so the same allowlist and audit trail apply as for Matrix users.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from community_stack.admin.common import flash
from community_stack.agent.client import AgentRefused, AgentUnavailable

STATE_LABELS = {
    "SUSPENDING": "Turning on",
    "SUSPENDED": "Suspended",
    "PARTIAL": "Partly suspended",
    "RESTORING": "Restoring",
}


def register_safemode(
    app: FastAPI, *, agent: Any, audit: Any, checked_form: Any, page: Any, render: Any, require: Any
) -> None:
    async def safemode_page(
        request: Request,
        session: dict[str, Any],
        outcome: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        overview, error = None, None
        if agent.available():
            try:
                overview = await run_in_threadpool(
                    agent.call, "safemode.overview", session["username"], {}, None
                )
            except (AgentUnavailable, AgentRefused) as exc:
                error = str(exc)
        return render(
            request,
            "safe_mode.html",
            page(
                session,
                agent_available=agent.available(),
                overview=overview,
                error=error,
                state_labels=STATE_LABELS,
                **({"flash": outcome} if outcome else {}),
            ),
            status_code=status_code,
        )

    @app.get("/admin/safe-mode")
    async def safe_mode(request: Request) -> Response:
        return await safemode_page(request, require(request, "system"))

    async def safemode_action(
        request: Request,
        op: str,
        title: str,
        fields: tuple[str, ...],
        *,
        password_required: bool = False,
        message_from: str | None = None,
    ) -> HTMLResponse:
        session, form = await checked_form(request, "system")
        params = {name: form.get(name, "").strip() for name in fields}
        params = {name: value for name, value in params.items() if value}
        password = form.get("password") or None
        if password_required and form.get("confirm") != "yes":
            return await safemode_page(
                request,
                session,
                flash("Confirm the change by ticking the box before continuing.", "error"),
                400,
            )
        try:
            result = await run_in_threadpool(agent.call, op, session["username"], params, password)
        except (AgentUnavailable, AgentRefused) as exc:
            audit(
                request,
                session["username"],
                f"system.{op}",
                params.get("user_id") or params.get("room_id"),
                outcome="failed",
            )
            return await safemode_page(request, session, flash(f"{title}: {exc}", "error"), 400)

        audit(
            request,
            session["username"],
            f"system.{op}",
            result.get("user_id") or result.get("room_id"),
            outcome="succeeded",
        )
        if message_from and result.get(message_from):
            text = str(result[message_from]).replace("\n", " · ")
        else:
            text = title
        message = flash(text if message_from is None else f"{title}: {text}")
        if op == "safemode.recovery_code" and result.get("code"):
            # Shown only in this response: never stored, logged or echoed again.
            message = flash(f"{title} for {result.get('user_id')}")
            message.update(
                secret=result["code"],
                secret_label="Recovery code, shown only now (give it to the member privately)",
            )
        return await safemode_page(request, session, message)

    @app.post("/admin/safe-mode/enroll")
    async def enroll(request: Request) -> Response:
        return await safemode_action(request, "safemode.enroll", "Enrolled", ("user_id",))

    @app.post("/admin/safe-mode/recovery-code")
    async def recovery_code(request: Request) -> Response:
        return await safemode_action(
            request, "safemode.recovery_code", "Recovery code issued", ("user_id",)
        )

    @app.post("/admin/safe-mode/status")
    async def status(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        user_id = form.get("user_id", "").strip()
        try:
            result = await run_in_threadpool(
                agent.call, "safemode.status", session["username"], {"user_id": user_id}, None
            )
        except (AgentUnavailable, AgentRefused) as exc:
            return await safemode_page(request, session, flash(f"Status: {exc}", "error"), 400)
        label = STATE_LABELS.get(result.get("state"), "Active (Safe Mode off)")
        return await safemode_page(
            request,
            session,
            flash(
                f"{result.get('user_id')}: {label}. Outstanding rooms: {result.get('outstanding', 0)}."
            ),
        )

    @app.post("/admin/safe-mode/protect")
    async def protect(request: Request) -> Response:
        return await safemode_action(
            request, "safemode.protect", "Room protected", ("room_id", "name")
        )

    @app.post("/admin/safe-mode/create-room")
    async def create_room(request: Request) -> Response:
        return await safemode_action(
            request,
            "safemode.create_room",
            "Protected room created",
            ("name", "invite"),
            message_from="room_id",
        )

    @app.post("/admin/safe-mode/unprotect")
    async def unprotect(request: Request) -> Response:
        return await safemode_action(
            request, "safemode.unprotect", "Room no longer protected", ("room_id",)
        )

    @app.post("/admin/safe-mode/doctor")
    async def doctor(request: Request) -> Response:
        return await safemode_action(
            request, "safemode.doctor", "Room check", (), message_from="report"
        )

    @app.post("/admin/safe-mode/restore")
    async def restore(request: Request) -> Response:
        return await safemode_action(
            request,
            "safemode.restore",
            "Account restored without its code",
            ("user_id",),
            password_required=True,
            message_from="report",
        )
