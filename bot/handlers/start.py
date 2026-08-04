from html import escape
from datetime import datetime
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ChatMemberStatus
from aiogram.types import BufferedInputFile, ChatMemberUpdated, Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.config import (
    CHANNEL_LINK,
    HELP_CHAT_ID,
    MANAGER_LINK,
    PAID_BEST_START,
    TEST_ADMIN_IDS,
)
from bot.db.analytics import (
    EVENT_BOT_BLOCKED,
    EVENT_BOT_START,
    EVENT_BOT_UNBLOCKED,
    EVENT_CMD_BUY_TICKET,
    EVENT_CMD_CHANNEL,
    EVENT_CMD_HELP,
    EVENT_CMD_MAIN_MENU,
    EVENT_CMD_MY_BOOKINGS,
    EVENT_HELP_QUESTION,
    set_user_blocked,
    track_event,
)
from bot.db.crud import (
    create_help_request,
    get_help_request_by_message,
    get_last_phone,
    get_user_bookings_for_commands,
    mark_help_request_answered,
)
from bot.handlers.formats import delete_linked_venue_album
from bot.utils.bot_commands import refresh_user_commands, setup_bot_commands
from bot.utils.free_text import is_meaningful_free_text as _is_meaningful_free_text
from bot.utils.nav_messages import (
    delete_my_bookings_messages,
    forget_my_bookings_message,
    remember_my_bookings_message,
)
from bot.utils.ticket import format_ticket_place, generate_ticket

router = Router()
logger = logging.getLogger(__name__)

# Маркер для WELCOME_MARKER в booking/formats — держать совпадение с текстом приветствия.
WELCOME_TEXT = (
    "<b>Moscow StandUp Show</b>\n\n"
    "Привет! Мы делаем шоу в различных заведениях в центре Москвы каждый день.\n\n"
    "<blockquote>Только опытные комики, участники проектов ТНТ и YouTube, "
    "харизматичные ведущие, интерактив со зрителями, атмосферные залы "
    "и подарки на каждом мероприятии — это всё мы!</blockquote>\n\n"
    "<b>Здесь можно</b>\n"
    "• Забронировать места на <b>бесплатные шоу</b>\n"
    "• Купить билеты на <b>StandUp BEST</b> и <b>Хитлото</b>"
)

WELCOME_RICH_HTML = """
<h2>Moscow StandUp Show</h2>
<p>Привет! Мы делаем шоу в различных заведениях в центре Москвы каждый день.</p>
<blockquote>Только опытные комики, участники проектов ТНТ и YouTube, харизматичные ведущие, интерактив со зрителями, атмосферные залы и подарки на каждом мероприятии — это всё мы!</blockquote>
<h3>Здесь можно</h3>
<p>• Забронировать места на <b>бесплатные шоу</b></p>
<p>• Купить билеты на <b>StandUp BEST</b> и <b>Хитлото</b></p>
"""


async def _send_welcome(message: Message):
    from bot.handlers.formats import _send_rich_or_html
    from bot.utils.reply_keyboard import clear_reply_keyboard

    # Убираем «Поделиться номером», если клиент ушёл из незаконченной брони.
    await clear_reply_keyboard(message)
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


async def submit_help_question(message: Message, *, thank_you: bool = True) -> bool:
    """Отправляет вопрос пользователя в чат уведомлений (/help). True если ушло."""
    track_event(
        EVENT_HELP_QUESTION,
        telegram_id=message.from_user.id,
        props={"chars": len((message.text or "").strip())},
    )
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
        disable_web_page_preview=True,
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
        await message.answer("Спасибо! Передали вопрос в техподдержку, скоро ответим.")
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
    _, format_name, _, event_date, event_time, address, location, *_ = row
    return (
        f"<b>Билет по брони</b>\n\n"
        f"{escape(_format_label(format_name))}\n"
        f"📅 {escape(event_date)} в {escape(event_time)}\n"
        f"📍 {escape(location or '')}\n"
        f"Адрес: {escape(address or '')}"
    )


def _ticket_command_photo(row):
    booking_id, format_name, _, event_date, event_time, address, location, guests, _, _, name = row
    place = format_ticket_place(location or "", address or "")
    ticket_buf = generate_ticket(name or "", event_date, event_time, place, guests)
    return BufferedInputFile(ticket_buf.getvalue(), filename=f"ticket_{booking_id}.jpg")


def _booking_command_kb(row, page: int = 0, total: int = 1):
    booking_id, format_name, status, *_ = row
    kb = InlineKeyboardBuilder()
    action_count = 0

    # «Получить билет» только из напоминания; в карточке — «Билет по брони», если уже выдан
    if status == "booked":
        if format_name == "rozygrysh":
            kb.button(text="Что, если я хочу прийти не один?", callback_data="rz_not_alone")
            kb.button(text="Отменить бронь", callback_data=f"rz_cancel_{booking_id}")
            action_count += 2
        else:
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

    kb.button(text="⬅️ В главное меню", callback_data="main_menu")
    if total > 1:
        kb.adjust(*([1] * action_count), nav_count, 1)
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
    telegram_id: int | None,
    full_name: str | None,
    username: str | None,
    question: str,
    phone: str | None = None,
    answer: str | None = None,
    manager_name: str | None = None,
    *,
    vk_id: int | None = None,
) -> str:
    if vk_id is not None:
        title = "✅ Вопрос отвечен" if answer is not None else "❓ Вопрос из VK"
        lines = [
            f"<b>{title}</b>",
            "",
            f"VK id: <code>{int(vk_id)}</code>",
        ]
        if phone:
            lines.append(f"Телефон: {escape(phone)}")
        lines.extend(["", f"<b>Вопрос:</b>\n{escape(question)}"])
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

    # По возрастанию даты/времени мероприятия (раньше → позже)
    def _booking_sort_key(row):
        try:
            return datetime.strptime(f"{row[3]} {row[4]}", "%d.%m.%Y %H:%M")
        except (TypeError, ValueError, IndexError):
            return datetime.max

    rows = sorted(rows, key=_booking_sort_key)

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
    """file_id стикера — только для TEST_ADMIN_IDS (чтобы гости не ловили служебный ответ)."""
    if await state.get_state() is not None:
        return
    if not message.from_user or message.from_user.id not in TEST_ADMIN_IDS:
        return
    file_id = message.sticker.file_id if message.sticker else ""
    if not file_id:
        return
    await message.answer(
        f"file_id стикера:\n<code>{file_id}</code>\n\n"
        f"Скопируй в .env как ROZYGRYSH_STICKER_FILE_ID=",
        parse_mode="HTML",
    )


@router.message(Command("techid"))
async def tech_chat_id_command(message: Message):
    """Показать chat_id текущего чата — для TECH_CHAT_ID (только TEST_ADMIN_IDS)."""
    if not message.from_user or message.from_user.id not in TEST_ADMIN_IDS:
        return
    chat = message.chat
    await message.answer(
        f"chat_id этого чата:\n<code>{chat.id}</code>\n"
        f"тип: <code>{chat.type}</code>\n\n"
        f"Скопируй в .env:\n<code>TECH_CHAT_ID={chat.id}</code>\n"
        f"(в /home/standup/app/.env и при необходимости в vk-app/.env)",
        parse_mode="HTML",
    )


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, state: FSMContext, command: CommandObject):
    await state.clear()
    await refresh_user_commands(message.bot, message.from_user.id)
    payload = (command.args or "").strip()
    track_event(
        EVENT_BOT_START,
        telegram_id=message.from_user.id,
        props={"payload": payload or None},
    )

    if payload == "standup_rozygr":
        from bot.handlers.rozygrysh import send_raffle_start
        await send_raffle_start(message, state)
        return

    if payload == "chek_list":
        from bot.handlers.offline_gift import send_check_list_start
        await send_check_list_start(message)
        return

    if payload == "new_stata":
        from bot.handlers.manager_stata import send_manager_stata_start
        await send_manager_stata_start(message, state)
        return

    if payload == "new_stata_rozygr":
        from bot.handlers.manager_stata import send_manager_stata_rozygr_start
        await send_manager_stata_rozygr_start(message, state)
        return

    if payload == "quick_booking":
        from bot.handlers.formats import send_all_formats
        await send_all_formats(message, from_deep_link=True)
        return

    if payload == "afisha_besplat":
        # бесплатная бронь «Проверка материала» (дата / площадка)
        from bot.handlers.booking import check_format_entry
        await check_format_entry(message)
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
    track_event(EVENT_CMD_MAIN_MENU, telegram_id=message.from_user.id)
    await refresh_user_commands(message.bot, message.from_user.id)
    await _send_welcome(message)


@router.message(
    F.chat.type == "private",
    F.text.func(lambda t: (t or "").casefold().strip() in {"начать", "старт"}),
)
async def start_word_alias(message: Message, state: FSMContext):
    """Слова «начать» / «старт» — как /start без payload (не уводим в help/меню-заглушку)."""
    await state.clear()
    await refresh_user_commands(message.bot, message.from_user.id)
    track_event(
        EVENT_BOT_START,
        telegram_id=message.from_user.id,
        props={"payload": None, "via": "text_alias"},
    )
    await _send_welcome(message)


@router.message(
    F.chat.type == "private",
    F.text.func(lambda t: (t or "").casefold().strip() in {"розыгрыш"}),
)
async def raffle_word_alias(message: Message, state: FSMContext):
    await state.clear()
    from bot.handlers.rozygrysh import send_raffle_start

    await send_raffle_start(message, state)


@router.message(
    F.chat.type == "private",
    F.text.func(
        lambda t: (t or "").casefold().strip()
        in {"подарок", "чек лист", "чек-лист", "chek_list", "check_list"}
    ),
)
async def gift_word_alias(message: Message, state: FSMContext):
    await state.clear()
    from bot.handlers.offline_gift import send_check_list_start

    await send_check_list_start(message)


@router.message(Command("buy_ticket"), F.chat.type == "private")
async def buy_ticket_command(message: Message, state: FSMContext):
    from bot.utils.reply_keyboard import clear_reply_keyboard

    await state.clear()
    await clear_reply_keyboard(message)
    track_event(EVENT_CMD_BUY_TICKET, telegram_id=message.from_user.id)
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
    track_event(EVENT_CMD_HELP, telegram_id=message.from_user.id)
    await message.answer(HELP_HUB_TEXT, reply_markup=_help_hub_kb())


@router.message(Command("manager"), F.chat.type == "private")
async def manager_command(message: Message, state: FSMContext):
    # Старая команда оставлена как алиас на новый хаб /help
    await _send_help_hub(message, state)


@router.message(Command("channel"), F.chat.type == "private")
async def channel_command(message: Message):
    track_event(EVENT_CMD_CHANNEL, telegram_id=message.from_user.id)
    await message.answer(
        "Канал с анонсами шоу:",
        reply_markup=_link_kb("📢 Открыть канал", CHANNEL_LINK),
    )


@router.message(Command("my_bookings"), F.chat.type == "private")
async def my_bookings_command(message: Message, state: FSMContext):
    from bot.utils.reply_keyboard import clear_reply_keyboard

    await state.clear()
    await clear_reply_keyboard(message)
    track_event(EVENT_CMD_MY_BOOKINGS, telegram_id=message.from_user.id)
    await _send_command_bookings(message)


@router.message(Command("active_bookings"), F.chat.type == "private")
async def active_bookings_command(message: Message, state: FSMContext):
    # Алиас на /my_bookings
    await state.clear()
    track_event(EVENT_CMD_MY_BOOKINGS, telegram_id=message.from_user.id, props={"alias": "active_bookings"})
    await _send_command_bookings(message)


@router.message(Command("myticket"), F.chat.type == "private")
async def myticket_command(message: Message, state: FSMContext):
    # Алиас на /my_bookings
    await state.clear()
    track_event(EVENT_CMD_MY_BOOKINGS, telegram_id=message.from_user.id, props={"alias": "myticket"})
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
    vk_id = request[5] if len(request) > 5 else None
    answer_text = message.text or message.caption or "Ответ отправлен файлом/медиа"

    if vk_id:
        sent_ok = await _send_vk_support_answer(int(vk_id), answer_text, message)
        if not sent_ok:
            await message.reply("Не удалось отправить ответ в VK. Проверьте VK_GROUP_TOKEN / права сообщений.")
            return
        phone = get_last_phone(vk_id=int(vk_id))
    elif telegram_id:
        if message.text:
            await message.bot.send_message(
                int(telegram_id),
                f"Ответ техподдержки:\n\n{message.text}",
            )
        else:
            await message.bot.copy_message(
                chat_id=int(telegram_id),
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
        phone = get_last_phone(int(telegram_id))
    else:
        return

    mark_help_request_answered(message.chat.id, replied.message_id)
    try:
        await replied.edit_text(
            _help_card_text(
                telegram_id=int(telegram_id) if telegram_id else None,
                full_name=full_name,
                username=username,
                question=question,
                phone=phone,
                answer=answer_text,
                manager_name=message.from_user.full_name if message.from_user else None,
                vk_id=int(vk_id) if vk_id else None,
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass
    # Ответ уже на карточке и у клиента — ответку менеджера убираем из хелп-чата.
    try:
        await message.delete()
    except Exception:
        try:
            await message.bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            logger.warning(
                "help chat: could not delete manager reply mid=%s",
                message.message_id,
            )


async def _send_vk_support_answer(vk_id: int, answer_text: str, message: Message) -> bool:
    """Отправляет ответ поддержки пользователю VK (текст; медиа — только подпись/уведомление)."""
    try:
        from bot.vk.client import VKClient
        from bot.vk.config import load_vk_settings

        settings = load_vk_settings()
        if not settings.is_configured:
            return False
        client = VKClient(settings)
        text = message.text or message.caption
        if text:
            await client.send_message(int(vk_id), f"Ответ техподдержки:\n\n{text}")
        else:
            await client.send_message(
                int(vk_id),
                "Ответ техподдержки: сообщение без текста (файл/медиа). "
                "Напишите менеджеру, если нужно уточнение.",
            )
        return True
    except Exception:
        logger.exception("Failed to send VK support answer vk_id=%s", vk_id)
        return False


@router.callback_query(F.data == "main_menu")
async def back_to_menu(call: CallbackQuery, state: FSMContext):
    # меню клиента — только в личке
    if call.message and call.message.chat.type != "private":
        await call.answer()
        return
    await state.clear()
    track_event(EVENT_CMD_MAIN_MENU, telegram_id=call.from_user.id, props={"via": "callback"})
    await _delete_previous_menu_message(call)
    await _send_welcome(call.message)
    await call.answer()


@router.my_chat_member()
async def bot_block_status(event: ChatMemberUpdated):
    """Track block/unblock of the bot in private chats (mailing audience)."""
    if event.chat.type != "private":
        return
    user = event.from_user
    if not user:
        return
    new_status = event.new_chat_member.status
    old_status = event.old_chat_member.status
    blocked_statuses = {ChatMemberStatus.KICKED, ChatMemberStatus.LEFT}
    active_statuses = {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    }
    from bot.db.crud import touch_user_profile

    touch_user_profile(
        telegram_id=user.id,
        username=user.username,
        name=" ".join(
            p for p in (user.first_name or "", user.last_name or "") if p
        ).strip(),
        source="telegram",
    )
    if new_status in blocked_statuses and old_status not in blocked_statuses:
        set_user_blocked(user.id, True)
        track_event(EVENT_BOT_BLOCKED, telegram_id=user.id)
    elif new_status in active_statuses and old_status in blocked_statuses:
        set_user_blocked(user.id, False)
        track_event(EVENT_BOT_UNBLOCKED, telegram_id=user.id)
