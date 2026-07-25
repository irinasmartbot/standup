import asyncio
import hmac
import html
import os
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import psycopg
from aiohttp import web
from psycopg.rows import dict_row


MSK = timezone(timedelta(hours=3))
STATUSES = ("booked", "confirmed", "cancelled", "annulled")
ACTIVE_STATUSES = {"booked", "confirmed"}
FORMAT_OPTIONS = ("proverka", "rozygrysh")
FORMAT_LABELS = {
    "proverka": "проверка",
    "rozygrysh": "розыгрыш",
}
STATUS_LABELS = {
    "booked": "Забронировано",
    "confirmed": "Подтверждено",
    "cancelled": "Отменено",
    "annulled": "Аннулировано",
}
BOOKING_SORT_OPTIONS = ("user_id", "status", "date", "location", "created", "changed")
STATUS_COLORS = {
    "booked": "#f59e0b",
    "confirmed": "#22c55e",
    "cancelled": "#ef4444",
    "annulled": "#64748b",
}
ADMIN_COOKIE_NAME = "standup_admin_token"
ADMIN_COOKIE_MAX_AGE = 60 * 60 * 24 * 30
DB_VIEW_TABLES = (
    "events",
    "users",
    "bookings",
    "raffle_submissions",
    "raffle_nav",
    "help_requests",
    "analytics_events",
)
DB_PAGE_SIZE = 50


def _load_env_file(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    _load_env_file()


@dataclass
class AdminConfig:
    database_url: str
    db_path: str
    bookings_source: str
    admin_token: str
    owner_token: str


def load_config() -> AdminConfig:
    database_url = os.getenv("DATABASE_URL", "")
    return AdminConfig(
        database_url=database_url,
        db_path=os.getenv("DB_PATH", "bookings.db"),
        bookings_source=os.getenv("BOOKINGS_SOURCE", "postgres" if database_url else "sqlite"),
        admin_token=os.getenv("ADMIN_TOKEN", ""),
        # Separate token for DB viewer; managers use ADMIN_TOKEN only
        owner_token=os.getenv("ADMIN_OWNER_TOKEN", ""),
    )


def _use_postgres(config: AdminConfig) -> bool:
    return config.bookings_source == "postgres" and bool(config.database_url)


def _h(value) -> str:
    return html.escape(str(value or ""))


def _parse_int(value, default=0):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _date_to_display(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%d.%m.%Y")
        except ValueError:
            pass
    return value


def _date_to_input(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def _parse_date_for_db(value: str):
    clean = _date_to_display(value)
    if not clean:
        return None
    try:
        return datetime.strptime(clean, "%d.%m.%Y").date()
    except ValueError:
        return None


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _short_dt(value):
    dt = _parse_dt(value)
    if not dt:
        return str(value)[:16] if value else ""
    if dt.tzinfo is None:
        # Postgres TIMESTAMPTZ is UTC under the hood; naive values treat as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK).strftime("%d.%m %H:%M")


def _event_dt_key(date_str: str, time_str: str):
    clean_time = (time_str or "00:00").strip().replace(".", ":")
    for fmt in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(f"{date_str} {clean_time}", fmt)
        except ValueError:
            pass
    return datetime.min


def _format_label(fmt: str) -> str:
    return FORMAT_LABELS.get(fmt, fmt or "")


def _normalize_status(value):
    return value if value in STATUSES else "booked"


def _format_filter_sql(filters: dict, params: dict, include_empty_events: bool) -> str:
    """Restrict admin to проверка/розыгрыш; hide hitloto and other sheet formats."""
    fmt = filters.get("format")
    if fmt in FORMAT_OPTIONS:
        params["format"] = fmt
        if fmt == "rozygrysh":
            # Розыгрыш-брони могут быть на event.format=best — берём по booking.format
            return "b.format = %(format)s"
        if include_empty_events:
            return "(b.format = %(format)s OR (b.id IS NULL AND e.format = %(format)s))"
        return "b.format = %(format)s"

    # «Все форматы» в админке = только проверка + розыгрыш (не hitloto/best без таких броней)
    params["admin_formats"] = list(FORMAT_OPTIONS)
    if include_empty_events:
        return (
            "(b.format = ANY(%(admin_formats)s) "
            "OR (b.id IS NULL AND e.format = ANY(%(admin_formats)s)))"
        )
    return "b.format = ANY(%(admin_formats)s)"


def _fetch_postgres_rows(config: AdminConfig, filters: dict, include_empty_events=False) -> list[dict]:
    where = []
    params = {}
    date_value = _parse_date_for_db(filters.get("date", ""))
    if date_value:
        where.append("e.event_date = %(event_date)s")
        params["event_date"] = date_value
    elif include_empty_events:
        where.append("e.event_date >= CURRENT_DATE")

    format_sql = _format_filter_sql(filters, params, include_empty_events)
    if format_sql:
        where.append(format_sql)
    if not include_empty_events:
        where.append("b.id IS NOT NULL")
    where_sql = "WHERE " + " AND ".join(where) if where else ""

    sql = f"""
        SELECT
            e.id AS event_id,
            e.format AS event_format,
            to_char(e.event_date, 'DD.MM.YYYY') AS event_date,
            to_char(e.event_time, 'HH24:MI') AS event_time,
            e.location,
            e.address,
            e.max_seats,
            b.id AS booking_id,
            b.format AS booking_format,
            b.source,
            b.status,
            b.guests,
            b.created_at::text,
            b.confirmed_at::text,
            b.cancelled_at::text,
            b.annulled_at::text,
            b.updated_at::text,
            b.reminder_24h_sent,
            b.reminder_day_sent,
            u.id AS user_id,
            u.telegram_id,
            u.vk_id,
            u.username,
            u.name,
            u.phone
        FROM events e
        LEFT JOIN bookings b ON b.event_id = e.id
        LEFT JOIN users u ON u.id = b.user_id
        {where_sql}
        ORDER BY e.event_date, e.event_time, e.location, b.created_at DESC NULLS LAST
    """
    with psycopg.connect(config.database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


def _sqlite_columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _fetch_sqlite_rows(config: AdminConfig, filters: dict, include_empty_events=False) -> list[dict]:
    if not os.path.exists(config.db_path):
        return []
    if filters.get("format") and filters["format"] != "proverka":
        return []

    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    try:
        columns = _sqlite_columns(conn, "bookings")
        where = []
        params = []
        date_display = _date_to_display(filters.get("date", ""))
        if date_display:
            where.append("event_date = ?")
            params.append(date_display)
        if filters.get("status"):
            where.append("status = ?")
            params.append(filters["status"])
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        annulled_expr = "annulled_at" if "annulled_at" in columns else "NULL"
        reminder_24h_expr = "reminder_24h_sent" if "reminder_24h_sent" in columns else "0"
        reminder_day_expr = "reminder_day_sent" if "reminder_day_sent" in columns else "0"
        rows = conn.execute(
            f"""
            SELECT
                id AS booking_id,
                telegram_id,
                username,
                name,
                phone,
                event_date,
                event_time,
                event_address AS address,
                event_location AS location,
                guests,
                status,
                created_at,
                {annulled_expr} AS annulled_at,
                {reminder_24h_expr} AS reminder_24h_sent,
                {reminder_day_expr} AS reminder_day_sent
            FROM bookings
            {where_sql}
            ORDER BY event_date, event_time, created_at DESC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        item = dict(row)
        item.update(
            {
                "event_id": f"{item.get('event_date')}|{item.get('event_time')}|{item.get('location')}",
                "event_format": "proverka",
                "booking_format": "proverka",
                "source": "telegram",
                "max_seats": 0,
                "confirmed_at": "",
                "cancelled_at": "",
                "updated_at": item.get("created_at") or "",
                "vk_id": "",
                "user_id": item.get("telegram_id") or "",
            }
        )
        result.append(item)
    return result


def fetch_admin_rows(config: AdminConfig, filters: dict, include_empty_events=False) -> list[dict]:
    if _use_postgres(config):
        rows = _fetch_postgres_rows(config, filters, include_empty_events)
    else:
        rows = _fetch_sqlite_rows(config, filters, include_empty_events)
    status = filters.get("status")
    if status:
        rows = [row for row in rows if row.get("booking_id") is not None and row.get("status") == status]
    return rows


def _safe_table_name(table: str) -> str | None:
    if table in DB_VIEW_TABLES:
        return table
    return None


def list_db_tables(config: AdminConfig) -> list[dict]:
    """Read-only table list with row counts for the DB viewer tab."""
    if _use_postgres(config):
        with psycopg.connect(config.database_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_type = 'BASE TABLE'
                      AND table_name = ANY(%s)
                    ORDER BY table_name
                    """,
                    (list(DB_VIEW_TABLES),),
                )
                names = [row["table_name"] for row in cur.fetchall()]
                result = []
                for name in names:
                    cur.execute(f'SELECT COUNT(*) AS cnt FROM "{name}"')
                    result.append({"name": name, "rows": cur.fetchone()["cnt"]})
                return result

    if not os.path.exists(config.db_path):
        return []
    conn = sqlite3.connect(config.db_path)
    try:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            if row[0] in DB_VIEW_TABLES or row[0] == "bookings"
        ]
        # Local SQLite historically has only bookings
        if not names and "bookings" in {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }:
            names = ["bookings"]
        result = []
        for name in names:
            cnt = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            result.append({"name": name, "rows": cnt})
        return result
    finally:
        conn.close()


def browse_db_table(config: AdminConfig, table: str, page: int = 1) -> dict:
    """Read-only browse of one table: columns + page of rows."""
    safe = _safe_table_name(table)
    if not safe:
        # Allow bookings-only sqlite fallback
        if not _use_postgres(config) and table == "bookings":
            safe = "bookings"
        else:
            return {"table": table, "columns": [], "rows": [], "total": 0, "page": 1, "pages": 1, "error": "Таблица недоступна"}

    page = max(1, _parse_int(page, 1))
    offset = (page - 1) * DB_PAGE_SIZE

    if _use_postgres(config):
        with psycopg.connect(config.database_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s
                    ORDER BY ordinal_position
                    """,
                    (safe,),
                )
                columns = [{"name": r["column_name"], "type": r["data_type"]} for r in cur.fetchall()]
                cur.execute(f'SELECT COUNT(*) AS cnt FROM "{safe}"')
                total = cur.fetchone()["cnt"]
                cur.execute(
                    f'SELECT * FROM "{safe}" ORDER BY 1 DESC NULLS LAST LIMIT %s OFFSET %s',
                    (DB_PAGE_SIZE, offset),
                )
                rows = [dict(r) for r in cur.fetchall()]
    else:
        if not os.path.exists(config.db_path):
            return {"table": safe, "columns": [], "rows": [], "total": 0, "page": 1, "pages": 1, "error": "Файл БД не найден"}
        conn = sqlite3.connect(config.db_path)
        conn.row_factory = sqlite3.Row
        try:
            columns = [
                {"name": row[1], "type": row[2] or ""}
                for row in conn.execute(f'PRAGMA table_info("{safe}")').fetchall()
            ]
            total = conn.execute(f'SELECT COUNT(*) FROM "{safe}"').fetchone()[0]
            rows = [
                dict(r)
                for r in conn.execute(
                    f'SELECT * FROM "{safe}" ORDER BY rowid DESC LIMIT ? OFFSET ?',
                    (DB_PAGE_SIZE, offset),
                ).fetchall()
            ]
        finally:
            conn.close()

    pages = max(1, (total + DB_PAGE_SIZE - 1) // DB_PAGE_SIZE)
    return {
        "table": safe,
        "columns": columns,
        "rows": rows,
        "total": total,
        "page": min(page, pages),
        "pages": pages,
        "error": "",
    }


def _booking_from_row(row: dict, event: dict | None = None) -> dict:
    status = _normalize_status(row.get("status"))
    changed_at = (
        row.get("cancelled_at")
        or row.get("annulled_at")
        or row.get("confirmed_at")
        or row.get("updated_at")
        or row.get("created_at")
    )
    event_id = ""
    if event and event.get("id") is not None:
        event_id = str(event["id"])
    elif row.get("event_id") is not None:
        event_id = str(row.get("event_id"))
    return {
        "id": row.get("booking_id"),
        "user_id": row.get("user_id") or "",
        "event_id": event_id,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "guests": _parse_int(row.get("guests")),
        "source": row.get("source") or "",
        "format": row.get("booking_format") or row.get("event_format") or "",
        "created_at": _short_dt(row.get("created_at")),
        "changed_at": _short_dt(changed_at),
        "created_at_raw": row.get("created_at") or "",
        "changed_at_raw": changed_at or "",
        "event_date": row.get("event_date") or "",
        "event_time": row.get("event_time") or "",
        "location": row.get("location") or "",
        "address": row.get("address") or "",
        "name": row.get("name") or "",
        "username": row.get("username") or "",
        "phone": row.get("phone") or "",
        "telegram_id": row.get("telegram_id") or "",
        "vk_id": row.get("vk_id") or "",
        "reminder_24h_sent": bool(row.get("reminder_24h_sent")),
        "reminder_day_sent": bool(row.get("reminder_day_sent")),
        "event": event,
    }


def build_dashboard(rows: list[dict]) -> dict:
    events = {}
    bookings = []
    users = {}
    totals = {"events": 0, "bookings": 0, "reserved_guests": 0, "confirmed_guests": 0}

    for row in rows:
        event_id = str(row.get("event_id") or "unknown")
        event = events.setdefault(
            event_id,
            {
                "id": event_id,
                "format": row.get("event_format") or "",
                "date": row.get("event_date") or "",
                "time": row.get("event_time") or "",
                "location": row.get("location") or "",
                "address": row.get("address") or "",
                "max_seats": _parse_int(row.get("max_seats")),
                "bookings": [],
                "status_counts": defaultdict(int),
                "status_guests": defaultdict(int),
                "reserved_guests": 0,
                "confirmed_guests": 0,
            },
        )
        if not row.get("booking_id"):
            continue

        booking = _booking_from_row(row, event)
        status = booking["status"]
        guests = booking["guests"]
        event["bookings"].append(booking)
        event["status_counts"][status] += 1
        event["status_guests"][status] += guests
        if status in ACTIVE_STATUSES:
            event["reserved_guests"] += guests
            totals["reserved_guests"] += guests
        if status == "confirmed":
            event["confirmed_guests"] += guests
            totals["confirmed_guests"] += guests
        totals["bookings"] += 1
        bookings.append(booking)

        user_key = str(
            booking["user_id"]
            or booking["telegram_id"]
            or booking["vk_id"]
            or booking["phone"]
            or booking["name"]
            or booking["id"]
        )
        user = users.setdefault(
            user_key,
            {
                "key": user_key,
                "user_id": booking["user_id"],
                "name": booking["name"],
                "username": booking["username"],
                "phone": booking["phone"],
                "telegram_id": booking["telegram_id"],
                "vk_id": booking["vk_id"],
                "source": booking["source"],
                "bookings": [],
                "status_counts": Counter(),
                "guests_confirmed": 0,
                "guests_reserved": 0,
            },
        )
        if booking["user_id"] and not user.get("user_id"):
            user["user_id"] = booking["user_id"]
        user["bookings"].append(booking)
        user["status_counts"][status] += 1
        if status in ACTIVE_STATUSES:
            user["guests_reserved"] += guests
        if status == "confirmed":
            user["guests_confirmed"] += guests

    for event in events.values():
        event["bookings"].sort(key=lambda b: (b["changed_at"], str(b["id"])), reverse=True)
    bookings.sort(key=lambda b: (b["event_date"], b["event_time"], str(b["id"])), reverse=True)
    for user in users.values():
        user["bookings"].sort(key=lambda b: (b["event_date"], b["event_time"], str(b["id"])), reverse=True)
    totals["events"] = len(events)
    return {"events": list(events.values()), "bookings": bookings, "users": users, "totals": totals}


def _query_link(filters: dict, **updates) -> str:
    next_filters = {k: v for k, v in filters.items() if v and k != "token"}
    for key, value in updates.items():
        if value:
            next_filters[key] = value
        else:
            next_filters.pop(key, None)
    return "/admin" + ("?" + urlencode(next_filters) if next_filters else "")


def _status_badge(status: str) -> str:
    color = STATUS_COLORS.get(status, "#64748b")
    return f'<span class="badge" style="background:{color}">{_h(STATUS_LABELS.get(status, status))}</span>'


def _status_bar(event: dict) -> str:
    total = sum(event["status_counts"].values())
    if total <= 0:
        return '<div class="status-bar empty"></div>'
    parts = []
    for status in STATUSES:
        count = event["status_counts"].get(status, 0)
        if not count:
            continue
        width = max(5, count / total * 100)
        parts.append(
            f'<span title="{_h(STATUS_LABELS[status])}: {count}" '
            f'style="width:{width:.1f}%;background:{STATUS_COLORS[status]}"></span>'
        )
    return f'<div class="status-bar">{"".join(parts)}</div>'


def _tracks_seats(event: dict) -> bool:
    """Seat capacity is only meaningful for проверка материала."""
    return (event.get("format") or "") == "proverka"


def _seat_bar(event: dict) -> str:
    reserved = event["reserved_guests"]
    confirmed = event["confirmed_guests"]
    if not _tracks_seats(event):
        # Розыгрыш / BEST / Hit Lotto — места не считаем
        return f'<div class="active-bookings">Активные брони: <b>{reserved} чел</b></div>'
    max_seats = event["max_seats"]
    if max_seats <= 0:
        return f'<div class="capacity muted">Активные брони: {reserved} чел. Лимит мест не указан.</div>'
    free = max(0, max_seats - confirmed)
    percent = min(100, confirmed / max_seats * 100)
    return (
        f'<div class="capacity-line"><span>Места заняты билетами: {confirmed}/{max_seats}</span>'
        f'<span>{free} свободно</span></div>'
        f'<div class="capacity-bar"><span style="width:{percent:.1f}%"></span></div>'
        f'<div class="active-bookings">Активные брони: <b>{reserved} чел</b></div>'
    )


def _sort_header(label: str, key: str, filters: dict, sortable: bool) -> str:
    if not sortable:
        return f"<th>{_h(label)}</th>"
    current = filters.get("sort") or ""
    order = filters.get("order") or "asc"
    if current == key:
        next_order = "desc" if order == "asc" else "asc"
        mark = " ▲" if order == "asc" else " ▼"
    else:
        next_order = "asc"
        mark = ""
    href = _query_link(filters, sort=key, order=next_order)
    return (
        f'<th class="sortable"><a href="{href}">{_h(label)}'
        f'<span class="sort-mark">{mark}</span></a></th>'
    )


def _sort_bookings(bookings: list[dict], filters: dict) -> list[dict]:
    sort_key = filters.get("sort") or ""
    if sort_key not in BOOKING_SORT_OPTIONS:
        return bookings
    reverse = (filters.get("order") or "asc") == "desc"

    def key_fn(booking: dict):
        if sort_key == "user_id":
            return (_parse_int(booking.get("user_id")), str(booking.get("user_id") or ""))
        if sort_key == "status":
            status = booking.get("status")
            return (STATUSES.index(status) if status in STATUSES else 99, status or "")
        if sort_key == "date":
            return (_event_dt_key(booking.get("event_date") or "", booking.get("event_time") or ""),)
        if sort_key == "location":
            return ((booking.get("location") or "").lower(), (booking.get("address") or "").lower())
        if sort_key in ("created", "changed"):
            raw = booking.get("created_at_raw" if sort_key == "created" else "changed_at_raw")
            dt = _parse_dt(raw)
            if dt is None:
                dt = datetime.min.replace(tzinfo=timezone.utc)
            elif dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (dt,)
        return (0,)

    return sorted(bookings, key=key_fn, reverse=reverse)


def _booking_table(
    bookings: list[dict],
    compact=False,
    show_format=False,
    filters: dict | None = None,
    sortable=False,
) -> str:
    if not bookings:
        return '<p class="muted">Броней пока нет.</p>'
    filters = filters or {}
    rows = []
    for booking in bookings:
        contact = _h(booking["phone"])
        if booking["username"]:
            contact += f'<br><span class="muted">@{_h(booking["username"])}</span>'
        event_cols = ""
        if not compact:
            event_cols = (
                f"<td>{_h(booking['event_date'])}<br><span class='muted'>{_h(booking['event_time'])}</span></td>"
                f"<td class='loc'>{_h(booking['location'])}<br><span class='muted'>{_h(booking['address'])}</span></td>"
            )
        status_cell = _status_badge(booking["status"])
        if show_format and booking.get("format"):
            status_cell += f'<br><span class="format-tag">{_h(_format_label(booking["format"]))}</span>'
        user_id = booking.get("user_id") or "—"
        rows.append(
            "<tr>"
            f"<td>{_h(user_id)}</td>"
            f"<td>{status_cell}</td>"
            f"<td><b>{_h(booking['name'])}</b><br><span class='muted'>{_h(booking['source'])}</span></td>"
            f"<td>{contact}</td>"
            f"<td>{_h(booking['guests'])}</td>"
            f"{event_cols}"
            f"<td>{_h(booking['created_at'])}</td>"
            f"<td>{_h(booking['changed_at'])}</td>"
            "</tr>"
        )
    if compact:
        headers = (
            f"{_sort_header('user_id', 'user_id', filters, False)}"
            f"{_sort_header('Статус', 'status', filters, False)}"
            "<th>Клиент</th><th>Контакт</th><th>Гости</th>"
            f"{_sort_header('Создана', 'created', filters, False)}"
            f"{_sort_header('Изменена', 'changed', filters, False)}"
        )
        table_class = "bookings compact"
    else:
        headers = (
            f"{_sort_header('user_id', 'user_id', filters, sortable)}"
            f"{_sort_header('Статус', 'status', filters, sortable)}"
            "<th>Клиент</th><th>Контакт</th><th>Гости</th>"
            f"{_sort_header('Дата', 'date', filters, sortable)}"
            f"{_sort_header('Локация', 'location', filters, sortable)}"
            f"{_sort_header('Создана', 'created', filters, sortable)}"
            f"{_sort_header('Изменена', 'changed', filters, sortable)}"
        )
        table_class = "bookings"
    return (
        f'<div class="table-wrap"><table class="{table_class}"><thead><tr>{headers}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _event_format_badge(event: dict) -> str:
    """Show only known admin labels; hide raw codes like best/hitloto."""
    label = FORMAT_LABELS.get(event.get("format") or "")
    if not label:
        return ""
    return f'<span class="format">{_h(label)}</span>'


def _event_card(event: dict) -> str:
    counts = " ".join(
        f'<span class="counter">{_h(STATUS_LABELS[s])}: <b>{event["status_counts"].get(s, 0)}</b></span>'
        for s in STATUSES
    )
    return (
        '<section class="card">'
        '<div class="event-head">'
        f'<div><h2>{_h(event["date"])} в {_h(event["time"])} · {_h(event["location"])}</h2>'
        f'<p>{_h(event["address"])}</p></div>'
        f'{_event_format_badge(event)}'
        '</div>'
        f'{_seat_bar(event)}'
        f'{_status_bar(event)}'
        f'<div class="counters">{counts}</div>'
        f'{_booking_table(event["bookings"], compact=True)}'
        '</section>'
    )


def _tabs(filters: dict, can_view_db: bool = False) -> str:
    tabs = [
        ("date", "По дате"),
        ("bookings", "Все брони"),
        ("users", "Users"),
        ("analytics", "Аналитика"),
    ]
    if can_view_db:
        tabs.append(("db", "База"))
    current = filters.get("tab") or "date"
    return "".join(
        f'<a class="tab {"active" if current == key else ""}" '
        f'href="{_query_link(filters, tab=key, date="", event="", u="", table="", page="", sort="", order="", date_from="", date_to="", channel="")}">{label}</a>'
        for key, label in tabs
    )


def _event_label(event: dict) -> str:
    parts = [event.get("time") or "—", event.get("location") or "Без площадки"]
    fmt = FORMAT_LABELS.get(event.get("format") or "")
    if not fmt:
        for booking in event.get("bookings") or []:
            fmt = FORMAT_LABELS.get(booking.get("format") or "")
            if fmt:
                break
    if fmt:
        parts.append(fmt)
    return " · ".join(parts)


def _event_matches_admin_format(event: dict, fmt: str = "") -> bool:
    """True if event belongs in admin for the selected format filter."""
    wanted = (fmt,) if fmt in FORMAT_OPTIONS else FORMAT_OPTIONS
    if event.get("format") in wanted:
        return True
    return any(b.get("format") in wanted for b in event.get("bookings") or [])


def _events_for_filter(dashboard: dict, filters: dict) -> list[dict]:
    """Events available for the show picker (current date + optional format)."""
    if not filters.get("date"):
        return []
    fmt = filters.get("format") or ""
    events = [
        event
        for event in (dashboard.get("events") or [])
        if _event_matches_admin_format(event, fmt)
    ]
    events.sort(key=lambda e: (_event_dt_key(e.get("date") or "", e.get("time") or ""), e.get("location") or ""))
    return events


def _normalize_event_filter(dashboard: dict, filters: dict) -> dict:
    """Drop stale event id when date/format no longer matches."""
    next_filters = dict(filters)
    if not next_filters.get("date"):
        next_filters["event"] = ""
        return next_filters
    allowed = {str(event["id"]) for event in _events_for_filter(dashboard, next_filters)}
    if next_filters.get("event") not in allowed:
        next_filters["event"] = ""
    return next_filters


def _event_select(dashboard: dict, filters: dict) -> str:
    events = _events_for_filter(dashboard, filters)
    if not filters.get("date"):
        return ""
    if not events:
        return '<select name="event" disabled><option value="">Нет шоу на эту дату</option></select>'
    options = ['<option value="">Все шоу</option>']
    selected = filters.get("event") or ""
    for event in events:
        eid = str(event["id"])
        mark = "selected" if selected == eid else ""
        options.append(f'<option value="{_h(eid)}" {mark}>{_h(_event_label(event))}</option>')
    return f'<select name="event">{"".join(options)}</select>'


def _cell_value(value) -> str:
    if value is None:
        return '<span class="muted">NULL</span>'
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if len(text) > 120:
        text = text[:117] + "..."
    return _h(text)


def _db_tab(tables: list[dict], browse: dict | None, filters: dict) -> str:
    table_links = []
    for item in tables:
        active = "active" if filters.get("table") == item["name"] else ""
        href = _query_link(filters, tab="db", table=item["name"], page="1")
        table_links.append(
            f'<a class="pill {active}" href="{href}">{_h(item["name"])} '
            f'<span class="muted">({item["rows"]})</span></a>'
        )
    links_html = "".join(table_links) or '<span class="muted">Таблиц не найдено</span>'
    nav = (
        '<section class="card">'
        "<h2>Таблицы базы</h2>"
        '<p class="muted">Только просмотр. Изменять данные здесь нельзя.</p>'
        f'<div class="counters">{links_html}</div>'
        "</section>"
    )
    if not browse:
        return nav + (
            '<section class="card empty-state">'
            "<h2>Выберите таблицу</h2>"
            "<p>Нажмите на таблицу выше, чтобы увидеть строки как в Excel.</p>"
            "</section>"
        )
    if browse.get("error"):
        return nav + f'<section class="card empty-state"><h2>{_h(browse["error"])}</h2></section>'

    cols = browse["columns"]
    col_meta = " · ".join(f'{c["name"]} ({c["type"]})' for c in cols)
    headers = "".join(f"<th>{_h(c['name'])}</th>" for c in cols)
    body_rows = []
    for row in browse["rows"]:
        cells = "".join(f"<td>{_cell_value(row.get(c['name']))}</td>" for c in cols)
        body_rows.append(f"<tr>{cells}</tr>")
    if not body_rows:
        body_rows.append(f'<tr><td colspan="{max(1, len(cols))}" class="muted">Пусто</td></tr>')

    page = browse["page"]
    pages = browse["pages"]
    prev_link = (
        f'<a class="pill" href="{_query_link(filters, tab="db", table=browse["table"], page=str(page - 1))}">← Назад</a>'
        if page > 1
        else ""
    )
    next_link = (
        f'<a class="pill" href="{_query_link(filters, tab="db", table=browse["table"], page=str(page + 1))}">Вперёд →</a>'
        if page < pages
        else ""
    )
    pager = (
        f'<div class="mini-metrics">'
        f'<span>Строк: <b>{browse["total"]}</b></span>'
        f'<span>Страница: <b>{page}/{pages}</b></span>'
        f"{prev_link}{next_link}"
        f"</div>"
    )
    return (
        nav
        + '<section class="card">'
        f'<h2>{_h(browse["table"])}</h2>'
        f'<p class="muted">{_h(col_meta)}</p>'
        f"{pager}"
        f'<div class="table-wrap"><table><thead><tr>{headers}</tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody></table></div>'
        "</section>"
    )


def _format_select(filters: dict) -> str:
    options = ['<option value="">Все форматы</option>']
    for fmt in FORMAT_OPTIONS:
        selected = "selected" if filters.get("format") == fmt else ""
        options.append(
            f'<option value="{fmt}" {selected}>{_h(FORMAT_LABELS.get(fmt, fmt))}</option>'
        )
    return "".join(options)


def _status_filter(filters: dict) -> str:
    links = [f'<a class="pill {"active" if not filters.get("status") else ""}" href="{_query_link(filters, status="")}">Все статусы</a>']
    for status in STATUSES:
        links.append(
            f'<a class="pill {"active" if filters.get("status") == status else ""}" '
            f'href="{_query_link(filters, status=status)}">{_h(STATUS_LABELS[status])}</a>'
        )
    return "".join(links)


def _date_tab(dashboard: dict, filters: dict) -> str:
    date_value = filters.get("date", "")
    if not date_value:
        return '<section class="card empty-state"><h2>Выберите дату</h2><p>Выберите дату в календаре выше, чтобы посмотреть брони по мероприятиям.</p></section>'
    events = list(dashboard["events"])
    if filters.get("event"):
        events = [event for event in events if str(event["id"]) == filters["event"]]
    events_with_bookings = [event for event in events if event["bookings"]]
    if not events_with_bookings:
        if filters.get("event"):
            return '<section class="card empty-state"><h2>На это шоу пока нет бронирований</h2><p>Выберите другое шоу или сбросьте фильтр.</p></section>'
        return '<section class="card empty-state"><h2>Пока нет бронирования на указанную дату</h2><p>На эту дату пока не создано ни одной брони.</p></section>'
    return "".join(_event_card(event) for event in events_with_bookings)


def _bookings_tab(dashboard: dict, filters: dict) -> str:
    bookings = dashboard["bookings"]
    if filters.get("event"):
        bookings = [b for b in bookings if str(b.get("event_id")) == filters["event"]]
    bookings = _sort_bookings(bookings, filters)
    by_format = defaultdict(list)
    for booking in bookings:
        by_format[booking["format"]].append(booking)
    sections = []
    for fmt, title in (("proverka", "Проверка материала"), ("rozygrysh", "Розыгрыш")):
        if filters.get("format") and filters["format"] != fmt:
            continue
        sections.append(
            f'<section class="card"><h2>{title}</h2>'
            f'{_booking_table(by_format.get(fmt, []), filters=filters, sortable=True)}</section>'
        )
    if not sections:
        return '<section class="card empty-state"><h2>Броней пока нет</h2></section>'
    return "".join(sections)


def _user_stage(user: dict) -> str:
    counts = user["status_counts"]
    if counts.get("confirmed"):
        return "Есть полученный билет"
    if counts.get("booked"):
        return "Есть активная бронь, билет не получен"
    if counts.get("cancelled"):
        return "Отменял бронь"
    if counts.get("annulled"):
        return "Бронь аннулировалась"
    return "Нет активного этапа"


def _fmt_msk(dt) -> str:
    if not dt:
        return "—"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return dt
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(MSK).replace(tzinfo=None)
    return dt.strftime("%d.%m.%Y %H:%M")


def _activity_label(name: str, props: dict | None) -> str:
    props = props or {}
    labels = {
        "bot_start": "Вход в бот (/start)",
        "help_open": "Help / FAQ",
        "help_question": "Написали в поддержку",
        "branch_proverka": "Раздел · Проверка",
        "branch_best": "Раздел · BEST",
        "branch_hitloto": "Раздел · Hit Loto",
        "show_card": "Карточка концерта",
        "raffle_enter": "Розыгрыш · вход",
        "raffle_branch": "Розыгрыш · выбор ветки",
        "raffle_screenshot": "Розыгрыш · отправили скрин",
        "raffle_approved": "Розыгрыш · скрин принят",
        "raffle_rejected": "Розыгрыш · скрин отклонён",
        "raffle_subscribed": "Розыгрыш · подписка ок",
        "raffle_sub_failed": "Розыгрыш · подписка нет",
        "booking_created": "Бронь создана",
        "booking_confirmed": "Билет получен",
        "booking_cancelled": "Бронь отменена",
        "booking_annulled": "Бронь аннулирована",
        "bot_blocked": "Заблокировали бота",
        "bot_unblocked": "Разблокировали бота",
    }
    base = labels.get(name, name)
    extras = []
    if name == "raffle_branch" and props.get("kind"):
        kind = "пост" if props.get("kind") == "post" else "отзыв" if props.get("kind") == "review" else props.get("kind")
        extras.append(str(kind))
    if name == "show_card":
        fmt = props.get("format") or ""
        browse = props.get("browse") or ""
        fmt_l = {"best": "BEST", "hitloto": "Hit Loto", "proverka": "Проверка"}.get(fmt, fmt)
        browse_l = {"date": "по дате", "venue": "по площадке"}.get(browse, browse)
        if fmt_l:
            extras.append(fmt_l)
        if browse_l:
            extras.append(browse_l)
        if props.get("date"):
            extras.append(str(props.get("date")))
    if name == "bot_start" and props.get("payload"):
        extras.append(str(props.get("payload")))
    if name == "booking_created" and props.get("format"):
        extras.append(_format_label(props.get("format")))
    return base + (f" · {' · '.join(extras)}" if extras else "")


def _user_reminders_html(bookings: list[dict]) -> str:
    from bot.utils.reminder_schedule import plan_booking_reminders
    from bot.utils.ticket import parse_created_at, parse_event_datetime

    now = datetime.now(MSK).replace(tzinfo=None)
    # Show active + confirmed (ticket taken) so raffle/shows with immediate ticket are visible.
    relevant = [
        b
        for b in bookings
        if b.get("status") in {"booked", "confirmed"}
    ]
    if not relevant:
        return '<p class="muted">Нет броней для напоминаний.</p>'

    # Newest event date first
    relevant = sorted(
        relevant,
        key=lambda b: (b.get("event_date") or "", b.get("event_time") or ""),
        reverse=True,
    )
    lines = []
    for booking in relevant:
        fmt = _format_label(booking.get("format") or "")
        date_label = booking.get("event_date") or "—"
        title = f"{date_label} {booking.get('event_time') or ''} · {fmt}".strip()
        status = booking.get("status")

        if status == "confirmed":
            lines.append(
                f"<div class='user-block-item'><b>{_h(title)}</b>"
                f"<p class='muted'>Билет получен — запланированных напоминаний по событию нет.</p></div>"
            )
            continue

        event_dt = parse_event_datetime(booking.get("event_date") or "", booking.get("event_time") or "")
        if not event_dt:
            continue
        created_at = parse_created_at(booking.get("created_at_raw") or booking.get("created_at"))
        plan = plan_booking_reminders(created_at, event_dt)

        def _line(label: str, when, sent: bool) -> str:
            if when is None:
                return f"<li>{_h(label)}: не планируется</li>"
            state = "отправлено" if sent else ("запланировано" if when >= now else "время прошло, не отмечено")
            return (
                f"<li>{_h(label)}: <b>{_h(_fmt_msk(when))}</b>"
                f" <span class='muted'>({_h(state)})</span></li>"
            )

        lines.append(
            f"<div class='user-block-item'><b>{_h(title)}</b><ul>"
            f"{_line('Напоминание за сутки (в 14:00 накануне)', plan['reminder_24h_at'], bool(booking.get('reminder_24h_sent')))}"
            f"{_line('Напоминание в день шоу (если билет не забран)', plan['reminder_day_at'], bool(booking.get('reminder_day_sent')))}"
            f"{_line('Аннулирование, если билет так и не получен', plan['annul_at'], False)}"
            "</ul></div>"
        )
    return "".join(lines) if lines else '<p class="muted">Нет броней для напоминаний.</p>'


def _user_activity_html(activity_counts: list[dict]) -> str:
    if not activity_counts:
        return '<p class="muted">Пока нет событий аналитики по этому гостю.</p>'

    order = [
        "bot_start",
        "branch_proverka",
        "branch_best",
        "branch_hitloto",
        "show_card",
        "raffle_enter",
        "raffle_branch",
        "raffle_screenshot",
        "raffle_approved",
        "raffle_rejected",
        "raffle_subscribed",
        "raffle_sub_failed",
        "booking_created",
        "booking_confirmed",
        "booking_cancelled",
        "booking_annulled",
        "help_open",
        "help_question",
        "bot_blocked",
        "bot_unblocked",
    ]
    short_labels = {
        "bot_start": "Вход в бот",
        "branch_proverka": "Ветка · Проверка",
        "branch_best": "Ветка · BEST",
        "branch_hitloto": "Ветка · Hit Loto",
        "show_card": "Карточка концерта",
        "raffle_enter": "Розыгрыш · вход",
        "raffle_branch": "Розыгрыш · выбор ветки",
        "raffle_screenshot": "Розыгрыш · отправили скрин",
        "raffle_approved": "Розыгрыш · скрин принят",
        "raffle_rejected": "Розыгрыш · скрин отклонён",
        "raffle_subscribed": "Розыгрыш · подписка ок",
        "raffle_sub_failed": "Розыгрыш · подписка нет",
        "booking_created": "Бронь создана",
        "booking_confirmed": "Билет получен",
        "booking_cancelled": "Бронь отменена",
        "booking_annulled": "Бронь аннулирована",
        "help_open": "Help / FAQ",
        "help_question": "Обращение в поддержку",
        "bot_blocked": "Заблокировали бота",
        "bot_unblocked": "Разблокировали бота",
    }
    counts = {
        row.get("name"): int(row.get("events") or 0)
        for row in activity_counts
        if row.get("name")
    }
    rows = []
    seen = set()
    for name in order:
        if name not in counts:
            continue
        seen.add(name)
        n = counts[name]
        word = "заход" if n == 1 else "захода" if 2 <= n <= 4 else "заходов"
        rows.append(
            "<tr>"
            f"<td>{_h(short_labels.get(name, name))}</td>"
            f"<td><b>{n}</b> <span class='muted'>{word}</span></td>"
            "</tr>"
        )
    for name, n in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        if name in seen:
            continue
        word = "заход" if n == 1 else "захода" if 2 <= n <= 4 else "заходов"
        rows.append(
            "<tr>"
            f"<td>{_h(short_labels.get(name, name))}</td>"
            f"<td><b>{n}</b> <span class='muted'>{word}</span></td>"
            "</tr>"
        )
    return (
        '<div class="table-wrap"><table class="user-extra user-activity-summary">'
        "<thead><tr><th>Событие</th><th>Сколько раз</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _user_raffle_html(submissions: list[dict], flags: dict) -> str:
    used = bool(flags.get("rozygrysh_used"))
    used_html = (
        '<p><span class="badge" style="background:#64748b">розыгрыш использован</span></p>'
        if used
        else '<p><span class="badge" style="background:#22c55e">розыгрыш ещё не использован</span></p>'
    )
    if not submissions:
        return used_html + '<p class="muted">Заявок со скринами пока нет.</p>'
    kind_labels = {"post": "пост", "review": "отзыв"}
    status_labels = {
        "pending": "на модерации",
        "approved": "принят",
        "rejected": "отклонён",
    }
    status_colors = {
        "pending": "#f59e0b",
        "approved": "#22c55e",
        "rejected": "#ef4444",
    }
    cards = []
    total = len(submissions)
    for idx, row in enumerate(submissions):
        kind = kind_labels.get(row.get("kind"), row.get("kind") or "—")
        status = row.get("status") or ""
        status_l = status_labels.get(status, status or "—")
        color = status_colors.get(status, "#64748b")
        created = _fmt_msk(row.get("created_at"))
        reviewed = _fmt_msk(row.get("reviewed_at")) if row.get("reviewed_at") else ""
        bits = [f"Создана {created}"]
        if reviewed:
            bits.append(f"Решение {reviewed}")
        if row.get("reject_reason"):
            bits.append(f"Причина: {row.get('reject_reason')}")
        if row.get("source_message_id"):
            bits.append(f"Сообщение в чате #{row.get('source_message_id')}")
        cards.append(
            f'<article class="screen-card" data-idx="{idx}">'
            f'<div class="screen-card-top">'
            f'<span class="screen-card-title">Скрин #{_h(str(row.get("id")))} · {_h(kind)}</span>'
            f'<span class="badge" style="background:{color}">{_h(status_l)}</span>'
            f"</div>"
            f'<p class="muted screen-card-meta">{_h(" · ".join(bits))}</p>'
            f'<div class="screen-card-nav muted">{idx + 1} / {total}</div>'
            "</article>"
        )
    return (
        used_html
        + '<div class="screen-carousel" tabindex="0">'
        + "".join(cards)
        + "</div>"
        + '<p class="muted screen-carousel-hint">Листайте карточки скринов вбок</p>'
    )


def _user_extra_details(title: str, body: str, open_by_default: bool = False) -> str:
    opened = " open" if open_by_default else ""
    return (
        f'<details class="user-extra-details"{opened}>'
        f'<summary class="user-extra-summary"><strong>{_h(title)}</strong>'
        '<span class="details-action"><span class="closed-label">Развернуть</span>'
        '<span class="open-label">Свернуть</span></span></summary>'
        f'<div class="user-extra-body">{body}</div>'
        "</details>"
    )


def _users_tab(dashboard: dict, filters: dict, user_extras: dict | None = None) -> str:
    status = filters.get("status") or ""
    users = sorted(
        dashboard["users"].values(),
        key=lambda u: (_parse_int(u.get("user_id")), u["name"] or "", u["phone"] or ""),
    )
    selected_key = filters.get("u", "")
    list_users = users
    if status:
        list_users = [user for user in users if user["status_counts"].get(status, 0) > 0]
        # Keep the opened guest in the list even if they have 0 bookings of this status.
        if selected_key and selected_key in dashboard["users"]:
            selected = dashboard["users"][selected_key]
            if all(user["key"] != selected_key for user in list_users):
                list_users = [selected] + list_users
    rows = []
    for user in list_users:
        rows.append(
            "<tr>"
            f"<td>{_h(user.get('user_id') or '—')}</td>"
            f"<td><a href='{_query_link(filters, u=user['key'])}'>{_h(user['name'] or 'Без имени')}</a>"
            f"<br><span class='muted'>{_h(user['source'])}</span></td>"
            f"<td>{_h(user['phone'])}<br><span class='muted'>@{_h(user['username'])}</span></td>"
            f"<td>{len(user['bookings'])}</td>"
            f"<td>{user['status_counts'].get('booked', 0)}</td>"
            f"<td>{user['status_counts'].get('confirmed', 0)}</td>"
            f"<td>{user['status_counts'].get('cancelled', 0)}</td>"
            f"<td>{_h(_user_stage(user))}</td>"
            "</tr>"
        )
    table = (
        '<div class="table-wrap"><table class="users">'
        "<thead><tr><th>user_id</th><th>Клиент</th><th>Контакт</th><th>Всего</th><th>Активные</th>"
        "<th>Билеты</th><th>Отмены</th><th>Этап</th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=\"8\" class=\"muted\">Пользователей пока нет</td></tr>'}</tbody>"
        "</table></div>"
    )
    detail = ""
    if selected_key and selected_key in dashboard["users"]:
        user = dashboard["users"][selected_key]
        bookings = user["bookings"]
        if status:
            bookings = [booking for booking in bookings if booking.get("status") == status]
        reminders_24h = sum(1 for b in user["bookings"] if b["reminder_24h_sent"])
        reminders_day = sum(1 for b in user["bookings"] if b["reminder_day_sent"])
        empty_note = ""
        if status and not bookings:
            empty_note = (
                f'<p class="muted">У этого гостя нет броней со статусом '
                f'«{_h(STATUS_LABELS.get(status, status))}».</p>'
            )
        detail = (
            '<section class="card user-detail">'
            f'<h2>{_h(user["name"] or "Без имени")}</h2>'
            f'<p class="muted">user_id: {_h(user.get("user_id") or "—")} · {_h(user["phone"])} · '
            f'@{_h(user["username"])} · источник: {_h(user["source"])}</p>'
            '<div class="mini-metrics">'
            f'<span>Всего броней: <b>{len(user["bookings"])}</b></span>'
            f'<span>Активных: <b>{user["status_counts"].get("booked", 0)}</b></span>'
            f'<span>Билетов: <b>{user["status_counts"].get("confirmed", 0)}</b></span>'
            f'<span>Отмен: <b>{user["status_counts"].get("cancelled", 0)}</b></span>'
            f'<span>Напоминание за сутки: <b>{reminders_24h}</b></span>'
            f'<span>Напоминание в день: <b>{reminders_day}</b></span>'
            '</div>'
            f'<p><b>Текущий этап:</b> {_h(_user_stage(user))}</p>'
            f"{empty_note}"
            f'{_booking_table(bookings, show_format=True)}'
        )
        extras = user_extras or {}
        detail += (
            '<div class="user-extra-stack">'
            f'{_user_extra_details("Куда заходил", _user_activity_html(extras.get("activity_counts") or []))}'
            f'{_user_extra_details("Розыгрыш", _user_raffle_html(extras.get("submissions") or [], extras.get("flags") or {}))}'
            f'{_user_extra_details("Напоминания", _user_reminders_html(user["bookings"]))}'
            "</div>"
            "</section>"
        )
    return detail + f'<section class="card"><h2>Users</h2>{table}</section>'


def _metric_pair(metric: dict | None) -> str:
    metric = metric or {"events": 0, "uniques": 0}
    return f'<b>{metric.get("events", 0)}</b><span class="muted"> · {metric.get("uniques", 0)} чел.</span>'


def _analytics_metric_card(title: str, metric: dict | None, note: str = "", css_class: str = "") -> str:
    metric = metric or {"events": 0, "uniques": 0}
    note_html = f'<span class="muted">{_h(note)}</span>' if note else ""
    cls = f"metric {css_class}".strip()
    return (
        f'<div class="{cls}">'
        f"<span>{_h(title)}</span>"
        f'<b>{metric.get("events", 0)}</b>'
        f'<small class="muted">{metric.get("uniques", 0)} уникальных</small>'
        f"{note_html}"
        "</div>"
    )


def _analytics_tab(report: dict, filters: dict) -> str:
    if not report.get("available"):
        return (
            '<section class="card empty-state">'
            "<h2>Аналитика пока недоступна</h2>"
            "<p>Нужен PostgreSQL и таблица analytics_events. "
            "После деплоя трекинга сюда подтянутся цифры.</p>"
            "</section>"
        )

    by_name = report.get("by_name") or {}
    today = datetime.now(MSK).date()
    today_s = today.strftime("%Y-%m-%d")
    week_s = (today - timedelta(days=6)).strftime("%Y-%m-%d")
    channel = filters.get("channel") or ""
    date_from = _date_to_input(filters.get("date_from", ""))
    date_to = _date_to_input(filters.get("date_to", ""))

    presets = [
        ("Сегодня", {"date_from": today_s, "date_to": today_s, "all": ""}),
        ("7 дней", {"date_from": week_s, "date_to": today_s, "all": ""}),
        ("Весь период", {"date_from": "", "date_to": "", "all": "1"}),
    ]
    preset_html = "".join(
        f'<a class="pill" href="{_query_link(filters, tab="analytics", channel=channel, **preset)}">{label}</a>'
        for label, preset in presets
    )

    channel_links = []
    for key, label in (("", "Все каналы"), ("telegram", "Telegram"), ("vkontakte", "VK")):
        active = "active" if (filters.get("channel") or "") == key else ""
        channel_links.append(
            f'<a class="pill {active}" href="{_query_link(filters, tab="analytics", channel=key)}">{label}</a>'
        )

    proverka_overview = report.get("proverka_bookings") or {}
    raffle_overview = report.get("raffle_bookings") or {}
    overview = (
        '<div class="summary analytics-summary">'
        f'{_analytics_metric_card("Зашли в бот", by_name.get("bot_start"))}'
        f'{_analytics_metric_card("Зашли · Проверка", by_name.get("branch_proverka"), css_class="tone-proverka")}'
        f'{_analytics_metric_card("Зашли · BEST", by_name.get("branch_best"), css_class="tone-best")}'
        f'{_analytics_metric_card("Зашли · Hit Loto", by_name.get("branch_hitloto"), css_class="tone-hitloto")}'
        f'{_analytics_metric_card("Help / FAQ · обращение", by_name.get("help_open"))}'
        f'{_analytics_metric_card("Брони созданы · проверка", proverka_overview.get("created"))}'
        f'{_analytics_metric_card("Билет получен · проверка", proverka_overview.get("confirmed"))}'
        f'{_analytics_metric_card("Отмены брони · проверка", proverka_overview.get("cancelled"))}'
        f'{_analytics_metric_card("Отправили скрин · розыгрыш", by_name.get("raffle_screenshot"))}'
        f'{_analytics_metric_card("Посетили розыгрыш", raffle_overview.get("visited"))}'
        "</div>"
    )

    event_labels = {
        "bot_start": "Зашли в бот (/start)",
        "help_open": "Открыли Help / FAQ",
        "help_question": "Написали в поддержку",
        "branch_best": "BEST · вход",
        "branch_hitloto": "Hit Loto · вход",
        "branch_proverka": "Проверка · вход",
        "buy_click": "Нажали «Купить»",
        "raffle_enter": "Вход в розыгрыш",
        "raffle_branch": "Выбор ветки",
        "raffle_screenshot": "Отправили скрин",
        "raffle_approved": "Скрин принят",
        "raffle_rejected": "Скрин отклонён",
        "raffle_subscribed": "Подписка ок",
        "raffle_sub_failed": "Подписка нет",
        "raffle_booked": "Забронировали",
        "raffle_ticket": "Получили билет",
        "raffle_booking_cancelled": "Отменили бронь",
        "raffle_annulled": "Аннулировано",
        "proverka_booked": "Бронь создана",
        "proverka_ticket": "Билет получен",
        "proverka_booking_cancelled": "Бронь отменена",
        "proverka_annulled": "Бронь аннулирована",
        "bot_blocked": "Заблокировали бота",
        "bot_unblocked": "Разблокировали бота",
    }
    # Always list every known step in these groups (even if 0).
    always_show_groups = {"Розыгрыш", "Ветки", "Брони · Проверка"}
    event_groups = [
        ("Вход в бот", ["bot_start"]),
        ("Ветки", ["branch_proverka", "branch_best", "branch_hitloto"]),
        ("Карточки концертов", []),  # filled from show_cards breakdown below
        ("Помощь", ["help_open", "help_question"]),
        (
            "Розыгрыш",
            [
                "raffle_enter",
                "raffle_branch",
                "raffle_screenshot",
                "raffle_approved",
                "raffle_rejected",
                "raffle_subscribed",
                "raffle_sub_failed",
                "raffle_booked",
                "raffle_ticket",
                "raffle_booking_cancelled",
                "raffle_annulled",
            ],
        ),
        (
            "Брони · Проверка",
            [
                "proverka_booked",
                "proverka_ticket",
                "proverka_booking_cancelled",
                "proverka_annulled",
            ],
        ),
        ("Блокировки бота", ["bot_blocked", "bot_unblocked"]),
    ]
    # Booking stages from bookings table by format (same source as funnel).
    raffle_bookings_preview = report.get("raffle_bookings") or {}
    proverka_bookings_preview = report.get("proverka_bookings") or {}
    by_name = dict(by_name)
    by_name["raffle_booked"] = raffle_bookings_preview.get("created") or {"events": 0, "uniques": 0}
    by_name["raffle_ticket"] = raffle_bookings_preview.get("confirmed") or {"events": 0, "uniques": 0}
    by_name["raffle_booking_cancelled"] = raffle_bookings_preview.get("cancelled") or {
        "events": 0,
        "uniques": 0,
    }
    by_name["raffle_annulled"] = raffle_bookings_preview.get("annulled") or {"events": 0, "uniques": 0}
    by_name["proverka_booked"] = proverka_bookings_preview.get("created") or {"events": 0, "uniques": 0}
    by_name["proverka_ticket"] = proverka_bookings_preview.get("confirmed") or {"events": 0, "uniques": 0}
    by_name["proverka_booking_cancelled"] = proverka_bookings_preview.get("cancelled") or {
        "events": 0,
        "uniques": 0,
    }
    by_name["proverka_annulled"] = proverka_bookings_preview.get("annulled") or {
        "events": 0,
        "uniques": 0,
    }
    grouped_names = {name for _, names in event_groups for name in names}
    grouped_names.update(
        {
            "show_card",
            "buy_click",
            "booking_created",
            "booking_confirmed",
            "booking_cancelled",
            "booking_annulled",
        }
    )
    other_names = sorted(
        name for name in by_name if name not in grouped_names and name != "show_card"
    )
    if other_names:
        event_groups.append(("Другое", other_names))

    show_card_rows = []
    format_titles = {"best": "BEST", "hitloto": "Hit Loto", "proverka": "Проверка"}
    browse_titles = {"date": "поиск по дате", "venue": "поиск по площадке"}
    for row in report.get("show_cards") or []:
        fmt = row.get("format") or ""
        browse = row.get("browse") or ""
        fmt_title = format_titles.get(fmt, fmt or "формат")
        browse_title = browse_titles.get(browse, browse or "карточка")
        show_card_rows.append(
            "<tr>"
            f"<td>{_h(f'{fmt_title} — открыли карточку концерта ({browse_title})')}</td>"
            f"<td>{int(row.get('events') or 0)}</td>"
            f"<td>{int(row.get('uniques') or 0)}</td>"
            "</tr>"
        )
    if not show_card_rows and by_name.get("show_card"):
        metric = by_name["show_card"]
        show_card_rows.append(
            "<tr>"
            "<td>Открыли карточку конкретного концерта</td>"
            f"<td>{metric.get('events', 0)}</td>"
            f"<td>{metric.get('uniques', 0)}</td>"
            "</tr>"
        )
    if by_name.get("buy_click"):
        metric = by_name["buy_click"]
        show_card_rows.append(
            "<tr>"
            f"<td>{_h(event_labels['buy_click'])}</td>"
            f"<td>{metric.get('events', 0)}</td>"
            f"<td>{metric.get('uniques', 0)}</td>"
            "</tr>"
        )

    group_blocks = []
    for group_title, names in event_groups:
        if group_title == "Карточки концертов":
            if not show_card_rows:
                continue
            group_blocks.append(
                f'<tr class="events-group-row"><td colspan="3">{_h(group_title)}</td></tr>'
                + "".join(show_card_rows)
            )
            continue
        rows = []
        show_zeros = group_title in always_show_groups
        for name in names:
            metric = by_name.get(name) or {"events": 0, "uniques": 0}
            if name not in by_name and name in grouped_names and not show_zeros:
                continue
            rows.append(
                "<tr>"
                f"<td>{_h(event_labels.get(name, name))}</td>"
                f"<td>{metric.get('events', 0)}</td>"
                f"<td>{metric.get('uniques', 0)}</td>"
                "</tr>"
            )
        if not rows:
            continue
        group_blocks.append(
            f'<tr class="events-group-row"><td colspan="3">{_h(group_title)}</td></tr>'
            + "".join(rows)
        )

    all_events_body = "".join(group_blocks)
    all_events_table = (
        '<section class="card details-card analytics-section">'
        "<details>"
        '<summary class="details-summary">'
        "<div>"
        "<strong>Все события за период</strong>"
        '<span class="muted">Полный список метрик · нажмите, чтобы раскрыть</span>'
        "</div>"
        '<span class="details-action"><span class="closed-label">Развернуть</span><span class="open-label">Свернуть</span></span>'
        "</summary>"
        '<div class="details-body">'
        '<p class="muted">События сгруппированы. «Карточки концертов» — сколько раз открыли конкретный концерт после поиска по дате или площадке. Страница не автообновляется.</p>'
        + (
            '<div class="table-wrap"><table class="analytics-events">'
            "<thead><tr>"
            "<th>Событие</th><th>Нажатий / заходов</th><th>Уникальных людей</th>"
            "</tr></thead>"
            f"<tbody>{all_events_body}</tbody></table></div>"
            if all_events_body
            else '<p class="muted">За выбранный период событий нет</p>'
        )
        + "</div>"
        "</details>"
        "</section>"
    )

    start_payload_labels = {
        "": "вход через старт",
        "standup_rozygr": "вход розыгрыш",
        "quick_booking": "вход быстрая бронь",
        "afisha_plat": "вход платный BEST",
    }
    payload_rows = []
    for row in report.get("starts_by_payload") or []:
        raw = row.get("payload") or ""
        label = start_payload_labels.get(raw, raw or "вход через старт")
        payload_rows.append(
            "<tr>"
            f"<td>{_h(label)}</td>"
            f"<td>{row['events']}</td>"
            f"<td>{row['uniques']}</td>"
            "</tr>"
        )
    starts_table = (
        '<section class="card analytics-section">'
        "<h2>Входы в бот по ссылкам</h2>"
        '<p class="muted">Заходы / уникальные люди.</p>'
        '<div class="table-wrap"><table><thead><tr><th>Вход</th><th>Заходы</th><th>Люди</th></tr></thead>'
        f"<tbody>{''.join(payload_rows) or '<tr><td colspan=\"3\" class=\"muted\">Пока нет данных</td></tr>'}</tbody>"
        "</table></div></section>"
    )

    def _card_cell(events: int, uniques: int) -> str:
        if not events and not uniques:
            return '<span class="muted">0</span>'
        return f"{events}<br><span class='muted'>{uniques} чел.</span>"

    card_matrix = {
        "best": {"date": (0, 0), "venue": (0, 0)},
        "hitloto": {"date": (0, 0), "venue": (0, 0)},
        "proverka": {"date": (0, 0), "venue": (0, 0)},
    }
    for row in report.get("show_cards") or []:
        fmt = row.get("format") or ""
        browse = row.get("browse") or "date"
        if fmt not in card_matrix:
            continue
        if browse not in ("date", "venue"):
            browse = "date"
        card_matrix[fmt][browse] = (int(row.get("events") or 0), int(row.get("uniques") or 0))

    show_blocks = []
    for fmt, title, tone in (
        ("best", "BEST", "tone-best"),
        ("hitloto", "Hit Loto", "tone-hitloto"),
        ("proverka", "Проверка", "tone-proverka"),
    ):
        by_date_e, by_date_u = card_matrix[fmt]["date"]
        by_venue_e, by_venue_u = card_matrix[fmt]["venue"]
        show_blocks.append(
            f'<div class="show-format-block {tone}">'
            f'<div class="show-format-title">{_h(title)}</div>'
            '<div class="show-format-stats">'
            f'<div><span>По дате</span><b>{by_date_e}</b>'
            f'<small class="muted">{by_date_u} уник.</small></div>'
            f'<div><span>По площадке</span><b>{by_venue_e}</b>'
            f'<small class="muted">{by_venue_u} уник.</small></div>'
            "</div></div>"
        )
    cards_table = (
        '<section class="card analytics-section">'
        "<h2>Просмотры карточек шоу</h2>"
        '<p class="muted">Открытия карточек по способу поиска.</p>'
        f'<div class="show-format-grid">{"".join(show_blocks)}</div>'
        "</section>"
    )

    raffle_bookings = report.get("raffle_bookings") or {}
    kind_steps = report.get("raffle_kind_steps") or {}
    kind_bookings = report.get("raffle_kind_bookings") or {}
    kind_created = kind_bookings.get("created") or {}
    kind_confirmed = kind_bookings.get("confirmed") or {}
    kind_cancelled = kind_bookings.get("cancelled") or {}

    def _funnel_step(title: str, metric: dict | None, note: str = "", tone: str = "main") -> str:
        metric = metric or {"events": 0, "uniques": 0}
        note_html = f'<span class="funnel-note">{_h(note)}</span>' if note else ""
        return (
            f'<div class="funnel-step funnel-{tone}">'
            f'<div class="funnel-title">{_h(title)}</div>'
            f'<div class="funnel-value"><b>{metric.get("events", 0)}</b>'
            f'<span class="muted">{metric.get("uniques", 0)} чел.</span></div>'
            f"{note_html}"
            "</div>"
        )

    def _funnel_row(main_html: str, side_html: str = "") -> str:
        side = side_html or '<div class="funnel-step funnel-spacer" aria-hidden="true"></div>'
        return f'<div class="funnel-row">{main_html}{side}</div>'

    def _branch_metric(title: str, metric: dict | None) -> str:
        metric = metric or {"events": 0, "uniques": 0}
        return (
            '<div class="metric branch-metric">'
            f'<span>{_h(title)}</span><b>{metric.get("events", 0)}</b>'
            f'<small class="muted">{metric.get("uniques", 0)} уникальных</small>'
            "</div>"
        )

    raffle_body = (
        '<div class="funnel-layout">'
        f'{_funnel_row(_funnel_step("1. Зашли в розыгрыш", by_name.get("raffle_enter")))}'
        f'{_funnel_row(_funnel_step("2. Выбрали ветку", by_name.get("raffle_branch")))}'
        f'{_funnel_row(_funnel_step("3. Отправили скрин", by_name.get("raffle_screenshot")))}'
        f'{_funnel_row(_funnel_step("4. Скрин принят", by_name.get("raffle_approved")), _funnel_step("Скрин отклонён", by_name.get("raffle_rejected"), "отвал", "side"))}'
        f'{_funnel_row(_funnel_step("5. Подписка ок", by_name.get("raffle_subscribed")), _funnel_step("Подписка нет", by_name.get("raffle_sub_failed"), "отвал", "side"))}'
        f'{_funnel_row(_funnel_step("6. Забронировали", raffle_bookings.get("created")), _funnel_step("Отменили бронь", raffle_bookings.get("cancelled"), "отвал", "side"))}'
        f'{_funnel_row(_funnel_step("7. Получили билет", raffle_bookings.get("confirmed")), _funnel_step("Аннулировано", raffle_bookings.get("annulled"), "отвал", "side"))}'
        "</div>"
    )

    branch_cards = []
    for kind, title in (("post", "Билет за пост"), ("review", "Билет за отзыв")):
        steps = kind_steps.get(kind) or {}
        branch_cards.append(
            '<div class="branch-card">'
            f"<h3>{_h(title)}</h3>"
            '<div class="summary branch-metrics">'
            f'{_branch_metric("Зашли в ветку", steps.get("raffle_branch"))}'
            f'{_branch_metric("Отправили скрин", steps.get("raffle_screenshot"))}'
            f'{_branch_metric("Скрин принят", steps.get("raffle_approved"))}'
            f'{_branch_metric("Есть бронь", kind_created.get(kind))}'
            f'{_branch_metric("Билет получен", kind_confirmed.get(kind))}'
            f'{_branch_metric("Бронь отменена", kind_cancelled.get(kind))}'
            "</div>"
            "</div>"
        )
    raffle = (
        '<section class="card details-card analytics-section">'
        "<details>"
        '<summary class="details-summary">'
        "<div>"
        "<strong>Розыгрыш</strong>"
        '<span class="muted">Воронка от входа до билета · справа — отвалы</span>'
        "</div>"
        '<span class="details-action"><span class="closed-label">Развернуть</span><span class="open-label">Свернуть</span></span>'
        "</summary>"
        '<div class="details-body">'
        f"{raffle_body}"
        f'<div class="branch-grid">{"".join(branch_cards)}</div>'
        "</div>"
        "</details>"
        "</section>"
    )

    audience = report.get("audience") or {}
    audience_html = (
        '<section class="card analytics-section">'
        "<h2>База для рассылки <span class='muted'>(всего)</span></h2>"
        '<div class="summary analytics-audience">'
        f'<div class="metric"><span>Telegram · всего</span><b>{audience.get("telegram_users", 0)}</b></div>'
        f'<div class="metric"><span>Telegram · можно слать</span><b>{audience.get("telegram_mailable", 0)}</b></div>'
        f'<div class="metric"><span>Telegram · заблокировали</span><b>{audience.get("telegram_blocked", 0)}</b></div>'
        f'<div class="metric"><span>VK · всего</span><b>{audience.get("vk_users", 0)}</b></div>'
        f'<div class="metric"><span>VK · можно слать</span><b>{audience.get("vk_mailable", 0)}</b></div>'
        f'<div class="metric"><span>VK · заблокировали</span><b>{audience.get("vk_blocked", 0)}</b></div>'
        "</div></section>"
    )

    filters_bar = f"""
    <div class="filters analytics-filters">
      <div class="counters">{''.join(channel_links)}</div>
      <div class="counters">{preset_html}</div>
      <form method="get" action="/admin">
        <input type="hidden" name="tab" value="analytics">
        <input type="hidden" name="channel" value="{_h(channel)}">
        <label class="muted">С</label>
        <input name="date_from" type="date" value="{_h(date_from)}">
        <label class="muted">По</label>
        <input name="date_to" type="date" value="{_h(date_to)}">
        <button type="submit">Показать</button>
        <a class="pill" href="/admin?tab=analytics&all=1">Сбросить</a>
      </form>
    </div>
    <p class="muted">Период: <b>{_h(report.get("period_label") or "весь период")}</b>.
    В карточках: число = заходы, ниже — уникальные люди.</p>
    """

    return filters_bar + overview + all_events_table + starts_table + cards_table + raffle + audience_html


def _content(
    dashboard: dict,
    filters: dict,
    db_data: dict | None = None,
    analytics: dict | None = None,
    user_extras: dict | None = None,
) -> str:
    tab = filters.get("tab") or "date"
    if tab == "bookings":
        return _bookings_tab(dashboard, filters)
    if tab == "users":
        return _users_tab(dashboard, filters, user_extras=user_extras)
    if tab == "analytics":
        return _analytics_tab(analytics or {}, filters)
    if tab == "db":
        db_data = db_data or {"tables": [], "browse": None}
        return _db_tab(db_data.get("tables") or [], db_data.get("browse"), filters)
    return _date_tab(dashboard, filters)


def render_admin_html(
    dashboard: dict,
    filters: dict,
    source_label: str,
    db_data: dict | None = None,
    can_view_db: bool = False,
    analytics: dict | None = None,
    user_extras: dict | None = None,
) -> str:
    totals = dashboard["totals"]
    tab = filters.get("tab") or "date"
    is_db = tab == "db"
    is_analytics = tab == "analytics"
    filters = _normalize_event_filter(dashboard, filters) if not is_analytics else filters
    date_value = _date_to_input(filters.get("date", ""))
    date_input = '<input name="date" type="date" value="{}">'.format(_h(date_value))
    event_select = _event_select(dashboard, filters)
    hidden_status = f'<input type="hidden" name="status" value="{_h(filters.get("status"))}">' if filters.get("status") else ""
    hidden_sort = f'<input type="hidden" name="sort" value="{_h(filters.get("sort"))}">' if filters.get("sort") else ""
    hidden_order = f'<input type="hidden" name="order" value="{_h(filters.get("order"))}">' if filters.get("sort") else ""
    summary_html = ""
    if not is_db and not is_analytics:
        summary_html = f"""
    <div class="summary">
      <div class="metric"><span>Мероприятий</span><b>{totals["events"]}</b></div>
      <div class="metric"><span>Всего броней</span><b>{totals["bookings"]}</b></div>
      <div class="metric"><span>Активные брони, гостей</span><b>{totals["reserved_guests"]}</b></div>
      <div class="metric"><span>Подтвердили билеты</span><b>{totals["confirmed_guests"]}</b></div>
    </div>
    <div class="filters">
      <div>{_status_filter(filters)}</div>
      <form method="get" action="/admin">
        <input type="hidden" name="tab" value="{_h(filters.get('tab') or 'date')}">
        {date_input}
        {event_select}
        <select name="format">{_format_select(filters)}</select>
        {hidden_status}
        {hidden_sort}
        {hidden_order}
        <button type="submit">Показать</button>
        <a class="pill" href="/admin?tab={_h(filters.get('tab') or 'date')}">Сбросить</a>
      </form>
    </div>"""
    refresh_meta = "" if is_db or is_analytics else '<meta http-equiv="refresh" content="30">'
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh_meta}
  <title>Стендап бронирование</title>
  <style>
    :root {{ color-scheme: light; --bg:#f4f6fb; --card:#fff; --text:#111827; --muted:#667085; --line:#e5e7eb; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family: Inter, Arial, sans-serif; background:var(--bg); color:var(--text); }}
    header {{ padding:28px 32px; background:#111827; color:white; }}
    header h1 {{ margin:0 0 8px; font-size:30px; }}
    header p {{ margin:0; color:#cbd5e1; }}
    header a {{ color:white; }}
    main {{ max-width:1280px; margin:0 auto; padding:24px; }}
    .tabs {{ display:flex; gap:10px; margin-bottom:16px; flex-wrap:wrap; }}
    .tab, .pill {{ padding:10px 14px; border-radius:999px; border:1px solid var(--line); color:#111827; background:white; text-decoration:none; }}
    .tab.active, .pill.active {{ background:#111827; color:white; border-color:#111827; }}
    .summary {{ display:grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap:16px; margin-bottom:20px; }}
    .analytics-summary {{ grid-template-columns: repeat(5, minmax(0,1fr)); }}
    .analytics-audience {{ grid-template-columns: repeat(3, minmax(0,1fr)); }}
    .analytics-show-pair {{ grid-template-columns: repeat(2, minmax(0,1fr)); margin:0; }}
    .branch-metrics {{ grid-template-columns: repeat(6, minmax(0,1fr)); gap:10px; margin:0; }}
    .branch-metric {{ padding:12px; border-radius:12px; box-shadow:none; }}
    .branch-metric span {{ font-size:12px; }}
    .branch-metric b {{ margin-top:4px; font-size:20px; }}
    .branch-metric small {{ margin-top:4px; font-size:11px; }}
    .card-compact {{ padding:16px 18px; }}
    .card-compact h2 {{ font-size:18px; margin-bottom:4px; }}
    .card-compact > .muted {{ margin:0 0 12px; font-size:13px; }}
    .analytics-section {{ padding:18px 20px; }}
    .analytics-section > h2 {{ margin:0 0 4px; font-size:18px; font-weight:700; line-height:1.3; }}
    .analytics-section > .muted {{ margin:0 0 14px; font-size:13px; }}
    .card.details-card,
    .card.details-card.analytics-section {{ padding:0; }}
    .show-format-grid {{ display:grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap:10px; }}
    .show-format-block {{ margin:0; padding:10px 12px; border-radius:12px; border:1px solid var(--line); }}
    .show-format-title {{ font-size:13px; font-weight:700; margin-bottom:8px; }}
    .show-format-stats {{ display:grid; grid-template-columns: 1fr 1fr; gap:8px; }}
    .show-format-stats > div {{ background:rgba(255,255,255,.72); border:1px solid rgba(15,23,42,.06); border-radius:10px; padding:8px 10px; }}
    .show-format-stats span {{ display:block; font-size:11px; color:var(--muted); }}
    .show-format-stats b {{ display:block; margin-top:2px; font-size:18px; line-height:1.2; }}
    .show-format-stats small {{ display:block; margin-top:2px; font-size:11px; }}
    .show-format-block h3, .branch-card h3 {{ margin:0 0 10px; font-size:16px; }}
    .tone-best {{ background:#eff6ff; border-color:#bfdbfe; }}
    .tone-best h3, .tone-best .show-format-title, .metric.tone-best span {{ color:#1d4ed8; }}
    .tone-hitloto {{ background:#fff7ed; border-color:#fed7aa; }}
    .tone-hitloto h3, .tone-hitloto .show-format-title, .metric.tone-hitloto span {{ color:#c2410c; }}
    .tone-proverka {{ background:#f0fdf4; border-color:#bbf7d0; }}
    .tone-proverka h3, .tone-proverka .show-format-title, .metric.tone-proverka span {{ color:#15803d; }}
    .metric.tone-best, .metric.tone-hitloto, .metric.tone-proverka {{ border-width:1px; }}
    .branch-grid {{ display:grid; grid-template-columns: 1fr; gap:14px; margin-top:18px; }}
    .branch-card {{ background:#f8fafc; border:1px solid var(--line); border-radius:16px; padding:16px; }}
    .user-extra-stack {{ display:grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap:10px; margin-top:18px; align-items:start; }}
    .user-extra-details {{ background:#f8fafc; border:1px solid var(--line); border-radius:14px; overflow:hidden; }}
    .user-extra-summary {{ display:flex; justify-content:space-between; align-items:center; gap:10px; padding:14px 16px; cursor:pointer; list-style:none; }}
    .user-extra-summary::-webkit-details-marker {{ display:none; }}
    .user-extra-summary strong {{ font-size:15px; }}
    .user-extra-body {{ padding:0 16px 16px; border-top:1px solid var(--line); }}
    .user-extra-details[open] {{ grid-column: 1 / -1; }}
    .user-block-item {{ margin-top:10px; padding-top:10px; border-top:1px solid var(--line); }}
    .user-block-item:first-of-type {{ margin-top:8px; padding-top:0; border-top:none; }}
    .user-block-item ul {{ margin:8px 0 0; padding-left:18px; }}
    .user-block-item li {{ margin:4px 0; }}
    table.user-extra {{ table-layout:fixed; min-width:420px; }}
    table.user-activity-summary th:nth-child(2), table.user-activity-summary td:nth-child(2) {{ width:34%; text-align:right; }}
    .screen-carousel {{
      display:flex; gap:12px; overflow-x:auto; scroll-snap-type:x mandatory;
      -webkit-overflow-scrolling:touch; padding:4px 2px 10px; margin-top:8px;
    }}
    .screen-card {{
      flex:0 0 min(320px, 85%); scroll-snap-align:start;
      background:white; border:1px solid var(--line); border-radius:14px; padding:14px;
    }}
    .screen-card-top {{ display:flex; justify-content:space-between; gap:10px; align-items:center; }}
    .screen-card-title {{ font-weight:700; font-size:14px; }}
    .screen-card-meta {{ margin:10px 0 0; font-size:13px; line-height:1.45; }}
    .screen-card-nav {{ margin-top:12px; font-size:12px; }}
    .screen-carousel-hint {{ margin:0; font-size:12px; }}
    .funnel-layout {{ display:flex; flex-direction:column; gap:10px; margin-top:14px; }}
    .funnel-row {{ display:grid; grid-template-columns: 1fr 1fr; gap:16px; align-items:stretch; }}
    .funnel-step {{ background:#f0fdf4; border:1px solid #bbf7d0; border-radius:14px; padding:12px 14px; min-height:78px; }}
    .funnel-step.funnel-side {{ background:#fff1f2; border-color:#fecdd3; }}
    .funnel-step.funnel-spacer {{ visibility:hidden; pointer-events:none; border-color:transparent; background:transparent; }}
    .funnel-title {{ font-size:13px; color:#475467; margin-bottom:6px; }}
    .funnel-value {{ display:flex; gap:10px; align-items:baseline; }}
    .funnel-value b {{ font-size:24px; }}
    .funnel-note {{ display:inline-block; margin-top:6px; font-size:12px; color:#be123c; font-weight:700; }}
    .details-card {{ padding:0; overflow:hidden; }}
    .details-summary {{ display:flex; justify-content:space-between; align-items:center; gap:16px; padding:18px 20px; cursor:pointer; list-style:none; }}
    .details-summary::-webkit-details-marker {{ display:none; }}
    .details-summary strong {{ display:block; font-size:18px; font-weight:700; line-height:1.3; }}
    .details-summary .muted {{ display:block; margin-top:4px; font-size:13px; }}
    .details-action {{ flex-shrink:0; padding:8px 12px; border-radius:999px; background:#111827; color:white; font-size:13px; }}
    .details-action .open-label {{ display:none; }}
    details[open] .details-action .closed-label {{ display:none; }}
    details[open] .details-action .open-label {{ display:inline; }}
    .details-body {{ padding:0 20px 20px; border-top:1px solid var(--line); }}
    .details-body > .muted {{ margin:14px 0; font-size:13px; }}
    .details-body > .funnel-layout {{ margin-top:14px; }}
    .details-body > .branch-grid {{ margin-top:16px; }}
    .events-group-title {{ margin:18px 0 8px; font-size:15px; color:#334155; }}
    .events-group-title:first-child {{ margin-top:8px; }}
    table.analytics-events {{ table-layout:fixed; min-width:640px; }}
    table.analytics-events th:nth-child(1), table.analytics-events td:nth-child(1) {{ width:46%; }}
    table.analytics-events th:nth-child(2), table.analytics-events td:nth-child(2),
    table.analytics-events th:nth-child(3), table.analytics-events td:nth-child(3) {{ width:27%; text-align:right; }}
    table.analytics-events tr.events-group-row td {{
      padding-top:18px; padding-bottom:8px; border-bottom:none;
      font-size:15px; font-weight:700; color:#334155; text-align:left; background:transparent;
    }}
    table.analytics-events tr.events-group-row:first-child td {{ padding-top:8px; }}
    .metric, .card, .filters {{ background:var(--card); border:1px solid var(--line); border-radius:18px; box-shadow:0 8px 30px rgba(15,23,42,.05); }}
    .metric {{ padding:18px; }}
    .metric span {{ display:block; color:var(--muted); font-size:14px; }}
    .metric b {{ display:block; margin-top:8px; font-size:30px; }}
    .metric small {{ display:block; margin-top:6px; font-size:13px; }}
    .filters {{ padding:16px; margin-bottom:20px; display:flex; gap:12px; flex-wrap:wrap; align-items:center; justify-content:space-between; }}
    .analytics-filters {{ flex-direction:column; align-items:stretch; }}
    .filters form {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
    input, select, button {{ border:1px solid var(--line); border-radius:10px; padding:10px 12px; background:white; font:inherit; }}
    button {{ background:#111827; color:white; cursor:pointer; }}
    .card {{ padding:20px; margin-bottom:18px; }}
    .card.analytics-section {{ padding:18px 20px; }}
    .card.details-card {{ padding:0; }}
    .event-head {{ display:flex; justify-content:space-between; gap:16px; align-items:start; }}
    h2 {{ margin:0 0 10px; font-size:18px; font-weight:700; }}
    .event-head p {{ margin:0; color:var(--muted); }}
    .format {{ background:#eef2ff; color:#3730a3; padding:7px 10px; border-radius:999px; font-weight:700; }}
    .capacity-line {{ display:flex; justify-content:space-between; margin-top:16px; font-weight:700; }}
    .capacity-bar, .status-bar {{ overflow:hidden; height:14px; background:#e5e7eb; border-radius:999px; margin-top:8px; display:flex; }}
    .capacity-bar span {{ display:block; background:#22c55e; }}
    .status-bar span {{ display:block; }}
    .status-bar.empty {{ background:#eef2f7; }}
    .active-bookings {{ margin-top:10px; color:#334155; }}
    .counters, .mini-metrics {{ display:flex; gap:8px; flex-wrap:wrap; margin:14px 0; align-items:center; }}
    .counter, .mini-metrics span {{ background:#f8fafc; border:1px solid var(--line); border-radius:999px; padding:7px 10px; color:#334155; }}
    .table-wrap {{ overflow-x:auto; -webkit-overflow-scrolling:touch; }}
    table {{ width:100%; border-collapse:collapse; margin-top:12px; }}
    table.bookings {{ table-layout:fixed; min-width:1080px; }}
    table.bookings.compact {{ min-width:760px; }}
    table.bookings th:nth-child(1), table.bookings td:nth-child(1) {{ width:72px; }}
    table.bookings th:nth-child(2), table.bookings td:nth-child(2) {{ width:120px; }}
    table.bookings th:nth-child(3), table.bookings td:nth-child(3) {{ width:140px; }}
    table.bookings th:nth-child(4), table.bookings td:nth-child(4) {{ width:140px; }}
    table.bookings th:nth-child(5), table.bookings td:nth-child(5) {{ width:56px; }}
    table.bookings:not(.compact) th:nth-child(6), table.bookings:not(.compact) td:nth-child(6) {{ width:96px; }}
    table.bookings:not(.compact) th:nth-child(7), table.bookings:not(.compact) td:nth-child(7) {{ width:220px; }}
    table.bookings:not(.compact) th:nth-child(8), table.bookings:not(.compact) td:nth-child(8),
    table.bookings:not(.compact) th:nth-child(9), table.bookings:not(.compact) td:nth-child(9) {{ width:92px; }}
    th, td {{ padding:11px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; white-space:nowrap; }}
    td.loc {{ white-space:normal; word-break:break-word; overflow-wrap:anywhere; }}
    th {{ color:#475467; font-size:13px; background:#f8fafc; }}
    th.sortable a {{ color:inherit; text-decoration:none; }}
    th.sortable a:hover {{ text-decoration:underline; }}
    .sort-mark {{ color:#94a3b8; font-size:11px; }}
    .badge {{ display:inline-block; color:white; border-radius:999px; padding:5px 9px; font-size:12px; font-weight:700; }}
    .format-tag {{ display:inline-block; margin-top:6px; background:#eef2ff; color:#3730a3; border-radius:999px; padding:3px 8px; font-size:12px; font-weight:700; }}
    .muted {{ color:var(--muted); }}
    .empty-state {{ text-align:center; padding:36px; color:#475467; }}
    details {{ margin:0; }}
    @media (max-width: 900px) {{
      .funnel-row, .analytics-show-pair, .show-format-grid, .user-extra-stack {{ grid-template-columns:1fr; }}
      .user-extra-details[open] {{ grid-column:auto; }}
      .funnel-step.funnel-spacer {{ display:none; }}
      .branch-metrics {{ grid-template-columns: repeat(2, minmax(0,1fr)); }}
    }}
    @media (max-width: 1100px) and (min-width: 901px) {{
      .branch-metrics {{ grid-template-columns: repeat(6, minmax(0,1fr)); }}
      .branch-metric {{ padding:10px; }}
      .branch-metric b {{ font-size:18px; }}
    }}
    @media (max-width: 780px) {{
      header {{ padding:22px 18px; }}
      main {{ padding:16px; }}
      .summary, .analytics-summary, .analytics-audience {{ grid-template-columns:1fr; }}
      .event-head {{ display:block; }}
      .details-summary {{ display:block; }}
      .details-action {{ display:inline-block; margin-top:10px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Стендап бронирование</h1>
    <p>Автообновление каждые 30 секунд · источник данных: {_h(source_label)} · <a href="/admin/logout">выйти</a></p>
  </header>
  <main>
    <nav class="tabs">{_tabs(filters, can_view_db)}</nav>
    {summary_html}
    {_content(dashboard, filters, db_data, analytics, user_extras)}
  </main>
</body>
</html>"""


def _filters_from_request(request: web.Request) -> dict:
    sort = request.query.get("sort", "").strip()
    order = request.query.get("order", "").strip().lower()
    if sort not in BOOKING_SORT_OPTIONS:
        sort = ""
    if order not in ("asc", "desc"):
        order = "asc" if sort else ""
    date = request.query.get("date", "").strip()
    event = request.query.get("event", "").strip() if date else ""
    channel = request.query.get("channel", "").strip()
    if channel not in ("telegram", "vkontakte"):
        channel = ""
    date_from = request.query.get("date_from", "").strip()
    date_to = request.query.get("date_to", "").strip()
    tab = request.query.get("tab", "date").strip() or "date"
    all_period = request.query.get("all", "").strip() == "1"
    if tab == "analytics" and not all_period and not date_from and not date_to:
        today = datetime.now(MSK).strftime("%Y-%m-%d")
        date_from = today
        date_to = today
    return {
        "tab": tab,
        "status": request.query.get("status", "").strip(),
        "date": date,
        "event": event,
        "format": request.query.get("format", "").strip(),
        "u": request.query.get("u", "").strip(),
        "table": request.query.get("table", "").strip(),
        "page": request.query.get("page", "1").strip() or "1",
        "sort": sort,
        "order": order,
        "channel": channel,
        "date_from": date_from,
        "date_to": date_to,
        "all": "1" if all_period else "",
    }


def _request_token(request: web.Request) -> str:
    return (
        request.query.get("token")
        or request.headers.get("X-Admin-Token")
        or request.cookies.get(ADMIN_COOKIE_NAME)
        or ""
    )


def _is_manager_token(candidate: str, config: AdminConfig) -> bool:
    return bool(candidate and config.admin_token and hmac.compare_digest(candidate, config.admin_token))


def _is_owner_token(candidate: str, config: AdminConfig) -> bool:
    return bool(candidate and config.owner_token and hmac.compare_digest(candidate, config.owner_token))


def _token_matches(candidate: str, config: AdminConfig) -> bool:
    """Any valid login token: manager or owner."""
    if not candidate:
        return False
    if not config.admin_token and not config.owner_token:
        return False
    return _is_manager_token(candidate, config) or _is_owner_token(candidate, config)


def _can_view_db(request: web.Request, config: AdminConfig) -> bool:
    """DB viewer is only for the owner token, not for managers."""
    return _is_owner_token(_request_token(request), config)


def _check_auth(request: web.Request, config: AdminConfig) -> bool:
    # Open only if no tokens configured at all (local/dev)
    if not config.admin_token and not config.owner_token:
        return True
    return _token_matches(_request_token(request), config)


def _set_auth_cookie(response: web.Response, token: str) -> None:
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        token,
        max_age=ADMIN_COOKIE_MAX_AGE,
        httponly=True,
        samesite="Lax",
    )


def render_login_html(error: str = "") -> str:
    error_html = f'<p class="error">{_h(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Вход · Стендап бронирование</title>
  <style>
    body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:#f4f6fb; font-family:Arial,sans-serif; color:#111827; }}
    form {{ width:min(420px, calc(100vw - 32px)); background:white; padding:28px; border-radius:18px; box-shadow:0 16px 50px rgba(15,23,42,.12); }}
    h1 {{ margin:0 0 10px; font-size:26px; }}
    p {{ margin:0 0 18px; color:#667085; }}
    input, button {{ width:100%; border:1px solid #e5e7eb; border-radius:10px; padding:12px; font:inherit; }}
    button {{ margin-top:12px; background:#111827; color:white; cursor:pointer; }}
    .error {{ color:#b91c1c; background:#fee2e2; border-radius:10px; padding:10px 12px; }}
  </style>
</head>
<body>
  <form method="post" action="/admin/login">
    <h1>Стендап бронирование</h1>
    <p>Введите токен доступа. У менеджера и владельца токены разные.</p>
    {error_html}
    <input name="token" type="password" autofocus placeholder="Токен доступа">
    <button type="submit">Войти</button>
  </form>
</body>
</html>"""


def _redirect_without_token(request: web.Request) -> str:
    query = [(key, value) for key, value in request.query.items() if key != "token"]
    return "/admin" + ("?" + urlencode(query) if query else "")


async def admin_page(request: web.Request) -> web.Response:
    config = request.app["config"]
    if not _check_auth(request, config):
        return web.Response(text=render_login_html(), status=401, content_type="text/html")
    query_token = (request.query.get("token") or "").strip()
    if query_token and _token_matches(query_token, config):
        response = web.HTTPFound(_redirect_without_token(request))
        _set_auth_cookie(response, query_token)
        raise response

    can_view_db = _can_view_db(request, config)
    filters = _filters_from_request(request)
    if filters.get("status") and filters["status"] not in STATUSES:
        filters["status"] = ""
    if filters.get("format") and filters["format"] not in FORMAT_OPTIONS:
        filters["format"] = ""
    if filters.get("tab") not in {"date", "bookings", "users", "analytics", "db"}:
        filters["tab"] = "date"
    # Managers must not open DB via direct URL
    if filters.get("tab") == "db" and not can_view_db:
        raise web.HTTPFound("/admin?tab=date")

    loop = asyncio.get_running_loop()
    source_label = "PostgreSQL" if _use_postgres(config) else f"SQLite ({config.db_path})"
    db_data = None
    analytics = None
    empty_dashboard = {
        "events": [],
        "bookings": [],
        "users": {},
        "totals": {"events": 0, "bookings": 0, "reserved_guests": 0, "confirmed_guests": 0},
    }
    if filters.get("tab") == "db":
        tables = await loop.run_in_executor(None, list_db_tables, config)
        browse = None
        if filters.get("table"):
            browse = await loop.run_in_executor(
                None,
                browse_db_table,
                config,
                filters["table"],
                filters.get("page", "1"),
            )
        db_data = {"tables": tables, "browse": browse}
        dashboard = empty_dashboard
    elif filters.get("tab") == "analytics":
        from bot.db.analytics import fetch_analytics_report

        analytics = await loop.run_in_executor(
            None,
            lambda: fetch_analytics_report(
                date_from=filters.get("date_from") or None,
                date_to=filters.get("date_to") or None,
                channel=filters.get("channel") or "",
            ),
        )
        dashboard = empty_dashboard
    else:
        # With a date selected, load all shows that day (even empty) so the show picker is complete.
        include_empty_events = bool(filters.get("date")) and filters.get("tab") in {"date", "bookings"}
        # Users tab needs every booking to keep the selected guest visible when status filter changes.
        fetch_filters = dict(filters)
        if filters.get("tab") == "users":
            fetch_filters["status"] = ""
        rows = await loop.run_in_executor(None, fetch_admin_rows, config, fetch_filters, include_empty_events)
        dashboard = build_dashboard(rows)
        # Keep header metrics in sync with the status pill on Users, without dropping guests.
        if filters.get("tab") == "users" and filters.get("status"):
            status = filters["status"]
            filtered = [b for b in dashboard["bookings"] if b.get("status") == status]
            event_ids = {str(b.get("event_id") or "") for b in filtered}
            dashboard = {
                **dashboard,
                "totals": {
                    "events": len(event_ids),
                    "bookings": len(filtered),
                    "reserved_guests": sum(
                        int(b.get("guests") or 0) for b in filtered if b.get("status") in ACTIVE_STATUSES
                    ),
                    "confirmed_guests": sum(
                        int(b.get("guests") or 0) for b in filtered if b.get("status") == "confirmed"
                    ),
                },
            }
    user_extras = None
    if filters.get("tab") == "users" and filters.get("u"):
        selected = (dashboard.get("users") or {}).get(filters.get("u") or "")
        if selected:
            telegram_id = selected.get("telegram_id")
            user_id = selected.get("user_id")

            def _load_user_extras():
                from bot.db.analytics import fetch_user_activity_counts
                from bot.db.crud import get_raffle_submissions_for_telegram, get_user_raffle_flags

                tid = int(telegram_id) if telegram_id else None
                uid = int(user_id) if user_id else None
                return {
                    "activity_counts": fetch_user_activity_counts(tid) if tid else [],
                    "submissions": get_raffle_submissions_for_telegram(tid) if tid else [],
                    "flags": get_user_raffle_flags(telegram_id=tid, user_id=uid),
                }

            user_extras = await loop.run_in_executor(None, _load_user_extras)
    return web.Response(
        text=render_admin_html(
            dashboard, filters, source_label, db_data, can_view_db, analytics, user_extras
        ),
        content_type="text/html",
    )


async def login_page(request: web.Request) -> web.Response:
    config = request.app["config"]
    data = await request.post()
    token = (data.get("token") or "").strip()
    if not _token_matches(token, config):
        return web.Response(text=render_login_html("Неверный токен"), status=401, content_type="text/html")
    response = web.HTTPFound("/admin")
    _set_auth_cookie(response, token)
    raise response


async def logout_page(request: web.Request) -> web.Response:
    response = web.HTTPFound("/admin")
    response.del_cookie(ADMIN_COOKIE_NAME)
    raise response


async def index_page(request: web.Request) -> web.Response:
    raise web.HTTPFound("/admin")


def create_app(config: AdminConfig | None = None) -> web.Application:
    app = web.Application()
    app["config"] = config or load_config()
    app.router.add_get("/", index_page)
    app.router.add_get("/admin", admin_page)
    app.router.add_post("/admin/login", login_page)
    app.router.add_get("/admin/logout", logout_page)
    return app


def run():
    host = os.getenv("ADMIN_HOST", "127.0.0.1")
    port = int(os.getenv("ADMIN_PORT", "8080"))
    web.run_app(create_app(), host=host, port=port)


if __name__ == "__main__":
    run()
