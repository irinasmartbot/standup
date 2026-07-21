from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from bot.db.crud import get_user_bookings_for_commands


BASE_COMMANDS = [
    BotCommand(command="main_menu", description="Главное меню"),
    BotCommand(command="manager", description="Связаться с менеджером"),
    BotCommand(command="help", description="Задать вопрос"),
    BotCommand(command="channel", description="Канал анонсов"),
]


async def setup_bot_commands(bot):
    await bot.set_my_commands(BASE_COMMANDS, scope=BotCommandScopeDefault())


async def refresh_user_commands(bot, telegram_id: int):
    commands = []

    if get_user_bookings_for_commands(telegram_id, "booked"):
        commands.append(BotCommand(command="active_bookings", description="Мои активные брони"))
    if get_user_bookings_for_commands(telegram_id, "confirmed"):
        commands.append(BotCommand(command="myticket", description="Мои билеты"))

    commands.extend(BASE_COMMANDS)
    await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=telegram_id))
