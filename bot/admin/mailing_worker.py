"""Background loop that sends queued mailing campaigns."""

from __future__ import annotations

import asyncio
import logging
import os
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


def _bot_token() -> str:
    return (os.getenv("BOT_TOKEN") or "").strip()


async def _send_telegram(campaign: dict, peer_id: int) -> None:
    from aiogram import Bot
    from aiogram.types import (
        FSInputFile,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
    )

    token = _bot_token()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан")

    text = (campaign.get("body_html") or "").strip()
    photo_path = (campaign.get("photo_path") or "").strip()
    button_text = (campaign.get("button_text") or "").strip()
    button_url = (campaign.get("button_url") or "").strip()
    followup = (campaign.get("followup_html") or "").strip()

    markup = None
    if button_text:
        if button_url:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=button_text, url=button_url)]
                ]
            )
        elif followup:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=button_text,
                            callback_data=f"mail_fu:{int(campaign['id'])}",
                        )
                    ]
                ]
            )

    bot = Bot(token=token)
    try:
        if photo_path and Path(photo_path).is_file():
            await bot.send_photo(
                chat_id=int(peer_id),
                photo=FSInputFile(photo_path),
                caption=text or None,
                parse_mode="HTML" if text else None,
                reply_markup=markup,
            )
        else:
            if not text:
                raise RuntimeError("Пустое сообщение")
            await bot.send_message(
                chat_id=int(peer_id),
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
                disable_web_page_preview=False,
            )
    finally:
        await bot.session.close()


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


async def send_one(campaign: dict, recipient: dict) -> None:
    channel = recipient.get("channel")
    peer_id = int(recipient["peer_id"])
    if channel == "telegram":
        await _send_telegram(campaign, peer_id)
    elif channel == "vkontakte":
        await _send_vkontakte(campaign, peer_id)
    else:
        raise RuntimeError(f"unknown channel {channel}")


async def mailing_worker_loop() -> None:
    ensure_mailing_tables()
    logger.info("mailing worker started")
    while True:
        try:
            campaign = await asyncio.to_thread(claim_next_campaign)
            if not campaign:
                await asyncio.sleep(2.0)
                continue

            campaign_id = int(campaign["id"])
            interval = float(campaign.get("interval_sec") or 0.1)
            interval = max(0.0, min(interval, 60.0))

            while True:
                fresh = await asyncio.to_thread(get_campaign, campaign_id)
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

                batch = await asyncio.to_thread(fetch_pending_recipients, campaign_id, 20)
                if not batch:
                    await asyncio.to_thread(finalize_if_complete, campaign_id)
                    break

                for recipient in batch:
                    fresh = await asyncio.to_thread(get_campaign, campaign_id)
                    if not fresh or fresh.get("status") != "running":
                        break
                    try:
                        await send_one(fresh, recipient)
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
                            error=str(exc),
                        )
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
        task = _app.get("mailing_worker")
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
