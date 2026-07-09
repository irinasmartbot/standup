import sqlite3
from datetime import datetime
from bot.config import DB_PATH


def get_booking(telegram_id, event_date, event_time):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT * FROM bookings WHERE telegram_id=? AND event_date=? AND event_time=? AND status IN ('booked', 'confirmed')",
        (telegram_id, event_date, event_time),
    )
    row = c.fetchone()
    conn.close()
    return row


def get_active_booking_by_id(booking_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT * FROM bookings WHERE id=? AND status IN ('booked', 'confirmed')",
        (booking_id,),
    )
    row = c.fetchone()
    conn.close()
    return row


def create_booking(telegram_id, username, name, phone, event_date, event_time, event_address, event_location, guests):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO bookings (telegram_id, username, name, phone, event_date, event_time,
            event_address, event_location, guests, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'booked', ?)
        """,
        (telegram_id, username, name, phone, event_date, event_time, event_address, event_location, guests, datetime.now().isoformat()),
    )
    booking_id = c.lastrowid
    conn.commit()
    conn.close()
    return booking_id


def update_booking_status(booking_id, status):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE bookings SET status=? WHERE id=?", (status, booking_id))
    conn.commit()
    conn.close()


def update_booking_guests(booking_id, guests):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE bookings SET guests=? WHERE id=?", (guests, booking_id))
    conn.commit()
    conn.close()


def get_total_guests(event_date, event_time, exclude_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if exclude_id:
        c.execute(
            "SELECT SUM(guests) FROM bookings WHERE event_date=? AND event_time=? AND status IN ('booked', 'confirmed') AND id!=?",
            (event_date, event_time, exclude_id),
        )
    else:
        c.execute(
            "SELECT SUM(guests) FROM bookings WHERE event_date=? AND event_time=? AND status IN ('booked', 'confirmed')",
            (event_date, event_time),
        )
    result = c.fetchone()[0]
    conn.close()
    return result or 0


def update_reminder_flag(booking_id, flag):
    if flag not in {"reminder_24h_sent", "reminder_day_sent"}:
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f"UPDATE bookings SET {flag}=1 WHERE id=?", (booking_id,))
    conn.commit()
    conn.close()


def annul_booking(booking_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE bookings SET status='annulled', annulled_at=? WHERE id=? AND status='booked'",
        (datetime.now().isoformat(), booking_id),
    )
    conn.commit()
    conn.close()


def get_booked_for_reminders():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, telegram_id, name, event_date, event_time, event_address, event_location,
               guests, created_at, reminder_24h_sent, reminder_day_sent
        FROM bookings
        WHERE status='booked'
    """)
    rows = c.fetchall()
    conn.close()
    return rows
