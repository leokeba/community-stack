"""Routes for the accounts pages of the admin console."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from markupsafe import Markup
from starlette.concurrency import run_in_threadpool

from community_stack.admin import totp
from community_stack.admin.common import SESSION_COOKIE, flash
from community_stack.admin.store import ROLE_DESCRIPTIONS, ROLES


def register_accounts(
    app: FastAPI,
    *,
    audit: Any,
    auth_store: Any,
    checked_form: Any,
    page: Any,
    redirect_with: Any,
    render: Any,
    require: Any,
) -> None:
    def accounts_page(
        request: Request, session: dict[str, Any], error: str | None = None, status_code=200
    ) -> HTMLResponse:
        assignable = [r for r in ROLES if r != "owner" or session["role"] == "owner"]
        return render(
            request,
            "accounts.html",
            page(
                session,
                accounts=auth_store.list_accounts(),
                assignable_roles=assignable,
                role_descriptions=ROLE_DESCRIPTIONS,
                **({"flash": flash(error, "error")} if error else {}),
            ),
            status_code=status_code,
        )

    def managed_account(session: dict[str, Any], form: dict[str, str]) -> dict[str, Any]:
        """The account a form targets, if the signed-in role may manage it."""
        account = auth_store.get_account(form.get("username", ""))
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found.")
        if account["username"] == session["username"]:
            raise HTTPException(
                status_code=400,
                detail="Change your own password on the My account page; "
                "ask another owner or admin to change your role or access.",
            )
        if account["role"] == "owner" and session["role"] != "owner":
            raise HTTPException(status_code=403, detail="Only owners can manage owners.")
        return account

    async def current_password_matches(session: dict[str, Any], form: dict[str, str]) -> bool:
        return await run_in_threadpool(
            auth_store.verify_password,
            session["username"],
            form.get("current_password", ""),
        )

    @app.get("/admin/accounts")
    async def accounts(request: Request) -> Response:
        return accounts_page(request, require(request, "accounts"))

    @app.post("/admin/accounts/create")
    async def create_account(request: Request) -> Response:
        session, form = await checked_form(request, "accounts")
        role = form.get("role", "")
        if role == "owner" and session["role"] != "owner":
            raise HTTPException(status_code=403, detail="Only owners can create owners.")
        if not await current_password_matches(session, form):
            return accounts_page(request, session, "Your current password is not correct.", 400)
        username = form.get("username", "").strip()
        try:
            await run_in_threadpool(
                auth_store.create_account, username, form.get("password", ""), role
            )
        except ValueError as exc:
            return accounts_page(request, session, str(exc), 400)
        audit(request, session["username"], "account.created", username, role=role)
        return redirect_with(
            request,
            "/admin/accounts",
            flash(f"Created the account {username}. Share the temporary password privately."),
        )

    @app.post("/admin/accounts/role")
    async def change_role(request: Request) -> Response:
        session, form = await checked_form(request, "accounts")
        account = managed_account(session, form)
        role = form.get("role", "")
        if role == "owner" and session["role"] != "owner":
            raise HTTPException(status_code=403, detail="Only owners can grant the owner role.")
        try:
            auth_store.set_role(account["username"], role)
        except ValueError as exc:
            return accounts_page(request, session, str(exc), 400)
        audit(
            request,
            session["username"],
            "account.role_changed",
            account["username"],
            previous=account["role"],
            role=role,
        )
        return redirect_with(
            request, "/admin/accounts", flash(f"{account['username']} is now {role}.")
        )

    @app.post("/admin/accounts/access")
    async def change_access(request: Request) -> Response:
        session, form = await checked_form(request, "accounts")
        account = managed_account(session, form)
        disable = form.get("action") == "disable"
        try:
            auth_store.set_disabled(account["username"], disable)
        except ValueError as exc:
            return accounts_page(request, session, str(exc), 400)
        action = "account.disabled" if disable else "account.enabled"
        audit(request, session["username"], action, account["username"])
        text = (
            f"{account['username']} can no longer sign in."
            if disable
            else f"{account['username']} can sign in again."
        )
        return redirect_with(request, "/admin/accounts", flash(text))

    @app.post("/admin/accounts/password")
    async def reset_password(request: Request) -> Response:
        session, form = await checked_form(request, "accounts")
        account = managed_account(session, form)
        if not await current_password_matches(session, form):
            return accounts_page(request, session, "Your current password is not correct.", 400)
        try:
            await run_in_threadpool(
                auth_store.set_password, account["username"], form.get("password", "")
            )
        except ValueError as exc:
            return accounts_page(request, session, str(exc), 400)
        audit(request, session["username"], "account.password_reset", account["username"])
        return redirect_with(
            request,
            "/admin/accounts",
            flash(f"Password reset for {account['username']}. Share it with them privately."),
        )

    def account_page(
        request: Request, session: dict[str, Any], status_code: int = 200, **context: Any
    ) -> HTMLResponse:
        username = session["username"]
        values = {
            "role_description": ROLE_DESCRIPTIONS[session["role"]],
            "error": None,
            "two_step": auth_store.totp_enabled(username),
            "codes_left": auth_store.recovery_codes_left(username),
            "setup": None,
            "recovery_codes": None,
            "two_step_error": None,
            **context,
        }
        return render(request, "account.html", page(session, **values), status_code=status_code)

    @app.post("/admin/accounts/two-step")
    async def reset_two_step(request: Request) -> Response:
        session, form = await checked_form(request, "accounts")
        account = managed_account(session, form)
        if not await current_password_matches(session, form):
            return accounts_page(request, session, "Your current password is not correct.", 400)
        auth_store.disable_totp(account["username"])
        audit(request, session["username"], "two_step.reset", account["username"])
        return redirect_with(
            request,
            "/admin/accounts",
            flash(
                f"Two-step login is off for {account['username']}. "
                "They can turn it on again from My account."
            ),
        )

    @app.get("/admin/account")
    async def my_account(request: Request) -> Response:
        session = require(request, "view")
        return account_page(request, session)

    @app.post("/admin/account/two-step/start")
    async def start_two_step(request: Request) -> Response:
        session, form = await checked_form(request, "view")
        if not await run_in_threadpool(
            auth_store.verify_password, session["username"], form.get("current_password", "")
        ):
            return account_page(
                request, session, 400, two_step_error="Your current password is not correct."
            )
        secret = auth_store.start_totp(session["username"])
        uri = totp.provisioning_uri(secret, session["username"], "Community Stack")
        setup = {"secret": secret, "qr": Markup(totp.qr_svg(uri))}
        return account_page(request, session, setup=setup)

    @app.post("/admin/account/two-step/confirm")
    async def confirm_two_step(request: Request) -> Response:
        session, form = await checked_form(request, "view")
        codes = auth_store.confirm_totp(session["username"], form.get("code", ""))
        if codes is None:
            return account_page(
                request,
                session,
                400,
                two_step_error="That code did not match. Start again and scan the new code.",
            )
        audit(request, session["username"], "two_step.enabled", session["username"])
        return account_page(request, session, recovery_codes=codes)

    @app.post("/admin/account/two-step/recovery-codes")
    async def new_recovery_codes(request: Request) -> Response:
        session, form = await checked_form(request, "view")
        if not auth_store.verify_second_factor(session["username"], form.get("code", "")):
            return account_page(request, session, 400, two_step_error="That code did not work.")
        codes = auth_store.regenerate_recovery_codes(session["username"])
        audit(request, session["username"], "two_step.recovery_codes_replaced")
        return account_page(request, session, recovery_codes=codes)

    @app.post("/admin/account/two-step/disable")
    async def disable_two_step(request: Request) -> Response:
        session, form = await checked_form(request, "view")
        password_ok = await run_in_threadpool(
            auth_store.verify_password, session["username"], form.get("current_password", "")
        )
        if not password_ok or not auth_store.verify_second_factor(
            session["username"], form.get("code", "")
        ):
            return account_page(
                request, session, 400, two_step_error="The password or the code did not work."
            )
        auth_store.disable_totp(session["username"])
        audit(request, session["username"], "two_step.disabled", session["username"])
        return redirect_with(request, "/admin/account", flash("Two-step login is off."))

    @app.post("/admin/account")
    async def change_my_password(request: Request) -> Response:
        session, form = await checked_form(request, "view")
        if not await run_in_threadpool(
            auth_store.verify_password, session["username"], form.get("current_password", "")
        ):
            error = "Your current password is not correct."
        else:
            try:
                await run_in_threadpool(
                    auth_store.set_password,
                    session["username"],
                    form.get("new_password", ""),
                    request.cookies.get(SESSION_COOKIE),
                )
            except ValueError as exc:
                error = str(exc)
            else:
                audit(request, session["username"], "password.changed", session["username"])
                return redirect_with(
                    request,
                    "/admin/account",
                    flash("Password changed. Your other sessions were signed out."),
                )
        return account_page(request, session, 400, error=error)

    @app.get("/admin/audit")
    async def audit_log(request: Request) -> Response:
        session = require(request, "audit")
        return render(request, "audit.html", page(session, events=auth_store.list_events()))
