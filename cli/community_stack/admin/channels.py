"""Routes for the channels pages of the admin console."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from community_stack.admin.common import flash
from community_stack.announcements.adapters import http
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
        context = page(
            session,
            connectors=connectors,
            pixelfed_redirect_uri=(
                f"{publisher.base_url.rstrip('/')}/admin/channels/pixelfed/callback"
            ),
        )
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

    @app.post("/admin/channels/pixelfed/oauth/start")
    async def start_pixelfed_oauth(request: Request) -> Response:
        session, form = await checked_form(request, "settings")
        instance_url = form.get("instance_url", "").strip().rstrip("/")
        try:
            instance_url = await run_in_threadpool(http.assert_public_https, instance_url)
        except UnsafeURLError as exc:
            return channels_page(
                request, session, flash(f"Pixelfed: {exc}", "error"), status_code=400
            )

        redirect_uri = f"{publisher.base_url.rstrip('/')}/admin/channels/pixelfed/callback"
        status, body = await run_in_threadpool(
            http.send,
            "POST",
            f"{instance_url}/api/v1/apps",
            {"Accept": "application/json"},
            {
                "client_name": "Community Stack",
                "redirect_uris": redirect_uri,
                "scopes": "read write",
                "website": publisher.base_url,
            },
        )
        if (
            status not in (200, 201)
            or not isinstance(body, dict)
            or not body.get("client_id")
            or not body.get("client_secret")
        ):
            detail = http.error_text(status, body)
            return channels_page(
                request,
                session,
                flash(f"Pixelfed could not register an OAuth client: {detail}", "error"),
                status_code=502,
            )

        state = secrets.token_urlsafe(32)
        storage.create_oauth_state(
            state,
            {
                "client_id": str(body["client_id"]),
                "client_secret": str(body["client_secret"]),
                "csrf_token": session["csrf_token"],
                "instance_url": instance_url,
                "redirect_uri": redirect_uri,
                "username": session["username"],
            },
            (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        )
        authorization_parameters = urlencode(
            {
                "client_id": body["client_id"],
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "read write",
                "state": state,
            }
        )
        authorize_url = f"{instance_url}/oauth/authorize?{authorization_parameters}"
        response = RedirectResponse(authorize_url, status_code=303)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/admin/channels/pixelfed/callback")
    async def pixelfed_oauth_callback(request: Request) -> Response:
        session = require(request, "settings")
        state = request.query_params.get("state", "")
        flow = storage.consume_oauth_state(state) if state else None
        if not flow or not (
            flow.get("username") == session["username"]
            and secrets.compare_digest(flow.get("csrf_token", ""), session["csrf_token"])
        ):
            return channels_page(
                request,
                session,
                flash(
                    "Pixelfed authorization expired or could not be verified. Please retry.",
                    "error",
                ),
                status_code=400,
            )

        provider_error = request.query_params.get("error")
        if provider_error:
            return redirect_with(
                request,
                "/admin/channels#channel-pixelfed",
                flash(f"Pixelfed authorization was not completed: {provider_error[:200]}", "error"),
            )
        code = request.query_params.get("code", "")
        if not code or len(code) > 4096:
            return channels_page(
                request,
                session,
                flash("Pixelfed did not return an authorization code. Please retry.", "error"),
                status_code=400,
            )

        status, body = await run_in_threadpool(
            http.send,
            "POST",
            f"{flow['instance_url']}/oauth/token",
            {"Accept": "application/json"},
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": flow["client_id"],
                "client_secret": flow["client_secret"],
                "redirect_uri": flow["redirect_uri"],
            },
        )
        access_token = body.get("access_token") if isinstance(body, dict) else None
        if status != 200 or not isinstance(access_token, str):
            detail = http.error_text(status, body)
            return channels_page(
                request,
                session,
                flash(f"Pixelfed could not issue an access token: {detail}", "error"),
                status_code=502,
            )
        try:
            values = CONNECTORS["pixelfed"].validate(
                {"instance_url": flow["instance_url"], "access_token": access_token}
            )
        except (UnsafeURLError, ValueError) as exc:
            return channels_page(
                request,
                session,
                flash(f"Pixelfed returned an unusable access token: {exc}", "error"),
                status_code=502,
            )

        was_connected = "pixelfed" in storage.list_connections()
        storage.save_connection("pixelfed", values, session["username"])
        publisher.reload_connections()
        audit(
            request,
            session["username"],
            "channel.updated" if was_connected else "channel.connected",
            "pixelfed",
        )
        response = redirect_with(
            request,
            "/admin/channels#channel-pixelfed",
            flash("Pixelfed connected. Use 'Test connection' to check it."),
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

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
