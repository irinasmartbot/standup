"""Product analytics events for the admin funnel.

Never raises to callers: tracking must not break booking / UX flows.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

import psycopg

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
EVENT_BOT_BLOCKED = "bot_blocked"
EVENT_BOT_UNBLOCKED = "bot_unblocked"


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
                # Lightweight touch: ensure TG user exists for start/branch events
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
