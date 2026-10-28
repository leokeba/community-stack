"""Channels organizers can connect from the admin console, and how to build them."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from community_stack.announcements.adapters import http
from community_stack.announcements.adapters.base import ChannelAdapter
from community_stack.announcements.adapters.messaging import (
    DiscordWebhookAdapter,
    SlackWebhookAdapter,
    TelegramAdapter,
)
from community_stack.announcements.adapters.social import BlueskyAdapter, MastodonAdapter

TOKEN = re.compile(r"^[^\s]{8,512}$")


@dataclass(frozen=True)
class ConnectorField:
    name: str
    label: str
    secret: bool = False
    required: bool = True
    placeholder: str = ""


@dataclass(frozen=True)
class ConnectorType:
    kind: str
    label: str
    steps: tuple[str, ...]
    fields: tuple[ConnectorField, ...]
    validate: Callable[[dict[str, str]], dict[str, str]]
    build: Callable[[dict[str, str]], ChannelAdapter]
    caveat: str = ""


def _token(values: dict[str, str], name: str, label: str) -> str:
    value = values.get(name, "").strip()
    if not TOKEN.fullmatch(value):
        raise ValueError(f"{label} looks incomplete; paste it again without spaces.")
    return value


def _validate_mastodon(values: dict[str, str]) -> dict[str, str]:
    url = values.get("instance_url", "").strip().rstrip("/")
    http.assert_public_https(url)
    return {"instance_url": url, "access_token": _token(values, "access_token", "The token")}


def _validate_bluesky(values: dict[str, str]) -> dict[str, str]:
    handle = values.get("handle", "").strip().lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,252}\.[a-z]{2,}", handle):
        raise ValueError("Enter the handle like community.bsky.social.")
    service = (values.get("service_url") or "https://bsky.social").strip().rstrip("/")
    http.assert_public_https(service)
    password = values.get("app_password", "").strip()
    if not re.fullmatch(r"[a-z0-9]{4}(-[a-z0-9]{4}){3}", password):
        raise ValueError("Use an app password (xxxx-xxxx-xxxx-xxxx), not the account password.")
    return {"handle": handle, "service_url": service, "app_password": password}


def _validate_telegram(values: dict[str, str]) -> dict[str, str]:
    chat_id = values.get("chat_id", "").strip()
    if not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}|-?\d{5,20}", chat_id):
        raise ValueError("Enter the channel as @channelname or its numeric ID.")
    token = values.get("bot_token", "").strip()
    if not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{30,64}", token):
        raise ValueError("The bot token from @BotFather looks like 123456789:AA...")
    return {"chat_id": chat_id, "bot_token": token}


def _validate_webhook(name: str, hosts: set[str], path_prefix: str):
    def validate(values: dict[str, str]) -> dict[str, str]:
        url = values.get("webhook_url", "").strip()
        http.assert_public_https(url, allowed_hosts=hosts)
        if not re.sub(r"^https://[^/]+", "", url).startswith(path_prefix):
            raise ValueError(f"That does not look like a {name} webhook address.")
        return {"webhook_url": url}

    return validate


CONNECTORS: dict[str, ConnectorType] = {
    connector.kind: connector
    for connector in (
        ConnectorType(
            kind="mastodon",
            label="Mastodon",
            steps=(
                "Sign in to the community's Mastodon account.",
                "Open Preferences, then Development, then New application.",
                "Give it a name, keep only the scopes write:statuses and read:accounts, and save.",
                "Open the application and copy 'Your access token'.",
            ),
            fields=(
                ConnectorField(
                    "instance_url", "Server address", placeholder="https://mastodon.social"
                ),
                ConnectorField("access_token", "Access token", secret=True),
            ),
            validate=_validate_mastodon,
            build=lambda c: MastodonAdapter(c["instance_url"], c["access_token"]),
        ),
        ConnectorType(
            kind="bluesky",
            label="Bluesky",
            steps=(
                "Sign in to the community's Bluesky account.",
                "Open Settings, then Privacy and security, then App passwords.",
                "Create an app password and copy it (it looks like xxxx-xxxx-xxxx-xxxx).",
            ),
            fields=(
                ConnectorField("handle", "Handle", placeholder="community.bsky.social"),
                ConnectorField("app_password", "App password", secret=True),
                ConnectorField(
                    "service_url",
                    "Server (only for self-hosted accounts)",
                    required=False,
                    placeholder="https://bsky.social",
                ),
            ),
            validate=_validate_bluesky,
            build=lambda c: BlueskyAdapter(c["handle"], c["app_password"], c["service_url"]),
            caveat="Bluesky posts cannot be edited; edits are not sent there.",
        ),
        ConnectorType(
            kind="telegram",
            label="Telegram channel",
            steps=(
                "In Telegram, message @BotFather, send /newbot and follow the steps.",
                "Copy the bot token it gives you.",
                "Add the bot to the channel as an administrator that can post, edit and delete.",
                "Enter the channel's public @name, or its numeric ID for a private channel.",
            ),
            fields=(
                ConnectorField("chat_id", "Channel", placeholder="@ourcommunity"),
                ConnectorField("bot_token", "Bot token", secret=True),
            ),
            validate=_validate_telegram,
            build=lambda c: TelegramAdapter(c["bot_token"], c["chat_id"]),
            caveat="A failed Telegram post is not retried automatically, to avoid duplicates.",
        ),
        ConnectorType(
            kind="discord",
            label="Discord channel",
            steps=(
                "In Discord, open the channel's settings, then Integrations, then Webhooks.",
                "Create a webhook, name it, and copy its URL.",
            ),
            fields=(ConnectorField("webhook_url", "Webhook URL", secret=True),),
            validate=_validate_webhook(
                "Discord",
                {"discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"},
                "/api/webhooks/",
            ),
            build=lambda c: DiscordWebhookAdapter(c["webhook_url"]),
        ),
        ConnectorType(
            kind="slack",
            label="Slack channel",
            steps=(
                "In Slack, create an app for your workspace and turn on Incoming Webhooks.",
                "Add a webhook for the channel and copy its URL.",
            ),
            fields=(ConnectorField("webhook_url", "Webhook URL", secret=True),),
            validate=_validate_webhook("Slack", {"hooks.slack.com"}, "/services/"),
            build=lambda c: SlackWebhookAdapter(c["webhook_url"]),
            caveat="Slack webhooks cannot edit or delete messages once sent.",
        ),
    )
}


def merge_values(
    connector: ConnectorType, submitted: dict[str, str], saved: dict[str, str]
) -> dict[str, str]:
    """Submitted values, keeping saved secrets when their fields are left blank."""
    merged = {}
    for field in connector.fields:
        value = submitted.get(field.name, "").strip()
        if not value and field.secret:
            value = saved.get(field.name, "")
        merged[field.name] = value
    return merged
