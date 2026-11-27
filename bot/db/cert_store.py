"""Черновики сертификата: номер, заявка на фото, ссылка на бронь."""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row

from bot.config import BOOKINGS_SOURCE, DATABASE_URL
from bot.utils.show_formats import BEST, CERTIFICATE, ELKA, HITLOTO

logger = logging.getLogger(__name__)

DRAFT_STATES = (
    "await_number",
    "await_photo",
    "pending_review",
    "need_name",
    "need_phone",
    "need_format",
    "need_date",
    "need_event",
)
SHOW_FORMATS = (HITLOTO, BEST, ELKA)
CERT_CAP = 2


def postgres_ready() -> bool:
    return BOOKINGS_SOURCE == "postgres" and bool(DATABASE_URL)


def ensure_certificate_tables() -> None:
    if not postgres_ready():
        return
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS certificate_sessions (
                    id BIGSERIAL PRIMARY KEY,
                    code TEXT NOT NULL DEFAULT '',
                    channel TEXT NOT NULL,
                    telegram_id BIGINT,
                    vk_id BIGINT,
                    peer_id BIGINT,
                    state TEXT NOT NULL,
                    claim_id BIGINT,
                    request_ref TEXT,
                    photo_ref TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    booking_id BIGINT,
                    booking_ref TEXT,
                    replace_booking_id BIGINT,
                    event_id BIGINT,
                    event_format TEXT,
                    name TEXT,
                    phone TEXT,
                    registry_confirmed BOOLEAN NOT NULL DEFAULT false,
                    confirm_by TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_certificate_sessions_tg
                ON certificate_sessions (telegram_id, id DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_certificate_sessions_vk
                ON certificate_sessions (vk_id, id DESC)
                """
            )
        conn.commit()


def _connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def close_drafts(*, telegram_id: int | None = None, vk_id: int | None = None) -> None:
    if not postgres_ready():
        return
    with _connect() as conn:
        with conn.cursor() as cur:
            if telegram_id is not None:
                cur.execute(
                    """
                    UPDATE certificate_sessions
                    SET state = 'closed', updated_at = now()
                    WHERE telegram_id = %s AND state = ANY(%s)
                    """,
                    (int(telegram_id), list(DRAFT_STATES)),
                )
            if vk_id is not None:
                cur.execute(
                    """
                    UPDATE certificate_sessions
                    SET state = 'closed', updated_at = now()
                    WHERE vk_id = %s AND state = ANY(%s)
                    """,
                    (int(vk_id), list(DRAFT_STATES)),
                )
        conn.commit()


def insert_session(**fields: Any) -> dict[str, Any]:
    columns = list(fields)
    values = [fields[name] for name in columns]
    placeholders = ", ".join(["%s"] * len(columns))
    names = ", ".join(columns)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO certificate_sessions ({names})
                VALUES ({placeholders})
                RETURNING *
                """,
                values,
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row)


def update_session(session_id: int, **fields: Any) -> dict[str, Any] | None:
    if not fields:
        return get_session(session_id)
    assignments = ", ".join(f"{name} = %s" for name in fields)
    values = list(fields.values()) + [int(session_id)]
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE certificate_sessions
                SET {assignments}, updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                values,
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


def get_session(session_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM certificate_sessions WHERE id = %s", (int(session_id),))
            row = cur.fetchone()
    return dict(row) if row else None


def code_already_used(code: str, *, except_session_id: int | None = None) -> bool:
    """Номер уже списан или висит в активной брони — другой гость его не берёт."""
    if not postgres_ready() or not (code or "").strip():
        return False
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM certificate_sessions
                WHERE code = %s
                  AND (registry_confirmed OR state = 'booked')
                  AND (%s::bigint IS NULL OR id <> %s)
                LIMIT 1
                """,
                (code, except_session_id, except_session_id),
            )
            return cur.fetchone() is not None


def list_sessions_for_user(
    *,
    telegram_id: int | None = None,
    vk_id: int | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    if not postgres_ready():
        return []
    if telegram_id is None and vk_id is None:
        return []
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id, code, channel, telegram_id, vk_id, state, photo_ref,
                    claim_id, booking_id, registry_confirmed, created_at, updated_at
                FROM certificate_sessions
                WHERE (%s::bigint IS NOT NULL AND telegram_id = %s)
                   OR (%s::bigint IS NOT NULL AND vk_id = %s)
                ORDER BY id DESC
                LIMIT %s
                """,
                (
                    telegram_id,
                    telegram_id,
                    vk_id,
                    vk_id,
                    int(limit),
                ),
            )
            return [dict(row) for row in cur.fetchall()]


def get_draft(*, telegram_id: int | None = None, vk_id: int | None = None) -> dict[str, Any] | None:
    if not postgres_ready():
        return None
    with _connect() as conn:
        with conn.cursor() as cur:
            if vk_id is not None:
                cur.execute(
                    """
                    SELECT * FROM certificate_sessions
                    WHERE vk_id = %s AND state = ANY(%s)
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (int(vk_id), list(DRAFT_STATES)),
                )
            elif telegram_id is not None:
                cur.execute(
                    """
                    SELECT * FROM certificate_sessions
                    WHERE telegram_id = %s AND state = ANY(%s)
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (int(telegram_id), list(DRAFT_STATES)),
                )
            else:
                return None
            row = cur.fetchone()
    return dict(row) if row else None


def get_session_by_booking(booking_id: int) -> dict[str, Any] | None:
    if not postgres_ready():
        return None
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM certificate_sessions
                WHERE booking_id = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(booking_id),),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def booking_context(booking_id: int) -> dict[str, Any] | None:
    if not postgres_ready():
        return None
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    b.id,
                    b.format,
                    b.status,
                    b.event_id,
                    b.guests,
                    u.telegram_id,
                    u.vk_id,
                    COALESCE(u.name, '') AS name,
                    COALESCE(u.phone, '') AS phone,
                    e.format AS event_format,
                    to_char(e.event_date, 'DD.MM.YYYY') AS event_date,
                    to_char(e.event_time, 'HH24:MI') AS event_time,
                    COALESCE(e.location, '') AS location,
                    COALESCE(e.address, '') AS address,
                    COALESCE(e.description, '') AS description
                FROM bookings b
                JOIN users u ON u.id = b.user_id
                JOIN events e ON e.id = b.event_id
                WHERE b.id = %s
                """,
                (int(booking_id),),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def active_on_event(*, event_id: int, telegram_id: int | None = None, vk_id: int | None = None) -> dict[str, Any] | None:
    if not postgres_ready():
        return None
    with _connect() as conn:
        with conn.cursor() as cur:
            if vk_id is not None:
                user_sql = "u.vk_id = %s"
                user_param = int(vk_id)
            else:
                user_sql = "u.telegram_id = %s"
                user_param = int(telegram_id or 0)
            cur.execute(
                f"""
                SELECT b.id, b.format, b.status
                FROM bookings b
                JOIN users u ON u.id = b.user_id
                WHERE {user_sql}
                  AND b.event_id = %s
                  AND b.status IN ('booked', 'confirmed')
                ORDER BY b.id DESC
                LIMIT 1
                """,
                (user_param, int(event_id)),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def certificate_booking_ids(event_id: int) -> list[int]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM bookings
                WHERE event_id = %s
                  AND format = %s
                  AND status IN ('booked', 'confirmed')
                ORDER BY id
                """,
                (int(event_id), CERTIFICATE),
            )
            return [int(row["id"]) for row in cur.fetchall()]
