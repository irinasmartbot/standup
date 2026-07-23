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


def _find_event_id(
    cur,
    event_date,
    event_time,
    event_location: Optional[str] = None,
    event_format: str = "proverka",
    event_id: Optional[int] = None,
):
    if event_id:
        cur.execute(
            "SELECT id FROM events WHERE id = %s AND format = %s LIMIT 1",
            (event_id, event_format),
        )
        row = cur.fetchone()
        return row[0] if row else None

    params = [event_format, _parse_event_date(event_date), _parse_event_time(event_time)]
    location_sql = ""
    if event_location:
        location_sql = " AND location = %s"
        params.append(event_location)

    cur.execute(
        f"""
        SELECT id
        FROM events
        WHERE format = %s
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


def create_booking(
    telegram_id,
    username,
    name,
    phone,
    event_date,
    event_time,
    event_address,
    event_location,
    guests,
    booking_format: str = "proverka",
    event_format: str = "proverka",
    event_id: Optional[int] = None,
):
    if _use_postgres():
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                user_id = _upsert_user(cur, telegram_id, username, name, phone)
                found_event_id = _find_event_id(
                    cur,
                    event_date,
                    event_time,
                    event_location,
                    event_format=event_format,
                    event_id=event_id,
                )
                if not found_event_id:
                    raise RuntimeError(
                        f"Event not found for booking: {event_format} {event_date} {event_time} {event_location}"
                    )

                cur.execute(
                    """
                    INSERT INTO bookings (user_id, event_id, guests, format, source, status, created_at)
                    VALUES (%s, %s, %s, %s, 'telegram', 'booked', %s)
                    ON CONFLICT (user_id, event_id)
                    WHERE status IN ('booked', 'confirmed')
                    DO UPDATE SET
                        guests = EXCLUDED.guests,
                        status = 'booked',
                        format = EXCLUDED.format,
                        updated_at = now()
                    RETURNING id
                    """,
                    (user_id, found_event_id, guests, booking_format, datetime.now()),
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
    """Guests that already took seats: only confirmed tickets count."""
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
                      AND b.status = 'confirmed'
                      {exclude_sql}
                    """,
                    params,
                )
                return cur.fetchone()[0] or 0

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if exclude_id:
        c.execute(
            "SELECT SUM(guests) FROM bookings WHERE event_date=? AND event_time=? AND status='confirmed' AND id!=?",
            (event_date, event_time, exclude_id),
        )
    else:
        c.execute(
            "SELECT SUM(guests) FROM bookings WHERE event_date=? AND event_time=? AND status='confirmed'",
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


def get_booked_for_reminders(booking_format: str = "proverka"):
    if _use_postgres():
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    REMINDER_SELECT_SQL + " WHERE b.status = 'booked' AND b.format = %s",
                    (booking_format,),
                )
                return _fetchall_tuples(cur)

    # SQLite path historically only has proverka-like rows
    if booking_format != "proverka":
        return []
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


def ensure_user(telegram_id, username=None, name=None, phone=None):
    if not _use_postgres():
        return None
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            user_id = _upsert_user(cur, telegram_id, username, name, phone)
        conn.commit()
    return user_id


def get_rozygrysh_used(telegram_id) -> bool:
    if not _use_postgres():
        return False
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(rozygrysh_used, false) FROM users WHERE telegram_id = %s",
                (telegram_id,),
            )
            row = cur.fetchone()
            return bool(row[0]) if row else False


def set_rozygrysh_used(telegram_id, used: bool):
    if not _use_postgres():
        return
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            _upsert_user(cur, telegram_id, None, None, None)
            cur.execute(
                """
                UPDATE users
                SET rozygrysh_used = %s, last_active_at = %s
                WHERE telegram_id = %s
                """,
                (used, datetime.now(), telegram_id),
            )
        conn.commit()


def get_active_raffle_booking(telegram_id):
    if not _use_postgres():
        return None
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                BOOKING_SELECT_SQL
                + """
                WHERE u.telegram_id = %s
                  AND b.format = 'rozygrysh'
                  AND b.status IN ('booked', 'confirmed')
                ORDER BY e.event_date, e.event_time
                LIMIT 1
                """,
                (telegram_id,),
            )
            return _fetchone_tuple(cur)


def get_user_bookings_for_commands(telegram_id, status=None):
    """Активные бесплатные брони для /my_bookings (proverka + rozygrysh).

    status=None — и booked, и confirmed.
    status='booked'|'confirmed' — фильтр (для совместимости со старыми вызовами).
    """
    if not _use_postgres():
        return []
    if status is not None and status not in {"booked", "confirmed"}:
        return []

    with _pg_connect() as conn:
        with conn.cursor() as cur:
            status_sql = "AND b.status = %s" if status else "AND b.status IN ('booked', 'confirmed')"
            params = (telegram_id, status) if status else (telegram_id,)
            cur.execute(
                f"""
                SELECT
                    b.id,
                    b.format,
                    b.status,
                    to_char(e.event_date, 'DD.MM.YYYY') AS event_date,
                    to_char(e.event_time, 'HH24:MI') AS event_time,
                    e.address,
                    e.location,
                    b.guests,
                    b.ticket_message_id,
                    b.confirm_message_id,
                    u.name
                FROM bookings b
                JOIN users u ON u.id = b.user_id
                JOIN events e ON e.id = b.event_id
                WHERE u.telegram_id = %s
                  AND b.format IN ('proverka', 'rozygrysh')
                  {status_sql}
                  AND e.event_date >= (now() AT TIME ZONE 'Europe/Moscow')::date
                ORDER BY e.event_date, e.event_time
                """,
                params,
            )
            return _fetchall_tuples(cur)


def reset_raffle_for_user(telegram_id) -> dict:
    """Сброс ветки розыгрыша для теста: флаг, pending, активные брони, nav."""
    result = {
        "rozygrysh_used_cleared": False,
        "bookings_cancelled": 0,
        "submissions_cancelled": 0,
        "nav_cleared": False,
    }
    if not _use_postgres():
        return result

    with _pg_connect() as conn:
        with conn.cursor() as cur:
            _upsert_user(cur, telegram_id, None, None, None)
            cur.execute(
                """
                UPDATE users
                SET rozygrysh_used = false, last_active_at = %s
                WHERE telegram_id = %s
                """,
                (datetime.now(), telegram_id),
            )
            result["rozygrysh_used_cleared"] = cur.rowcount > 0

            cur.execute(
                """
                UPDATE bookings b
                SET status = 'cancelled',
                    cancelled_at = %s,
                    updated_at = now()
                FROM users u
                WHERE b.user_id = u.id
                  AND u.telegram_id = %s
                  AND b.format = 'rozygrysh'
                  AND b.status IN ('booked', 'confirmed')
                """,
                (datetime.now(), telegram_id),
            )
            result["bookings_cancelled"] = cur.rowcount or 0

            cur.execute(
                """
                UPDATE raffle_submissions
                SET status = 'rejected',
                    reject_reason = 'test_reset',
                    reviewed_at = %s
                WHERE telegram_id = %s
                  AND status = 'pending'
                """,
                (datetime.now(), telegram_id),
            )
            result["submissions_cancelled"] = cur.rowcount or 0

            cur.execute("DELETE FROM raffle_nav WHERE telegram_id = %s", (telegram_id,))
            result["nav_cleared"] = cur.rowcount > 0
        conn.commit()
    return result


def ensure_help_tables():
    if not _use_postgres():
        return
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS help_requests (
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    question_text TEXT,
                    help_chat_id BIGINT NOT NULL,
                    help_message_id BIGINT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'answered')),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    answered_at TIMESTAMPTZ,
                    UNIQUE (help_chat_id, help_message_id)
                )
                """
            )
        conn.commit()


def create_help_request(
    telegram_id,
    username,
    full_name,
    question_text,
    help_chat_id,
    help_message_id,
):
    if not _use_postgres():
        return
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO help_requests (
                    telegram_id, username, full_name, question_text,
                    help_chat_id, help_message_id
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (help_chat_id, help_message_id) DO UPDATE SET
                    telegram_id = EXCLUDED.telegram_id,
                    username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name,
                    question_text = EXCLUDED.question_text,
                    status = 'open',
                    answered_at = NULL
                """,
                (
                    telegram_id,
                    username or None,
                    full_name or None,
                    question_text or None,
                    help_chat_id,
                    help_message_id,
                ),
            )
        conn.commit()


def get_help_request_by_message(help_chat_id, help_message_id):
    if not _use_postgres():
        return None
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT telegram_id, username, full_name, question_text, status
                FROM help_requests
                WHERE help_chat_id = %s AND help_message_id = %s
                """,
                (help_chat_id, help_message_id),
            )
            return _fetchone_tuple(cur)


def mark_help_request_answered(help_chat_id, help_message_id):
    if not _use_postgres():
        return
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE help_requests
                SET status = 'answered', answered_at = %s
                WHERE help_chat_id = %s AND help_message_id = %s
                """,
                (datetime.now(), help_chat_id, help_message_id),
            )
        conn.commit()


def get_booking_format(booking_id) -> Optional[str]:
    if not _use_postgres():
        return "proverka"
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT format FROM bookings WHERE id = %s", (booking_id,))
            row = cur.fetchone()
            return row[0] if row else None


def ensure_raffle_tables():
    """Создаёт таблицы модерации/навигации розыгрыша, если их ещё нет."""
    if not _use_postgres():
        return
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS raffle_submissions (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    telegram_id BIGINT NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    kind TEXT NOT NULL CHECK (kind IN ('post', 'review')),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'approved', 'rejected')),
                    photo_file_id TEXT NOT NULL,
                    moderation_chat_id BIGINT,
                    moderation_message_id BIGINT,
                    reject_reason TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    reviewed_at TIMESTAMPTZ
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_raffle_submissions_user_status
                ON raffle_submissions (telegram_id, status)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS raffle_nav (
                    telegram_id BIGINT PRIMARY KEY,
                    dates_message_id BIGINT,
                    card_message_id BIGINT,
                    prompt_message_id BIGINT,
                    awaiting_kind TEXT,
                    awaiting_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                "ALTER TABLE raffle_nav ADD COLUMN IF NOT EXISTS prompt_message_id BIGINT"
            )
            cur.execute(
                "ALTER TABLE raffle_nav ADD COLUMN IF NOT EXISTS awaiting_kind TEXT"
            )
            cur.execute(
                "ALTER TABLE raffle_nav ADD COLUMN IF NOT EXISTS awaiting_at TIMESTAMPTZ"
            )
        conn.commit()


def get_pending_raffle_submission(telegram_id):
    if not _use_postgres():
        return None
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, kind, status, photo_file_id, moderation_message_id
                FROM raffle_submissions
                WHERE telegram_id = %s AND status = 'pending'
                ORDER BY id DESC
                LIMIT 1
                """,
                (telegram_id,),
            )
            return _fetchone_tuple(cur)


def create_raffle_submission(telegram_id, username, full_name, kind, photo_file_id):
    if not _use_postgres():
        raise RuntimeError("Raffle submissions require PostgreSQL")
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            user_id = _upsert_user(cur, telegram_id, username, full_name, None)
            cur.execute(
                """
                INSERT INTO raffle_submissions
                    (user_id, telegram_id, username, full_name, kind, status, photo_file_id)
                VALUES (%s, %s, %s, %s, %s, 'pending', %s)
                RETURNING id
                """,
                (user_id, telegram_id, username, full_name, kind, photo_file_id),
            )
            submission_id = cur.fetchone()[0]
        conn.commit()
    return submission_id


def save_raffle_moderation_message(submission_id, chat_id, message_id):
    if not _use_postgres():
        return
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE raffle_submissions
                SET moderation_chat_id = %s, moderation_message_id = %s
                WHERE id = %s
                """,
                (chat_id, message_id, submission_id),
            )
        conn.commit()


def get_raffle_submission(submission_id):
    if not _use_postgres():
        return None
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, telegram_id, username, full_name, kind, status, photo_file_id,
                       moderation_chat_id, moderation_message_id, reject_reason
                FROM raffle_submissions
                WHERE id = %s
                """,
                (submission_id,),
            )
            return _fetchone_tuple(cur)


def get_raffle_submission_by_mod_message(moderation_chat_id, moderation_message_id):
    if not _use_postgres():
        return None
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, telegram_id, username, full_name, kind, status, photo_file_id,
                       moderation_chat_id, moderation_message_id, reject_reason
                FROM raffle_submissions
                WHERE moderation_chat_id = %s
                  AND moderation_message_id = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (moderation_chat_id, moderation_message_id),
            )
            return _fetchone_tuple(cur)


def update_raffle_submission_status(submission_id, status, reject_reason=None):
    if not _use_postgres():
        return
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE raffle_submissions
                SET status = %s,
                    reject_reason = %s,
                    reviewed_at = %s
                WHERE id = %s
                """,
                (status, reject_reason, datetime.now(), submission_id),
            )
        conn.commit()


def cancel_raffle_submission(submission_id, reason="send_failed"):
    """Снимает pending, если скрин не удалось отправить в чат модерации."""
    update_raffle_submission_status(submission_id, "rejected", reject_reason=reason)


def save_raffle_nav(telegram_id, dates_message_id=None, card_message_id=None, prompt_message_id=None):
    if not _use_postgres():
        return
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO raffle_nav (
                    telegram_id, dates_message_id, card_message_id, prompt_message_id, updated_at
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    dates_message_id = COALESCE(EXCLUDED.dates_message_id, raffle_nav.dates_message_id),
                    card_message_id = COALESCE(EXCLUDED.card_message_id, raffle_nav.card_message_id),
                    prompt_message_id = COALESCE(EXCLUDED.prompt_message_id, raffle_nav.prompt_message_id),
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    telegram_id,
                    dates_message_id,
                    card_message_id,
                    prompt_message_id,
                    datetime.now(),
                ),
            )
        conn.commit()


def get_raffle_nav(telegram_id):
    if not _use_postgres():
        return None
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT dates_message_id, card_message_id, prompt_message_id
                FROM raffle_nav
                WHERE telegram_id = %s
                """,
                (telegram_id,),
            )
            return _fetchone_tuple(cur)


def clear_raffle_nav(telegram_id):
    if not _use_postgres():
        return
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM raffle_nav WHERE telegram_id = %s", (telegram_id,))
        conn.commit()


def set_raffle_awaiting_screenshot(telegram_id: int, kind: str):
    """Пишем в БД, что ждём скрин — FSM в памяти сбрасывается при рестарте."""
    if not _use_postgres() or kind not in {"post", "review"}:
        return
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO raffle_nav (telegram_id, awaiting_kind, awaiting_at, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    awaiting_kind = EXCLUDED.awaiting_kind,
                    awaiting_at = EXCLUDED.awaiting_at,
                    updated_at = EXCLUDED.updated_at
                """,
                (telegram_id, kind, datetime.now(), datetime.now()),
            )
        conn.commit()


def get_raffle_awaiting_screenshot(telegram_id: int):
    if not _use_postgres():
        return None
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT awaiting_kind FROM raffle_nav WHERE telegram_id = %s",
                (telegram_id,),
            )
            row = _fetchone_tuple(cur)
            if not row or not row[0]:
                return None
            kind = row[0]
            return kind if kind in {"post", "review"} else None


def clear_raffle_awaiting_screenshot(telegram_id: int):
    if not _use_postgres():
        return
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE raffle_nav
                SET awaiting_kind = NULL, awaiting_at = NULL, updated_at = %s
                WHERE telegram_id = %s
                """,
                (datetime.now(), telegram_id),
            )
        conn.commit()


def get_confirmed_raffle_past_for_cleanup():
    """Подтверждённые розыгрыш-брони после окончания шоу — для очистки UI."""
    if not _use_postgres():
        return []
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.telegram_id
                FROM bookings b
                JOIN users u ON u.id = b.user_id
                JOIN events e ON e.id = b.event_id
                JOIN raffle_nav n ON n.telegram_id = u.telegram_id
                WHERE b.format = 'rozygrysh'
                  AND b.status = 'confirmed'
                  AND (e.event_date + e.event_time) < now()
                """
            )
            return [row[0] for row in cur.fetchall()]
