"""Routes for the announcements pages of the admin console."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from markupsafe import Markup
from starlette.concurrency import run_in_threadpool

from community_stack.admin.common import flash
from community_stack.admin.store import tokens_match
from community_stack.announcements.adapters.manual import ManualChannelAdapter
from community_stack.announcements.formatting import render_html
from community_stack.announcements.media import (
    MAX_UPLOAD_BYTES,
    MEDIA_ID,
    MediaError,
    process_image,
    validate_alt,
)
from community_stack.announcements.models import (
    UNPUBLISHED,
    Announcement,
    AnnouncementStatus,
    ModelValidationError,
)
from community_stack.announcements.platforms import PLATFORM_CATALOG, PLATFORMS
from community_stack.announcements.publisher import ManualOutcomeConflict


def register_announcements(
    app: FastAPI,
    *,
    announcement_or_404: Any,
    as_editor_values: Any,
    audit: Any,
    auth_store: Any,
    checked_form: Any,
    delivery_message: Any,
    editor_page: Any,
    page: Any,
    parse_channel_text: Any,
    parse_tags: Any,
    publisher: Any,
    redirect_with: Any,
    render: Any,
    require: Any,
    require_approved: Any,
    review_required: Any,
    storage: Any,
    template_or_404: Any,
) -> None:
    ANNOUNCEMENT_FILTERS = {
        "draft": ("Drafts", (AnnouncementStatus.DRAFT, AnnouncementStatus.APPROVED)),
        "in_review": ("In review", (AnnouncementStatus.IN_REVIEW,)),
        "scheduled": ("Scheduled", (AnnouncementStatus.SCHEDULED,)),
        "published": ("Published", (AnnouncementStatus.PUBLISHED, AnnouncementStatus.UPDATED)),
        "withdrawn": ("Withdrawn", (AnnouncementStatus.WITHDRAWN,)),
    }

    @app.get("/admin/announcements")
    async def announcement_list(request: Request) -> Response:
        session = require(request, "view")
        everything = storage.list_announcements(limit=None)
        selected = request.query_params.get("status", "")
        if selected not in ANNOUNCEMENT_FILTERS:
            selected = ""
        filters = [{"key": "", "label": "All", "count": len(everything)}] + [
            {"key": key, "label": label, "count": sum(a.status in states for a in everything)}
            for key, (label, states) in ANNOUNCEMENT_FILTERS.items()
        ]
        shown = (
            [a for a in everything if a.status in ANNOUNCEMENT_FILTERS[selected][1]]
            if selected
            else everything
        )
        return render(
            request,
            "announcements.html",
            page(session, announcements=shown, filters=filters, selected=selected),
        )

    @app.get("/admin/announcements/new")
    async def new_announcement_page(request: Request) -> Response:
        session = require(request, "edit")
        template_id = request.query_params.get("template")
        prefill = as_editor_values(template_or_404(template_id)) if template_id else None
        context = editor_page(session, prefill, None)
        context["templates"] = storage.list_templates()
        return render(request, "editor.html", context)

    @app.post("/admin/announcements/new")
    async def create_announcement(request: Request) -> Response:
        session, form = await checked_form(request, "edit")
        try:
            announcement = publisher.create_draft(
                title=form.get("title", ""),
                body=form.get("body", ""),
                author=form.get("author") or None,
                tags=parse_tags(form),
                channel_text=parse_channel_text(form),
            )
        except ModelValidationError as exc:
            return render(
                request, "editor.html", editor_page(session, form, str(exc)), status_code=400
            )
        audit(
            request,
            session["username"],
            "announcement.created",
            announcement.id,
            title=announcement.title,
        )
        return redirect_with(
            request, f"/admin/announcements/{announcement.id}", flash("Draft saved.")
        )

    def editable_announcement(announcement_id: str):
        try:
            announcement = publisher.preview(announcement_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        editable = announcement.status == AnnouncementStatus.DRAFT or (
            announcement.status == AnnouncementStatus.SCHEDULED and not review_required()
        )
        if not editable:
            raise HTTPException(
                status_code=409,
                detail="Images can only be changed on drafts (edit the announcement to reopen it).",
            )
        return announcement

    @app.post("/admin/announcements/{announcement_id}/image")
    async def upload_image(request: Request, announcement_id: str) -> Response:
        session = require(request, "edit")
        if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES + 65536:
            raise HTTPException(status_code=413, detail="Images must be 10 MB or smaller.")
        form = await request.form()
        if not tokens_match(session["csrf_token"], str(form.get("csrf_token", ""))):
            raise HTTPException(status_code=403, detail="Invalid or expired CSRF token.")
        editable_announcement(announcement_id)
        upload = form.get("image")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="Choose an image to upload.")
        try:
            alt = validate_alt(str(form.get("alt", "")))
            data = await upload.read(MAX_UPLOAD_BYTES + 1)
            processed = await run_in_threadpool(process_image, data)
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        image = storage.save_image(announcement_id, processed, alt, session["username"])
        audit(
            request,
            session["username"],
            "announcement.image_added",
            announcement_id,
            width=image.width,
            height=image.height,
        )
        return redirect_with(
            request, f"/admin/announcements/{announcement_id}", flash("Image added.")
        )

    @app.post("/admin/announcements/{announcement_id}/image/alt")
    async def change_image_alt(request: Request, announcement_id: str) -> Response:
        session, form = await checked_form(request, "edit")
        announcement = editable_announcement(announcement_id)
        if not announcement.image:
            raise HTTPException(status_code=404, detail="This announcement has no image.")
        try:
            storage.set_image_alt(announcement.image.id, validate_alt(form.get("alt", "")))
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(request, session["username"], "announcement.image_described", announcement_id)
        return redirect_with(
            request, f"/admin/announcements/{announcement_id}", flash("Image description saved.")
        )

    @app.post("/admin/announcements/{announcement_id}/image/remove")
    async def remove_image(request: Request, announcement_id: str) -> Response:
        session, _ = await checked_form(request, "edit")
        editable_announcement(announcement_id)
        storage.remove_image(announcement_id)
        audit(request, session["username"], "announcement.image_removed", announcement_id)
        return redirect_with(
            request, f"/admin/announcements/{announcement_id}", flash("Image removed.")
        )

    @app.post("/admin/announcements/{announcement_id}/channel-image")
    async def upload_channel_image(request: Request, announcement_id: str) -> Response:
        session = require(request, "edit")
        if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES + 65536:
            raise HTTPException(status_code=413, detail="Images must be 10 MB or smaller.")
        form = await request.form()
        if not tokens_match(session["csrf_token"], str(form.get("csrf_token", ""))):
            raise HTTPException(status_code=403, detail="Invalid or expired CSRF token.")
        editable_announcement(announcement_id)
        channel = str(form.get("channel", ""))
        if channel not in PLATFORMS:
            raise HTTPException(status_code=400, detail="Choose a supported platform.")
        upload = form.get("image")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="Choose an image to upload.")
        try:
            alt = validate_alt(str(form.get("alt", "")))
            data = await upload.read(MAX_UPLOAD_BYTES + 1)
            processed = await run_in_threadpool(process_image, data)
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        image = storage.save_channel_image(
            announcement_id, channel, processed, alt, session["username"]
        )
        audit(
            request,
            session["username"],
            "announcement.channel_image_added",
            announcement_id,
            channel=channel,
            width=image.width,
            height=image.height,
        )
        return redirect_with(
            request,
            f"/admin/announcements/{announcement_id}",
            flash(f"{PLATFORMS[channel].name} image saved."),
        )

    @app.post("/admin/announcements/{announcement_id}/channel-image/{channel}/alt")
    async def change_channel_image_alt(
        request: Request, announcement_id: str, channel: str
    ) -> Response:
        session, form = await checked_form(request, "edit")
        if channel not in PLATFORMS:
            raise HTTPException(status_code=404)
        announcement = editable_announcement(announcement_id)
        image = announcement.channel_media.get(channel)
        if image is None:
            raise HTTPException(status_code=404, detail="This platform has no image override.")
        try:
            storage.set_image_alt(image.id, validate_alt(form.get("alt", "")))
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(
            request,
            session["username"],
            "announcement.channel_image_described",
            announcement_id,
            channel=channel,
        )
        return redirect_with(
            request,
            f"/admin/announcements/{announcement_id}",
            flash(f"{PLATFORMS[channel].name} image description saved."),
        )

    @app.post("/admin/announcements/{announcement_id}/channel-image/{channel}/remove")
    async def remove_channel_image(
        request: Request, announcement_id: str, channel: str
    ) -> Response:
        session, _ = await checked_form(request, "edit")
        if channel not in PLATFORMS:
            raise HTTPException(status_code=404)
        editable_announcement(announcement_id)
        if not storage.remove_channel_image(announcement_id, channel):
            raise HTTPException(status_code=404, detail="This platform has no image override.")
        audit(
            request,
            session["username"],
            "announcement.channel_image_removed",
            announcement_id,
            channel=channel,
        )
        return redirect_with(
            request,
            f"/admin/announcements/{announcement_id}",
            flash(f"{PLATFORMS[channel].name} image override removed."),
        )

    @app.get("/admin/media/{media_id}")
    async def media(request: Request, media_id: str) -> Response:
        require(request, "view")
        image = storage.get_media(media_id) if MEDIA_ID.fullmatch(media_id) else None
        if image is None or not image.path.is_file():
            raise HTTPException(status_code=404, detail="Image not found.")
        return FileResponse(
            image.path,
            media_type=image.content_type,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.post("/admin/preview")
    async def preview(request: Request) -> Response:
        _, form = await checked_form(request, "edit")
        try:
            draft = Announcement(
                title=form.get("title", "").strip() or "(no title yet)",
                body=form.get("body", "").strip() or "(no message yet)",
                author=form.get("author") or None,
                channel_text=parse_channel_text(form),
            )
        except ModelValidationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(
            {
                "html": render_html(draft.body),
                "channels": [
                    {
                        "name": check.name,
                        "label": check.capabilities.label,
                        "length": check.text_length,
                        "limit": check.capabilities.max_text_length,
                        "over_limit": check.over_limit,
                        "shortened": check.over_limit and check.capabilities.shortens_long_text,
                        "preview": check.preview,
                    }
                    for check in publisher.channel_checks(draft)
                ],
            }
        )

    @app.get("/admin/announcements/{announcement_id}")
    async def announcement_detail(request: Request, announcement_id: str) -> Response:
        session = require(request, "view")
        try:
            announcement = publisher.preview(announcement_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        scheduled_local = (
            datetime.fromisoformat(announcement.scheduled_at)
            .astimezone(publisher.timezone)
            .strftime("%A %d %B %Y, %H:%M")
            if announcement.scheduled_at
            else None
        )
        delivery_states = storage.get_delivery_states(announcement_id)
        delivery_by_channel = {item["adapter_name"]: item for item in delivery_states}
        adapters_by_name = {
            adapter.name: adapter for adapter in publisher.adapters if adapter.name in PLATFORMS
        }
        platform_content = []
        for platform in PLATFORM_CATALOG:
            adapter = adapters_by_name.get(platform.identifier)
            delivery = delivery_by_channel.get(platform.identifier)
            operation = delivery["operation"] if delivery else None
            manual_action = {
                "manual_publish": "publish",
                "manual_update": "update",
                "manual_withdraw": "withdraw",
            }.get(operation)
            if adapter is None and manual_action is None:
                continue
            if adapter is None:
                adapter = ManualChannelAdapter(platform)
            if manual_action is None and isinstance(adapter, ManualChannelAdapter):
                manual_action = (
                    operation if operation in ("publish", "update", "withdraw") else None
                )
            if announcement.status == AnnouncementStatus.WITHDRAWN and not (
                manual_action == "withdraw"
                and delivery
                and delivery["status"] in ("prepared", "failed", "unknown")
            ):
                continue
            variant = publisher.channel_variant(announcement, platform.identifier)
            check = adapter.check(variant)
            platform_content.append(
                {
                    "spec": platform,
                    "text": check.preview,
                    "image": variant.image,
                    "check": check,
                    "delivery": delivery,
                    "manual": isinstance(adapter, ManualChannelAdapter)
                    or manual_action is not None,
                    "manual_action": manual_action,
                }
            )
        return render(
            request,
            "detail.html",
            page(
                session,
                announcement=announcement,
                review_required=review_required(),
                submitter=storage.last_submitter(announcement_id),
                review_history=storage.review_history(announcement_id),
                body_html=Markup(render_html(announcement.body)),
                scheduled_local=scheduled_local,
                timezone_name=str(publisher.timezone),
                now_local=datetime.now(publisher.timezone).strftime("%Y-%m-%dT%H:%M"),
                deliveries=delivery_states,
                jobs=storage.get_active_jobs(announcement_id),
                checks=publisher.channel_checks(announcement),
                platform_content=platform_content,
                platform_media=[
                    {
                        "identifier": platform.identifier,
                        "name": platform.name,
                        "image": announcement.channel_media.get(platform.identifier),
                    }
                    for platform in PLATFORM_CATALOG
                ],
                delivery_attempts=storage.get_delivery_attempts(announcement_id),
            ),
        )

    @app.get("/admin/announcements/{announcement_id}/edit")
    async def edit_announcement_page(request: Request, announcement_id: str) -> Response:
        session = require(request, "edit")
        try:
            announcement = publisher.preview(announcement_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        if announcement.status.value == "withdrawn":
            raise HTTPException(status_code=409, detail="Withdrawn announcements cannot be edited.")
        return render(request, "editor.html", editor_page(session, announcement, None))

    @app.post("/admin/announcements/{announcement_id}/update")
    async def update_announcement(request: Request, announcement_id: str) -> Response:
        session, form = await checked_form(request, "edit")
        existing = announcement_or_404(announcement_id)
        before = existing.status
        try:
            announcement, _ = publisher.update(
                announcement_id,
                title=form.get("title"),
                body=form.get("body"),
                author=form.get("author") or None,
                tags=parse_tags(form) if "tags" in form else None,
                channel_text=parse_channel_text(form, existing),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        except (ValueError, ModelValidationError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        message = flash("Changes saved.")
        if review_required() and before in (
            AnnouncementStatus.IN_REVIEW,
            AnnouncementStatus.APPROVED,
            AnnouncementStatus.SCHEDULED,
        ):
            # What gets published must be exactly what was approved.
            publisher.set_review_status(announcement_id, (before,), AnnouncementStatus.DRAFT)
            storage.add_review_event(
                announcement_id, "returned_to_draft", session["username"], "Edited after review."
            )
            message = flash(
                "Changes saved. It changed after review, so it is a draft again and needs a new review.",
                "warning",
            )
        audit(
            request,
            session["username"],
            "announcement.updated",
            announcement_id,
            title=announcement.title,
        )
        return redirect_with(request, f"/admin/announcements/{announcement_id}", message)

    @app.post("/admin/announcements/{announcement_id}/publish")
    async def publish_announcement(request: Request, announcement_id: str) -> Response:
        session, form = await checked_form(request, "edit")
        if form.get("confirm") != "publish":
            raise HTTPException(
                status_code=400, detail="Explicit publish confirmation is required."
            )
        require_approved(announcement_or_404(announcement_id))
        try:
            announcement, results = publisher.publish_draft(announcement_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit(
            request,
            session["username"],
            "announcement.published",
            announcement_id,
            title=announcement.title,
            channels=sorted(results),
        )
        return redirect_with(
            request,
            f"/admin/announcements/{announcement_id}",
            delivery_message("Published.", results),
        )

    @app.post("/admin/announcements/{announcement_id}/channels/{channel}/outcome")
    async def record_manual_outcome(
        request: Request, announcement_id: str, channel: str
    ) -> Response:
        session, form = await checked_form(request, "edit")
        if channel not in PLATFORMS:
            raise HTTPException(status_code=404)
        outcome = form.get("outcome", "")
        try:
            manual_action, outcome, recorded_post_url = publisher.record_manual_outcome(
                announcement_id,
                channel,
                outcome,
                recorded_by=session["username"],
                post_url=form.get("post_url"),
                note=form.get("note"),
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Announcement or platform not found."
            ) from None
        except ManualOutcomeConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(
            request,
            session["username"],
            "announcement.manual_outcome_recorded",
            announcement_id,
            channel=channel,
            outcome=outcome,
            post_url=recorded_post_url,
        )
        return redirect_with(
            request,
            f"/admin/announcements/{announcement_id}",
            flash(f"{PLATFORMS[channel].name} manual {manual_action} recorded as {outcome}."),
        )

    @app.post("/admin/announcements/{announcement_id}/submit")
    async def submit_for_review(request: Request, announcement_id: str) -> Response:
        session, _ = await checked_form(request, "edit")
        try:
            announcement = publisher.set_review_status(
                announcement_id, (AnnouncementStatus.DRAFT,), AnnouncementStatus.IN_REVIEW
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        storage.add_review_event(announcement_id, "submitted", session["username"])
        audit(
            request,
            session["username"],
            "announcement.submitted",
            announcement_id,
            title=announcement.title,
        )
        return redirect_with(
            request,
            f"/admin/announcements/{announcement_id}",
            flash("Submitted for review. Someone else needs to approve it."),
        )

    async def review_decision(request: Request, announcement_id: str, approve: bool) -> Response:
        session, form = await checked_form(request, "approve")
        announcement_or_404(announcement_id)
        if storage.last_submitter(announcement_id) == session["username"]:
            raise HTTPException(
                status_code=403, detail="Someone other than the submitter must review it."
            )
        note = " ".join(form.get("note", "").split())[:1000] or None
        if not approve and not note:
            raise HTTPException(status_code=400, detail="Say what needs to change.")
        target = AnnouncementStatus.APPROVED if approve else AnnouncementStatus.DRAFT
        try:
            announcement = publisher.set_review_status(
                announcement_id, (AnnouncementStatus.IN_REVIEW,), target
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        action = "approved" if approve else "changes_requested"
        storage.add_review_event(announcement_id, action, session["username"], note)
        audit(
            request,
            session["username"],
            f"announcement.{action}",
            announcement_id,
            title=announcement.title,
        )
        text = (
            "Approved. It can now be published or scheduled."
            if approve
            else "Sent back for changes, with your note."
        )
        return redirect_with(request, f"/admin/announcements/{announcement_id}", flash(text))

    @app.post("/admin/announcements/{announcement_id}/approve")
    async def approve_announcement(request: Request, announcement_id: str) -> Response:
        return await review_decision(request, announcement_id, approve=True)

    @app.post("/admin/announcements/{announcement_id}/request-changes")
    async def request_changes(request: Request, announcement_id: str) -> Response:
        return await review_decision(request, announcement_id, approve=False)

    @app.post("/admin/announcements/{announcement_id}/publish-urgently")
    async def publish_urgently(request: Request, announcement_id: str) -> Response:
        """Safety notices that cannot wait for a reviewer: owners and admins, with a reason."""
        session, form = await checked_form(request, "settings")
        announcement = announcement_or_404(announcement_id)
        if announcement.status not in UNPUBLISHED:
            raise HTTPException(status_code=409, detail="This announcement is already public.")
        reason = " ".join(form.get("reason", "").split())[:500]
        if len(reason) < 10:
            raise HTTPException(status_code=400, detail="Explain why this cannot wait for review.")
        if not await run_in_threadpool(
            auth_store.verify_password, session["username"], form.get("password", "")
        ):
            raise HTTPException(status_code=403, detail="Password confirmation failed.")
        try:
            announcement, results = publisher.publish_draft(announcement_id)
        except ValueError:
            # Submitted or in review: claim it from whatever unpublished state it is in.
            publisher.set_review_status(
                announcement_id, (AnnouncementStatus.IN_REVIEW,), AnnouncementStatus.APPROVED
            )
            announcement, results = publisher.publish_draft(announcement_id)
        storage.add_review_event(
            announcement_id, "published_without_review", session["username"], reason
        )
        audit(
            request,
            session["username"],
            "announcement.published_urgently",
            announcement_id,
            title=announcement.title,
            reason=reason,
            channels=sorted(results),
        )
        return redirect_with(
            request,
            f"/admin/announcements/{announcement_id}",
            delivery_message(
                "Published without review. Your reason is in the activity log.", results
            ),
        )

    @app.post("/admin/settings/review")
    async def update_review_setting(request: Request) -> Response:
        session, form = await checked_form(request, "settings")
        enabled = form.get("review_required") == "true"
        storage.set_setting("review_required", "true" if enabled else "false")
        audit(request, session["username"], "settings.review", required=enabled)
        state = "on" if enabled else "off"
        return redirect_with(
            request, "/admin/settings", flash(f"Review before publishing is now {state}.")
        )

    @app.post("/admin/announcements/{announcement_id}/schedule")
    async def schedule_announcement(request: Request, announcement_id: str) -> Response:
        session, form = await checked_form(request, "edit")
        current = announcement_or_404(announcement_id)
        if review_required() and current.status != AnnouncementStatus.APPROVED:
            raise HTTPException(
                status_code=409, detail="Review is on: this announcement must be approved first."
            )
        try:
            local = datetime.strptime(form.get("scheduled_for", ""), "%Y-%m-%dT%H:%M")
        except ValueError:
            raise HTTPException(status_code=400, detail="Choose a date and time.") from None
        try:
            announcement = publisher.schedule(
                announcement_id, local.replace(tzinfo=publisher.timezone)
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit(
            request,
            session["username"],
            "announcement.scheduled",
            announcement_id,
            title=announcement.title,
            scheduled_at=announcement.scheduled_at,
        )
        when = local.strftime("%A %d %B %Y, %H:%M")
        return redirect_with(
            request,
            f"/admin/announcements/{announcement_id}",
            flash(f"Scheduled for {when} ({publisher.timezone})."),
        )

    @app.post("/admin/announcements/{announcement_id}/unschedule")
    async def unschedule_announcement(request: Request, announcement_id: str) -> Response:
        session, _ = await checked_form(request, "edit")
        try:
            announcement = publisher.unschedule(announcement_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit(
            request,
            session["username"],
            "announcement.unscheduled",
            announcement_id,
            title=announcement.title,
        )
        return redirect_with(
            request,
            f"/admin/announcements/{announcement_id}",
            flash("Schedule cancelled. The announcement is a draft again."),
        )

    @app.post("/admin/announcements/{announcement_id}/withdraw")
    async def withdraw_announcement(request: Request, announcement_id: str) -> Response:
        session, form = await checked_form(request, "edit")
        if form.get("confirm") != "withdraw":
            raise HTTPException(
                status_code=400, detail="Explicit withdrawal confirmation is required."
            )
        try:
            announcement, results = publisher.withdraw(announcement_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Announcement not found.") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit(
            request,
            session["username"],
            "announcement.withdrawn",
            announcement_id,
            title=announcement.title,
        )
        return redirect_with(
            request,
            f"/admin/announcements/{announcement_id}",
            delivery_message("Withdrawn from public channels.", results),
        )

    @app.post("/admin/announcements/{announcement_id}/retry")
    async def retry_announcement(request: Request, announcement_id: str) -> Response:
        session, _ = await checked_form(request, "edit")
        results = publisher.retry_failed(announcement_id)
        audit(
            request,
            session["username"],
            "announcement.retried",
            announcement_id,
            channels=sorted(key.split(":", 1)[1] for key in results),
        )
        text = f"Retried {len(results)} failed deliveries." if results else "Nothing to retry."
        return redirect_with(request, f"/admin/announcements/{announcement_id}", flash(text))
