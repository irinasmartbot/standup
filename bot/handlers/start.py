from collections import Counter
from html import escape
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.config import MANAGER_LINK, CHANNEL_LINK, PAID_BEST_START, HELP_CHAT_ID
from bot.db.crud import (
    create_help_request,
    get_help_request_by_message,
    get_last_phone,
    get_user_bookings_for_commands,
    mark_help_request_answered,
)
from bot.handlers.formats import delete_linked_venue_album
from bot.utils.bot_commands import refresh_user_commands, setup_bot_commands
from bot.utils.nav_messages import (
    delete_my_bookings_messages,
    forget_my_bookings_message,
    remember_my_bookings_message,
)
from bot.utils.ticket import generate_ticket

router = Router()

# Маркер для WELCOME_MARKER в booking/formats — держать совпадение с текстом приветствия.
WELCOME_TEXT = (
    "<b>Moscow StandUp Show</b>\n\n"
    "Привет! Мы делаем шоу в различных заведениях в центре Москвы каждый день.\n\n"
    "⭐ <i>Только опытные комики, участники проектов ТНТ и YouTube, харизматичные ведущие, "
    "интерактив со зрителями, атмосферные залы, подарки на каждом мероприятии — это всё мы!</i>\n\n"
    "<blockquote>Здесь можно</blockquote>\n"
    "🎟 забронировать места на <b>бесплатные шоу</b>\n"
    "💳 купить билеты на <b>StandUp BEST</b> и <b>Хитлото</b>"
)

WELCOME_RICH_HTML = """
<h2>Moscow StandUp Show</h2>
<p>Привет! Мы делаем шоу в различных заведениях в центре Москвы каждый день.</p>
<p>⭐ <i>Только опытные комики, участники проектов ТНТ и YouTube, харизматичные ведущие, интерактив со зрителями, атмосферные залы, подарки на каждом мероприятии — это всё мы!</i></p>
<blockquote>Здесь можно</blockquote>
<p>🎟 забронировать места на <b>бесплатные шоу</b></p>
<p>💳 купить билеты на <b>StandUp BEST</b> и <b>Хитлото</b></p>
"""


async def _send_welcome(message: Message):
    from bot.handlers.formats import _send_rich_or_html

    await _send_rich_or_html(
        message,
        rich_html=WELCOME_RICH_HTML,
        fallback_html=WELCOME_TEXT,
        reply_markup=main_menu_kb(),
    )


class HelpState(StatesGroup):
    waiting_question = State()


def _help_chat_id():
    try:
        return int(HELP_CHAT_ID)
    except (TypeError, ValueError):
        return None


def _is_meaningful_free_text(text: str | None) -> bool:
    """Осмысленный текст от 10 символов; короткий спам и абракадабру отсекаем."""
    text = (text or "").strip()
    if len(text) < 10:
        return False
    if text.startswith("/"):
        return False

    letters = [c for c in text.lower() if c.isalpha()]
    if len(letters) < 6:
        return False

    unique_ratio = len(set(letters)) / len(letters)
    if unique_ratio < 0.25:
        return False

    most_common = Counter(letters).most_common(1)[0][1]
    if most_common / len(letters) > 0.6:
        return False

    vowels = set("аеёиоуыэюяaeiouy")
    if sum(1 for c in letters if c in vowels) == 0:
        return False

    return True


async def submit_help_question(message: Message, *, thank_you: bool = True) -> bool:
    """Отправляет вопрос пользователя в чат уведомлений (/help). True если ушло."""
    help_chat_id = _help_chat_id()
    if not help_chat_id:
        return False

    user = message.from_user
    question = message.text or message.caption or "Вопрос без текста"
    phone = get_last_phone(user.id)
    sent = await message.bot.send_message(
        help_chat_id,
        _help_card_text(
            telegram_id=user.id,
            full_name=user.full_name,
            username=user.username,
            question=question,
            phone=phone,
        ),
        parse_mode="HTML",
    )
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

    if thank_you:
        await message.answer("Спасибо! Передали вопрос менеджеру, скоро ответим.")
    return True


def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Забронировать места", callback_data="book")
    kb.button(text="💳 Купить билет", callback_data="buy_ticket")
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


MY_BOOKINGS_INTRO = (
    "Здесь ты можешь смотреть свои активные брони по бесплатному бронированию."
)


def _booking_command_text(row, page: int = 0, total: int = 1) -> str:
    _, format_name, status, event_date, event_time, address, location, guests, *_ = row
    position = f" {page + 1}/{total}" if total > 1 else ""
    title = escape(_format_label(format_name))
    # Счётчик броней — рядом с типом, строку «Ваши активные брони» не показываем
    title_line = f"<b>{title}</b>{position}" if position else f"<b>{title}</b>"
    lines = [
        f"<b><i>{escape(MY_BOOKINGS_INTRO)}</i></b>",
        "",
        title_line,
        f"📅 {escape(event_date)} в {escape(event_time)}",
        f"📍 {escape(location or '')}",
        f"Адрес: {escape(address or '')}",
        f"Гостей: {guests}",
    ]
    if status == "confirmed":
        lines.extend(["", "✅ Бронь подтверждена"])
    return "\n".join(lines)


def _ticket_command_caption(row) -> str:
    _, format_name, _, event_date, event_time, _, location, *_ = row
    return (
        f"<b>Билет по брони</b>\n\n"
        f"{escape(_format_label(format_name))}\n"
        f"📅 {escape(event_date)} в {escape(event_time)}\n"
        f"📍 {escape(location or '')}"
    )


def _ticket_command_photo(row):
    booking_id, _, _, event_date, event_time, address, location, guests, _, _, name = row
    address_part = address.split(",", 1)[1].strip() if address and "," in address else (address or "")
    short_address = f"{location or ''}, {address_part}".strip(", ")
    ticket_buf = generate_ticket(name or "", event_date, event_time, short_address, guests)
    return BufferedInputFile(ticket_buf.getvalue(), filename=f"ticket_{booking_id}.jpg")


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
        kb.button(text="🎟 Билет по брони", callback_data=f"cmd_booking_ticket:{page}")
        action_count += 1
        if format_name == "rozygrysh":
            kb.button(text="Что, если я хочу прийти не один?", callback_data="rz_not_alone")
            kb.button(text="Отменить бронь", callback_data=f"rz_cancel_{booking_id}")
            action_count += 2
        else:
            kb.button(text="Отменить бронь", callback_data=f"cancel_confirm_{booking_id}")
            kb.button(text="Изменить дату", callback_data=f"change_date_{booking_id}")
            action_count += 2

    nav_count = 0
    if total > 1:
        # На первой странице — только «Далее», на последней — только «Назад»
        if page > 0:
            kb.button(text="⬅️ Назад", callback_data=f"cmd_bookings:{page - 1}")
            nav_count += 1
        kb.button(text=f"{page + 1}/{total}", callback_data="cmd_bookings_noop")
        nav_count += 1
        if page < total - 1:
            kb.button(text="Далее ➡️", callback_data=f"cmd_bookings:{page + 1}")
            nav_count += 1

    kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
    kb.button(text="⬅️ В главное меню", callback_data="main_menu")
    if total > 1:
        kb.adjust(*([1] * action_count), nav_count, 1, 1)
    else:
        kb.adjust(1)
    return kb.as_markup()


def _ticket_view_kb(page: int = 0):
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад к броням", callback_data=f"cmd_bookings_back:{page}")
    kb.button(text="⬅️ В главное меню", callback_data="main_menu")
    kb.adjust(1)
    return kb.as_markup()


def _help_card_text(
    telegram_id: int,
    full_name: str | None,
    username: str | None,
    question: str,
    phone: str | None = None,
    answer: str | None = None,
    manager_name: str | None = None,
) -> str:
    username_label = f"@{username}" if username else "без username"
    title = "✅ Вопрос отвечен" if answer is not None else "🆕 Новый вопрос из бота"
    lines = [
        f"<b>{title}</b>",
        "",
        f"Пользователь: {escape(full_name or '')} ({escape(username_label)})",
        f"Telegram ID: <code>{telegram_id}</code>",
    ]
    if phone:
        lines.append(f"Телефон: {escape(phone)}")
    lines.extend([
        "",
        f"<b>Вопрос:</b>\n{escape(question)}",
    ])
    if answer is not None:
        from bot.utils.ticket import now_msk

        answered_at = now_msk().strftime("%d.%m.%Y в %H:%M")
        manager = escape(manager_name or "менеджера")
        lines.extend([
            "",
            f"<b>Ответ от {manager} ({answered_at}):</b>",
            escape(answer),
        ])
    else:
        lines.extend([
            "",
            "Чтобы ответить пользователю, ответьте reply на это сообщение.",
        ])
    return "\n".join(lines)


async def _send_command_bookings(
    message: Message,
    page: int = 0,
    telegram_id: int | None = None,
):
    # Из callback message.from_user — это бот; нужен id клиента явно.
    user_id = telegram_id or (message.from_user.id if message.from_user else message.chat.id)
    await refresh_user_commands(message.bot, user_id)
    # Убираем прошлый вывод /my_bookings, чтобы не копились устаревшие карточки
    await delete_my_bookings_messages(message.bot, message.chat.id)
    rows = get_user_bookings_for_commands(user_id)
    if not rows:
        sent = await message.answer(
            f"<b><i>{escape(MY_BOOKINGS_INTRO)}</i></b>\n\nАктивных броней пока нет.",
            reply_markup=main_menu_kb(),
            parse_mode="HTML",
        )
        remember_my_bookings_message(message.chat.id, sent.message_id)
        return

    page = page % len(rows)
    sent = await message.answer(
        _booking_command_text(rows[page], page=page, total=len(rows)),
        parse_mode="HTML",
        reply_markup=_booking_command_kb(rows[page], page=page, total=len(rows)),
    )
    remember_my_bookings_message(message.chat.id, sent.message_id)


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

    if payload == "quick_booking":
        from bot.handlers.formats import send_all_formats
        await send_all_formats(message, from_deep_link=True)
        return

    if payload == PAID_BEST_START:
        # платная ветка BEST для друга (из розыгрыша)
        from bot.handlers.formats import best_format_entry
        await best_format_entry(message)
        return

    await _send_welcome(message)


@router.message(Command("main_menu"), F.chat.type == "private")
async def main_menu_command(message: Message, state: FSMContext):
    await state.clear()
    await refresh_user_commands(message.bot, message.from_user.id)
    await _send_welcome(message)


@router.message(Command("buy_ticket"), F.chat.type == "private")
async def buy_ticket_command(message: Message, state: FSMContext):
    await state.clear()
    from bot.handlers.formats import send_buy_ticket_formats
    await send_buy_ticket_formats(message)


HELP_HUB_TEXT = (
    "Если у вас вопрос по мероприятию, посещению, афише и др — напишите менеджеру.\n\n"
    "Если вопрос по боту, его работе, проблемам с бронированием, — напишите в техподдержку."
)


def _help_hub_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Написать менеджеру", url=MANAGER_LINK)
    kb.button(text="🛠 Написать техподдержке", callback_data="help_support")
    kb.button(text="⬅️ В главное меню", callback_data="main_menu")
    kb.adjust(1)
    return kb.as_markup()


async def _send_help_hub(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(HELP_HUB_TEXT, reply_markup=_help_hub_kb())


@router.message(Command("manager"), F.chat.type == "private")
async def manager_command(message: Message, state: FSMContext):
    # Старая команда оставлена как алиас на новый хаб /help
    await _send_help_hub(message, state)


@router.message(Command("channel"), F.chat.type == "private")
async def channel_command(message: Message):
    await message.answer(
        "Канал с анонсами шоу:",
        reply_markup=_link_kb("📢 Открыть канал", CHANNEL_LINK),
    )


@router.message(Command("my_bookings"), F.chat.type == "private")
async def my_bookings_command(message: Message, state: FSMContext):
    await state.clear()
    await _send_command_bookings(message)


@router.message(Command("active_bookings"), F.chat.type == "private")
async def active_bookings_command(message: Message, state: FSMContext):
    # Алиас на /my_bookings
    await state.clear()
    await _send_command_bookings(message)


@router.message(Command("myticket"), F.chat.type == "private")
async def myticket_command(message: Message, state: FSMContext):
    # Алиас на /my_bookings
    await state.clear()
    await _send_command_bookings(message)


@router.callback_query(F.data == "cmd_bookings_noop")
async def command_bookings_noop(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data.startswith("cmd_bookings:"))
async def command_bookings_page(call: CallbackQuery):
    parts = call.data.split(":")
    if len(parts) != 2:
        await call.answer()
        return
    try:
        page = int(parts[1])
    except ValueError:
        await call.answer()
        return

    rows = get_user_bookings_for_commands(call.from_user.id)
    await refresh_user_commands(call.message.bot, call.from_user.id)
    chat_id = call.message.chat.id
    if not rows:
        await call.message.edit_text(
            f"<b><i>{escape(MY_BOOKINGS_INTRO)}</i></b>\n\nАктивных броней пока нет.",
            reply_markup=main_menu_kb(),
            parse_mode="HTML",
        )
        remember_my_bookings_message(chat_id, call.message.message_id)
        await call.answer()
        return

    page = page % len(rows)
    await call.message.edit_text(
        _booking_command_text(rows[page], page=page, total=len(rows)),
        parse_mode="HTML",
        reply_markup=_booking_command_kb(rows[page], page=page, total=len(rows)),
    )
    remember_my_bookings_message(chat_id, call.message.message_id)
    await call.answer()


@router.callback_query(F.data.startswith("cmd_booking_ticket:"))
async def command_booking_ticket(call: CallbackQuery):
    parts = call.data.split(":")
    if len(parts) != 2:
        await call.answer()
        return
    try:
        page = int(parts[1])
    except ValueError:
        await call.answer()
        return

    rows = get_user_bookings_for_commands(call.from_user.id)
    if not rows:
        await call.answer("Активных броней пока нет.", show_alert=True)
        return

    page = page % len(rows)
    row = rows[page]
    if row[2] != "confirmed":
        await call.answer("Билет ещё не подтверждён.", show_alert=True)
        return

    chat_id = call.message.chat.id
    old_id = call.message.message_id
    try:
        await call.message.delete()
        forget_my_bookings_message(chat_id, old_id)
    except Exception:
        pass
    sent = await call.message.answer_photo(
        photo=_ticket_command_photo(row),
        caption=_ticket_command_caption(row),
        parse_mode="HTML",
        reply_markup=_ticket_view_kb(page),
    )
    remember_my_bookings_message(chat_id, sent.message_id)
    await call.answer()


@router.callback_query(F.data.startswith("cmd_bookings_back:"))
async def command_bookings_back(call: CallbackQuery):
    parts = call.data.split(":")
    if len(parts) != 2:
        await call.answer()
        return
    try:
        page = int(parts[1])
    except ValueError:
        await call.answer()
        return

    chat_id = call.message.chat.id
    old_id = call.message.message_id
    try:
        await call.message.delete()
        forget_my_bookings_message(chat_id, old_id)
    except Exception:
        pass
    await _send_command_bookings(
        call.message,
        page=page,
        telegram_id=call.from_user.id,
    )
    await call.answer()


@router.message(Command("help"), F.chat.type == "private")
async def help_command(message: Message, state: FSMContext):
    await _send_help_hub(message, state)


@router.callback_query(F.data == "help_support", F.message.chat.type == "private")
async def help_support_callback(call: CallbackQuery, state: FSMContext):
    if not _help_chat_id():
        await call.message.answer(
            "Сейчас вопрос лучше отправить менеджеру напрямую:",
            reply_markup=_link_kb("💬 Написать менеджеру", MANAGER_LINK),
        )
        await call.answer()
        return
    await state.set_state(HelpState.waiting_question)
    await call.message.answer("Напиши вопрос ниже (одним сообщением), и мы передадим его команде 👇")
    await call.answer()


@router.message(HelpState.waiting_question, F.chat.type == "private")
async def help_question(message: Message, state: FSMContext):
    if not _help_chat_id():
        await state.clear()
        await message.answer(
            "Сейчас вопрос лучше отправить менеджеру напрямую:",
            reply_markup=_link_kb("💬 Написать менеджеру", MANAGER_LINK),
        )
        return

    await submit_help_question(message, thank_you=True)
    await state.clear()


@router.message(F.reply_to_message, lambda message: message.chat.id == _help_chat_id())
async def help_chat_reply(message: Message):
    replied = message.reply_to_message
    request = get_help_request_by_message(message.chat.id, replied.message_id)
    if not request:
        return

    telegram_id = request[0]
    username = request[1]
    full_name = request[2]
    question = request[3] or "Вопрос без текста"
    answer_text = message.text or message.caption or "Ответ отправлен файлом/медиа"
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
    try:
        await replied.edit_text(
            _help_card_text(
                telegram_id=telegram_id,
                full_name=full_name,
                username=username,
                question=question,
                phone=get_last_phone(telegram_id),
                answer=answer_text,
                manager_name=message.from_user.full_name if message.from_user else None,
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data == "main_menu")
async def back_to_menu(call: CallbackQuery, state: FSMContext):
    # меню клиента — только в личке
    if call.message and call.message.chat.type != "private":
        await call.answer()
        return
    await state.clear()
    await _delete_previous_menu_message(call)
    await _send_welcome(call.message)
    await call.answer()
