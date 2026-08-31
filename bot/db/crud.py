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
    b.reminder_day_sent::int,
    u.vk_id,
    b.source
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


def _upsert_user(cur, telegram_id, username, name, phone, *, vk_id=None, source=None):
    now = datetime.now()
    if vk_id is not None and telegram_id is None:
        cur.execute(
            """
            INSERT INTO users (vk_id, username, name, phone, source, created_at, last_active_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (vk_id)
            DO UPDATE SET
                username = COALESCE(EXCLUDED.username, users.username),
                name = COALESCE(EXCLUDED.name, users.name),
                phone = COALESCE(EXCLUDED.phone, users.phone),
                last_active_at = EXCLUDED.last_active_at
            RETURNING id
            """,
            (
                vk_id,
                username or None,
                name or None,
                phone or None,
                source or "vkontakte",
                now,
                now,
            ),
        )
        return cur.fetchone()[0]

    if telegram_id is None:
        raise ValueError("telegram_id or vk_id is required for user upsert")

    cur.execute(
        """
        INSERT INTO users (telegram_id, username, name, phone, source, created_at, last_active_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (telegram_id)
        DO UPDATE SET
            username = COALESCE(EXCLUDED.username, users.username),
            name = COALESCE(EXCLUDED.name, users.name),
            phone = COALESCE(EXCLUDED.phone, users.phone),
            last_active_at = EXCLUDED.last_active_at
        RETURNING id
        """,
        (
            telegram_id,
            username or None,
            name or None,
            phone or None,
            source or "telegram",
            now,
            now,
        ),
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


def get_booking(telegram_id=None, event_date=None, event_time=None, *, vk_id=None):
    """Активная бронь пользователя на слот (дата+время). telegram_id или vk_id."""
    if telegram_id is None and vk_id is None:
        return None
    if _use_postgres():
        if vk_id is not None:
            user_sql = "u.vk_id = %s"
            user_param = int(vk_id)
        else:
            user_sql = "u.telegram_id = %s"
            user_param = telegram_id
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    BOOKING_SELECT_SQL
                    + f"""
                    WHERE {user_sql}
                      AND e.event_date = %s
                      AND e.event_time = %s
                      AND b.status IN ('booked', 'confirmed')
                    LIMIT 1
                    """,
                    (user_param, _parse_event_date(event_date), _parse_event_time(event_time)),
                )
                return _fetchone_tuple(cur)

    if telegram_id is None:
        return None
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
    *,
    vk_id=None,
    source: Optional[str] = None,
):
    booking_source = source or ("vkontakte" if vk_id is not None and telegram_id is None else "telegram")
    if _use_postgres():
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                user_id = _upsert_user(
                    cur,
                    telegram_id,
                    username,
                    name,
                    phone,
                    vk_id=vk_id,
                    source=booking_source,
                )
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
                    VALUES (%s, %s, %s, %s, %s, 'booked', %s)
                    ON CONFLICT (user_id, event_id)
                    WHERE status IN ('booked', 'confirmed')
                    DO UPDATE SET
                        guests = EXCLUDED.guests,
                        status = 'booked',
                        format = EXCLUDED.format,
                        source = EXCLUDED.source,
                        updated_at = now()
                    RETURNING id
                    """,
                    (user_id, found_event_id, guests, booking_format, booking_source, datetime.now()),
                )
                booking_id = cur.fetchone()[0]
            conn.commit()
            try:
                from bot.db.analytics import (
                    EVENT_BOOKING_CREATED,
                    latest_metrika_attribution,
                    track_event,
                )
                from bot.services.metrika import queue_booking_created_goal

                attribution = latest_metrika_attribution(
                    telegram_id=telegram_id,
                    vk_id=vk_id,
                    user_id=user_id,
                )
                event_props = {
                    "format": booking_format,
                    "event_format": event_format,
                    "guests": guests,
                    "event_date": event_date,
                    "event_time": event_time,
                    "location": event_location,
                }
                if attribution.get("cid"):
                    event_props["cid"] = attribution["cid"]
                if attribution.get("source"):
                    event_props["source"] = attribution["source"]

                track_event(
                    EVENT_BOOKING_CREATED,
                    telegram_id=telegram_id,
                    vk_id=vk_id,
                    user_id=user_id,
                    channel=booking_source if booking_source in {"telegram", "vkontakte"} else "unknown",
                    event_id=found_event_id,
                    booking_id=booking_id,
                    props=event_props,
                )
                queue_booking_created_goal(
                    client_id=str(attribution.get("cid") or ""),
                    created_at=datetime.now(),
                    context={
                        "booking_id": booking_id,
                        "user_id": user_id,
                        "telegram_id": telegram_id,
                        "vk_id": vk_id,
                        "source": attribution.get("source") or "",
                    },
                )
            except Exception:
                pass
            return booking_id

    if telegram_id is None:
        raise RuntimeError("SQLite bookings require telegram_id")

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
                    ORDER BY e.event_date ASC, e.event_time ASC, b.id ASC
                    """,
                    (telegram_id,),
                )
                return _fetchall_tuples(cur)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT * FROM bookings WHERE telegram_id=? AND status IN ('booked', 'confirmed') ORDER BY event_date ASC, event_time ASC, id ASC",
        (telegram_id,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def get_same_day_bookings_summary(telegram_id=None, event_date=None, exclude_time=None, *, vk_id=None):
    """Активные брони на дату: [(time, location, format), ...]. telegram_id или vk_id."""
    if telegram_id is None and vk_id is None:
        return []
    if _use_postgres():
        if vk_id is not None:
            user_sql = "u.vk_id = %s"
            user_param = int(vk_id)
        else:
            user_sql = "u.telegram_id = %s"
            user_param = telegram_id
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        to_char(e.event_time, 'HH24:MI') AS event_time,
                        e.location,
                        b.format
                    FROM bookings b
                    JOIN users u ON u.id = b.user_id
                    JOIN events e ON e.id = b.event_id
                    WHERE {user_sql}
                      AND b.status IN ('booked', 'confirmed')
                      AND e.event_date = %s
                    ORDER BY e.event_time ASC, b.id ASC
                    """,
                    (user_param, _parse_event_date(event_date)),
                )
                rows = _fetchall_tuples(cur)
        if exclude_time:
            rows = [r for r in rows if (r[0] or "") != exclude_time]
        return rows

    if telegram_id is None:
        return []
    rows = []
    for booking in get_active_bookings_by_user(telegram_id):
        if (booking[5] or "") != event_date:
            continue
        if exclude_time and (booking[6] or "") == exclude_time:
            continue
        # SQLite legacy: format колонки может не быть
        rows.append((booking[6], booking[8], "proverka"))
    return rows


def get_last_phone(telegram_id=None, *, vk_id=None):
    """Возвращает последний номер телефона пользователя из users."""
    if not _use_postgres():
        if telegram_id is None:
            return None
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "SELECT phone FROM bookings WHERE telegram_id=? AND phone IS NOT NULL AND phone != '' ORDER BY id DESC LIMIT 1",
            (telegram_id,),
        )
        row = c.fetchone()
        conn.close()
        return row[0] if row else None

    with _pg_connect() as conn:
        with conn.cursor() as cur:
            if vk_id is not None:
                cur.execute(
                    """
                    SELECT phone
                    FROM users
                    WHERE vk_id = %s
                      AND phone IS NOT NULL
                      AND phone != ''
                    LIMIT 1
                    """,
                    (vk_id,),
                )
                row = cur.fetchone()
                return row[0] if row else None
            if telegram_id is None:
                return None
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

        meta = None
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT u.telegram_id, u.vk_id, b.format, b.event_id, b.user_id, b.source
                    FROM bookings b
                    JOIN users u ON u.id = b.user_id
                    WHERE b.id = %s
                    """,
                    (booking_id,),
                )
                meta = cur.fetchone()
                cur.execute(sql, params)
            conn.commit()
        if meta:
            try:
                from bot.db.analytics import (
                    EVENT_BOOKING_ANNULLED,
                    EVENT_BOOKING_CANCELLED,
                    EVENT_BOOKING_CONFIRMED,
                    track_event,
                )

                telegram_id, vk_id, booking_format, event_id, user_id, booking_source = meta
                event_name = {
                    "confirmed": EVENT_BOOKING_CONFIRMED,
                    "cancelled": EVENT_BOOKING_CANCELLED,
                    "annulled": EVENT_BOOKING_ANNULLED,
                }.get(status)
                if event_name:
                    channel = booking_source if booking_source in {"telegram", "vkontakte"} else "unknown"
                    track_event(
                        event_name,
                        telegram_id=telegram_id,
                        vk_id=vk_id,
                        user_id=user_id,
                        channel=channel,
                        event_id=event_id,
                        booking_id=booking_id,
                        props={"format": booking_format},
                    )
            except Exception:
                pass
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
        meta = None
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT u.telegram_id, u.vk_id, b.format, b.event_id, b.user_id, b.source
                    FROM bookings b
                    JOIN users u ON u.id = b.user_id
                    WHERE b.id = %s AND b.status = 'booked'
                    """,
                    (booking_id,),
                )
                meta = cur.fetchone()
                cur.execute(
                    """
                    UPDATE bookings
                    SET status = 'annulled', annulled_at = %s, updated_at = now()
                    WHERE id = %s AND status = 'booked'
                    """,
                    (datetime.now(), booking_id),
                )
                updated = cur.rowcount or 0
            conn.commit()
        if updated and meta:
            try:
                from bot.db.analytics import EVENT_BOOKING_ANNULLED, track_event

                telegram_id, vk_id, booking_format, event_id, user_id, booking_source = meta
                channel = booking_source if booking_source in {"telegram", "vkontakte"} else "unknown"
                track_event(
                    EVENT_BOOKING_ANNULLED,
                    telegram_id=telegram_id,
                    vk_id=vk_id,
                    user_id=user_id,
                    channel=channel,
                    event_id=event_id,
                    booking_id=booking_id,
                    props={"format": booking_format, "source": "annul_booking"},
                )
            except Exception:
                pass
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
               guests, created_at, reminder_24h_sent, reminder_day_sent,
               NULL AS vk_id, 'telegram' AS source
        FROM bookings
        WHERE status='booked'
    """)
    rows = c.fetchall()
    conn.close()
    return rows


def ensure_user(telegram_id=None, username=None, name=None, phone=None, *, vk_id=None, source=None):
    if not _use_postgres():
        return None
    if telegram_id is None and vk_id is None:
        return None
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            user_id = _upsert_user(
                cur,
                telegram_id,
                username,
                name,
                phone,
                vk_id=vk_id,
                source=source,
            )
        conn.commit()
    return user_id


def touch_user_profile(
    *,
    telegram_id=None,
    vk_id=None,
    username=None,
    name=None,
    source=None,
):
    """Обновить имя/ник с профиля мессенджера (пустое не затирает уже сохранённое)."""
    clean_name = (name or "").strip() or None
    clean_username = (username or "").strip().lstrip("@") or None
    return ensure_user(
        telegram_id=telegram_id,
        vk_id=vk_id,
        username=clean_username,
        name=clean_name,
        source=source,
    )


def get_rozygrysh_used(telegram_id=None, *, vk_id=None) -> bool:
    if not _use_postgres():
        return False
    if telegram_id is None and vk_id is None:
        return False
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            if vk_id is not None:
                cur.execute(
                    "SELECT COALESCE(rozygrysh_used, false) FROM users WHERE vk_id = %s",
                    (int(vk_id),),
                )
            else:
                cur.execute(
                    "SELECT COALESCE(rozygrysh_used, false) FROM users WHERE telegram_id = %s",
                    (telegram_id,),
                )
            row = cur.fetchone()
            return bool(row[0]) if row else False


def set_rozygrysh_used(telegram_id=None, used: bool = True, *, vk_id=None):
    if not _use_postgres():
        return
    if telegram_id is None and vk_id is None:
        raise ValueError("telegram_id or vk_id required")
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            if vk_id is not None:
                _upsert_user(cur, None, None, None, None, vk_id=int(vk_id))
                cur.execute(
                    """
                    UPDATE users
                    SET rozygrysh_used = %s, last_active_at = %s
                    WHERE vk_id = %s
                    """,
                    (used, datetime.now(), int(vk_id)),
                )
            else:
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


def ensure_pdn_consent_columns() -> None:
    if not _use_postgres():
        return
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS consent_accepted_at TIMESTAMPTZ"
            )
            cur.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS consent_version TEXT"
            )
        conn.commit()


def has_pdn_consent(telegram_id=None, *, vk_id=None, version: str | None = None) -> bool:
    """True, если пользователь уже принял согласие нужной версии.

    Без PostgreSQL (локальный sqlite) — True, чтобы не блокировать разработку.
    """
    if not _use_postgres():
        return True
    if telegram_id is None and vk_id is None:
        return False
    from bot.pdn_consent import CONSENT_VERSION

    need_version = version or CONSENT_VERSION
    ensure_pdn_consent_columns()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            if vk_id is not None:
                cur.execute(
                    """
                    SELECT consent_accepted_at IS NOT NULL
                           AND COALESCE(consent_version, '') = %s
                    FROM users
                    WHERE vk_id = %s
                    """,
                    (need_version, int(vk_id)),
                )
            else:
                cur.execute(
                    """
                    SELECT consent_accepted_at IS NOT NULL
                           AND COALESCE(consent_version, '') = %s
                    FROM users
                    WHERE telegram_id = %s
                    """,
                    (need_version, int(telegram_id)),
                )
            row = cur.fetchone()
            return bool(row and row[0])


def set_pdn_consent(
    telegram_id=None,
    *,
    vk_id=None,
    version: str | None = None,
    username=None,
    source=None,
) -> None:
    if not _use_postgres():
        return
    if telegram_id is None and vk_id is None:
        raise ValueError("telegram_id or vk_id required")
    from bot.pdn_consent import CONSENT_VERSION

    consent_version = version or CONSENT_VERSION
    ensure_pdn_consent_columns()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            if vk_id is not None:
                _upsert_user(
                    cur,
                    None,
                    username,
                    None,
                    None,
                    vk_id=int(vk_id),
                    source=source or "vkontakte",
                )
                cur.execute(
                    """
                    UPDATE users
                    SET consent_accepted_at = %s,
                        consent_version = %s,
                        last_active_at = %s
                    WHERE vk_id = %s
                    """,
                    (datetime.now(), consent_version, datetime.now(), int(vk_id)),
                )
            else:
                _upsert_user(
                    cur,
                    int(telegram_id),
                    username,
                    None,
                    None,
                    source=source or "telegram",
                )
                cur.execute(
                    """
                    UPDATE users
                    SET consent_accepted_at = %s,
                        consent_version = %s,
                        last_active_at = %s
                    WHERE telegram_id = %s
                    """,
                    (datetime.now(), consent_version, datetime.now(), int(telegram_id)),
                )
        conn.commit()


def get_active_raffle_booking(telegram_id=None, *, vk_id=None):
    if not _use_postgres():
        return None
    if telegram_id is None and vk_id is None:
        return None
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            if vk_id is not None:
                cur.execute(
                    BOOKING_SELECT_SQL
                    + """
                    WHERE u.vk_id = %s
                      AND b.format = 'rozygrysh'
                      AND b.status IN ('booked', 'confirmed')
                    ORDER BY e.event_date, e.event_time
                    LIMIT 1
                    """,
                    (int(vk_id),),
                )
            else:
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


def get_user_bookings_for_commands(telegram_id=None, status=None, *, vk_id=None):
    """Активные бесплатные брони для /my_bookings (proverka + rozygrysh).

    status=None — и booked, и confirmed.
    status='booked'|'confirmed' — фильтр (для совместимости со старыми вызовами).
    Передайте telegram_id или vk_id.
    """
    if not _use_postgres():
        return []
    if status is not None and status not in {"booked", "confirmed"}:
        return []
    if telegram_id is None and vk_id is None:
        return []

    with _pg_connect() as conn:
        with conn.cursor() as cur:
            status_sql = "AND b.status = %s" if status else "AND b.status IN ('booked', 'confirmed')"
            if vk_id is not None:
                user_sql = "u.vk_id = %s"
                user_param = int(vk_id)
            else:
                user_sql = "u.telegram_id = %s"
                user_param = telegram_id
            params = (user_param, status) if status else (user_param,)
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
                WHERE {user_sql}
                  AND b.format IN ('proverka', 'rozygrysh')
                  {status_sql}
                  AND e.event_date >= (now() AT TIME ZONE 'Europe/Moscow')::date
                ORDER BY e.event_date ASC, e.event_time ASC, b.id ASC
                """,
                params,
            )
            return _fetchall_tuples(cur)


def reset_raffle_for_user(telegram_id=None, *, vk_id=None) -> dict:
    """Сброс ветки розыгрыша для теста: флаг, pending, активные брони, nav."""
    result = {
        "rozygrysh_used_cleared": False,
        "bookings_cancelled": 0,
        "submissions_cancelled": 0,
        "nav_cleared": False,
    }
    if not _use_postgres():
        return result
    if telegram_id is None and vk_id is None:
        return result

    with _pg_connect() as conn:
        with conn.cursor() as cur:
            if vk_id is not None:
                vk_id = int(vk_id)
                _upsert_user(cur, None, None, None, None, vk_id=vk_id)
                cur.execute(
                    """
                    UPDATE users
                    SET rozygrysh_used = false, last_active_at = %s
                    WHERE vk_id = %s
                    """,
                    (datetime.now(), vk_id),
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
                      AND u.vk_id = %s
                      AND b.format = 'rozygrysh'
                      AND b.status IN ('booked', 'confirmed')
                    """,
                    (datetime.now(), vk_id),
                )
                result["bookings_cancelled"] = cur.rowcount or 0

                cur.execute(
                    """
                    UPDATE raffle_submissions
                    SET status = 'rejected',
                        reject_reason = 'test_reset',
                        reviewed_at = %s
                    WHERE vk_id = %s
                      AND status = 'pending'
                    """,
                    (datetime.now(), vk_id),
                )
                result["submissions_cancelled"] = cur.rowcount or 0

                cur.execute(
                    "DELETE FROM raffle_vk_awaiting WHERE vk_id = %s",
                    (vk_id,),
                )
                result["nav_cleared"] = cur.rowcount > 0
            else:
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
                    telegram_id BIGINT,
                    vk_id BIGINT,
                    username TEXT,
                    full_name TEXT,
                    question_text TEXT,
                    help_chat_id BIGINT NOT NULL,
                    help_message_id BIGINT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'answered')),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    answered_at TIMESTAMPTZ,
                    UNIQUE (help_chat_id, help_message_id),
                    CHECK (telegram_id IS NOT NULL OR vk_id IS NOT NULL)
                )
                """
            )
            # Миграции для уже существующей таблицы (TG-only).
            cur.execute("ALTER TABLE help_requests ADD COLUMN IF NOT EXISTS vk_id BIGINT")
            cur.execute("ALTER TABLE help_requests ALTER COLUMN telegram_id DROP NOT NULL")
        conn.commit()


def create_help_request(
    telegram_id,
    username,
    full_name,
    question_text,
    help_chat_id,
    help_message_id,
    *,
    vk_id=None,
):
    if not _use_postgres():
        return
    if telegram_id is None and vk_id is None:
        raise ValueError("telegram_id or vk_id is required for help request")
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO help_requests (
                    telegram_id, vk_id, username, full_name, question_text,
                    help_chat_id, help_message_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (help_chat_id, help_message_id) DO UPDATE SET
                    telegram_id = EXCLUDED.telegram_id,
                    vk_id = EXCLUDED.vk_id,
                    username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name,
                    question_text = EXCLUDED.question_text,
                    status = 'open',
                    answered_at = NULL
                """,
                (
                    telegram_id,
                    vk_id,
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
                SELECT telegram_id, username, full_name, question_text, status, vk_id
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
                    telegram_id BIGINT,
                    vk_id BIGINT,
                    username TEXT,
                    full_name TEXT,
                    kind TEXT NOT NULL CHECK (kind IN ('post', 'review')),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'approved', 'rejected')),
                    photo_file_id TEXT NOT NULL,
                    photo_file_unique_id TEXT,
                    source_chat_id BIGINT,
                    source_message_id BIGINT,
                    source_message_at TIMESTAMPTZ,
                    moderation_chat_id BIGINT,
                    moderation_message_id BIGINT,
                    reject_reason TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    reviewed_at TIMESTAMPTZ,
                    CHECK (telegram_id IS NOT NULL OR vk_id IS NOT NULL)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_raffle_submissions_user_status
                ON raffle_submissions (telegram_id, status)
                """
            )
            for ddl in (
                "ALTER TABLE raffle_submissions ADD COLUMN IF NOT EXISTS photo_file_unique_id TEXT",
                "ALTER TABLE raffle_submissions ADD COLUMN IF NOT EXISTS source_chat_id BIGINT",
                "ALTER TABLE raffle_submissions ADD COLUMN IF NOT EXISTS source_message_id BIGINT",
                "ALTER TABLE raffle_submissions ADD COLUMN IF NOT EXISTS source_message_at TIMESTAMPTZ",
                "ALTER TABLE raffle_submissions ADD COLUMN IF NOT EXISTS vk_id BIGINT",
                "ALTER TABLE raffle_submissions ALTER COLUMN telegram_id DROP NOT NULL",
            ):
                cur.execute(ddl)
            # Индекс только после ADD COLUMN: на старых БД таблицы уже есть без vk_id.
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_raffle_submissions_vk_status
                ON raffle_submissions (vk_id, status)
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
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS raffle_vk_awaiting (
                    vk_id BIGINT PRIMARY KEY,
                    awaiting_kind TEXT NOT NULL CHECK (awaiting_kind IN ('post', 'review')),
                    awaiting_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        conn.commit()


def get_pending_raffle_submission(telegram_id=None, *, vk_id=None):
    if not _use_postgres():
        return None
    if telegram_id is None and vk_id is None:
        return None
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            if vk_id is not None:
                cur.execute(
                    """
                    SELECT id, kind, status, photo_file_id, moderation_message_id
                    FROM raffle_submissions
                    WHERE vk_id = %s AND status = 'pending'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (int(vk_id),),
                )
            else:
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


def get_approved_raffle_submission(telegram_id=None, *, vk_id=None):
    """Последний принятый скрин (даёт право выбрать дату / забронировать)."""
    if not _use_postgres():
        return None
    if telegram_id is None and vk_id is None:
        return None
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            if vk_id is not None:
                cur.execute(
                    """
                    SELECT id, kind, status, photo_file_id, moderation_message_id
                    FROM raffle_submissions
                    WHERE vk_id = %s AND status = 'approved'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (int(vk_id),),
                )
            else:
                cur.execute(
                    """
                    SELECT id, kind, status, photo_file_id, moderation_message_id
                    FROM raffle_submissions
                    WHERE telegram_id = %s AND status = 'approved'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (telegram_id,),
                )
            return _fetchone_tuple(cur)


def revoke_approved_raffle_submissions(
    telegram_id=None, *, vk_id=None, reason: str = "booking_cancelled"
) -> int:
    """Снять право бронировать по старому скрину (после отмены / аннуляции брони)."""
    if not _use_postgres():
        return 0
    if telegram_id is None and vk_id is None:
        return 0
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            if vk_id is not None:
                cur.execute(
                    """
                    UPDATE raffle_submissions
                    SET status = 'rejected',
                        reject_reason = %s,
                        reviewed_at = %s
                    WHERE vk_id = %s AND status = 'approved'
                    """,
                    (reason, datetime.now(), int(vk_id)),
                )
            else:
                cur.execute(
                    """
                    UPDATE raffle_submissions
                    SET status = 'rejected',
                        reject_reason = %s,
                        reviewed_at = %s
                    WHERE telegram_id = %s AND status = 'approved'
                    """,
                    (reason, datetime.now(), telegram_id),
                )
            n = cur.rowcount or 0
        conn.commit()
    return n


def has_raffle_screen_entitlement(telegram_id=None, *, vk_id=None) -> bool:
    """Можно бронировать/менять дату: есть активная бронь или принятый скрин."""
    if get_active_raffle_booking(telegram_id=telegram_id, vk_id=vk_id):
        return True
    return get_approved_raffle_submission(telegram_id=telegram_id, vk_id=vk_id) is not None


def clear_raffle_after_user_cancel(
    telegram_id=None, *, vk_id=None, reason: str = "booking_cancelled"
) -> int:
    """Отмена/аннуляция брони гостем: снова можно участвовать, но только с новым скрином."""
    if telegram_id is not None:
        set_rozygrysh_used(telegram_id, False)
    elif vk_id is not None:
        set_rozygrysh_used(vk_id=vk_id, used=False)
    return revoke_approved_raffle_submissions(
        telegram_id=telegram_id, vk_id=vk_id, reason=reason
    )


def create_raffle_submission(
    telegram_id=None,
    username=None,
    full_name=None,
    kind=None,
    photo_file_id=None,
    *,
    vk_id=None,
    photo_file_unique_id=None,
    source_chat_id=None,
    source_message_id=None,
    source_message_at=None,
):
    if not _use_postgres():
        raise RuntimeError("Raffle submissions require PostgreSQL")
    if telegram_id is None and vk_id is None:
        raise ValueError("telegram_id or vk_id required")
    if not kind or not photo_file_id:
        raise ValueError("kind and photo_file_id required")
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            if vk_id is not None:
                user_id = _upsert_user(
                    cur, None, username, full_name, None, vk_id=int(vk_id)
                )
            else:
                user_id = _upsert_user(cur, telegram_id, username, full_name, None)
            cur.execute(
                """
                INSERT INTO raffle_submissions
                    (user_id, telegram_id, vk_id, username, full_name, kind, status, photo_file_id,
                     photo_file_unique_id, source_chat_id, source_message_id, source_message_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    user_id,
                    telegram_id,
                    int(vk_id) if vk_id is not None else None,
                    username,
                    full_name,
                    kind,
                    photo_file_id,
                    photo_file_unique_id,
                    source_chat_id,
                    source_message_id,
                    source_message_at,
                ),
            )
            submission_id = cur.fetchone()[0]
        conn.commit()
    return submission_id


def get_raffle_submissions_for_telegram(telegram_id: int, limit: int = 20) -> list[dict]:
    if not _use_postgres() or not telegram_id:
        return []
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id, kind, status, photo_file_id, photo_file_unique_id,
                    source_chat_id, source_message_id, source_message_at,
                    moderation_chat_id, moderation_message_id, reject_reason,
                    created_at, reviewed_at, vk_id, telegram_id
                FROM raffle_submissions
                WHERE telegram_id = %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (telegram_id, limit),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_raffle_submissions_for_vk(vk_id: int, limit: int = 20) -> list[dict]:
    if not _use_postgres() or not vk_id:
        return []
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id, kind, status, photo_file_id, photo_file_unique_id,
                    source_chat_id, source_message_id, source_message_at,
                    moderation_chat_id, moderation_message_id, reject_reason,
                    created_at, reviewed_at, vk_id, telegram_id
                FROM raffle_submissions
                WHERE vk_id = %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (int(vk_id), limit),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_user_raffle_flags(
    telegram_id: int = None, user_id: int = None, *, vk_id: int = None
) -> dict:
    if not _use_postgres() or (not telegram_id and not user_id and not vk_id):
        return {"rozygrysh_used": False, "is_blocked": False}
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            try:
                if user_id:
                    cur.execute(
                        """
                        SELECT COALESCE(rozygrysh_used, false), COALESCE(is_blocked, false)
                        FROM users WHERE id = %s
                        """,
                        (user_id,),
                    )
                elif vk_id is not None:
                    cur.execute(
                        """
                        SELECT COALESCE(rozygrysh_used, false), COALESCE(is_blocked, false)
                        FROM users WHERE vk_id = %s
                        """,
                        (int(vk_id),),
                    )
                else:
                    cur.execute(
                        """
                        SELECT COALESCE(rozygrysh_used, false), COALESCE(is_blocked, false)
                        FROM users WHERE telegram_id = %s
                        """,
                        (telegram_id,),
                    )
                row = cur.fetchone()
            except Exception:
                if user_id:
                    cur.execute(
                        "SELECT COALESCE(rozygrysh_used, false) FROM users WHERE id = %s",
                        (user_id,),
                    )
                elif vk_id is not None:
                    cur.execute(
                        "SELECT COALESCE(rozygrysh_used, false) FROM users WHERE vk_id = %s",
                        (int(vk_id),),
                    )
                else:
                    cur.execute(
                        "SELECT COALESCE(rozygrysh_used, false) FROM users WHERE telegram_id = %s",
                        (telegram_id,),
                    )
                row = cur.fetchone()
                if not row:
                    return {"rozygrysh_used": False, "is_blocked": False}
                return {"rozygrysh_used": bool(row[0]), "is_blocked": False}
            if not row:
                return {"rozygrysh_used": False, "is_blocked": False}
            return {"rozygrysh_used": bool(row[0]), "is_blocked": bool(row[1])}


def ensure_offline_gift_tables():
    if not _use_postgres():
        return
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS vk_offline_gift_entries (
                    id BIGSERIAL PRIMARY KEY,
                    event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    vk_id BIGINT NOT NULL,
                    full_name TEXT,
                    is_winner BOOLEAN NOT NULL DEFAULT false,
                    subscribed_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (event_id, vk_id)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_vk_offline_gift_event
                ON vk_offline_gift_entries (event_id, created_at)
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_vk_offline_gift_winner
                ON vk_offline_gift_entries (event_id)
                WHERE is_winner
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS vk_offline_gift_pending (
                    vk_id BIGINT PRIMARY KEY,
                    event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS vk_offline_gift_timers (
                    vk_id BIGINT PRIMARY KEY,
                    kind TEXT NOT NULL
                        CHECK (kind IN ('choose', 'sub_check')),
                    event_id BIGINT REFERENCES events(id) ON DELETE CASCADE,
                    due_at TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_vk_offline_gift_timers_due
                ON vk_offline_gift_timers (due_at)
                """
            )
        conn.commit()


def schedule_offline_gift_timer(
    *,
    vk_id: int,
    kind: str,
    delay_sec: float,
    event_id: int | None = None,
) -> None:
    """Persist offline-gift reminder so it survives VK bot restarts."""
    if not _use_postgres():
        return
    if kind not in {"choose", "sub_check"}:
        raise ValueError(f"unknown offline gift timer kind: {kind}")
    ensure_offline_gift_tables()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO vk_offline_gift_timers (vk_id, kind, event_id, due_at, created_at)
                VALUES (
                    %s, %s, %s,
                    now() + (%s || ' seconds')::interval,
                    now()
                )
                ON CONFLICT (vk_id) DO UPDATE
                SET kind = EXCLUDED.kind,
                    event_id = EXCLUDED.event_id,
                    due_at = EXCLUDED.due_at,
                    created_at = now()
                """,
                (
                    int(vk_id),
                    kind,
                    int(event_id) if event_id is not None else None,
                    str(float(delay_sec)),
                ),
            )
        conn.commit()


def clear_offline_gift_timer(vk_id: int) -> None:
    if not _use_postgres():
        return
    ensure_offline_gift_tables()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM vk_offline_gift_timers WHERE vk_id = %s",
                (int(vk_id),),
            )
        conn.commit()


def pop_due_offline_gift_timers(limit: int = 50) -> list[dict]:
    """Atomically take due timers (survives restarts; no double-fire)."""
    if not _use_postgres():
        return []
    ensure_offline_gift_tables()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM vk_offline_gift_timers t
                WHERE t.vk_id IN (
                    SELECT vk_id
                    FROM vk_offline_gift_timers
                    WHERE due_at <= now()
                    ORDER BY due_at ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING vk_id, kind, event_id, due_at
                """,
                (int(limit),),
            )
            rows = cur.fetchall() or []
        conn.commit()
    return [
        {
            "vk_id": int(row[0]),
            "kind": row[1],
            "event_id": int(row[2]) if row[2] is not None else None,
            "due_at": row[3],
        }
        for row in rows
    ]


def set_offline_gift_pending(*, vk_id: int, event_id: int) -> None:
    if not _use_postgres():
        return
    ensure_offline_gift_tables()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO vk_offline_gift_pending (vk_id, event_id, created_at)
                VALUES (%s, %s, now())
                ON CONFLICT (vk_id) DO UPDATE
                SET event_id = EXCLUDED.event_id,
                    created_at = now()
                """,
                (int(vk_id), int(event_id)),
            )
        conn.commit()


def pop_offline_gift_pending(vk_id: int) -> int | None:
    """Returns pending event_id and clears the row, or None."""
    if not _use_postgres():
        return None
    ensure_offline_gift_tables()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM vk_offline_gift_pending
                WHERE vk_id = %s
                RETURNING event_id
                """,
                (int(vk_id),),
            )
            row = cur.fetchone()
        conn.commit()
    return int(row[0]) if row else None


def clear_offline_gift_pending(vk_id: int) -> None:
    if not _use_postgres():
        return
    ensure_offline_gift_tables()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM vk_offline_gift_pending WHERE vk_id = %s",
                (int(vk_id),),
            )
        conn.commit()


def _offline_event_row(row) -> dict:
    return {
        "id": row[0],
        "format": row[1],
        "date": row[2],
        "weekday": row[3] or "",
        "time": row[4],
        "location": row[5] or "",
        "address": row[6] or "",
        "entries_count": int(row[7] or 0) if len(row) > 7 else 0,
        "winner_name": row[8] if len(row) > 8 else None,
    }


def get_offline_gift_dates(limit: int = 14) -> list[dict]:
    if not _use_postgres():
        return []
    ensure_offline_gift_tables()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    to_char(e.event_date, 'DD.MM.YYYY') AS event_date,
                    COALESCE(min(e.weekday), '') AS weekday,
                    count(*) AS events_count
                FROM events e
                WHERE e.status = 'active'
                  AND e.event_date >= (now() AT TIME ZONE 'Europe/Moscow')::date
                  AND e.format IN ('proverka', 'best', 'hitloto')
                GROUP BY e.event_date
                ORDER BY e.event_date
                LIMIT %s
                """,
                (int(limit),),
            )
            return [
                {"date": row[0], "weekday": row[1] or "", "events_count": int(row[2] or 0)}
                for row in cur.fetchall()
            ]


def get_offline_gift_events_for_date(event_date: str) -> list[dict]:
    if not _use_postgres():
        return []
    ensure_offline_gift_tables()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    e.id,
                    e.format,
                    to_char(e.event_date, 'DD.MM.YYYY') AS event_date,
                    e.weekday,
                    to_char(e.event_time, 'HH24:MI') AS event_time,
                    e.location,
                    e.address,
                    count(g.id) AS entries_count,
                    max(CASE WHEN g.is_winner THEN COALESCE(g.full_name, 'VK ' || g.vk_id::text) END)
                        AS winner_name
                FROM events e
                LEFT JOIN vk_offline_gift_entries g ON g.event_id = e.id
                WHERE e.status = 'active'
                  AND e.event_date = %s
                  AND e.format IN ('proverka', 'best', 'hitloto')
                GROUP BY e.id
                ORDER BY e.event_time, e.location, e.format
                """,
                (_parse_event_date(event_date),),
            )
            return [_offline_event_row(row) for row in cur.fetchall()]


def get_offline_gift_today_events() -> list[dict]:
    if not _use_postgres():
        return []
    ensure_offline_gift_tables()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    e.id,
                    e.format,
                    to_char(e.event_date, 'DD.MM.YYYY') AS event_date,
                    e.weekday,
                    to_char(e.event_time, 'HH24:MI') AS event_time,
                    e.location,
                    e.address,
                    count(g.id) AS entries_count,
                    max(CASE WHEN g.is_winner THEN COALESCE(g.full_name, 'VK ' || g.vk_id::text) END)
                        AS winner_name
                FROM events e
                LEFT JOIN vk_offline_gift_entries g ON g.event_id = e.id
                WHERE e.status = 'active'
                  AND e.event_date = (now() AT TIME ZONE 'Europe/Moscow')::date
                  AND e.format IN ('proverka', 'best', 'hitloto')
                GROUP BY e.id
                ORDER BY e.event_time, e.location, e.format
                """
            )
            return [_offline_event_row(row) for row in cur.fetchall()]


def get_offline_gift_event(event_id: int) -> dict | None:
    if not _use_postgres():
        return None
    ensure_offline_gift_tables()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    e.id,
                    e.format,
                    to_char(e.event_date, 'DD.MM.YYYY') AS event_date,
                    e.weekday,
                    to_char(e.event_time, 'HH24:MI') AS event_time,
                    e.location,
                    e.address,
                    count(g.id) AS entries_count,
                    max(CASE WHEN g.is_winner THEN COALESCE(g.full_name, 'VK ' || g.vk_id::text) END)
                        AS winner_name
                FROM events e
                LEFT JOIN vk_offline_gift_entries g ON g.event_id = e.id
                WHERE e.id = %s
                  AND e.status = 'active'
                  AND e.format IN ('proverka', 'best', 'hitloto')
                GROUP BY e.id
                """,
                (int(event_id),),
            )
            row = cur.fetchone()
            return _offline_event_row(row) if row else None


def has_offline_gift_entry(*, vk_id: int, event_id: int | None = None) -> bool:
    """True if VK user already in offline-gift list (optionally for one event)."""
    if not _use_postgres() or not vk_id:
        return False
    ensure_offline_gift_tables()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            if event_id is not None:
                cur.execute(
                    """
                    SELECT 1 FROM vk_offline_gift_entries
                    WHERE vk_id = %s AND event_id = %s
                    LIMIT 1
                    """,
                    (int(vk_id), int(event_id)),
                )
            else:
                cur.execute(
                    """
                    SELECT 1 FROM vk_offline_gift_entries
                    WHERE vk_id = %s
                    LIMIT 1
                    """,
                    (int(vk_id),),
                )
            return cur.fetchone() is not None


def record_offline_gift_entry(*, event_id: int, vk_id: int, full_name: str = "") -> dict | None:
    """Добавить в список выбранного шоу; из остальных списков участника убрать."""
    if not _use_postgres():
        return None
    ensure_offline_gift_tables()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            event = get_offline_gift_event(int(event_id))
            if not event:
                return None
            user_id = _upsert_user(
                cur,
                None,
                None,
                full_name or None,
                None,
                vk_id=int(vk_id),
                source="vkontakte",
            )
            # Один человек — только в последнем выбранном шоу
            cur.execute(
                """
                DELETE FROM vk_offline_gift_entries
                WHERE vk_id = %s AND event_id <> %s
                """,
                (int(vk_id), int(event_id)),
            )
            cur.execute(
                """
                INSERT INTO vk_offline_gift_entries (
                    event_id, user_id, vk_id, full_name, subscribed_checked_at, created_at
                )
                VALUES (%s, %s, %s, %s, now(), now())
                ON CONFLICT (event_id, vk_id)
                DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    full_name = COALESCE(EXCLUDED.full_name, vk_offline_gift_entries.full_name),
                    subscribed_checked_at = now()
                RETURNING id, (xmax = 0) AS inserted
                """,
                (int(event_id), user_id, int(vk_id), full_name or None),
            )
            row = cur.fetchone()
        conn.commit()
    event = get_offline_gift_event(int(event_id)) or {}
    return {
        "entry_id": row[0] if row else None,
        "inserted": bool(row[1]) if row else False,
        "event": event,
    }


def get_offline_gift_entries(event_id: int) -> list[dict]:
    if not _use_postgres():
        return []
    ensure_offline_gift_tables()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    vk_id,
                    COALESCE(full_name, 'VK ' || vk_id::text) AS full_name,
                    is_winner,
                    to_char(created_at AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI') AS created_label
                FROM vk_offline_gift_entries
                WHERE event_id = %s
                ORDER BY created_at, id
                """,
                (int(event_id),),
            )
            return [
                {
                    "id": row[0],
                    "vk_id": row[1],
                    "full_name": row[2],
                    "is_winner": bool(row[3]),
                    "created_label": row[4],
                }
                for row in cur.fetchall()
            ]


def remove_offline_gift_entries_for_vk(vk_id: int) -> int:
    """Убрать участника из всех чек-листов (отписка от сообщества)."""
    if not _use_postgres():
        return 0
    ensure_offline_gift_tables()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM vk_offline_gift_entries WHERE vk_id = %s",
                (int(vk_id),),
            )
            deleted = cur.rowcount or 0
            cur.execute(
                "DELETE FROM vk_offline_gift_pending WHERE vk_id = %s",
                (int(vk_id),),
            )
        conn.commit()
    return int(deleted)


def draw_offline_gift_winner(event_id: int, *, redraw: bool = False) -> dict | None:
    if not _use_postgres():
        return None
    ensure_offline_gift_tables()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            prev_winner_id = None
            if not redraw:
                cur.execute(
                    """
                    SELECT id, vk_id, COALESCE(full_name, 'VK ' || vk_id::text), is_winner
                    FROM vk_offline_gift_entries
                    WHERE event_id = %s AND is_winner = true
                    LIMIT 1
                    """,
                    (int(event_id),),
                )
                existing = cur.fetchone()
                if existing:
                    return {
                        "id": existing[0],
                        "vk_id": existing[1],
                        "full_name": existing[2],
                        "is_winner": bool(existing[3]),
                        "already_had_winner": True,
                    }
            else:
                cur.execute(
                    """
                    SELECT id
                    FROM vk_offline_gift_entries
                    WHERE event_id = %s AND is_winner = true
                    LIMIT 1
                    """,
                    (int(event_id),),
                )
                prev = cur.fetchone()
                if prev:
                    prev_winner_id = int(prev[0])
            cur.execute(
                "UPDATE vk_offline_gift_entries SET is_winner = false WHERE event_id = %s",
                (int(event_id),),
            )
            # Перевыбор: если участников > 1, стараемся взять другого
            if redraw and prev_winner_id is not None:
                cur.execute(
                    """
                    SELECT id
                    FROM vk_offline_gift_entries
                    WHERE event_id = %s AND id <> %s
                    ORDER BY random()
                    LIMIT 1
                    """,
                    (int(event_id), prev_winner_id),
                )
                picked = cur.fetchone()
                if not picked:
                    cur.execute(
                        """
                        SELECT id
                        FROM vk_offline_gift_entries
                        WHERE event_id = %s
                        ORDER BY random()
                        LIMIT 1
                        """,
                        (int(event_id),),
                    )
                    picked = cur.fetchone()
            else:
                cur.execute(
                    """
                    SELECT id
                    FROM vk_offline_gift_entries
                    WHERE event_id = %s
                    ORDER BY random()
                    LIMIT 1
                    """,
                    (int(event_id),),
                )
                picked = cur.fetchone()
            if not picked:
                conn.commit()
                return None
            cur.execute(
                """
                UPDATE vk_offline_gift_entries
                SET is_winner = true
                WHERE id = %s
                RETURNING id, vk_id, COALESCE(full_name, 'VK ' || vk_id::text), is_winner
                """,
                (picked[0],),
            )
            row = cur.fetchone()
        conn.commit()
    return {
        "id": row[0],
        "vk_id": row[1],
        "full_name": row[2],
        "is_winner": bool(row[3]),
        "already_had_winner": False,
    }


def save_raffle_moderation_message(
    submission_id,
    chat_id,
    message_id,
    *,
    photo_file_id: str | None = None,
):
    if not _use_postgres():
        return
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            if photo_file_id:
                cur.execute(
                    """
                    UPDATE raffle_submissions
                    SET moderation_chat_id = %s,
                        moderation_message_id = %s,
                        photo_file_id = %s
                    WHERE id = %s
                    """,
                    (chat_id, message_id, photo_file_id, submission_id),
                )
            else:
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
                       moderation_chat_id, moderation_message_id, reject_reason, vk_id
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
                       moderation_chat_id, moderation_message_id, reject_reason, vk_id
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


def set_raffle_vk_awaiting_screenshot(vk_id: int, kind: str):
    """Ждём скрин от VK-пользователя (TG-модерация и VK-бот — разные процессы)."""
    if not _use_postgres() or kind not in {"post", "review"}:
        return
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO raffle_vk_awaiting (vk_id, awaiting_kind, awaiting_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (vk_id) DO UPDATE SET
                    awaiting_kind = EXCLUDED.awaiting_kind,
                    awaiting_at = EXCLUDED.awaiting_at
                """,
                (int(vk_id), kind, datetime.now()),
            )
        conn.commit()


def get_raffle_vk_awaiting_screenshot(vk_id: int):
    if not _use_postgres():
        return None
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT awaiting_kind FROM raffle_vk_awaiting WHERE vk_id = %s",
                (int(vk_id),),
            )
            row = _fetchone_tuple(cur)
            if not row or not row[0]:
                return None
            kind = row[0]
            return kind if kind in {"post", "review"} else None


def clear_raffle_vk_awaiting_screenshot(vk_id: int):
    if not _use_postgres():
        return
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM raffle_vk_awaiting WHERE vk_id = %s",
                (int(vk_id),),
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


def get_manager_stata_dates(limit: int = 16, *, event_format: str = "proverka") -> list[str]:
    """Ближайшие даты с активными шоу нужного формата (кнопки менеджера)."""
    if not _use_postgres():
        return []
    fmt = (event_format or "proverka").strip().lower() or "proverka"
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT to_char(e.event_date, 'DD.MM.YYYY') AS event_date
                FROM events e
                WHERE e.status = 'active'
                  AND e.format = %s
                  AND e.event_date >= (now() AT TIME ZONE 'Europe/Moscow')::date
                GROUP BY e.event_date
                ORDER BY e.event_date
                LIMIT %s
                """,
                (fmt, int(limit)),
            )
            return [row[0] for row in cur.fetchall() if row and row[0]]


def get_manager_stata_bookings_for_date(
    event_date: str,
    *,
    booking_format: str = "proverka",
    event_format: str = "proverka",
    statuses: tuple[str, ...] | list[str] | None = None,
) -> list[dict]:
    """Брони на дату по шоу.

    По умолчанию только confirmed (как new_stata).
    Для new_stata_all передайте statuses=('booked', 'confirmed').
    """
    if not _use_postgres():
        return []
    try:
        parsed = _parse_event_date(event_date)
    except (TypeError, ValueError):
        return []
    b_fmt = (booking_format or "proverka").strip().lower() or "proverka"
    e_fmt = (event_format or "proverka").strip().lower() or "proverka"
    status_list = [str(s).strip().lower() for s in (statuses or ("confirmed",)) if str(s).strip()]
    if not status_list:
        status_list = ["confirmed"]
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    e.id AS event_id,
                    to_char(e.event_time, 'HH24:MI') AS event_time,
                    COALESCE(e.location, '') AS location,
                    COALESCE(e.address, '') AS address,
                    COALESCE(u.name, '') AS name,
                    COALESCE(u.phone, '') AS phone,
                    b.guests,
                    b.status,
                    b.id AS booking_id
                FROM bookings b
                JOIN users u ON u.id = b.user_id
                JOIN events e ON e.id = b.event_id
                WHERE e.event_date = %s
                  AND e.format = %s
                  AND b.format = %s
                  AND b.status = ANY(%s)
                ORDER BY e.event_time, e.location,
                         CASE b.status WHEN 'confirmed' THEN 0 WHEN 'booked' THEN 1 ELSE 2 END,
                         b.id
                """,
                (parsed, e_fmt, b_fmt, status_list),
            )
            rows = cur.fetchall()
    return [
        {
            "event_id": int(row[0]),
            "event_time": row[1] or "",
            "location": row[2] or "",
            "address": row[3] or "",
            "name": row[4] or "",
            "phone": row[5] or "",
            "guests": int(row[6] or 0),
            "status": row[7] or "",
            "booking_id": int(row[8]),
        }
        for row in rows
    ]


def anonymize_user(user_id: int) -> dict:
    """Обезличить гостя по запросу: стереть ПДн, брони оставить без контактов.

    Hard-delete users запрещён (bookings ON DELETE RESTRICT). Возвращает сводку
    для аудита; в details не кладём исходные имя/телефон.
    """
    result = {
        "ok": False,
        "already": False,
        "user_id": int(user_id) if user_id else 0,
        "had_telegram": False,
        "had_vk": False,
        "had_phone": False,
        "bookings_cancelled": 0,
        "raffle_scrubbed": 0,
        "help_scrubbed": 0,
        "gift_scrubbed": 0,
        "analytics_scrubbed": 0,
        "error": "",
    }
    if not _use_postgres():
        result["error"] = "postgres_only"
        return result
    if not user_id:
        result["error"] = "user_id_required"
        return result

    uid = int(user_id)
    ensure_pdn_consent_columns()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, telegram_id, vk_id, username, name, phone
                FROM users
                WHERE id = %s
                FOR UPDATE
                """,
                (uid,),
            )
            row = cur.fetchone()
            if not row:
                result["error"] = "not_found"
                return result

            _id, telegram_id, vk_id, _username, name, phone = row
            phone_s = (phone or "").strip()
            already = (
                telegram_id is None
                and vk_id is None
                and phone_s.startswith("deleted-")
                and (name or "").strip() == "Удалён"
            )
            if already:
                result["ok"] = True
                result["already"] = True
                return result

            result["had_telegram"] = telegram_id is not None
            result["had_vk"] = vk_id is not None
            result["had_phone"] = bool(phone_s)
            # Сентинел для CHECK (нужен хотя бы один из telegram_id / vk_id / phone)
            # и для CHECK в raffle_submissions / help_requests.
            sentinel_id = -uid

            cur.execute(
                """
                UPDATE bookings
                SET status = 'cancelled',
                    cancelled_at = COALESCE(cancelled_at, now()),
                    updated_at = now()
                WHERE user_id = %s
                  AND status IN ('booked', 'confirmed')
                """,
                (uid,),
            )
            result["bookings_cancelled"] = cur.rowcount or 0

            if telegram_id is not None:
                cur.execute("DELETE FROM raffle_nav WHERE telegram_id = %s", (telegram_id,))
            if vk_id is not None:
                cur.execute("DELETE FROM raffle_vk_awaiting WHERE vk_id = %s", (vk_id,))
                cur.execute(
                    "DELETE FROM vk_offline_gift_pending WHERE vk_id = %s",
                    (vk_id,),
                )

            cur.execute(
                """
                UPDATE raffle_submissions
                SET user_id = NULL,
                    telegram_id = CASE
                        WHEN telegram_id IS NOT NULL THEN %s
                        WHEN vk_id IS NOT NULL THEN NULL
                        ELSE %s
                    END,
                    vk_id = CASE
                        WHEN vk_id IS NOT NULL AND telegram_id IS NULL THEN %s
                        ELSE NULL
                    END,
                    username = NULL,
                    full_name = 'Удалён',
                    photo_file_id = 'deleted'
                WHERE user_id = %s
                   OR (%s::bigint IS NOT NULL AND telegram_id = %s)
                   OR (%s::bigint IS NOT NULL AND vk_id = %s)
                """,
                (
                    sentinel_id,
                    sentinel_id,
                    sentinel_id,
                    uid,
                    telegram_id,
                    telegram_id,
                    vk_id,
                    vk_id,
                ),
            )
            result["raffle_scrubbed"] = cur.rowcount or 0

            cur.execute(
                """
                UPDATE help_requests
                SET telegram_id = CASE
                        WHEN telegram_id IS NOT NULL THEN %s
                        WHEN vk_id IS NOT NULL THEN NULL
                        ELSE %s
                    END,
                    vk_id = CASE
                        WHEN vk_id IS NOT NULL AND telegram_id IS NULL THEN %s
                        ELSE NULL
                    END,
                    username = NULL,
                    full_name = 'Удалён',
                    question_text = '[удалено]'
                WHERE (%s::bigint IS NOT NULL AND telegram_id = %s)
                   OR (%s::bigint IS NOT NULL AND vk_id = %s)
                """,
                (
                    sentinel_id,
                    sentinel_id,
                    sentinel_id,
                    telegram_id,
                    telegram_id,
                    vk_id,
                    vk_id,
                ),
            )
            result["help_scrubbed"] = cur.rowcount or 0

            cur.execute(
                """
                UPDATE vk_offline_gift_entries
                SET user_id = NULL,
                    vk_id = %s,
                    full_name = 'Удалён'
                WHERE user_id = %s
                   OR (%s::bigint IS NOT NULL AND vk_id = %s)
                """,
                (sentinel_id, uid, vk_id, vk_id),
            )
            result["gift_scrubbed"] = cur.rowcount or 0

            cur.execute(
                """
                UPDATE analytics_events
                SET user_id = NULL,
                    telegram_id = NULL,
                    vk_id = NULL,
                    props = '{}'::jsonb
                WHERE user_id = %s
                   OR (%s::bigint IS NOT NULL AND telegram_id = %s)
                   OR (%s::bigint IS NOT NULL AND vk_id = %s)
                """,
                (uid, telegram_id, telegram_id, vk_id, vk_id),
            )
            result["analytics_scrubbed"] = cur.rowcount or 0

            cur.execute(
                """
                UPDATE users
                SET telegram_id = NULL,
                    vk_id = NULL,
                    username = NULL,
                    name = 'Удалён',
                    phone = %s,
                    rozygrysh_used = false,
                    is_blocked = true,
                    blocked_at = COALESCE(blocked_at, now()),
                    consent_accepted_at = NULL,
                    consent_version = NULL,
                    last_active_at = now()
                WHERE id = %s
                """,
                (f"deleted-{uid}", uid),
            )
            if cur.rowcount <= 0:
                result["error"] = "update_failed"
                conn.rollback()
                return result

        conn.commit()
    result["ok"] = True
    return result
