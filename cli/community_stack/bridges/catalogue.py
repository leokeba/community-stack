"""The bridges Community Stack knows how to run. Explicit and small: no plugin discovery.

Each supported entry names the upstream bridge, the pinned image (read from its compose
file, never `latest`), and the bridge-specific facts the console needs. Planned entries
are listed for visibility only; they cannot be installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BridgeSpec:
    key: str
    name: str
    status: str  # "supported", "planned" (validated, not built) or "deferred" (blocked)
    upstream: str
    docs: str
    service: str = ""  # compose service directory under services/
    container: str = ""
    appservice_id: str = ""
    bot_localpart: str = ""
    registration_file: str = ""
    image: str = ""  # pinned image for planned bridges; supported bridges read their compose file
    login: str = ""  # how accounts are linked: "qr", "phone code", "api credentials", ...
    limitations: tuple[str, ...] = field(default_factory=tuple)


WHATSAPP = BridgeSpec(
    key="whatsapp",
    name="WhatsApp",
    status="supported",
    upstream="https://github.com/mautrix/whatsapp",
    docs="https://docs.mau.fi/bridges/go/whatsapp/",
    service="whatsapp_bridge",
    container="community_whatsapp_bridge",
    appservice_id="whatsapp",
    bot_localpart="whatsappbot",
    registration_file="whatsapp.yaml",
    login="QR code scanned in WhatsApp Linked devices",
    limitations=(
        "Unofficial, reverse-engineered integration; WhatsApp may restrict the linked account.",
        "The linked phone must stay online; linked devices disconnect after about two weeks offline.",
        "Bridged rooms are bidirectional and readable by the server operator.",
    ),
)

CATALOG: dict[str, BridgeSpec] = {
    WHATSAPP.key: WHATSAPP,
    "telegram": BridgeSpec(
        key="telegram",
        name="Telegram",
        status="planned",
        upstream="https://github.com/mautrix/telegram",
        docs="https://docs.mau.fi/bridges/go/telegram/",
        # Rewritten on bridgev2 (Go) in v26.04; fresh installs use the current v26.x release.
        # dock.mau.dev/mautrix/telegram:v26.10 exists in the registry; login flow still to verify.
        image="dock.mau.dev/mautrix/telegram:v26.10",
    ),
    "signal": BridgeSpec(
        key="signal",
        name="Signal",
        status="planned",
        upstream="https://github.com/mautrix/signal",
        docs="https://docs.mau.fi/bridges/go/signal/",
        # Validated: bridgev2 (Go); v26.10 image is in the registry. Not the same as services/signal,
        # which runs signal-cli-rest-api for announcements. A bridge would need its own service name.
        image="dock.mau.dev/mautrix/signal:v26.10",
        login="QR code linked as a secondary device (to verify on the bridge bot)",
    ),
    "discord": BridgeSpec(
        key="discord",
        name="Discord",
        status="deferred",
        upstream="https://github.com/mautrix/discord",
        docs="https://docs.mau.fi/bridges/go/discord/",
        # Validated: the current release (v0.7.7) is the legacy architecture. Shared relay logins
        # for bridgev2 are not released yet. User-token logins are refused by design.
        image="dock.mau.dev/mautrix/discord:v0.7.7",
        login="bot token (legacy bridge only); needs a decision before it is built",
    ),
    "meta": BridgeSpec(
        key="meta",
        name="Facebook Messenger (Instagram via a separate bridge)",
        status="planned",
        upstream="https://github.com/mautrix/meta",
        docs="https://docs.mau.fi/bridges/go/meta/",
        # Validated: mautrix-meta v26.10 is in the registry and handles Messenger only.
        # Instagram moved to mautrix-instagram (July 2026), which is not validated yet.
        image="dock.mau.dev/mautrix/meta:v26.10",
        login="account login; experimental and subject to account restrictions",
    ),
}


def supported() -> list[BridgeSpec]:
    return [spec for spec in CATALOG.values() if spec.status == "supported"]


def get(key: str) -> BridgeSpec:
    spec = CATALOG.get(key)
    if spec is None or spec.status != "supported":
        raise KeyError(key)
    return spec
