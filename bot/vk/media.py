import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VKSystemImage:
    key: str
    path: str
    attachment: str


class VKSystemImageCache:
    """Stores pre-uploaded VK attachment ids for local system images.

    VK is faster and more reliable when the bot sends a saved attachment like
    photo123_456 instead of uploading the same file for every message.
    """

    def __init__(self, cache_path: str):
        self.cache_path = Path(cache_path)
        self._items = self._load()

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.cache_path.exists():
            return {}
        with self.cache_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {
            str(key): value
            for key, value in data.items()
            if isinstance(value, dict) and value.get("attachment")
        }

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cache_path.open("w", encoding="utf-8") as f:
            json.dump(self._items, f, ensure_ascii=False, indent=2)

    def get(self, key: str) -> str | None:
        item = self._items.get(key)
        return item.get("attachment") if item else None

    def set(self, key: str, path: str, attachment: str) -> None:
        self._items[key] = {
            "path": os.path.normpath(path),
            "attachment": attachment,
        }
        self.save()

    def all(self) -> list[VKSystemImage]:
        return [
            VKSystemImage(key=key, path=value.get("path", ""), attachment=value["attachment"])
            for key, value in self._items.items()
        ]


class VKRemoteImageCache:
    """Caches VK attachments for remote event poster URLs."""

    def __init__(self, cache_path: str):
        self.cache_path = Path(cache_path)
        self._items = self._load()

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.cache_path.exists():
            return {}
        try:
            with self.cache_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): value
            for key, value in data.items()
            if isinstance(value, dict) and value.get("attachment")
        }

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cache_path.open("w", encoding="utf-8") as f:
            json.dump(self._items, f, ensure_ascii=False, indent=2)

    def get(self, url: str) -> str | None:
        item = self._items.get(url.strip())
        return item.get("attachment") if item else None

    def set(self, url: str, attachment: str) -> None:
        self._items[url.strip()] = {"attachment": attachment}
        self.save()


def _filename_from_url(url: str) -> str:
    path = urlparse(url).path
    name = Path(path).name or "poster.jpg"
    if "." not in name:
        name = f"{name}.jpg"
    return name[:80]


async def download_image_bytes(url: str, *, session: aiohttp.ClientSession | None = None) -> bytes:
    close = False
    if session is None:
        from bot.vk.client import _connector

        session = aiohttp.ClientSession(connector=_connector())
        close = True
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"Failed to download image {url}: HTTP {resp.status}")
            data = await resp.read()
        if not data:
            raise RuntimeError(f"Empty image response for {url}")
        return data
    finally:
        if close:
            await session.close()


async def resolve_image_attachment(
    client: Any,
    peer_id: int,
    image_url: str | None,
    cache: VKRemoteImageCache,
) -> str | None:
    """Download remote poster once, upload to VK, reuse cached attachment id."""
    url = (image_url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    cached = cache.get(url)
    if cached:
        return cached
    try:
        image_bytes = await download_image_bytes(url)
        attachment = await client.upload_message_photo(
            peer_id,
            image_bytes,
            filename=_filename_from_url(url),
        )
        cache.set(url, attachment)
        return attachment
    except Exception:
        logger.exception("Failed to prepare VK attachment for image %s", url)
        return None
