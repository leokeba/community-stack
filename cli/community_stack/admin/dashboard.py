"""Routes for the dashboard pages of the admin console."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from community_stack.admin.common import DASHBOARD_AGENT_TIMEOUT_SECONDS
from community_stack.admin.store import can
from community_stack.agent.client import AgentClient, AgentRefused, AgentUnavailable
from community_stack.announcements.models import AnnouncementStatus


def register_dashboard(
    app: FastAPI,
    *,
    agent: Any,
    channel_label: Any,
    page: Any,
    publisher: Any,
    render: Any,
    require: Any,
    setup_steps: Any,
    storage: Any,
) -> None:
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    status_agent = AgentClient(agent.socket_path, timeout=DASHBOARD_AGENT_TIMEOUT_SECONDS)

    async def service_status(session: dict[str, Any]) -> dict[str, Any] | None:
        """Running services for the dashboard strip, or why they are unknown."""
        if not can(session["role"], "system"):
            return None
        if not agent.available():
            return {"error": "System agent not running"}
        try:
            status = await run_in_threadpool(status_agent.call, "status.get", session["username"])
        except (AgentUnavailable, AgentRefused):
            return {"error": "System agent not answering"}
        if not status.get("docker_available"):
            return {"error": "Docker is not reachable"}
        return {"containers": status.get("containers") or []}

    @app.get("/admin")
    async def dashboard(request: Request) -> Response:
        session = require(request, "view")
        steps = setup_steps() if can(session["role"], "system") else []
        everything = storage.list_announcements(limit=None)
        month = datetime.now(publisher.timezone).strftime("%Y-%m")
        failed = storage.get_failed_deliveries()
        titles = {a.id: a.title for a in everything}
        failures: dict[str, dict[str, Any]] = {}
        for delivery in failed:
            entry = failures.setdefault(
                delivery["adapter_name"],
                {
                    "label": channel_label(delivery["adapter_name"]),
                    "count": 0,
                    "error": delivery.get("last_error"),
                    "announcement_id": delivery["announcement_id"],
                },
            )
            entry["count"] += 1
        failing = set(failures)
        enabled = storage.get_channel_settings()
        recent = everything[:8]
        return render(
            request,
            "dashboard.html",
            page(
                session,
                setup_done=sum(step["done"] for step in steps),
                setup_total=len(steps),
                next_step=next((step for step in steps if not step["done"]), None),
                announcements=recent,
                deliveries={a.id: storage.get_delivery_states(a.id) for a in recent},
                awaiting_review=[
                    a
                    for a in everything
                    if a.status == AnnouncementStatus.IN_REVIEW
                    and storage.last_submitter(a.id) != session["username"]
                ]
                if can(session["role"], "approve")
                else [],
                counts={
                    "published": sum(
                        1
                        for a in everything
                        if a.status in (AnnouncementStatus.PUBLISHED, AnnouncementStatus.UPDATED)
                        and (a.published_at or "").startswith(month)
                    ),
                    "scheduled": sum(a.status == AnnouncementStatus.SCHEDULED for a in everything),
                    "in_review": sum(a.status == AnnouncementStatus.IN_REVIEW for a in everything),
                },
                failed_count=len(failed),
                failures=list(failures.values()),
                jobs=[
                    {**job, "title": titles.get(job["announcement_id"], "")}
                    for job in storage.get_active_jobs()
                ],
                channels=[
                    {
                        "label": adapter.capabilities.label,
                        "state": "failing"
                        if adapter.name in failing
                        else ("on" if enabled.get(adapter.name, True) else "off"),
                    }
                    for adapter in publisher.adapters
                ],
                services=await service_status(session),
            ),
        )

    @app.get("/admin/start")
    async def start(request: Request) -> Response:
        session = require(request, "system")
        return render(request, "start.html", page(session, steps=setup_steps()))
