"""Routes for the templates pages of the admin console."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response

from community_stack.admin.common import flash
from community_stack.announcements.models import Announcement, ModelValidationError


def register_templates(
    app: FastAPI,
    *,
    announcement_or_404: Any,
    as_editor_values: Any,
    audit: Any,
    checked_form: Any,
    editor_page: Any,
    page: Any,
    parse_channel_text: Any,
    parse_tags: Any,
    redirect_with: Any,
    render: Any,
    require: Any,
    storage: Any,
    template_or_404: Any,
) -> None:
    def template_from_form(form: dict[str, str], template_id: str | None = None) -> dict:
        name = " ".join(form.get("name", "").split())
        if not name or len(name) > 80:
            raise ModelValidationError("Give the template a name of up to 80 characters.")
        template = {
            "id": template_id,
            "name": name,
            "title": form.get("title", "").strip(),
            "body": form.get("body", "").strip(),
            "author": form.get("author") or None,
            "tags": parse_tags(form),
            "channel_text": parse_channel_text(form),
        }
        # Templates must be usable as announcements, so they follow the same rules.
        Announcement(
            title=template["title"], body=template["body"], channel_text=template["channel_text"]
        )
        return template

    @app.get("/admin/templates")
    async def templates_page(request: Request) -> Response:
        session = require(request, "edit")
        return render(
            request, "templates.html", page(session, templates=storage.list_templates(), error=None)
        )

    @app.get("/admin/templates/new")
    async def new_template_page(request: Request) -> Response:
        session = require(request, "edit")
        context = editor_page(session, None, None)
        context.update(template_mode=True, form_action="/admin/templates/new")
        return render(request, "editor.html", context)

    @app.post("/admin/templates/new")
    async def create_template(request: Request) -> Response:
        session, form = await checked_form(request, "edit")
        try:
            template_id = storage.save_template(template_from_form(form), session["username"])
        except ModelValidationError as exc:
            context = editor_page(session, form, str(exc))
            context.update(template_mode=True, form_action="/admin/templates/new")
            return render(request, "editor.html", context, status_code=400)
        audit(request, session["username"], "template.created", template_id, name=form.get("name"))
        return redirect_with(request, "/admin/templates", flash("Template saved."))

    @app.get("/admin/templates/{template_id}/edit")
    async def edit_template_page(request: Request, template_id: str) -> Response:
        session = require(request, "edit")
        context = editor_page(session, as_editor_values(template_or_404(template_id)), None)
        context.update(template_mode=True, form_action=f"/admin/templates/{template_id}/update")
        return render(request, "editor.html", context)

    @app.post("/admin/templates/{template_id}/update")
    async def update_template(request: Request, template_id: str) -> Response:
        session, form = await checked_form(request, "edit")
        template_or_404(template_id)
        try:
            storage.save_template(template_from_form(form, template_id), session["username"])
        except ModelValidationError as exc:
            context = editor_page(session, form, str(exc))
            context.update(template_mode=True, form_action=f"/admin/templates/{template_id}/update")
            return render(request, "editor.html", context, status_code=400)
        audit(request, session["username"], "template.updated", template_id, name=form.get("name"))
        return redirect_with(request, "/admin/templates", flash("Template saved."))

    @app.post("/admin/templates/{template_id}/delete")
    async def delete_template(request: Request, template_id: str) -> Response:
        session, _ = await checked_form(request, "edit")
        template = template_or_404(template_id)
        storage.delete_template(template_id)
        audit(request, session["username"], "template.deleted", template_id, name=template["name"])
        return redirect_with(
            request, "/admin/templates", flash(f"Deleted the template {template['name']}.")
        )

    @app.post("/admin/announcements/{announcement_id}/save-as-template")
    async def save_as_template(request: Request, announcement_id: str) -> Response:
        session, form = await checked_form(request, "edit")
        announcement = announcement_or_404(announcement_id)
        name = " ".join(form.get("name", "").split())[:80] or announcement.title[:80]
        template_id = storage.save_template(
            {
                "name": name,
                "title": announcement.title,
                "body": announcement.body,
                "author": announcement.author,
                "tags": announcement.tags,
                "channel_text": announcement.channel_text,
            },
            session["username"],
        )
        audit(request, session["username"], "template.created", template_id, name=name)
        return redirect_with(request, "/admin/templates", flash(f"Saved as the template {name}."))
