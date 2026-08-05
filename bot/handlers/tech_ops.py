"""Remote tech-chat ops: /tech_status, /tech_restart, inline restart buttons."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from bot.config import TECH_CHAT_ID, TEST_ADMIN_IDS
from bot.utils.systemd_ops import UNITS, restart_unit, status_report

logger = logging.getLogger(__name__)
router = Router(name="tech_ops")


def _tech_chat() -> int | None:
    raw = (TECH_CHAT_ID or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _is_tech_admin(chat_id: int | None, user_id: int | None) -> bool:
    tech = _tech_chat()
    if tech is None or chat_id is None or user_id is None:
        return False
    if chat_id != tech:
        return False
    if not TEST_ADMIN_IDS:
        return False
    return user_id in TEST_ADMIN_IDS


@router.message(Command("tech_status"))
async def cmd_tech_status(message: Message):
    if not _is_tech_admin(message.chat.id, message.from_user.id if message.from_user else None):
        return
    await message.answer(f"<b>Статус сервисов</b>\n<pre>{status_report()}</pre>", parse_mode="HTML")


@router.message(Command("tech_restart"))
async def cmd_tech_restart(message: Message, command: CommandObject):
    if not _is_tech_admin(message.chat.id, message.from_user.id if message.from_user else None):
        return
    alias = (command.args or "").strip().lower()
    if not alias:
        await message.answer(
            "Использование: <code>/tech_restart bot</code> | <code>vk</code> | <code>admin</code>",
            parse_mode="HTML",
        )
        return
    if alias not in UNITS:
        await message.answer(f"Можно только: {', '.join(UNITS)}")
        return
    if alias == "bot":
        # Answer first — systemctl will SIGTERM this process.
        await message.answer(
            "Перезапуск <code>standup-bot</code> запущен. "
            "Бот сейчас кратко отключится; через несколько секунд снова ответит. "
            "Проверка: /tech_status",
            parse_mode="HTML",
        )
        ok, text = restart_unit(alias)
        if not ok:
            await message.answer(text)
            logger.warning("tech_restart bot failed: %s", text)
        else:
            logger.info("tech_restart bot scheduled by=%s", message.from_user.id)
        return
    await message.answer(f"Перезапускаю <code>{alias}</code>…", parse_mode="HTML")
    ok, text = restart_unit(alias)
    await message.answer(text)
    if ok:
        logger.info("tech_restart ok alias=%s by=%s", alias, message.from_user.id)
    else:
        logger.warning("tech_restart failed alias=%s: %s", alias, text)


@router.message(Command("tech_help"))
async def cmd_tech_help(message: Message):
    if not _is_tech_admin(message.chat.id, message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "<b>Техчат — команды</b>\n"
        "• <code>/tech_status</code> — статус bot / vk / admin\n"
        "• <code>/tech_restart vk</code> — перезапуск (bot | vk | admin)\n"
        "• <code>/tech_help</code> — эта справка\n"
        "• <code>/techid</code> — chat_id текущего чата\n\n"
        "На алертах «проблема» можно жать кнопку Restart.",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "tech:status")
async def cb_tech_status(call: CallbackQuery):
    chat_id = call.message.chat.id if call.message else None
    if not _is_tech_admin(chat_id, call.from_user.id if call.from_user else None):
        await call.answer("Нет доступа", show_alert=True)
        return
    await call.answer()
    if call.message:
        await call.message.answer(
            f"<b>Статус сервисов</b>\n<pre>{status_report()}</pre>",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("tech:rst:"))
async def cb_tech_restart(call: CallbackQuery):
    chat_id = call.message.chat.id if call.message else None
    if not _is_tech_admin(chat_id, call.from_user.id if call.from_user else None):
        await call.answer("Нет доступа", show_alert=True)
        return
    alias = (call.data or "").split(":")[-1].strip().lower()
    if alias not in UNITS:
        await call.answer("Неизвестный сервис", show_alert=True)
        return
    await call.answer(f"Restart {alias}…")
    who = call.from_user.id if call.from_user else "?"
    if alias == "bot":
        if call.message:
            await call.message.answer(
                "Перезапуск <code>standup-bot</code> запущен "
                f"(от {who}). Проверка через пару секунд: /tech_status",
                parse_mode="HTML",
            )
        ok, text = restart_unit(alias)
        if not ok and call.message:
            await call.message.answer(text)
            logger.warning("tech callback restart bot failed: %s", text)
        return
    ok, text = restart_unit(alias)
    if call.message:
        await call.message.answer(f"{text}\n\n(от {who})")
    if not ok:
        logger.warning("tech callback restart failed alias=%s: %s", alias, text)
