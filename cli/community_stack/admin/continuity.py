"""Routes for the continuity pages of the admin console."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool

from community_stack.admin.common import flash
from community_stack.community import load_community
from community_stack.continuity.readiness import build_readiness_report
from community_stack.continuity.service import DEFAULT_ENGLISH_TEMPLATE


def register_continuity(
    app: FastAPI,
    *,
    audit: Any,
    auth_store: Any,
    checked_form: Any,
    config_path: Any,
    continuity_service: Any,
    page: Any,
    publisher: Any,
    redirect_with: Any,
    render: Any,
    require: Any,
    storage: Any,
) -> None:
    def continuity_translation_values(
        form: Any, languages: tuple[str, ...]
    ) -> dict[str, dict[str, str]]:
        translations = {}
        for language in languages:
            item = {
                key: str(form.get(f"{key}:{language}", "")).strip()
                for key in ("title", "body_template", "what_happened", "join_instructions")
            }
            if any(item.values()):
                translations[language] = item
        return translations

    def render_continuity_page(
        request: Request,
        session: dict[str, Any],
        status: dict[str, Any],
        form_values: dict[str, Any],
        plan: dict[str, Any] | None = None,
        error: str | None = None,
        status_code: int = 200,
    ) -> Response:
        profile, _config = load_community(config_path)
        translations = form_values.get("translations")
        if translations is None:
            current = status["current"]
            translations = (
                current["translations"]
                if current and current["active"]
                else {
                    language: (
                        DEFAULT_ENGLISH_TEMPLATE.copy()
                        if language == "en"
                        else {
                            "title": "",
                            "body_template": "",
                            "what_happened": "",
                            "join_instructions": "",
                        }
                    )
                    for language in profile.languages
                }
            )
        return render(
            request,
            "continuity.html",
            page(
                session,
                status=status,
                channel_options=continuity_service.channel_options(),
                languages=profile.languages,
                translations=translations,
                form_values=form_values,
                plan=plan,
                error=error,
            ),
            status_code=status_code,
        )

    @app.get("/admin/continuity")
    async def continuity_page(request: Request) -> Response:
        session = require(request, "settings")
        status = continuity_service.status()
        current = status["current"]
        return render_continuity_page(
            request,
            session,
            status,
            {
                "reason": current["reason"] if current and current["active"] else "",
                "affected": current["affected_channels"] if current and current["active"] else [],
            },
        )

    @app.post("/admin/continuity/plan")
    async def continuity_plan(request: Request) -> Response:
        session, form = await checked_form(request, "settings")
        submitted = await request.form()
        profile, _config = load_community(config_path)
        values = {
            "reason": form.get("reason", ""),
            "affected": submitted.getlist("affected"),
            "translations": continuity_translation_values(form, profile.languages),
        }
        try:
            plan = await run_in_threadpool(
                continuity_service.plan,
                values["reason"],
                values["affected"],
                values["translations"] or None,
            )
        except ValueError as exc:
            return render_continuity_page(
                request,
                session,
                continuity_service.status(),
                values,
                error=str(exc),
                status_code=400,
            )
        audit(
            request,
            session["username"],
            "continuity.plan",
            plan["event_id"],
            affected_channels=plan["affected_channels"],
        )
        return render_continuity_page(
            request, session, continuity_service.status(), values, plan=plan
        )

    @app.post("/admin/continuity/activate")
    async def continuity_activate(request: Request) -> Response:
        session, form = await checked_form(request, "settings")
        if not await run_in_threadpool(
            auth_store.verify_password, session["username"], form.get("password", "")
        ):
            raise HTTPException(status_code=403, detail="Password confirmation failed.")
        submitted = await request.form()
        profile, _config = load_community(config_path)
        values = {
            "reason": form.get("reason", ""),
            "affected": submitted.getlist("affected"),
            "translations": continuity_translation_values(form, profile.languages),
        }
        try:
            await run_in_threadpool(
                continuity_service.plan,
                values["reason"],
                values["affected"],
                values["translations"] or None,
            )
            result = await run_in_threadpool(
                continuity_service.activate,
                session["username"],
                values["reason"],
                values["affected"],
                values["translations"] or None,
            )
        except ValueError as exc:
            return render_continuity_page(
                request,
                session,
                continuity_service.status(),
                values,
                error=str(exc),
                status_code=400,
            )
        audit(
            request,
            session["username"],
            "continuity.activated",
            result["event_id"],
            affected_channels=values["affected"],
            steps=result["steps"],
        )
        failures = [
            f"{step}: {status}" for step, status in result["steps"].items() if status == "failed"
        ]
        message = (
            "Continuity mode is active. Failed steps can be resumed from this page."
            if failures
            else "Continuity mode is active."
        )
        return redirect_with(
            request,
            "/admin/continuity",
            flash(message, "warning" if failures else "success", failures),
        )

    @app.post("/admin/continuity/deactivate")
    async def continuity_deactivate(request: Request) -> Response:
        session, form = await checked_form(request, "settings")
        if not await run_in_threadpool(
            auth_store.verify_password, session["username"], form.get("password", "")
        ):
            raise HTTPException(status_code=403, detail="Password confirmation failed.")
        try:
            result = await run_in_threadpool(
                continuity_service.deactivate, session["username"], form.get("reason", "")
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(
            request,
            session["username"],
            "continuity.deactivated",
            result["event_id"],
            steps=result["steps"],
        )
        failures = [
            f"{step}: {status}" for step, status in result["steps"].items() if status == "failed"
        ]
        return redirect_with(
            request,
            "/admin/continuity",
            flash(
                "Continuity notice removed; event history is retained.",
                "warning" if failures else "success",
                failures,
            ),
        )

    @app.post("/admin/continuity/channel")
    async def continuity_channel_state(request: Request) -> Response:
        session, form = await checked_form(request, "settings")
        if not await run_in_threadpool(
            auth_store.verify_password, session["username"], form.get("password", "")
        ):
            raise HTTPException(status_code=403, detail="Password confirmation failed.")
        channel = form.get("channel", "")
        status = form.get("status", "")
        reason = form.get("reason", "")
        try:
            await run_in_threadpool(
                continuity_service.set_channel_state, channel, status, reason, session["username"]
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            audit(
                request,
                session["username"],
                "continuity.channel_state",
                channel,
                status=status,
                reason=reason,
                page_refresh="failed",
            )
            raise
        audit(
            request,
            session["username"],
            "continuity.channel_state",
            channel,
            status=status,
            reason=reason,
        )
        return redirect_with(
            request, "/admin/continuity", flash(f"{channel} is now marked {status}.")
        )

    @app.get("/admin/continuity/guide.zip")
    async def continuity_guide(request: Request) -> Response:
        session = require(request, "settings")
        if not continuity_service.guide_path.is_file():
            raise HTTPException(status_code=404, detail="No continuity guide is available.")
        audit(request, session["username"], "continuity.guide_downloaded")
        return FileResponse(
            continuity_service.guide_path,
            media_type="application/zip",
            filename="continuity-guide.zip",
        )

    @app.get("/admin/continuity/readiness")
    async def continuity_readiness(request: Request) -> Response:
        session = require(request, "settings")
        report = await run_in_threadpool(
            build_readiness_report, storage, publisher, config_path, continuity_service.guide_path
        )
        status = continuity_service.status()
        return render(
            request,
            "continuity_readiness.html",
            page(
                session,
                report=report,
                drill_channels=continuity_service.drill_channels(),
                exercises=status["exercises"],
            ),
        )

    @app.post("/admin/continuity/drill/start")
    async def continuity_drill_start(request: Request) -> Response:
        session, form = await checked_form(request, "settings")
        if not await run_in_threadpool(
            auth_store.verify_password, session["username"], form.get("password", "")
        ):
            raise HTTPException(status_code=403, detail="Password confirmation failed.")
        submitted = await request.form()
        try:
            exercise = await run_in_threadpool(
                continuity_service.start_drill,
                session["username"],
                form.get("scenario", ""),
                submitted.getlist("channel"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(
            request,
            session["username"],
            "continuity.drill_started",
            exercise["exercise_id"],
            channels=exercise["channels"],
            delivery=exercise["delivery"],
        )
        failed = [name for name, result in exercise["delivery"].items() if result == "failed"]
        return redirect_with(
            request,
            "/admin/continuity/readiness",
            flash(
                "Simulation published. Verify each selected channel and record the results.",
                "warning" if failed else "success",
                failed,
            ),
        )

    @app.post("/admin/continuity/drill/review")
    async def continuity_drill_review(request: Request) -> Response:
        session, form = await checked_form(request, "settings")
        if not await run_in_threadpool(
            auth_store.verify_password, session["username"], form.get("password", "")
        ):
            raise HTTPException(status_code=403, detail="Password confirmation failed.")
        try:
            exercise = await run_in_threadpool(
                continuity_service.review_drill,
                form.get("exercise_id", ""),
                session["username"],
                form.get("access_verified") == "yes",
                form.get("problems", ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(
            request,
            session["username"],
            "continuity.drill_reviewed",
            exercise["exercise_id"],
            status=exercise["status"],
            access_verified=exercise["access_verified"],
            problems=exercise["problems"],
        )
        return redirect_with(
            request,
            "/admin/continuity/readiness",
            flash(f"Exercise review recorded: {exercise['status']}."),
        )
