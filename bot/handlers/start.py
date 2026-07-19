from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.config import MANAGER_LINK, CHANNEL_LINK, PAID_BEST_START
from bot.handlers.formats import delete_linked_venue_album

router = Router()

WELCOME_TEXT = (
    "Привет! Это Moscow StandUp Show! Мы делаем шоу в различных заведениях в центре Москвы каждый день!\n\n"
    "Только опытные комики, участники проектов ТНТ и YouTube, харизматичные ведущие, интерактив со зрителями, "
    "атмосферные залы, подарки на каждом мероприятии - это всё мы! 😊\n\n"
    "Здесь ты сможешь узнать о нас побольше и забронировать места:"
)


def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Забронировать места", callback_data="book")
    kb.button(text="🎭 Наши форматы ШОУ", callback_data="formats")
    kb.button(text="📍 Наши площадки", callback_data="venues")
    kb.button(text="📋 Правила посещения шоу", callback_data="rules")
    kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
    kb.button(text="📢 Заглянуть на наш канал анонсов", url=CHANNEL_LINK)
    kb.adjust(1)
    return kb.as_markup()


async def _delete_previous_menu_message(call: CallbackQuery):
    await delete_linked_venue_album(call)
    try:
        await call.message.delete()
    except Exception:
        pass


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, state: FSMContext, command: CommandObject):
    await state.clear()
    payload = (command.args or "").strip()

    if payload == "standup_rozygr":
        from bot.handlers.rozygrysh import send_raffle_start
        await send_raffle_start(message, state)
        return

    if payload == PAID_BEST_START:
        # платная ветка BEST для друга (из розыгрыша)
        from bot.handlers.formats import best_format_entry
        await best_format_entry(message)
        return

    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())


@router.callback_query(F.data == "main_menu")
async def back_to_menu(call: CallbackQuery, state: FSMContext):
    # меню клиента — только в личке
    if call.message and call.message.chat.type != "private":
        await call.answer()
        return
    await state.clear()
    await _delete_previous_menu_message(call)
    await call.message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())
    await call.answer()
