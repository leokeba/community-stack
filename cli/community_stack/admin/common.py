"""Constants, status badges and small helpers shared by the admin console modules."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
from pathlib import Path
from typing import Any

from markupsafe import Markup

PREFERENCE_TOPICS: dict[str, list[str]] = {"all": [], "important": ["important"]}


SESSION_COOKIE = "community_admin_session"
PREAUTH_CSRF_COOKIE = "__Host-community_admin_csrf"
SECOND_STEP_COOKIE = "community_admin_second_step"
LOGIN_IP_LIMIT = 20
ANY_USERNAME = "\0any"
TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
WORKER_INTERVAL_SECONDS = 15.0
DEFAULT_AGENT_SOCKET = "/run/community-stack-agent/agent.sock"
SOCIAL_ROWS = 5
# Everything a console page uses is served from here; nothing is loaded from elsewhere.
CONSOLE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; object-src 'none'"
)
# The dashboard shows service status; it must never wait on a slow agent.
DASHBOARD_AGENT_TIMEOUT_SECONDS = 5.0

mimetypes.add_type("font/woff2", ".woff2")

logger = logging.getLogger("community_stack.admin.app")

# Announcement, delivery, job and health states as people read them, with a colour tone.
STATUS_BADGES = {
    "draft": ("Draft", "neutral"),
    "in_review": ("In review", "warning"),
    "approved": ("Approved", "info"),
    "scheduled": ("Scheduled", "info"),
    "published": ("Published", "success"),
    "updated": ("Updated", "success"),
    "withdrawn": ("Withdrawn", "neutral"),
    "prepared": ("Prepared", "info"),
    "delivered": ("Published", "success"),
    "failed": ("Failed", "danger"),
    "unknown": ("Unknown", "warning"),
    "queued": ("Waiting to retry", "warning"),
    "running": ("In progress", "info"),
    "ok": ("OK", "success"),
    "warning": ("Needs attention", "warning"),
    "problem": ("Problem", "danger"),
}


def status_badge(value: Any, label: str | None = None, tone: str | None = None) -> Markup:
    key = str(getattr(value, "value", value))
    known_label, known_tone = STATUS_BADGES.get(
        key, (key.replace("_", " ").capitalize(), "neutral")
    )
    return Markup('<span class="badge badge-{}">{}</span>').format(
        tone or known_tone, label or known_label
    )


def flash(text: str, tone: str = "success", details: list[str] | None = None, **extra: Any):
    """A message shown once at the top of the next page: success, warning or error."""
    return {"tone": tone, "text": text, "details": details or [], **extra}


def static_version() -> str:
    """Changes whenever a static file does, so browsers never mix old scripts with new pages."""
    digest = hashlib.sha256()
    for path in sorted(item for item in STATIC_DIR.rglob("*") if item.is_file()):
        digest.update(path.relative_to(STATIC_DIR).as_posix().encode() + path.read_bytes())
    return digest.hexdigest()[:12]


class LoginRequired(Exception):
    """Raised when a page needs a signed-in account."""
