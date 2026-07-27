import asyncio
import logging
from datetime import datetime, timedelta

from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import bot, CHANNEL_LINK
from bot.db.crud import get_booked_for_reminders, update_reminder_flag, annul_booking, save_confirm_message_id, get_booking_by_id
from bot.utils.bot_commands import refresh_user_commands
from bot.utils.nav_messages import delete_my_bookings_messages
from bot.utils.ticket import format_date, now_msk, parse_event_datetime, parse_created_at

logger = logging.getLogger(__name__)


async def _clear_prev_buttons(booking_id: int, telegram_id: int):
    """Убирает кнопки из предыдущего сообщения цепочки (confirm_message_id)."""
    booking = get_booking_by_id(booking_id)
    if not booking:
        return
    # confirm_message_id — последняя колонка в BOOKING_SELECT_SQL.
    try:
        confirm_message_id = booking[-1]
    except (IndexError, TypeError):
        return
    if confirm_message_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=telegram_id,
                message_id=confirm_message_id,
                reply_markup=None,
            )
        except Exception:
            pass


def booking_manage_kb(booking_id, include_ticket=True):
    kb = InlineKeyboardBuilder()
    if include_ticket:
        kb.button(
            text="🎟 Получить билет 🎟",
            callback_data=f"get_ticket_{booking_id}",
            style="success",
        )
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

    await _clear_prev_buttons(booking_id, telegram_id)
    sent = await bot.send_message(telegram_id, text, reply_markup=booking_manage_kb(booking_id), parse_mode="HTML")
    save_confirm_message_id(booking_id, sent.message_id)
    await refresh_user_commands(bot, telegram_id)


async def send_annulled_message(row):
    booking_id, telegram_id, *_ = row
    await _clear_prev_buttons(booking_id, telegram_id)
    await delete_my_bookings_messages(bot, telegram_id)
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
    await refresh_user_commands(bot, telegram_id)


async def process_due_reminders():
    now = now_msk().replace(tzinfo=None)
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

        # Фиксированные точки
        one_day_reminder_at = datetime.combine(event_dt.date() - timedelta(days=1), datetime.min.time()).replace(hour=14)
        ten_am_on_event_day  = datetime.combine(event_dt.date(), datetime.min.time()).replace(hour=10)
        event_is_today       = event_dt.date() == now.date()
        time_until_at_booking = event_dt - created_at

        # --- Когда должно сработать напоминание в день шоу ---
        if created_at < ten_am_on_event_day:
            # Бронь была до 10:00 дня шоу (включая накануне и раньше) → 10:00 в день шоу
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
            day_fire_at = None  # менее 10 мин до шоу — напоминание не нужно

        # --- Аннулирование ---
        if created_at >= event_dt - timedelta(hours=2):
            # Бронь сделана менее чем за 2 часа до шоу → аннулируем через 30 мин после старта
            annul_at = event_dt + timedelta(minutes=30)
        else:
            # Бронь заранее → аннулируем за 2 часа до шоу
            annul_at = event_dt - timedelta(hours=2)

        try:
            # Напоминание накануне в 14:00 — только если бронь сделана за 2+ дня до шоу
            days_before_event = (event_dt.date() - created_at.date()).days
            if (not reminder_24h_sent
                    and days_before_event >= 2
                    and now >= one_day_reminder_at
                    and now < event_dt):
                await send_booking_reminder(row, "24h")
                update_reminder_flag(booking_id, "reminder_24h_sent")

            # Напоминание в день шоу
            if (not reminder_day_sent
                    and day_fire_at is not None
                    and now >= day_fire_at
                    and now < event_dt):
                await send_booking_reminder(row, "day")
                update_reminder_flag(booking_id, "reminder_day_sent")

            # Аннулирование
            if now >= annul_at:
                await send_annulled_message(row)

        except Exception:
            logger.exception("Failed to process reminder for booking %s", booking_id)


async def reminder_loop():
    while True:
        await process_due_reminders()
        await asyncio.sleep(60)
