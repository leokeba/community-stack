"""Routes for the backups pages of the admin console."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from community_stack.admin.common import flash
from community_stack.agent.client import AgentRefused, AgentUnavailable
from community_stack.config import ConfigError, load_config
from community_stack.continuity.connect_page import offline_connect_files
from community_stack.continuity.offline_exporter import OfflineArchiveExporter


def register_backups(
    app: FastAPI,
    *,
    agent: Any,
    audit: Any,
    backups_page: Any,
    checked_form: Any,
    config_path: Any,
    redirect_with: Any,
    require: Any,
    storage: Any,
) -> None:
    @app.get("/admin/backups")
    async def backups(request: Request) -> Response:
        return await backups_page(request, require(request, "system"))

    async def backup_change(
        request: Request, op: str, title: str, fields: tuple[str, ...]
    ) -> HTMLResponse:
        session, form = await checked_form(request, "system")
        params = {name: form.get(name, "").strip() for name in fields if form.get(name, "").strip()}
        try:
            result = await run_in_threadpool(
                agent.call, op, session["username"], params, form.get("password") or None
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(request, session["username"], f"system.{op}", outcome="failed")
            return await backups_page(request, session, flash(f"{title}: {exc}", "error"), 400)
        audit(request, session["username"], f"system.{op}", outcome="succeeded")
        return redirect_with(request, "/admin/backups", flash(title, details=result["steps"]))

    @app.post("/admin/backups/offsite")
    async def backups_offsite(request: Request) -> Response:
        return await backup_change(
            request,
            "backups.use_offsite",
            "Off-site backups",
            ("endpoint", "bucket", "prefix", "region", "access_key_id", "secret_access_key"),
        )

    @app.post("/admin/backups/local")
    async def backups_local(request: Request) -> Response:
        return await backup_change(request, "backups.use_local", "Backups on this server", ())

    @app.get("/admin/backups/offline-archive.zip")
    async def offline_archive(request: Request) -> Response:
        session = require(request, "system")
        try:
            name = load_config(config_path)["project"]["name"]
        except (ConfigError, KeyError, OSError):
            name = "Community"
        connect_files, _warning = offline_connect_files(config_path, storage)
        with tempfile.TemporaryDirectory() as directory:
            path = OfflineArchiveExporter(storage=storage, community_name=name).export_archive(
                Path(directory) / "offline-announcements.zip", connect_files=connect_files
            )
            content = path.read_bytes()
        audit(request, session["username"], "announcements.offline_archive_downloaded")
        return Response(
            content,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="offline-announcements.zip"'},
        )
