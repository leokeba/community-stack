"""Matrix account management through Synapse's admin API, called inside its container.

The admin API is never exposed: the agent runs requests with the Python interpreter
inside the Synapse container against localhost. A dedicated admin account is created on
first use with Synapse's registration shared secret, and its token is kept in a
root-only file next to the agent's audit log.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import subprocess
from typing import Any
from urllib.parse import quote

import yaml

SYNAPSE_CONTAINER = "community_synapse"
ADMIN_LOCALPART = "community-stack-admin"
LOCALPART = re.compile(r"^[a-z0-9._=/-]{1,64}$")
USER_ID = re.compile(r"^@[a-z0-9._=/-]{1,64}:[A-Za-z0-9.-]{1,253}(:\d{1,5})?$")

# Runs inside the Synapse container: one HTTP request to localhost, JSON in and out.
_REQUEST_SCRIPT = """
import json, sys, urllib.error, urllib.request
spec = json.load(sys.stdin)
headers = {"Content-Type": "application/json"}
if spec.get("token"):
    headers["Authorization"] = "Bearer " + spec["token"]
data = json.dumps(spec["body"]).encode() if spec.get("body") is not None else None
request = urllib.request.Request("http://localhost:8008" + spec["path"], data=data,
                                 headers=headers, method=spec["method"])
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        status, raw = response.status, response.read()
except urllib.error.HTTPError as exc:
    status, raw = exc.code, exc.read()
print(json.dumps({"status": status, "body": json.loads(raw or b"{}")}))
"""


class MatrixAdminError(Exception):
    """A Matrix account action the agent refused or Synapse rejected; shown to people."""


def synapse_request(method: str, path: str, body: Any = None, token: str | None = None):
    result = subprocess.run(
        ["docker", "exec", "-i", SYNAPSE_CONTAINER, "python", "-c", _REQUEST_SCRIPT],
        input=json.dumps({"method": method, "path": path, "body": body, "token": token}),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise MatrixAdminError("Matrix is not running, so accounts cannot be managed now.")
    reply = json.loads(result.stdout.strip().splitlines()[-1])
    return reply["status"], reply["body"]


def _homeserver(context) -> dict:
    path = context.project_dir / "data" / "matrix" / "homeserver.yaml"
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise MatrixAdminError("Matrix has not been set up on this server yet.") from exc


def _credentials_path(context):
    return context.project_dir / "data" / "agent" / "matrix-admin.json"


def admin_token(context) -> str:
    """The agent's own Matrix admin token, creating the account on first use."""
    path = _credentials_path(context)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))["access_token"]
    secret = _homeserver(context).get("registration_shared_secret")
    if not secret:
        raise MatrixAdminError("Synapse has no registration_shared_secret configured.")
    status, body = synapse_request("GET", "/_synapse/admin/v1/register")
    if status != 200:
        raise MatrixAdminError("Synapse refused to start creating the admin account.")
    password = secrets.token_urlsafe(32)
    mac = hmac.new(
        secret.encode(),
        b"\x00".join(
            [body["nonce"].encode(), ADMIN_LOCALPART.encode(), password.encode(), b"admin"]
        ),
        hashlib.sha1,
    ).hexdigest()
    status, created = synapse_request(
        "POST",
        "/_synapse/admin/v1/register",
        {
            "nonce": body["nonce"],
            "username": ADMIN_LOCALPART,
            "password": password,
            "admin": True,
            "mac": mac,
        },
    )
    if status != 200 or "access_token" not in created:
        raise MatrixAdminError(f"Synapse refused the admin account: {created.get('error', status)}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"user_id": created["user_id"], "access_token": created["access_token"]}),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return created["access_token"]


def _call(context, method: str, path: str, body: Any = None):
    status, reply = synapse_request(method, path, body, admin_token(context))
    if status == 401:
        # The stored token was revoked; make a new admin session next time.
        _credentials_path(context).unlink(missing_ok=True)
        raise MatrixAdminError("The agent's Matrix admin session expired; try again.")
    return status, reply


def _user_id(context, value: str) -> str:
    if not USER_ID.fullmatch(value):
        raise MatrixAdminError("That is not a Matrix account on this server.")
    server = _homeserver(context).get("server_name", "")
    if value.split(":", 1)[1] != server:
        raise MatrixAdminError("Only accounts on this server can be managed here.")
    if value.split(":", 1)[0] == f"@{ADMIN_LOCALPART}":
        raise MatrixAdminError("The stack's own admin account cannot be changed here.")
    return value


def list_users(context, params: dict[str, Any]) -> dict[str, Any]:
    status, reply = _call(
        context, "GET", "/_synapse/admin/v2/users?from=0&limit=500&guests=false&deactivated=true"
    )
    if status != 200:
        raise MatrixAdminError(f"Synapse refused the account list: {reply.get('error', status)}")
    users = [
        {
            "user_id": user["name"],
            "display_name": user.get("displayname") or "",
            "admin": bool(user.get("admin")),
            "deactivated": bool(user.get("deactivated")),
            "created": user.get("creation_ts"),
        }
        for user in reply.get("users", [])
        if not user["name"].startswith(f"@{ADMIN_LOCALPART}:")
    ]
    return {"server_name": _homeserver(context).get("server_name"), "users": users}


def create_user(context, params: dict[str, Any]) -> dict[str, Any]:
    localpart = params["username"].lower()
    if not LOCALPART.fullmatch(localpart) or localpart == ADMIN_LOCALPART:
        raise MatrixAdminError("Use lowercase letters, digits and . _ = / - for the username.")
    user_id = f"@{localpart}:{_homeserver(context).get('server_name')}"
    path = f"/_synapse/admin/v2/users/{quote(user_id, safe='')}"
    status, _ = _call(context, "GET", path)
    if status == 200:
        raise MatrixAdminError(f"{user_id} already exists.")
    password = secrets.token_urlsafe(12)
    body = {"password": password, "admin": False}
    if params.get("display_name"):
        body["displayname"] = params["display_name"]
    status, reply = _call(context, "PUT", path, body)
    if status not in (200, 201):
        raise MatrixAdminError(f"Synapse refused the account: {reply.get('error', status)}")
    return {"user_id": user_id, "temporary_password": password}


def reset_password(context, params: dict[str, Any]) -> dict[str, Any]:
    user_id = _user_id(context, params["user_id"])
    password = secrets.token_urlsafe(12)
    status, reply = _call(
        context,
        "POST",
        f"/_synapse/admin/v1/reset_password/{quote(user_id, safe='')}",
        {"new_password": password, "logout_devices": True},
    )
    if status != 200:
        raise MatrixAdminError(f"Synapse refused the reset: {reply.get('error', status)}")
    return {"user_id": user_id, "temporary_password": password}


def deactivate_user(context, params: dict[str, Any]) -> dict[str, Any]:
    user_id = _user_id(context, params["user_id"])
    status, reply = _call(
        context,
        "POST",
        f"/_synapse/admin/v1/deactivate/{quote(user_id, safe='')}",
        {"erase": False},
    )
    if status != 200:
        raise MatrixAdminError(f"Synapse refused to deactivate: {reply.get('error', status)}")
    return {"user_id": user_id}
