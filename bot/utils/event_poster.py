"""Загрузка постера мероприятия по image_url из афиши (без случайных обложек)."""

from __future__ import annotations

import logging
import re
from io import BytesIO
from pathlib import PurePosixPath
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)


def _filename_from_url(url: str) -> str:
    path = urlparse(url).path
    name = PurePosixPath(path).name or "poster.jpg"
    # Tilda: .../file.png.webp или .../format/webp/file.png — расширение врёт.
    lower = name.lower()
    if lower.endswith(".webp") or ".webp." in lower or lower.endswith(".png.webp"):
        name = re.sub(r"(\.png)?\.webp$", "", name, flags=re.I) + ".jpg"
    elif "." not in name:
        name = f"{name}.jpg"
    return name[:80]


def normalize_poster_url(url: str) -> str:
    """Tilda optim часто отдаёт webp под .png — просим jpeg с CDN, если путь позволяет."""
    raw = (url or "").strip()
    if "tildacdn.com" not in raw.casefold():
        return raw
    # /-/format/webp/ → /-/format/jpg/
    fixed = re.sub(
        r"/-/format/webp/",
        "/-/format/jpg/",
        raw,
        flags=re.IGNORECASE,
    )
    # ...file.png.webp → ...file.jpg (на всякий)
    fixed = re.sub(r"\.png\.webp(\?|$)", r".jpg\1", fixed, flags=re.IGNORECASE)
    return fixed


def _poster_url_candidates(url: str) -> list[str]:
    """Варианты URL: jpg-формат, оригинал, без /-/format/…/, static вместо optim."""
    primary = (url or "").strip()
    if not primary:
        return []
    alt = normalize_poster_url(primary)
    bare = re.sub(r"/-/format/[^/]+/", "/", primary, count=1, flags=re.IGNORECASE)
    static = primary.replace("://optim.tildacdn.com/", "://static.tildacdn.com/", 1)
    static_bare = bare.replace("://optim.tildacdn.com/", "://static.tildacdn.com/", 1)
    out: list[str] = []
    for candidate in (alt, primary, bare, static, static_bare):
        c = (candidate or "").strip()
        if c and c not in out:
            out.append(c)
    return out


def _looks_like_jpeg(data: bytes) -> bool:
    return len(data) >= 3 and data[:3] == b"\xff\xd8\xff"


def _looks_like_webp(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def poster_bytes_to_jpeg(image_bytes: bytes, *, max_side: int = 2560, quality: int = 85) -> bytes:
    """Любой формат (webp/png/…) → JPEG. Иначе TG путает Content-Type и имя .png."""
    from PIL import Image

    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    out = BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    data = out.getvalue()
    if not data or not _looks_like_jpeg(data):
        raise RuntimeError("empty or non-JPEG after normalize")
    return data


async def download_poster_bytes(url: str) -> bytes:
    timeout = aiohttp.ClientTimeout(total=30)
    # Не просим webp в Accept — Tilda иначе отдаёт webp даже на /format/jpg/.
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "image/jpeg,image/png,image/*;q=0.8,*/*;q=0.5",
        "Referer": "https://tilda.cc/",
    }
    candidates = _poster_url_candidates(url)

    last_err: Exception | None = None
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for candidate in candidates:
            try:
                async with session.get(candidate, allow_redirects=True) as resp:
                    if resp.status >= 400:
                        raise RuntimeError(f"HTTP {resp.status} for {candidate}")
                    data = await resp.read()
                if not data:
                    raise RuntimeError(f"Empty image for {candidate}")
                if _looks_like_jpeg(data) and not _looks_like_webp(data):
                    try:
                        return poster_bytes_to_jpeg(data)
                    except Exception:
                        return data
                return poster_bytes_to_jpeg(data)
            except Exception as exc:
                last_err = exc
                logger.warning("Poster download failed for %s: %s", candidate, exc)
    raise RuntimeError(f"Failed to download poster {url}: {last_err}")


async def tg_event_poster(image_url: str | None):
    """Фото для answer_photo / InputMediaPhoto: байты с URL афиши, иначе None.

    Не подставляет случайные обложки — только то, что в image_url мероприятия.
    """
    url = (image_url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    from aiogram.types import BufferedInputFile, URLInputFile

    try:
        data = await download_poster_bytes(url)
        name = _filename_from_url(url)
        if not name.lower().endswith((".jpg", ".jpeg")):
            name = (name.rsplit(".", 1)[0] if "." in name else name) + ".jpg"
        return BufferedInputFile(data, filename=name)
    except Exception:
        logger.warning("Failed to download event poster %s, trying URLInputFile", url, exc_info=True)
        try:
            return URLInputFile(normalize_poster_url(url), filename=_filename_from_url(url))
        except Exception:
            logger.exception("URLInputFile also failed for %s", url)
            return None


async def tg_send_event_card(
    message,
    image_url: str | None,
    *,
    caption: str,
    reply_markup=None,
    parse_mode: str | None = None,
):
    """Карточка шоу: постер из афиши или текст без фото (не random cover)."""
    photo = await tg_event_poster(image_url)
    if photo is not None:
        try:
            return await message.answer_photo(
                photo=photo,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        except Exception:
            logger.exception("Failed to send TG event poster for %s", image_url)
    return await message.answer(caption, reply_markup=reply_markup, parse_mode=parse_mode)
