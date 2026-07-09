import asyncio
import logging

from bot.config import bot, dp
from bot.db.models import init_db
from bot.handlers import start, formats, booking, reminders
from bot.handlers.reminders import reminder_loop

logging.basicConfig(level=logging.INFO)


async def main():
    init_db()
    dp.include_router(start.router)
    dp.include_router(formats.router)
    dp.include_router(booking.router)
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
