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


def _is_http_url(value: str) -> bool:
    raw = (value or "").strip()
    if not raw:
        return False
    from urllib.parse import urlparse

    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _fmt_audit_date(event_date) -> str:
    return (
        event_date.strftime("%d.%m.%Y")
        if hasattr(event_date, "strftime")
        else str(event_date or "")
    )


def _fmt_audit_time(event_time) -> str:
    return (
        event_time.strftime("%H:%M")
        if hasattr(event_time, "strftime")
        else str(event_time or "")[:5]
    )


def _audit_event_item(
    event_id,
    event_date=None,
    event_time=None,
    location: str = "",
    *,
    change: str = "changed",
    changes: list[str] | None = None,
    before: dict | None = None,
    address: str = "",
) -> dict:
    try:
        eid = int(event_id) if event_id is not None else None
    except (TypeError, ValueError):
        eid = None
    item = {
        "id": eid,
        "date": _fmt_audit_date(event_date),
        "time": _fmt_audit_time(event_time),
        "location": (location or "").strip(),
        "change": (change or "changed").strip() or "changed",
    }
    addr = (address or "").strip()
    if addr:
        item["address"] = addr
    if changes:
        item["changes"] = list(changes)
    if before:
        item["before"] = before
    return item


def _norm_audit_text(value) -> str:
    text = str(value or "").replace("\xa0", " ").strip()
    text = " ".join(text.split())
    for ch in ("–", "—", "−"):
        text = text.replace(ch, "-")
    return text


def _event_snapshot_row(row) -> dict:
    """Normalize a DB events row (tuple or mapping) for audit compare."""
    if row is None:
        return {}
    if isinstance(row, dict):
        d, t, loc = row.get("event_date"), row.get("event_time"), row.get("location")
        address = row.get("address")
        description = row.get("description")
        image_url = row.get("image_url")
        price = row.get("price")
        payment_url = row.get("payment_url")
        host = row.get("host")
        max_seats = row.get("max_seats")
        status = row.get("status")
    else:
        # SELECT order used in _save_one
        (
            d,
            t,
            loc,
            address,
            description,
            image_url,
            price,
            payment_url,
            host,
            max_seats,
            status,
        ) = row
    try:
        price_i = int(price or 0)
    except (TypeError, ValueError):
        price_i = 0
    try:
        seats_i = int(max_seats or 0)
    except (TypeError, ValueError):
        seats_i = 0
    return {
        "date": _fmt_audit_date(d),
        "time": _fmt_audit_time(t),
        "location": _norm_audit_text(loc),
        "address": _norm_audit_text(address),
        "description": _norm_audit_text(description),
        "image_url": _norm_audit_text(image_url),
        "price": price_i,
        "payment_url": _norm_audit_text(payment_url),
        "host": _norm_audit_text(host),
        "max_seats": seats_i,
        "status": _norm_audit_text(status),
    }


# Полный diff — решать, нужно ли UPDATE в БД.
_SAVE_COMPARE_KEYS = (
    "date",
    "time",
    "location",
    "address",
    "description",
    "image_url",
    "price",
    "payment_url",
    "host",
    "max_seats",
)
# В журнал — только то, что видно как «какое шоу / когда / где».
# Состав/цена/описание при массовом «Обновить» часто дают шум без смены даты.
_AUDIT_COMPARE_KEYS = ("date", "time", "location", "address")
_FIELD_LABELS = {
    "date": "дату",
    "time": "время",
    "location": "площадку",
    "address": "адрес",
    "description": "описание",
    "image_url": "картинку",
    "price": "цену",
    "payment_url": "ссылку оплаты",
    "host": "состав",
    "max_seats": "места",
}


def _diff_event_fields(before: dict, after: dict, keys: tuple[str, ...] | None = None) -> list[str]:
    keys = keys or _SAVE_COMPARE_KEYS
    changed = []
    for key in keys:
        if before.get(key) != after.get(key):
            label = _FIELD_LABELS.get(key, key)
            changed.append(label)
    return changed


def save_events_batch(event_format: str, rows: list[dict[str, Any]]) -> dict:
    """
    Upsert/update/hide events from admin form rows.
    Each row may have id (int|None), date, time, location, address, ...
    Empty required fields → skip. delete=True → status hidden.
    """
    result = {
        "saved": 0,
        "hidden": 0,
        "deleted": 0,
        "added": 0,
        "changed": 0,
        "errors": [],
        "hidden_ids": [],
        "deleted_ids": [],
        "saved_items": [],
        "hidden_items": [],
        "deleted_items": [],
        "actions": [],
    }
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
            """
            SELECT event_date, event_time, location
            FROM events
            WHERE id = %s AND format = %s
            """,
            (event_id, event_format),
        )
        meta = cur.fetchone()
        cur.execute(
            "DELETE FROM events WHERE id = %s AND format = %s",
            (event_id, event_format),
        )
        if cur.rowcount:
            result["deleted"] += 1
            result.setdefault("deleted_ids", []).append(int(event_id))
            if meta:
                item = _audit_event_item(
                    event_id, meta[0], meta[1], meta[2] or "", change="deleted"
                )
                result.setdefault("deleted_items", []).append(item)
                result.setdefault("actions", []).append(item)
        return
    if delete:
        if event_id:
            form_date = parse_admin_date(str(raw.get("date") or ""))
            form_time = parse_admin_time(str(raw.get("time") or ""))
            form_loc = (raw.get("location") or "").strip()
            if not form_date or not form_time or not form_loc:
                cur.execute(
                    """
                    SELECT event_date, event_time, location
                    FROM events
                    WHERE id = %s AND format = %s
                    """,
                    (event_id, event_format),
                )
                meta = cur.fetchone()
                if meta:
                    form_date = form_date or meta[0]
                    form_time = form_time or meta[1]
                    form_loc = form_loc or (meta[2] or "")
            cur.execute(
                """
                UPDATE events
                SET status = 'hidden', updated_at = now()
                WHERE id = %s AND format = %s AND status IS DISTINCT FROM 'hidden'
                """,
                (event_id, event_format),
            )
            if cur.rowcount:
                result["hidden"] += 1
                result.setdefault("hidden_ids", []).append(int(event_id))
                item = _audit_event_item(
                    event_id, form_date, form_time, form_loc, change="hidden"
                )
                result.setdefault("hidden_items", []).append(item)
                result.setdefault("actions", []).append(item)
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

    row_ref = f"#{event_id}" if event_id else "новая строка"
    if image_url and not _is_http_url(image_url):
        result["errors"].append(
            f"{row_ref}: «Картинка» должна быть ссылкой http(s)://…"
        )
        return
    if event_format in {"best", "hitloto"}:
        if not payment_url:
            result["errors"].append(f"{row_ref}: нужна ссылка оплаты")
            return
        if not _is_http_url(payment_url):
            result["errors"].append(
                f"{row_ref}: «Оплата» должна быть ссылкой http(s)://…"
            )
            return
        if not image_url:
            result["errors"].append(f"{row_ref}: нужна ссылка на картинку")
            return
        if price <= 0:
            result["errors"].append(f"{row_ref}: укажите цену больше 0")
            return
    elif payment_url and not _is_http_url(payment_url):
        result["errors"].append(
            f"{row_ref}: «Оплата» должна быть ссылкой http(s)://…"
        )
        return

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

    after = {
        "date": _fmt_audit_date(event_date),
        "time": _fmt_audit_time(event_time),
        "location": _norm_audit_text(location),
        "address": _norm_audit_text(address),
        "description": _norm_audit_text(description),
        "image_url": _norm_audit_text(image_url),
        "price": int(price or 0),
        "payment_url": _norm_audit_text(payment_url),
        "host": _norm_audit_text(host),
        "max_seats": int(max_seats or 0),
        "status": status,
    }

    def _record_change(eid, before: dict) -> None:
        result["saved"] += 1
        audit_changes = _diff_event_fields(before, after, _AUDIT_COMPARE_KEYS)
        if not audit_changes:
            # Состав/цена/описание обновили в БД, но в журнал не шумим.
            return
        result["changed"] += 1
        before_identity = {
            "date": before.get("date") or "",
            "time": before.get("time") or "",
            "location": before.get("location") or "",
        }
        after_identity = {
            "date": after.get("date") or "",
            "time": after.get("time") or "",
            "location": after.get("location") or "",
        }
        item = _audit_event_item(
            eid,
            event_date,
            event_time,
            location,
            change="changed",
            changes=audit_changes,
            before=before_identity if before_identity != after_identity else None,
        )
        result.setdefault("saved_items", []).append(item)
        result.setdefault("actions", []).append(item)

    if event_id:
        cur.execute(
            """
            SELECT event_date, event_time, location, address, description,
                   image_url, price, payment_url, host, max_seats, status
            FROM events
            WHERE id = %s AND format = %s
            """,
            (event_id, event_format),
        )
        old_row = cur.fetchone()
        if not old_row:
            result["errors"].append(f"Мероприятие #{event_id} не найдено")
            return
        before = _event_snapshot_row(old_row)
        field_changes = _diff_event_fields(before, after)
        if not field_changes:
            return
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
            _record_change(event_id, before)
        return

    cur.execute(
        """
        SELECT id, event_date, event_time, location, address, description,
               image_url, price, payment_url, host, max_seats, status
        FROM events
        WHERE format = %s AND event_date = %s AND event_time = %s AND location = %s
        """,
        (event_format, event_date, event_time, location),
    )
    existing = cur.fetchone()
    if existing:
        existing_id = existing[0]
        before = _event_snapshot_row(existing[1:])
        field_changes = _diff_event_fields(before, after)
        if not field_changes:
            return
        cur.execute(
            """
            UPDATE events SET
                weekday = %s,
                address = %s,
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
                weekday,
                address,
                description,
                image_url,
                price,
                payment_url,
                host,
                max_seats,
                status,
                existing_id,
                event_format,
            ),
        )
        if cur.rowcount:
            _record_change(existing_id, before)
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
        RETURNING id
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
    new_id = (cur.fetchone() or [None])[0]
    result["saved"] += 1
    result["added"] += 1
    item = _audit_event_item(
        new_id,
        event_date,
        event_time,
        location,
        change="added",
        address=address,
    )
    result.setdefault("saved_items", []).append(item)
    result.setdefault("actions", []).append(item)


def restore_events(event_format: str, event_ids: list[int]) -> dict:
    """Bring hidden (or past) events back to afisha."""
    result = {"restored": 0, "errors": [], "restored_items": [], "actions": []}
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
                        SELECT event_date, event_time, location, status
                        FROM events
                        WHERE id = %s AND format = %s
                        """,
                        (event_id, event_format),
                    )
                    row = cur.fetchone()
                    if not row:
                        result["errors"].append(f"#{event_id} не найдено")
                        continue
                    event_date, event_time, location, status = row[0], row[1], row[2], row[3]
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
                        item = _audit_event_item(
                            event_id,
                            event_date,
                            event_time,
                            location or "",
                            change="restored",
                        )
                        result.setdefault("restored_items", []).append(item)
                        result.setdefault("actions", []).append(item)
            conn.commit()
    except Exception as exc:
        logger.exception("restore_events failed")
        result["errors"].append(str(exc))
    return result
