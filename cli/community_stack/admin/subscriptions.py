"""Routes for the subscriptions pages of the admin console."""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from community_stack.admin.common import PREAUTH_CSRF_COOKIE, PREFERENCE_TOPICS, flash, logger
from community_stack.announcements.adapters.email_newsletter import EmailNewsletterAdapter
from community_stack.announcements.sms import (
    CODES_PER_GLOBAL_DAY,
    SmsGatewayAdapter,
    SmsSignup,
    normalize_phone,
)
from community_stack.community import connection_warnings, load_community
from community_stack.config import ConfigError
from community_stack.subscriptions.email_signup import (
    EMAIL_CONSENT_TEXT,
    EMAIL_CONSENT_VERSION,
    PRIVACY_NOTICE_REVIEWED,
    SIGNUP_SOURCE,
    confirmation_message,
    read_unsubscribe_token,
    test_message,
)
from community_stack.subscriptions.email_transport import TransportError
from community_stack.subscriptions.manager import (
    Subscriber,
    SubscriberManager,
    validate_destination,
)


def register_subscriptions(
    app: FastAPI,
    *,
    audit: Any,
    checked_form: Any,
    client_host: Any,
    community_name: Any,
    config_path: Any,
    page: Any,
    preauth_csrf_or_forbidden: Any,
    preauth_csrf_token: Any,
    publisher: Any,
    redirect_with: Any,
    render: Any,
    require: Any,
    set_cookie: Any,
    storage: Any,
    submitted_form: Any,
) -> None:
    # Public SMS sign-up: every join and leave is confirmed with a texted code.

    def sms_adapter():
        return next((a for a in publisher.adapters if isinstance(a, SmsGatewayAdapter)), None)

    def sms_signup() -> SmsSignup:
        secret = storage.get_setting("sms_code_secret", "")
        if not secret:
            secret = secrets.token_hex(32)
            storage.set_setting("sms_code_secret", secret)
        return SmsSignup(storage.db_path.parent / "subscribers.db", secret)

    def alert_sms_signup_limit(request: Request, signup: SmsSignup) -> None:
        if signup.global_limit_alert_needed:
            audit(request, None, "sms.signup_daily_limit_reached", limit=CODES_PER_GLOBAL_DAY)
            logger.warning("Daily SMS sign-up code limit reached (limit=%s).", CODES_PER_GLOBAL_DAY)

    def subscribe_page(request: Request, step: str, status_code: int = 200, **context) -> Response:
        csrf_token = preauth_csrf_token(request)
        response = render(
            request,
            "subscribe.html",
            {
                "csrf_token": csrf_token,
                "step": step,
                "available": sms_adapter() is not None,
                "community": community_name(),
                "error": None,
                "number": "",
                **context,
            },
            status_code=status_code,
        )
        set_cookie(response, csrf_token, PREAUTH_CSRF_COOKIE, path="/")
        return response

    @app.get("/subscribe")
    async def subscribe_form(request: Request) -> Response:
        return subscribe_page(request, "join")

    @app.get("/subscribe/stop")
    async def unsubscribe_form(request: Request) -> Response:
        return subscribe_page(request, "leave")

    @app.post("/subscribe/{purpose}/code")
    async def subscribe_send_code(request: Request, purpose: str) -> Response:
        if purpose not in ("join", "leave"):
            raise HTTPException(status_code=404)
        form = await submitted_form(request)
        preauth_csrf_or_forbidden(request, form)
        adapter = sms_adapter()
        if adapter is None:
            raise HTTPException(status_code=404)
        # Bots fill in every field, including this invisible one.
        if form.get("website"):
            return subscribe_page(request, "code", purpose=purpose, number="")
        if purpose == "join" and form.get("consent") != "yes":
            return subscribe_page(request, purpose, 400, error="Please tick the consent box.")
        signup = sms_signup()
        try:
            number = normalize_phone(form.get("number", ""))
            code = signup.new_code(number, purpose, client_host(request))
            alert_sms_signup_limit(request, signup)
        except ValueError as exc:
            alert_sms_signup_limit(request, signup)
            return subscribe_page(request, purpose, 400, error=str(exc))
        text = f"{code} is your code to {'join' if purpose == 'join' else 'leave'} {community_name()} alerts."
        await run_in_threadpool(
            adapter.send, [number], text, f"code:{number}:{secrets.token_hex(4)}"
        )
        return subscribe_page(request, "code", purpose=purpose, number=number)

    @app.post("/subscribe/{purpose}/confirm")
    async def subscribe_confirm(request: Request, purpose: str) -> Response:
        if purpose not in ("join", "leave"):
            raise HTTPException(status_code=404)
        form = await submitted_form(request)
        preauth_csrf_or_forbidden(request, form)
        adapter = sms_adapter()
        if adapter is None:
            raise HTTPException(status_code=404)
        try:
            number = normalize_phone(form.get("number", ""))
        except ValueError as exc:
            return subscribe_page(request, purpose, 400, error=str(exc))
        if not sms_signup().confirm(number, purpose, form.get("code", "")):
            return subscribe_page(
                request,
                "code",
                400,
                purpose=purpose,
                number=number,
                error="That code did not work. Check it, or ask for a new one later.",
            )
        if purpose == "join":
            adapter.subscribers.add_subscriber("sms", number)
        else:
            adapter.subscribers.unsubscribe("sms", number)
        return subscribe_page(request, "joined" if purpose == "join" else "left")

    # Email sign-up (CR2.6, CR2.7). Confirmation and unsubscribe links are safe to open:
    # only a button press changes anything, so mail scanners cannot sign people up or out.

    def email_adapter() -> EmailNewsletterAdapter | None:
        return next((a for a in publisher.adapters if isinstance(a, EmailNewsletterAdapter)), None)

    def email_signup_page(
        request: Request, step: str, status_code: int = 200, **context
    ) -> Response:
        adapter = email_adapter()
        csrf_token = preauth_csrf_token(request)
        response = render(
            request,
            "subscribe_email.html",
            {
                "csrf_token": csrf_token,
                "step": step,
                "available": adapter is not None and adapter.unsubscribe_secret is not None,
                "community": community_name(),
                "consent_text": EMAIL_CONSENT_TEXT,
                "error": None,
                "email": "",
                "token": "",
                "masked": "",
                "reviewed": PRIVACY_NOTICE_REVIEWED,
                "contact": "",
                **context,
            },
            status_code=status_code,
        )
        set_cookie(response, csrf_token, PREAUTH_CSRF_COOKIE, path="/")
        return response

    def unsubscribe_secret_bytes() -> bytes | None:
        secret = storage.get_setting("subscriber_token_secret", "")
        return secret.encode() if secret else None

    @app.get("/subscribe/email/privacy")
    async def email_privacy_notice(request: Request) -> Response:
        try:
            profile, _config = load_community(config_path)
            contact = profile.contact_email or ""
        except (ConfigError, OSError):
            contact = ""
        return email_signup_page(
            request, "privacy", reviewed=PRIVACY_NOTICE_REVIEWED, contact=contact
        )

    @app.get("/subscribe/email")
    async def email_signup_form(request: Request) -> Response:
        return email_signup_page(request, "form")

    @app.post("/subscribe/email")
    async def email_signup_submit(request: Request) -> Response:
        form = await submitted_form(request)
        preauth_csrf_or_forbidden(request, form)
        adapter = email_adapter()
        if adapter is None or adapter.unsubscribe_secret is None or not PRIVACY_NOTICE_REVIEWED:
            raise HTTPException(status_code=404)
        # Bots fill in every field, including this invisible one. They get the same answer as people.
        if form.get("website"):
            return email_signup_page(request, "check")
        address = form.get("email", "").strip()
        if form.get("consent") != "yes":
            return email_signup_page(
                request, "form", 400, email=address, error="Please tick the consent box."
            )
        subscribers: SubscriberManager = adapter.subscriber_manager
        try:
            subscribers.validate_destination("email", address)
        except ValueError:
            return email_signup_page(
                request, "form", 400, email=address, error="Enter a valid email address."
            )
        client_key = hashlib.sha256(
            (client_host(request) + (unsubscribe_secret_bytes() or b"").decode()).encode()
        ).hexdigest()
        if not subscribers.allow_signup(client_key, address):
            return email_signup_page(
                request,
                "form",
                429,
                email=address,
                error="Too many sign-up requests. Please try again later.",
            )
        preference = form.get("preference", "all")
        if preference not in PREFERENCE_TOPICS:
            return email_signup_page(
                request, "form", 400, email=address, error="Choose what to receive."
            )
        subscribers.register_consent_text(EMAIL_CONSENT_VERSION, EMAIL_CONSENT_TEXT)
        _subscriber, token = subscribers.request_subscription(
            "email",
            address,
            consent_version=EMAIL_CONSENT_VERSION,
            consent_source=SIGNUP_SOURCE,
            topics=PREFERENCE_TOPICS[preference],
        )
        if token is not None:
            message = confirmation_message(
                adapter.from_address,
                address,
                community_name(),
                f"{adapter.public_base_url}/subscribe/email/confirm?t={token}",
            )
            try:
                await run_in_threadpool(adapter.transport.send, message)
            except TransportError:
                logger.warning("Could not send an email sign-up confirmation.")
                return email_signup_page(
                    request,
                    "form",
                    503,
                    email=address,
                    error="We could not send the confirmation email. Please try again later.",
                )
        # The same answer whether or not the address is already subscribed.
        return email_signup_page(request, "check")

    @app.get("/subscribe/email/confirm")
    async def email_confirm_form(request: Request) -> Response:
        token = request.query_params.get("t", "")
        if not token or email_adapter() is None:
            return email_signup_page(request, "invalid", 400)
        return email_signup_page(request, "confirm", token=token)

    # Not /confirm: the SMS route POST /subscribe/{purpose}/confirm is declared first and would match.
    @app.post("/subscribe/email/activate")
    async def email_confirm_submit(request: Request) -> Response:
        form = await submitted_form(request)
        preauth_csrf_or_forbidden(request, form)
        adapter = email_adapter()
        if adapter is None:
            raise HTTPException(status_code=404)
        confirmed = adapter.subscriber_manager.confirm_subscription(form.get("t", ""))
        if confirmed is None:
            return email_signup_page(
                request,
                "invalid",
                400,
                error="This link has expired or was already used. Sign up again for a new one.",
            )
        return email_signup_page(request, "subscribed")

    @app.get("/subscribe/email/unsubscribe")
    async def email_unsubscribe_form(request: Request) -> Response:
        token = request.query_params.get("t", "")
        secret = unsubscribe_secret_bytes()
        address = read_unsubscribe_token(secret, token) if secret and token else None
        if address is None or email_adapter() is None:
            return email_signup_page(request, "invalid", 400)
        masked = Subscriber(channel="email", destination=address).mask_destination()
        return email_signup_page(request, "unsubscribe", token=token, masked=masked)

    @app.post("/subscribe/email/unsubscribe")
    async def email_unsubscribe_submit(request: Request) -> Response:
        # No CSRF cookie here: mail clients send one-click unsubscribes without it (RFC 8058).
        # The signed token in the link is the proof of authority.
        token = request.query_params.get("t", "")
        secret = unsubscribe_secret_bytes()
        address = read_unsubscribe_token(secret, token) if secret and token else None
        adapter = email_adapter()
        if address is None or adapter is None:
            return email_signup_page(request, "invalid", 400)
        adapter.subscriber_manager.unsubscribe("email", address)
        return email_signup_page(request, "unsubscribed")

    # Subscribers console (CR2.10, CR2.13). Owners and admins only.

    def subscriber_records() -> tuple[dict[str, int], list[dict[str, str]]]:
        manager = SubscriberManager(storage.db_path.parent / "subscribers.db")
        email = manager.export_records()
        counts = {
            "email_active": sum(1 for r in email if r["status"] == "active"),
            "email_pending": sum(1 for r in email if r["status"] == "pending"),
            "email_unsubscribed": sum(1 for r in email if r["status"] == "unsubscribed"),
            "sms_active": len(manager.list_subscribers(channel="sms")),
        }
        rows = []
        for record in email:
            consent = record["consent_source"] or "not recorded"
            if record["consent_source"] == "legacy":
                consent = "recorded before consent tracking"
            elif record["consent_at"]:
                consent = f"{record['consent_at'][:10]}, {record['consent_source']}"
            rows.append(
                {
                    "address": record["destination"],
                    "masked": Subscriber(
                        channel="email", destination=record["destination"]
                    ).mask_destination(),
                    "status": record["status"],
                    "consent": consent,
                    "topics": ", ".join(record["topics"]) or "everything",
                }
            )
        return counts, rows

    @app.get("/admin/subscribers")
    async def subscribers_page(request: Request) -> Response:
        session = require(request, "settings")
        counts, records = subscriber_records()
        return render(
            request,
            "subscribers.html",
            page(
                session,
                counts=counts,
                records=records,
                reviewed=PRIVACY_NOTICE_REVIEWED,
                warnings=connection_warnings(storage.list_connections()),
            ),
        )

    @app.get("/admin/subscribers/export")
    async def subscribers_export(request: Request) -> Response:
        session = require(request, "settings")
        manager = SubscriberManager(storage.db_path.parent / "subscribers.db")
        body = json.dumps(
            {"format": "community-stack/subscribers", "records": manager.export_records()}, indent=2
        )
        audit(request, session["username"], "subscribers.exported")
        return Response(
            body,
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="subscribers-export.json"'},
        )

    @app.post("/admin/subscribers/erase")
    async def subscribers_erase(request: Request) -> Response:
        session, form = await checked_form(request, "settings")
        address = form.get("address", "").strip().lower()
        masked = Subscriber(channel="email", destination=address).mask_destination()
        if form.get("confirm") != "yes":
            raise HTTPException(status_code=400, detail="Tick the box to confirm erasure.")
        manager = SubscriberManager(storage.db_path.parent / "subscribers.db")
        erased = manager.delete_subscriber("email", address)
        audit(
            request,
            session["username"],
            "subscribers.erased",
            masked,
            outcome="succeeded" if erased else "not_found",
        )
        message = (
            flash(f"Erased everything held about {masked}.")
            if erased
            else flash("No record was found for that address.", "warning")
        )
        return redirect_with(request, "/admin/subscribers", message)

    @app.post("/admin/subscribers/test-email")
    async def subscribers_test_email(request: Request) -> Response:
        session, form = await checked_form(request, "settings")
        address = form.get("to", "").strip()
        masked = Subscriber(channel="email", destination=address).mask_destination()
        try:
            validate_destination("email", address)
        except ValueError:
            return redirect_with(
                request, "/admin/subscribers", flash("Enter a valid email address.", "error")
            )
        adapter = email_adapter()
        if adapter is None or adapter.unsubscribe_secret is None:
            return redirect_with(
                request,
                "/admin/subscribers",
                flash("No email server is connected. Connect one on the Channels page.", "error"),
            )
        message = test_message(
            adapter.from_address, address, community_name(), adapter.unsubscribe_url(address)
        )
        try:
            await run_in_threadpool(adapter.transport.send, message)
        except TransportError as exc:
            audit(request, session["username"], "subscribers.test_sent", masked, outcome="failed")
            return redirect_with(
                request, "/admin/subscribers", flash(f"The test was not sent: {exc}", "error")
            )
        audit(request, session["username"], "subscribers.test_sent", masked, outcome="succeeded")
        return redirect_with(
            request,
            "/admin/subscribers",
            flash(f"Test email sent to {masked}. Check the inbox and spam folder."),
        )
