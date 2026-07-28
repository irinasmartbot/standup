"""Admin CRUD for afisha events (replaces Google Sheets sync as source of truth)."""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any

import psycopg
from psycopg.rows import dict_row

from bot.config import BOOKINGS_SOURCE, DATABASE_URL
from bot.utils.ticket import WEEKDAYS_RU

logger = logging.getLogger(__name__)

AFISHA_FORMATS = ("best", "proverka", "hitloto")
AFISHA_FORMAT_LABELS = {
    "best": "BEST",
    "proverka": "Проверка",
    "hitloto": "Hit Loto",
}


def _use_postgres() -> bool:
    return BOOKINGS_SOURCE == "postgres" and bool(DATABASE_URL)


def weekday_ru_from_date(value: date) -> str:
    return WEEKDAYS_RU.get(value.strftime("%A"), "")


def parse_admin_date(value: str) -> date | None:
    raw = (value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_admin_time(value: str) -> time | None:
    raw = (value or "").strip().replace(".", ":")
    if not raw:
        return None
    for fmt in ("%H:%M", "%H"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    return None


def mark_past_events() -> int:
    """Flip active events with date before today (MSK calendar) to status=past."""
    if not _use_postgres():
        return 0
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE events
                    SET status = 'past', updated_at = now()
                    WHERE status = 'active'
                      AND event_date < (now() AT TIME ZONE 'Europe/Moscow')::date
                    """
                )
                n = cur.rowcount or 0
            conn.commit()
            return n
    except Exception:
        logger.exception("mark_past_events failed")
        return 0


def list_events_for_admin(event_format: str) -> dict[str, list[dict]]:
    """Return {active, past, hidden} for one format."""
    empty = {"active": [], "past": [], "hidden": []}
    if not _use_postgres() or event_format not in AFISHA_FORMATS:
        return empty
    mark_past_events()
    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id, format, event_date, weekday, event_time,
                        address, location, description, image_url,
                        price, payment_url, host, max_seats, status
                    FROM events
                    WHERE format = %(format)s
                      AND status IN ('active', 'past', 'hidden')
                    ORDER BY event_date DESC, event_time DESC, location
                    """,
                    {"format": event_format},
                )
                rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        logger.exception("list_events_for_admin failed for %s", event_format)
        return empty

    active, past, hidden = [], [], []
    for row in rows:
        item = _serialize_event(row)
        status = row.get("status")
        if status == "past":
            past.append(item)
        elif status == "hidden":
            hidden.append(item)
        else:
            active.append(item)
    active.sort(key=lambda e: (e.get("date_iso") or "", e.get("time") or "", e.get("location") or ""))
    return {"active": active, "past": past, "hidden": hidden}


def _serialize_event(row: dict) -> dict:
    d = row.get("event_date")
    t = row.get("event_time")
    return {
        "id": row.get("id"),
        "format": row.get("format") or "",
        "date_iso": d.isoformat() if hasattr(d, "isoformat") else str(d or ""),
        "date_display": d.strftime("%d.%m.%Y") if hasattr(d, "strftime") else str(d or ""),
        "weekday": row.get("weekday") or "",
        "time": t.strftime("%H:%M") if hasattr(t, "strftime") else str(t or "")[:5],
        "address": row.get("address") or "",
        "location": row.get("location") or "",
        "description": row.get("description") or "",
        "image_url": row.get("image_url") or "",
        "price": int(row.get("price") or 0),
        "payment_url": row.get("payment_url") or "",
        "host": row.get("host") or "",
        "max_seats": int(row.get("max_seats") or 0),
        "status": row.get("status") or "active",
    }


def save_events_batch(event_format: str, rows: list[dict[str, Any]]) -> dict:
    """
    Upsert/update/hide events from admin form rows.
    Each row may have id (int|None), date, time, location, address, ...
    Empty required fields → skip. delete=True → status hidden.
    """
    result = {"saved": 0, "hidden": 0, "deleted": 0, "errors": [], "hidden_ids": [], "deleted_ids": []}
    if not _use_postgres():
        result["errors"].append("Мероприятия правятся только в PostgreSQL.")
        return result
    if event_format not in AFISHA_FORMATS:
        result["errors"].append("Неизвестный формат.")
        return result

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                for idx, raw in enumerate(rows, start=1):
                    try:
                        _save_one(cur, event_format, raw, result)
                    except Exception as exc:
                        logger.exception("save event row %s failed", idx)
                        result["errors"].append(f"Строка {idx}: {exc}")
            conn.commit()
    except Exception as exc:
        logger.exception("save_events_batch failed")
        result["errors"].append(str(exc))
    return result


def _save_one(cur, event_format: str, raw: dict, result: dict) -> None:
    event_id = raw.get("id")
    purge = bool(raw.get("purge"))
    delete = bool(raw.get("delete"))
    if purge:
        if not event_id:
            return
        cur.execute(
            "SELECT COUNT(*) FROM bookings WHERE event_id = %s",
            (event_id,),
        )
        booking_count = int((cur.fetchone() or [0])[0] or 0)
        if booking_count:
            result["errors"].append(
                f"#{event_id}: есть брони ({booking_count}) — можно только скрыть, не удалить"
            )
            return
        cur.execute(
            "DELETE FROM events WHERE id = %s AND format = %s",
            (event_id, event_format),
        )
        if cur.rowcount:
            result["deleted"] += 1
            result.setdefault("deleted_ids", []).append(int(event_id))
        return
    if delete:
        if event_id:
            cur.execute(
                """
                UPDATE events
                SET status = 'hidden', updated_at = now()
                WHERE id = %s AND format = %s
                """,
                (event_id, event_format),
            )
            if cur.rowcount:
                result["hidden"] += 1
                result.setdefault("hidden_ids", []).append(int(event_id))
        return

    event_date = parse_admin_date(str(raw.get("date") or ""))
    event_time = parse_admin_time(str(raw.get("time") or ""))
    location = (raw.get("location") or "").strip()
    address = (raw.get("address") or "").strip()
    # Skip blank new rows
    if not event_id and not any([event_date, event_time, location, address, (raw.get("description") or "").strip()]):
        return
    if not event_date or not event_time or not location or not address:
        result["errors"].append(
            f"Нужны дата, время, площадка и адрес"
            + (f" (id {event_id})" if event_id else " (новая строка)")
        )
        return

    weekday = weekday_ru_from_date(event_date)
    description = (raw.get("description") or "").strip() or None
    image_url = (raw.get("image_url") or "").strip() or None
    host = (raw.get("host") or "").strip() or None
    payment_url = (raw.get("payment_url") or "").strip() or None
    try:
        price = max(0, int(raw.get("price") or 0))
    except (TypeError, ValueError):
        price = 0
    try:
        max_seats = max(0, int(raw.get("max_seats") or 0))
    except (TypeError, ValueError):
        max_seats = 60 if event_format == "proverka" else 0

    if event_format == "proverka":
        price = 0
        payment_url = None
        host = None

    today = datetime.now().astimezone().date()
    # Prefer MSK calendar day
    try:
        from bot.utils.ticket import now_msk

        today = now_msk().date()
    except Exception:
        pass
    status = "past" if event_date < today else "active"

    if event_id:
        cur.execute(
            """
            UPDATE events SET
                event_date = %s,
                weekday = %s,
                event_time = %s,
                address = %s,
                location = %s,
                description = %s,
                image_url = %s,
                price = %s,
                payment_url = %s,
                host = %s,
                max_seats = %s,
                status = %s,
                source_sheet = 'admin',
                updated_at = now()
            WHERE id = %s AND format = %s
            """,
            (
                event_date,
                weekday,
                event_time,
                address,
                location,
                description,
                image_url,
                price,
                payment_url,
                host,
                max_seats,
                status,
                event_id,
                event_format,
            ),
        )
        if cur.rowcount:
            result["saved"] += 1
        else:
            result["errors"].append(f"Мероприятие #{event_id} не найдено")
        return

    cur.execute(
        """
        INSERT INTO events (
            format, event_date, weekday, event_time, address, location, description,
            image_url, price, payment_url, host, max_seats, status, source_sheet, source_row
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, 'admin', NULL
        )
        ON CONFLICT (format, event_date, event_time, location)
        DO UPDATE SET
            weekday = EXCLUDED.weekday,
            address = EXCLUDED.address,
            description = EXCLUDED.description,
            image_url = EXCLUDED.image_url,
            price = EXCLUDED.price,
            payment_url = EXCLUDED.payment_url,
            host = EXCLUDED.host,
            max_seats = EXCLUDED.max_seats,
            status = EXCLUDED.status,
            source_sheet = 'admin',
            updated_at = now()
        """,
        (
            event_format,
            event_date,
            weekday,
            event_time,
            address,
            location,
            description,
            image_url,
            price,
            payment_url,
            host,
            max_seats,
            status,
        ),
    )
    result["saved"] += 1


def restore_events(event_format: str, event_ids: list[int]) -> dict:
    """Bring hidden (or past) events back to afisha."""
    result = {"restored": 0, "errors": []}
    if not _use_postgres():
        result["errors"].append("Мероприятия правятся только в PostgreSQL.")
        return result
    if event_format not in AFISHA_FORMATS:
        result["errors"].append("Неизвестный формат.")
        return result
    ids = []
    for i in event_ids:
        try:
            ids.append(int(i))
        except (TypeError, ValueError):
            continue
    if not ids:
        return result
    try:
        from bot.utils.ticket import now_msk

        today = now_msk().date()
    except Exception:
        today = datetime.now().date()
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                for event_id in ids:
                    cur.execute(
                        """
                        SELECT event_date, status
                        FROM events
                        WHERE id = %s AND format = %s
                        """,
                        (event_id, event_format),
                    )
                    row = cur.fetchone()
                    if not row:
                        result["errors"].append(f"#{event_id} не найдено")
                        continue
                    event_date, status = row[0], row[1]
                    if status not in {"hidden", "past"}:
                        continue
                    new_status = "past" if event_date < today else "active"
                    cur.execute(
                        """
                        UPDATE events
                        SET status = %s, updated_at = now(), source_sheet = 'admin'
                        WHERE id = %s AND format = %s
                        """,
                        (new_status, event_id, event_format),
                    )
                    if cur.rowcount:
                        result["restored"] += 1
            conn.commit()
    except Exception as exc:
        logger.exception("restore_events failed")
        result["errors"].append(str(exc))
    return result
