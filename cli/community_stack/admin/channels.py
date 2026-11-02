"""Routes for the channels pages of the admin console."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from community_stack.admin.common import flash
from community_stack.announcements.adapters.http import UnsafeURLError
from community_stack.announcements.adapters.signal import SIGNAL_API, signal_send
from community_stack.announcements.connectors import CONNECTORS, build_connector, merge_values
from community_stack.config import ConfigError, load_config


def register_channels(
    app: FastAPI,
    *,
    audit: Any,
    checked_form: Any,
    config_path: Any,
    page: Any,
    publisher: Any,
    redirect_with: Any,
    render: Any,
    require: Any,
    storage: Any,
) -> None:
    def channels_page(
        request: Request,
        session: dict[str, Any],
        message: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        """The channels page; `message` (an error from a form) replaces the stored flash."""
        saved = storage.list_connections()
        enabled = storage.get_channel_settings()
        connectors = [
            {
                "type": connector,
                "connected": kind in saved,
                "enabled": enabled.get(kind, True),
                "available": kind != "whatsapp"
                or (publisher.whatsapp_bridge_enabled and publisher.matrix_auth is not None),
                "values": {
                    field.name: saved.get(kind, {}).get(field.name, "")
                    for field in connector.fields
                    if not field.secret
                },
            }
            for kind, connector in CONNECTORS.items()
        ]
        context = page(session, connectors=connectors)
        if message:
            context["flash"] = message
        return render(request, "channels.html", context, status_code=status_code)

    def connector_for(kind: str):
        connector = CONNECTORS.get(kind)
        if connector is None:
            raise HTTPException(status_code=404, detail="Unknown channel type.")
        return connector

    @app.get("/admin/channels")
    async def channels(request: Request) -> Response:
        return channels_page(request, require(request, "settings"))

    @app.post("/admin/channels/{kind}/save")
    async def save_channel(request: Request, kind: str) -> Response:
        session, form = await checked_form(request, "settings")
        connector = connector_for(kind)
        if kind == "whatsapp" and not publisher.whatsapp_bridge_enabled:
            return channels_page(
                request,
                session,
                flash("Enable the WhatsApp bridge first on the Services page.", "error"),
                409,
            )
        if kind == "whatsapp" and publisher.matrix_auth is None:
            return channels_page(
                request,
                session,
                flash(
                    "Connect Matrix announcements first; WhatsApp forwarding uses that account.",
                    "error",
                ),
                409,
            )
        saved = storage.list_connections().get(kind, {})
        try:
            values = await run_in_threadpool(
                connector.validate, merge_values(connector, form, saved)
            )
        except (UnsafeURLError, ValueError) as exc:
            return channels_page(request, session, flash(f"{connector.label}: {exc}", "error"), 400)
        storage.save_connection(kind, values, session["username"])
        publisher.reload_connections()
        public = {f.name: values.get(f.name) for f in connector.fields if not f.secret}
        audit(
            request,
            session["username"],
            "channel.updated" if saved else "channel.connected",
            kind,
            **public,
        )
        return redirect_with(
            request,
            f"/admin/channels#channel-{kind}",
            flash(f"{connector.label} saved. Use 'Test connection' to check it."),
        )

    @app.get("/admin/channels/signal/link")
    async def signal_link(request: Request) -> Response:
        session = require(request, "settings")
        status, accounts = await run_in_threadpool(
            signal_send, "GET", f"{SIGNAL_API}/v1/accounts", {}, None
        )
        groups, error = [], None
        if status != 200 or not isinstance(accounts, list):
            error = "The Signal service is not running. Turn on Signal on the Services page first."
            accounts = []
        for account in accounts:
            number = account.get("number") or account.get("aci")
            g_status, found = await run_in_threadpool(
                signal_send, "GET", f"{SIGNAL_API}/v1/groups/{quote(number, safe='')}", {}, None
            )
            for group in found if g_status == 200 and isinstance(found, list) else []:
                groups.append({"number": number, "id": group.get("id"), "name": group.get("name")})
        return render(
            request,
            "signal_link.html",
            page(session, accounts=accounts, groups=groups, error=error),
        )

    @app.get("/admin/channels/signal/qr")
    async def signal_qr(request: Request) -> Response:
        require(request, "settings")
        status, image = await run_in_threadpool(
            signal_send,
            "GET",
            f"{SIGNAL_API}/v1/qrcodelink?device_name=Community%20Stack",
            {},
            None,
        )
        if status != 200 or not isinstance(image, bytes):
            raise HTTPException(status_code=503, detail="The Signal service is not running.")
        return Response(image, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/admin/channels/signal/use")
    async def signal_use_group(request: Request) -> Response:
        session, form = await checked_form(request, "settings")
        try:
            values = CONNECTORS["signal"].validate(form)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        storage.save_connection("signal", values, session["username"])
        publisher.reload_connections()
        audit(
            request, session["username"], "channel.connected", "signal", group=values["group_name"]
        )
        return redirect_with(
            request,
            "/admin/channels#channel-signal",
            flash(f"Announcements will be posted to the Signal group {values['group_name']}."),
        )

    @app.post("/admin/channels/{kind}/test")
    async def test_channel(request: Request, kind: str) -> Response:
        session, _ = await checked_form(request, "settings")
        connector = connector_for(kind)
        if kind == "whatsapp" and not (
            publisher.whatsapp_bridge_enabled and publisher.matrix_auth is not None
        ):
            raise HTTPException(
                status_code=409,
                detail="Enable the WhatsApp bridge and connect Matrix announcements first.",
            )
        values = storage.list_connections().get(kind)
        if values is None:
            raise HTTPException(status_code=409, detail="This channel is not connected.")
        try:
            adapter = next((a for a in publisher.adapters if a.name == kind), None)
            if adapter is None:
                adapter = build_connector(kind, values, publisher.matrix_auth)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        result = await run_in_threadpool(adapter.health_check)
        audit(request, session["username"], "channel.tested", kind, ok=result.ok)
        return redirect_with(
            request,
            f"/admin/channels#channel-{kind}",
            flash(f"{connector.label}: {result.details}", "success" if result.ok else "error"),
        )

    @app.post("/admin/channels/{kind}/disconnect")
    async def disconnect_channel(request: Request, kind: str) -> Response:
        session, _ = await checked_form(request, "settings")
        connector = connector_for(kind)
        storage.delete_connection(kind)
        publisher.reload_connections()
        audit(request, session["username"], "channel.disconnected", kind)
        return redirect_with(
            request,
            f"/admin/channels#channel-{kind}",
            flash(f"{connector.label} disconnected. Its saved credentials were deleted."),
        )

    @app.get("/admin/whatsapp")
    async def whatsapp_setup(request: Request) -> Response:
        session = require(request, "settings")
        try:
            config = load_config(config_path) if Path(config_path).is_file() else {}
        except ConfigError:
            config = {}
        return render(
            request,
            "whatsapp.html",
            page(
                session,
                bridge_enabled=publisher.whatsapp_bridge_enabled,
                matrix_connected=publisher.matrix_auth is not None,
                channel_connected="whatsapp" in storage.list_connections(),
                matrix_domain=config.get("domain", {}).get("matrix", "your Matrix server"),
            ),
        )
