from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.config import MANAGER_LINK, CHANNEL_LINK

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


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())


@router.callback_query(lambda c: c.data == "main_menu")
async def back_to_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())
    await call.answer()
