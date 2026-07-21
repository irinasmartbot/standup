import asyncio
import logging
from datetime import datetime, timedelta
from html import escape

from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import CHANNEL_LINK, SITE_URL, bot
from bot.db.crud import (
    annul_booking,
    clear_raffle_nav,
    get_booked_for_reminders,
    get_booking_by_id,
    get_confirmed_raffle_past_for_cleanup,
    get_raffle_nav,
    save_confirm_message_id,
    set_rozygrysh_used,
    update_reminder_flag,
)
from bot.utils.bot_commands import refresh_user_commands
from bot.utils.ticket import format_date, now_msk, parse_created_at, parse_event_datetime

logger = logging.getLogger(__name__)


def _manager_username():
    from bot.config import MANAGER_LINK
    return "@" + MANAGER_LINK.rstrip("/").split("/")[-1]


def _raffle_reminder_kb(booking_id, include_ticket=True):
    kb = InlineKeyboardBuilder()
    if include_ticket:
        kb.button(
            text="🎟 Получить билет 🎟",
            callback_data=f"rz_ticket_{booking_id}",
            style="success",
        )
    kb.button(text="Отменить бронь", callback_data=f"rz_cancel_{booking_id}")
    kb.adjust(1)
    return kb.as_markup()


async def _clear_prev_buttons(booking_id: int, telegram_id: int):
    booking = get_booking_by_id(booking_id)
    if not booking:
        return
    confirm_message_id = booking[-1]
    if confirm_message_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=telegram_id,
                message_id=confirm_message_id,
                reply_markup=None,
            )
        except Exception:
            pass


async def _delete_raffle_nav_messages(telegram_id: int):
    nav = get_raffle_nav(telegram_id)
    if not nav:
        return
    for mid in nav:
        if mid:
            try:
                await bot.delete_message(telegram_id, mid)
            except Exception:
                pass
    clear_raffle_nav(telegram_id)


async def send_raffle_reminder(row, reminder_type: str):
    booking_id, telegram_id, name, event_date, event_time, event_address, event_location, guests, *_ = row
    date_str = format_date(event_date)
    location_line = f"📍 Локация {event_location}, {event_address}".strip(", ")

    if reminder_type == "day":
        text = (
            f"{name}, мне необходимо подтвердить, либо отменить Вашу бронь на {date_str} в {event_time} 😊\n\n"
            f"<u>Чтобы подтвердить бронь, нажми на «Получить билет»</u> 👇"
        )
    else:
        text = (
            f"Привет! 😊 Пишу подтвердить бронь на завтрашнее ШОУ! 🎤\n\n"
            f"<b>Чтобы подтвердить бронь, нажми на кнопку «Получить билет»</b>\n"
            f"❗️ <b>Внимание, если Вы не успеете подтвердить бронь, она будет аннулирована.</b>\n\n"
            f"Напоминаем, что :\n"
            f"1. Сбор гостей начинается за полчаса до начала шоу, старт в {event_time}\n"
            f"2. Рассадка осуществляется администратором рассадки на ближайшие к сцене свободные места. "
            f"Возможна подсадка за один стол других гостей для небольших компаний.\n"
            f"3. Обратите внимание, что при посещении шоу заказ минимум одной позиции по меню является обязательным.\n"
            f"4. {escape(location_line)}\n"
            f"5. Количество гостей - 1 чел.\n"
            f"6. Если поменяются планы, пожалуйста, ОБЯЗАТЕЛЬНО ПРЕДУПРЕДИТЕ 😊"
        )

    await _clear_prev_buttons(booking_id, telegram_id)
    sent = await bot.send_message(
        telegram_id,
        text,
        reply_markup=_raffle_reminder_kb(booking_id, include_ticket=True),
        parse_mode="HTML",
    )
    save_confirm_message_id(booking_id, sent.message_id)
    await refresh_user_commands(bot, telegram_id)


async def send_raffle_annulled(row):
    booking_id, telegram_id, *_ = row
    await _clear_prev_buttons(booking_id, telegram_id)
    await _delete_raffle_nav_messages(telegram_id)

    kb = InlineKeyboardBuilder()
    kb.button(text="Перейти в главное меню", callback_data="main_menu")
    kb.adjust(1)
    site = SITE_URL.replace("https://", "").replace("http://", "")
    await bot.send_message(
        telegram_id,
        f"Ваша бронь аннулирована, ждём Вас на других мероприятиях, "
        f"актуальная афиша всегда на нашем сайте: {site}\n\n"
        f"При возникновении вопросов - можно писать менеджеру {_manager_username()}\n\n"
        f"И не забудь заглянуть на наш <a href='{CHANNEL_LINK}'>канал анонсов</a> "
        f"(там часто дарят бесплатные билеты на платные шоу 😉)",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    annul_booking(booking_id)
    set_rozygrysh_used(telegram_id, False)
    await refresh_user_commands(bot, telegram_id)


async def process_raffle_reminders():
    now = now_msk().replace(tzinfo=None)
    for telegram_id in get_confirmed_raffle_past_for_cleanup():
        try:
            await _delete_raffle_nav_messages(telegram_id)
        except Exception:
            logger.exception("Failed to cleanup raffle nav for %s", telegram_id)

    for row in get_booked_for_reminders("rozygrysh"):
        booking_id = row[0]
        event_date = row[3]
        event_time = row[4]
        created_at = parse_created_at(row[8])
        reminder_24h_sent = bool(row[9])
        reminder_day_sent = bool(row[10])
        event_dt = parse_event_datetime(event_date, event_time)
        if not event_dt:
            logger.warning("Cannot parse raffle event datetime for booking %s", booking_id)
            continue

        one_day_reminder_at = datetime.combine(event_dt.date() - timedelta(days=1), datetime.min.time()).replace(hour=14)
        ten_am_on_event_day = datetime.combine(event_dt.date(), datetime.min.time()).replace(hour=10)
        time_until_at_booking = event_dt - created_at

        if created_at < ten_am_on_event_day:
            day_fire_at = ten_am_on_event_day
        elif time_until_at_booking >= timedelta(hours=2):
            day_fire_at = created_at + timedelta(hours=2)
        elif time_until_at_booking >= timedelta(hours=1):
            day_fire_at = event_dt - timedelta(hours=1)
        elif time_until_at_booking >= timedelta(minutes=30):
            day_fire_at = created_at + timedelta(minutes=15)
        elif time_until_at_booking >= timedelta(minutes=10):
            day_fire_at = created_at + timedelta(minutes=1)
        else:
            day_fire_at = None

        if created_at >= event_dt - timedelta(hours=2):
            annul_at = event_dt + timedelta(minutes=30)
        else:
            annul_at = event_dt - timedelta(hours=2)

        try:
            days_before_event = (event_dt.date() - created_at.date()).days
            if (
                not reminder_24h_sent
                and days_before_event >= 2
                and now >= one_day_reminder_at
                and now < event_dt
            ):
                await send_raffle_reminder(row, "24h")
                update_reminder_flag(booking_id, "reminder_24h_sent")

            if (
                not reminder_day_sent
                and day_fire_at is not None
                and now >= day_fire_at
                and now < event_dt
            ):
                await send_raffle_reminder(row, "day")
                update_reminder_flag(booking_id, "reminder_day_sent")

            if now >= annul_at:
                await send_raffle_annulled(row)

        except Exception:
            logger.exception("Failed to process raffle reminder for booking %s", booking_id)


async def raffle_reminder_loop():
    while True:
        await process_raffle_reminders()
        await asyncio.sleep(60)
