"""Admin: notify about cancelled show + send BEST raffle date menu (Telegram)."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CANCEL_TEXT = (
    "Добрый день! По техническим обстоятельствам завтрашнее мероприятие "
    "Escobar 20:00 отменяется, Вы можете выбрать любую другую дату."
)

DATES_CAPTION = "Выбирай дату мероприятия в рамках розыгрыша 👇"


async def send_cancel_and_raffle_dates(
    *,
    telegram_id: int,
    body_text: str,
    reset_raffle: bool = True,
) -> dict[str, Any]:
    """Сброс розыгрыша (опц.) → текст → меню дат BEST без сегодня."""
    from aiogram import Bot
    from bot.db.crud import reset_raffle_for_user
    from bot.handlers.rozygrysh import RAFFLE_DATES_CAPTION, _dates_kb

    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        return {"ok": False, "error": "BOT_TOKEN не задан", "telegram_id": telegram_id}

    text = (body_text or "").strip()
    if not text:
        return {"ok": False, "error": "пустой текст", "telegram_id": telegram_id}

    prep: dict = {}
    if reset_raffle:
        try:
            prep = reset_raffle_for_user(int(telegram_id)) or {}
        except Exception as exc:
            logger.exception("raffle reset failed for tg=%s", telegram_id)
            return {
                "ok": False,
                "error": f"не удалось сбросить розыгрыш: {exc}",
                "telegram_id": telegram_id,
            }

    bot = Bot(token=token)
    try:
        await bot.send_message(chat_id=int(telegram_id), text=text)
        markup, dates = await _dates_kb()
        if not dates:
            await bot.send_message(
                chat_id=int(telegram_id),
                text="Пока нет доступных дат для выбора 😔",
            )
            return {
                "ok": True,
                "warning": "нет доступных дат",
                "telegram_id": telegram_id,
                "reset": prep,
            }
        caption = RAFFLE_DATES_CAPTION or DATES_CAPTION
        await bot.send_message(
            chat_id=int(telegram_id),
            text=caption,
            reply_markup=markup,
        )
        return {"ok": True, "error": "", "telegram_id": telegram_id, "reset": prep}
    except Exception as exc:
        logger.exception("send_cancel_and_raffle_dates failed tg=%s", telegram_id)
        return {"ok": False, "error": str(exc), "telegram_id": telegram_id, "reset": prep}
    finally:
        await bot.session.close()


async def send_cancel_and_raffle_dates_bulk(
    *,
    user_ids: list[int],
    body_text: str,
    reset_raffle: bool = True,
) -> dict[str, Any]:
    """user_ids — внутренние id из таблицы users (с telegram_id)."""
    from bot.db.mailing import get_user_for_mailing

    items = []
    ok_n = 0
    fail_n = 0
    for uid in user_ids:
        user = get_user_for_mailing(int(uid))
        if not user:
            items.append({"user_id": uid, "ok": False, "error": "пользователь не найден"})
            fail_n += 1
            continue
        tid = user.get("telegram_id")
        if not tid:
            items.append(
                {
                    "user_id": uid,
                    "ok": False,
                    "error": "нет telegram_id",
                    "username": user.get("username") or "",
                    "name": user.get("name") or "",
                }
            )
            fail_n += 1
            continue
        one = await send_cancel_and_raffle_dates(
            telegram_id=int(tid),
            body_text=body_text,
            reset_raffle=reset_raffle,
        )
        one["user_id"] = uid
        one["username"] = user.get("username") or ""
        one["name"] = user.get("name") or ""
        items.append(one)
        if one.get("ok"):
            ok_n += 1
        else:
            fail_n += 1
    return {"ok": ok_n, "fail": fail_n, "items": items, "total": len(items)}
