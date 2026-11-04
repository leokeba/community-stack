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
    status: str  # "supported" or "planned"
    upstream: str
    docs: str
    service: str = ""  # compose service directory under services/
    container: str = ""
    appservice_id: str = ""
    bot_localpart: str = ""
    registration_file: str = ""
    command_prefix: str = ""  # bot commands must start with this, e.g. "!whatsapp"
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
    command_prefix="!whatsapp",
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
        docs="https://docs.mau.fi/bridges/python/telegram/",
    ),
    "signal": BridgeSpec(
        key="signal",
        name="Signal",
        status="planned",
        upstream="https://github.com/mautrix/signal",
        docs="https://docs.mau.fi/bridges/go/signal/",
    ),
    "discord": BridgeSpec(
        key="discord",
        name="Discord",
        status="planned",
        upstream="https://github.com/mautrix/discord",
        docs="https://docs.mau.fi/bridges/go/discord/",
    ),
    "meta": BridgeSpec(
        key="meta",
        name="Instagram and Messenger",
        status="planned",
        upstream="https://github.com/mautrix/meta",
        docs="https://docs.mau.fi/bridges/go/meta/",
    ),
}


def supported() -> list[BridgeSpec]:
    return [spec for spec in CATALOG.values() if spec.status == "supported"]


def get(key: str) -> BridgeSpec:
    spec = CATALOG.get(key)
    if spec is None or spec.status != "supported":
        raise KeyError(key)
    return spec
