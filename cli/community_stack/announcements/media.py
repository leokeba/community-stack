"""Announcement images: checked, metadata stripped, resized, and described for screen readers."""

from __future__ import annotations

import io
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_DIMENSION = 1600
MAX_ALT_LENGTH = 1000
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}
MEDIA_ID = re.compile(r"^img_[A-Za-z0-9_-]{16,64}$")

# Refuse "decompression bombs": small files that expand to huge pixel counts.
Image.MAX_IMAGE_PIXELS = 40_000_000


class MediaError(ValueError):
    """An upload that is not a usable image; the message is shown to people."""


@dataclass(frozen=True)
class ProcessedImage:
    content: bytes
    extension: str
    content_type: str
    width: int
    height: int


@dataclass(frozen=True)
class MediaImage:
    """A stored image attached to an announcement."""

    id: str
    path: Path
    alt: str
    content_type: str
    width: int
    height: int

    @property
    def filename(self) -> str:
        return self.path.name

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()

    def public_url(self, canonical_url: str) -> str | None:
        """Where the public feed serves this image, derived from the announcement's URL."""
        if "/feed/announcements/" not in canonical_url:
            return None
        return canonical_url.split("/feed/announcements/", 1)[0] + f"/feed/media/{self.filename}"

    def bytes_within(self, max_bytes: int) -> tuple[bytes, str]:
        """The image re-encoded, if needed, to fit a channel's size limit."""
        data = self.read_bytes()
        if len(data) <= max_bytes:
            return data, self.content_type
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            for scale in (1.0, 0.8, 0.6, 0.45, 0.3):
                resized = image.resize(
                    (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
                )
                for quality in (85, 70, 55):
                    buffer = io.BytesIO()
                    resized.save(buffer, "JPEG", quality=quality, optimize=True)
                    if buffer.tell() <= max_bytes:
                        return buffer.getvalue(), "image/jpeg"
        raise MediaError("The image could not be made small enough for this channel.")


def new_media_id() -> str:
    return f"img_{secrets.token_urlsafe(18)}"


def validate_alt(alt: str) -> str:
    alt = " ".join(alt.split())
    if not alt:
        raise MediaError("Describe the image for people who cannot see it.")
    if len(alt) > MAX_ALT_LENGTH:
        raise MediaError(f"Keep the description under {MAX_ALT_LENGTH} characters.")
    return alt


def process_image(data: bytes) -> ProcessedImage:
    """Check an upload is a real image, apply its rotation, drop metadata, and resize it."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise MediaError("Images must be 10 MB or smaller.")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
            image_format = probe.format
        if image_format not in ALLOWED_FORMATS:
            raise MediaError("Use a JPEG, PNG, WebP or GIF image.")
        with Image.open(io.BytesIO(data)) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source)
            image.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
            has_alpha = image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            )
            buffer = io.BytesIO()
            # Re-encoding a fresh image drops EXIF (including GPS location) and text chunks.
            if has_alpha:
                image = image.convert("RGBA")
                image.save(buffer, "PNG", optimize=True)
                extension, content_type = "png", "image/png"
            else:
                image = image.convert("RGB")
                image.save(buffer, "JPEG", quality=85, optimize=True)
                extension, content_type = "jpg", "image/jpeg"
            return ProcessedImage(
                buffer.getvalue(), extension, content_type, image.width, image.height
            )
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError) as exc:
        if isinstance(exc, Image.DecompressionBombError):
            raise MediaError("That image is too large in pixels.") from exc
        raise MediaError("That file is not an image that can be used.") from exc
