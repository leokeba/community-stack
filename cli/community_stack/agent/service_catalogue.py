"""The settings the console may change, grouped by the service they belong to.

Explicit and small, like the bridge catalogue: no plugin discovery. Each `ServiceSpec`
owns its settings, the defaults a legacy configuration gets when one of them changes, the
checks that refuse a change, the warnings shown while reviewing one, and the facts the
agent needs to start and stop it. Adding a service means adding one entry here (and its
default block in `config_edit.SERVICE_DEFAULTS` when older configurations lack it).

The agent imports this to plan and apply changes; the console imports it to draw one card,
with its own form, per service.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from community_stack.bridges.catalogue import BRIDGE_SERVICES
from community_stack.collaborative_apps import COLLABORATIVE_APPS_BY_KEY, CollaborativeApp
from community_stack.config_edit import SERVICE_DEFAULTS, get_value, set_value


class SettingsError(Exception):
    """A change the agent refuses or could not apply; the message is shown to people."""


@dataclass(frozen=True)
class Setting:
    key: str
    label: str  # full name, used in reviews and errors where the service is not obvious
    help: str
    kind: str  # bool, int, choice, hostname, email, version, git_ref, tile_url or timezone
    default: Any
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    dangerous: bool = False
    short: str = ""  # name inside the service's own card, when shorter than the label
    advanced: bool = False  # shown under "Advanced" in the card
    # Worked out from the configuration when the key is absent or still the default.
    derive: Callable[[dict, dict[str, str]], Any] | None = None

    @property
    def path(self) -> tuple[str, ...]:
        return tuple(self.key.split("."))

    @property
    def field_id(self) -> str:
        return "setting-" + self.key.replace(".", "-").replace("_", "-")

    def current(self, config: dict, env: dict[str, str]) -> Any:
        value = get_value(config, self.path, None)
        if self.derive is not None and value in (None, self.default):
            return self.derive(config, env)
        return self.default if value is None else value

    def parse(self, raw: Any) -> Any:
        text = str(raw).strip()
        if self.kind == "bool":
            if text not in ("true", "false"):
                raise SettingsError(f"{self.label}: choose on or off.")
            return text == "true"
        if self.kind == "int":
            if not text.isdigit():
                raise SettingsError(f"{self.label}: enter a whole number.")
            value = int(text)
            if (self.minimum is not None and value < self.minimum) or (
                self.maximum is not None and value > self.maximum
            ):
                raise SettingsError(
                    f"{self.label}: choose between {self.minimum} and {self.maximum}."
                )
            return value
        if self.kind == "choice":
            if text not in self.choices:
                raise SettingsError(f"{self.label}: choose one of {', '.join(self.choices)}.")
            return text
        if self.kind == "hostname":
            if not re.fullmatch(r"[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text):
                raise SettingsError(f"{self.label}: enter a valid public hostname.")
            return text
        if self.kind == "email":
            if not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9.-]{1,189}\.[A-Za-z]{2,63}",
                text,
            ):
                raise SettingsError(f"{self.label}: enter a valid email address.")
            return text
        if self.kind == "version":
            if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-[0-9]+\.[0-9]+\.[0-9]+", text):
                raise SettingsError(f"{self.label}: enter a pinned app-container release tag.")
            return text
        if self.kind == "git_ref":
            if not re.fullmatch(r"[a-f0-9]{40}", text):
                raise SettingsError(f"{self.label}: enter a full lowercase Git commit SHA.")
            return text
        if self.kind == "tile_url":
            try:
                parsed = urlsplit(text)
                hostname = parsed.hostname
            except ValueError:
                parsed = None
                hostname = None
            if (
                parsed is None
                or parsed.scheme != "https"
                or not hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
                or any(char.isspace() for char in text)
                or not all(f"{{{axis}}}" in text for axis in ("z", "x", "y"))
            ):
                raise SettingsError(
                    f"{self.label}: enter an HTTPS tile URL containing {{z}}, {{x}}, and {{y}}."
                )
            return text
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError):
            raise SettingsError(f"{self.label}: '{text[:64]}' is not a known time zone.") from None
        return text


@dataclass(frozen=True)
class PlanContext:
    """What a check or warning sees: the configuration before and after, and what changed."""

    current: dict
    proposed: dict
    effective: dict[str, Any]
    env: dict[str, str]

    def turned_on(self, service: str) -> bool:
        return self.effective.get(f"services.{service}.enabled") is True

    def changed(self, key: str) -> bool:
        return key in self.effective

    def touches(self, service: str) -> bool:
        return any(key.startswith(f"services.{service}.") for key in self.effective)

    def enabled(self, service: str) -> bool:
        return bool(get_value(self.proposed, ("services", service, "enabled"), False))

    def proposed_value(self, service: str, name: str, default: Any = None) -> Any:
        return get_value(self.proposed, ("services", service, name), default)


Check = Callable[[PlanContext], None]
Warnings = Callable[[PlanContext], list[str]]
Adjust = Callable[[PlanContext, str], str]


@dataclass(frozen=True)
class ServiceSpec:
    key: str
    label: str
    group: str  # heading the card sits under
    settings: tuple[Setting, ...]
    page: str = "services"  # console page with the card: services, community or backups
    intro: str = ""  # shown at the top of the card when there is no on/off switch
    service: str | None = None  # services/<name> compose project its switch starts and stops
    app: CollaborativeApp | None = None  # a collaborative app with its own hostname and route
    build: bool = False  # docker compose builds the image locally
    # Order among the services one change starts (on) or stops (off); lower goes first.
    rank_on: int = 1
    rank_off: int = 1
    checks: Check | None = None
    warnings: Warnings | None = None
    adjust: Adjust | None = None  # extra configuration edits that go with a change

    @property
    def enable_key(self) -> str | None:
        return f"services.{self.service}.enabled" if self.service else None

    @property
    def enable_setting(self) -> Setting | None:
        return next((s for s in self.settings if s.key == self.enable_key), None)

    @property
    def description(self) -> str:
        return self.intro or (self.enable_setting.help if self.enable_setting else "")

    @property
    def anchor(self) -> str:
        return f"settings-{self.key}"

    @property
    def return_url(self) -> str:
        return f"/admin/{self.page}#{self.anchor}"

    @property
    def config_defaults(self) -> bool:
        return self.service in SERVICE_DEFAULTS

    @property
    def routed(self) -> bool:
        """Caddy serves it on its own hostname, so its route file can change."""
        return self.app is not None

    @property
    def appservice(self) -> bool:
        """A Matrix appservice: Synapse must reload its registration before it starts."""
        return self.service in BRIDGE_SERVICES

    @property
    def uses_bridge_net(self) -> bool:
        return self.service == "matrix" or self.appservice


# --- Shared helpers ---------------------------------------------------------------------


def _domain(config: dict) -> str:
    return get_value(config, ("domain", "base"), "example.org")


def hostname(config: dict, app: CollaborativeApp) -> str:
    configured = get_value(config, ("services", app.key, "hostname"), None)
    return configured or f"{app.hostname_prefix}.{_domain(config)}"


def _derived_hostname(app: CollaborativeApp) -> Callable[[dict, dict[str, str]], str]:
    return lambda config, env: hostname(config, app)


def _toggle(
    service: str, label: str, help_text: str, *, dangerous: bool, default: bool = False
) -> Setting:
    """The switch that starts and stops a service."""
    return Setting(
        f"services.{service}.enabled", label, help_text, "bool", default, dangerous=dangerous
    )


def _on_off(value: Any) -> str:
    return "enabled" if value else "disabled"


# --- General and backups (shown on the Community profile and Backups pages) -------------


def _backups_checks(ctx: PlanContext) -> None:
    if ctx.effective.get("operations.backups.encryption") == "restic":
        if shutil.which("restic") is None:
            raise SettingsError("Install restic on the server first (sudo apt install restic).")
        if not ctx.env.get("RESTIC_PASSWORD"):
            raise SettingsError("RESTIC_PASSWORD must be set in the server's .env first.")


def _backups_warnings(ctx: PlanContext) -> list[str]:
    if ctx.effective.get("operations.backups.encryption") == "restic":
        return [
            (
                "Keep a copy of RESTIC_PASSWORD offline: encrypted backups cannot be restored "
                "without it."
            )
        ]
    return []


GENERAL = ServiceSpec(
    key="general",
    label="Time zone",
    group="General",
    page="community",
    intro="Scheduled announcements use this time zone.",
    settings=(
        Setting(
            "project.timezone",
            "Community time zone",
            "An IANA name such as Europe/Paris.",
            "timezone",
            "UTC",
            short="Time zone",
        ),
    ),
)

BACKUPS = ServiceSpec(
    key="backups",
    label="Schedule and retention",
    group="Backups",
    page="backups",
    settings=(
        Setting(
            "operations.backups.schedule",
            "Automatic backups",
            "How often the server makes and checks a backup on its own.",
            "choice",
            "daily",
            choices=("hourly", "daily", "weekly"),
            short="How often",
        ),
        Setting(
            "operations.backups.retention_days",
            "Keep backups for (days)",
            "Older snapshots are deleted after each new backup.",
            "int",
            14,
            minimum=1,
            maximum=3650,
        ),
        Setting(
            "operations.backups.encryption",
            "Backup encryption",
            "restic encrypts backups on the server; none keeps unencrypted, owner-only archives.",
            "choice",
            "none",
            choices=("restic", "none"),
            short="Encryption",
        ),
    ),
    checks=_backups_checks,
    warnings=_backups_warnings,
)

# --- Chat -------------------------------------------------------------------------------


def _matrix_warnings(ctx: PlanContext) -> list[str]:
    if ctx.effective.get("services.matrix.enabled") is False and get_value(
        ctx.current, ("services", "announcements", "channels", "matrix_room", "enabled"), False
    ):
        return ["Matrix announcements will fail while Matrix is off."]
    return []


MATRIX = ServiceSpec(
    key="matrix",
    label="Matrix chat server",
    group="Chat",
    service="matrix",
    rank_on=0,
    settings=(
        _toggle(
            "matrix",
            "Matrix chat server",
            "Turning it off stops all community chat, and Matrix announcements, until it is "
            "turned back on. Messages and accounts are kept.",
            dangerous=True,
            default=True,
        ),
    ),
    warnings=_matrix_warnings,
)

ELEMENT = ServiceSpec(
    key="element",
    label="Element web chat",
    group="Chat",
    service="element",
    settings=(
        _toggle(
            "element",
            "Element web chat",
            "The browser chat app at /app/. Turning it off does not affect Matrix itself.",
            dangerous=False,
            default=True,
        ),
    ),
)

# --- Bridges and messaging --------------------------------------------------------------


def _whatsapp_checks(ctx: PlanContext) -> None:
    if ctx.turned_on("whatsapp_bridge") and not ctx.enabled("matrix"):
        raise SettingsError("Enable the Matrix server before enabling the WhatsApp bridge.")


def _whatsapp_adjust(ctx: PlanContext, text: str) -> str:
    # A configuration that explicitly says mode "none" would leave the new bridge unused.
    bridge = ctx.current.get("services", {}).get("whatsapp_bridge", {})
    if (
        ctx.turned_on("whatsapp_bridge")
        and bridge.get("mode", "none") == "none"
        and "mode" in (bridge)
    ):
        return set_value(text, ("services", "whatsapp_bridge", "mode"), "announcements_only")
    return text


def _whatsapp_warnings(ctx: PlanContext) -> list[str]:
    if ctx.turned_on("whatsapp_bridge"):
        return [
            (
                "WhatsApp is an unofficial, bidirectional bridge. Link only a phone and group "
                "that the community intends to bridge."
            )
        ]
    return []


WHATSAPP_BRIDGE = ServiceSpec(
    key="whatsapp_bridge",
    label="WhatsApp bridge (unofficial)",
    group="Bridges and messaging",
    service="whatsapp_bridge",
    rank_on=1,
    rank_off=0,
    settings=(
        _toggle(
            "whatsapp_bridge",
            "WhatsApp bridge (unofficial)",
            "Starts mautrix-whatsapp and registers it with Matrix. It links a phone to this "
            "server and is bidirectional; only bridge a group intended for public announcements.",
            dangerous=True,
        ),
    ),
    checks=_whatsapp_checks,
    warnings=_whatsapp_warnings,
    adjust=_whatsapp_adjust,
)

SIGNAL_BRIDGE = ServiceSpec(
    key="signal_bridge",
    label="Signal bridge (experimental)",
    group="Bridges and messaging",
    service="signal_bridge",
    settings=(
        _toggle(
            "signal_bridge",
            "Signal bridge (experimental)",
            "Starts mautrix-signal and registers it with Matrix. It links a Signal account to "
            "this server and is bidirectional; Signal may restrict accounts it considers "
            "automated.",
            dangerous=True,
        ),
    ),
)

META_BRIDGE = ServiceSpec(
    key="meta_bridge",
    label="Facebook Messenger bridge (experimental)",
    group="Bridges and messaging",
    service="meta_bridge",
    settings=(
        _toggle(
            "meta_bridge",
            "Facebook Messenger bridge (experimental)",
            "Starts mautrix-meta and registers it with Matrix for Messenger. It signs an "
            "account in to this server and is bidirectional; Meta may restrict automated "
            "accounts.",
            dangerous=True,
        ),
    ),
)

SIGNAL = ServiceSpec(
    key="signal",
    label="Signal announcements (experimental)",
    group="Bridges and messaging",
    service="signal",
    settings=(
        _toggle(
            "signal",
            "Signal (experimental)",
            "Runs signal-cli so announcements can go to a Signal group. Link an account on "
            "the Channels page afterwards. Signal may block accounts that look automated.",
            dangerous=False,
        ),
    ),
)

# --- Community apps ---------------------------------------------------------------------

_VIKUNJA = COLLABORATIVE_APPS_BY_KEY["vikunja"]
_FACILMAP = COLLABORATIVE_APPS_BY_KEY["facilmap"]
_LIBREBOOKING = COLLABORATIVE_APPS_BY_KEY["librebooking"]
_FIELD_MAP = COLLABORATIVE_APPS_BY_KEY["field_map"]
OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


def _vikunja_warnings(ctx: PlanContext) -> list[str]:
    warnings = []
    if ctx.turned_on("vikunja"):
        warnings.append(
            f"Vikunja will be deployed at https://{hostname(ctx.proposed, _VIKUNJA)}/. Its "
            "accounts are separate from Matrix; self-registration is "
            f"{_on_off(ctx.proposed_value('vikunja', 'allow_registration', False))} "
            "and public link sharing is "
            f"{_on_off(ctx.proposed_value('vikunja', 'enable_link_sharing', False))}."
        )
    if ctx.effective.get("services.vikunja.allow_registration") is True:
        warnings.append("Anyone can create a Vikunja account while self-registration is enabled.")
    if ctx.effective.get("services.vikunja.enable_link_sharing") is True:
        warnings.append(
            "Project owners can create anonymous Vikunja share links with Read, Read & Write, "
            "or Admin permission. Review each project's contents and choose Read for public lists."
        )
    return warnings


def _facilmap_warnings(ctx: PlanContext) -> list[str]:
    if ctx.turned_on("facilmap") or (
        ctx.changed("services.facilmap.hostname") and ctx.enabled("facilmap")
    ):
        return [
            (
                f"FacilMap will be deployed at https://{hostname(ctx.proposed, _FACILMAP)}/. It has "
                "no accounts or OIDC; map admin/edit/read-only URLs are bearer credentials, anyone "
                "who reaches the service can create maps, and uploaded photos are not supported. "
                "Do not store sensitive locations."
            )
        ]
    return []


def _field_map_checks(ctx: PlanContext) -> None:
    # Checked on every change, so neither app can move onto the other's hostname.
    if (
        ctx.enabled("field_map")
        and ctx.enabled("facilmap")
        and hostname(ctx.proposed, _FIELD_MAP).casefold()
        == hostname(ctx.proposed, _FACILMAP).casefold()
    ):
        raise SettingsError(
            "Field Map and FacilMap cannot use the same hostname. Disable FacilMap before "
            "moving its hostname to Field Map."
        )


def _field_map_warnings(ctx: PlanContext) -> list[str]:
    warnings = []
    if ctx.turned_on("field_map") or (ctx.touches("field_map") and ctx.enabled("field_map")):
        warnings.append(
            f"Field Map will be deployed at https://{hostname(ctx.proposed, _FIELD_MAP)}/. Map "
            "share URLs are bearer credentials. Its photos and locations may be sensitive; do "
            "not add sensitive observations to this test instance."
        )
    if ctx.effective.get("services.field_map.anonymous_links_enabled") is True or (
        ctx.enabled("field_map")
        and ctx.proposed_value("field_map", "anonymous_links_enabled", True)
    ):
        warnings.append(
            "Anyone who can reach Field Map can create maps and consume storage while "
            "anonymous map creation is enabled."
        )
    if ctx.proposed_value("field_map", "tile_url") == OSM_TILES and (
        ctx.turned_on("field_map") or ctx.changed("services.field_map.tile_url")
    ):
        warnings.append(
            "The OpenStreetMap tile URL is for low-volume testing only; use an appropriate "
            "tile provider before production."
        )
    return warnings


def _librebooking_warnings(ctx: PlanContext) -> list[str]:
    warnings = []
    if ctx.turned_on("librebooking") or (
        ctx.touches("librebooking") and ctx.enabled("librebooking")
    ):
        warnings.append(
            f"LibreBooking will be deployed at https://{hostname(ctx.proposed, _LIBREBOOKING)}"
            "/Web/. Configure each space's approval rules in LibreBooking. For first setup, "
            "briefly enable self-registration to create the administrator account, then turn "
            "it off."
        )
    if ctx.effective.get("services.librebooking.allow_registration") is True:
        warnings.append(
            "Anyone can create a LibreBooking account while self-registration is enabled. "
            "Turn it off immediately after registering the initial administrator."
        )
    return warnings


def _librebooking_admin_email(config: dict, env: dict[str, str]) -> str:
    return env.get("ACME_EMAIL", "admin@example.org")


VIKUNJA = ServiceSpec(
    key="vikunja",
    label="Vikunja task management",
    group="Community apps",
    service="vikunja",
    app=_VIKUNJA,
    settings=(
        _toggle(
            "vikunja",
            "Vikunja task management",
            "Deploy Vikunja at tasks.<community domain> with its own accounts. Add the "
            "hostname's DNS record before enabling it.",
            dangerous=True,
        ),
        Setting(
            "services.vikunja.allow_registration",
            "Vikunja self-registration",
            "Allow anyone to create a Vikunja account. Existing accounts are unaffected; "
            "turn this off to keep account creation admin-managed.",
            "bool",
            False,
            dangerous=True,
            short="Self-registration",
        ),
        Setting(
            "services.vikunja.enable_link_sharing",
            "Vikunja public project sharing",
            "Allow project owners to create anonymous share links. Anyone with a link gets "
            "its selected permission; use Read for public to-do lists.",
            "bool",
            False,
            dangerous=True,
            short="Public project sharing",
        ),
    ),
    warnings=_vikunja_warnings,
)

FACILMAP = ServiceSpec(
    key="facilmap",
    label="FacilMap collaborative maps",
    group="Community apps",
    service="facilmap",
    app=_FACILMAP,
    rank_on=3,
    rank_off=2,
    settings=(
        _toggle(
            "facilmap",
            "FacilMap collaborative maps",
            "Deploy maps.<community domain>. FacilMap has no user accounts or OIDC; anyone "
            "who can reach it can create maps, and shared map URLs are bearer credentials. "
            "Do not store sensitive locations.",
            dangerous=True,
        ),
        Setting(
            "services.facilmap.hostname",
            "FacilMap public hostname",
            "The HTTPS hostname routed to FacilMap. Add its DNS record before enabling the "
            "service.",
            "hostname",
            "",
            short="Public hostname",
            derive=_derived_hostname(_FACILMAP),
        ),
    ),
    warnings=_facilmap_warnings,
)

FIELD_MAP = ServiceSpec(
    key="field_map",
    label="Field Map mobile mapping",
    group="Community apps",
    service="field_map",
    app=_FIELD_MAP,
    build=True,
    rank_on=3,
    rank_off=2,
    settings=(
        _toggle(
            "field_map",
            "Field Map mobile mapping",
            "Deploy Field Map at its public hostname. Maps are private by default, but share "
            "URLs are bearer credentials. Confirm the anonymous-map setting before enabling it.",
            dangerous=True,
        ),
        Setting(
            "services.field_map.hostname",
            "Field Map public hostname",
            "The HTTPS hostname routed to Field Map. Add its DNS record before enabling the "
            "service.",
            "hostname",
            "",
            short="Public hostname",
            derive=_derived_hostname(_FIELD_MAP),
        ),
        Setting(
            "services.field_map.anonymous_links_enabled",
            "Field Map anonymous map creation",
            "Anyone who can reach Field Map can create maps when this is on. Share links are "
            "bearer credentials; disable this setting unless anonymous creation is intended.",
            "bool",
            True,
            dangerous=True,
            short="Anonymous map creation",
        ),
        Setting(
            "services.field_map.tile_url",
            "Field Map tile URL",
            "HTTPS tile template containing {z}, {x}, and {y}. The URL is embedded in the "
            "public frontend; use a provider that permits your expected traffic.",
            "tile_url",
            OSM_TILES,
            short="Tile URL",
            advanced=True,
        ),
        Setting(
            "services.field_map.version",
            "Field Map source commit",
            "Pinned full Git commit SHA from leokeba/field-map. Changing it rebuilds the app "
            "after a safety backup.",
            "git_ref",
            "c9646113b2817d838e340f649a6445d0381fb702",
            dangerous=True,
            short="Source commit",
            advanced=True,
        ),
    ),
    checks=_field_map_checks,
    warnings=_field_map_warnings,
)

LIBREBOOKING = ServiceSpec(
    key="librebooking",
    label="LibreBooking space reservations",
    group="Community apps",
    service="librebooking",
    app=_LIBREBOOKING,
    build=True,
    settings=(
        _toggle(
            "librebooking",
            "LibreBooking space reservations",
            "Deploy booking.<community domain> with its own accounts and approval calendar. "
            "Add the hostname's DNS record before enabling it.",
            dangerous=True,
        ),
        Setting(
            "services.librebooking.hostname",
            "LibreBooking public hostname",
            "The HTTPS hostname routed to LibreBooking. Add its DNS record before enabling it.",
            "hostname",
            "",
            short="Public hostname",
            derive=_derived_hostname(_LIBREBOOKING),
        ),
        Setting(
            "services.librebooking.admin_email",
            "LibreBooking administrator email",
            "The email address LibreBooking grants administrator privileges to during first setup.",
            "email",
            "admin@example.org",
            short="Administrator email",
            derive=_librebooking_admin_email,
        ),
        Setting(
            "services.librebooking.allow_registration",
            "LibreBooking self-registration",
            "Allow anyone to create a LibreBooking account. Enable temporarily for initial "
            "admin registration, then turn it off.",
            "bool",
            False,
            dangerous=True,
            short="Self-registration",
        ),
        Setting(
            "services.librebooking.version",
            "LibreBooking container version",
            "Pinned app-container release tag (app version-container version). Review upstream "
            "releases before changing it; the service is rebuilt and a safety backup is taken.",
            "version",
            "7.2.0-3.5.2",
            dangerous=True,
            short="Container version",
            advanced=True,
        ),
    ),
    warnings=_librebooking_warnings,
)

# Only settings that take effect when changed belong here. Matrix registration,
# federation and upload size are not listed because install does not render
# them into Synapse's configuration yet.
SERVICES: tuple[ServiceSpec, ...] = (
    GENERAL,
    BACKUPS,
    MATRIX,
    ELEMENT,
    WHATSAPP_BRIDGE,
    SIGNAL_BRIDGE,
    META_BRIDGE,
    SIGNAL,
    VIKUNJA,
    FACILMAP,
    FIELD_MAP,
    LIBREBOOKING,
)
SERVICES_BY_KEY: dict[str, ServiceSpec] = {spec.key: spec for spec in SERVICES}
SPEC_FOR_SETTING: dict[str, ServiceSpec] = {
    setting.key: spec for spec in SERVICES for setting in spec.settings
}
SETTINGS: dict[str, Setting] = {
    setting.key: setting for spec in SERVICES for setting in spec.settings
}
SPECS_BY_SERVICE: dict[str, ServiceSpec] = {spec.service: spec for spec in SERVICES if spec.service}
