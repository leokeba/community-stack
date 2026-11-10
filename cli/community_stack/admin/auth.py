"""Routes for the auth pages of the admin console."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from community_stack.admin.common import (
    ANY_USERNAME,
    LOGIN_IP_LIMIT,
    PREAUTH_CSRF_COOKIE,
    SECOND_STEP_COOKIE,
    SESSION_COOKIE,
    flash,
)
from community_stack.admin.store import tokens_match


def register_auth(
    app: FastAPI,
    *,
    audit: Any,
    auth_store: Any,
    authenticated: Any,
    client_host: Any,
    csrf_or_forbidden: Any,
    login_redirect: Any,
    preauth_csrf_or_forbidden: Any,
    preauth_csrf_token: Any,
    render: Any,
    set_cookie: Any,
    setup_token: Any,
    submitted_form: Any,
) -> None:
    @app.get("/admin/setup")
    async def setup_page(request: Request) -> Response:
        if auth_store.has_admin():
            return login_redirect()
        csrf_token = preauth_csrf_token(request)
        response = render(
            request,
            "setup.html",
            {"csrf_token": csrf_token, "setup_available": bool(setup_token), "error": None},
        )
        set_cookie(response, csrf_token, PREAUTH_CSRF_COOKIE)
        return response

    @app.post("/admin/setup")
    async def setup_admin(request: Request) -> Response:
        if auth_store.has_admin():
            return login_redirect()
        form = await submitted_form(request)
        preauth_csrf_or_forbidden(request, form)
        host = client_host(request)
        if auth_store.is_rate_limited(host, "setup"):
            raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
        if not tokens_match(setup_token, form.get("setup_token")):
            auth_store.record_failed_attempt(host, "setup")
            raise HTTPException(status_code=403, detail="Administrator setup token is invalid.")
        username = form.get("username", "").strip()
        try:
            await run_in_threadpool(auth_store.create_admin, username, form.get("password", ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        auth_store.clear_failed_attempts(host, "setup")
        audit(request, username, "setup.completed", username, role="owner")
        auth_store.delete_session(request.cookies.get(SESSION_COOKIE))
        token, _ = auth_store.new_session(username)
        auth_store.set_flash(
            token, flash(f"Welcome, {username}. Next, work through the setup checklist.")
        )
        response = RedirectResponse("/admin", status_code=303)
        set_cookie(response, token)
        return response

    @app.get("/admin/login")
    async def login_page(request: Request) -> Response:
        csrf_token = preauth_csrf_token(request)
        response = render(request, "login.html", {"csrf_token": csrf_token, "error": None})
        set_cookie(response, csrf_token, PREAUTH_CSRF_COOKIE)
        return response

    @app.post("/admin/login")
    async def login(request: Request) -> Response:
        form = await submitted_form(request)
        csrf_token = preauth_csrf_or_forbidden(request, form)
        username = form.get("username", "").strip()
        host = client_host(request)
        if auth_store.is_rate_limited(host, username) or auth_store.is_rate_limited(
            host, ANY_USERNAME, LOGIN_IP_LIMIT
        ):
            raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
        if not await run_in_threadpool(
            auth_store.verify_password, username, form.get("password", "")
        ):
            auth_store.record_failed_attempt(host, username)
            auth_store.record_failed_attempt(host, ANY_USERNAME)
            audit(request, None, "login.failed", username[:80])
            return render(
                request,
                "login.html",
                {"csrf_token": csrf_token, "error": "Login failed."},
                status_code=401,
            )
        auth_store.clear_failed_attempts(host, username)
        if auth_store.totp_enabled(username):
            response = RedirectResponse("/admin/login/verify", status_code=303)
            set_cookie(response, auth_store.begin_pending_login(username), SECOND_STEP_COOKIE)
            return response
        audit(request, username, "login.succeeded")
        return signed_in_response(request, username)

    def signed_in_response(request: Request, username: str) -> RedirectResponse:
        auth_store.delete_session(request.cookies.get(SESSION_COOKIE))
        token, _ = auth_store.new_session(username)
        response = RedirectResponse("/admin", status_code=303)
        set_cookie(response, token)
        response.delete_cookie(
            SECOND_STEP_COOKIE, path="/admin", secure=True, httponly=True, samesite="strict"
        )
        return response

    @app.get("/admin/login/verify")
    async def second_step_page(request: Request) -> Response:
        if not auth_store.pending_login(request.cookies.get(SECOND_STEP_COOKIE), count=False):
            return login_redirect()
        csrf_token = preauth_csrf_token(request)
        response = render(request, "login_verify.html", {"csrf_token": csrf_token, "error": None})
        set_cookie(response, csrf_token, PREAUTH_CSRF_COOKIE)
        return response

    @app.post("/admin/login/verify")
    async def second_step(request: Request) -> Response:
        form = await submitted_form(request)
        csrf_token = preauth_csrf_or_forbidden(request, form)
        pending = request.cookies.get(SECOND_STEP_COOKIE)
        username = auth_store.pending_login(pending, count=False)
        if not username:
            return login_redirect()
        if auth_store.is_second_step_rate_limited(username):
            raise HTTPException(
                status_code=429,
                detail="Too many second-step failures for this account. Try again later.",
            )
        if not auth_store.pending_login(pending):
            return login_redirect()
        used = auth_store.verify_second_factor(username, form.get("code", ""))
        if not used:
            auth_store.record_failed_attempt(client_host(request), ANY_USERNAME)
            auth_store.record_second_step_failure(username)
            audit(request, None, "login.second_step_failed", username)
            if auth_store.is_second_step_rate_limited(username):
                audit(request, None, "login.second_step_rate_limited", username, window="1h")
            return render(
                request,
                "login_verify.html",
                {"csrf_token": csrf_token, "error": "That code did not work. Try the newest one."},
                status_code=401,
            )
        auth_store.end_pending_login(pending)
        auth_store.clear_second_step_failures(username)
        audit(request, username, "login.succeeded", second_step=used)
        return signed_in_response(request, username)

    @app.post("/admin/logout")
    async def logout(request: Request) -> Response:
        session = authenticated(request)
        if not session:
            return login_redirect()
        form = await submitted_form(request)
        csrf_or_forbidden(request, session, form)
        auth_store.delete_session(request.cookies.get(SESSION_COOKIE))
        audit(request, session["username"], "logout")
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie(
            SESSION_COOKIE, path="/admin", secure=True, httponly=True, samesite="strict"
        )
        return response
