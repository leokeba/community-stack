"""Reversible continuity activation with independently resumable side effects."""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from community_stack.announcements.models import (
    UNPUBLISHED,
    Announcement,
    AnnouncementStatus,
)
from community_stack.announcements.platforms import PLATFORMS
from community_stack.announcements.publisher import AnnouncementPublisher
from community_stack.announcements.storage import AnnouncementStorage
from community_stack.community import INDEPENDENT_KEYS, load_community, resolve_independent
from community_stack.continuity.connect_page import (
    build_site,
    load_identity,
    write_files,
)
from community_stack.continuity.package import build_continuity_package
from community_stack.continuity.store import CHANNEL_STATES, ContinuityStore

logger = logging.getLogger(__name__)

DEFAULT_ENGLISH_TEMPLATE = {
    "title": "Important community update",
    "body_template": (
        "What happened: {what_happened}\n\n"
        "Affected channels: {affected_channels}\n\n"
        "Official contacts and independent channels: {connect_url}\n\n"
        "How to reach us: {join_instructions}\n\n"
        "Updated: {updated_at}"
    ),
    "what_happened": "{reason}",
    "join_instructions": "Use the official community contacts and channel links at {connect_url}.",
}
REQUIRED_PLACEHOLDERS = frozenset(
    {"what_happened", "affected_channels", "connect_url", "join_instructions", "updated_at"}
)
ALLOWED_PLACEHOLDERS = REQUIRED_PLACEHOLDERS | {"reason", "community"}
INDEPENDENT_ADAPTERS = {
    "website": "web_feed",
    "rss": "web_feed",
    "email": "email",
    "matrix": "matrix_room",
    "activitypub": "fediverse",
}
CHANNEL_LABELS = {
    "website": "Website",
    "rss": "RSS and Atom feeds",
    "email": "Email",
    "matrix": "Matrix",
    "activitypub": "Fediverse",
}


class ContinuityService:
    """Coordinate continuity operations without coupling core publishing to providers."""

    def __init__(
        self,
        storage: AnnouncementStorage,
        publisher: AnnouncementPublisher,
        config_path: Path | str = "community-stack.yaml",
    ) -> None:
        self.storage = storage
        self.publisher = publisher
        self.config_path = Path(config_path)
        self.store = ContinuityStore(storage.db_path)
        self.connect_dir = publisher.feed_dir.parent / "connect"
        self.continuity_dir = publisher.feed_dir.parent / "continuity"
        self.guide_path = self.continuity_dir / "guide.zip"

    def _profile(self):
        return load_community(self.config_path)[0]

    def _known_channels(self) -> set[str]:
        profile = self._profile()
        return (
            set(INDEPENDENT_KEYS)
            | set(PLATFORMS)
            | {adapter.name for adapter in self.publisher.adapters}
            | {account.platform for account in profile.social}
        )

    def channel_options(self) -> list[tuple[str, str]]:
        profile = self._profile()
        channels = (
            set(INDEPENDENT_KEYS)
            | set(PLATFORMS)
            | {adapter.name for adapter in self.publisher.adapters}
            | {account.platform for account in profile.social}
        )
        return sorted((channel, self._channel_label(channel)) for channel in channels)

    def _validate_affected(self, affected_channels: list[str]) -> list[str]:
        normalized = sorted({channel.strip() for channel in affected_channels if channel.strip()})
        if not normalized:
            raise ValueError("Choose at least one affected channel.")
        unknown = set(normalized) - self._known_channels()
        if unknown:
            raise ValueError(f"Unknown affected channel(s): {', '.join(sorted(unknown))}.")
        return normalized

    @staticmethod
    def _translation_map(
        languages: tuple[str, ...],
        translations: dict[str, dict[str, str]] | None,
    ) -> dict[str, dict[str, str]]:
        supplied = translations or {}
        unknown = set(supplied) - set(languages)
        if unknown:
            raise ValueError(
                f"Translations include unconfigured languages: {', '.join(sorted(unknown))}."
            )
        result: dict[str, dict[str, str]] = {}
        for language in languages:
            item = supplied.get(language)
            if item is None and language == "en":
                item = DEFAULT_ENGLISH_TEMPLATE
            if item is None:
                raise ValueError(
                    f"Add a migration template translation for configured language '{language}'."
                )
            title = " ".join(str(item.get("title", "")).split())
            body_template = str(item.get("body_template", "")).strip()
            what_happened = str(item.get("what_happened", "")).strip()
            join_instructions = str(item.get("join_instructions", "")).strip()
            if not title or len(title) > 200 or not body_template:
                raise ValueError(f"The {language} migration title and body template are required.")
            if language != "en" and (not what_happened or not join_instructions):
                raise ValueError(
                    f"Add translated what-happened and joining instructions for '{language}'."
                )
            if language == "en":
                what_happened = what_happened or "{reason}"
                join_instructions = (
                    join_instructions
                    or "Use the official community contacts and channel links at {connect_url}."
                )
            try:
                placeholders = {
                    field_name
                    for _, field_name, _, _ in string.Formatter().parse(body_template)
                    if field_name
                }
            except ValueError as exc:
                raise ValueError(f"The {language} body template has invalid braces.") from exc
            if not REQUIRED_PLACEHOLDERS <= placeholders:
                missing = ", ".join(sorted(REQUIRED_PLACEHOLDERS - placeholders))
                raise ValueError(
                    f"The {language} body template must include these placeholders: {missing}."
                )
            if not placeholders <= ALLOWED_PLACEHOLDERS:
                unknown_fields = ", ".join(sorted(placeholders - ALLOWED_PLACEHOLDERS))
                raise ValueError(
                    f"The {language} body template has unknown placeholders: {unknown_fields}."
                )
            result[language] = {
                "title": title,
                "body_template": body_template,
                "what_happened": what_happened,
                "join_instructions": join_instructions,
            }
        return result

    @staticmethod
    def _channel_label(channel: str) -> str:
        if channel in PLATFORMS:
            return PLATFORMS[channel].name
        return CHANNEL_LABELS.get(channel, channel.replace("_", " ").title())

    def _render_messages(
        self,
        reason: str,
        affected_channels: list[str],
        translations: dict[str, dict[str, str]],
        connect_url: str,
        updated_at: str,
        community_name: str,
    ) -> dict[str, dict[str, str]]:
        labels = ", ".join(self._channel_label(channel) for channel in affected_channels)
        messages = {}
        for language, template in translations.items():
            try:
                values = {
                    "reason": reason,
                    "what_happened": template["what_happened"].format(reason=reason),
                    "affected_channels": labels,
                    "connect_url": connect_url,
                    "join_instructions": template["join_instructions"].format(
                        connect_url=connect_url
                    ),
                    "updated_at": updated_at,
                    "community": community_name,
                }
                title = template["title"].format_map(values)
                body = template["body_template"].format_map(values)
            except (KeyError, ValueError) as exc:
                raise ValueError(
                    f"The {language} migration template could not be rendered."
                ) from exc
            if len(body) > 50_000:
                raise ValueError(f"The rendered {language} migration message is too long.")
            messages[language] = {"title": title, "body": body}
        return messages

    def _target_channels(self, affected_channels: list[str]) -> list[str]:
        blocked = set(affected_channels)
        blocked_adapters = {INDEPENDENT_ADAPTERS.get(key, key) for key in blocked}
        settings = self.storage.get_channel_settings()
        return [
            adapter.name
            for adapter in self.publisher.adapters
            if adapter.name not in blocked_adapters and settings.get(adapter.name, True)
        ]

    def plan(
        self,
        reason: str,
        affected_channels: list[str],
        translations: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        profile = self._profile()
        current = self.store.active_notice()
        if current:
            if translations is None:
                translations = current["translations"]
            reason = current["reason"]
            affected_channels = current["affected_channels"]
        affected = self._validate_affected(affected_channels)
        translation_map = self._translation_map(profile.languages, translations)
        messages = self._render_messages(
            reason,
            affected,
            translation_map,
            profile.connect_url,
            current["changed_at"] if current else datetime.now(timezone.utc).isoformat(),
            profile.name,
        )
        return {
            "active": bool(current),
            "event_id": current["event_id"] if current else None,
            "affected_channels": affected,
            "target_channels": self._target_channels(affected),
            "messages": messages,
            "steps": self.store.list_steps(current["event_id"]) if current else [],
            "guide_path": str(self.guide_path),
        }

    def _refresh_public_pages(self) -> None:
        profile, resolution, announcements, logo, cover = load_identity(
            self.config_path, self.storage
        )
        files = build_site(
            profile,
            resolution,
            announcements,
            logo,
            cover=cover,
            channel_states=self.store.list_channel_states(),
            notice=self.store.active_notice(),
            guide_available=self.guide_path.is_file(),
        )
        write_files(self.connect_dir, files)

    def _publish_migration_messages(self, current: dict[str, Any]) -> list[str]:
        plan = self.plan(current["reason"], current["affected_channels"], current["translations"])
        targets = plan["target_channels"]
        if not targets:
            return []
        announcement_ids = []
        event_suffix = current["event_id"].removeprefix("cont_")
        for language, message in plan["messages"].items():
            language_slug = re.sub(r"[^A-Za-z0-9_-]", "_", language)[:10]
            language_hash = hashlib.sha256(language.encode()).hexdigest()[:6]
            language_suffix = f"{language_slug}_{language_hash}"
            announcement_id = f"ann_cont_{event_suffix}_{language_suffix}"
            announcement_ids.append(announcement_id)
            announcement = self.storage.get_announcement(announcement_id)
            if announcement is None:
                announcement = Announcement(
                    id=announcement_id,
                    title=message["title"],
                    body=message["body"],
                    author=current["actor"],
                    language=language,
                    channels=targets,
                    status=AnnouncementStatus.DRAFT,
                )
                self.storage.save_announcement(announcement)
            if announcement.status in UNPUBLISHED:
                if announcement.status == AnnouncementStatus.IN_REVIEW:
                    self.publisher.set_review_status(
                        announcement.id,
                        (AnnouncementStatus.IN_REVIEW,),
                        AnnouncementStatus.APPROVED,
                    )
                self.publisher.publish_draft(announcement.id)
            if not any(
                item["action"] == "published_without_review"
                for item in self.storage.review_history(announcement_id)
            ):
                self.storage.add_review_event(
                    announcement_id,
                    "published_without_review",
                    current["actor"],
                    current["reason"],
                )
        return announcement_ids

    def _run_steps(self, event_id: str, steps: list[tuple[str, Any]]) -> dict[str, str]:
        outcomes = {}
        refresh_dependents = False
        for name, action in steps:
            completed = self.store.step_completed(event_id, name)
            if completed and not refresh_dependents:
                outcomes[name] = "completed"
                continue
            self.store.begin_step(event_id, name)
            try:
                action()
            except Exception as exc:
                logger.exception("Continuity step %s failed for %s", name, event_id)
                self.store.finish_step(event_id, name, f"{type(exc).__name__}: {exc}")
                outcomes[name] = "failed"
            else:
                self.store.finish_step(event_id, name)
                outcomes[name] = "completed"
                if not completed:
                    refresh_dependents = True
        return outcomes

    def activate(
        self,
        actor: str,
        reason: str,
        affected_channels: list[str],
        translations: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        profile = self._profile()
        existing = self.store.active_notice()
        affected = self._validate_affected(affected_channels)
        if existing and translations is None:
            translation_map = existing["translations"]
        else:
            translation_map = self._translation_map(profile.languages, translations)
        occurred_at = existing["changed_at"] if existing else datetime.now(timezone.utc).isoformat()
        notice_translations = self._render_messages(
            reason,
            affected,
            translation_map,
            profile.connect_url,
            occurred_at,
            profile.name,
        )
        current, created = self.store.start_activation(
            actor,
            reason,
            affected,
            translation_map,
            notice_translations,
            occurred_at,
        )
        if not created and (
            current["reason"] != " ".join(reason.split())[:500]
            or current["affected_channels"] != affected
            or (translations is not None and current["translations"] != translation_map)
        ):
            raise ValueError(
                "Continuity mode is already active; repeat its original details to resume it."
            )
        outcomes = self._run_steps(
            current["event_id"],
            [
                ("public_notice", self._refresh_public_pages),
                ("migration_announcements", lambda: self._publish_migration_messages(current)),
                (
                    "offline_guide",
                    lambda: build_continuity_package(
                        self.config_path, self.storage, self.guide_path
                    ),
                ),
                ("refresh_connect_page", self._refresh_public_pages),
            ],
        )
        return {
            "event_id": current["event_id"],
            "created": created,
            "steps": outcomes,
            "target_channels": self._target_channels(affected),
        }

    def deactivate(self, actor: str, reason: str) -> dict[str, Any]:
        current, created = self.store.start_deactivation(actor, reason)
        outcomes = self._run_steps(
            current["event_id"],
            [
                (
                    "offline_guide",
                    lambda: build_continuity_package(
                        self.config_path, self.storage, self.guide_path
                    ),
                ),
                ("refresh_connect_page", self._refresh_public_pages),
            ],
        )
        return {"event_id": current["event_id"], "created": created, "steps": outcomes}

    def status(self) -> dict[str, Any]:
        current = self.store.get_current()
        return {
            "current": current,
            "channel_states": self.store.list_channel_states(),
            "events": self.store.list_events(),
            "steps": self.store.list_steps(current["event_id"]) if current else [],
            "guide_available": self.guide_path.is_file(),
            "exercises": self.store.list_exercises(),
        }

    def set_channel_state(self, channel: str, status: str, reason: str, actor: str) -> None:
        if channel not in self._known_channels():
            raise ValueError(f"Unknown channel: {channel}.")
        if status not in CHANNEL_STATES:
            raise ValueError("Choose operational, degraded, unavailable or retired.")
        self.store.set_channel_state(channel, status, reason, actor)
        self._refresh_public_pages()

    def drill_channels(self) -> list[tuple[str, str]]:
        profile, config = load_community(self.config_path)
        resolution = resolve_independent(profile, self.storage.list_connections(), config)
        adapters = {adapter.name for adapter in self.publisher.adapters}
        settings = self.storage.get_channel_settings()
        return sorted(
            (channel.key, channel.label)
            for channel in resolution.shown
            if (adapter := INDEPENDENT_ADAPTERS[channel.key]) in adapters
            and settings.get(adapter, True)
        )

    def start_drill(self, actor: str, scenario: str, channels: list[str]) -> dict[str, Any]:
        if self.store.active_notice():
            raise ValueError("End active continuity mode before starting a migration drill.")
        available = dict(self.drill_channels())
        selected = sorted({channel.strip() for channel in channels if channel.strip()})
        if not selected:
            raise ValueError("Choose at least one active independent channel for the drill.")
        unknown = set(selected) - set(available)
        if unknown:
            raise ValueError(f"Unavailable independent channel(s): {', '.join(sorted(unknown))}.")
        targets = sorted({INDEPENDENT_ADAPTERS[channel] for channel in selected})
        exercise_id = f"ex_{secrets.token_hex(10)}"
        announcement_id = f"ann_drill_{exercise_id.removeprefix('ex_')}"
        normalized_scenario = " ".join(scenario.split())[:500]
        if not normalized_scenario:
            raise ValueError("Describe the simulated outage scenario.")
        self.store.create_exercise(
            exercise_id, announcement_id, actor, normalized_scenario, selected
        )
        announcement = Announcement(
            id=announcement_id,
            title="Community communication drill",
            body=(
                f"Simulated scenario: {normalized_scenario}\n\n"
                "Please check that this message is accessible on this channel. "
                "This exercise does not indicate a real outage."
            ),
            author=actor,
            tags=["continuity-exercise"],
            channels=targets,
            status=AnnouncementStatus.DRAFT,
        )
        try:
            self.storage.save_announcement(announcement)
            _published, results = self.publisher.publish_draft(announcement_id)
        except Exception as exc:
            logger.exception("Continuity drill publication failed for %s", exercise_id)
            self.store.finish_exercise(exercise_id, {}, failed=True)
            raise RuntimeError(f"The drill announcement could not be published: {exc}") from exc
        delivery = {
            channel: (
                "prepared" if result.prepared else "delivered" if result.success else "failed"
            )
            for channel, result in results.items()
        }
        self.store.finish_exercise(exercise_id, delivery)
        result = self.store.get_exercise(exercise_id)
        if result is None:
            raise RuntimeError("The continuity drill report could not be loaded.")
        return result

    def review_drill(
        self, exercise_id: str, reviewer: str, access_verified: bool, problems: str
    ) -> dict[str, Any]:
        if not access_verified and not problems.strip():
            raise ValueError("Describe the problem found when access could not be verified.")
        return self.store.review_exercise(exercise_id, reviewer, access_verified, problems)
