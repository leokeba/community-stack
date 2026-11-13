"""Routes for the services pages of the admin console."""

from __future__ import annotations

from typing import Any
from zoneinfo import available_timezones

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from community_stack.admin.common import flash
from community_stack.agent.client import AgentRefused, AgentUnavailable
from community_stack.agent.settings import SETTINGS


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
        (
            values,
            status,
            components,
            vikunja_url,
            facilmap_url,
            librebooking_url,
            field_map_url,
            error,
        ) = (
            None,
            None,
            [],
            None,
            None,
            None,
            None,
            None,
        )
        settings = []
        agent_outdated = False
        if agent.available():
            try:
                config = await run_in_threadpool(agent.call, "config.get", session["username"])
                values = config["values"]
                settings = [setting for setting in SETTINGS.values() if setting.key in values]
                agent_outdated = len(settings) != len(SETTINGS)
                vikunja_url = config.get("vikunja_url")
                facilmap_url = config.get("facilmap_url")
                librebooking_url = config.get("librebooking_url")
                field_map_url = config.get("field_map_url")
                status = await run_in_threadpool(agent.call, "status.get", session["username"])
                components = (
                    await run_in_threadpool(agent.call, "services.versions", session["username"])
                )["components"]
            except (AgentUnavailable, AgentRefused) as exc:
                error = str(exc)
        return render(
            request,
            "services.html",
            page(
                session,
                agent_available=agent.available(),
                settings=settings,
                agent_outdated=agent_outdated,
                values=values,
                status=status,
                components=components,
                vikunja_url=vikunja_url,
                facilmap_url=facilmap_url,
                librebooking_url=librebooking_url,
                field_map_url=field_map_url,
                timezones=sorted(available_timezones()),
                error=error,
                **({"flash": message} if message else {}),
            ),
            status_code=status_code,
        )

    def submitted_changes(form: dict[str, str]) -> dict[str, str]:
        return {key: form[f"setting:{key}"] for key in SETTINGS if f"setting:{key}" in form}

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
                request, session, flash(f"Updating {service} failed: {exc}", "error"), 400
            )
        audit(request, session["username"], "system.services.update", service, outcome="succeeded")
        return redirect_with(
            request, "/admin/services", flash(f"Updated {service}.", details=result["steps"])
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
                request, session, flash(f"LibreBooking setup details: {exc}", "error"), 400
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
        changes = submitted_changes(form)
        try:
            plan = await run_in_threadpool(
                agent.call, "config.plan", session["username"], {"changes": changes}
            )
        except (AgentUnavailable, AgentRefused) as exc:
            return await services_page(request, session, flash(str(exc), "error"), 400)
        if not plan["changes"]:
            return await services_page(
                request, session, flash("Nothing was changed.", "warning"), 400
            )
        applied = {change["key"]: form[f"setting:{change['key']}"] for change in plan["changes"]}
        return render(
            request, "services_review.html", page(session, plan=plan, changes=applied, result=None)
        )

    @app.post("/admin/services/apply")
    async def apply_services(request: Request) -> Response:
        session, form = await checked_form(request, "system")
        changes = submitted_changes(form)
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
                outcome="failed",
                settings=sorted(changes),
            )
            return render(
                request,
                "services_review.html",
                page(session, plan=None, changes=changes, result={"error": str(exc)}),
                status_code=400,
            )
        publisher.reload_connections()
        audit(
            request,
            session["username"],
            "system.config.apply",
            outcome="succeeded",
            settings=sorted(changes),
        )
        return render(
            request, "services_review.html", page(session, plan=None, changes={}, result=result)
        )
