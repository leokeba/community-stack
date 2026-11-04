"""WhatsApp bridge setup pages: linking the account and finding the group, from the console."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from community_stack.admin.common import flash
from community_stack.agent.client import AgentRefused, AgentUnavailable
from community_stack.config import ConfigError, load_config


def register_whatsapp(
    app: FastAPI,
    *,
    agent: Any,
    audit: Any,
    checked_form: Any,
    config_path: Path | str,
    page: Any,
    publisher: Any,
    render: Any,
    require: Any,
    storage: Any,
) -> None:
    async def whatsapp_page(
        request: Request,
        session: dict[str, Any],
        outcome: dict[str, Any] | None = None,
        replies: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        try:
            config = load_config(config_path) if Path(config_path).is_file() else {}
        except ConfigError:
            config = {}
        bridge, error = None, None
        if agent.available():
            try:
                bridge = await run_in_threadpool(
                    agent.call, "bridge.whatsapp.status", session["username"]
                )
            except (AgentUnavailable, AgentRefused) as exc:
                error = str(exc)
        return render(
            request,
            "whatsapp.html",
            page(
                session,
                agent_available=agent.available(),
                bridge=bridge,
                bridge_error=error,
                bridge_enabled=publisher.whatsapp_bridge_enabled,
                matrix_connected=publisher.matrix_auth is not None,
                channel_connected="whatsapp" in storage.list_connections(),
                matrix_domain=config.get("domain", {}).get("matrix", "your Matrix server"),
                replies=replies,
                **({"flash": outcome} if outcome else {}),
            ),
            status_code=status_code,
        )

    @app.get("/admin/whatsapp")
    async def whatsapp_setup(request: Request) -> Response:
        return await whatsapp_page(request, require(request, "settings"))

    @app.post("/admin/whatsapp/command")
    async def whatsapp_command(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        command = form.get("command", "")
        try:
            result = await run_in_threadpool(
                agent.call,
                "bridge.whatsapp.command",
                session["username"],
                {"command": command},
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(
                request, session["username"], "bridge.whatsapp.command", command, outcome="failed"
            )
            return await whatsapp_page(
                request, session, flash(f"WhatsApp bridge: {exc}", "error"), status_code=400
            )
        # Replies can contain a linking QR code: they are shown to this administrator only,
        # and only the command name is written to the activity log.
        audit(request, session["username"], "bridge.whatsapp.command", command, outcome="succeeded")
        return await whatsapp_page(
            request,
            session,
            flash(f"WhatsApp bridge: sent '{command}'."),
            replies=result,
        )
