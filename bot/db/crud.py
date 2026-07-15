import sqlite3
from datetime import datetime
from typing import Optional

import psycopg

from bot.config import BOOKINGS_SOURCE, DATABASE_URL, DB_PATH


BOOKING_SELECT_SQL = """
SELECT
    b.id,
    u.telegram_id,
    u.username,
    u.name,
    u.phone,
    to_char(e.event_date, 'DD.MM.YYYY') AS event_date,
    to_char(e.event_time, 'HH24:MI') AS event_time,
    e.address AS event_address,
    e.location AS event_location,
    b.guests,
    b.status,
    b.created_at::text,
    b.reminder_24h_sent::int,
    b.reminder_day_sent::int,
    b.annulled_at::text,
    b.ticket_message_id,
    b.confirm_message_id
FROM bookings b
JOIN users u ON u.id = b.user_id
JOIN events e ON e.id = b.event_id
"""


REMINDER_SELECT_SQL = """
SELECT
    b.id,
    u.telegram_id,
    u.name,
    to_char(e.event_date, 'DD.MM.YYYY') AS event_date,
    to_char(e.event_time, 'HH24:MI') AS event_time,
    e.address AS event_address,
    e.location AS event_location,
    b.guests,
    b.created_at::text,
    b.reminder_24h_sent::int,
    b.reminder_day_sent::int
FROM bookings b
JOIN users u ON u.id = b.user_id
JOIN events e ON e.id = b.event_id
"""


def _use_postgres():
    return BOOKINGS_SOURCE == "postgres" and bool(DATABASE_URL)


def _parse_event_date(value):
    return datetime.strptime(value.strip(), "%d.%m.%Y").date()


def _parse_event_time(value):
    clean = (value or "").strip().replace(".", ":")
    for fmt in ("%H:%M", "%H"):
        try:
            return datetime.strptime(clean, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Invalid event time: {value}")


def _pg_connect():
    return psycopg.connect(DATABASE_URL)


def _fetchone_tuple(cur):
    row = cur.fetchone()
    return tuple(row) if row else None


def _fetchall_tuples(cur):
    return [tuple(row) for row in cur.fetchall()]


def _upsert_user(cur, telegram_id, username, name, phone):
    now = datetime.now()
    cur.execute(
        """
        INSERT INTO users (telegram_id, username, name, phone, source, created_at, last_active_at)
        VALUES (%s, %s, %s, %s, 'telegram', %s, %s)
        ON CONFLICT (telegram_id)
        DO UPDATE SET
            username = COALESCE(EXCLUDED.username, users.username),
            name = COALESCE(EXCLUDED.name, users.name),
            phone = COALESCE(EXCLUDED.phone, users.phone),
            last_active_at = EXCLUDED.last_active_at
        RETURNING id
        """,
        (telegram_id, username or None, name or None, phone or None, now, now),
    )
    return cur.fetchone()[0]


def _find_event_id(cur, event_date, event_time, event_location: Optional[str] = None):
    params = [_parse_event_date(event_date), _parse_event_time(event_time)]
    location_sql = ""
    if event_location:
        location_sql = " AND location = %s"
        params.append(event_location)

    cur.execute(
        f"""
        SELECT id
        FROM events
        WHERE format = 'proverka'
          AND event_date = %s
          AND event_time = %s
          {location_sql}
        ORDER BY id
        LIMIT 1
        """,
        params,
    )
    row = cur.fetchone()
    return row[0] if row else None


def get_booking(telegram_id, event_date, event_time):
    if _use_postgres():
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    BOOKING_SELECT_SQL
                    + """
                    WHERE u.telegram_id = %s
                      AND e.event_date = %s
                      AND e.event_time = %s
                      AND b.status IN ('booked', 'confirmed')
                    LIMIT 1
                    """,
                    (telegram_id, _parse_event_date(event_date), _parse_event_time(event_time)),
                )
                return _fetchone_tuple(cur)

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
    if _use_postgres():
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    BOOKING_SELECT_SQL
                    + " WHERE b.id = %s AND b.status IN ('booked', 'confirmed')",
                    (booking_id,),
                )
                return _fetchone_tuple(cur)

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
    if _use_postgres():
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                user_id = _upsert_user(cur, telegram_id, username, name, phone)
                event_id = _find_event_id(cur, event_date, event_time, event_location)
                if not event_id:
                    raise RuntimeError(f"Event not found for booking: {event_date} {event_time} {event_location}")

                cur.execute(
                    """
                    INSERT INTO bookings (user_id, event_id, guests, format, source, status, created_at)
                    VALUES (%s, %s, %s, 'proverka', 'telegram', 'booked', %s)
                    ON CONFLICT (user_id, event_id)
                    WHERE status IN ('booked', 'confirmed')
                    DO UPDATE SET
                        guests = EXCLUDED.guests,
                        status = 'booked',
                        updated_at = now()
                    RETURNING id
                    """,
                    (user_id, event_id, guests, datetime.now()),
                )
                booking_id = cur.fetchone()[0]
            conn.commit()
            return booking_id

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


def get_active_bookings_by_user(telegram_id):
    """Все активные брони пользователя."""
    if _use_postgres():
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    BOOKING_SELECT_SQL
                    + """
                    WHERE u.telegram_id = %s
                      AND b.status IN ('booked', 'confirmed')
                    ORDER BY e.event_date, e.event_time
                    """,
                    (telegram_id,),
                )
                return _fetchall_tuples(cur)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT * FROM bookings WHERE telegram_id=? AND status IN ('booked', 'confirmed') ORDER BY event_date",
        (telegram_id,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def get_last_phone(telegram_id):
    """Возвращает последний номер телефона пользователя из его броней."""
    if _use_postgres():
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT phone
                    FROM users
                    WHERE telegram_id = %s
                      AND phone IS NOT NULL
                      AND phone != ''
                    LIMIT 1
                    """,
                    (telegram_id,),
                )
                row = cur.fetchone()
                return row[0] if row else None

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT phone FROM bookings WHERE telegram_id=? AND phone IS NOT NULL AND phone != '' ORDER BY id DESC LIMIT 1",
        (telegram_id,),
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def get_booking_by_id(booking_id):
    """Возвращает бронь по id вне зависимости от статуса."""
    if _use_postgres():
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(BOOKING_SELECT_SQL + " WHERE b.id = %s", (booking_id,))
                return _fetchone_tuple(cur)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,))
    row = c.fetchone()
    conn.close()
    return row


def save_confirm_message_id(booking_id, message_id):
    if _use_postgres():
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE bookings SET confirm_message_id = %s, updated_at = now() WHERE id = %s",
                    (message_id, booking_id),
                )
            conn.commit()
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE bookings SET confirm_message_id=? WHERE id=?", (message_id, booking_id))
    conn.commit()
    conn.close()


def save_ticket_message_id(booking_id, message_id):
    if _use_postgres():
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE bookings SET ticket_message_id = %s, updated_at = now() WHERE id = %s",
                    (message_id, booking_id),
                )
            conn.commit()
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE bookings SET ticket_message_id=? WHERE id=?", (message_id, booking_id))
    conn.commit()
    conn.close()


def update_booking_status(booking_id, status):
    if _use_postgres():
        timestamp_field = {
            "confirmed": "confirmed_at",
            "cancelled": "cancelled_at",
            "annulled": "annulled_at",
        }.get(status)
        if timestamp_field:
            sql = f"UPDATE bookings SET status = %s, {timestamp_field} = %s, updated_at = now() WHERE id = %s"
            params = (status, datetime.now(), booking_id)
        else:
            sql = "UPDATE bookings SET status = %s, updated_at = now() WHERE id = %s"
            params = (status, booking_id)

        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE bookings SET status=? WHERE id=?", (status, booking_id))
    conn.commit()
    conn.close()


def update_booking_guests(booking_id, guests):
    if _use_postgres():
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE bookings SET guests = %s, updated_at = now() WHERE id = %s",
                    (guests, booking_id),
                )
            conn.commit()
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE bookings SET guests=? WHERE id=?", (guests, booking_id))
    conn.commit()
    conn.close()


def get_total_guests(event_date, event_time, exclude_id=None):
    if _use_postgres():
        params = [_parse_event_date(event_date), _parse_event_time(event_time)]
        exclude_sql = ""
        if exclude_id:
            exclude_sql = " AND b.id != %s"
            params.append(exclude_id)

        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT COALESCE(SUM(b.guests), 0)
                    FROM bookings b
                    JOIN events e ON e.id = b.event_id
                    WHERE e.event_date = %s
                      AND e.event_time = %s
                      AND b.status IN ('booked', 'confirmed')
                      {exclude_sql}
                    """,
                    params,
                )
                return cur.fetchone()[0] or 0

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
    if _use_postgres():
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE bookings SET {flag} = true, updated_at = now() WHERE id = %s",
                    (booking_id,),
                )
            conn.commit()
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f"UPDATE bookings SET {flag}=1 WHERE id=?", (booking_id,))
    conn.commit()
    conn.close()


def annul_booking(booking_id):
    if _use_postgres():
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bookings
                    SET status = 'annulled', annulled_at = %s, updated_at = now()
                    WHERE id = %s AND status = 'booked'
                    """,
                    (datetime.now(), booking_id),
                )
            conn.commit()
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE bookings SET status='annulled', annulled_at=? WHERE id=? AND status='booked'",
        (datetime.now().isoformat(), booking_id),
    )
    conn.commit()
    conn.close()


def get_booked_for_reminders():
    if _use_postgres():
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(REMINDER_SELECT_SQL + " WHERE b.status = 'booked'")
                return _fetchall_tuples(cur)

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
