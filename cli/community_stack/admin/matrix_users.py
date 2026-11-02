"""Routes for the matrix users pages of the admin console."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from community_stack.admin.common import flash
from community_stack.agent.client import AgentRefused, AgentUnavailable


def register_matrix_users(
    app: FastAPI, *, agent: Any, audit: Any, checked_form: Any, page: Any, render: Any, require: Any
) -> None:
    async def matrix_users_page(
        request: Request,
        session: dict[str, Any],
        outcome: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        listing, error = None, None
        if agent.available():
            try:
                listing = await run_in_threadpool(
                    agent.call, "matrix.users.list", session["username"]
                )
            except (AgentUnavailable, AgentRefused) as exc:
                error = str(exc)
        return render(
            request,
            "matrix_users.html",
            page(
                session,
                agent_available=agent.available(),
                listing=listing,
                error=error,
                **({"flash": outcome} if outcome else {}),
            ),
            status_code=status_code,
        )

    @app.get("/admin/matrix-users")
    async def matrix_users(request: Request) -> Response:
        return await matrix_users_page(request, require(request, "system"))

    async def matrix_user_action(
        request: Request, op: str, title: str, fields: tuple[str, ...]
    ) -> HTMLResponse:
        session, form = await checked_form(request, "system")
        params = {name: form.get(name, "").strip() for name in fields if form.get(name, "").strip()}
        try:
            result = await run_in_threadpool(
                agent.call, op, session["username"], params, form.get("password") or None
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(
                request,
                session["username"],
                f"system.{op}",
                params.get("user_id") or params.get("username"),
                outcome="failed",
            )
            return await matrix_users_page(request, session, flash(f"{title}: {exc}", "error"), 400)
        # Temporary passwords are shown once and never written to the activity log or
        # stored as a message: this response is the only place they appear.
        audit(
            request, session["username"], f"system.{op}", result.get("user_id"), outcome="succeeded"
        )
        message = flash(f"{title}: {result.get('user_id')}")
        if result.get("temporary_password"):
            message.update(
                secret=result["temporary_password"],
                secret_label="Temporary password, shown only now",
            )
        return await matrix_users_page(request, session, message)

    @app.post("/admin/matrix-users/create")
    async def create_matrix_user(request: Request) -> Response:
        return await matrix_user_action(
            request, "matrix.users.create", "New Matrix account", ("username", "display_name")
        )

    @app.post("/admin/matrix-users/reset-password")
    async def reset_matrix_password(request: Request) -> Response:
        return await matrix_user_action(
            request, "matrix.users.reset_password", "New temporary password", ("user_id",)
        )

    @app.post("/admin/matrix-users/deactivate")
    async def deactivate_matrix_user(request: Request) -> Response:
        return await matrix_user_action(
            request, "matrix.users.deactivate", "Account deactivated", ("user_id",)
        )
