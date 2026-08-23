"""Background loop that sends queued mailing campaigns."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from bot.db.mailing import (
    claim_next_campaign,
    ensure_mailing_tables,
    finalize_if_complete,
    fetch_pending_recipients,
    get_campaign,
    mark_recipient,
)

logger = logging.getLogger(__name__)

_worker_started = False

# Переиспользуем HTTP-сессию бота на всю жизнь воркера — иначе TLS на каждое сообщение.
_tg_bot = None
# file_id картинки по campaign_id: без повторной загрузки одного и того же файла.
_tg_photo_file_ids: dict[int, str] = {}


def _bot_token() -> str:
    return (os.getenv("BOT_TOKEN") or "").strip()


async def _get_tg_bot():
    global _tg_bot
    from aiogram import Bot

    token = _bot_token()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан")
    if _tg_bot is None or getattr(_tg_bot, "token", None) != token:
        if _tg_bot is not None:
            try:
                await _tg_bot.session.close()
            except Exception:
                pass
        _tg_bot = Bot(token=token)
    return _tg_bot


def _tg_markup(campaign: dict):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    button_text = (campaign.get("button_text") or "").strip()
    button_url = (campaign.get("button_url") or "").strip()
    followup = (campaign.get("followup_html") or "").strip()
    if not button_text:
        return None
    if button_url:
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=button_text, url=button_url)]]
        )
    if followup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=button_text,
                        callback_data=f"mail_fu:{int(campaign['id'])}",
                    )
                ]
            ]
        )
    return None


def _tg_preview_kwargs(campaign: dict) -> dict:
    if not bool(campaign.get("disable_link_preview")):
        return {}
    try:
        from aiogram.types import LinkPreviewOptions

        return {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
    except Exception:
        return {"disable_web_page_preview": True}


async def _send_telegram(
    campaign: dict,
    peer_id: int,
    *,
    bot=None,
    photo_file_id: str | None = None,
) -> str | None:
    """Send one TG message. Returns photo file_id if a photo was sent (for reuse)."""
    from aiogram.types import FSInputFile

    bot = bot or await _get_tg_bot()
    text = (campaign.get("body_html") or "").strip()
    photo_path = (campaign.get("photo_path") or "").strip()
    markup = _tg_markup(campaign)
    send_kwargs = _tg_preview_kwargs(campaign)
    campaign_id = int(campaign["id"])

    if photo_path and Path(photo_path).is_file():
        cached = photo_file_id or _tg_photo_file_ids.get(campaign_id)
        photo = cached if cached else FSInputFile(photo_path)
        msg = await bot.send_photo(
            chat_id=int(peer_id),
            photo=photo,
            caption=text or None,
            parse_mode="HTML" if text else None,
            reply_markup=markup,
        )
        file_id = None
        if msg.photo:
            file_id = msg.photo[-1].file_id
            _tg_photo_file_ids[campaign_id] = file_id
        return file_id

    if not text:
        raise RuntimeError("Пустое сообщение")
    await bot.send_message(
        chat_id=int(peer_id),
        text=text,
        parse_mode="HTML",
        reply_markup=markup,
        **send_kwargs,
    )
    return None


async def _send_vkontakte(campaign: dict, peer_id: int) -> None:
    from bot.vk.client import VKClient
    from bot.vk.config import load_vk_settings
    from bot.vk.keyboards import VKKeyboardBuilder

    settings = load_vk_settings()
    if not settings.is_configured:
        raise RuntimeError("VK не настроен")

    text = (campaign.get("body_html") or "").strip() or " "
    photo_path = (campaign.get("photo_path") or "").strip()
    button_text = (campaign.get("button_text") or "").strip()
    button_url = (campaign.get("button_url") or "").strip()
    followup = (campaign.get("followup_html") or "").strip()

    client = VKClient(settings)
    attachment = None
    if photo_path and Path(photo_path).is_file():
        data = Path(photo_path).read_bytes()
        attachment = await client.upload_message_photo(
            int(peer_id),
            data,
            filename=Path(photo_path).name,
        )

    keyboard = None
    if button_text:
        kb = VKKeyboardBuilder(inline=True)
        if button_url:
            kb.button(button_text, link=button_url, color="primary")
        elif followup:
            kb.button(
                button_text,
                {"cmd": "mail_fu", "cid": int(campaign["id"])},
                color="primary",
                callback=True,
            )
        keyboard = kb.as_json()

    await client.send_message(
        int(peer_id),
        text,
        keyboard=keyboard,
        attachment=attachment,
    )


async def send_one(campaign: dict, recipient: dict, *, bot=None) -> None:
    channel = recipient.get("channel")
    peer_id = int(recipient["peer_id"])
    if channel == "telegram":
        await _send_telegram(campaign, peer_id, bot=bot)
    elif channel == "vkontakte":
        await _send_vkontakte(campaign, peer_id)
    else:
        raise RuntimeError(f"unknown channel {channel}")


def _is_flood_wait(exc: BaseException) -> float | None:
    """Return retry-after seconds if this is a Telegram flood/retry error."""
    retry = getattr(exc, "retry_after", None)
    if retry is not None:
        try:
            return float(retry)
        except (TypeError, ValueError):
            pass
    # aiogram TelegramRetryAfter
    if exc.__class__.__name__ in {"TelegramRetryAfter", "RetryAfter"}:
        try:
            return float(getattr(exc, "retry_after", 1) or 1)
        except (TypeError, ValueError):
            return 1.0
    text = str(exc).lower()
    if "retry after" in text or "flood" in text or "too many requests" in text:
        return 5.0
    return None


async def _send_with_flood_retry(campaign: dict, recipient: dict, *, bot=None) -> None:
    attempts = 0
    while True:
        try:
            await send_one(campaign, recipient, bot=bot)
            return
        except Exception as exc:
            wait = _is_flood_wait(exc)
            attempts += 1
            if wait is None or attempts > 5:
                raise
            sleep_for = min(max(wait, 0.5), 60.0)
            logger.warning(
                "mailing flood wait %.1fs campaign=%s recipient=%s",
                sleep_for,
                campaign.get("id"),
                recipient.get("id"),
            )
            await asyncio.sleep(sleep_for)


async def mailing_worker_loop() -> None:
    ensure_mailing_tables()
    logger.info("mailing worker started")
    bot = None
    try:
        bot = await _get_tg_bot()
    except Exception:
        logger.warning("mailing worker: TG bot not ready yet (BOT_TOKEN?)")

    while True:
        try:
            campaign = await asyncio.to_thread(claim_next_campaign)
            if not campaign:
                await asyncio.sleep(2.0)
                continue

            campaign_id = int(campaign["id"])
            interval = float(campaign.get("interval_sec") or 0.1)
            interval = max(0.0, min(interval, 60.0))
            # Статус паузы/отмены — не на каждое сообщение (дорого через to_thread).
            status_check_every = 25
            sent_in_run = 0
            last_status_check = 0.0
            try:
                bot = await _get_tg_bot()
            except Exception:
                bot = None

            while True:
                now = time.monotonic()
                if now - last_status_check >= 2.0 or sent_in_run % status_check_every == 0:
                    fresh = await asyncio.to_thread(get_campaign, campaign_id)
                    last_status_check = now
                    if not fresh:
                        break
                    status = fresh.get("status")
                    if status == "paused":
                        await asyncio.sleep(2.0)
                        break
                    if status in ("cancelled", "done"):
                        break
                    if status != "running":
                        break
                    campaign = fresh
                else:
                    fresh = campaign

                batch = await asyncio.to_thread(fetch_pending_recipients, campaign_id, 50)
                if not batch:
                    await asyncio.to_thread(finalize_if_complete, campaign_id)
                    break

                for recipient in batch:
                    if sent_in_run > 0 and sent_in_run % status_check_every == 0:
                        paused = await asyncio.to_thread(get_campaign, campaign_id)
                        last_status_check = time.monotonic()
                        if not paused or paused.get("status") != "running":
                            break
                        fresh = paused
                        campaign = paused
                    try:
                        await _send_with_flood_retry(fresh, recipient, bot=bot)
                        await asyncio.to_thread(
                            mark_recipient, int(recipient["id"]), status="sent"
                        )
                    except Exception as exc:
                        logger.warning(
                            "mailing send fail campaign=%s recipient=%s: %s",
                            campaign_id,
                            recipient.get("id"),
                            exc,
                        )
                        await asyncio.to_thread(
                            mark_recipient,
                            int(recipient["id"]),
                            status="failed",
                            error=str(exc)[:500],
                        )
                    sent_in_run += 1
                    if interval > 0:
                        await asyncio.sleep(interval)
        except Exception:
            logger.exception("mailing worker iteration failed")
            await asyncio.sleep(3.0)


def start_mailing_worker(app) -> None:
    global _worker_started
    if _worker_started:
        return
    _worker_started = True

    async def _on_startup(_app):
        ensure_mailing_tables()
        _app["mailing_worker"] = asyncio.create_task(mailing_worker_loop())

    async def _on_cleanup(_app):
        global _tg_bot
        task = _app.get("mailing_worker")
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if _tg_bot is not None:
            try:
                await _tg_bot.session.close()
            except Exception:
                pass
            _tg_bot = None

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
