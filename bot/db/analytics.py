"""Product analytics events for the admin funnel.

Never raises to callers: tracking must not break booking / UX flows.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

from bot.config import BOOKINGS_SOURCE, DATABASE_URL

logger = logging.getLogger(__name__)

# Stable event names used by handlers and (later) the analytics UI.
EVENT_BOT_START = "bot_start"
EVENT_HELP_OPEN = "help_open"
EVENT_HELP_QUESTION = "help_question"
EVENT_BRANCH_BEST = "branch_best"
EVENT_BRANCH_HITLOTO = "branch_hitloto"
EVENT_BRANCH_PROVERKA = "branch_proverka"
EVENT_SHOW_CARD = "show_card"
EVENT_BUY_CLICK = "buy_click"
EVENT_RAFFLE_ENTER = "raffle_enter"
EVENT_RAFFLE_BRANCH = "raffle_branch"
EVENT_RAFFLE_SCREENSHOT = "raffle_screenshot"
EVENT_RAFFLE_APPROVED = "raffle_approved"
EVENT_RAFFLE_REJECTED = "raffle_rejected"
EVENT_RAFFLE_SUBSCRIBED = "raffle_subscribed"
EVENT_RAFFLE_SUB_FAILED = "raffle_sub_failed"
EVENT_BOOKING_CREATED = "booking_created"
EVENT_BOOKING_CONFIRMED = "booking_confirmed"
EVENT_BOOKING_CANCELLED = "booking_cancelled"
EVENT_BOOKING_ANNULLED = "booking_annulled"
EVENT_BOOKING_START = "booking_start"

# Канон payload для таблицы «Входы в бот по ссылкам»:
# только старт / актуальные deeplink (дубли mini-app → одна строка).
_BOT_START_PAYLOAD_RAW = "LOWER(TRIM(COALESCE(props->>'payload', '')))"
BOT_START_PAYLOAD_CANON_SQL = f"""
CASE
  WHEN {_BOT_START_PAYLOAD_RAW} = '' THEN ''
  WHEN {_BOT_START_PAYLOAD_RAW} IN (
         'standup_rozygr', 'rozygrysh', 'raffle', 'розыгрыш'
       )
    OR {_BOT_START_PAYLOAD_RAW} LIKE 'standup_rozygr_c%%'
    OR {_BOT_START_PAYLOAD_RAW} LIKE 'raffle_c%%'
    THEN 'standup_rozygr'
  WHEN {_BOT_START_PAYLOAD_RAW} IN (
         'standup_book', 'booking', 'book', 'бронь', 'proverka'
       )
    OR {_BOT_START_PAYLOAD_RAW} LIKE 'standup_book_c%%'
    OR {_BOT_START_PAYLOAD_RAW} LIKE 'booking_c%%'
    THEN 'standup_book'
  WHEN {_BOT_START_PAYLOAD_RAW} IN (
         'offline_gift', 'gift', 'chek_list', 'check_list'
       )
    OR {_BOT_START_PAYLOAD_RAW} LIKE 'offline_gift_%%'
    OR {_BOT_START_PAYLOAD_RAW} LIKE 'gift_%%'
    OR {_BOT_START_PAYLOAD_RAW} LIKE 'chek_list_%%'
    OR {_BOT_START_PAYLOAD_RAW} LIKE 'check_list_%%'
    OR {_BOT_START_PAYLOAD_RAW} LIKE 'offline_gift_c%%'
    THEN 'offline_gift'
  WHEN {_BOT_START_PAYLOAD_RAW} = 'quick_booking' THEN 'quick_booking'
  WHEN {_BOT_START_PAYLOAD_RAW} = 'afisha_plat' THEN 'afisha_plat'
  ELSE NULL
END
""".strip()
EVENT_BOOKING_GUESTS_CHANGED = "booking_guests_changed"
EVENT_BOOKING_REMINDER_24H = "booking_reminder_24h"
EVENT_BOOKING_REMINDER_DAY = "booking_reminder_day"
EVENT_BOOKING_TICKET_SENT = "booking_ticket_sent"
EVENT_BROWSE_DATES = "browse_dates"
EVENT_BROWSE_VENUES = "browse_venues"
EVENT_BOT_BLOCKED = "bot_blocked"
EVENT_BOT_UNBLOCKED = "bot_unblocked"
# Menu / slash commands (Telegram command menu — 5 items)
EVENT_CMD_MY_BOOKINGS = "cmd_my_bookings"
EVENT_CMD_MAIN_MENU = "cmd_main_menu"
EVENT_CMD_BUY_TICKET = "cmd_buy_ticket"
EVENT_CMD_HELP = "cmd_help"
EVENT_CMD_CHANNEL = "cmd_channel"

COMMAND_EVENT_NAMES = (
    EVENT_CMD_MY_BOOKINGS,
    EVENT_CMD_MAIN_MENU,
    EVENT_CMD_BUY_TICKET,
    EVENT_CMD_HELP,
    EVENT_CMD_CHANNEL,
)


def _use_postgres() -> bool:
    return BOOKINGS_SOURCE == "postgres" and bool(DATABASE_URL)


def ensure_analytics_tables() -> None:
    """Create analytics_events + blocked flags on users (Postgres only)."""
    if not _use_postgres():
        return
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS analytics_events (
                    id BIGSERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'telegram'
                        CHECK (channel IN ('telegram', 'vkontakte', 'import', 'unknown')),
                    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    telegram_id BIGINT,
                    vk_id BIGINT,
                    event_id BIGINT REFERENCES events(id) ON DELETE SET NULL,
                    booking_id BIGINT REFERENCES bookings(id) ON DELETE SET NULL,
                    props JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_analytics_events_name_created
                    ON analytics_events (name, created_at DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_analytics_events_telegram_created
                    ON analytics_events (telegram_id, created_at DESC)
                    WHERE telegram_id IS NOT NULL
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_analytics_events_vk_created
                    ON analytics_events (vk_id, created_at DESC)
                    WHERE vk_id IS NOT NULL
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_analytics_events_channel_created
                    ON analytics_events (channel, created_at DESC)
                """
            )
            # Mailing audience helpers on users
            cur.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS is_blocked BOOLEAN NOT NULL DEFAULT false
                """
            )
            cur.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS blocked_at TIMESTAMPTZ
                """
            )
        conn.commit()


def _resolve_user_id(cur, telegram_id: Optional[int], vk_id: Optional[int]) -> Optional[int]:
    if telegram_id is not None:
        cur.execute("SELECT id FROM users WHERE telegram_id = %s", (telegram_id,))
        row = cur.fetchone()
        if row:
            return row[0]
    if vk_id is not None:
        cur.execute("SELECT id FROM users WHERE vk_id = %s", (vk_id,))
        row = cur.fetchone()
        if row:
            return row[0]
    return None


def track_event(
    name: str,
    *,
    telegram_id: Optional[int] = None,
    vk_id: Optional[int] = None,
    user_id: Optional[int] = None,
    channel: str = "telegram",
    event_id: Optional[int] = None,
    booking_id: Optional[int] = None,
    props: Optional[dict[str, Any]] = None,
) -> None:
    """Insert one analytics row. Safe to call from any handler."""
    if not name:
        return
    if not _use_postgres():
        return
    if channel not in ("telegram", "vkontakte", "import", "unknown"):
        channel = "unknown"
    payload = props or {}
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                resolved_user_id = user_id or _resolve_user_id(cur, telegram_id, vk_id)
                # Lightweight touch: ensure user exists for start/branch events
                if resolved_user_id is None and telegram_id is not None:
                    now = datetime.now()
                    cur.execute(
                        """
                        INSERT INTO users (telegram_id, source, created_at, last_active_at)
                        VALUES (%s, 'telegram', %s, %s)
                        ON CONFLICT (telegram_id)
                        DO UPDATE SET last_active_at = EXCLUDED.last_active_at
                        RETURNING id
                        """,
                        (telegram_id, now, now),
                    )
                    resolved_user_id = cur.fetchone()[0]
                if resolved_user_id is None and vk_id is not None:
                    now = datetime.now()
                    cur.execute(
                        """
                        INSERT INTO users (vk_id, source, created_at, last_active_at)
                        VALUES (%s, 'vkontakte', %s, %s)
                        ON CONFLICT (vk_id)
                        DO UPDATE SET last_active_at = EXCLUDED.last_active_at
                        RETURNING id
                        """,
                        (vk_id, now, now),
                    )
                    resolved_user_id = cur.fetchone()[0]
                cur.execute(
                    """
                    INSERT INTO analytics_events (
                        name, channel, user_id, telegram_id, vk_id,
                        event_id, booking_id, props, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    """,
                    (
                        name,
                        channel,
                        resolved_user_id,
                        telegram_id,
                        vk_id,
                        int(event_id) if event_id is not None else None,
                        int(booking_id) if booking_id is not None else None,
                        json.dumps(payload, ensure_ascii=False, default=str),
                        datetime.now(),
                    ),
                )
            conn.commit()
    except Exception:
        logger.exception("analytics track failed: %s", name)


def set_user_blocked(telegram_id: int, blocked: bool) -> None:
    """Update users.is_blocked from my_chat_member."""
    if not _use_postgres() or not telegram_id:
        return
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                now = datetime.now()
                cur.execute(
                    """
                    INSERT INTO users (telegram_id, source, created_at, last_active_at, is_blocked, blocked_at)
                    VALUES (%s, 'telegram', %s, %s, %s, %s)
                    ON CONFLICT (telegram_id)
                    DO UPDATE SET
                        is_blocked = EXCLUDED.is_blocked,
                        blocked_at = CASE
                            WHEN EXCLUDED.is_blocked THEN EXCLUDED.blocked_at
                            ELSE NULL
                        END,
                        last_active_at = EXCLUDED.last_active_at
                    """,
                    (
                        telegram_id,
                        now,
                        now,
                        blocked,
                        now if blocked else None,
                    ),
                )
            conn.commit()
    except Exception:
        logger.exception("set_user_blocked failed for %s", telegram_id)


def browse_mode_from_callback(back_callback: str = "") -> str:
    text = back_callback or ""
    if "venue" in text or "loc_carousel" in text:
        return "venue"
    return "date"


def _metric_map(rows) -> dict:
    result = {}
    for row in rows:
        result[row["name"]] = {
            "events": int(row["events"] or 0),
            "uniques": int(row["uniques"] or 0),
        }
    return result


# Count unique people across Telegram and VK (telegram_id-only misses VK).
_UNIQUE_PERSON_SQL = """
COUNT(DISTINCT COALESCE(
    user_id::text,
    CASE WHEN telegram_id IS NOT NULL THEN 'tg:' || telegram_id::text END,
    CASE WHEN vk_id IS NOT NULL THEN 'vk:' || vk_id::text END
))::int
""".strip()


def fetch_analytics_report(
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    channel: str = "",
) -> dict:
    """Aggregate funnel metrics for the admin analytics tab.

    date_from / date_to: YYYY-MM-DD in Moscow calendar, inclusive.
    Empty both = all time.
    """
    empty = {
        "available": False,
        "period_label": "",
        "channel": channel or "all",
        "by_name": {},
        "starts_by_payload": [],
        "show_cards": [],
        "raffle_branches": [],
        "raffle_kind_steps": {},
        "raffle_kind_bookings": {},
        "raffle_bookings": {},
        "proverka_bookings": {},
        "audience": {
            "telegram_users": 0,
            "telegram_blocked": 0,
            "telegram_mailable": 0,
            "vk_users": 0,
            "vk_blocked": 0,
            "vk_mailable": 0,
        },
    }
    if not _use_postgres():
        return empty

    from datetime import time, timedelta, timezone

    msk = timezone(timedelta(hours=3))
    where = ["1=1"]
    params: dict[str, Any] = {}
    period_label = "весь период"

    def _parse_day(value: str):
        value = (value or "").strip()
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            try:
                return datetime.strptime(value, "%d.%m.%Y").date()
            except ValueError:
                return None

    day_from = _parse_day(date_from or "")
    day_to = _parse_day(date_to or "")
    if day_from and not day_to:
        day_to = day_from
    if day_to and not day_from:
        day_from = day_to
    if day_from and day_to and day_to < day_from:
        day_from, day_to = day_to, day_from

    if day_from and day_to:
        start = datetime.combine(day_from, time.min, tzinfo=msk)
        end = datetime.combine(day_to + timedelta(days=1), time.min, tzinfo=msk)
        where.append("created_at >= %(start)s AND created_at < %(end)s")
        params["start"] = start
        params["end"] = end
        if day_from == day_to:
            period_label = day_from.strftime("%d.%m.%Y")
        else:
            period_label = f"{day_from.strftime('%d.%m.%Y')} — {day_to.strftime('%d.%m.%Y')}"

    if channel in ("telegram", "vkontakte"):
        where.append("channel = %(channel)s")
        params["channel"] = channel

    where_sql = " AND ".join(where)

    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        name,
                        COUNT(*)::int AS events,
                        {_UNIQUE_PERSON_SQL} AS uniques
                    FROM analytics_events
                    WHERE {where_sql}
                    GROUP BY name
                    """,
                    params,
                )
                by_name = _metric_map(cur.fetchall())

                cur.execute(
                    f"""
                    SELECT
                        payload,
                        COUNT(*)::int AS events,
                        {_UNIQUE_PERSON_SQL} AS uniques
                    FROM (
                        SELECT
                            {BOT_START_PAYLOAD_CANON_SQL} AS payload,
                            telegram_id,
                            vk_id
                        FROM analytics_events
                        WHERE {where_sql} AND name = 'bot_start'
                    ) starts
                    WHERE payload IS NOT NULL
                    GROUP BY payload
                    ORDER BY events DESC
                    """,
                    params,
                )
                starts_by_payload = [
                    {
                        "payload": row["payload"] or "",
                        "events": row["events"],
                        "uniques": row["uniques"],
                    }
                    for row in cur.fetchall()
                ]

                cur.execute(
                    f"""
                    SELECT
                        COALESCE(props->>'format', 'unknown') AS format,
                        COALESCE(props->>'browse', '') AS browse,
                        COUNT(*)::int AS events,
                        {_UNIQUE_PERSON_SQL} AS uniques
                    FROM analytics_events
                    WHERE {where_sql} AND name = 'show_card'
                    GROUP BY 1, 2
                    ORDER BY format, browse
                    """,
                    params,
                )
                show_cards = [dict(row) for row in cur.fetchall()]

                cur.execute(
                    f"""
                    SELECT
                        COALESCE(props->>'kind', 'unknown') AS kind,
                        COUNT(*)::int AS events,
                        {_UNIQUE_PERSON_SQL} AS uniques
                    FROM analytics_events
                    WHERE {where_sql} AND name = 'raffle_branch'
                    GROUP BY 1
                    ORDER BY kind
                    """,
                    params,
                )
                raffle_branches = [dict(row) for row in cur.fetchall()]

                cur.execute(
                    f"""
                    SELECT
                        COALESCE(props->>'kind', 'unknown') AS kind,
                        name,
                        COUNT(*)::int AS events,
                        {_UNIQUE_PERSON_SQL} AS uniques
                    FROM analytics_events
                    WHERE {where_sql}
                      AND name IN (
                        'raffle_branch',
                        'raffle_screenshot',
                        'raffle_approved',
                        'raffle_rejected'
                      )
                    GROUP BY 1, 2
                    """,
                    params,
                )
                raffle_kind_steps: dict[str, dict] = {}
                for row in cur.fetchall():
                    kind = row["kind"] or "unknown"
                    bucket = raffle_kind_steps.setdefault(kind, {})
                    bucket[row["name"]] = {
                        "events": int(row["events"] or 0),
                        "uniques": int(row["uniques"] or 0),
                    }

                # Booking stages from bookings table by format,
                # so history and annulments are visible even before analytics existed.
                booking_params: dict[str, Any] = {}
                if day_from and day_to:
                    booking_params["start"] = params["start"]
                    booking_params["end"] = params["end"]
                if channel in ("telegram", "vkontakte"):
                    booking_params["channel"] = channel

                def _format_booking_metric(
                    booking_format: str,
                    time_column: str,
                    *,
                    source_mode: str = "organic",
                ) -> dict:
                    """source_mode: organic (tg/vk), import, or all."""
                    where = [f"b.format = '{booking_format}'"]
                    if "channel" in booking_params:
                        where.append("b.source = %(channel)s")
                    elif source_mode == "organic":
                        # Импорт из Sheets/Salebot не должен ломать воронку «от входа в бот».
                        where.append("b.source IN ('telegram', 'vkontakte')")
                    elif source_mode == "import":
                        where.append("b.source = 'import'")
                    where_sql = " AND ".join(where)
                    time_filter = ""
                    if "start" in booking_params and "end" in booking_params:
                        time_filter = f" AND b.{time_column} >= %(start)s AND b.{time_column} < %(end)s"
                    cur.execute(
                        f"""
                        SELECT
                            COUNT(*)::int AS events,
                            COUNT(DISTINCT b.user_id)::int AS uniques
                        FROM bookings b
                        WHERE {where_sql}
                          AND b.{time_column} IS NOT NULL
                          {time_filter}
                        """,
                        booking_params,
                    )
                    row = cur.fetchone() or {}
                    return {
                        "events": int(row.get("events") or 0),
                        "uniques": int(row.get("uniques") or 0),
                    }

                def _format_bookings(booking_format: str, *, source_mode: str = "organic") -> dict:
                    return {
                        "created": _format_booking_metric(
                            booking_format, "created_at", source_mode=source_mode
                        ),
                        "confirmed": _format_booking_metric(
                            booking_format, "confirmed_at", source_mode=source_mode
                        ),
                        "cancelled": _format_booking_metric(
                            booking_format, "cancelled_at", source_mode=source_mode
                        ),
                        "annulled": _format_booking_metric(
                            booking_format, "annulled_at", source_mode=source_mode
                        ),
                    }

                raffle_bookings = _format_bookings("rozygrysh")
                proverka_bookings = _format_bookings("proverka")
                # Отдельно: заливка из таблиц/Salebot (не шаги воронки бота).
                if "channel" not in booking_params:
                    proverka_bookings["imported"] = _format_booking_metric(
                        "proverka", "created_at", source_mode="import"
                    )
                    raffle_bookings["imported"] = _format_booking_metric(
                        "rozygrysh", "created_at", source_mode="import"
                    )
                else:
                    proverka_bookings["imported"] = {"events": 0, "uniques": 0}
                    raffle_bookings["imported"] = {"events": 0, "uniques": 0}

                # Воронка бота: только analytics_events (импорт/заливка сюда не пишет).
                # Импорт часто ставит source=telegram/vkontakte — таблица bookings врёт.
                def _analytics_format_metric(event_name: str, booking_format: str) -> dict:
                    cur.execute(
                        f"""
                        SELECT
                            COUNT(*)::int AS events,
                            {_UNIQUE_PERSON_SQL} AS uniques,
                            COUNT(DISTINCT booking_id)::int AS bookings,
                            COUNT(DISTINCT booking_id) FILTER (
                                WHERE booking_id IS NOT NULL
                                  AND EXISTS (
                                      SELECT 1
                                      FROM bookings b
                                      WHERE b.id = analytics_events.booking_id
                                        AND b.status IN ('booked', 'confirmed')
                                  )
                            )::int AS active_bookings
                        FROM analytics_events
                        WHERE {where_sql}
                          AND name = %(event_name)s
                          AND props->>'format' = %(booking_format)s
                        """,
                        {
                            **params,
                            "event_name": event_name,
                            "booking_format": booking_format,
                        },
                    )
                    row = cur.fetchone() or {}
                    bookings = int(row.get("bookings") or 0)
                    active = int(row.get("active_bookings") or 0)
                    return {
                        "events": int(row.get("events") or 0),
                        "uniques": int(row.get("uniques") or 0),
                        "bookings": bookings,
                        "active_bookings": active,
                        # Для карточек/воронки бронирований показываем разные booking_id, не «клики».
                        "display": active if event_name == "booking_confirmed" else bookings,
                    }

                def _show_card_format_metric(booking_format: str) -> dict:
                    cur.execute(
                        f"""
                        SELECT
                            COUNT(*)::int AS events,
                            {_UNIQUE_PERSON_SQL} AS uniques
                        FROM analytics_events
                        WHERE {where_sql}
                          AND name = 'show_card'
                          AND props->>'format' = %(booking_format)s
                        """,
                        {**params, "booking_format": booking_format},
                    )
                    row = cur.fetchone() or {}
                    return {
                        "events": int(row.get("events") or 0),
                        "uniques": int(row.get("uniques") or 0),
                    }

                def _proverka_browsed_metric() -> dict:
                    """show_card proverka ∩ branch_proverka (same period/channel)."""
                    period_filter = ""
                    if "start" in params and "end" in params:
                        period_filter = (
                            " AND ae.created_at >= %(start)s AND ae.created_at < %(end)s"
                        )
                    channel_filter = ""
                    if "channel" in params:
                        channel_filter = " AND ae.channel = %(channel)s"
                    cur.execute(
                        f"""
                        WITH show_people AS (
                            SELECT DISTINCT {_PERSON_KEY_SQL} AS person_key
                            FROM analytics_events ae
                            WHERE ae.name = 'show_card'
                              AND ae.props->>'format' = 'proverka'
                              AND {_PERSON_KEY_SQL} IS NOT NULL
                              {period_filter}
                              {channel_filter}
                        ),
                        branch_people AS (
                            SELECT DISTINCT {_PERSON_KEY_SQL} AS person_key
                            FROM analytics_events ae
                            WHERE ae.name = 'branch_proverka'
                              AND {_PERSON_KEY_SQL} IS NOT NULL
                              {period_filter}
                              {channel_filter}
                        ),
                        matched AS (
                            SELECT s.person_key
                            FROM show_people s
                            INNER JOIN branch_people b ON s.person_key = b.person_key
                        )
                        SELECT
                            (SELECT COUNT(*)::int FROM matched) AS uniques,
                            (
                                SELECT COUNT(*)::int
                                FROM analytics_events ae
                                WHERE ae.name = 'show_card'
                                  AND ae.props->>'format' = 'proverka'
                                  AND {_PERSON_KEY_SQL} IN (SELECT person_key FROM matched)
                                  {period_filter}
                                  {channel_filter}
                            ) AS events
                        """,
                        params,
                    )
                    row = cur.fetchone() or {}
                    return {
                        "events": int(row.get("events") or 0),
                        "uniques": int(row.get("uniques") or 0),
                    }

                def _bookings_without_bot_event(booking_format: str) -> dict:
                    """Брони в БД за период без analytics booking_created (заливка и т.п.)."""
                    time_filter = ""
                    bp = dict(booking_params)
                    bp["booking_format"] = booking_format
                    if "start" in bp and "end" in bp:
                        time_filter = " AND b.created_at >= %(start)s AND b.created_at < %(end)s"
                    channel_filter = ""
                    if "channel" in bp:
                        channel_filter = " AND b.source = %(channel)s"
                    cur.execute(
                        f"""
                        SELECT
                            COUNT(*)::int AS events,
                            COUNT(DISTINCT b.user_id)::int AS uniques
                        FROM bookings b
                        WHERE b.format = %(booking_format)s
                          AND b.created_at IS NOT NULL
                          {time_filter}
                          {channel_filter}
                          AND NOT EXISTS (
                              SELECT 1
                              FROM analytics_events ae
                              WHERE ae.booking_id = b.id
                                AND ae.name = 'booking_created'
                          )
                        """,
                        bp,
                    )
                    row = cur.fetchone() or {}
                    return {
                        "events": int(row.get("events") or 0),
                        "uniques": int(row.get("uniques") or 0),
                    }

                proverka_bookings["from_bot"] = {
                    "entered": by_name.get("branch_proverka")
                    or {"events": 0, "uniques": 0},
                    "browsed": _proverka_browsed_metric(),
                    "created": _analytics_format_metric("booking_created", "proverka"),
                    "confirmed": _analytics_format_metric("booking_confirmed", "proverka"),
                    "cancelled": _analytics_format_metric("booking_cancelled", "proverka"),
                    "annulled": _analytics_format_metric("booking_annulled", "proverka"),
                }
                proverka_bookings["off_bot"] = _bookings_without_bot_event("proverka")
                raffle_bookings["from_bot"] = {
                    "created": _analytics_format_metric("booking_created", "rozygrysh"),
                    "confirmed": _analytics_format_metric("booking_confirmed", "rozygrysh"),
                    "cancelled": _analytics_format_metric("booking_cancelled", "rozygrysh"),
                    "annulled": _analytics_format_metric("booking_annulled", "rozygrysh"),
                }
                raffle_bookings["off_bot"] = _bookings_without_bot_event("rozygrysh")

                # Visited raffle: got a ticket and still have it (not cancelled/annulled).
                visited_time_filter = ""
                if "start" in booking_params and "end" in booking_params:
                    visited_time_filter = (
                        " AND b.confirmed_at >= %(start)s AND b.confirmed_at < %(end)s"
                    )
                visited_where = ["b.format = 'rozygrysh'", "b.status = 'confirmed'"]
                if "channel" in booking_params:
                    visited_where.append("b.source = %(channel)s")
                else:
                    visited_where.append("b.source IN ('telegram', 'vkontakte')")
                cur.execute(
                    f"""
                    SELECT
                        COUNT(*)::int AS events,
                        COUNT(DISTINCT b.user_id)::int AS uniques
                    FROM bookings b
                    WHERE {" AND ".join(visited_where)}
                      AND b.confirmed_at IS NOT NULL
                      {visited_time_filter}
                    """,
                    booking_params,
                )
                visited_row = cur.fetchone() or {}
                raffle_bookings["visited"] = {
                    "events": int(visited_row.get("events") or 0),
                    "uniques": int(visited_row.get("uniques") or 0),
                }

                # Per-branch booking/ticket: attribute via latest approved submission
                # of that user before the booking was created.
                booking_where = ["b.format = 'rozygrysh'"]
                if "channel" in booking_params:
                    booking_where.append("b.source = %(channel)s")
                else:
                    booking_where.append("b.source IN ('telegram', 'vkontakte')")
                booking_where_sql = " AND ".join(booking_where)

                def _raffle_kind_booking_metric(time_column: str) -> dict[str, dict]:
                    time_filter = ""
                    if "start" in booking_params and "end" in booking_params:
                        time_filter = (
                            f" AND b.{time_column} >= %(start)s AND b.{time_column} < %(end)s"
                        )
                    cur.execute(
                        f"""
                        SELECT
                            attributed.kind,
                            COUNT(*)::int AS events,
                            COUNT(DISTINCT b.user_id)::int AS uniques
                        FROM bookings b
                        JOIN users u ON u.id = b.user_id
                        JOIN LATERAL (
                            SELECT rs.kind
                            FROM raffle_submissions rs
                            WHERE rs.telegram_id = u.telegram_id
                              AND rs.status = 'approved'
                              AND COALESCE(rs.reviewed_at, rs.created_at) <= b.created_at
                            ORDER BY COALESCE(rs.reviewed_at, rs.created_at) DESC, rs.id DESC
                            LIMIT 1
                        ) attributed ON true
                        WHERE {booking_where_sql}
                          AND b.{time_column} IS NOT NULL
                          {time_filter}
                        GROUP BY attributed.kind
                        """,
                        booking_params,
                    )
                    out: dict[str, dict] = {}
                    for row in cur.fetchall():
                        out[row["kind"]] = {
                            "events": int(row["events"] or 0),
                            "uniques": int(row["uniques"] or 0),
                        }
                    return out

                raffle_kind_bookings = {
                    "created": _raffle_kind_booking_metric("created_at"),
                    "confirmed": _raffle_kind_booking_metric("confirmed_at"),
                    "cancelled": _raffle_kind_booking_metric("cancelled_at"),
                }

                # Audience is a snapshot (not period-bound), except blocked_at if we want — keep snapshot.
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE telegram_id IS NOT NULL)::int AS telegram_users,
                        COUNT(*) FILTER (WHERE telegram_id IS NOT NULL AND COALESCE(is_blocked, false))::int AS telegram_blocked,
                        COUNT(*) FILTER (
                            WHERE telegram_id IS NOT NULL AND NOT COALESCE(is_blocked, false)
                        )::int AS telegram_mailable,
                        COUNT(*) FILTER (WHERE vk_id IS NOT NULL)::int AS vk_users,
                        COUNT(*) FILTER (WHERE vk_id IS NOT NULL AND COALESCE(is_blocked, false))::int AS vk_blocked,
                        COUNT(*) FILTER (
                            WHERE vk_id IS NOT NULL AND NOT COALESCE(is_blocked, false)
                        )::int AS vk_mailable
                    FROM users
                    """
                )
                audience = dict(cur.fetchone() or {})

        return {
            "available": True,
            "period_label": period_label,
            "channel": channel or "all",
            "by_name": by_name,
            "starts_by_payload": starts_by_payload,
            "show_cards": show_cards,
            "raffle_branches": raffle_branches,
            "raffle_kind_steps": raffle_kind_steps,
            "raffle_kind_bookings": raffle_kind_bookings,
            "raffle_bookings": raffle_bookings,
            "proverka_bookings": proverka_bookings,
            "audience": audience,
        }
    except Exception:
        logger.exception("fetch_analytics_report failed")
        empty["period_label"] = period_label
        return empty


def fetch_user_activity(
    *,
    user_id: Optional[int] = None,
    telegram_id: Optional[int] = None,
    vk_id: Optional[int] = None,
    limit: int = 40,
) -> list[dict]:
    """Recent analytics events for one guest (admin user card)."""
    if not _use_postgres():
        return []
    where = None
    params: dict[str, Any] = {"limit": limit}
    if user_id is not None:
        where = "user_id = %(user_id)s"
        params["user_id"] = int(user_id)
    elif telegram_id is not None:
        where = "telegram_id = %(telegram_id)s"
        params["telegram_id"] = int(telegram_id)
    elif vk_id is not None:
        where = "vk_id = %(vk_id)s"
        params["vk_id"] = int(vk_id)
    else:
        return []
    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT name, props, created_at, channel
                    FROM analytics_events
                    WHERE {where}
                    ORDER BY created_at DESC
                    LIMIT %(limit)s
                    """,
                    params,
                )
                return [dict(row) for row in cur.fetchall()]
    except Exception:
        logger.exception(
            "fetch_user_activity failed user_id=%s telegram_id=%s vk_id=%s",
            user_id,
            telegram_id,
            vk_id,
        )
        return []


def fetch_user_last_event(
    *,
    user_id: Optional[int] = None,
    telegram_id: Optional[int] = None,
    vk_id: Optional[int] = None,
) -> dict | None:
    """Latest analytics event for one guest, or None."""
    rows = fetch_user_activity(user_id=user_id, telegram_id=telegram_id, vk_id=vk_id, limit=1)
    return rows[0] if rows else None


def fetch_users_last_events(user_ids: list[int]) -> dict[int, dict]:
    """Latest analytics event per users.id for Users table «Этап»."""
    ids = sorted({int(uid) for uid in user_ids if uid})
    if not _use_postgres() or not ids:
        return {}
    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON (resolved_user_id)
                        resolved_user_id AS user_id,
                        name,
                        props,
                        created_at,
                        channel
                    FROM (
                        SELECT
                            COALESCE(
                                ae.user_id,
                                u_tg.id,
                                u_vk.id
                            ) AS resolved_user_id,
                            ae.name,
                            ae.props,
                            ae.created_at,
                            ae.channel
                        FROM analytics_events ae
                        LEFT JOIN users u_tg
                            ON ae.user_id IS NULL
                           AND ae.telegram_id IS NOT NULL
                           AND u_tg.telegram_id = ae.telegram_id
                        LEFT JOIN users u_vk
                            ON ae.user_id IS NULL
                           AND ae.vk_id IS NOT NULL
                           AND u_vk.vk_id = ae.vk_id
                        WHERE COALESCE(ae.user_id, u_tg.id, u_vk.id) = ANY(%(ids)s)
                    ) t
                    WHERE resolved_user_id IS NOT NULL
                    ORDER BY resolved_user_id, created_at DESC
                    """,
                    {"ids": ids},
                )
                return {
                    int(row["user_id"]): dict(row)
                    for row in cur.fetchall()
                    if row.get("user_id") is not None
                }
    except Exception:
        logger.exception("fetch_users_last_events failed")
        return {}


def fetch_user_activity_counts(
    *,
    user_id: Optional[int] = None,
    telegram_id: Optional[int] = None,
    vk_id: Optional[int] = None,
) -> list[dict]:
    """Per-event counts for one guest (compact admin summary)."""
    if not _use_postgres():
        return []
    where = None
    params: dict[str, Any] = {}
    if user_id is not None:
        where = "user_id = %(user_id)s"
        params["user_id"] = int(user_id)
    elif telegram_id is not None:
        where = "telegram_id = %(telegram_id)s"
        params["telegram_id"] = int(telegram_id)
    elif vk_id is not None:
        where = "vk_id = %(vk_id)s"
        params["vk_id"] = int(vk_id)
    else:
        return []
    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT name, COUNT(*)::int AS events
                    FROM analytics_events
                    WHERE {where}
                    GROUP BY name
                    ORDER BY events DESC, name
                    """,
                    params,
                )
                return [dict(row) for row in cur.fetchall()]
    except Exception:
        logger.exception(
            "fetch_user_activity_counts failed user_id=%s telegram_id=%s vk_id=%s",
            user_id,
            telegram_id,
            vk_id,
        )
        return []


def _period_bounds(
    date_from: Optional[str],
    date_to: Optional[str],
) -> tuple[Any, Any, list[str], dict[str, Any]]:
    """Return (day_from, day_to, where_parts, params) for MSK calendar days."""
    from datetime import time, timedelta, timezone

    msk = timezone(timedelta(hours=3))
    where: list[str] = []
    params: dict[str, Any] = {}

    def _parse_day(value: str):
        value = (value or "").strip()
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            try:
                return datetime.strptime(value, "%d.%m.%Y").date()
            except ValueError:
                return None

    day_from = _parse_day(date_from or "")
    day_to = _parse_day(date_to or "")
    if day_from and not day_to:
        day_to = day_from
    if day_to and not day_from:
        day_from = day_to
    if day_from and day_to and day_to < day_from:
        day_from, day_to = day_to, day_from
    if day_from and day_to:
        where.append("ae.created_at >= %(start)s AND ae.created_at < %(end)s")
        params["start"] = datetime.combine(day_from, time.min, tzinfo=msk)
        params["end"] = datetime.combine(day_to + timedelta(days=1), time.min, tzinfo=msk)
    return day_from, day_to, where, params


_PERSON_KEY_SQL = """
COALESCE(
    ae.user_id::text,
    CASE WHEN ae.telegram_id IS NOT NULL THEN 'tg:' || ae.telegram_id::text END,
    CASE WHEN ae.vk_id IS NOT NULL THEN 'vk:' || ae.vk_id::text END
)
""".strip()


def fetch_analytics_event_people(
    name: str,
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    channel: str = "",
    limit: int = 300,
) -> list[dict]:
    """Unique people who fired `name` in the period (for analytics drill-down)."""
    name = (name or "").strip()
    if not name or not _use_postgres():
        return []
    _, _, where, params = _period_bounds(date_from, date_to)
    where.insert(0, "ae.name = %(name)s")
    params["name"] = name
    params["limit"] = max(1, min(int(limit), 1000))
    if channel in ("telegram", "vkontakte"):
        where.append("ae.channel = %(channel)s")
        params["channel"] = channel
    where_sql = " AND ".join(where)
    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        {_PERSON_KEY_SQL} AS person_key,
                        COALESCE(MAX(ae.user_id), MAX(u_tg.id), MAX(u_vk.id)) AS user_id,
                        COALESCE(
                            NULLIF(MAX(u_by_id.name), ''),
                            NULLIF(MAX(u_tg.name), ''),
                            NULLIF(MAX(u_vk.name), ''),
                            ''
                        ) AS name,
                        COALESCE(MAX(ae.telegram_id), MAX(u_by_id.telegram_id), MAX(u_tg.telegram_id)) AS telegram_id,
                        COALESCE(MAX(ae.vk_id), MAX(u_by_id.vk_id), MAX(u_vk.vk_id)) AS vk_id,
                        COALESCE(
                            NULLIF(MAX(u_by_id.username), ''),
                            NULLIF(MAX(u_tg.username), ''),
                            NULLIF(MAX(u_vk.username), ''),
                            ''
                        ) AS username,
                        MAX(ae.channel) AS channel,
                        COUNT(*)::int AS events,
                        COUNT(DISTINCT ae.booking_id)::int AS bookings,
                        MIN(ae.created_at) AS first_at,
                        MAX(ae.created_at) AS last_at
                    FROM analytics_events ae
                    LEFT JOIN users u_by_id ON u_by_id.id = ae.user_id
                    LEFT JOIN users u_tg
                        ON ae.telegram_id IS NOT NULL
                       AND u_tg.telegram_id = ae.telegram_id
                    LEFT JOIN users u_vk
                        ON ae.vk_id IS NOT NULL
                       AND u_vk.vk_id = ae.vk_id
                    WHERE {where_sql}
                      AND {_PERSON_KEY_SQL} IS NOT NULL
                    GROUP BY 1
                    ORDER BY MAX(ae.created_at) DESC
                    LIMIT %(limit)s
                    """,
                    params,
                )
                return [dict(row) for row in cur.fetchall()]
    except Exception:
        logger.exception("fetch_analytics_event_people failed name=%s", name)
        return []


def fetch_analytics_event_by_day(
    name: str,
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    channel: str = "",
    limit: int = 90,
) -> list[dict]:
    """Per-day events/uniques for one analytics event name (MSK calendar)."""
    name = (name or "").strip()
    if not name or not _use_postgres():
        return []
    day_from, day_to, where, params = _period_bounds(date_from, date_to)
    params["limit"] = max(1, min(int(limit), 366))
    if channel in ("telegram", "vkontakte"):
        params["channel"] = channel
    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                if name == "bot_start":
                    channel_filter = ""
                    if channel in ("telegram", "vkontakte"):
                        channel_filter = " AND ae.channel = %(channel)s"
                    first_day_filters = []
                    if day_from:
                        first_day_filters.append("fs.first_day >= %(day_from)s")
                        params["day_from"] = day_from
                    if day_to:
                        first_day_filters.append("fs.first_day <= %(day_to)s")
                        params["day_to"] = day_to
                    first_day_where = (
                        (" AND " + " AND ".join(first_day_filters)) if first_day_filters else ""
                    )
                    cur.execute(
                        f"""
                        WITH first_starts AS (
                            SELECT
                                {_PERSON_KEY_SQL} AS person_key,
                                MIN((ae.created_at AT TIME ZONE 'Europe/Moscow')::date) AS first_day
                            FROM analytics_events ae
                            WHERE ae.name = 'bot_start'
                              AND {_PERSON_KEY_SQL} IS NOT NULL
                              {channel_filter}
                            GROUP BY 1
                        )
                        SELECT
                            fs.first_day AS day,
                            COUNT(*)::int AS uniques,
                            COUNT(*)::int AS events
                        FROM first_starts fs
                        WHERE fs.person_key IS NOT NULL
                          {first_day_where}
                        GROUP BY fs.first_day
                        ORDER BY fs.first_day DESC
                        LIMIT %(limit)s
                        """,
                        params,
                    )
                    return [dict(row) for row in cur.fetchall()]

                where.insert(0, "ae.name = %(name)s")
                params["name"] = name
                if channel in ("telegram", "vkontakte"):
                    where.append("ae.channel = %(channel)s")
                where_sql = " AND ".join(where)
                cur.execute(
                    f"""
                    SELECT
                        ((ae.created_at AT TIME ZONE 'Europe/Moscow')::date) AS day,
                        COUNT(*)::int AS events,
                        COUNT(DISTINCT {_PERSON_KEY_SQL})::int AS uniques
                    FROM analytics_events ae
                    WHERE {where_sql}
                    GROUP BY 1
                    ORDER BY 1 DESC
                    LIMIT %(limit)s
                    """,
                    params,
                )
                return [dict(row) for row in cur.fetchall()]
    except Exception:
        logger.exception("fetch_analytics_event_by_day failed name=%s", name)
        return []

