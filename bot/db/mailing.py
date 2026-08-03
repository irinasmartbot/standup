"""Mailing campaigns: schema, audience filters, queue CRUD."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from bot.config import BOOKINGS_SOURCE, DATABASE_URL

logger = logging.getLogger(__name__)

BOOKING_STATUSES = ("booked", "confirmed", "cancelled", "annulled")
CHANNELS = ("telegram", "vkontakte", "both")
CAMPAIGN_STATUSES = ("draft", "queued", "running", "paused", "done", "cancelled")


def _use_postgres() -> bool:
    return BOOKINGS_SOURCE == "postgres" and bool(DATABASE_URL)


def ensure_mailing_tables() -> None:
    if not _use_postgres():
        return
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mailing_campaigns (
                        id BIGSERIAL PRIMARY KEY,
                        title TEXT NOT NULL DEFAULT '',
                        channel TEXT NOT NULL
                            CHECK (channel IN ('telegram', 'vkontakte', 'both')),
                        status TEXT NOT NULL DEFAULT 'draft'
                            CHECK (status IN (
                                'draft', 'queued', 'running', 'paused', 'done', 'cancelled'
                            )),
                        body_html TEXT NOT NULL DEFAULT '',
                        photo_path TEXT,
                        button_text TEXT,
                        button_url TEXT,
                        followup_html TEXT,
                        interval_sec NUMERIC(8, 3) NOT NULL DEFAULT 0.100,
                        batch_limit INTEGER,
                        filters JSONB NOT NULL DEFAULT '{}'::jsonb,
                        total_count INTEGER NOT NULL DEFAULT 0,
                        sent_count INTEGER NOT NULL DEFAULT 0,
                        failed_count INTEGER NOT NULL DEFAULT 0,
                        skipped_count INTEGER NOT NULL DEFAULT 0,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        started_at TIMESTAMPTZ,
                        finished_at TIMESTAMPTZ,
                        created_by TEXT NOT NULL DEFAULT 'owner'
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mailing_recipients (
                        id BIGSERIAL PRIMARY KEY,
                        campaign_id BIGINT NOT NULL
                            REFERENCES mailing_campaigns(id) ON DELETE CASCADE,
                        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        channel TEXT NOT NULL CHECK (channel IN ('telegram', 'vkontakte')),
                        peer_id BIGINT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'sent', 'failed', 'skipped')),
                        error TEXT,
                        sent_at TIMESTAMPTZ,
                        UNIQUE (campaign_id, channel, peer_id)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_mailing_recipients_pending
                    ON mailing_recipients (campaign_id, id)
                    WHERE status = 'pending'
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_mailing_recipients_user_sent
                    ON mailing_recipients (user_id, channel, sent_at)
                    WHERE status = 'sent'
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_mailing_campaigns_status
                    ON mailing_campaigns (status, id)
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS is_blocked BOOLEAN NOT NULL DEFAULT false
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE mailing_campaigns
                    ADD COLUMN IF NOT EXISTS disable_link_preview BOOLEAN NOT NULL DEFAULT false
                    """
                )
            conn.commit()
    except Exception:
        logger.exception("ensure_mailing_tables failed")


def normalize_filters(raw: dict | None) -> dict[str, Any]:
    data = dict(raw or {})
    statuses = data.get("booking_statuses") or []
    if isinstance(statuses, str):
        statuses = [s.strip() for s in statuses.split(",") if s.strip()]
    # «active» = бронь или билет (частый смысл «активная бронь» у оператора).
    expanded: list[str] = []
    for status in statuses:
        if status == "active":
            expanded.extend(["booked", "confirmed"])
        elif status in BOOKING_STATUSES:
            expanded.append(status)
    statuses = list(dict.fromkeys(expanded))
    try:
        exclude_days = int(data.get("exclude_sent_days") or 0)
    except (TypeError, ValueError):
        exclude_days = 0
    exclude_days = max(0, min(exclude_days, 3650))
    try:
        batch_limit = data.get("batch_limit")
        batch_limit = int(batch_limit) if batch_limit not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        batch_limit = None
    if batch_limit is not None:
        batch_limit = max(1, min(batch_limit, 100_000))
    # Только дата шоу (event_date). Одна заполненная дата = конкретный день.
    date_from = (data.get("date_from") or data.get("booking_date_from") or "").strip() or None
    date_to = (data.get("date_to") or data.get("booking_date_to") or "").strip() or None
    if date_from and not date_to:
        date_to = date_from
    elif date_to and not date_from:
        date_from = date_to
    return {
        "booking_statuses": statuses,
        "date_mode": "event",
        "date_from": date_from,
        "date_to": date_to,
        "has_phone": bool(data.get("has_phone")),
        "exclude_blocked": bool(data.get("exclude_blocked", True)),
        "exclude_sent_days": exclude_days,
        "batch_limit": batch_limit,
    }


def _status_ts_sql() -> str:
    """Timestamp of the booking status change (fallback: created_at)."""
    return """
        CASE b.status
            WHEN 'confirmed' THEN COALESCE(b.confirmed_at, b.created_at)
            WHEN 'cancelled' THEN COALESCE(b.cancelled_at, b.updated_at, b.created_at)
            WHEN 'annulled' THEN COALESCE(b.annulled_at, b.updated_at, b.created_at)
            ELSE b.created_at
        END
    """


def _audience_sql(channel: str, filters: dict) -> tuple[str, dict]:
    """Build SELECT user_id, channel, peer_id for one messenger channel."""
    if channel not in ("telegram", "vkontakte"):
        raise ValueError("bad channel")
    params: dict[str, Any] = {"msg_channel": channel}
    where = ["TRUE"]
    if channel == "telegram":
        where.append("u.telegram_id IS NOT NULL")
        peer_expr = "u.telegram_id"
        if filters.get("exclude_blocked"):
            where.append("COALESCE(u.is_blocked, false) = false")
    else:
        where.append("u.vk_id IS NOT NULL")
        peer_expr = "u.vk_id"

    if filters.get("has_phone"):
        where.append("NULLIF(TRIM(COALESCE(u.phone, '')), '') IS NOT NULL")

    statuses = list(filters.get("booking_statuses") or [])
    date_from = filters.get("date_from") or filters.get("booking_date_from")
    date_to = filters.get("date_to") or filters.get("booking_date_to")
    if statuses or date_from or date_to:
        booking_where = ["b.user_id = u.id"]
        if statuses:
            params["booking_statuses"] = list(statuses)
            booking_where.append("b.status = ANY(%(booking_statuses)s)")
        use_event_dates = bool(date_from or date_to)
        if date_from:
            params["date_from"] = str(date_from)
            booking_where.append("e.event_date >= %(date_from)s::date")
        if date_to:
            params["date_to"] = str(date_to)
            booking_where.append("e.event_date <= %(date_to)s::date")
        from_sql = (
            "bookings b JOIN events e ON e.id = b.event_id"
            if use_event_dates
            else "bookings b"
        )
        where.append(
            "EXISTS (SELECT 1 FROM " + from_sql + " WHERE " + " AND ".join(booking_where) + ")"
        )

    exclude_days = int(filters.get("exclude_sent_days") or 0)
    if exclude_days > 0:
        params["exclude_days"] = exclude_days
        where.append(
            """
            NOT EXISTS (
                SELECT 1
                FROM mailing_recipients mr
                WHERE mr.user_id = u.id
                  AND mr.channel = %(msg_channel)s
                  AND mr.status = 'sent'
                  AND mr.sent_at >= NOW() - (%(exclude_days)s || ' days')::interval
            )
            """
        )

    where_sql = " AND ".join(where)
    # channel — только whitelist-значение, в SELECT безопасно как литерал.
    sql = f"""
        SELECT u.id AS user_id, '{channel}'::text AS channel, {peer_expr} AS peer_id
        FROM users u
        WHERE {where_sql}
    """
    return sql, params


def preview_audience(channel: str, filters: dict | None) -> dict[str, Any]:
    """Count recipients per channel without inserting."""
    ensure_mailing_tables()
    filters = normalize_filters(filters)
    if channel not in CHANNELS:
        raise ValueError("bad channel")
    if not _use_postgres():
        raise RuntimeError(
            "Рассылка недоступна: нужен PostgreSQL "
            "(BOOKINGS_SOURCE=postgres и DATABASE_URL)."
        )

    counts = {"telegram": 0, "vkontakte": 0}
    db_totals = {"telegram": 0, "vkontakte": 0}
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE telegram_id IS NOT NULL)::int AS telegram,
                    COUNT(*) FILTER (WHERE vk_id IS NOT NULL)::int AS vkontakte
                FROM users
                """
            )
            row = cur.fetchone() or {}
            db_totals["telegram"] = int(row.get("telegram") or 0)
            db_totals["vkontakte"] = int(row.get("vkontakte") or 0)

            # Всегда оба канала в ответе — чтобы в UI не казалось, что VK «пустой»,
            # когда выбран только Telegram.
            for ch in ("telegram", "vkontakte"):
                sql, params = _audience_sql(ch, filters)
                cur.execute(f"SELECT COUNT(*) AS n FROM ({sql}) AS aud", params)
                counts[ch] = int(cur.fetchone()["n"] or 0)

    if channel == "both":
        total = counts["telegram"] + counts["vkontakte"]
    else:
        total = counts[channel]
    limit = filters.get("batch_limit")
    capped = min(total, limit) if limit else total
    return {
        "telegram": counts["telegram"],
        "vkontakte": counts["vkontakte"],
        "total": total,
        "capped_total": capped,
        "batch_limit": limit,
        "filters": filters,
        "db_totals": db_totals,
        "selected_channel": channel,
    }


def estimate_duration_sec(count: int, interval_sec: float) -> float:
    n = max(0, int(count))
    delay = max(0.0, float(interval_sec))
    if n <= 0:
        return 0.0
    return max(0.0, (n - 1) * delay)


def format_duration(seconds: float) -> str:
    sec = int(round(seconds))
    if sec < 60:
        return f"{sec} сек"
    minutes, rem = divmod(sec, 60)
    if minutes < 60:
        return f"{minutes} мин {rem} сек" if rem else f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if minutes:
        return f"{hours} ч {minutes} мин"
    return f"{hours} ч"


def create_campaign(
    *,
    title: str,
    channel: str,
    body_html: str,
    interval_sec: float,
    filters: dict | None,
    photo_path: str | None = None,
    button_text: str | None = None,
    button_url: str | None = None,
    followup_html: str | None = None,
    disable_link_preview: bool = False,
    created_by: str = "owner",
    start: bool = True,
) -> dict:
    ensure_mailing_tables()
    if not _use_postgres():
        raise RuntimeError("PostgreSQL required for mailing")
    if channel not in CHANNELS:
        raise ValueError("bad channel")
    body_html = (body_html or "").strip()
    if not body_html and not photo_path:
        raise ValueError("Нужен текст или картинка")
    interval_sec = max(0.0, min(float(interval_sec), 60.0))
    filters_n = normalize_filters(filters)
    button_text = (button_text or "").strip() or None
    button_url = (button_url or "").strip() or None
    followup_html = (followup_html or "").strip() or None
    if button_text and not button_url and not followup_html:
        raise ValueError("Для кнопки укажите ссылку или текст follow-up")

    preview = preview_audience(channel, filters_n)
    capped = int(preview["capped_total"])
    if capped <= 0:
        raise ValueError("По фильтрам никого нет")

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mailing_campaigns (
                    title, channel, status, body_html, photo_path,
                    button_text, button_url, followup_html, disable_link_preview,
                    interval_sec, batch_limit, filters, total_count, created_by
                )
                VALUES (
                    %(title)s, %(channel)s, %(status)s, %(body_html)s, %(photo_path)s,
                    %(button_text)s, %(button_url)s, %(followup_html)s, %(disable_link_preview)s,
                    %(interval_sec)s, %(batch_limit)s, %(filters)s, 0, %(created_by)s
                )
                RETURNING *
                """,
                {
                    "title": (title or "").strip() or "Рассылка",
                    "channel": channel,
                    "status": "queued" if start else "draft",
                    "body_html": body_html,
                    "photo_path": photo_path,
                    "button_text": button_text,
                    "button_url": button_url,
                    "followup_html": followup_html,
                    "disable_link_preview": bool(disable_link_preview),
                    "interval_sec": interval_sec,
                    "batch_limit": filters_n.get("batch_limit"),
                    "filters": Json(filters_n),
                    "created_by": created_by or "owner",
                },
            )
            campaign = dict(cur.fetchone())
            campaign_id = int(campaign["id"])

            channels = ["telegram", "vkontakte"] if channel == "both" else [channel]
            inserted = 0
            for ch in channels:
                sql, params = _audience_sql(ch, filters_n)
                params = dict(params)
                params["campaign_id"] = campaign_id
                limit_sql = ""
                remaining = None
                if filters_n.get("batch_limit"):
                    remaining = max(0, int(filters_n["batch_limit"]) - inserted)
                    if remaining <= 0:
                        break
                    limit_sql = f" LIMIT {int(remaining)}"
                cur.execute(
                    f"""
                    INSERT INTO mailing_recipients (campaign_id, user_id, channel, peer_id)
                    SELECT %(campaign_id)s, aud.user_id, aud.channel, aud.peer_id
                    FROM ({sql}) AS aud
                    ORDER BY aud.user_id
                    {limit_sql}
                    ON CONFLICT (campaign_id, channel, peer_id) DO NOTHING
                    """,
                    params,
                )
                inserted += cur.rowcount or 0

            cur.execute(
                """
                UPDATE mailing_campaigns
                SET total_count = (
                    SELECT COUNT(*) FROM mailing_recipients WHERE campaign_id = %(id)s
                )
                WHERE id = %(id)s
                RETURNING *
                """,
                {"id": campaign_id},
            )
            campaign = dict(cur.fetchone())
        conn.commit()
    return campaign


def list_campaigns(limit: int = 40) -> list[dict]:
    ensure_mailing_tables()
    if not _use_postgres():
        return []
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM mailing_campaigns
                WHERE title IS DISTINCT FROM 'test-followup'
                ORDER BY id DESC
                LIMIT %(limit)s
                """,
                {"limit": max(1, min(int(limit), 200))},
            )
            return [dict(r) for r in cur.fetchall()]


def get_campaign(campaign_id: int) -> dict | None:
    ensure_mailing_tables()
    if not _use_postgres():
        return None
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM mailing_campaigns WHERE id = %s",
                (int(campaign_id),),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def get_campaign_followup(campaign_id: int) -> str | None:
    row = get_campaign(campaign_id)
    if not row:
        return None
    text = (row.get("followup_html") or "").strip()
    return text or None


def set_campaign_status(campaign_id: int, status: str) -> dict | None:
    if status not in CAMPAIGN_STATUSES:
        raise ValueError("bad status")
    ensure_mailing_tables()
    extras = ""
    if status == "running":
        extras = ", started_at = COALESCE(started_at, NOW())"
    if status in ("done", "cancelled"):
        extras = ", finished_at = NOW()"
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE mailing_campaigns
                SET status = %(status)s {extras}
                WHERE id = %(id)s
                RETURNING *
                """,
                {"id": int(campaign_id), "status": status},
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


def claim_next_campaign() -> dict | None:
    """Pick queued campaign or continue a running one."""
    ensure_mailing_tables()
    if not _use_postgres():
        return None
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM mailing_campaigns
                WHERE status = 'running'
                ORDER BY id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    """
                    SELECT id FROM mailing_campaigns
                    WHERE status = 'queued'
                    ORDER BY id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """
                )
                row = cur.fetchone()
                if not row:
                    conn.commit()
                    return None
                cur.execute(
                    """
                    UPDATE mailing_campaigns
                    SET status = 'running',
                        started_at = COALESCE(started_at, NOW())
                    WHERE id = %s
                    RETURNING *
                    """,
                    (int(row["id"]),),
                )
            else:
                cur.execute(
                    "SELECT * FROM mailing_campaigns WHERE id = %s",
                    (int(row["id"]),),
                )
            campaign = dict(cur.fetchone())
        conn.commit()
    return campaign


def fetch_pending_recipients(campaign_id: int, limit: int = 25) -> list[dict]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM mailing_recipients
                WHERE campaign_id = %s AND status = 'pending'
                ORDER BY id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (int(campaign_id), max(1, min(int(limit), 100))),
            )
            rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
    return rows


def mark_recipient(
    recipient_id: int,
    *,
    status: str,
    error: str | None = None,
) -> None:
    if status not in ("sent", "failed", "skipped"):
        raise ValueError("bad recipient status")
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE mailing_recipients
                SET status = %(status)s,
                    error = %(error)s,
                    sent_at = CASE WHEN %(status)s = 'sent' THEN NOW() ELSE sent_at END
                WHERE id = %(id)s AND status = 'pending'
                """,
                {
                    "id": int(recipient_id),
                    "status": status,
                    "error": (error or "")[:500] or None,
                },
            )
            cur.execute(
                """
                UPDATE mailing_campaigns c
                SET
                    sent_count = (
                        SELECT COUNT(*) FROM mailing_recipients r
                        WHERE r.campaign_id = c.id AND r.status = 'sent'
                    ),
                    failed_count = (
                        SELECT COUNT(*) FROM mailing_recipients r
                        WHERE r.campaign_id = c.id AND r.status = 'failed'
                    ),
                    skipped_count = (
                        SELECT COUNT(*) FROM mailing_recipients r
                        WHERE r.campaign_id = c.id AND r.status = 'skipped'
                    )
                WHERE c.id = (
                    SELECT campaign_id FROM mailing_recipients WHERE id = %(id)s
                )
                """,
                {"id": int(recipient_id)},
            )
        conn.commit()


def finalize_if_complete(campaign_id: int) -> dict | None:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM mailing_recipients
                WHERE campaign_id = %s AND status = 'pending'
                """,
                (int(campaign_id),),
            )
            pending = int(cur.fetchone()["n"] or 0)
            if pending > 0:
                cur.execute(
                    "SELECT * FROM mailing_campaigns WHERE id = %s",
                    (int(campaign_id),),
                )
                row = cur.fetchone()
                conn.commit()
                return dict(row) if row else None
            cur.execute(
                """
                UPDATE mailing_campaigns
                SET status = 'done', finished_at = NOW()
                WHERE id = %s AND status = 'running'
                RETURNING *
                """,
                (int(campaign_id),),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else get_campaign(campaign_id)


def list_recipients(
    campaign_id: int,
    *,
    status: str = "",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    ensure_mailing_tables()
    if not _use_postgres():
        return {"rows": [], "total": 0, "page": 1, "pages": 1}
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 100))
    where = ["r.campaign_id = %(campaign_id)s"]
    params: dict[str, Any] = {"campaign_id": int(campaign_id)}
    if status in ("pending", "sent", "failed", "skipped"):
        where.append("r.status = %(status)s")
        params["status"] = status
    where_sql = " AND ".join(where)
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) AS n FROM mailing_recipients r WHERE {where_sql}",
                params,
            )
            total = int(cur.fetchone()["n"] or 0)
            pages = max(1, (total + page_size - 1) // page_size)
            if page > pages:
                page = pages
            params["limit"] = page_size
            params["offset"] = (page - 1) * page_size
            cur.execute(
                f"""
                SELECT r.*, u.name, u.username, u.phone
                FROM mailing_recipients r
                JOIN users u ON u.id = r.user_id
                WHERE {where_sql}
                ORDER BY r.id
                LIMIT %(limit)s OFFSET %(offset)s
                """,
                params,
            )
            rows = [dict(r) for r in cur.fetchall()]
    return {"rows": rows, "total": total, "page": page, "pages": pages}


def set_campaign_photo(campaign_id: int, photo_path: str | None) -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE mailing_campaigns SET photo_path = %s WHERE id = %s",
                (photo_path, int(campaign_id)),
            )
        conn.commit()


def iso(dt: Any) -> str:
    if not dt:
        return ""
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return str(dt)


def search_users_for_test(q: str, *, channel: str = "", limit: int = 20) -> list[dict]:
    """Find users by name / @username / phone for test mailing."""
    ensure_mailing_tables()
    if not _use_postgres():
        return []
    q = (q or "").strip()
    if len(q) < 1:
        return []
    q_user = q[1:].strip() if q.startswith("@") else q
    phone_digits = "".join(ch for ch in q if ch.isdigit())
    where = [
        "("
        "COALESCE(u.name, '') ILIKE %(q_like)s"
        " OR COALESCE(u.username, '') ILIKE %(q_user_like)s"
        " OR COALESCE(u.phone, '') ILIKE %(q_like)s"
        + (
            " OR regexp_replace(COALESCE(u.phone, ''), '\\D', '', 'g') LIKE %(q_phone_digits)s"
            if len(phone_digits) >= 3
            else ""
        )
        + ")"
    ]
    params: dict[str, Any] = {
        "q_like": f"%{q}%",
        "q_user_like": f"%{q_user}%",
        "limit": max(1, min(int(limit), 50)),
    }
    if len(phone_digits) >= 3:
        params["q_phone_digits"] = f"%{phone_digits}%"
    if channel == "telegram":
        where.append("u.telegram_id IS NOT NULL")
    elif channel == "vkontakte":
        where.append("u.vk_id IS NOT NULL")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT u.id, u.name, u.username, u.phone, u.telegram_id, u.vk_id, u.source
                FROM users u
                WHERE {" AND ".join(where)}
                ORDER BY u.id DESC
                LIMIT %(limit)s
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]


def create_followup_stub(
    *,
    followup_html: str,
    body_html: str = "",
    channel: str = "telegram",
    created_by: str = "owner",
) -> int:
    """Store follow-up text so test/callback buttons can resolve mail_fu:<id>."""
    ensure_mailing_tables()
    if not _use_postgres():
        raise RuntimeError("PostgreSQL required")
    ch = channel if channel in ("telegram", "vkontakte", "both") else "telegram"
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mailing_campaigns (
                    title, channel, status, body_html, followup_html,
                    interval_sec, filters, total_count, created_by,
                    started_at, finished_at
                )
                VALUES (
                    'test-followup', %(channel)s, 'done', %(body)s, %(followup)s,
                    0, '{}'::jsonb, 0, %(created_by)s, NOW(), NOW()
                )
                RETURNING id
                """,
                {
                    "channel": ch,
                    "body": body_html or "",
                    "followup": (followup_html or "").strip(),
                    "created_by": created_by or "owner",
                },
            )
            cid = int(cur.fetchone()["id"])
        conn.commit()
    return cid


def get_user_for_mailing(user_id: int) -> dict | None:
    ensure_mailing_tables()
    if not _use_postgres():
        return None
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, username, phone, telegram_id, vk_id, source
                FROM users
                WHERE id = %s
                """,
                (int(user_id),),
            )
            row = cur.fetchone()
            return dict(row) if row else None
