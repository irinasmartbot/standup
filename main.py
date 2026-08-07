import asyncio
import logging
import traceback

from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.types import ErrorEvent

from bot.config import MODERATION_CHAT_ID, TECH_CHAT_ID, bot, dp
from bot.db.models import init_db
from bot.db.analytics import ensure_analytics_tables
from bot.db.crud import ensure_help_tables, ensure_offline_gift_tables, ensure_raffle_tables
from bot.handlers import start, formats, booking, rozygrysh, offline_gift, manager_stata
from bot.handlers import mailing_callbacks, fallback, tech_ops
from bot.db.mailing import ensure_mailing_tables
from bot.handlers.reminders import reminder_loop
from bot.handlers.rozygrysh_reminders import raffle_reminder_loop
from bot.middlewares import ModerationChatSilenceMiddleware, TouchUserProfileMiddleware
from bot.utils.tech_alerts import format_alert, notify_tech, notify_tech_sync

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dp.errors()
async def on_unhandled_error(event: ErrorEvent):
    exc = event.exception
    logger.exception("Unhandled update error: %s", exc)
    # Сетевые сбои Telegram не шлём в техчат — иначе шум при Bad Gateway.
    if isinstance(exc, (TelegramNetworkError, TelegramServerError)):
        return True
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    update = event.update
    update_hint = type(update).__name__ if update else "update"
    # Короткий контекст: callback data / текст, без огромного JSON.
    ctx = ""
    try:
        if update and update.callback_query:
            cq = update.callback_query
            ctx = f"callback data={cq.data!r} from={getattr(cq.from_user, 'id', None)}"
        elif update and update.message:
            msg = update.message
            ctx = f"message text={(msg.text or msg.caption or '')[:120]!r} from={getattr(msg.from_user, 'id', None)}"
    except Exception:
        ctx = ""
    head = f"{type(exc).__name__}: {exc}"
    body = "\n".join(part for part in (head, ctx, f"update={update_hint}", tb) if part)
    text = format_alert(
        "TG bot: необработанная ошибка",
        body,
        source="standup-bot",
    )
    await notify_tech(text, key="tg_unhandled", throttle_sec=180, bot=bot)
    return True


async def main():
    init_db()
    ensure_raffle_tables()
    ensure_help_tables()
    ensure_offline_gift_tables()
    ensure_analytics_tables()
    ensure_mailing_tables()
    try:
        await start.setup_bot_commands(bot)
    except (TelegramNetworkError, TelegramServerError) as exc:
        # Telegram Bad Gateway / timeouts must not keep the bot from starting
        logger.warning("Skip set_my_commands on startup: %s", exc)
    if MODERATION_CHAT_ID:
        logger.info("MODERATION_CHAT_ID is set (%s…)", str(MODERATION_CHAT_ID)[:6])
    else:
        logger.error(
            "MODERATION_CHAT_ID is NOT set — raffle screenshots will not reach moderation chat"
        )
    if TECH_CHAT_ID:
        logger.info("TECH_CHAT_ID is set (%s…)", str(TECH_CHAT_ID)[:6])
    else:
        logger.warning("TECH_CHAT_ID is NOT set — tech alerts disabled")
    touch_profile = TouchUserProfileMiddleware()
    silence = ModerationChatSilenceMiddleware()
    # Сначала профиль (имя/@username), потом тишина модерации.
    dp.message.middleware(touch_profile)
    dp.callback_query.middleware(touch_profile)
    dp.message.middleware(silence)
    dp.callback_query.middleware(silence)
    dp.include_router(start.router)
    dp.include_router(tech_ops.router)
    dp.include_router(manager_stata.router)
    dp.include_router(offline_gift.router)
    dp.include_router(rozygrysh.router)
    dp.include_router(formats.router)
    dp.include_router(booking.router)
    dp.include_router(mailing_callbacks.router)
    # Последним: неизвестные callback-кнопки → меню (не перехватывать раньше).
    dp.include_router(fallback.router)
    asyncio.create_task(reminder_loop())
    asyncio.create_task(raffle_reminder_loop())
    # Telegram Bad Gateway on initial getMe must not kill the process forever
    while True:
        try:
            await dp.start_polling(
                bot,
                allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"],
            )
            break
        except (TelegramNetworkError, TelegramServerError) as exc:
            logger.warning("Polling failed due to Telegram API: %s; retry in 15s", exc)
            await asyncio.sleep(15)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        logger.exception("TG bot crashed")
        notify_tech_sync(
            format_alert("TG bot: процесс упал", f"{type(exc).__name__}: {exc}", source="standup-bot"),
            key="tg_crash",
            throttle_sec=60,
        )
        raise
