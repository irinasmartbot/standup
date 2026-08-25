import json
import logging
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

_MAX_RANDOM_PHOTO_SIZE = 5 * 1024 * 1024
_DEFAULT_SHOW_COVER = "фото/IMG_20220511_201818.jpg"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_vk_system_images_cache_path(cache_path: str) -> Path:
    """Найти vk_system_images.json: app, cwd или соседний vk-app на VPS."""
    raw = Path(cache_path or "data/storage/vk_system_images.json")
    if raw.is_absolute():
        return raw
    root = _repo_root()
    candidates = [
        root / raw,
        Path.cwd() / raw,
        root.parent / "vk-app" / raw,
    ]
    for path in candidates:
        if path.exists():
            return path
    return root / raw


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


def load_vk_system_images_cache(cache_path: str) -> VKSystemImageCache:
    resolved = resolve_vk_system_images_cache_path(cache_path)
    cache = VKSystemImageCache(str(resolved))
    logger.info("VK system images cache: %s (items=%s)", resolved, len(cache.all()))
    return cache


# Как в TG / VK-боте: не крутим в рандоме карточки площадок / хитлото / билет / отзывы.
_EXCLUDED_RANDOM_COVER_KEYS = frozenset(
    {
        "temple_bar",
        "escobar",
        "nebar",
        "hitloto_start",
        "photo_2026-07-21_01-59-43",
        "rozygrysh_otzyv_1",
        "rozygrysh_otzyv_2",
    }
)
_EXCLUDED_RANDOM_COVER_NAMES = frozenset(
    {
        "temple_bar.jpg",
        "escobar.jpg",
        "nebar.jpg",
        "ticket_template.jpg",
        "hitloto_start.png",
        "photo_2026-07-21_01-59-43.jpg",
    }
)


def system_cover_attachment(cache: VKSystemImageCache, *keys: str) -> str | None:
    for key in keys:
        attachment = cache.get(key)
        if attachment:
            return attachment
    return None


def random_show_cover_attachment(cache: VKSystemImageCache) -> str | None:
    """Случайная обложка шоу из кэша VK (без площадок / хитлото / билета)."""
    banned_keys = set(_EXCLUDED_RANDOM_COVER_KEYS)
    banned_attachments = {(cache.get(key) or "").strip() for key in banned_keys}
    banned_attachments.discard("")
    pool: list[str] = []
    for img in cache.all():
        key = (img.key or "").strip().lower()
        path = (img.path or "").replace("\\", "/").lower()
        name = path.rsplit("/", 1)[-1] if path else key
        attachment = (img.attachment or "").strip()
        if not key or not attachment:
            continue
        if attachment in banned_attachments:
            continue
        if key in banned_keys or name in _EXCLUDED_RANDOM_COVER_NAMES:
            continue
        if (
            key.startswith("hitloto")
            or name.startswith("hitloto")
            or "hitloto" in key
            or "hitloto" in name
            or "хитлото" in key
            or "хитлото" in name
            or key.startswith("rozygrysh_otzyv")
            or name.startswith("rozygrysh_otzyv")
        ):
            continue
        if "ticket" in key or "ticket" in name:
            continue
        pool.append(attachment)
    if pool:
        return random.choice(pool)
    return None


def _is_random_cover_file(path: Path) -> bool:
    name = path.name.lower()
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        return False
    if name in _EXCLUDED_RANDOM_COVER_NAMES:
        return False
    if name.startswith("hitloto") or name.startswith("rozygrysh_otzyv"):
        return False
    if "ticket" in name:
        return False
    try:
        if path.stat().st_size > _MAX_RANDOM_PHOTO_SIZE:
            return False
    except OSError:
        return False
    return True


def pick_random_cover_file(*, photos_dir: Path | None = None) -> Path | None:
    directory = photos_dir or (_repo_root() / "фото")
    if not directory.is_dir():
        return None
    pool = [path for path in sorted(directory.iterdir()) if path.is_file() and _is_random_cover_file(path)]
    if pool:
        return random.choice(pool)
    fallback = _repo_root() / _DEFAULT_SHOW_COVER
    return fallback if fallback.is_file() else None


async def resolve_booking_cover_attachment(client, peer_id: int, settings) -> str | None:
    """Случайная обложка из кэша VK; если кэша нет — загрузка из папки фото/."""
    cache = load_vk_system_images_cache(settings.system_images_cache)
    attachment = random_show_cover_attachment(cache) or system_cover_attachment(cache, "show_cover")
    if attachment:
        return attachment

    cover_file = pick_random_cover_file()
    if not cover_file:
        logger.warning("VK booking cover: cache empty and no local photo in фото/")
        return None
    try:
        attachment = await client.upload_message_photo(
            int(peer_id),
            cover_file.read_bytes(),
            filename=cover_file.name,
        )
    except Exception:
        logger.exception("VK booking cover upload failed file=%s", cover_file)
        return None

    try:
        cache.set(cover_file.stem, os.path.relpath(cover_file, _repo_root()), attachment)
    except Exception:
        logger.exception("VK booking cover cache save failed path=%s", cache.cache_path)
    return attachment


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
        from bot.utils.event_poster import download_poster_bytes

        image_bytes = await download_poster_bytes(url)
        filename = _filename_from_url(url)
        if not filename.lower().endswith((".jpg", ".jpeg")):
            filename = (filename.rsplit(".", 1)[0] if "." in filename else filename) + ".jpg"
        attachment = await client.upload_message_photo(
            peer_id,
            image_bytes,
            filename=filename,
        )
        cache.set(url, attachment)
        return attachment
    except Exception:
        logger.exception("Failed to prepare VK attachment for image %s", url)
        # Сломанный кэш не оставляем — иначе будет «левая» картинка.
        return None
