"""The bridges Community Stack knows how to run. Explicit and small: no plugin discovery.

Each entry names the upstream bridge, the pinned image (never `latest`), and the facts the
agent and console need to run it: service directory, container, bot account, registration
file, and the commands the console may send. Planned entries are visible but cannot be
installed; deferred entries are blocked on a decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field

LOGIN_COMMAND = "login"

# Commands every bridgev2 bridge answers in its management room, in the order the console
# offers them. Bridge-specific commands are added per entry.
GENERIC_COMMANDS: dict[str, str] = {
    "help": "Show the bridge's commands.",
    "list-logins": "Show which accounts are linked.",
    "cancel": "Cancel a linking attempt that is waiting for a scan or code.",
    "set-relay": "Let the linked account relay messages for people who are not logged in.",
    LOGIN_COMMAND: "Ask the bot which login options it offers.",
}


@dataclass(frozen=True)
class BridgeSpec:
    key: str
    name: str
    status: str  # "supported", "planned" (validated, not built), or "deferred" (blocked)
    upstream: str
    docs: str
    service: str = ""  # compose directory under services/
    container: str = ""
    bot_localpart: str = ""
    registration_file: str = ""
    secret_prefix: str = ""  # env prefix for the appservice tokens, e.g. WHATSAPP
    image: str = ""  # pinned image, when the bridge is supported or planned
    login: str = ""  # how accounts are linked, shown to people
    experimental: bool = False
    # Commands the console may send. A bare "login" asks the bot for its login options.
    commands: dict[str, str] = field(default_factory=lambda: dict(GENERIC_COMMANDS))
    login_flows: tuple[str, ...] = ()  # fixed flow IDs when they are verified, e.g. ("qr",)
    limitations: tuple[str, ...] = field(default_factory=tuple)


WHATSAPP = BridgeSpec(
    key="whatsapp",
    name="WhatsApp",
    status="supported",
    upstream="https://github.com/mautrix/whatsapp",
    docs="https://docs.mau.fi/bridges/go/whatsapp/",
    service="whatsapp_bridge",
    container="community_whatsapp_bridge",
    bot_localpart="whatsappbot",
    registration_file="whatsapp.yaml",
    secret_prefix="WHATSAPP",
    image="dock.mau.dev/mautrix/whatsapp:v26.10",
    login="QR code scanned in WhatsApp Linked devices",
    commands={
        **GENERIC_COMMANDS,
        "login qr": "Start linking a WhatsApp device; the bot replies with a QR code.",
    },
    login_flows=("qr",),
    limitations=(
        "Unofficial, reverse-engineered integration; WhatsApp may restrict the linked account.",
        "The linked phone must stay online; linked devices disconnect after about two weeks offline.",
        "Bridged rooms are bidirectional and readable by the server operator.",
    ),
)

SIGNAL = BridgeSpec(
    key="signal",
    name="Signal",
    status="supported",
    upstream="https://github.com/mautrix/signal",
    docs="https://docs.mau.fi/bridges/go/signal/",
    service="signal_bridge",
    container="community_signal_bridge",
    bot_localpart="signalbot",
    registration_file="signal.yaml",
    secret_prefix="SIGNAL",
    image="dock.mau.dev/mautrix/signal:v26.10",
    login="Linked as a secondary device; the bot shows the linking code (flow to confirm live)",
    experimental=True,
    limitations=(
        "Signal may restrict accounts it considers automated.",
        "Linking flows and the bridge's first-run settings are not yet tested on a live account.",
        "Bridged rooms are bidirectional and readable by the server operator.",
    ),
)

MESSENGER = BridgeSpec(
    key="messenger",
    name="Facebook Messenger",
    status="supported",
    upstream="https://github.com/mautrix/meta",
    docs="https://docs.mau.fi/bridges/go/meta/",
    service="meta_bridge",
    container="community_meta_bridge",
    bot_localpart="metabot",
    registration_file="meta.yaml",
    secret_prefix="META",
    image="dock.mau.dev/mautrix/meta:v26.10",
    login="Account login through the bot; the login options are listed by the bot",
    experimental=True,
    limitations=(
        "Experimental. Platform restrictions on automated accounts apply.",
        "Covers Messenger only. Instagram moved to a separate bridge, which is not supported here.",
        "Bridged rooms are bidirectional and readable by the server operator.",
    ),
)

CATALOG: dict[str, BridgeSpec] = {
    spec.key: spec
    for spec in (
        WHATSAPP,
        SIGNAL,
        MESSENGER,
        BridgeSpec(
            key="instagram",
            name="Instagram",
            status="planned",
            upstream="https://github.com/mautrix/instagram",
            docs="https://docs.mau.fi/bridges/go/instagram/",
            image="",
            limitations=("Moved out of mautrix-meta in v26.08; not validated yet.",),
        ),
        BridgeSpec(
            key="telegram",
            name="Telegram",
            status="planned",
            upstream="https://github.com/mautrix/telegram",
            docs="https://docs.mau.fi/bridges/go/telegram/",
            image="dock.mau.dev/mautrix/telegram:v26.10",
            login="Needs your Telegram API ID and hash, then a verified login flow.",
        ),
        BridgeSpec(
            key="discord",
            name="Discord",
            status="deferred",
            upstream="https://github.com/mautrix/discord",
            docs="https://docs.mau.fi/bridges/go/discord/",
            image="dock.mau.dev/mautrix/discord:v0.7.7",
            login="Bot token on the legacy bridge only; a decision is needed before it is built.",
        ),
    )
}


def supported() -> list[BridgeSpec]:
    return [spec for spec in CATALOG.values() if spec.status == "supported"]


def get(key: str) -> BridgeSpec:
    spec = CATALOG.get(key)
    if spec is None or spec.status != "supported":
        raise KeyError(key)
    return spec


# Compose service names of the bridges the console and installer manage as services.
BRIDGE_SERVICES: tuple[str, ...] = tuple(spec.service for spec in supported())
