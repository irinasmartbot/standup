from html import escape
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.config import MANAGER_LINK, CHANNEL_LINK, PAID_BEST_START, HELP_CHAT_ID
from bot.db.crud import (
    create_help_request,
    get_help_request_by_message,
    get_user_bookings_for_commands,
    mark_help_request_answered,
)
from bot.handlers.formats import delete_linked_venue_album
from bot.utils.bot_commands import refresh_user_commands, setup_bot_commands

router = Router()

WELCOME_TEXT = (
    "Привет! Это Moscow StandUp Show! Мы делаем шоу в различных заведениях в центре Москвы каждый день!\n\n"
    "Только опытные комики, участники проектов ТНТ и YouTube, харизматичные ведущие, интерактив со зрителями, "
    "атмосферные залы, подарки на каждом мероприятии - это всё мы! 😊\n\n"
    "Здесь ты сможешь узнать о нас побольше и забронировать места:"
)


class HelpState(StatesGroup):
    waiting_question = State()


def _help_chat_id():
    try:
        return int(HELP_CHAT_ID)
    except (TypeError, ValueError):
        return None


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


def _link_kb(text: str, url: str):
    kb = InlineKeyboardBuilder()
    kb.button(text=text, url=url)
    kb.button(text="⬅️ В главное меню", callback_data="main_menu")
    kb.adjust(1)
    return kb.as_markup()


def _status_label(status: str) -> str:
    return "билет подтверждён" if status == "confirmed" else "бронь активна"


def _format_label(format_name: str) -> str:
    if format_name == "rozygrysh":
        return "Розыгрыш"
    if format_name == "proverka":
        return "Проверка материала"
    return format_name


def _days_until(event_date: str) -> int | None:
    try:
        date_value = datetime.strptime(event_date, "%d.%m.%Y").date()
    except (TypeError, ValueError):
        return None
    from bot.utils.ticket import now_msk

    return (date_value - now_msk().date()).days


def _booking_command_title(status: str) -> str:
    return "Ваши активные брони" if status == "booked" else "Ваши активные билеты"


def _booking_command_text(row, page: int = 0, total: int = 1) -> str:
    booking_id, format_name, status, event_date, event_time, address, location, guests, *_ = row
    title = escape(_booking_command_title(status))
    position = f" {page + 1}/{total}" if total > 1 else ""
    return (
        f"<b>{title}{position}</b>\n\n"
        f"<b>{escape(_format_label(format_name))}</b>\n"
        f"📅 {escape(event_date)} в {escape(event_time)}\n"
        f"📍 {escape(location or '')}\n"
        f"Адрес: {escape(address or '')}\n"
        f"Гостей: {guests}\n"
        f"Статус: {escape(_status_label(status))}\n"
        f"Номер брони: {booking_id}"
    )


def _booking_command_kb(row, page: int = 0, total: int = 1):
    booking_id, format_name, status, event_date, *_ = row
    kb = InlineKeyboardBuilder()
    days_until = _days_until(event_date)
    can_get_ticket = days_until is not None and days_until <= 1
    action_count = 0

    if status == "booked":
        if format_name == "rozygrysh":
            if can_get_ticket:
                kb.button(text="🎟 Получить билет 🎟", callback_data=f"rz_ticket_{booking_id}")
                action_count += 1
            kb.button(text="Что, если я хочу прийти не один?", callback_data="rz_not_alone")
            kb.button(text="Отменить бронь", callback_data=f"rz_cancel_{booking_id}")
            action_count += 2
        else:
            if can_get_ticket:
                kb.button(text="🎟 Получить билет 🎟", callback_data=f"get_ticket_{booking_id}")
                action_count += 1
            kb.button(text="Отменить бронь", callback_data=f"cancel_confirm_{booking_id}")
            kb.button(text="Изменить дату", callback_data=f"change_date_{booking_id}")
            kb.button(text="Изменить количество гостей", callback_data=f"change_guests_confirm_{booking_id}")
            action_count += 3
    else:
        if format_name == "rozygrysh":
            kb.button(text="Что, если я хочу прийти не один?", callback_data="rz_not_alone")
            kb.button(text="Отменить бронь", callback_data=f"rz_cancel_{booking_id}")
            action_count += 2
        else:
            kb.button(text="Отменить бронь", callback_data=f"cancel_confirm_{booking_id}")
            kb.button(text="Изменить дату", callback_data=f"change_date_{booking_id}")
            action_count += 2

    if total > 1:
        prev_page = (page - 1) % total
        next_page = (page + 1) % total
        kb.button(text="⬅️ Назад", callback_data=f"cmd_bookings:{status}:{prev_page}")
        kb.button(text=f"{page + 1}/{total}", callback_data="cmd_bookings_noop")
        kb.button(text="Далее ➡️", callback_data=f"cmd_bookings:{status}:{next_page}")

    kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
    kb.button(text="⬅️ В главное меню", callback_data="main_menu")
    if total > 1:
        kb.adjust(*([1] * action_count), 3, 1, 1)
    else:
        kb.adjust(1)
    return kb.as_markup()


async def _send_command_bookings(message: Message, status: str):
    await refresh_user_commands(message.bot, message.from_user.id)
    rows = get_user_bookings_for_commands(message.from_user.id, status)
    if not rows:
        text = (
            "Активных броней пока нет."
            if status == "booked"
            else "Активных билетов пока нет."
        )
        await message.answer(text, reply_markup=main_menu_kb())
        return

    await message.answer(
        _booking_command_text(rows[0], page=0, total=len(rows)),
        parse_mode="HTML",
        reply_markup=_booking_command_kb(rows[0], page=0, total=len(rows)),
    )


async def _delete_previous_menu_message(call: CallbackQuery):
    await delete_linked_venue_album(call)
    try:
        await call.message.delete()
    except Exception:
        pass


@router.message(F.sticker, F.chat.type == "private")
async def private_sticker_file_id(message: Message, state: FSMContext):
    """Временно: отвечает file_id стикера — удобно прописать ROZYGRYSH_STICKER_FILE_ID."""
    if await state.get_state() is not None:
        return
    file_id = message.sticker.file_id if message.sticker else ""
    if not file_id:
        return
    await message.answer(
        f"file_id стикера:\n<code>{file_id}</code>\n\n"
        f"Скопируй в .env как ROZYGRYSH_STICKER_FILE_ID=",
        parse_mode="HTML",
    )


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, state: FSMContext, command: CommandObject):
    await state.clear()
    await refresh_user_commands(message.bot, message.from_user.id)
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


@router.message(Command("main_menu"), F.chat.type == "private")
async def main_menu_command(message: Message, state: FSMContext):
    await state.clear()
    await refresh_user_commands(message.bot, message.from_user.id)
    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())


@router.message(Command("manager"), F.chat.type == "private")
async def manager_command(message: Message):
    await message.answer(
        "Написать менеджеру можно здесь:",
        reply_markup=_link_kb("💬 Написать менеджеру", MANAGER_LINK),
    )


@router.message(Command("channel"), F.chat.type == "private")
async def channel_command(message: Message):
    await message.answer(
        "Канал с анонсами шоу:",
        reply_markup=_link_kb("📢 Открыть канал", CHANNEL_LINK),
    )


@router.message(Command("active_bookings"), F.chat.type == "private")
async def active_bookings_command(message: Message, state: FSMContext):
    await state.clear()
    await _send_command_bookings(message, "booked")


@router.message(Command("myticket"), F.chat.type == "private")
async def myticket_command(message: Message, state: FSMContext):
    await state.clear()
    await _send_command_bookings(message, "confirmed")


@router.callback_query(F.data == "cmd_bookings_noop")
async def command_bookings_noop(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data.startswith("cmd_bookings:"))
async def command_bookings_page(call: CallbackQuery):
    parts = call.data.split(":")
    if len(parts) != 3:
        await call.answer()
        return

    _, status, raw_page = parts
    if status not in {"booked", "confirmed"}:
        await call.answer()
        return
    try:
        page = int(raw_page)
    except ValueError:
        await call.answer()
        return

    rows = get_user_bookings_for_commands(call.from_user.id, status)
    if not rows:
        empty_text = (
            "Активных броней пока нет."
            if status == "booked"
            else "Активных билетов пока нет."
        )
        await refresh_user_commands(call.message.bot, call.from_user.id)
        await call.message.edit_text(empty_text, reply_markup=main_menu_kb())
        await call.answer()
        return

    page = page % len(rows)
    await refresh_user_commands(call.message.bot, call.from_user.id)
    await call.message.edit_text(
        _booking_command_text(rows[page], page=page, total=len(rows)),
        parse_mode="HTML",
        reply_markup=_booking_command_kb(rows[page], page=page, total=len(rows)),
    )
    await call.answer()


@router.message(Command("help"), F.chat.type == "private")
async def help_command(message: Message, state: FSMContext):
    if not _help_chat_id():
        await message.answer("Сейчас вопрос лучше отправить менеджеру напрямую:", reply_markup=_link_kb("💬 Написать менеджеру", MANAGER_LINK))
        return
    await state.set_state(HelpState.waiting_question)
    await message.answer("Напишите ваш вопрос одним сообщением, и мы передадим его менеджеру.")


@router.message(HelpState.waiting_question, F.chat.type == "private")
async def help_question(message: Message, state: FSMContext):
    help_chat_id = _help_chat_id()
    if not help_chat_id:
        await state.clear()
        await message.answer("Сейчас вопрос лучше отправить менеджеру напрямую:", reply_markup=_link_kb("💬 Написать менеджеру", MANAGER_LINK))
        return

    user = message.from_user
    username = f"@{user.username}" if user.username else "без username"
    question = message.text or message.caption or "Вопрос без текста"
    header = (
        "<b>Новый вопрос из бота</b>\n\n"
        f"Пользователь: {escape(user.full_name or '')} ({escape(username)})\n"
        f"Telegram ID: <code>{user.id}</code>\n\n"
        f"<b>Вопрос:</b>\n{escape(question)}\n\n"
        "Чтобы ответить пользователю, ответьте reply на это сообщение."
    )
    sent = await message.bot.send_message(help_chat_id, header, parse_mode="HTML")
    create_help_request(
        user.id,
        user.username,
        user.full_name,
        question,
        help_chat_id,
        sent.message_id,
    )

    if not message.text:
        copied = await message.bot.copy_message(
            chat_id=help_chat_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
        create_help_request(
            user.id,
            user.username,
            user.full_name,
            question,
            help_chat_id,
            copied.message_id,
        )

    await state.clear()
    await message.answer("Спасибо! Передали вопрос менеджеру, скоро ответим.")


@router.message(F.reply_to_message, lambda message: message.chat.id == _help_chat_id())
async def help_chat_reply(message: Message):
    replied = message.reply_to_message
    request = get_help_request_by_message(message.chat.id, replied.message_id)
    if not request:
        return

    telegram_id = request[0]
    if message.text:
        await message.bot.send_message(
            telegram_id,
            f"Ответ менеджера:\n\n{message.text}",
        )
    else:
        await message.bot.copy_message(
            chat_id=telegram_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    mark_help_request_answered(message.chat.id, replied.message_id)
    await message.reply("Ответ отправлен пользователю.")


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
