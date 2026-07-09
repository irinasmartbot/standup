import asyncio
import logging
from datetime import datetime, timedelta

from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import bot, CHANNEL_LINK
from bot.db.crud import get_booked_for_reminders, update_reminder_flag, annul_booking
from bot.utils.ticket import format_date, parse_event_datetime, parse_created_at

logger = logging.getLogger(__name__)


def booking_manage_kb(booking_id, include_ticket=True):
    kb = InlineKeyboardBuilder()
    if include_ticket:
        kb.button(text="🎟 Получить билет", callback_data=f"get_ticket_{booking_id}")
    kb.button(text="Отменить бронь", callback_data=f"cancel_confirm_{booking_id}")
    kb.button(text="Изменить дату", callback_data=f"change_date_{booking_id}")
    kb.button(text="Изменить количество гостей", callback_data=f"change_guests_confirm_{booking_id}")
    kb.adjust(1)
    return kb.as_markup()


async def send_booking_reminder(row, reminder_type):
    booking_id, telegram_id, name, event_date, event_time, event_address, event_location, guests, *_ = row
    date_str = format_date(event_date)

    if reminder_type == "day":
        text = (
            f"{name}, мне необходимо подтвердить, либо отменить Вашу бронь на {date_str} в {event_time} 😊\n\n"
            f"Чтобы подтвердить бронь, нажми на «Получить билет» 👇"
        )
    else:
        text = (
            f"Напоминание о брони на Moscow StandUp Show:\n\n"
            f"<b>Дата:</b> {date_str}\n"
            f"<b>Время:</b> {event_time}\n"
            f"<b>Адрес:</b> {event_address}\n"
            f"<b>Количество гостей:</b> {guests} чел.\n\n"
            f"Сбор гостей начинается за полчаса до начала шоу. "
            f"Для подтверждения брони нажмите «Получить билет» 👇"
        )

    await bot.send_message(telegram_id, text, reply_markup=booking_manage_kb(booking_id), parse_mode="HTML")


async def send_annulled_message(row):
    booking_id, telegram_id, *_ = row
    kb = InlineKeyboardBuilder()
    kb.button(text="Перейти в главное меню", callback_data="main_menu")
    kb.adjust(1)
    await bot.send_message(
        telegram_id,
        f"Ваша бронь аннулирована, ждём Вас на других мероприятиях 😊\n\n"
        f"При возникновении вопросов - можно писать менеджеру @ccoverr\n\n"
        f"И не забудь заглянуть на наш <a href='{CHANNEL_LINK}'>канал анонсов</a>",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    annul_booking(booking_id)


async def process_due_reminders():
    now = datetime.now()
    for row in get_booked_for_reminders():
        booking_id = row[0]
        event_date = row[3]
        event_time = row[4]
        created_at = parse_created_at(row[8])
        reminder_24h_sent = bool(row[9])
        reminder_day_sent = bool(row[10])
        event_dt = parse_event_datetime(event_date, event_time)
        if not event_dt:
            logger.warning("Cannot parse event datetime for booking %s: %s %s", booking_id, event_date, event_time)
            continue

        one_day_reminder_at = datetime.combine(event_dt.date() - timedelta(days=1), datetime.min.time()).replace(hour=14)
        day_reminder_at = datetime.combine(event_dt.date(), datetime.min.time()).replace(hour=10)
        same_day_first_reminder_at = created_at + (
            timedelta(minutes=20) if event_dt - created_at <= timedelta(hours=1) else timedelta(hours=1)
        )

        try:
            if event_dt.date() == now.date() and not reminder_24h_sent and now >= same_day_first_reminder_at:
                await send_booking_reminder(row, "24h")
                update_reminder_flag(booking_id, "reminder_24h_sent")
                reminder_24h_sent = True
            elif not reminder_24h_sent and now >= one_day_reminder_at and now < event_dt:
                await send_booking_reminder(row, "24h")
                update_reminder_flag(booking_id, "reminder_24h_sent")
                reminder_24h_sent = True

            if not reminder_day_sent and now >= day_reminder_at and now < event_dt:
                await send_booking_reminder(row, "day")
                update_reminder_flag(booking_id, "reminder_day_sent")

            if created_at <= event_dt - timedelta(hours=3):
                annul_at = event_dt - timedelta(hours=2)
            else:
                last_reminder_at = same_day_first_reminder_at if event_dt.date() == created_at.date() else max(one_day_reminder_at, day_reminder_at)
                annul_at = last_reminder_at + timedelta(minutes=30)

            if now >= annul_at and now < event_dt:
                await send_annulled_message(row)
        except Exception:
            logger.exception("Failed to process reminder for booking %s", booking_id)


async def reminder_loop():
    while True:
        await process_due_reminders()
        await asyncio.sleep(60)
