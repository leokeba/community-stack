"""Mirror chosen Matrix rooms into announcements on other channels.

The stack's Matrix announcements account follows the rooms. A message from someone with
enough power in the room becomes an announcement; its original sender can edit it, and
deleting it withdraws the announcement. Messages from the account itself are ignored, so
the account's own posts are never mirrored back.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from community_stack.announcements.adapters.matrix_room import MatrixRoomAdapter
from community_stack.announcements.models import AnnouncementStatus, ModelValidationError
from community_stack.matrix_ids import ROOM_ID

logger = logging.getLogger(__name__)

MODERATOR_POWER = 50
MAX_PER_ROOM_PER_HOUR = 10
BACKFILL_PAGE_SIZE = 100
MAX_BACKFILL_PAGES = 20
TITLE_LENGTH = 120
TEXT_TYPES = ("m.text", "m.notice")


class MatrixTimelineGap(RuntimeError):
    """The limited timeline could not be backfilled to the last processed event."""


def split_message(text: str) -> tuple[str, str]:
    """First line as the title, the rest as the body (or the whole text if one line)."""
    text = text.strip()
    first, _, rest = text.partition("\n")
    first = first.strip().lstrip("#").strip()
    if rest.strip():
        return first[:TITLE_LENGTH], rest.strip()
    title = first if len(first) <= TITLE_LENGTH else first[: TITLE_LENGTH - 1].rstrip() + "…"
    return title, text


class MatrixMirror:
    def __init__(self, storage, publisher, matrix: MatrixRoomAdapter, review_required) -> None:
        self.storage = storage
        self.publisher = publisher
        self.matrix = matrix
        self.review_required = review_required
        with storage._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS mirror_rooms (
                    room_id TEXT PRIMARY KEY,
                    alias TEXT,
                    mode TEXT NOT NULL,
                    min_power INTEGER NOT NULL,
                    channels_json TEXT NOT NULL,
                    added_by TEXT,
                    added_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mirror_events (
                    event_id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    announcement_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mirror_room_cursors (
                    room_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(mirror_events)")}
            if "sender" not in columns:
                conn.execute("ALTER TABLE mirror_events ADD COLUMN sender TEXT")
        self._room_create_cache: dict[str, dict[str, Any]] = {}

    # Matrix calls go through the announcements adapter, with its token and transport.

    def _call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        return self.matrix._http_request(method, path, payload)

    def bot_user(self) -> str | None:
        cached = self.storage.get_setting("mirror_bot_user", "")
        if cached:
            return cached
        status, body = self._call("GET", "/_matrix/client/v3/account/whoami")
        user = body.get("user_id") if status == 200 else None
        if user:
            self.storage.set_setting("mirror_bot_user", user)
        return user

    # Rooms

    def rooms(self) -> list[dict]:
        with self.storage._get_connection() as conn:
            rows = conn.execute("SELECT * FROM mirror_rooms ORDER BY added_at").fetchall()
        rooms = []
        for row in rows:
            room = dict(row)
            room["channels"] = json.loads(room.pop("channels_json"))
            rooms.append(room)
        return rooms

    def add_room(
        self, room: str, mode: str, min_power: int, channels: list[str], actor: str | None
    ) -> dict:
        """Join a room (ID or alias) and start mirroring it."""
        if mode not in ("publish", "review"):
            raise ValueError("Choose to publish directly or to create drafts for review.")
        is_alias = room.startswith("#") and ":" in room
        if not (is_alias or ROOM_ID.fullmatch(room)) or len(room) > 255:
            raise ValueError("Enter a room address like #news:matrix.ourtown.org.")
        if room == self.matrix.room_id:
            raise ValueError("That is the announcements room itself; it cannot be mirrored.")
        status, body = self._call("POST", f"/_matrix/client/v3/join/{quote(room, safe='')}", {})
        if status != 200 or not body.get("room_id"):
            raise ValueError(
                "The announcements account could not join that room. Invite it to the room "
                f"first. ({body.get('error', status)})"
            )
        room_id = body["room_id"]
        if room_id == self.matrix.room_id:
            raise ValueError("That is the announcements room itself; it cannot be mirrored.")
        with self.storage._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO mirror_rooms (room_id, alias, mode, min_power, channels_json,
                    added_by, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(room_id) DO UPDATE SET alias=excluded.alias, mode=excluded.mode,
                    min_power=excluded.min_power, channels_json=excluded.channels_json
                """,
                (
                    room_id,
                    room if room.startswith("#") else None,
                    mode,
                    min_power,
                    json.dumps(channels),
                    actor,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        # Start from now: earlier messages in the room are not imported.
        self.storage.set_setting("mirror_sync_token", "")
        return {"room_id": room_id}

    def remove_room(self, room_id: str) -> None:
        with self.storage._get_connection() as conn:
            conn.execute("DELETE FROM mirror_rooms WHERE room_id = ?", (room_id,))

    # Sync

    def _filter(self, room_ids: list[str]) -> str:
        return json.dumps(
            {
                "room": {
                    "rooms": room_ids,
                    "timeline": {"limit": 50, "types": ["m.room.message", "m.room.redaction"]},
                    "state": {"types": []},
                    "ephemeral": {"types": []},
                    "account_data": {"types": []},
                },
                "presence": {"types": []},
                "account_data": {"types": []},
            }
        )

    def sync_once(self) -> int:
        """Fetch new events from mirrored rooms and act on them; returns how many acted on."""
        rooms = {room["room_id"]: room for room in self.rooms()}
        if not rooms:
            return 0
        since = self.storage.get_setting("mirror_sync_token", "")
        query = f"filter={quote(self._filter(list(rooms)))}&timeout=0"
        if since:
            query += f"&since={quote(since)}"
        status, body = self._call("GET", f"/_matrix/client/v3/sync?{query}")
        if status != 200 or "next_batch" not in body:
            logger.warning("Matrix sync for mirroring failed: %s", body.get("error", status))
            return 0
        if not since:
            for room_id, joined in (body.get("rooms", {}).get("join") or {}).items():
                events = (joined.get("timeline") or {}).get("events") or []
                if events and events[-1].get("event_id"):
                    self._set_room_cursor(room_id, events[-1]["event_id"])
            self.storage.set_setting("mirror_sync_token", body["next_batch"])
            return 0
        bot = self.bot_user()
        acted = 0
        for room_id, joined in (body.get("rooms", {}).get("join") or {}).items():
            room = rooms.get(room_id)
            if room is None:
                continue
            timeline = joined.get("timeline") or {}
            try:
                events = self._timeline_events(room_id, timeline, self._room_cursor(room_id))
            except MatrixTimelineGap as exc:
                logger.warning(
                    "Matrix timeline gap for room %s; sync token was not advanced: %s",
                    room_id,
                    exc,
                )
                return 0
            for event in events:
                if event.get("sender") == bot:
                    continue
                try:
                    acted += self._handle(room, event)
                except (ValueError, KeyError, ModelValidationError) as exc:
                    logger.warning("Could not mirror %s: %s", event.get("event_id"), exc)
            if events and events[-1].get("event_id"):
                self._set_room_cursor(room_id, events[-1]["event_id"])
        self.storage.set_setting("mirror_sync_token", body["next_batch"])
        return acted

    def _room_cursor(self, room_id: str) -> str | None:
        with self.storage._get_connection() as conn:
            row = conn.execute(
                "SELECT event_id FROM mirror_room_cursors WHERE room_id = ?", (room_id,)
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT event_id FROM mirror_events WHERE room_id = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (room_id,),
                ).fetchone()
        return row["event_id"] if row else None

    def _set_room_cursor(self, room_id: str, event_id: str) -> None:
        with self.storage._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO mirror_room_cursors (room_id, event_id) VALUES (?, ?)
                ON CONFLICT(room_id) DO UPDATE SET event_id=excluded.event_id
                """,
                (room_id, event_id),
            )

    def _timeline_events(
        self, room_id: str, timeline: dict, cursor: str | None
    ) -> list[dict[str, Any]]:
        events = timeline.get("events") or []
        if cursor:
            cursor_index = next(
                (
                    index
                    for index in range(len(events) - 1, -1, -1)
                    if events[index].get("event_id") == cursor
                ),
                None,
            )
            if cursor_index is not None:
                return events[cursor_index + 1 :]
        if not timeline.get("limited") or not cursor:
            return events

        token = timeline.get("prev_batch")
        if not isinstance(token, str) or not token:
            raise MatrixTimelineGap("the limited timeline did not include a prev_batch token")
        pages = []
        for _ in range(MAX_BACKFILL_PAGES):
            path = (
                f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/messages"
                f"?from={quote(token)}&dir=b&limit={BACKFILL_PAGE_SIZE}"
            )
            status, body = self._call("GET", path)
            if status != 200 or not isinstance(body.get("chunk"), list):
                raise MatrixTimelineGap(f"history pagination failed with status {status}")
            chunk = body["chunk"]
            cursor_index = next(
                (index for index, event in enumerate(chunk) if event.get("event_id") == cursor),
                None,
            )
            if cursor_index is not None:
                result = list(reversed(chunk[:cursor_index]))
                for page in reversed(pages):
                    result.extend(reversed(page))
                return result + events
            pages.append(chunk)
            next_token = body.get("end")
            if (
                not chunk
                or not isinstance(next_token, str)
                or not next_token
                or next_token == token
            ):
                break
            token = next_token
        raise MatrixTimelineGap(
            f"could not find the previous event within {MAX_BACKFILL_PAGES} history pages"
        )

    def _power(self, room_id: str, user: str) -> float:
        create = self._room_create_cache.get(room_id)
        if create is None:
            status, event = self._call(
                "GET",
                f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state/m.room.create",
            )
            if status == 200 and isinstance(event, dict):
                create = event
                self._room_create_cache[room_id] = create
        content = (create or {}).get("content") or {}
        creators = {create.get("sender"), content.get("creator")} | set(
            content.get("additional_creators") or []
        )
        if str(content.get("room_version", "1")) == "12" and user in creators:
            return float("inf")

        status, levels = self._call(
            "GET",
            f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state/m.room.power_levels",
        )
        if status != 200:
            return 100 if status == 404 and user in creators else 0
        if not levels:
            return 100 if user in creators else 0
        return int((levels.get("users") or {}).get(user, levels.get("users_default", 0)))

    def _mapped(self, event_id: str) -> str | None:
        with self.storage._get_connection() as conn:
            row = conn.execute(
                "SELECT announcement_id FROM mirror_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return row["announcement_id"] if row else None

    def _mapped_sender(self, event_id: str) -> str | None:
        with self.storage._get_connection() as conn:
            row = conn.execute(
                "SELECT sender FROM mirror_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return row["sender"] if row else None

    def _recent_count(self, room_id: str) -> int:
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with self.storage._get_connection() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM mirror_events WHERE room_id = ? AND created_at >= ?",
                (room_id, since),
            ).fetchone()[0]

    def _handle(self, room: dict, event: dict[str, Any]) -> int:
        kind, sender = event.get("type"), event.get("sender", "")
        if self._power(room["room_id"], sender) < room["min_power"]:
            return 0
        if kind == "m.room.redaction":
            announcement_id = self._mapped(event.get("redacts", ""))
            if announcement_id:
                announcement = self.publisher.preview(announcement_id)
                if announcement.status != AnnouncementStatus.WITHDRAWN:
                    self.publisher.withdraw(announcement_id)
                    return 1
            return 0

        content = event.get("content") or {}
        relation = content.get("m.relates_to") or {}
        if relation.get("rel_type") == "m.replace":
            original_event_id = relation.get("event_id", "")
            announcement_id = self._mapped(original_event_id)
            new = content.get("m.new_content") or {}
            if (
                not announcement_id
                or sender != self._mapped_sender(original_event_id)
                or new.get("msgtype") not in TEXT_TYPES
            ):
                return 0
            announcement = self.publisher.preview(announcement_id)
            if announcement.status == AnnouncementStatus.WITHDRAWN:
                return 0
            needs_review = room["mode"] == "review" or self.review_required()
            if needs_review:
                if announcement.status in (
                    AnnouncementStatus.PUBLISHED,
                    AnnouncementStatus.UPDATED,
                ):
                    logger.info(
                        "Ignoring Matrix edit %s because announcement %s is already published "
                        "and review is required.",
                        event.get("event_id"),
                        announcement_id,
                    )
                    return 0
                if announcement.status in (
                    AnnouncementStatus.APPROVED,
                    AnnouncementStatus.SCHEDULED,
                ):
                    self.publisher.set_review_status(
                        announcement_id,
                        (announcement.status,),
                        AnnouncementStatus.IN_REVIEW,
                    )
            title, body = split_message(new.get("body", ""))
            self.publisher.update(announcement_id, title=title, body=body)
            return 1
        if content.get("msgtype") not in TEXT_TYPES or not content.get("body", "").strip():
            return 0
        if (
            self._mapped(event["event_id"])
            or self._recent_count(room["room_id"]) >= MAX_PER_ROOM_PER_HOUR
        ):
            return 0

        title, body = split_message(content["body"])
        draft = self.publisher.create_draft(title=title, body=body, author=None)
        draft.channels = room["channels"]
        self.storage.save_announcement(draft)
        with self.storage._get_connection() as conn:
            conn.execute(
                "INSERT INTO mirror_events (event_id, room_id, announcement_id, created_at, sender) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    event["event_id"],
                    room["room_id"],
                    draft.id,
                    datetime.now(timezone.utc).isoformat(),
                    sender,
                ),
            )
        if room["mode"] == "review" or self.review_required():
            self.publisher.set_review_status(
                draft.id, (AnnouncementStatus.DRAFT,), AnnouncementStatus.IN_REVIEW
            )
            self.storage.add_review_event(draft.id, "submitted", f"matrix:{sender}", "From Matrix.")
        else:
            self.publisher.publish_draft(draft.id)
        return 1


def mirror_for(storage, publisher, review_required) -> MatrixMirror | None:
    """The mirror, when the Matrix announcements account is configured."""
    matrix = next((a for a in publisher.adapters if isinstance(a, MatrixRoomAdapter)), None)
    if matrix is None or (not matrix.access_token and matrix._custom_transport is None):
        return None
    return MatrixMirror(storage, publisher, matrix, review_required)
