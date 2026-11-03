"""Vikunja account creation in the admin console."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from community_stack.admin.common import flash
from community_stack.agent.client import AgentRefused, AgentUnavailable


def register_vikunja_users(
    app: FastAPI,
    *,
    agent: Any,
    audit: Any,
    checked_form: Any,
    page: Any,
    render: Any,
    require: Any,
) -> None:
    async def vikunja_users_page(
        request: Request,
        session: dict[str, Any],
        outcome: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        enabled = False
        supported = False
        vikunja_url = None
        error = None
        if agent.available():
            try:
                config = await run_in_threadpool(agent.call, "config.get", session["username"])
                values = config.get("values", {})
                supported = "services.vikunja.enabled" in values
                enabled = bool(values.get("services.vikunja.enabled", False))
                vikunja_url = config.get("vikunja_url")
            except (AgentUnavailable, AgentRefused) as exc:
                error = str(exc)
        return render(
            request,
            "vikunja_users.html",
            page(
                session,
                agent_available=agent.available(),
                supported=supported,
                enabled=enabled,
                vikunja_url=vikunja_url,
                error=error,
                **({"flash": outcome} if outcome else {}),
            ),
            status_code=status_code,
        )

    @app.get("/admin/vikunja-users")
    async def vikunja_users(request: Request) -> Response:
        return await vikunja_users_page(request, require(request, "system"))

    @app.post("/admin/vikunja-users/create")
    async def create_vikunja_user(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        username = form.get("username", "").strip()
        email = form.get("email", "").strip()
        try:
            result = await run_in_threadpool(
                agent.call,
                "vikunja.users.create",
                session["username"],
                {"username": username, "email": email},
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(
                request,
                session["username"],
                "system.vikunja.users.create",
                username or None,
                outcome="failed",
            )
            return await vikunja_users_page(
                request, session, flash(f"Creating the Vikunja account failed: {exc}", "error"), 400
            )

        audit(
            request,
            session["username"],
            "system.vikunja.users.create",
            username,
            outcome="succeeded",
        )
        temporary_password = result.get("temporary_password")
        if not isinstance(temporary_password, str) or not temporary_password:
            audit(
                request,
                session["username"],
                "system.vikunja.users.create",
                username,
                outcome="failed",
            )
            return await vikunja_users_page(
                request,
                session,
                flash(
                    "The account command completed, but no temporary password was returned. "
                    "Check Vikunja before retrying.",
                    "error",
                ),
                500,
            )
        message = flash(f"Vikunja account created for {username}.")
        message.update(
            secret=temporary_password,
            secret_label="Temporary password, shown only now",
        )
        response = await vikunja_users_page(request, session, message)
        response.headers["Cache-Control"] = "no-store"
        return response
