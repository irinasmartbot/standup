"""Загрузка постера мероприятия по image_url из афиши (без случайных обложек)."""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)


def _filename_from_url(url: str) -> str:
    path = urlparse(url).path
    name = PurePosixPath(path).name or "poster.jpg"
    if "." not in name:
        name = f"{name}.jpg"
    return name[:80]


async def download_poster_bytes(url: str) -> bytes:
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; StandUpBot/1.0)",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status} for {url}")
            data = await resp.read()
    if not data:
        raise RuntimeError(f"Empty image for {url}")
    return data


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
        return BufferedInputFile(data, filename=_filename_from_url(url))
    except Exception:
        logger.warning("Failed to download event poster %s, trying URLInputFile", url, exc_info=True)
        try:
            return URLInputFile(url, filename=_filename_from_url(url))
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
