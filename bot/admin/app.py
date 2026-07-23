import asyncio
import hmac
import html
import os
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

import psycopg
from aiohttp import web
from psycopg.rows import dict_row


STATUSES = ("booked", "confirmed", "cancelled", "annulled")
ACTIVE_STATUSES = {"booked", "confirmed"}
FORMAT_OPTIONS = ("proverka", "rozygrysh")
STATUS_LABELS = {
    "booked": "Забронировано",
    "confirmed": "Подтверждено",
    "cancelled": "Отменено",
    "annulled": "Аннулировано",
}
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


def _short_dt(value):
    if not value:
        return ""
    text = str(value)
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt).strftime("%d.%m %H:%M")
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%d.%m %H:%M")
    except ValueError:
        return text[:16]


def _normalize_status(value):
    return value if value in STATUSES else "booked"


def _format_filter_sql(filters: dict, params: dict, include_empty_events: bool) -> str:
    fmt = filters.get("format")
    if not fmt:
        return ""
    # Always bind %(format)s when the SQL fragment uses it
    params["format"] = fmt
    if fmt == "rozygrysh":
        return "b.format = %(format)s"
    if fmt == "proverka":
        if include_empty_events:
            return "(b.format = %(format)s OR (b.id IS NULL AND e.format = %(format)s))"
        return "b.format = %(format)s"
    return ""


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
    return {
        "id": row.get("booking_id"),
        "status": status,
        "status_label": STATUS_LABELS[status],
        "guests": _parse_int(row.get("guests")),
        "source": row.get("source") or "",
        "format": row.get("booking_format") or row.get("event_format") or "",
        "created_at": _short_dt(row.get("created_at")),
        "changed_at": _short_dt(changed_at),
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

        user_key = str(booking["telegram_id"] or booking["vk_id"] or booking["phone"] or booking["name"] or booking["id"])
        user = users.setdefault(
            user_key,
            {
                "key": user_key,
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


def _seat_bar(event: dict) -> str:
    max_seats = event["max_seats"]
    reserved = event["reserved_guests"]
    confirmed = event["confirmed_guests"]
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


def _booking_table(bookings: list[dict], compact=False) -> str:
    if not bookings:
        return '<p class="muted">Броней пока нет.</p>'
    rows = []
    for booking in bookings:
        contact = _h(booking["phone"])
        if booking["username"]:
            contact += f'<br><span class="muted">@{_h(booking["username"])}</span>'
        event_cols = ""
        if not compact:
            event_cols = (
                f"<td>{_h(booking['event_date'])}</td>"
                f"<td>{_h(booking['event_time'])}</td>"
                f"<td>{_h(booking['location'])}<br><span class='muted'>{_h(booking['address'])}</span></td>"
            )
        rows.append(
            "<tr>"
            f"<td>#{_h(booking['id'])}</td>"
            f"<td>{_status_badge(booking['status'])}</td>"
            f"<td><b>{_h(booking['name'])}</b><br><span class='muted'>{_h(booking['source'])}</span></td>"
            f"<td>{contact}</td>"
            f"<td>{_h(booking['guests'])}</td>"
            f"{event_cols}"
            f"<td>{_h(booking['created_at'])}</td>"
            f"<td>{_h(booking['changed_at'])}</td>"
            "</tr>"
        )
    event_headers = "" if compact else "<th>Дата</th><th>Время</th><th>Локация</th>"
    return (
        "<table><thead><tr><th>ID</th><th>Статус</th><th>Клиент</th><th>Контакт</th><th>Гости</th>"
        f"{event_headers}<th>Создана</th><th>Изменена</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


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
        f'<span class="format">{_h(event["format"])}</span>'
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
    ]
    if can_view_db:
        tabs.append(("db", "База"))
    current = filters.get("tab") or "date"
    return "".join(
        f'<a class="tab {"active" if current == key else ""}" href="{_query_link(filters, tab=key, u="", table="", page="")}">{label}</a>'
        for key, label in tabs
    )


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
        options.append(f'<option value="{fmt}" {selected}>{fmt}</option>')
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
    events_with_bookings = [event for event in dashboard["events"] if event["bookings"]]
    if not events_with_bookings:
        return '<section class="card empty-state"><h2>Пока нет бронирования на указанную дату</h2><p>На эту дату пока не создано ни одной брони.</p></section>'
    return "".join(_event_card(event) for event in events_with_bookings)


def _bookings_tab(dashboard: dict, filters: dict) -> str:
    bookings = dashboard["bookings"]
    by_format = defaultdict(list)
    for booking in bookings:
        by_format[booking["format"]].append(booking)
    sections = []
    for fmt, title in (("proverka", "Проверка материала"), ("rozygrysh", "Розыгрыш")):
        if filters.get("format") and filters["format"] != fmt:
            continue
        sections.append(f'<section class="card"><h2>{title}</h2>{_booking_table(by_format.get(fmt, []))}</section>')
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


def _users_tab(dashboard: dict, filters: dict) -> str:
    users = sorted(dashboard["users"].values(), key=lambda u: (u["name"] or "", u["phone"] or ""))
    selected_key = filters.get("u", "")
    rows = []
    for user in users:
        rows.append(
            "<tr>"
            f"<td><a href='{_query_link(filters, u=user['key'])}'>{_h(user['name'] or 'Без имени')}</a><br><span class='muted'>{_h(user['source'])}</span></td>"
            f"<td>{_h(user['phone'])}<br><span class='muted'>@{_h(user['username'])}</span></td>"
            f"<td>{len(user['bookings'])}</td>"
            f"<td>{user['status_counts'].get('booked', 0)}</td>"
            f"<td>{user['status_counts'].get('confirmed', 0)}</td>"
            f"<td>{user['status_counts'].get('cancelled', 0)}</td>"
            f"<td>{_h(_user_stage(user))}</td>"
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>Клиент</th><th>Контакт</th><th>Всего</th><th>Активные</th>"
        "<th>Билеты</th><th>Отмены</th><th>Этап</th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=\"7\" class=\"muted\">Пользователей пока нет</td></tr>'}</tbody></table>"
    )
    detail = ""
    if selected_key and selected_key in dashboard["users"]:
        user = dashboard["users"][selected_key]
        reminders_24h = sum(1 for b in user["bookings"] if b["reminder_24h_sent"])
        reminders_day = sum(1 for b in user["bookings"] if b["reminder_day_sent"])
        detail = (
            '<section class="card user-detail">'
            f'<h2>{_h(user["name"] or "Без имени")}</h2>'
            f'<p class="muted">{_h(user["phone"])} · @{_h(user["username"])} · источник: {_h(user["source"])}</p>'
            '<div class="mini-metrics">'
            f'<span>Всего броней: <b>{len(user["bookings"])}</b></span>'
            f'<span>Активных: <b>{user["status_counts"].get("booked", 0)}</b></span>'
            f'<span>Билетов: <b>{user["status_counts"].get("confirmed", 0)}</b></span>'
            f'<span>Отмен: <b>{user["status_counts"].get("cancelled", 0)}</b></span>'
            f'<span>Напоминание за сутки: <b>{reminders_24h}</b></span>'
            f'<span>Напоминание в день: <b>{reminders_day}</b></span>'
            '</div>'
            f'<p><b>Текущий этап:</b> {_h(_user_stage(user))}</p>'
            f'{_booking_table(user["bookings"])}'
            '</section>'
        )
    return detail + f'<section class="card"><h2>Users</h2>{table}</section>'


def _content(dashboard: dict, filters: dict, db_data: dict | None = None) -> str:
    tab = filters.get("tab") or "date"
    if tab == "bookings":
        return _bookings_tab(dashboard, filters)
    if tab == "users":
        return _users_tab(dashboard, filters)
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
) -> str:
    totals = dashboard["totals"]
    date_value = _date_to_input(filters.get("date", ""))
    date_input = '<input name="date" type="date" value="{}">'.format(_h(date_value))
    hidden_status = f'<input type="hidden" name="status" value="{_h(filters.get("status"))}">' if filters.get("status") else ""
    is_db = (filters.get("tab") or "date") == "db"
    summary_html = ""
    filters_html = ""
    if not is_db:
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
        <select name="format">{_format_select(filters)}</select>
        {hidden_status}
        <button type="submit">Показать</button>
        <a class="pill" href="/admin?tab={_h(filters.get('tab') or 'date')}">Сбросить</a>
      </form>
    </div>"""
    refresh_meta = "" if is_db else '<meta http-equiv="refresh" content="30">'
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
    .metric, .card, .filters {{ background:var(--card); border:1px solid var(--line); border-radius:18px; box-shadow:0 8px 30px rgba(15,23,42,.05); }}
    .metric {{ padding:18px; }}
    .metric span {{ display:block; color:var(--muted); font-size:14px; }}
    .metric b {{ display:block; margin-top:8px; font-size:30px; }}
    .filters {{ padding:16px; margin-bottom:20px; display:flex; gap:12px; flex-wrap:wrap; align-items:center; justify-content:space-between; }}
    .filters form {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
    input, select, button {{ border:1px solid var(--line); border-radius:10px; padding:10px 12px; background:white; font:inherit; }}
    button {{ background:#111827; color:white; cursor:pointer; }}
    .card {{ padding:20px; margin-bottom:18px; }}
    .event-head {{ display:flex; justify-content:space-between; gap:16px; align-items:start; }}
    h2 {{ margin:0 0 10px; font-size:22px; }}
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
    .table-wrap {{ overflow-x:auto; }}
    table {{ width:100%; border-collapse:collapse; margin-top:12px; }}
    th, td {{ padding:11px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; white-space:nowrap; }}
    th {{ color:#475467; font-size:13px; background:#f8fafc; }}
    .badge {{ display:inline-block; color:white; border-radius:999px; padding:5px 9px; font-size:12px; font-weight:700; }}
    .muted {{ color:var(--muted); }}
    .empty-state {{ text-align:center; padding:36px; color:#475467; }}
    @media (max-width: 780px) {{
      header {{ padding:22px 18px; }}
      main {{ padding:16px; }}
      .summary {{ grid-template-columns:1fr; }}
      .event-head {{ display:block; }}
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
    {_content(dashboard, filters, db_data)}
  </main>
</body>
</html>"""


def _filters_from_request(request: web.Request) -> dict:
    return {
        "tab": request.query.get("tab", "date").strip() or "date",
        "status": request.query.get("status", "").strip(),
        "date": request.query.get("date", "").strip(),
        "format": request.query.get("format", "").strip(),
        "u": request.query.get("u", "").strip(),
        "table": request.query.get("table", "").strip(),
        "page": request.query.get("page", "1").strip() or "1",
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
    if filters.get("tab") not in {"date", "bookings", "users", "db"}:
        filters["tab"] = "date"
    # Managers must not open DB via direct URL
    if filters.get("tab") == "db" and not can_view_db:
        raise web.HTTPFound("/admin?tab=date")

    loop = asyncio.get_running_loop()
    source_label = "PostgreSQL" if _use_postgres(config) else f"SQLite ({config.db_path})"
    db_data = None
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
        dashboard = {
            "events": [],
            "bookings": [],
            "users": {},
            "totals": {"events": 0, "bookings": 0, "reserved_guests": 0, "confirmed_guests": 0},
        }
    else:
        include_empty_events = filters.get("tab") == "date" and bool(filters.get("date"))
        rows = await loop.run_in_executor(None, fetch_admin_rows, config, filters, include_empty_events)
        dashboard = build_dashboard(rows)
    return web.Response(
        text=render_admin_html(dashboard, filters, source_label, db_data, can_view_db),
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
