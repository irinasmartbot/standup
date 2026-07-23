from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from bot.db.crud import get_user_bookings_for_commands


BASE_COMMANDS = [
    BotCommand(command="main_menu", description="Главное меню"),
    BotCommand(command="buy_ticket", description="Купить билет"),
    BotCommand(command="help", description="Задать вопрос"),
    BotCommand(command="channel", description="Канал анонсов"),
]


async def setup_bot_commands(bot):
    await bot.set_my_commands(BASE_COMMANDS, scope=BotCommandScopeDefault())


async def refresh_user_commands(bot, telegram_id: int):
    commands = []

    if get_user_bookings_for_commands(telegram_id):
        commands.append(BotCommand(command="my_bookings", description="Мои брони"))

    commands.extend(BASE_COMMANDS)
    await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=telegram_id))
