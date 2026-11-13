"""Routes for the services pages of the admin console.

Every service in the agent's catalogue gets its own card and form. A form sends only that
service's settings, so each review and password prompt covers one service. The general
settings in the catalogue are shown the same way on the Community profile and Backups pages.
"""

from __future__ import annotations

from typing import Any
from zoneinfo import available_timezones

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from community_stack.admin.common import flash
from community_stack.agent.client import AgentRefused, AgentUnavailable
from community_stack.agent.service_catalogue import (
    SERVICES,
    SERVICES_BY_KEY,
    SETTINGS,
    ServiceSpec,
)


async def load_settings(agent: Any, username: str) -> tuple[dict[str, Any] | None, str | None]:
    """The agent's current setting values and app addresses, or why they are unavailable."""
    if not agent.available():
        return None, None
    try:
        return await run_in_threadpool(agent.call, "config.get", username), None
    except (AgentUnavailable, AgentRefused) as exc:
        return None, str(exc)


def settings_cards(
    page_name: str, config: dict[str, Any] | None, components: list[dict[str, Any]] = ()
) -> list[dict[str, Any]]:
    """One card per service shown on a page, with only the settings the agent supports.

    An older agent may not know every setting yet. Its unknown fields are left out, and a
    service whose on/off switch it does not know is left out entirely.
    """
    if not config:
        return []
    values = config["values"]
    cards = []
    for spec in SERVICES:
        if spec.page != page_name or (spec.enable_key and spec.enable_key not in values):
            continue
        fields = [setting for setting in spec.settings if setting.key in values]
        if not fields:
            continue
        rows = [row for row in components if spec.service and row["service"] == spec.service]
        cards.append(
            {
                "spec": spec,
                "values": values,
                "fields": [setting for setting in fields if not setting.advanced],
                "advanced": [setting for setting in fields if setting.advanced],
                "enabled": bool(values[spec.enable_key]) if spec.enable_key else None,
                "running": any(row["running"] for row in rows) if rows else None,
                "update_available": any(row["update_available"] for row in rows),
                "url": config.get(f"{spec.key}_url"),
                "components": rows,
            }
        )
    return cards


def register_services(
    app: FastAPI,
    *,
    agent: Any,
    audit: Any,
    checked_form: Any,
    page: Any,
    publisher: Any,
    redirect_with: Any,
    render: Any,
    require: Any,
) -> None:
    async def services_page(
        request: Request,
        session: dict[str, Any],
        message: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        config, error = await load_settings(agent, session["username"])
        status, components = None, []
        if config:
            try:
                status = await run_in_threadpool(agent.call, "status.get", session["username"])
                components = (
                    await run_in_threadpool(agent.call, "services.versions", session["username"])
                )["components"]
            except (AgentUnavailable, AgentRefused) as exc:
                error = str(exc)
        cards = settings_cards("services", config, components)
        groups: dict[str, list[dict[str, Any]]] = {}
        for card in cards:
            groups.setdefault(card["spec"].group, []).append(card)
        shown = {card["spec"].service for card in cards}
        return render(
            request,
            "services.html",
            page(
                session,
                agent_available=agent.available(),
                loaded=config is not None,
                agent_outdated=bool(config) and set(config["values"]) != set(SETTINGS),
                groups=list(groups.items()),
                core_components=[row for row in components if row["service"] not in shown],
                status=status,
                error=error,
                **({"flash": message} if message else {}),
            ),
            status_code=status_code,
        )

    def submitted_spec(form: dict[str, str]) -> ServiceSpec:
        spec = SERVICES_BY_KEY.get(form.get("service", ""))
        if spec is None:
            raise HTTPException(status_code=400, detail="Unknown service.")
        return spec

    def submitted_changes(form: dict[str, str], spec: ServiceSpec) -> dict[str, str]:
        """Only the settings of the service the form belongs to."""
        return {
            setting.key: form[f"setting:{setting.key}"]
            for setting in spec.settings
            if f"setting:{setting.key}" in form
        }

    @app.get("/admin/services")
    async def services(request: Request) -> Response:
        return await services_page(request, require(request, "system"))

    @app.post("/admin/services/update")
    async def update_service(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        service = form.get("service", "")
        try:
            result = await run_in_threadpool(
                agent.call, "services.update", session["username"], {"service": service}
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(request, session["username"], "system.services.update", service, outcome="failed")
            return await services_page(
                request,
                session,
                flash(f"Updating {service} failed: {exc}", "error", service=service),
                400,
            )
        audit(request, session["username"], "system.services.update", service, outcome="succeeded")
        return redirect_with(
            request,
            f"/admin/services#settings-{service}",
            flash(f"Updated {service}.", details=result["steps"], service=service),
        )

    @app.post("/admin/librebooking/setup")
    async def librebooking_setup(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        try:
            details = await run_in_threadpool(
                agent.call,
                "librebooking.setup_details",
                session["username"],
                {},
                form.get("password") or None,
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(
                request,
                session["username"],
                "system.librebooking.setup",
                outcome="failed",
            )
            return await services_page(
                request,
                session,
                flash(f"LibreBooking setup details: {exc}", "error", service="librebooking"),
                400,
            )
        audit(
            request,
            session["username"],
            "system.librebooking.setup",
            outcome="succeeded",
        )
        response = render(
            request,
            "librebooking_setup.html",
            page(session, details=details),
        )
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.post("/admin/services/review")
    async def review_services(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        spec = submitted_spec(form)
        changes = submitted_changes(form, spec)
        if not changes:
            return redirect_with(
                request, spec.return_url, flash("Nothing was changed.", "warning", service=spec.key)
            )
        try:
            plan = await run_in_threadpool(
                agent.call, "config.plan", session["username"], {"changes": changes}
            )
        except (AgentUnavailable, AgentRefused) as exc:
            return redirect_with(
                request, spec.return_url, flash(str(exc), "error", service=spec.key)
            )
        if not plan["changes"]:
            return redirect_with(
                request, spec.return_url, flash("Nothing was changed.", "warning", service=spec.key)
            )
        applied = {change["key"]: changes[change["key"]] for change in plan["changes"]}
        return render(
            request,
            "services_review.html",
            page(session, spec=spec, plan=plan, changes=applied, result=None),
        )

    @app.post("/admin/services/apply")
    async def apply_services(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        spec = submitted_spec(form)
        changes = submitted_changes(form, spec)
        try:
            result = await run_in_threadpool(
                agent.call,
                "config.apply",
                session["username"],
                {"changes": changes},
                form.get("password") or None,
            )
        except (AgentUnavailable, AgentRefused) as exc:
            audit(
                request,
                session["username"],
                "system.config.apply",
                spec.key,
                outcome="failed",
                settings=sorted(changes),
            )
            return render(
                request,
                "services_review.html",
                page(session, spec=spec, plan=None, changes=changes, result={"error": str(exc)}),
                status_code=400,
            )
        publisher.reload_connections()
        audit(
            request,
            session["username"],
            "system.config.apply",
            spec.key,
            outcome="succeeded",
            settings=sorted(changes),
        )
        return redirect_with(
            request,
            spec.return_url,
            flash(f"{spec.label}: changes applied.", details=result["steps"], service=spec.key),
        )


def timezone_names() -> list[str]:
    return sorted(available_timezones())
