"""Compute planned reminder / annulment times for booked bookings (admin + loops)."""

from __future__ import annotations

from datetime import datetime, timedelta

# После дневного напоминания даём время забрать билет, прежде чем аннулировать.
_ANNUL_AFTER_REMINDER = timedelta(minutes=30)


def plan_booking_reminders(created_at: datetime, event_dt: datetime) -> dict:
    """Return planned reminder timestamps for a still-booked booking.

    Used by admin UI and by TG/VK/raffle reminder loops — keep one source of truth.
    """
    if created_at.tzinfo is not None:
        created_at = created_at.replace(tzinfo=None)
    if event_dt.tzinfo is not None:
        event_dt = event_dt.replace(tzinfo=None)

    one_day_reminder_at = datetime.combine(
        event_dt.date() - timedelta(days=1), datetime.min.time()
    ).replace(hour=14)
    ten_am_on_event_day = datetime.combine(event_dt.date(), datetime.min.time()).replace(hour=10)
    time_until_at_booking = event_dt - created_at
    days_before_event = (event_dt.date() - created_at.date()).days

    reminder_24h_at = one_day_reminder_at if days_before_event >= 2 else None

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

    annul_at = _annul_at(created_at, event_dt, day_fire_at)

    return {
        "reminder_24h_at": reminder_24h_at,
        "reminder_day_at": day_fire_at,
        "annul_at": annul_at,
    }


def _annul_at(
    created_at: datetime,
    event_dt: datetime,
    day_fire_at: datetime | None,
) -> datetime:
    """Аннуляция не должна быть раньше дневного напоминания.

    Раньше при брони днём (например 14:38 на 18:00) day-reminder был 16:38,
    а annul жёстко event−2ч = 16:00 — бронь снимали до напоминания.
    """
    late_booking = created_at >= event_dt - timedelta(hours=2)
    if late_booking:
        return event_dt + timedelta(minutes=30)

    annul_at = event_dt - timedelta(hours=2)
    if day_fire_at is not None:
        after_reminder = day_fire_at + _ANNUL_AFTER_REMINDER
        if after_reminder > annul_at:
            annul_at = after_reminder

    # Если после сдвига упёрлись в старт шоу — как у поздней брони: +30 мин после начала.
    if annul_at >= event_dt:
        return event_dt + timedelta(minutes=30)
    return annul_at
