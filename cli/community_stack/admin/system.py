"""Routes for the system pages of the admin console."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from community_stack.admin.common import flash
from community_stack.agent.client import AgentRefused, AgentUnavailable
from community_stack.agent.health import LOG_LINES, LOG_SERVICES


def register_system(
    app: FastAPI,
    *,
    agent: Any,
    audit: Any,
    backups_page: Any,
    checked_form: Any,
    page: Any,
    redirect_with: Any,
    render: Any,
    require: Any,
) -> None:
    async def run_system_operation(
        request: Request, session: dict[str, Any], op: str, title: str, password: str | None = None
    ) -> HTMLResponse:
        try:
            result = await run_in_threadpool(agent.call, op, session["username"], None, password)
        except (AgentUnavailable, AgentRefused) as exc:
            audit(request, session["username"], f"system.{op}", outcome="refused")
            return await backups_page(
                request, session, flash(f"{title} failed: {exc}", "error"), 400
            )
        audit(request, session["username"], f"system.{op}", outcome="succeeded")
        details = []
        if result.get("id"):
            encrypted = "encrypted" if result.get("encrypted") else "not encrypted"
            details.append(f"Snapshot {result['id']}, {encrypted}.")
        details += [f"Warning: {line}" for line in result.get("warnings") or []]
        details += list(result.get("messages") or []) + list(result.get("checks") or [])
        tone = "warning" if result.get("warnings") else "success"
        return redirect_with(request, "/admin/backups", flash(f"{title} finished.", tone, details))

    @app.get("/admin/system")
    async def system(request: Request) -> Response:
        # Its backup actions moved to Backups and its service list to Services.
        require(request, "system")
        return RedirectResponse("/admin/backups", status_code=303)

    @app.post("/admin/system/backup")
    async def system_backup(request: Request) -> Response:
        session, _ = await checked_form(request, "system")
        return await run_system_operation(request, session, "backup.create", "Backup")

    @app.post("/admin/system/verify")
    async def system_verify(request: Request) -> Response:
        session, _ = await checked_form(request, "system")
        return await run_system_operation(request, session, "backup.verify", "Backup check")

    @app.post("/admin/system/drill")
    async def system_drill(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        return await run_system_operation(
            request, session, "restore.drill", "Restore drill", form.get("password", "")
        )

    @app.get("/admin/health")
    async def health_page(request: Request) -> Response:
        session = require(request, "system")
        service = request.query_params.get("service")
        lines = request.query_params.get("lines", "200")
        health_result, logs, error = None, None, None
        if agent.available():
            try:
                health_result = await run_in_threadpool(
                    agent.call, "health.get", session["username"]
                )
                if service:
                    logs = await run_in_threadpool(
                        agent.call,
                        "logs.get",
                        session["username"],
                        {"service": service, "lines": lines},
                    )
            except (AgentUnavailable, AgentRefused) as exc:
                error = str(exc)
        return render(
            request,
            "health.html",
            page(
                session,
                agent_available=agent.available(),
                health=health_result,
                logs=logs,
                error=error,
                log_services=LOG_SERVICES,
                log_lines=LOG_LINES,
                selected_service=service,
                selected_lines=lines,
            ),
        )
