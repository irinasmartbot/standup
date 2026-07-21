import asyncio
import logging

from bot.config import MODERATION_CHAT_ID, bot, dp
from bot.db.models import init_db
from bot.db.crud import ensure_help_tables, ensure_raffle_tables
from bot.handlers import start, formats, booking, rozygrysh
from bot.handlers.reminders import reminder_loop
from bot.handlers.rozygrysh_reminders import raffle_reminder_loop
from bot.middlewares import ModerationChatSilenceMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    init_db()
    ensure_raffle_tables()
    ensure_help_tables()
    await start.setup_bot_commands(bot)
    if MODERATION_CHAT_ID:
        logger.info("MODERATION_CHAT_ID is set (%s…)", str(MODERATION_CHAT_ID)[:6])
    else:
        logger.error(
            "MODERATION_CHAT_ID is NOT set — raffle screenshots will not reach moderation chat"
        )
    silence = ModerationChatSilenceMiddleware()
    dp.message.middleware(silence)
    dp.callback_query.middleware(silence)
    dp.include_router(start.router)
    dp.include_router(rozygrysh.router)
    dp.include_router(formats.router)
    dp.include_router(booking.router)
    asyncio.create_task(reminder_loop())
    asyncio.create_task(raffle_reminder_loop())
    await dp.start_polling(
        bot,
        allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"],
    )


if __name__ == "__main__":
    asyncio.run(main())
