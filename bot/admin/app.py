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
ACTIVITY_SHORT_LABELS = {
    "bot_start": "Вход в бот",
    "branch_proverka": "Ветка · Проверка",
    "branch_best": "Ветка · BEST",
    "branch_hitloto": "Ветка · Hit Loto",
    "browse_dates": "Выбор по дате",
    "browse_venues": "Выбор по площадке",
    "show_card": "Карточка концерта",
    "booking_start": "Начал бронь (имя)",
    "cmd_my_bookings": "Команда · /my_bookings",
    "cmd_main_menu": "Команда · /main_menu",
    "cmd_buy_ticket": "Команда · /buy_ticket",
    "cmd_help": "Команда · /help",
    "cmd_channel": "Команда · /channel",
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
# Rough "where the guest is now" from the latest tracked event.
LAST_PLACE_LABELS = {
    "bot_start": "Старт бота",
    "cmd_my_bookings": "Мои брони",
    "cmd_main_menu": "Главное меню",
    "cmd_buy_ticket": "Купить билет",
    "cmd_help": "Помощь / FAQ",
    "cmd_channel": "Канал анонсов",
    "help_open": "Помощь / FAQ",
    "help_question": "Написал в поддержку",
    "branch_proverka": "Ветка «Проверка»",
    "branch_best": "Ветка BEST",
    "branch_hitloto": "Ветка Hit Loto",
    "browse_dates": "Выбор даты",
    "browse_venues": "Выбор площадки",
    "show_card": "Карточка концерта",
    "booking_start": "Оформляет бронь",
    "buy_click": "Нажал «Купить»",
    "raffle_enter": "Розыгрыш",
    "raffle_branch": "Розыгрыш · выбор ветки",
    "raffle_screenshot": "Розыгрыш · отправил скрин",
    "raffle_approved": "Розыгрыш · скрин принят",
    "raffle_rejected": "Розыгрыш · скрин отклонён",
    "raffle_subscribed": "Розыгрыш · подписка",
    "raffle_sub_failed": "Розыгрыш · подписка не прошла",
    "booking_created": "Создал бронь",
    "booking_confirmed": "Получил билет",
    "booking_cancelled": "Отменил бронь",
    "booking_annulled": "Бронь аннулирована",
    "bot_blocked": "Заблокировал бота",
    "bot_unblocked": "Разблокировал бота",
}
DB_VIEW_TABLES = (
    "events",
    "users",
    "bookings",
    "raffle_submissions",
    "raffle_nav",
    "raffle_vk_awaiting",
    "help_requests",
    "analytics_events",
)
DB_PAGE_SIZE = 50
USERS_PAGE_SIZE = 50


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
    client_token: str
    owner_token: str


def load_config() -> AdminConfig:
    database_url = os.getenv("DATABASE_URL", "")
    return AdminConfig(
        database_url=database_url,
        db_path=os.getenv("DB_PATH", "bookings.db"),
        bookings_source=os.getenv("BOOKINGS_SOURCE", "postgres" if database_url else "sqlite"),
        # Manager: only «Мероприятия»
        admin_token=os.getenv("ADMIN_TOKEN", "").strip(),
        # Client: bookings/users/analytics/events/journal (no DB, no ticket resend)
        client_token=os.getenv("ADMIN_CLIENT_TOKEN", "").strip(),
        # Owner: full admin + DB + ticket resend
        owner_token=os.getenv("ADMIN_OWNER_TOKEN", "").strip(),
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
        "confirmed_at_raw": row.get("confirmed_at") or "",
        "cancelled_at_raw": row.get("cancelled_at") or "",
        "annulled_at_raw": row.get("annulled_at") or "",
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


def _user_channel(user: dict) -> str:
    """Messenger for UI/filters: prefer ids (source='import' is not a channel)."""
    if user.get("vk_id"):
        return "vkontakte"
    if user.get("telegram_id"):
        return "telegram"
    source = (user.get("source") or "").strip().lower()
    if source in ("telegram", "vkontakte"):
        return source
    return ""


def _channel_badge_html(channel: str) -> str:
    if channel == "vkontakte":
        return '<span class="channel-badge channel-vk">VK</span>'
    if channel == "telegram":
        return '<span class="channel-badge channel-tg">TG</span>'
    return '<span class="channel-badge">?</span>'


def _user_from_directory_row(row: dict) -> dict:
    uid = int(row["id"])
    key = str(uid)
    source = (row.get("source") or "").strip() or (
        "vkontakte" if row.get("vk_id") else "telegram"
    )
    counts = Counter(
        {
            "booked": int(row.get("cnt_booked") or 0),
            "confirmed": int(row.get("cnt_confirmed") or 0),
            "cancelled": int(row.get("cnt_cancelled") or 0),
            "annulled": int(row.get("cnt_annulled") or 0),
        }
    )
    return {
        "key": key,
        "user_id": uid,
        "name": row.get("name") or "",
        "username": row.get("username") or "",
        "phone": row.get("phone") or "",
        "telegram_id": row.get("telegram_id"),
        "vk_id": row.get("vk_id"),
        "source": source,
        "consent_accepted_at": row.get("consent_accepted_at"),
        "consent_version": row.get("consent_version") or "",
        "bookings": [],
        "status_counts": counts,
        "guests_confirmed": 0,
        "guests_reserved": 0,
        "bookings_total": int(row.get("bookings_total") or sum(counts.values())),
    }


def _users_directory_where(q: str, status: str, channel: str = "") -> tuple[str, dict]:
    where = ["TRUE"]
    params: dict = {}
    q = (q or "").strip()
    if q:
        q_user = q[1:].strip() if q.startswith("@") else q
        params["q_like"] = f"%{q}%"
        params["q_user_like"] = f"%{q_user}%"
        phone_digits = "".join(ch for ch in q if ch.isdigit())
        parts = [
            "COALESCE(u.name, '') ILIKE %(q_like)s",
            "COALESCE(u.username, '') ILIKE %(q_user_like)s",
            "COALESCE(u.phone, '') ILIKE %(q_like)s",
        ]
        # Телефон без +, пробелов и скобок: 8900… найдёт +7 (900) …
        if len(phone_digits) >= 3:
            params["q_phone_digits"] = f"%{phone_digits}%"
            parts.append(
                "regexp_replace(COALESCE(u.phone, ''), '\\D', '', 'g') LIKE %(q_phone_digits)s"
            )
        where.append("(" + " OR ".join(parts) + ")")
    if status in STATUSES:
        params["status"] = status
        where.append(
            "EXISTS ("
            "SELECT 1 FROM bookings b2 "
            "WHERE b2.user_id = u.id AND b2.status = %(status)s"
            ")"
        )
    if channel in ("telegram", "vkontakte"):
        params["channel"] = channel
        # ids first: imported users have source='import', not telegram/vkontakte.
        where.append(
            "("
            "CASE "
            "WHEN u.vk_id IS NOT NULL THEN 'vkontakte' "
            "WHEN u.telegram_id IS NOT NULL THEN 'telegram' "
            "WHEN TRIM(COALESCE(u.source, '')) IN ('telegram', 'vkontakte') "
            "THEN TRIM(u.source) "
            "ELSE '' "
            "END"
            ") = %(channel)s"
        )
    return " AND ".join(where), params


def fetch_users_page(
    config: AdminConfig,
    *,
    q: str = "",
    page: int = 1,
    status: str = "",
    channel: str = "",
) -> dict:
    """Paginated users directory with search — safe for 10k+ guests."""
    empty = {
        "users": [],
        "total": 0,
        "page": 1,
        "pages": 1,
        "q": (q or "").strip(),
        "page_size": USERS_PAGE_SIZE,
    }
    if not _use_postgres(config):
        return empty
    page = max(1, _parse_int(page, 1))
    where_sql, base_params = _users_directory_where(q, status, channel)
    try:
        with psycopg.connect(config.database_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) AS n FROM users u WHERE {where_sql}",
                    base_params,
                )
                total = int((cur.fetchone() or {}).get("n") or 0)
                pages = max(1, (total + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE)
                page = min(page, pages)
                list_params = {
                    **base_params,
                    "limit": USERS_PAGE_SIZE,
                    "offset": (page - 1) * USERS_PAGE_SIZE,
                }
                cur.execute(
                    f"""
                    SELECT
                        u.id,
                        u.telegram_id,
                        u.vk_id,
                        u.name,
                        u.username,
                        u.phone,
                        u.source,
                        COUNT(b.id) AS bookings_total,
                        COUNT(b.id) FILTER (WHERE b.status = 'booked') AS cnt_booked,
                        COUNT(b.id) FILTER (WHERE b.status = 'confirmed') AS cnt_confirmed,
                        COUNT(b.id) FILTER (WHERE b.status = 'cancelled') AS cnt_cancelled,
                        COUNT(b.id) FILTER (WHERE b.status = 'annulled') AS cnt_annulled
                    FROM users u
                    LEFT JOIN bookings b ON b.user_id = u.id
                    WHERE {where_sql}
                    GROUP BY u.id
                    ORDER BY COALESCE(u.last_active_at, u.created_at) DESC NULLS LAST, u.id DESC
                    LIMIT %(limit)s OFFSET %(offset)s
                    """,
                    list_params,
                )
                rows = [dict(r) for r in cur.fetchall()]
        return {
            "users": [_user_from_directory_row(r) for r in rows],
            "total": total,
            "page": page,
            "pages": pages,
            "q": (q or "").strip(),
            "page_size": USERS_PAGE_SIZE,
        }
    except Exception:
        return empty


def fetch_one_directory_user(config: AdminConfig, user_id: int) -> dict | None:
    if not _use_postgres(config) or not user_id:
        return None
    try:
        from bot.db.crud import ensure_pdn_consent_columns

        ensure_pdn_consent_columns()
        with psycopg.connect(config.database_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        u.id,
                        u.telegram_id,
                        u.vk_id,
                        u.name,
                        u.username,
                        u.phone,
                        u.source,
                        u.consent_accepted_at,
                        u.consent_version,
                        COUNT(b.id) AS bookings_total,
                        COUNT(b.id) FILTER (WHERE b.status = 'booked') AS cnt_booked,
                        COUNT(b.id) FILTER (WHERE b.status = 'confirmed') AS cnt_confirmed,
                        COUNT(b.id) FILTER (WHERE b.status = 'cancelled') AS cnt_cancelled,
                        COUNT(b.id) FILTER (WHERE b.status = 'annulled') AS cnt_annulled
                    FROM users u
                    LEFT JOIN bookings b ON b.user_id = u.id
                    WHERE u.id = %(user_id)s
                    GROUP BY u.id
                    """,
                    {"user_id": int(user_id)},
                )
                row = cur.fetchone()
                return _user_from_directory_row(dict(row)) if row else None
    except Exception:
        return None


def fetch_user_booking_rows(config: AdminConfig, user_id: int) -> list[dict]:
    """All bookings for one guest (users tab detail)."""
    if not _use_postgres(config) or not user_id:
        return []
    try:
        with psycopg.connect(config.database_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
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
                    FROM bookings b
                    JOIN users u ON u.id = b.user_id
                    JOIN events e ON e.id = b.event_id
                    WHERE u.id = %(user_id)s
                    ORDER BY e.event_date DESC, e.event_time DESC, b.id DESC
                    """,
                    {"user_id": int(user_id)},
                )
                return [dict(row) for row in cur.fetchall()]
    except Exception:
        return []


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
    """Show проверка/розыгрыш; for BEST-events with raffle bookings prefer розыгрыш."""
    fmt = event.get("format") or ""
    label = FORMAT_LABELS.get(fmt)
    if not label:
        for booking in event.get("bookings") or []:
            bfmt = booking.get("format") or ""
            label = FORMAT_LABELS.get(bfmt)
            if label:
                fmt = bfmt
                break
    if not label:
        return ""
    tone = f" format--{fmt}" if fmt in FORMAT_LABELS else ""
    return f'<span class="format{tone}">{_h(label)}</span>'


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


def _tabs(
    filters: dict,
    *,
    can_view_ops: bool = True,
    can_view_db: bool = False,
    can_send_mailing: bool = False,
) -> str:
    if can_view_ops:
        tabs = [
            ("users", "Пользователи"),
            ("bookings", "Все брони"),
            ("date", "По дате"),
            ("events", "Мероприятия"),
            ("analytics", "Аналитика"),
            ("audit", "Журнал"),
        ]
        if can_send_mailing:
            tabs.append(("mailing", "Рассылка"))
        if can_view_db:
            tabs.append(("db", "База"))
    else:
        # Manager: афиша + быстрый просмотр броней по дате
        tabs = [
            ("date", "По дате"),
            ("events", "Мероприятия"),
        ]
    current = filters.get("tab") or ("date" if can_view_ops else "events")
    return "".join(
        f'<a class="tab {"active" if current == key else ""}" '
        f'href="{_query_link(filters, tab=key, date="", event="", u="", table="", page="", sort="", order="", date_from="", date_to="", channel="", ef="", tickets="", q="")}">{label}</a>'
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


_AUDIT_ROLE_LABELS = {
    "owner": "Владелец",
    "client": "Клиент",
    "manager": "Менеджер",
}
_AUDIT_ACTION_META = {
    "login": ("Вход в админку", "login"),
    "events_save": ("Сохранил афишу", "save"),
    "events_restore": ("Вернул даты в афишу", "restore"),
    "events_cancel_notify": ("Разослал сообщение об отмене", "notify"),
    "events_cancel_bookings": ("Отменил брони по скрытым датам", "cancel"),
    "resend_ticket": ("Переотправил билет", "ticket"),
    "resend_tickets_event": ("Массово переотправил билеты", "ticket"),
    "user_anonymize": ("Обезличил данные гостя", "user"),
}
_AUDIT_AFISHA_LABELS = {
    "best": "BEST",
    "proverka": "Проверка",
    "hitloto": "Hit Loto",
    "rozygrysh": "Розыгрыш",
}
_AUDIT_AUDIENCE_LABELS = {
    "booked": "гостям с бронью",
    "confirmed": "гостям с билетом",
    "both": "гостям с бронью и билетом",
}


def _audit_details_dict(row: dict) -> dict:
    details = row.get("details") or {}
    if isinstance(details, dict):
        return details
    if isinstance(details, str):
        try:
            import json as _json

            parsed = _json.loads(details)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _audit_role_label(role: str) -> str:
    return _AUDIT_ROLE_LABELS.get((role or "").strip(), (role or "").strip() or "—")


def _audit_action_meta(action: str) -> tuple[str, str]:
    return _AUDIT_ACTION_META.get((action or "").strip(), ((action or "Действие").strip() or "Действие", "other"))


def _audit_target_label(entity_type: str, entity_id: str) -> str:
    et = (entity_type or "").strip()
    eid = (entity_id or "").strip()
    if et == "afisha":
        fmt = _AUDIT_AFISHA_LABELS.get(eid, eid or "афиша")
        return f"афиша «{fmt}»"
    if et == "event":
        if not eid:
            return "мероприятие"
        if "," in eid:
            ids = [x.strip() for x in eid.split(",") if x.strip()]
            if len(ids) == 1:
                return f"мероприятие #{ids[0]}"
            return f"мероприятия #{', #'.join(ids[:4])}" + ("…" if len(ids) > 4 else "")
        return f"мероприятие #{eid}"
    if et == "booking":
        return f"бронь #{eid}" if eid else "бронь"
    if et == "user":
        return f"гость #{eid}" if eid else "гость"
    if et == "admin":
        return ""
    if et or eid:
        return f"{et} {eid}".strip()
    return ""


def _audit_num(details: dict, *keys, default=None):
    for key in keys:
        if key in details and details.get(key) is not None:
            return details.get(key)
    return default


def _audit_event_line(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    parts = [
        str(item.get("date") or "").strip(),
        str(item.get("time") or "").strip(),
        str(item.get("location") or "").strip(),
    ]
    address = str(item.get("address") or "").strip()
    # Адрес в строку только если площадка пустая — иначе дублирует.
    if address and not parts[-1]:
        parts.append(address)
    label = " · ".join(p for p in parts if p)
    eid = item.get("id") if item.get("id") is not None else item.get("event_id")
    if eid is not None and str(eid).strip():
        return f"{label} (#{eid})" if label else f"#{eid}"
    return label


def _audit_booking_line(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    bid = item.get("booking_id")
    name = str(item.get("name") or "").strip()
    event_bits = [
        str(item.get("date") or "").strip(),
        str(item.get("time") or "").strip(),
        str(item.get("location") or "").strip(),
    ]
    event_s = " · ".join(p for p in event_bits if p)
    head = f"#{bid}" if bid is not None else "бронь"
    if name:
        head = f"{head} · {name}"
    if event_s:
        head = f"{head} · {event_s}"
    if "ok" in item:
        head = f"{head} — {'ок' if item.get('ok') else 'ошибка'}"
    return head


def _audit_item_lines(items, *, kind: str = "event", limit: int = 12) -> list[str]:
    if not isinstance(items, list) or not items:
        return []
    lines = []
    for item in items[:limit]:
        line = _audit_event_line(item) if kind == "event" else _audit_booking_line(item)
        if line:
            lines.append(line)
    extra = len(items) - limit
    if extra > 0:
        lines.append(f"…и ещё {extra}")
    return lines


_AUDIT_CHANGE_VERBS = {
    "added": "добавил",
    "changed": "изменил",
    "hidden": "скрыл",
    "deleted": "удалил",
    "restored": "вернул",
}
_AUDIT_FIELD_LINE_LABELS = {
    "price": "Цена",
    "payment_url": "Оплата",
    "image_url": "Фото",
    "address": "Адрес",
    "description": "Описание",
    "host": "Состав / ведущий",
    "max_seats": "Мест",
    "status": "Статус",
}


def _audit_short_value(value, *, limit: int = 120) -> str:
    text = str(value if value is not None else "").strip()
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _audit_event_field_lines(item: dict) -> list[str]:
    """Extra journal lines: price, photo, payment link, etc. (mainly for added shows)."""
    if not isinstance(item, dict):
        return []
    change = str(item.get("change") or "").strip()
    # Полный снимок полей — при добавлении; при правке хватает списка изменённых ключей.
    if change not in {"added", ""}:
        return []
    fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
    if not fields:
        if item.get("address"):
            fields = {"address": item.get("address")}
        else:
            return []
    lines = []
    for key, label in _AUDIT_FIELD_LINE_LABELS.items():
        if key not in fields:
            continue
        raw = fields.get(key)
        if key == "price":
            try:
                price_i = int(raw or 0)
            except (TypeError, ValueError):
                price_i = 0
            lines.append(f"{label}: {'бесплатно' if price_i <= 0 else f'{price_i} ₽'}")
            continue
        if key == "max_seats":
            try:
                seats_i = int(raw or 0)
            except (TypeError, ValueError):
                seats_i = 0
            if seats_i > 0:
                lines.append(f"{label}: {seats_i}")
            continue
        if key == "status":
            continue  # active/past — служебное, в кат не тащим
        text = _audit_short_value(raw, limit=160 if key in {"payment_url", "image_url"} else 140)
        if text:
            lines.append(f"{label}: {text}")
    return lines


def _audit_action_sentence(item: dict, *, afisha: str = "") -> str:
    """Human sentence for one afisha change (for disputes / journal)."""
    if not isinstance(item, dict):
        return ""
    where = _audit_event_line(item)
    if not where:
        eid = item.get("id") if item.get("id") is not None else item.get("event_id")
        where = f"#{eid}" if eid is not None and str(eid).strip() else ""
    if not where:
        return ""
    afisha_s = f" афиши «{afisha}»" if afisha else " афиши"
    change = str(item.get("change") or "").strip()
    if change == "added":
        return (
            f"Добавил в{afisha_s} дату {where}. "
            "Сохранено — шоу появится в боте."
        )
    if change == "changed":
        fields = item.get("changes") or []
        field_s = ", ".join(str(x) for x in fields[:6] if x)
        before = item.get("before") if isinstance(item.get("before"), dict) else {}
        before_line = _audit_event_line(before) if before else ""
        head = f"Изменил в{afisha_s} дату {where}"
        if field_s:
            head += f" ({field_s})"
        if before_line and before_line != where:
            head += f". Было: {before_line}"
        return head + ". Сохранено — обновление видно в боте."
    if change == "hidden":
        return (
            f"Скрыл из{afisha_s} дату {where}. "
            "Шоу убрано из бота; активные брони и билеты по нему отменяются."
        )
    if change == "deleted":
        return (
            f"Удалил из{afisha_s} дату {where} насовсем. "
            "Запись снята с афиши."
        )
    if change == "restored":
        return (
            f"Вернул в{afisha_s} дату {where}. "
            "Шоу снова доступно в боте."
        )
    return f"Обновил в{afisha_s} дату {where}."


def _audit_collect_actions(details: dict) -> list[dict]:
    actions = details.get("actions")
    if isinstance(actions, list) and actions:
        return [a for a in actions if isinstance(a, dict)]
    # Старые записи журнала без actions — собираем из списков.
    out = []
    for key, change in (
        ("saved_items", "changed"),
        ("hidden_items", "hidden"),
        ("deleted_items", "deleted"),
        ("restored_items", "restored"),
    ):
        for item in details.get(key) or []:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row.setdefault("change", change)
            out.append(row)
    return out


def _audit_friendly_detail_lines(action: str, details: dict) -> list[str]:
    """Narrative lines for the journal feed (what exactly happened)."""
    action = (action or "").strip()
    details = details or {}
    if action == "login":
        return []
    if action == "events_save":
        afisha = _AUDIT_AFISHA_LABELS.get(str(details.get("format") or ""), "")
        lines = []
        for item in _audit_collect_actions(details)[:20]:
            sentence = _audit_action_sentence(item, afisha=afisha)
            if sentence:
                lines.append(sentence)
            for field_line in _audit_event_field_lines(item):
                lines.append(field_line)
        if details.get("notify"):
            aud = _AUDIT_AUDIENCE_LABELS.get(
                str(details.get("notify_audience") or ""),
                "гостям",
            )
            lines.append(f"Запланирована рассылка об отмене {aud}.")
        errors = _audit_num(details, "errors")
        if errors:
            lines.append(f"При сохранении были ошибки: {errors}.")
        if not lines:
            # Fallback, если в details только счётчики без actions.
            counts = _audit_afisha_change_counts(details)
            bits = []
            for key, label in (
                ("added", "добавлено"),
                ("changed", "изменено"),
                ("hidden", "скрыто"),
                ("deleted", "удалено"),
            ):
                n = int(counts.get(key) or _audit_num(details, key) or 0)
                if n:
                    bits.append(f"{label}: {n}")
            if bits:
                return [f"Изменения в афише «{afisha or 'BEST'}»: " + ", ".join(bits) + "."]
            return ["Нажал «Обновить», но изменений в афише не было."]
        return lines
    if action == "events_restore":
        afisha = _AUDIT_AFISHA_LABELS.get(str(details.get("format") or ""), "")
        lines = []
        for item in _audit_collect_actions(details)[:20]:
            sentence = _audit_action_sentence(item, afisha=afisha)
            if sentence:
                lines.append(sentence)
        if not lines:
            restored = _audit_num(details, "restored")
            if restored:
                return [f"Вернул в афишу дат: {restored}."]
        return lines
    if action == "events_cancel_notify":
        if details.get("skipped"):
            return ["Рассылку об отмене не отправляли."]
        ok = int(_audit_num(details, "ok") or 0)
        fail = int(_audit_num(details, "fail") or 0)
        aud = _AUDIT_AUDIENCE_LABELS.get(str(details.get("audience") or ""), "гостям")
        lines = [f"Разослал сообщение об отмене {aud}: доставлено {ok}, ошибок {fail}."]
        for ev in (details.get("events") or [])[:8]:
            el = _audit_event_line(ev) if isinstance(ev, dict) else ""
            if el:
                lines.append(f"Шоу: {el}")
        for guest in (details.get("items") or [])[:12]:
            gl = _audit_booking_line(guest) if isinstance(guest, dict) else ""
            if gl:
                lines.append(f"Кому: {gl}")
        preview = str(details.get("message_preview") or "").strip()
        if preview:
            lines.append(f"Текст сообщения: {preview}")
        return lines
    if action == "events_cancel_bookings":
        if details.get("skipped"):
            return ["Отмену броней не запускали."]
        cancelled = int(_audit_num(details, "cancelled") or 0)
        fail = int(_audit_num(details, "fail") or 0)
        lines = [
            f"Отменил активные брони/билеты по скрытым датам: {cancelled}"
            + (f", ошибок {fail}" if fail else "")
            + ". Напоминания остановлены, билеты аннулированы."
        ]
        for ev in (details.get("events") or [])[:8]:
            el = _audit_event_line(ev) if isinstance(ev, dict) else ""
            if el:
                lines.append(f"Шоу: {el}")
        for guest in (details.get("items") or [])[:12]:
            gl = _audit_booking_line(guest) if isinstance(guest, dict) else ""
            if gl:
                lines.append(f"Бронь: {gl}")
        return lines
    if action == "resend_ticket":
        guest = _audit_booking_line(details)
        if details.get("ok"):
            line = f"Переотправил билет гостю {guest}." if guest and guest != "бронь" else "Переотправил билет."
            if details.get("extra_note"):
                line = line[:-1] + " с дополнительным текстом."
            return [line]
        err = str(details.get("error") or "").strip()
        line = f"Не удалось переотправить билет"
        if guest and guest != "бронь":
            line += f" гостю {guest}"
        if err:
            line += f": {err}"
        else:
            line += "."
        return [line]
    if action == "resend_tickets_event":
        ok = int(_audit_num(details, "ok") or 0)
        fail = int(_audit_num(details, "fail") or 0)
        event = details.get("event") if isinstance(details.get("event"), dict) else {}
        event_line = _audit_event_line(event) if event else ""
        head = "Массово переотправил билеты"
        if event_line:
            head += f" по шоу {event_line}"
        head += f": успешно {ok}, ошибок {fail}."
        if details.get("extra_note"):
            head = head[:-1] + ", с дополнительным текстом."
        lines = [head]
        for guest in (details.get("items") or [])[:12]:
            gl = _audit_booking_line(guest) if isinstance(guest, dict) else ""
            if gl:
                lines.append(gl)
        return lines
    if action == "user_anonymize":
        # Заголовок журнала уже содержит гостя; сырые флаги ok/had_* не показываем.
        ok_val = details.get("ok")
        failed = ok_val in (False, "false", "0", 0)
        if failed:
            err = str(details.get("error") or "").strip()
            return [f"Не удалось обезличить{': ' + err if err else '.'}"]
        if details.get("already") in (True, "true", "1", 1):
            return ["Данные уже были обезличены ранее."]
        cancelled = int(_audit_num(details, "bookings_cancelled") or 0)
        if cancelled:
            return [f"Активных броней отменено: {cancelled}."]
        return []
    # Fallback: short readable key=value list, not raw JSON dump / bool flags
    if not details:
        return []
    skip_keys = {
        "ok",
        "already",
        "had_telegram",
        "had_vk",
        "had_phone",
        "error",
        "raffle_scrubbed",
        "help_scrubbed",
        "gift_scrubbed",
        "analytics_scrubbed",
        "bookings_cancelled",
    }
    bits = []
    for key, value in list(details.items())[:8]:
        if key in skip_keys or str(key).startswith("had_"):
            continue
        if value in ("", None, [], {}, True, False) or isinstance(value, (list, dict, bool)):
            continue
        bits.append(f"{key}: {value}")
    return [" · ".join(bits)] if bits else []


def _audit_friendly_details(action: str, details: dict) -> str:
    """Plain-text summary (first line) — kept for any callers/tests."""
    lines = _audit_friendly_detail_lines(action, details)
    return lines[0] if lines else ""


def _audit_afisha_change_counts(details: dict) -> dict[str, int]:
    actions = _audit_collect_actions(details)
    return {
        "added": sum(1 for a in actions if a.get("change") == "added"),
        "changed": sum(1 for a in actions if a.get("change") == "changed"),
        "hidden": sum(1 for a in actions if a.get("change") == "hidden")
        or int(_audit_num(details, "hidden") or 0),
        "deleted": sum(1 for a in actions if a.get("change") == "deleted")
        or int(_audit_num(details, "deleted") or 0),
        "restored": sum(1 for a in actions if a.get("change") == "restored")
        or int(_audit_num(details, "restored") or 0),
    }


def _audit_afisha_cut_summary(action: str, details: dict, lines: list[str]) -> str:
    """Short summary line for the collapsible «Подробнее» cut."""
    counts = _audit_afisha_change_counts(details)
    bits = []
    if action == "events_restore" or counts["restored"]:
        n = counts["restored"] or len(lines)
        if n:
            bits.append(f"вернул {n}")
    if counts["added"]:
        bits.append(f"добавил {counts['added']}")
    if counts["changed"]:
        bits.append(f"изменил {counts['changed']}")
    if counts["hidden"]:
        bits.append(f"скрыл {counts['hidden']}")
    if counts["deleted"]:
        bits.append(f"удалил {counts['deleted']}")
    head = "Подробнее"
    if bits:
        head += ": " + ", ".join(bits)
    # Если правка одна — сразу показать дату/площадку в summary.
    actions = [a for a in _audit_collect_actions(details) if a.get("change")]
    if len(actions) == 1:
        where = _audit_event_line(actions[0])
        if where:
            return f"{head} — {where}"
    if len(lines) == 1 and not bits:
        short = lines[0]
        if len(short) > 90:
            short = short[:87] + "…"
        return f"{head} — {short}"
    return head


def _audit_friendly_details_html(action: str, details: dict) -> str:
    lines = _audit_friendly_detail_lines(action, details)
    if not lines:
        return ""
    items = []
    for line in lines:
        if line.endswith(":") and not line.startswith("…"):
            items.append(f'<li class="audit-item-group">{_h(line)}</li>')
        else:
            items.append(f"<li>{_h(line)}</li>")
    body = f'<ul class="audit-item-list">{"".join(items)}</ul>'
    # Афиша: подробный список дат под катом, чтобы лента не раздувалась.
    if action in {"events_save", "events_restore"}:
        summary = _audit_afisha_cut_summary(action, details, lines)
        return (
            f'<details class="audit-cut">'
            f"<summary>{_h(summary)}</summary>"
            f"{body}"
            f"</details>"
        )
    summary = lines[0]
    rest_html = ""
    if len(lines) > 1:
        rest_items = []
        for line in lines[1:]:
            if line.endswith(":") and not line.startswith("…"):
                rest_items.append(f'<li class="audit-item-group">{_h(line)}</li>')
            else:
                rest_items.append(f"<li>{_h(line)}</li>")
        rest_html = f'<ul class="audit-item-list">{"".join(rest_items)}</ul>'
    return f'<p class="audit-item-details">{_h(summary)}</p>{rest_html}'


def _audit_friendly_title(
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict | None = None,
) -> str:
    label, _tone = _audit_action_meta(action)
    details = details or {}
    action = (action or "").strip()

    if action == "events_save":
        afisha = _AUDIT_AFISHA_LABELS.get(
            str(details.get("format") or entity_id or ""),
            entity_id or "афиша",
        )
        actions = [a for a in _audit_collect_actions(details) if isinstance(a, dict)]
        # Одна правка — сразу дата/площадка в заголовке.
        if len(actions) == 1:
            item = actions[0]
            where = _audit_event_line(item)
            verb = _AUDIT_CHANGE_VERBS.get(str(item.get("change") or "").strip(), "обновил")
            if where:
                return f"Правки в афише «{afisha}»: {verb} {where}"
        counts = _audit_afisha_change_counts(details)
        # Подстраховка счётчиками из корня details (старые/урезанные записи).
        for key in ("added", "changed", "hidden", "deleted"):
            if not counts.get(key):
                counts[key] = int(_audit_num(details, key) or 0)
        kinds = []
        if counts["added"]:
            kinds.append(f"добавил {counts['added']}")
        if counts["changed"]:
            kinds.append(f"изменил {counts['changed']}")
        if counts["hidden"]:
            kinds.append(f"скрыл {counts['hidden']}")
        if counts["deleted"]:
            kinds.append(f"удалил {counts['deleted']}")
        if kinds:
            return f"Правки в афише «{afisha}»: " + ", ".join(kinds)
        return f"Сохранение афиши «{afisha}»"

    if action == "events_restore":
        afisha = _AUDIT_AFISHA_LABELS.get(str(entity_id or ""), entity_id or "афиша")
        n = int(_audit_num(details, "restored") or len(_audit_collect_actions(details)) or 0)
        return f"Вернул даты в афишу «{afisha}»" + (f" ({n})" if n else "")

    if action in {"events_cancel_notify", "events_cancel_bookings"}:
        events = details.get("events") or details.get("hidden_items") or []
        if isinstance(events, list) and events:
            lines = [_audit_event_line(e) for e in events[:2] if isinstance(e, dict)]
            lines = [x for x in lines if x]
            if lines:
                more = f" и ещё {len(events) - 2}" if len(events) > 2 else ""
                return f"{label}: {'; '.join(lines)}{more}"

    if action == "resend_ticket":
        bits = [
            f"#{entity_id}" if entity_id else "",
            str(details.get("name") or "").strip(),
            " · ".join(
                p
                for p in [
                    str(details.get("date") or "").strip(),
                    str(details.get("time") or "").strip(),
                ]
                if p
            ),
        ]
        target = " · ".join(p for p in bits if p)
        if target:
            return f"{label}: {target}"

    if action == "resend_tickets_event":
        event = details.get("event") if isinstance(details.get("event"), dict) else {}
        event_line = _audit_event_line(event) if event else ""
        if event_line:
            return f"{label}: {event_line}"

    target = _audit_target_label(entity_type, entity_id)
    if target and action != "login":
        return f"{label}: {target}"
    return label


def _audit_section(rows: list[dict] | None) -> str:
    """Technical table for owner DB tab (raw codes kept for debugging)."""
    rows = rows or []
    body = []
    for row in rows:
        details = row.get("details") or {}
        if isinstance(details, str):
            details_txt = details
        else:
            try:
                import json as _json

                details_txt = _json.dumps(details, ensure_ascii=False)
            except Exception:
                details_txt = str(details)
        if len(details_txt) > 180:
            details_txt = details_txt[:177] + "…"
        body.append(
            "<tr>"
            f"<td>{_h(_fmt_msk(row.get('created_at')))}</td>"
            f"<td>{_h(row.get('actor_role') or '—')}</td>"
            f"<td>{_h(row.get('action') or '')}</td>"
            f"<td>{_h(row.get('entity_type') or '')} {_h(row.get('entity_id') or '')}</td>"
            f"<td class='muted'>{_h(details_txt)}</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="5" class="muted">Пока пусто — действия появятся здесь.</td></tr>')
    return (
        '<section class="card audit-log">'
        "<h2>Журнал действий (технический)</h2>"
        '<p class="muted">Сырые коды для отладки. Удобный вид — во вкладке «Журнал».</p>'
        '<div class="table-wrap"><table><thead><tr>'
        "<th>Когда</th><th>Роль</th><th>Действие</th><th>Объект</th><th>Детали</th>"
        f"</tr></thead><tbody>{''.join(body)}</tbody></table></div>"
        "</section>"
    )


def _audit_tab(rows: list[dict] | None) -> str:
    """Client-friendly activity feed for owner/client."""
    rows = rows or []
    items = []
    for row in rows:
        action = (row.get("action") or "").strip()
        details = _audit_details_dict(row)
        title = _audit_friendly_title(
            action,
            row.get("entity_type") or "",
            row.get("entity_id") or "",
            details,
        )
        _, tone = _audit_action_meta(action)
        role = _audit_role_label(row.get("actor_role") or "")
        when = _fmt_msk(row.get("created_at"))
        detail_html = _audit_friendly_details_html(action, details)
        items.append(
            f'<article class="audit-item audit-item--{tone}">'
            f'<div class="audit-item-top">'
            f'<span class="audit-role audit-role--{(row.get("actor_role") or "other")}">{_h(role)}</span>'
            f'<time class="audit-when muted">{_h(when)}</time>'
            f"</div>"
            f'<h3 class="audit-item-title">{_h(title)}</h3>'
            f"{detail_html}"
            "</article>"
        )
    feed = (
        "".join(items)
        if items
        else '<p class="muted audit-empty">Пока записей нет — здесь появятся сохранения афиши, рассылки и переотправки билетов.</p>'
    )
    return (
        '<section class="card audit-feed">'
        "<h2>Журнал действий</h2>"
        '<p class="muted">Кто что менял в админке: афиша, отмены, рассылки, переотправка билетов.</p>'
        f'<div class="audit-list">{feed}</div>'
        "</section>"
    )


def _db_tab(
    tables: list[dict],
    browse: dict | None,
    filters: dict,
    audit_rows: list[dict] | None = None,
) -> str:
    audit_html = _audit_section(audit_rows)
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
        return audit_html + nav + (
            '<section class="card empty-state">'
            "<h2>Выберите таблицу</h2>"
            "<p>Нажмите на таблицу выше, чтобы увидеть строки как в Excel.</p>"
            "</section>"
        )
    if browse.get("error"):
        return audit_html + nav + f'<section class="card empty-state"><h2>{_h(browse["error"])}</h2></section>'

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
        audit_html
        + nav
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
    links = [
        f'<a class="pill {"active" if not filters.get("status") else ""}" '
        f'href="{_query_link(filters, status="", page="")}">Все статусы</a>'
    ]
    for status in STATUSES:
        links.append(
            f'<a class="pill {"active" if filters.get("status") == status else ""}" '
            f'href="{_query_link(filters, status=status, page="")}">{_h(STATUS_LABELS[status])}</a>'
        )
    return "".join(links)


def _users_channel_filter(filters: dict) -> str:
    links = []
    for key, label in (("", "Все каналы"), ("telegram", "Telegram"), ("vkontakte", "VK")):
        active = "active" if (filters.get("channel") or "") == key else ""
        links.append(
            f'<a class="pill {active}" '
            f'href="{_query_link(filters, tab="users", channel=key, page="", u="")}">{label}</a>'
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
    """Fallback booking portfolio summary (users table column)."""
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


def _user_stage_line(user: dict, last_event: dict | None = None) -> str:
    """Актуальный этап клиента: последнее tracked-событие или сводка по броням."""
    if last_event:
        name = last_event.get("name") or ""
        place = LAST_PLACE_LABELS.get(name) or ACTIVITY_SHORT_LABELS.get(name) or name or "событие"
        props = last_event.get("props") or {}
        if isinstance(props, str):
            props = {}
        extras = []
        if props.get("format"):
            extras.append(str(props["format"]))
        if props.get("browse") == "venue":
            extras.append("площадка")
        elif props.get("browse") == "date":
            extras.append("дата")
        if props.get("location"):
            extras.append(str(props["location"]))
        if extras:
            place = f"{place} · {', '.join(extras)}"
        channel = last_event.get("channel") or ""
        if channel == "vkontakte":
            place = f"VK · {place}"
        when = _fmt_msk(last_event.get("created_at"))
        return f"{place} ({when})"
    return _user_stage(user)


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


def _user_reminders_html(bookings: list[dict]) -> str:
    from bot.utils.reminder_schedule import plan_booking_reminders
    from bot.utils.ticket import parse_created_at, parse_event_datetime

    now = datetime.now(MSK).replace(tzinfo=None)
    # Newest upcoming first; skip past event dates.
    relevant = []
    for b in bookings:
        if b.get("status") not in {"booked", "confirmed"}:
            continue
        event_dt = parse_event_datetime(b.get("event_date") or "", b.get("event_time") or "")
        if event_dt and event_dt < now:
            continue
        relevant.append(b)
    if not relevant:
        return '<p class="muted">Нет актуальных броней для напоминаний.</p>'

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


def _user_activity_html(activity_counts: list[dict], recent: list[dict] | None = None) -> str:
    if not activity_counts and not recent:
        return '<p class="muted">Пока нет событий аналитики по этому гостю.</p>'

    order = [
        "bot_start",
        "cmd_my_bookings",
        "cmd_main_menu",
        "cmd_buy_ticket",
        "cmd_help",
        "cmd_channel",
        "branch_proverka",
        "branch_best",
        "branch_hitloto",
        "browse_dates",
        "browse_venues",
        "show_card",
        "booking_start",
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
    short_labels = ACTIVITY_SHORT_LABELS
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
        word = "раз" if n == 1 else "раза" if 2 <= n <= 4 else "раз"
        rows.append(
            "<tr>"
            f"<td>{_h(short_labels.get(name, name))}</td>"
            f"<td><b>{n}</b> <span class='muted'>{word}</span></td>"
            "</tr>"
        )
    for name, n in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        if name in seen:
            continue
        word = "раз" if n == 1 else "раза" if 2 <= n <= 4 else "раз"
        rows.append(
            "<tr>"
            f"<td>{_h(short_labels.get(name, name))}</td>"
            f"<td><b>{n}</b> <span class='muted'>{word}</span></td>"
            "</tr>"
        )
    counts_html = ""
    if rows:
        counts_html = (
            '<div class="table-wrap"><table class="user-extra user-activity-summary">'
            "<thead><tr><th>Событие</th><th>Сколько раз</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    recent_html = ""
    if recent:
        recent_rows = []
        for row in recent[:20]:
            name = row.get("name") or ""
            label = LAST_PLACE_LABELS.get(name) or short_labels.get(name) or name
            channel = row.get("channel") or ""
            channel_note = f" · {channel}" if channel and channel != "telegram" else ""
            props = row.get("props") or {}
            if isinstance(props, str):
                props = {}
            detail_bits = []
            if props.get("format"):
                detail_bits.append(str(props.get("format")))
            if props.get("browse"):
                detail_bits.append("по площадке" if props.get("browse") == "venue" else "по дате")
            if props.get("location"):
                detail_bits.append(str(props.get("location")))
            if props.get("date"):
                detail_bits.append(str(props.get("date")))
            detail = f" ({', '.join(detail_bits)})" if detail_bits else ""
            recent_rows.append(
                "<tr>"
                f"<td>{_h(_fmt_msk(row.get('created_at')))}</td>"
                f"<td>{_h(label)}{_h(detail)}<span class='muted'>{_h(channel_note)}</span></td>"
                "</tr>"
            )
        recent_html = (
            '<p class="muted" style="margin:12px 0 6px">Последние шаги</p>'
            '<div class="table-wrap"><table class="user-extra user-activity-summary">'
            "<thead><tr><th>Когда</th><th>Куда зашёл</th></tr></thead>"
            f"<tbody>{''.join(recent_rows)}</tbody></table></div>"
        )
    if not counts_html and not recent_html:
        return '<p class="muted">Пока нет событий аналитики по этому гостю.</p>'
    return counts_html + recent_html


def _looks_like_telegram_file_id(value: str) -> bool:
    """TG file_id vs VK ref / url, stored historically in photo_file_id."""
    v = (value or "").strip()
    if not v or len(v) < 16:
        return False
    if v.startswith(("vk_", "http://", "https://", "photo")):
        return False
    return True


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
    cards = []
    total = len(submissions)
    for idx, row in enumerate(submissions):
        kind = kind_labels.get(row.get("kind"), row.get("kind") or "—")
        status = row.get("status") or ""
        status_l = status_labels.get(status, status or "—")
        status_mod = status if status in {"pending", "approved", "rejected"} else "other"
        created = _fmt_msk(row.get("created_at"))
        reviewed = _fmt_msk(row.get("reviewed_at")) if row.get("reviewed_at") else ""
        facts = [f"<div><dt>Создана</dt><dd>{_h(created)}</dd></div>"]
        if reviewed:
            facts.append(f"<div><dt>Решение</dt><dd>{_h(reviewed)}</dd></div>")
        if row.get("source_message_id"):
            facts.append(
                f"<div><dt>В чате</dt><dd>#{_h(str(row.get('source_message_id')))}</dd></div>"
            )
        reject_html = ""
        if status == "rejected" or row.get("reject_reason"):
            reason = (row.get("reject_reason") or "").strip() or "не указана"
            reject_html = (
                f'<p class="screen-card-reject"><b>Причина отклонения:</b> {_h(reason)}</p>'
            )
        file_html = ""
        sid = row.get("id")
        file_id = (row.get("photo_file_id") or "").strip()
        if sid and file_id:
            preview_url = f"/admin/raffle-screen/{int(sid)}"
            if _looks_like_telegram_file_id(file_id):
                file_html = (
                    f'<div class="screen-preview">'
                    f'<a class="pill" href="{_h(preview_url)}" target="_blank" rel="noopener">Показать скрин</a>'
                    f'<img src="{_h(preview_url)}" alt="Скрин заявки #{_h(str(sid))}" loading="lazy" />'
                    f"</div>"
                )
            else:
                file_html = (
                    '<p class="muted screen-preview-note">'
                    "Превью недоступно для этой заявки — смотрите скрин в чате модерации."
                    "</p>"
                )
            file_html += (
                '<details class="screen-file-id">'
                "<summary>file_id</summary>"
                f"<code>{_h(file_id)}</code>"
                "</details>"
            )
        cards.append(
            f'<article class="screen-card screen-card--{status_mod}" data-idx="{idx}">'
            f'<div class="screen-card-top">'
            f'<span class="screen-card-title">#{_h(str(row.get("id")))} · {_h(kind)}</span>'
            f'<span class="badge screen-status screen-status--{status_mod}">{_h(status_l)}</span>'
            f"</div>"
            f'<dl class="screen-card-facts">{"".join(facts)}</dl>'
            f"{reject_html}"
            f"{file_html}"
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


def _user_tickets_html(
    bookings: list[dict],
    user_key: str = "",
    *,
    can_resend_tickets: bool = True,
) -> str:
    """Cards for tickets the guest actually received (confirmed_at), including later cancels."""
    tickets = [
        b
        for b in bookings
        if b.get("status") == "confirmed" or b.get("confirmed_at_raw")
    ]
    if not tickets:
        return '<p class="muted">Полученных билетов пока нет.</p>'

    def _sort_key(b: dict):
        return (
            b.get("event_date") or "",
            b.get("event_time") or "",
            str(b.get("id") or ""),
        )

    tickets = sorted(tickets, key=_sort_key, reverse=True)
    status_labels = {
        "confirmed": "получен",
        "cancelled": "отменён",
        "annulled": "аннулирован",
        "booked": "бронь",
    }
    cards = []
    total = len(tickets)
    for idx, row in enumerate(tickets):
        status = row.get("status") or ""
        status_mod = status if status in {"confirmed", "cancelled", "annulled"} else "other"
        status_l = status_labels.get(status, row.get("status_label") or status or "—")
        fmt = FORMAT_LABELS.get(row.get("format"), row.get("format") or "—")
        loc = row.get("location") or "—"
        if row.get("address"):
            loc = f"{loc}, {row.get('address')}"
        facts = [
            f"<div><dt>Тип</dt><dd>{_h(fmt)}</dd></div>",
            f"<div><dt>Дата</dt><dd>{_h(row.get('event_date') or '—')}</dd></div>",
            f"<div><dt>Время</dt><dd>{_h(row.get('event_time') or '—')}</dd></div>",
            f"<div><dt>Гости</dt><dd>{_h(str(row.get('guests') or 0))}</dd></div>",
            f"<div><dt>Локация</dt><dd class='ticket-loc'>{_h(loc)}</dd></div>",
        ]
        if row.get("confirmed_at_raw"):
            facts.append(
                f"<div><dt>Выдан</dt><dd>{_h(_fmt_msk(row.get('confirmed_at_raw')))}</dd></div>"
            )
        note = ""
        resend = ""
        if status == "cancelled":
            when = _fmt_msk(row.get("cancelled_at_raw")) if row.get("cancelled_at_raw") else ""
            note = (
                f'<p class="ticket-card-note ticket-card-note--cancelled">'
                f"<b>Билет отменён</b>{' · ' + _h(when) if when and when != '—' else ''}</p>"
            )
        elif status == "annulled":
            when = _fmt_msk(row.get("annulled_at_raw")) if row.get("annulled_at_raw") else ""
            note = (
                f'<p class="ticket-card-note ticket-card-note--annul">'
                f"<b>Билет аннулирован</b>{' · ' + _h(when) if when and when != '—' else ''}</p>"
            )
        elif status == "confirmed" and can_resend_tickets:
            resend = (
                f'<form method="post" action="/admin/events/resend-ticket" class="ticket-resend-form">'
                f'<input type="hidden" name="booking_id" value="{_h(str(row.get("id")))}">'
                f'<input type="hidden" name="updated" value="0">'
                f'<input type="hidden" name="back" value="user">'
                f'<input type="hidden" name="u" value="{_h(user_key)}">'
                '<label class="events-note-label">Сообщение с билетом <span class="muted">(необяз.)</span></label>'
                '<textarea name="extra_note" class="events-extra-note" rows="2" '
                'placeholder="Например: дата изменилась — актуальный билет"></textarea>'
                '<button type="submit">Переотправить билет</button>'
                "</form>"
            )
        cards.append(
            f'<article class="screen-card ticket-card ticket-card--{status_mod}" data-idx="{idx}">'
            f'<div class="screen-card-top">'
            f'<span class="screen-card-title">Билет #{_h(str(row.get("id")))}</span>'
            f'<span class="badge screen-status screen-status--{status_mod}">{_h(status_l)}</span>'
            f"</div>"
            f'<dl class="screen-card-facts">{"".join(facts)}</dl>'
            f"{note}"
            f"{resend}"
            f'<div class="screen-card-nav muted">{idx + 1} / {total}</div>'
            "</article>"
        )
    return (
        '<div class="screen-carousel" tabindex="0">'
        + "".join(cards)
        + "</div>"
        + '<p class="muted screen-carousel-hint">Листайте карточки билетов вбок</p>'
    )


def _user_extra_details(
    title: str,
    body: str,
    *,
    tone: str = "",
    open_by_default: bool = False,
) -> str:
    opened = " open" if open_by_default else ""
    tone_cls = f" user-extra-{tone}" if tone else ""
    return (
        f'<details class="user-extra-details{tone_cls}" data-persist-key="user:{_h(title)}"{opened}>'
        f'<summary class="user-extra-summary"><strong>{_h(title)}</strong>'
        '<span class="details-action"><span class="closed-label">Развернуть</span>'
        '<span class="open-label">Свернуть</span></span></summary>'
        f'<div class="user-extra-body">{body}</div>'
        "</details>"
    )


def _users_tab(
    dashboard: dict,
    filters: dict,
    user_extras: dict | None = None,
    flash: str = "",
    *,
    can_resend_tickets: bool = True,
    can_anonymize_user: bool = False,
    stage_by_user: dict | None = None,
) -> str:
    status = filters.get("status") or ""
    meta = dashboard.get("users_meta") or {}
    list_keys = meta.get("list_keys") or []
    if list_keys:
        list_users = [
            dashboard["users"][key]
            for key in list_keys
            if key in dashboard["users"]
        ]
    else:
        list_users = sorted(
            dashboard["users"].values(),
            key=lambda u: (_parse_int(u.get("user_id")), u["name"] or "", u["phone"] or ""),
        )
    selected_key = filters.get("u", "")
    stage_by_user = stage_by_user or {}
    rows = []
    for user in list_users:
        last_event = None
        uid = user.get("user_id")
        if uid is not None:
            try:
                last_event = stage_by_user.get(int(uid))
            except (TypeError, ValueError):
                last_event = None
        stage_text = _user_stage_line(user, last_event)
        total_bookings = user.get("bookings_total")
        if total_bookings is None:
            total_bookings = len(user.get("bookings") or [])
        channel = _user_channel(user)
        source = (user.get("source") or "").strip()
        source_note = f" · {_h(source)}" if source and source not in ("telegram", "vkontakte") else ""
        username = (user.get("username") or "").strip()
        phone = (user.get("phone") or "").strip()
        contact_main = _h(phone) if phone else "—"
        contact_sub = f"@{_h(username)}" if username else ""
        contact_cell = (
            f"{contact_main}<br><span class='muted'>{contact_sub}</span>"
            if contact_sub
            else contact_main
        )
        rows.append(
            "<tr>"
            f"<td>{_h(user.get('user_id') or '—')}</td>"
            f"<td><a href='{_query_link(filters, u=user['key'], page=filters.get('page') or '1')}'>{_h(user['name'] or 'Без имени')}</a>"
            f"<br>{_channel_badge_html(channel)}"
            f"<span class='muted'>{source_note}</span></td>"
            f"<td>{contact_cell}</td>"
            f"<td>{int(total_bookings)}</td>"
            f"<td>{user['status_counts'].get('booked', 0)}</td>"
            f"<td>{user['status_counts'].get('confirmed', 0)}</td>"
            f"<td>{user['status_counts'].get('cancelled', 0)}</td>"
            f"<td>{_h(stage_text)}</td>"
            "</tr>"
        )
    q_val = filters.get("q") or meta.get("q") or ""
    page = int(meta.get("page") or _parse_int(filters.get("page"), 1) or 1)
    pages = int(meta.get("pages") or 1)
    total = int(meta.get("total") or len(list_users))
    prev_link = (
        f'<a class="pill" href="{_query_link(filters, page=str(page - 1))}">← Назад</a>'
        if page > 1
        else ""
    )
    next_link = (
        f'<a class="pill" href="{_query_link(filters, page=str(page + 1))}">Вперёд →</a>'
        if page < pages
        else ""
    )
    pager = (
        '<div class="users-pager">'
        f"{prev_link}"
        f'<span class="muted">Страница <b>{page}</b> из <b>{pages}</b> · найдено <b>{total}</b></span>'
        f"{next_link}"
        "</div>"
    )
    channel = filters.get("channel") or ""
    search_bar = (
        '<div class="filters users-filters">'
        f"<div>{_users_channel_filter(filters)}</div>"
        f"<div>{_status_filter(filters)}</div>"
        '<form method="get" action="/admin" class="users-search-form">'
        '<input type="hidden" name="tab" value="users">'
        f'<input type="hidden" name="status" value="{_h(status)}">'
        f'<input type="hidden" name="channel" value="{_h(channel)}">'
        f'<input type="search" name="q" value="{_h(q_val)}" '
        'placeholder="Имя, @username или телефон" '
        'autocomplete="off">'
        '<button type="submit">Найти</button>'
        f'<a class="pill" href="/admin?tab=users">Сбросить</a>'
        "</form>"
        "</div>"
    )
    table = (
        '<div class="table-wrap"><table class="users">'
        "<thead><tr><th>user_id</th><th>Клиент</th><th>Контакт</th><th>Всего</th><th>Активные</th>"
        "<th>Билеты</th><th>Отмены</th><th>Этап</th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=\"8\" class=\"muted\">Никого не найдено — измените поиск или фильтр</td></tr>'}</tbody>"
        "</table></div>"
    )
    flash_html = f'<p class="events-flash">{_h(flash)}</p>' if flash else ""
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
        extras = user_extras or {}
        stage_text = _user_stage_line(user, extras.get("last_event"))
        phone_s = (user.get("phone") or "").strip()
        already_anon = (
            (user.get("name") or "").strip() == "Удалён"
            and phone_s.startswith("deleted-")
            and not user.get("telegram_id")
            and not user.get("vk_id")
        )
        anonymize_block = ""
        if can_anonymize_user:
            if already_anon:
                anonymize_block = (
                    '<div class="user-anonymize-box">'
                    "<b>ПДн:</b> персональные данные уже обезличены."
                    "</div>"
                )
            else:
                anonymize_block = (
                    '<div class="user-anonymize-box">'
                    "<b>Запрос на удаление данных</b>"
                    '<p class="muted" style="margin:6px 0 10px">'
                    "Стереть имя, телефон и Telegram/VK ID. Активные брони отменятся. "
                    "История броней без контактов останется."
                    "</p>"
                    '<form method="post" action="/admin/users/anonymize" class="ticket-resend-form">'
                    f'<input type="hidden" name="user_id" value="{_h(user.get("user_id") or "")}">'
                    f'<input type="hidden" name="u" value="{_h(user["key"])}">'
                    '<button type="submit" onclick="return confirm('
                    "'Обезличить этого гостя? Действие нельзя отменить.');\">"
                    "Удалить персональные данные"
                    "</button>"
                    "</form>"
                    "</div>"
                )
        if user.get("consent_accepted_at"):
            consent_ver = (user.get("consent_version") or "").strip()
            consent_box = (
                '<div class="user-consent-box user-consent-yes">'
                "<b>Согласие на ПДн:</b> да"
                f' · {_h(_fmt_msk(user.get("consent_accepted_at")))}'
                + (f' · {_h(consent_ver)}' if consent_ver else "")
                + "</div>"
            )
        else:
            consent_box = (
                '<div class="user-consent-box user-consent-no">'
                "<b>Согласие на ПДн:</b> нет"
                "</div>"
            )
        detail_channel = _user_channel(user)
        detail_username = (user.get("username") or "").strip()
        detail_phone = (user.get("phone") or "").strip() or "—"
        detail_user_bit = f" · @{_h(detail_username)}" if detail_username else ""
        detail = (
            '<section class="card user-detail">'
            f'<h2>{_h(user["name"] or "Без имени")} {_channel_badge_html(detail_channel)}</h2>'
            f'<p class="muted">user_id: {_h(user.get("user_id") or "—")} · {_h(detail_phone)}'
            f'{detail_user_bit} · источник: {_h(user["source"])}</p>'
            f"{consent_box}"
            '<div class="mini-metrics">'
            f'<span>Всего броней: <b>{len(user["bookings"])}</b></span>'
            f'<span>Активных: <b>{user["status_counts"].get("booked", 0)}</b></span>'
            f'<span>Билетов: <b>{user["status_counts"].get("confirmed", 0)}</b></span>'
            f'<span>Отмен: <b>{user["status_counts"].get("cancelled", 0)}</b></span>'
            f'<span>Напоминание за сутки: <b>{reminders_24h}</b></span>'
            f'<span>Напоминание в день: <b>{reminders_day}</b></span>'
            '</div>'
            f'<p class="user-stage"><b>Где сейчас:</b> {_h(stage_text)}</p>'
            f"{empty_note}"
            f"{anonymize_block}"
            '<div class="user-extra-stack">'
            f'{_user_extra_details("Куда заходил", _user_activity_html(extras.get("activity_counts") or [], extras.get("activity_recent") or []), tone="activity")}'
            f'{_user_extra_details("Напоминания", _user_reminders_html(user["bookings"]), tone="reminders")}'
            f'{_user_extra_details("Билеты", _user_tickets_html(user["bookings"], user_key=user["key"], can_resend_tickets=can_resend_tickets), tone="tickets")}'
            f'{_user_extra_details("Скрины розыгрыша", _user_raffle_html(extras.get("submissions") or [], extras.get("flags") or {}), tone="raffle")}'
            "</div>"
            f'{_booking_table(bookings, show_format=True)}'
            "</section>"
        )
    return (
        flash_html
        + detail
        + '<section class="card">'
        "<h2>Пользователи</h2>"
        '<p class="muted">Поиск и страницы по 50 человек — база не выводится целиком.</p>'
        f"{search_bar}{pager}{table}{pager}"
        "</section>"
    )


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
        '<h3 class="analytics-section-title">Основные события за период</h3>'
        '<div class="summary analytics-summary">'
        f'{_analytics_metric_card("Зашли в бот", by_name.get("bot_start"), css_class="tone-bot-start")}'
        f'{_analytics_metric_card("Зашли · Проверка", by_name.get("branch_proverka"), css_class="tone-proverka")}'
        f'{_analytics_metric_card("Зашли · BEST", by_name.get("branch_best"), css_class="tone-best")}'
        f'{_analytics_metric_card("Зашли · Hit Loto", by_name.get("branch_hitloto"), css_class="tone-hitloto")}'
        f'{_analytics_metric_card("Help / FAQ · обращение", by_name.get("cmd_help") or by_name.get("help_open"))}'
        f'{_analytics_metric_card("Брони созданы · проверка", proverka_overview.get("created"))}'
        f'{_analytics_metric_card("Билет получен · проверка", proverka_overview.get("confirmed"))}'
        f'{_analytics_metric_card("Отмены брони · проверка", proverka_overview.get("cancelled"))}'
        f'{_analytics_metric_card("Отправили скрин · розыгрыш", by_name.get("raffle_screenshot"))}'
        f'{_analytics_metric_card("Посетили розыгрыш", raffle_overview.get("visited"))}'
        "</div>"
    )

    if channel == "vkontakte":
        cmd_labels = {
            "cmd_my_bookings": "Мои брони",
            "cmd_main_menu": "Главное меню",
            "cmd_buy_ticket": "Купить билет",
            "cmd_help": "Задать вопрос · менеджеру",
            "cmd_channel": "Канал анонсов",
            "bot_start": "Зашли в бот",
        }
    else:
        cmd_labels = {
            "cmd_my_bookings": "/my_bookings · Мои брони",
            "cmd_main_menu": "/main_menu · Главное меню",
            "cmd_buy_ticket": "/buy_ticket · Купить билет",
            "cmd_help": "/help · Задать вопрос",
            "cmd_channel": "/channel · Канал анонсов",
            "bot_start": "Зашли в бот (/start)",
        }
    event_labels = {
        **cmd_labels,
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
    always_show_groups = {"Розыгрыш", "Ветки", "Брони · Проверка", "Запуск команд меню"}
    event_groups = [
        ("Вход в бот", ["bot_start"]),
        (
            "Запуск команд меню",
            [
                "cmd_my_bookings",
                "cmd_main_menu",
                "cmd_buy_ticket",
                "cmd_help",
                "cmd_channel",
            ],
        ),
        ("Ветки", ["branch_proverka", "branch_best", "branch_hitloto"]),
        ("Карточки концертов", []),  # filled from show_cards breakdown below
        ("Помощь", ["help_question"]),
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
        '<details data-persist-key="analytics:all-events">'
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
        '<section class="card details-card analytics-section">'
        '<details data-persist-key="analytics:starts-by-link">'
        '<summary class="details-summary">'
        "<div>"
        "<strong>Входы в бот по ссылкам</strong>"
        '<span class="muted">Все заходы и уникальные люди</span>'
        "</div>"
        '<span class="details-action"><span class="closed-label">Развернуть</span>'
        '<span class="open-label">Свернуть</span></span>'
        "</summary>"
        '<div class="details-body">'
        '<div class="table-wrap"><table><thead><tr>'
        "<th>Вход</th><th>Все заходы</th><th>Уникальные люди</th>"
        "</tr></thead>"
        f"<tbody>{''.join(payload_rows) or '<tr><td colspan=\"3\" class=\"muted\">Пока нет данных</td></tr>'}</tbody>"
        "</table></div>"
        "</div></details></section>"
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
        '<section class="card details-card analytics-section">'
        '<details data-persist-key="analytics:show-cards">'
        '<summary class="details-summary">'
        "<div>"
        "<strong>Просмотры карточек шоу</strong>"
        '<span class="muted">Открытия карточек по способу поиска</span>'
        "</div>"
        '<span class="details-action"><span class="closed-label">Развернуть</span>'
        '<span class="open-label">Свернуть</span></span>'
        "</summary>"
        '<div class="details-body">'
        f'<div class="show-format-grid">{"".join(show_blocks)}</div>'
        "</div></details></section>"
    )

    is_vk_channel = channel == "vkontakte"
    command_blocks = []
    for name, cmd, title, tone in (
        ("cmd_my_bookings", "/my_bookings", "Мои брони", "tone-cmd-bookings"),
        ("cmd_main_menu", "/main_menu", "Главное меню", "tone-cmd-menu"),
        ("cmd_buy_ticket", "/buy_ticket", "Купить билет", "tone-cmd-buy"),
        ("cmd_help", "/help", "Задать вопрос", "tone-cmd-help"),
        ("cmd_channel", "/channel", "Канал анонсов", "tone-cmd-channel"),
    ):
        metric = by_name.get(name) or {"events": 0, "uniques": 0}
        events = int(metric.get("events") or 0)
        uniques = int(metric.get("uniques") or 0)
        cmd_html = (
            ""
            if is_vk_channel
            else f'<div class="command-block-cmd">{_h(cmd)}</div>'
        )
        command_blocks.append(
            f'<div class="command-block {tone}">'
            f"{cmd_html}"
            f'<div class="command-block-title">{_h(title)}</div>'
            f'<b>{events}</b>'
            f'<small class="muted">{uniques} уник.</small>'
            "</div>"
        )
    if is_vk_channel:
        commands_hint = (
            "Переходы из меню VK · «Задать вопрос» — кнопка менеджеру, "
            "«Канал анонсов» — кнопка канала · заходы и уникальные люди"
        )
    else:
        commands_hint = "Переходы по командам из меню Telegram · заходы и уникальные люди"
    commands_table = (
        '<section class="card details-card analytics-section">'
        '<details data-persist-key="analytics:menu-commands">'
        '<summary class="details-summary">'
        "<div>"
        "<strong>Запуск команд меню</strong>"
        f'<span class="muted">{_h(commands_hint)}</span>'
        "</div>"
        '<span class="details-action"><span class="closed-label">Развернуть</span>'
        '<span class="open-label">Свернуть</span></span>'
        "</summary>"
        '<div class="details-body">'
        f'<div class="command-grid">{"".join(command_blocks)}</div>'
        "</div></details></section>"
    )

    raffle_bookings = report.get("raffle_bookings") or {}
    kind_steps = report.get("raffle_kind_steps") or {}
    kind_bookings = report.get("raffle_kind_bookings") or {}
    kind_created = kind_bookings.get("created") or {}
    kind_confirmed = kind_bookings.get("confirmed") or {}
    kind_cancelled = kind_bookings.get("cancelled") or {}

    def _metric_events(metric: dict | None) -> int:
        return int((metric or {}).get("events") or 0)

    def _metric_uniques(metric: dict | None) -> int:
        return int((metric or {}).get("uniques") or 0)

    def _bar_funnel_html(
        steps: list[tuple[str, dict | None]],
        *,
        drops: list[tuple[str, dict | None]] | None = None,
        drop_base: dict | None = None,
    ) -> str:
        base = _metric_events(steps[0][1]) if steps else 0
        prev = base
        book_base = _metric_events(drop_base) if drop_base is not None else 0
        rows = []
        for idx, (title, metric) in enumerate(steps):
            events = _metric_events(metric)
            uniques = _metric_uniques(metric)
            pct_base = round(100 * events / base) if base else (100 if idx == 0 and events else 0)
            if idx == 0:
                pct_note = "100% · начало воронки"
            elif prev:
                pct_prev = round(100 * events / prev)
                lost = max(prev - events, 0)
                pct_note = f"{pct_prev}% от прошлого шага · не перешли: {lost}"
            else:
                pct_note = "—"
            width = max(pct_base, 2 if events else 0)
            rows.append(
                '<div class="bar-funnel-row">'
                f'<div class="bar-funnel-label">{_h(title)}</div>'
                f'<div class="bar-funnel-nums"><b>{events}</b><span>{uniques} чел.</span></div>'
                '<div class="bar-funnel-track">'
                f'<div class="bar-funnel-fill" style="width:{width}%"></div>'
                "</div>"
                f'<div class="bar-funnel-pct">{_h(pct_note)}</div>'
                "</div>"
            )
            prev = events
        for title, metric in drops or []:
            events = _metric_events(metric)
            uniques = _metric_uniques(metric)
            ref = book_base if drop_base is not None else base
            pct = round(100 * events / ref) if ref else 0
            if drop_base is not None:
                pct_note = f"{pct}% от брони" if ref else "—"
            else:
                pct_note = f"{pct}% от начала" if ref else "—"
            width = max(min(pct, 100), 2 if events else 0)
            rows.append(
                '<div class="bar-funnel-row drop">'
                f'<div class="bar-funnel-label">{_h(title)}</div>'
                f'<div class="bar-funnel-nums"><b>{events}</b><span>{uniques} чел.</span></div>'
                '<div class="bar-funnel-track">'
                f'<div class="bar-funnel-fill" style="width:{width}%"></div>'
                "</div>"
                f'<div class="bar-funnel-pct">{_h(pct_note)}</div>'
                "</div>"
            )
        return '<div class="bar-funnel">' + "".join(rows) + "</div>"

    # Проверка: карточки по дате+локации суммарно (заходы); уники — сумма с возможным пересечением.
    proverka_card_events = 0
    proverka_card_uniques = 0
    for row in report.get("show_cards") or []:
        if (row.get("format") or "") != "proverka":
            continue
        proverka_card_events += int(row.get("events") or 0)
        proverka_card_uniques += int(row.get("uniques") or 0)
    proverka_browse = {"events": proverka_card_events, "uniques": proverka_card_uniques}
    proverka_bookings = report.get("proverka_bookings") or {}
    proverka_funnel = (
        '<section class="card details-card analytics-section">'
        '<details data-persist-key="analytics:proverka-funnel">'
        '<summary class="details-summary">'
        "<div>"
        "<strong>Воронка · Проверка</strong>"
        '<span class="muted">Бесплатное бронирование · от входа до билета</span>'
        "</div>"
        '<span class="details-action"><span class="closed-label">Развернуть</span>'
        '<span class="open-label">Свернуть</span></span>'
        "</summary>"
        '<div class="details-body">'
        + _bar_funnel_html(
            [
                ("1. Зашли в бесплатное бронирование (по дате / локации)", proverka_browse),
                ("2. Выбрали дату / начали бронирование", by_name.get("branch_proverka")),
                ("3. Бронь есть", proverka_bookings.get("created")),
                ("4. Получили билет / подтвердили бронь", proverka_bookings.get("confirmed")),
            ],
            drops=[
                ("5. Отменили бронирование", proverka_bookings.get("cancelled")),
                ("6. Бронь аннулирована / не подтвердили", proverka_bookings.get("annulled")),
            ],
            drop_base=proverka_bookings.get("created"),
        )
        + "</div></details></section>"
    )

    raffle_body = _bar_funnel_html(
        [
            ("1. Зашли в розыгрыш", by_name.get("raffle_enter")),
            ("2. Выбрали путь (отзыв / скрин)", by_name.get("raffle_branch")),
            ("3. Отправили скрин", by_name.get("raffle_screenshot")),
            ("4. Скрин принят", by_name.get("raffle_approved")),
            ("5. Подтвердили подписку", by_name.get("raffle_subscribed")),
            ("6. Забронировали бесплатный билет", raffle_bookings.get("created")),
            ("7. Получили билет / подтвердили бронь", raffle_bookings.get("confirmed")),
        ],
        drops=[
            ("8. Отменили бронирование", raffle_bookings.get("cancelled")),
            ("9. Бронь аннулирована / не подтвердили", raffle_bookings.get("annulled")),
        ],
        drop_base=raffle_bookings.get("created"),
    )

    def _branch_metric(title: str, metric: dict | None) -> str:
        metric = metric or {"events": 0, "uniques": 0}
        return (
            '<div class="metric branch-metric">'
            f'<span>{_h(title)}</span><b>{metric.get("events", 0)}</b>'
            f'<small class="muted">{metric.get("uniques", 0)} уникальных</small>'
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
        '<details data-persist-key="analytics:raffle">'
        '<summary class="details-summary">'
        "<div>"
        "<strong>Розыгрыш</strong>"
        '<span class="muted">Воронка от входа до билета</span>'
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

    return (
        filters_bar
        + overview
        + all_events_table
        + proverka_funnel
        + starts_table
        + commands_table
        + cards_table
        + raffle
        + audience_html
    )


def _content(
    dashboard: dict,
    filters: dict,
    db_data: dict | None = None,
    analytics: dict | None = None,
    user_extras: dict | None = None,
    events_bundle: dict | None = None,
    events_flash: str = "",
    events_errors: list[str] | None = None,
    ticket_holders: list[dict] | None = None,
    *,
    can_resend_tickets: bool = True,
    can_anonymize_user: bool = False,
    user_stage_by_user: dict | None = None,
    mailing_data: dict | None = None,
) -> str:
    tab = filters.get("tab") or "date"
    if tab == "bookings":
        return _bookings_tab(dashboard, filters)
    if tab == "users":
        return _users_tab(
            dashboard,
            filters,
            user_extras=user_extras,
            flash=events_flash,
            can_resend_tickets=can_resend_tickets,
            can_anonymize_user=can_anonymize_user,
            stage_by_user=user_stage_by_user,
        )
    if tab == "analytics":
        return _analytics_tab(analytics or {}, filters)
    if tab == "events":
        from bot.admin.events_tab import render_events_tab

        return render_events_tab(
            filters.get("ef") or "best",
            events_bundle,
            flash=events_flash,
            errors=events_errors,
            tickets_event_id=(filters.get("tickets") or "") if can_resend_tickets else "",
            ticket_holders=ticket_holders if can_resend_tickets else None,
            can_resend_tickets=can_resend_tickets,
        )
    if tab == "mailing":
        from bot.admin.mailing_tab import render_mailing_tab

        data = mailing_data or {}
        return render_mailing_tab(
            flash=events_flash or data.get("flash") or "",
            error=data.get("error") or "",
            can_send=can_resend_tickets,
            campaigns=data.get("campaigns"),
            detail=data.get("detail"),
            recipients=data.get("recipients"),
        )
    if tab == "audit":
        db_data = db_data or {"audit": []}
        return _audit_tab(db_data.get("audit") or [])
    if tab == "db":
        db_data = db_data or {"tables": [], "browse": None, "audit": []}
        return _db_tab(
            db_data.get("tables") or [],
            db_data.get("browse"),
            filters,
            audit_rows=db_data.get("audit"),
        )
    return _date_tab(dashboard, filters)


def render_admin_html(
    dashboard: dict,
    filters: dict,
    source_label: str,
    db_data: dict | None = None,
    can_view_db: bool = False,
    analytics: dict | None = None,
    user_extras: dict | None = None,
    events_bundle: dict | None = None,
    events_flash: str = "",
    events_errors: list[str] | None = None,
    ticket_holders: list[dict] | None = None,
    *,
    can_view_ops: bool = True,
    can_resend_tickets: bool = True,
    can_anonymize_user: bool | None = None,
    user_stage_by_user: dict | None = None,
    mailing_data: dict | None = None,
) -> str:
    if can_anonymize_user is None:
        can_anonymize_user = bool(can_view_db)
    totals = dashboard["totals"]
    tab = filters.get("tab") or "date"
    is_db = tab == "db"
    is_audit = tab == "audit"
    is_analytics = tab == "analytics"
    is_events = tab == "events"
    is_mailing = tab == "mailing"
    filters = (
        _normalize_event_filter(dashboard, filters)
        if not is_analytics and not is_events and not is_db and not is_audit and not is_mailing
        else filters
    )
    date_value = _date_to_input(filters.get("date", ""))
    date_input = '<input name="date" type="date" value="{}">'.format(_h(date_value))
    event_select = _event_select(dashboard, filters)
    hidden_status = f'<input type="hidden" name="status" value="{_h(filters.get("status"))}">' if filters.get("status") else ""
    hidden_sort = f'<input type="hidden" name="sort" value="{_h(filters.get("sort"))}">' if filters.get("sort") else ""
    hidden_order = f'<input type="hidden" name="order" value="{_h(filters.get("order"))}">' if filters.get("sort") else ""
    summary_html = ""
    show_booking_chrome = tab in {"date", "bookings"}
    role_class = "role-ops" if can_view_ops else "role-manager"
    if show_booking_chrome:
        # KPI-карточки нужны owner/client; менеджеру на смене они не принципиальны.
        summary_block = ""
        if can_view_ops:
            summary_block = f"""
    <div class="summary booking-summary">
      <div class="metric"><span>Мероприятий</span><b>{totals["events"]}</b></div>
      <div class="metric"><span>Всего броней</span><b>{totals["bookings"]}</b></div>
      <div class="metric"><span>Активные брони, гостей</span><b>{totals["reserved_guests"]}</b></div>
      <div class="metric"><span>Подтвердили билеты</span><b>{totals["confirmed_guests"]}</b></div>
    </div>"""
        summary_html = summary_block + f"""
    <div class="filters booking-filters">
      <div class="filter-status-pills">{_status_filter(filters)}</div>
      <form method="get" action="/admin" class="booking-filters-form">
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
    refresh_meta = (
        ""
        if is_db or is_audit or is_analytics or is_events or is_mailing or tab == "users"
        else '<meta http-equiv="refresh" content="30">'
    )
    header_note = (
        "Без автообновления — форма рассылки не сбрасывается"
        if is_mailing
        else "Автообновление каждые 30 секунд"
    )
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
    main {{ max-width:1680px; margin:0 auto; padding:24px; }}
    body.tab-events main {{ max-width:1840px; }}
    .tabs {{ display:flex; gap:10px; margin:0 0 16px; padding-left:16px; flex-wrap:wrap; }}
    .tab, .pill {{ padding:10px 14px; border-radius:999px; border:1px solid var(--line); color:#111827; background:white; text-decoration:none; }}
    .tab.active, .pill.active {{ background:#111827; color:white; border-color:#111827; }}
    .channel-badge {{
      display:inline-block; margin-right:4px; padding:1px 7px; border-radius:6px;
      font-size:11px; font-weight:700; letter-spacing:.02em; vertical-align:middle;
      border:1px solid var(--line); background:#f8fafc; color:#475467;
    }}
    .channel-badge.channel-tg {{ background:#eff6ff; color:#1d4ed8; border-color:#bfdbfe; }}
    .channel-badge.channel-vk {{ background:#eef2ff; color:#3730a3; border-color:#c7d2fe; }}
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
    .command-grid {{ display:grid; grid-template-columns: repeat(5, minmax(0,1fr)); gap:10px; }}
    .command-block {{
      margin:0; padding:12px 14px; border-radius:12px; border:1px solid var(--line);
      background:#f8fafc; min-height:108px;
    }}
    .command-block-cmd {{ font-size:12px; font-weight:700; font-family:ui-monospace,Consolas,monospace; }}
    .command-block-title {{ margin:4px 0 10px; font-size:13px; color:#475467; }}
    .command-block b {{ display:block; font-size:24px; line-height:1.1; }}
    .command-block small {{ display:block; margin-top:4px; font-size:12px; }}
    .tone-cmd-bookings {{ background:#eff6ff; border-color:#bfdbfe; }}
    .tone-cmd-bookings .command-block-cmd {{ color:#1d4ed8; }}
    .tone-cmd-menu {{ background:#f8fafc; border-color:#cbd5e1; }}
    .tone-cmd-menu .command-block-cmd {{ color:#334155; }}
    .tone-cmd-buy {{ background:#fff7ed; border-color:#fed7aa; }}
    .tone-cmd-buy .command-block-cmd {{ color:#c2410c; }}
    .tone-cmd-help {{ background:#fdf4ff; border-color:#e9d5ff; }}
    .tone-cmd-help .command-block-cmd {{ color:#7e22ce; }}
    .tone-cmd-channel {{ background:#f0fdf4; border-color:#bbf7d0; }}
    .tone-cmd-channel .command-block-cmd {{ color:#15803d; }}
    .users-filters {{ margin:12px 0 14px; }}
    .users-filters > div {{ margin-bottom:8px; }}
    .users-search-form {{
      display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-top:10px;
    }}
    .users-search-form input[type="search"] {{
      flex:1 1 260px; min-width:200px; height:42px; padding:0 12px;
      border:1px solid var(--line); border-radius:10px; font:inherit;
    }}
    .users-pager {{
      display:flex; flex-wrap:wrap; gap:10px; align-items:center;
      margin:10px 0 12px;
    }}
    .events-subtabs {{ display:flex; gap:8px; flex-wrap:wrap; }}
    .events-filters {{ margin-bottom:16px; }}
    .events-toolbar {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin:12px 0; }}
    .events-toolbar-bottom {{ margin-top:16px; }}
    .events-must-update {{
      margin:10px 0 0; padding:10px 12px; border-radius:10px;
      background:#fff7ed; border:1px solid #fdba74; color:#9a3412; font-size:14px;
    }}
    .events-edit-hint {{
      margin:12px 0; padding:12px 14px; border-radius:12px;
      background:#fef3c7; border:1px solid #f59e0b; color:#92400e; font-size:14px;
    }}
    .events-edit-hint[hidden] {{ display:none; }}
    .events-errors {{ background:#fef2f2; border:1px solid #fecaca; color:#991b1b; border-radius:12px; padding:10px 14px; margin-bottom:12px; }}
    .events-errors ul {{ margin:8px 0 0; padding-left:18px; }}
    table.events-edit {{
      table-layout:fixed; width:100%; min-width:1480px; border-collapse:collapse;
    }}
    .events-table-scroll {{ margin:0; }}
    .events-table-scroll-top {{
      overflow-x:auto; overflow-y:hidden; height:14px; margin-bottom:6px;
      -webkit-overflow-scrolling:touch;
    }}
    .events-table-scroll-top-inner {{ height:1px; }}
    .events-table-wrap {{ overflow-x:auto; -webkit-overflow-scrolling:touch; }}
    table.events-edit th, table.events-edit td {{
      padding:10px 8px; vertical-align:top; text-align:left;
    }}
    table.events-edit th {{
      white-space:nowrap; vertical-align:bottom; padding-bottom:8px;
    }}
    table.events-edit input {{
      display:block; width:100%; box-sizing:border-box;
      padding:8px 10px; font-size:13px; margin:0;
    }}
    table.events-edit .events-id {{ width:58px; }}
    table.events-edit .events-col-date {{ width:148px; }}
    table.events-edit .events-col-time {{ width:168px; }}
    table.events-edit .events-col-loc {{ width:152px; }}
    table.events-edit .events-col-addr {{ width:190px; }}
    table.events-edit .events-col-seats {{ width:72px; }}
    table.events-edit .events-col-price {{ width:78px; }}
    table.events-edit .events-col-host {{ width:150px; }}
    table.events-edit .events-col-desc {{ width:140px; }}
    table.events-edit .events-col-url {{ width:150px; }}
    table.events-edit .events-del {{ width:100px; }}
    table.events-edit th.events-col-addr,
    table.events-edit th.events-col-price,
    table.events-edit th.events-col-url,
    table.events-edit th.events-col-host,
    table.events-edit th.events-col-desc {{
      padding-left:18px;
    }}
    table.events-edit input[type="date"] {{
      width:132px; max-width:100%;
    }}
    table.events-edit input[type="time"] {{
      width:118px; max-width:100%;
    }}
    table.events-edit input[name="e_location"] {{
      width:136px; max-width:100%;
    }}
    table.events-edit input[name="e_price"],
    table.events-edit input[name="e_seats"] {{
      width:64px; max-width:100%;
    }}
    table.events-edit input.events-grow {{
      width:120px; max-width:100%; transition: width .12s ease;
      text-overflow:ellipsis; overflow:hidden; white-space:nowrap;
    }}
    table.events-edit input.events-grow:focus,
    table.events-edit input.events-grow.events-grow-open {{
      text-overflow:clip; overflow:visible; position:relative; z-index:3;
      max-width:none; background:#fff;
      box-shadow:0 4px 16px rgba(15,23,42,.12);
    }}
    .events-notify-box {{
      margin-top:16px; padding:14px 16px; border:1px solid #fecaca; border-radius:12px;
      background:#fff7f7; display:flex; flex-direction:column; gap:8px;
    }}
    .events-notify-box textarea,
    .events-extra-note {{
      width:100%; max-width:720px; font:inherit; padding:10px 12px; border-radius:10px;
      border:1px solid #cbd5e1; resize:vertical; min-height:72px;
    }}
    .events-notify-audience {{ display:flex; flex-wrap:wrap; gap:12px 18px; font-size:13px; }}
    .events-note-label {{ display:block; margin:10px 0 6px; font-weight:600; }}
    .events-impact-host {{ margin-top:14px; }}
    .events-impact-box {{
      padding:14px 16px; border:1px solid #f59e0b; border-radius:12px;
      background:#fffbeb; color:#78350f;
    }}
    .events-impact-box.events-impact-empty {{ border-color:#cbd5e1; background:#f8fafc; color:#334155; }}
    .events-impact-summary {{ margin:8px 0 12px; padding-left:18px; }}
    .events-impact-summary li {{ margin:4px 0; }}
    .events-impact-table {{ font-size:13px; background:#fff; }}
    .events-impact-table th {{ white-space:nowrap; }}
    .events-impact-msg {{ color:#b45309; font-weight:700; }}
    .audit-log {{ margin-bottom:16px; }}
    .audit-log table {{ font-size:13px; }}
    .audit-log td {{ vertical-align:top; }}
    .events-time-cell, .events-loc-cell {{ vertical-align:top; }}
    .events-tpls {{
      display:flex; flex-wrap:nowrap; gap:4px; margin-top:6px;
      align-items:center; justify-content:flex-start;
      overflow-x:auto; max-width:100%;
    }}
    .events-tpl {{
      border:1px solid #cbd5e1; background:#f8fafc; color:#334155;
      border-radius:999px; padding:2px 8px; font-size:11px; cursor:pointer;
      white-space:nowrap; flex:0 0 auto;
    }}
    .events-tpl:hover {{ background:#e2e8f0; }}
    table.events-edit tr.events-row-new td {{ background:#fffbeb; }}
    table.events-edit tr.events-row-new input {{
      background:#fffef5; border-color:#f6e05e;
    }}
    .events-purge {{ color:#b91c1c; }}
    .events-update-btn {{
      background:#1d4ed8; color:#fff; border:1px solid #1d4ed8;
      border-radius:10px; padding:10px 16px; font:inherit; cursor:pointer;
    }}
    .events-update-btn:hover {{ background:#1e40af; border-color:#1e40af; }}
    .events-raffle-badge {{
      display:inline-block; margin-left:6px; padding:2px 7px; border-radius:999px;
      background:#fef3c7; color:#92400e; font-size:11px; font-weight:600;
    }}
    .events-afisha-help {{ margin:0 0 12px; }}
    .events-afisha-help > summary {{ display:none; }}
    .events-afisha-help .events-afisha-help-body p {{ margin:0 0 8px; }}
    .events-scroll-fab {{ display:none; }}
    @media (max-width: 820px) {{
      body.tab-events main {{ padding-bottom:88px; }}
      body.tab-events .analytics-section > .muted {{ font-size:13px; }}
      .events-table-scroll-top {{ display:none; }}
      .events-table-wrap {{ overflow:visible; }}
      table.events-edit {{
        display:block; min-width:0; width:100%; table-layout:auto;
      }}
      table.events-edit thead {{ display:none; }}
      table.events-edit tbody {{ display:block; }}
      table.events-edit tr {{
        display:block; background:#fff; border:1px solid var(--line);
        border-radius:14px; padding:12px 12px 8px; margin:0 0 12px;
        box-shadow:0 4px 14px rgba(15,23,42,.04);
      }}
      table.events-edit tr.events-row-new {{
        border-color:#f6e05e; background:#fffbeb;
      }}
      table.events-edit td {{
        display:grid; grid-template-columns:84px minmax(0,1fr); gap:8px 10px;
        align-items:start; width:auto !important; max-width:none;
        padding:8px 0; border:0; border-bottom:1px solid #f1f5f9;
      }}
      table.events-edit td:last-child {{ border-bottom:0; }}
      table.events-edit td::before {{
        content:attr(data-label); font-size:12px; font-weight:700;
        color:var(--muted); line-height:1.3; padding-top:10px;
      }}
      table.events-edit td.events-id {{
        grid-template-columns:84px auto 1fr; align-items:center;
      }}
      table.events-edit .events-weekday {{ margin:0; }}
      table.events-edit input,
      table.events-edit input[type="date"],
      table.events-edit input[type="time"],
      table.events-edit input[name="e_location"],
      table.events-edit input[name="e_seats"] {{
        width:100% !important; max-width:none !important;
        min-height:40px; font-size:16px; padding:8px 10px;
        position:static; box-shadow:none;
      }}
      table.events-edit input.events-grow {{
        width:100% !important; max-width:none !important;
        min-height:36px; height:36px; font-size:16px; padding:6px 10px;
        white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
        position:static; box-shadow:none; resize:none;
      }}
      table.events-edit input.events-grow:focus,
      table.events-edit input.events-grow.events-grow-open {{
        width:100% !important; max-width:none !important;
        height:auto; min-height:88px; white-space:pre-wrap; overflow:auto;
        text-overflow:clip; word-break:break-word; overflow-wrap:anywhere;
        box-shadow:0 0 0 2px rgba(37,99,235,.25); z-index:2;
      }}
      table.events-edit input.events-grow[name="e_price"]:focus,
      table.events-edit input.events-grow[name="e_price"].events-grow-open {{
        min-height:40px; height:40px; white-space:nowrap;
      }}
      .events-afisha-help {{
        margin:0 0 12px; border:1px solid var(--line); border-radius:12px;
        background:#f8fafc; padding:0;
      }}
      .events-afisha-help > summary {{
        display:block; list-style:none; cursor:pointer; padding:10px 12px; font-size:13px; font-weight:700;
      }}
      .events-afisha-help > summary::-webkit-details-marker {{ display:none; }}
      .events-afisha-help > summary::after {{ content:" ▾"; color:var(--muted); font-weight:600; }}
      .events-afisha-help[open] > summary::after {{ content:" ▴"; }}
      .events-afisha-help .events-afisha-help-body {{ padding:0 12px 12px; }}
      .events-afisha-help .events-afisha-help-body p {{ margin:0; font-size:12px; line-height:1.4; }}
      body.tab-events .events-scroll-fab {{
        position:fixed; right:12px; bottom:calc(72px + env(safe-area-inset-bottom, 0px));
        z-index:60; display:flex; flex-direction:column; gap:8px;
      }}
      body.tab-events .events-scroll-fab button {{
        width:44px; height:44px; border-radius:999px; border:1px solid var(--line);
        background:#111827; color:#fff; font-size:18px; line-height:1; padding:0;
        box-shadow:0 6px 16px rgba(15,23,42,.18); cursor:pointer;
      }}
      body.tab-events .events-scroll-fab button.events-scroll-up {{ background:#fff; color:#111827; }}
      table.events-edit td.events-time-cell,
      table.events-edit td.events-loc-cell {{
        grid-template-columns:72px minmax(0,1fr);
        grid-template-areas:"label field" "tpls tpls";
      }}
      table.events-edit td.events-time-cell::before,
      table.events-edit td.events-loc-cell::before {{
        grid-area:label; padding-top:8px;
      }}
      table.events-edit td.events-time-cell > input,
      table.events-edit td.events-loc-cell > input {{
        grid-area:field; min-height:40px; font-size:16px; padding:8px 10px;
      }}
      table.events-edit td.events-time-cell > .events-tpls,
      table.events-edit td.events-loc-cell > .events-tpls {{
        grid-area:tpls; display:flex; flex-direction:row; flex-wrap:nowrap;
        gap:6px; margin-top:6px; overflow-x:auto; -webkit-overflow-scrolling:touch;
        padding-bottom:2px;
      }}
      .events-tpl {{
        flex:0 0 auto; padding:6px 10px; font-size:12px; white-space:nowrap;
      }}
      table.events-edit td {{
        grid-template-columns:72px minmax(0,1fr); padding:6px 0; font-size:13px;
      }}
      table.events-edit td::before {{ font-size:11px; padding-top:8px; }}
      table.events-edit tr {{ padding:10px 10px 6px; margin:0 0 10px; border-radius:12px; }}
      table.events-edit td.events-del {{
        display:flex; flex-wrap:wrap; gap:8px 12px; align-items:center;
        padding-top:10px; margin-top:2px; border-top:1px dashed #e2e8f0;
        border-bottom:0;
      }}
      table.events-edit td.events-del::before {{
        width:100%; padding-top:0; flex:0 0 100%;
      }}
      table.events-edit .events-del label {{
        display:inline-flex; align-items:center; gap:8px;
        min-height:40px; font-size:13px; padding:0 4px;
      }}
      table.events-edit .events-del input[type="checkbox"] {{
        width:18px; height:18px; min-height:0; padding:0;
      }}
      .events-tickets-link {{ min-height:36px; display:inline-flex; align-items:center; font-size:12px; }}
      .events-toolbar:not(.events-toolbar-bottom) {{ display:none; }}
      .events-toolbar-bottom {{
        position:fixed; left:0; right:0; bottom:0; z-index:40;
        margin:0; padding:10px 14px calc(10px + env(safe-area-inset-bottom, 0px));
        background:rgba(255,255,255,.96); border-top:1px solid var(--line);
        box-shadow:0 -8px 24px rgba(15,23,42,.08);
        display:flex; gap:8px; align-items:center; flex-wrap:nowrap;
      }}
      .events-toolbar-bottom .events-update-btn {{
        flex:1 1 auto; min-height:44px; font-size:15px; font-weight:700;
      }}
      .events-toolbar-bottom .pill {{
        min-height:44px; display:inline-flex; align-items:center; padding:10px 12px;
      }}
      .events-toolbar-bottom .muted {{ font-size:11px; white-space:nowrap; }}
      .events-notify-audience {{ flex-direction:column; gap:10px; }}
      .events-notify-audience label {{
        display:flex; align-items:center; gap:10px; min-height:40px; font-size:14px;
      }}
      .events-notify-box textarea {{ max-width:none; font-size:16px; }}
    }}
    .metric.tone-bot-start {{
      background:#eef2ff; border-color:#c7d2fe; color:#312e81;
      box-shadow:none;
    }}
    .metric.tone-bot-start span, .metric.tone-bot-start small {{ color:#6366f1; }}
    .metric.tone-bot-start b {{ color:#312e81; font-size:34px; }}
    .analytics-section-title {{
      margin:18px 0 10px; font-size:18px; font-weight:700; color:#0f172a;
    }}
    .bar-funnel {{ display:flex; flex-direction:column; gap:8px; margin-top:10px; }}
    .bar-funnel-row {{
      display:grid; grid-template-columns: minmax(180px, 1.2fr) 96px minmax(120px, 1.4fr) minmax(150px, 0.95fr);
      gap:10px; align-items:center; padding:8px 10px; border-radius:12px; background:#f8fafc;
      border:1px solid #e2e8f0;
    }}
    .bar-funnel-row.drop {{ background:#fff1f2; border-color:#fecdd3; }}
    .bar-funnel-label {{ font-size:13px; color:#334155; font-weight:600; line-height:1.25; }}
    .bar-funnel-nums {{
      display:grid; grid-template-columns: 36px 1fr; gap:4px; align-items:baseline;
      font-variant-numeric: tabular-nums;
    }}
    .bar-funnel-nums b {{ font-size:18px; text-align:right; line-height:1.1; }}
    .bar-funnel-nums span {{ color:#64748b; font-size:12px; white-space:nowrap; }}
    .bar-funnel-track {{ height:10px; background:#e2e8f0; border-radius:999px; overflow:hidden; }}
    .bar-funnel-row.drop .bar-funnel-track {{ background:#fecdd3; }}
    .bar-funnel-fill {{ height:100%; background:linear-gradient(90deg, #60a5fa, #2563eb); border-radius:999px; }}
    .bar-funnel-row.drop .bar-funnel-fill {{ background:linear-gradient(90deg, #fb7185, #e11d48); }}
    .bar-funnel-pct {{ font-size:12px; color:#64748b; text-align:right; line-height:1.3; }}
    @media (max-width: 900px) {{
      .bar-funnel-row {{ grid-template-columns: 1fr 96px; }}
      .bar-funnel-track, .bar-funnel-pct {{ grid-column: 1 / -1; }}
      .bar-funnel-pct {{ text-align:left; }}
    }}
    .events-weekday {{ font-size:11px; margin-top:4px; }}
    .events-del {{
      white-space:nowrap; font-size:12px;
      display:flex; flex-direction:column; align-items:flex-start; gap:4px;
      vertical-align:top;
    }}
    .events-del label {{
      display:grid; grid-template-columns:16px auto; align-items:center; gap:6px;
      margin:0; line-height:1.2; cursor:pointer;
    }}
    table.events-edit .events-del input[type="checkbox"] {{
      display:inline-block; width:14px; height:14px; min-width:14px; max-width:14px;
      margin:0; padding:0; flex:none; appearance:auto;
    }}
    .events-past {{ margin-top:16px; }}
    .events-tickets-link {{
      display:inline-block; margin:1px 0 0; padding:2px 8px; font-size:11px; line-height:1.2;
    }}
    .events-tickets-panel {{ margin-bottom:16px; border:1px solid #c4b5fd; }}
    .inline-form {{ display:inline; }}
    .ticket-resend-form {{ margin-top:10px; }}
    .ticket-resend-form button {{ font-size:12px; padding:8px 10px; }}
    .user-anonymize-box {{
      margin:14px 0; padding:12px 14px; border-radius:12px;
      border:1px solid #fecaca; background:#fef2f2;
    }}
    .user-anonymize-box button {{
      background:#b91c1c; color:#fff; border:0; border-radius:10px;
      padding:10px 14px; font:inherit; cursor:pointer;
    }}
    .user-anonymize-box button:hover {{ background:#991b1b; }}
    .user-consent-box {{
      margin:10px 0 14px; padding:10px 14px; border-radius:12px; font-size:14px;
    }}
    .user-consent-yes {{
      border:1px solid #bbf7d0; background:#f0fdf4; color:#166534;
    }}
    .user-consent-no {{
      border:1px solid #e2e8f0; background:#f8fafc; color:#475569;
    }}
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
    .user-extra-stack {{ display:grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap:10px; margin:12px 0 18px; align-items:start; }}
    .user-extra-details {{ background:#f8fafc; border:1px solid var(--line); border-radius:14px; overflow:hidden; }}
    .user-extra-activity {{ background:#eff6ff; border-color:#93c5fd; }}
    .user-extra-activity .user-extra-summary strong {{ color:#1d4ed8; }}
    .user-extra-raffle {{ background:#fff7ed; border-color:#fdba74; }}
    .user-extra-raffle .user-extra-summary strong {{ color:#c2410c; }}
    .user-extra-tickets {{ background:#f5f3ff; border-color:#c4b5fd; }}
    .user-extra-tickets .user-extra-summary strong {{ color:#6d28d9; }}
    .user-extra-reminders {{ background:#f0fdf4; border-color:#86efac; }}
    .user-extra-reminders .user-extra-summary strong {{ color:#15803d; }}
    .user-extra-summary {{ display:flex; justify-content:space-between; align-items:center; gap:10px; padding:14px 16px; cursor:pointer; list-style:none; }}
    .user-extra-summary::-webkit-details-marker {{ display:none; }}
    .user-extra-summary strong {{ font-size:15px; }}
    .user-extra-body {{ padding:0 16px 16px; border-top:1px solid rgba(15,23,42,.08); background:rgba(255,255,255,.55); }}
    .user-extra-details[open] {{ grid-column: 1 / -1; }}
    .user-stage {{ margin:10px 0 4px; padding:10px 12px; border-radius:12px; background:#f8fafc; border:1px solid var(--line); }}
    .user-block-item {{ margin-top:10px; padding-top:10px; border-top:1px solid var(--line); }}
    .user-block-item:first-of-type {{ margin-top:8px; padding-top:0; border-top:none; }}
    .user-block-item ul {{ margin:8px 0 0; padding-left:18px; }}
    .user-block-item li {{ margin:4px 0; }}
    table.user-extra {{ table-layout:fixed; min-width:420px; }}
    table.user-activity-summary th:nth-child(2), table.user-activity-summary td:nth-child(2) {{ width:34%; text-align:right; }}
    .screen-carousel {{
      display:flex; gap:10px; overflow-x:auto; scroll-snap-type:x mandatory;
      -webkit-overflow-scrolling:touch; padding:4px 2px 8px; margin-top:6px;
    }}
    .screen-card {{
      flex:0 0 min(260px, 82%); scroll-snap-align:start;
      background:white; border:1px solid var(--line); border-left-width:4px;
      border-radius:12px; padding:10px 12px;
    }}
    .screen-card--approved {{ border-left-color:#22c55e; }}
    .screen-card--rejected {{ border-left-color:#ef4444; }}
    .screen-card--pending {{ border-left-color:#f59e0b; }}
    .screen-card--other {{ border-left-color:#94a3b8; }}
    .screen-card-top {{ display:flex; justify-content:space-between; gap:8px; align-items:center; }}
    .screen-card-title {{ font-weight:700; font-size:13px; }}
    .screen-status--approved {{ background:#22c55e; }}
    .screen-status--rejected {{ background:#ef4444; }}
    .screen-status--pending {{ background:#f59e0b; }}
    .screen-status--other {{ background:#64748b; }}
    .screen-card-facts {{ margin:8px 0 0; display:grid; gap:3px; font-size:12px; }}
    .screen-card-facts div {{ display:grid; grid-template-columns:64px 1fr; gap:6px; }}
    .screen-card-facts dt {{ color:var(--muted); }}
    .screen-card-facts dd {{ margin:0; }}
    .screen-card-reject {{
      margin:8px 0 0; padding:8px 10px; border-radius:10px;
      background:#fef2f2; color:#b91c1c; font-size:12px; line-height:1.4;
    }}
    .screen-preview {{ margin-top:8px; display:grid; gap:6px; }}
    .screen-preview img {{
      max-width:100%; max-height:220px; width:auto; height:auto;
      border-radius:8px; border:1px solid var(--line); object-fit:contain;
      background:#0f172a08;
    }}
    .screen-preview-note {{ margin:8px 0 0; font-size:12px; }}
    .screen-file-id {{ margin-top:6px; font-size:11px; }}
    .screen-file-id summary {{ cursor:pointer; color:var(--muted); list-style:none; }}
    .screen-file-id summary::-webkit-details-marker {{ display:none; }}
    .screen-file-id summary::before {{ content:"▸ "; }}
    .screen-file-id[open] summary::before {{ content:"▾ "; }}
    .screen-file-id code {{
      display:block; margin-top:4px; padding:6px 8px; background:#f8fafc;
      border:1px solid var(--line); border-radius:8px; word-break:break-all;
      white-space:pre-wrap; font-size:10px; line-height:1.35; color:#334155;
    }}
    .screen-card-nav {{ margin-top:8px; font-size:11px; }}
    .screen-carousel-hint {{ margin:0; font-size:12px; }}
    .audit-feed h2 {{ margin:0 0 6px; }}
    .audit-list {{ display:grid; gap:10px; margin-top:14px; }}
    .audit-item {{
      background:#fff; border:1px solid var(--line); border-left-width:4px;
      border-radius:14px; padding:12px 14px;
    }}
    .audit-item--save {{ border-left-color:#2563eb; }}
    .audit-item--restore {{ border-left-color:#0d9488; }}
    .audit-item--notify {{ border-left-color:#d97706; }}
    .audit-item--cancel {{ border-left-color:#ef4444; }}
    .audit-item--ticket {{ border-left-color:#7c3aed; }}
    .audit-item--login {{ border-left-color:#94a3b8; }}
    .audit-item--other {{ border-left-color:#64748b; }}
    .audit-item-top {{ display:flex; justify-content:space-between; gap:10px; align-items:center; }}
    .audit-role {{
      display:inline-block; font-size:11px; font-weight:700; letter-spacing:.02em;
      padding:3px 8px; border-radius:999px; background:#f1f5f9; color:#334155;
    }}
    .audit-role--owner {{ background:#111827; color:#fff; }}
    .audit-role--client {{ background:#dbeafe; color:#1d4ed8; }}
    .audit-role--manager {{ background:#ffedd5; color:#c2410c; }}
    .audit-when {{ font-size:12px; white-space:nowrap; }}
    .audit-item-title {{ margin:8px 0 0; font-size:15px; font-weight:700; line-height:1.35; }}
    .audit-item-details {{ margin:6px 0 0; font-size:13px; color:var(--muted); line-height:1.4; }}
    .audit-item-list {{ margin:6px 0 0; padding:0 0 0 1.1em; font-size:13px; color:var(--muted); line-height:1.45; }}
    .audit-item-list li {{ margin:2px 0; }}
    .audit-item-list .audit-item-group {{ margin-top:6px; list-style:none; margin-left:-1.1em; font-weight:600; color:var(--text); }}
    .audit-cut {{ margin:8px 0 0; }}
    .audit-cut > summary {{
      cursor:pointer; font-size:13px; color:#1d4ed8; list-style:none;
      user-select:none; line-height:1.4;
    }}
    .audit-cut > summary::-webkit-details-marker {{ display:none; }}
    .audit-cut > summary::before {{ content:"▸ "; color:#64748b; }}
    .audit-cut[open] > summary::before {{ content:"▾ "; }}
    .audit-cut .audit-item-list {{ margin-top:8px; }}
    .audit-empty {{ margin:8px 0 0; }}
    .ticket-card .ticket-loc {{ white-space:normal; word-break:break-word; }}
    .ticket-card-note {{
      margin:8px 0 0; padding:8px 10px; border-radius:10px; font-size:12px; line-height:1.4;
    }}
    .ticket-card-note--cancel {{ background:#fef2f2; color:#b91c1c; }}
    .ticket-card-note--annul {{ background:#f1f5f9; color:#475569; }}
    .ticket-card--confirmed {{ border-left-color:#22c55e; }}
    .ticket-card--cancelled {{ border-left-color:#ef4444; }}
    .ticket-card--annulled {{ border-left-color:#64748b; }}
    .screen-status--annulled {{ background:#64748b; }}
    .screen-status--cancelled {{ background:#ef4444; }}
    .screen-status--confirmed {{ background:#22c55e; }}
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
    .format--proverka {{ background:#f0fdf4; color:#15803d; }}
    .format--rozygrysh {{ background:#fff7ed; color:#c2410c; }}
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
      .funnel-row, .analytics-show-pair, .show-format-grid, .user-extra-stack, .command-grid {{ grid-template-columns:1fr; }}
      .user-extra-details[open] {{ grid-column:auto; }}
      .funnel-step.funnel-spacer {{ display:none; }}
      .branch-metrics {{ grid-template-columns: repeat(2, minmax(0,1fr)); }}
    }}
    @media (max-width: 1100px) and (min-width: 901px) {{
      .user-extra-stack, .command-grid {{ grid-template-columns: repeat(2, minmax(0,1fr)); }}
      .branch-metrics {{ grid-template-columns: repeat(6, minmax(0,1fr)); }}
      .branch-metric {{ padding:10px; }}
      .branch-metric b {{ font-size:18px; }}
    }}
    @media (max-width: 780px) {{
      body {{ font-size:14px; }}
      header {{ padding:14px 14px 12px; }}
      header h1 {{ font-size:20px; margin:0 0 4px; }}
      header p {{ font-size:12px; line-height:1.35; }}
      main {{ padding:12px; }}
      .tabs {{ gap:6px; margin:0 0 12px; padding-left:0; }}
      .tab, .pill {{ padding:7px 11px; font-size:13px; }}
      .booking-summary {{
        grid-template-columns:repeat(2, minmax(0,1fr)); gap:8px; margin-bottom:12px;
      }}
      .booking-summary .metric {{ padding:10px 12px; border-radius:12px; }}
      .booking-summary .metric span {{ font-size:11px; }}
      .booking-summary .metric b {{ margin-top:4px; font-size:18px; }}
      .analytics-summary, .analytics-audience {{ grid-template-columns:repeat(2, minmax(0,1fr)); gap:8px; }}
      .metric {{ padding:12px; border-radius:12px; }}
      .metric span {{ font-size:12px; }}
      .metric b {{ margin-top:4px; font-size:20px; }}
      .booking-filters {{
        flex-direction:column; align-items:stretch; gap:10px; padding:12px;
        margin-bottom:12px; border-radius:14px;
      }}
      .filter-status-pills {{
        display:flex; gap:6px; flex-wrap:nowrap; overflow-x:auto;
        -webkit-overflow-scrolling:touch; padding-bottom:2px; width:100%;
      }}
      .filter-status-pills .pill {{
        flex:0 0 auto; white-space:nowrap; font-size:12px; padding:6px 10px;
      }}
      .booking-filters-form {{
        display:grid; grid-template-columns:1fr 1fr; gap:8px; width:100%;
        align-items:stretch;
      }}
      .booking-filters-form input[type="date"],
      .booking-filters-form select {{
        grid-column:1 / -1; width:100%; min-width:0; font-size:16px; padding:8px 10px;
      }}
      .booking-filters-form button,
      .booking-filters-form > .pill {{
        width:100%; text-align:center; justify-content:center;
        display:inline-flex; align-items:center; min-height:40px; font-size:14px;
      }}
      .card {{ padding:14px; margin-bottom:12px; border-radius:14px; }}
      .event-head {{ display:block; }}
      .event-head h2 {{
        font-size:15px; line-height:1.35; white-space:normal; word-break:break-word;
      }}
      .event-head p {{ font-size:12px; margin-top:4px; white-space:normal; }}
      .format {{ font-size:11px; padding:4px 8px; display:inline-block; margin-top:6px; }}
      .counters, .mini-metrics {{ gap:6px; margin:10px 0; }}
      .counter, .mini-metrics span {{
        font-size:11px; padding:5px 8px; white-space:nowrap;
      }}
      .capacity-line {{ font-size:13px; margin-top:12px; }}
      .empty-state {{ padding:22px 14px; }}
      .empty-state h2 {{ font-size:16px; }}
      .empty-state p {{ font-size:13px; }}
      .details-summary {{ display:block; }}
      .details-action {{ display:inline-block; margin-top:10px; }}
      h2 {{ font-size:16px; }}
    }}
  </style>
</head>
<body class="tab-{_h(tab)} {_h(role_class)}">
  <header>
    <h1>Стендап бронирование</h1>
    <p>{_h(header_note)} · источник данных: {_h(source_label)} · <a href="/admin/logout">выйти</a></p>
  </header>
  <main>
    <nav class="tabs">{_tabs(filters, can_view_ops=can_view_ops, can_view_db=can_view_db, can_send_mailing=can_resend_tickets)}</nav>
    {summary_html}
    {_content(dashboard, filters, db_data, analytics, user_extras, events_bundle, events_flash, events_errors, ticket_holders, can_resend_tickets=can_resend_tickets, can_anonymize_user=can_anonymize_user, user_stage_by_user=user_stage_by_user, mailing_data=mailing_data)}
  </main>
  <script>
    (function () {{
      var STORAGE_KEY = "admin-details-open";
      function load() {{
        try {{ return JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "{{}}"); }}
        catch (e) {{ return {{}}; }}
      }}
      function save(state) {{
        try {{ sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state)); }}
        catch (e) {{}}
      }}
      function asOpen(entry) {{
        if (entry && typeof entry === "object" && Object.prototype.hasOwnProperty.call(entry, "open")) {{
          return !!entry.open;
        }}
        return !!entry;
      }}
      // Meta-refresh / F5 = reload → keep user sections. Tab clicks = navigate → collapse.
      var nav = performance.getEntriesByType && performance.getEntriesByType("navigation")[0];
      var isReload = !!(nav && nav.type === "reload");
      var state = load();
      var dirty = false;
      document.querySelectorAll("details[data-persist-key]").forEach(function (el) {{
        var key = el.getAttribute("data-persist-key");
        var isUser = key.indexOf("user:") === 0;
        if (isUser) {{
          if (isReload && Object.prototype.hasOwnProperty.call(state, key)) {{
            el.open = asOpen(state[key]);
          }} else {{
            el.open = false;
            if (Object.prototype.hasOwnProperty.call(state, key)) {{
              delete state[key];
              dirty = true;
            }}
          }}
        }} else if (Object.prototype.hasOwnProperty.call(state, key)) {{
          el.open = asOpen(state[key]);
        }}
        el.addEventListener("toggle", function () {{
          var next = load();
          if (el.open) {{
            next[key] = {{ open: true }};
          }} else {{
            delete next[key];
          }}
          save(next);
        }});
      }});
      if (dirty) save(state);
    }})();
  </script>
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
    # Analytics default: whole period (not today), unless dates are set explicitly.
    if tab == "analytics" and not all_period and not date_from and not date_to:
        all_period = True
    if all_period:
        date_from = ""
        date_to = ""
    ef = request.query.get("ef", "").strip()
    if ef not in ("best", "proverka", "hitloto"):
        ef = "best" if tab == "events" else ""
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
        "ef": ef,
        "tickets": request.query.get("tickets", "").strip(),
        "q": request.query.get("q", "").strip(),
    }


def _request_token(request: web.Request) -> str:
    return (
        request.query.get("token")
        or request.headers.get("X-Admin-Token")
        or request.cookies.get(ADMIN_COOKIE_NAME)
        or ""
    )


def _tokens_configured(config: AdminConfig) -> bool:
    return bool(config.admin_token or config.client_token or config.owner_token)


def _token_eq(candidate: str, expected: str) -> bool:
    """Constant-time compare; encode to bytes so non-ASCII env values cannot crash auth."""
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def _is_manager_token(candidate: str, config: AdminConfig) -> bool:
    return _token_eq(candidate, config.admin_token)


def _is_client_token(candidate: str, config: AdminConfig) -> bool:
    return _token_eq(candidate, config.client_token)


def _is_owner_token(candidate: str, config: AdminConfig) -> bool:
    return _token_eq(candidate, config.owner_token)


def _token_matches(candidate: str, config: AdminConfig) -> bool:
    """Any valid login token: manager, client, or owner."""
    if not candidate:
        return False
    if not _tokens_configured(config):
        return False
    return (
        _is_manager_token(candidate, config)
        or _is_client_token(candidate, config)
        or _is_owner_token(candidate, config)
    )


def _admin_role(request: web.Request, config: AdminConfig) -> str:
    """owner | client | manager. Open local (no tokens) → owner."""
    if not _tokens_configured(config):
        return "owner"
    token = _request_token(request)
    if _is_owner_token(token, config):
        return "owner"
    if _is_client_token(token, config):
        return "client"
    return "manager"


def _can_view_db(request: web.Request, config: AdminConfig) -> bool:
    return _admin_role(request, config) == "owner"


def _can_view_ops(request: web.Request, config: AdminConfig) -> bool:
    """Full ops: users / bookings / analytics / journal (not manager)."""
    return _admin_role(request, config) in {"owner", "client"}


def _can_resend_tickets(request: web.Request, config: AdminConfig) -> bool:
    return _admin_role(request, config) == "owner"


def _check_auth(request: web.Request, config: AdminConfig) -> bool:
    # Open only if no tokens configured at all (local/dev)
    if not _tokens_configured(config):
        return True
    return _token_matches(_request_token(request), config)


def _is_https_request(request: web.Request | None) -> bool:
    if request is None:
        return False
    forwarded = (request.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip().lower()
    if forwarded == "https" or request.scheme == "https":
        return True
    host = (request.headers.get("Host") or request.host or "").split(":", 1)[0].lower()
    # Боевые домены всегда за TLS (nginx) — даже если upstream видит http://127.0.0.1.
    return host.endswith("moscowstandupshow.ru") or host.endswith("duckdns.org")


def _set_auth_cookie(
    response: web.Response,
    token: str,
    *,
    request: web.Request | None = None,
) -> None:
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        token,
        max_age=ADMIN_COOKIE_MAX_AGE,
        httponly=True,
        samesite="Lax",
        secure=_is_https_request(request),
        path="/",
    )


def _login_success_response(request: web.Request, token: str, dest: str) -> web.Response:
    """После токена — 200 + cookie + redirect.

    302/303 за nginx auth_basic у части браузеров снова поднимает окно логина/пароля.
    """
    safe = dest if dest.startswith("/admin") else "/admin"
    href = _h(safe)
    html = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="0; url={href}">
  <title>Вход…</title>
</head>
<body>
  <p>Вход выполнен. Если страница не открылась — <a href="{href}">продолжить</a>.</p>
</body>
</html>"""
    response = web.Response(text=html, status=200, content_type="text/html")
    _set_auth_cookie(response, token, request=request)
    return response


def render_login_html(error: str = "") -> str:
    error_html = f'<p class="error">{_h(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Вход · Стендап бронирование</title>
  <style>
    * {{ box-sizing:border-box; }}
    body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:#f4f6fb; font-family:Arial,sans-serif; color:#111827; }}
    form {{ width:min(420px, calc(100vw - 32px)); background:white; padding:28px; border-radius:18px; box-shadow:0 16px 50px rgba(15,23,42,.12); }}
    h1 {{ margin:0 0 18px; font-size:26px; }}
    input, button {{
      display:block; width:100%; height:48px; margin:0; border:1px solid #e5e7eb;
      border-radius:10px; padding:0 14px; font:inherit; line-height:46px;
    }}
    button {{ margin-top:12px; background:#111827; color:white; border-color:#111827; cursor:pointer; }}
    .error {{ margin:0 0 12px; color:#b91c1c; background:#fee2e2; border-radius:10px; padding:10px 12px; }}
  </style>
</head>
<body>
  <form method="post" action="/admin/login">
    <h1>Стендап бронирование</h1>
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
        # 200, не 401: иначе за nginx auth_basic браузер снова просит логин/пароль.
        return web.Response(text=render_login_html(), status=200, content_type="text/html")
    query_token = (request.query.get("token") or "").strip()
    if query_token and _token_matches(query_token, config):
        return _login_success_response(request, query_token, _redirect_without_token(request))

    can_view_db = _can_view_db(request, config)
    can_view_ops = _can_view_ops(request, config)
    can_resend = _can_resend_tickets(request, config)
    filters = _filters_from_request(request)
    if filters.get("status") and filters["status"] not in STATUSES:
        filters["status"] = ""
    if filters.get("format") and filters["format"] not in FORMAT_OPTIONS:
        filters["format"] = ""
    if filters.get("tab") not in {
        "date",
        "bookings",
        "users",
        "analytics",
        "events",
        "audit",
        "db",
        "mailing",
    }:
        filters["tab"] = "date"
    # Manager: «Мероприятия» + «По дате». Client/owner: full ops; tickets only for owner.
    if not can_view_ops:
        if filters.get("tab") not in {"events", "date"}:
            raise web.HTTPFound("/admin?tab=events")
        filters["tickets"] = ""
    elif filters.get("tab") == "db" and not can_view_db:
        raise web.HTTPFound("/admin?tab=date")
    elif filters.get("tab") == "mailing" and not can_resend:
        raise web.HTTPFound("/admin?tab=date")
    if not can_resend:
        filters["tickets"] = ""

    loop = asyncio.get_running_loop()
    source_label = "PostgreSQL" if _use_postgres(config) else f"SQLite ({config.db_path})"
    db_data = None
    analytics = None
    events_bundle = None
    saved_flag = (request.query.get("saved") or "").strip()
    events_flash = ""
    events_errors: list[str] = []
    ticket_holders = None
    empty_dashboard = {
        "events": [],
        "bookings": [],
        "users": {},
        "totals": {"events": 0, "bookings": 0, "reserved_guests": 0, "confirmed_guests": 0},
    }
    if filters.get("tab") == "audit":
        from bot.db.admin_audit import fetch_admin_audit

        audit_rows = await loop.run_in_executor(None, fetch_admin_audit, 120)
        db_data = {"audit": audit_rows}
        dashboard = empty_dashboard
    elif filters.get("tab") == "db":
        from bot.db.admin_audit import fetch_admin_audit

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
        audit_rows = await loop.run_in_executor(None, fetch_admin_audit, 80)
        db_data = {"tables": tables, "browse": browse, "audit": audit_rows}
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
    elif filters.get("tab") == "events":
        from bot.db.events_admin import list_events_for_admin

        ef = filters.get("ef") or "best"
        events_bundle = await loop.run_in_executor(None, list_events_for_admin, ef)
        dashboard = empty_dashboard
        tickets_id = (filters.get("tickets") or "").strip()
        if can_resend and tickets_id.isdigit():
            from bot.admin.ticket_resend import list_event_ticket_holders

            ticket_holders = await loop.run_in_executor(
                None, list_event_ticket_holders, int(tickets_id)
            )
        if saved_flag == "1":
            events_flash = "Сохранено. Афиша в боте уже с новыми данными."
            n_ok = request.query.get("n_ok")
            n_fail = request.query.get("n_fail")
            if n_ok is not None or n_fail is not None:
                events_flash += (
                    f" Рассылка об отмене: успешно {n_ok or '0'}, ошибок {n_fail or '0'}."
                )
            c_ok = request.query.get("c_ok")
            c_fail = request.query.get("c_fail")
            if c_ok is not None or c_fail is not None:
                events_flash += f" Отменено броней/билетов: {c_ok or '0'}"
                if c_fail and c_fail != "0":
                    events_flash += f", ошибок {c_fail}"
                events_flash += "."
        elif saved_flag == "resend":
            ok = request.query.get("ok") or "0"
            fail = request.query.get("fail") or "0"
            err = (request.query.get("err") or "").strip()
            events_flash = f"Переотправка билетов: успешно {ok}, ошибок {fail}."
            if err:
                events_flash += f" ({err[:200]})"
        else:
            events_flash = ""
    elif filters.get("tab") == "users":
        def _load_users_directory():
            page_data = fetch_users_page(
                config,
                q=filters.get("q") or "",
                page=filters.get("page") or 1,
                status=filters.get("status") or "",
                channel=filters.get("channel") or "",
            )
            users_map = {u["key"]: u for u in page_data["users"]}
            list_keys = [u["key"] for u in page_data["users"]]
            selected_key = (filters.get("u") or "").strip()
            selected_uid = int(selected_key) if selected_key.isdigit() else None
            directory_user = None
            if selected_uid:
                directory_user = fetch_one_directory_user(config, selected_uid)
                if directory_user:
                    users_map[directory_user["key"]] = directory_user
            if selected_uid and selected_key in users_map:
                booking_rows = fetch_user_booking_rows(config, selected_uid)
                detail_dash = build_dashboard(booking_rows)
                detail_user = (detail_dash.get("users") or {}).get(selected_key)
                if detail_user:
                    # build_dashboard из броней не знает про согласие ПДн — переносим с directory.
                    base = directory_user or users_map.get(selected_key) or {}
                    detail_user["consent_accepted_at"] = base.get("consent_accepted_at")
                    detail_user["consent_version"] = base.get("consent_version") or ""
                    if not detail_user.get("telegram_id"):
                        detail_user["telegram_id"] = base.get("telegram_id")
                    if not detail_user.get("vk_id"):
                        detail_user["vk_id"] = base.get("vk_id")
                    users_map[selected_key] = detail_user
                elif selected_key in users_map:
                    # гость без броней — оставляем карточку из directory
                    pass
            return {
                "events": [],
                "bookings": [],
                "users": users_map,
                "totals": {
                    "events": 0,
                    "bookings": 0,
                    "reserved_guests": 0,
                    "confirmed_guests": 0,
                },
                "users_meta": {
                    "total": page_data["total"],
                    "page": page_data["page"],
                    "pages": page_data["pages"],
                    "q": page_data["q"],
                    "list_keys": list_keys,
                },
            }

        dashboard = await loop.run_in_executor(None, _load_users_directory)
        if saved_flag == "resend":
            ok = request.query.get("ok") or "0"
            fail = request.query.get("fail") or "0"
            err = (request.query.get("err") or "").strip()
            events_flash = (
                "Билет переотправлен."
                if ok == "1" and fail == "0"
                else f"Не удалось переотправить билет{': ' + err[:200] if err else '.'}"
            )
        elif saved_flag == "anonymize":
            ok = request.query.get("ok") or "0"
            already = request.query.get("already") or "0"
            err = (request.query.get("err") or "").strip()
            if ok == "1" and already == "1":
                events_flash = "Персональные данные этого гостя уже были обезличены."
            elif ok == "1":
                cancelled = request.query.get("cancelled") or "0"
                events_flash = (
                    "Персональные данные гостя удалены (обезличены). "
                    f"Активных броней отменено: {cancelled}."
                )
            else:
                events_flash = (
                    f"Не удалось обезличить гостя{': ' + err[:200] if err else '.'}"
                )
    else:
        # With a date selected, load all shows that day (even empty) so the show picker is complete.
        include_empty_events = bool(filters.get("date")) and filters.get("tab") in {"date", "bookings"}
        fetch_filters = dict(filters)
        rows = await loop.run_in_executor(
            None, fetch_admin_rows, config, fetch_filters, include_empty_events
        )
        dashboard = build_dashboard(rows)
    user_extras = None
    user_stage_by_user: dict = {}
    if filters.get("tab") == "users":
        meta = dashboard.get("users_meta") or {}
        user_ids = []
        for key in meta.get("list_keys") or []:
            u = (dashboard.get("users") or {}).get(key) or {}
            uid = u.get("user_id")
            if uid:
                try:
                    user_ids.append(int(uid))
                except (TypeError, ValueError):
                    pass
        selected_key = (filters.get("u") or "").strip()
        if selected_key and selected_key.isdigit():
            try:
                sid = int(selected_key)
            except (TypeError, ValueError):
                sid = None
            if sid is not None and sid not in user_ids:
                user_ids.append(sid)

        def _load_user_stages():
            from bot.db.analytics import fetch_users_last_events

            return fetch_users_last_events(user_ids)

        user_stage_by_user = await loop.run_in_executor(None, _load_user_stages)

        if selected_key:
            selected = (dashboard.get("users") or {}).get(selected_key)
            if selected:
                telegram_id = selected.get("telegram_id")
                vk_id = selected.get("vk_id")
                user_id = selected.get("user_id")

                def _load_user_extras():
                    from bot.db.analytics import fetch_user_activity, fetch_user_activity_counts
                    from bot.db.crud import (
                        get_raffle_submissions_for_telegram,
                        get_raffle_submissions_for_vk,
                        get_user_raffle_flags,
                    )

                    tid = int(telegram_id) if telegram_id else None
                    vid = int(vk_id) if vk_id else None
                    uid = int(user_id) if user_id else None
                    last_event = user_stage_by_user.get(uid) if uid is not None else None
                    submissions = []
                    if tid:
                        submissions.extend(get_raffle_submissions_for_telegram(tid))
                    if vid:
                        submissions.extend(get_raffle_submissions_for_vk(vid))
                    if tid and vid and submissions:
                        by_id = {}
                        for row in submissions:
                            sid = row.get("id")
                            if sid is not None:
                                by_id[sid] = row
                        submissions = sorted(
                            by_id.values(),
                            key=lambda r: int(r.get("id") or 0),
                            reverse=True,
                        )[:20]
                    return {
                        "activity_counts": fetch_user_activity_counts(
                            user_id=uid, telegram_id=tid, vk_id=vid
                        ),
                        "activity_recent": fetch_user_activity(
                            user_id=uid, telegram_id=tid, vk_id=vid, limit=20
                        ),
                        "submissions": submissions,
                        "flags": get_user_raffle_flags(
                            telegram_id=tid, user_id=uid, vk_id=vid
                        ),
                        "last_event": last_event,
                    }

                user_extras = await loop.run_in_executor(None, _load_user_extras)
    mailing_data = None
    if filters.get("tab") == "mailing" and can_resend:
        from bot.db.mailing import get_campaign, list_campaigns, list_recipients

        def _load_mailing():
            campaigns = list_campaigns(40)
            detail = None
            recipients = None
            cid_raw = (request.query.get("campaign") or "").strip()
            if cid_raw.isdigit():
                detail = get_campaign(int(cid_raw))
                if detail:
                    rstatus = (request.query.get("rstatus") or "").strip()
                    rpage = (request.query.get("rpage") or "1").strip()
                    recipients = list_recipients(
                        int(cid_raw),
                        status=rstatus,
                        page=int(rpage) if rpage.isdigit() else 1,
                    )
                    recipients["filter_status"] = rstatus
            return {
                "campaigns": campaigns,
                "detail": detail,
                "recipients": recipients,
                "flash": (request.query.get("m_flash") or "").strip(),
                "error": (request.query.get("m_err") or "").strip(),
            }

        mailing_data = await loop.run_in_executor(None, _load_mailing)
        if mailing_data.get("flash"):
            events_flash = mailing_data["flash"]
    return web.Response(
        text=render_admin_html(
            dashboard,
            filters,
            source_label,
            db_data,
            can_view_db,
            analytics,
            user_extras,
            events_bundle,
            events_flash,
            events_errors,
            ticket_holders,
            can_view_ops=can_view_ops,
            can_resend_tickets=can_resend,
            can_anonymize_user=can_view_db,
            user_stage_by_user=user_stage_by_user,
            mailing_data=mailing_data,
        ),
        content_type="text/html",
    )


async def users_anonymize_page(request: web.Request) -> web.Response:
    """Owner-only: scrub PII for a guest (right to erasure)."""
    config = request.app["config"]
    if not _check_auth(request, config):
        return web.Response(text=render_login_html(), status=200, content_type="text/html")
    if not _can_resend_tickets(request, config):
        raise web.HTTPFound("/admin?tab=users")

    from urllib.parse import urlencode

    from bot.db.admin_audit import log_admin_action
    from bot.db.crud import anonymize_user

    post = await request.post()
    user_id_raw = (post.get("user_id") or "").strip()
    user_key = (post.get("u") or user_id_raw).strip()
    actor = _admin_role(request, config)

    if not user_id_raw.isdigit():
        q = urlencode({"tab": "users", "saved": "anonymize", "ok": "0", "err": "no_user_id"})
        raise web.HTTPFound(f"/admin?{q}")

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, anonymize_user, int(user_id_raw))

    log_details = {
        "ok": bool(result.get("ok")),
        "already": bool(result.get("already")),
        "bookings_cancelled": int(result.get("bookings_cancelled") or 0),
    }
    if result.get("error"):
        log_details["error"] = (result.get("error") or "")[:80]
    log_admin_action(
        actor_role=actor,
        action="user_anonymize",
        entity_type="user",
        entity_id=user_id_raw,
        details=log_details,
    )

    q = {
        "tab": "users",
        "u": user_key or user_id_raw,
        "saved": "anonymize",
        "ok": "1" if result.get("ok") else "0",
        "already": "1" if result.get("already") else "0",
        "cancelled": str(int(result.get("bookings_cancelled") or 0)),
    }
    if not result.get("ok"):
        q["err"] = (result.get("error") or "failed")[:80]
    raise web.HTTPFound(f"/admin?{urlencode(q)}")


async def events_resend_ticket_page(request: web.Request) -> web.Response:
    config = request.app["config"]
    if not _check_auth(request, config):
        return web.Response(text=render_login_html(), status=200, content_type="text/html")
    if not _can_resend_tickets(request, config):
        raise web.HTTPFound("/admin?tab=events")
    from bot.admin.ticket_resend import resend_ticket_async, resend_tickets_for_event_async
    from urllib.parse import quote, urlencode

    post = await request.post()
    event_format = (post.get("ef") or "best").strip()
    if event_format not in {"best", "proverka", "hitloto"}:
        event_format = "best"
    updated = (post.get("updated") or "1").strip() != "0"
    extra_note = (post.get("extra_note") or "").strip()
    tickets = (post.get("tickets") or "").strip()
    back = (post.get("back") or "").strip()
    user_key = (post.get("u") or "").strip()
    actor = _admin_role(request, config)

    event_id_raw = (post.get("event_id") or "").strip()
    booking_id_raw = (post.get("booking_id") or "").strip()

    if event_id_raw.isdigit():
        result = await resend_tickets_for_event_async(
            int(event_id_raw), updated=updated, extra_note=extra_note
        )
        from bot.db.admin_audit import log_admin_action

        log_admin_action(
            actor_role=actor,
            action="resend_tickets_event",
            entity_type="event",
            entity_id=event_id_raw,
            details={
                "ok": result.get("ok"),
                "fail": result.get("fail"),
                "extra_note": bool(extra_note),
                "event": result.get("event") or {},
                "items": (result.get("items") or [])[:40],
            },
        )
        q = urlencode(
            {
                "tab": "events",
                "ef": event_format,
                "tickets": event_id_raw,
                "saved": "resend",
                "ok": str(result.get("ok") or 0),
                "fail": str(result.get("fail") or 0),
            }
        )
        raise web.HTTPFound(f"/admin?{q}")

    if booking_id_raw.isdigit():
        one = await resend_ticket_async(
            int(booking_id_raw), updated=updated, extra_note=extra_note
        )
        from bot.db.admin_audit import log_admin_action

        log_admin_action(
            actor_role=actor,
            action="resend_ticket",
            entity_type="booking",
            entity_id=booking_id_raw,
            details={
                "ok": bool(one.get("ok")),
                "extra_note": bool(extra_note),
                "booking_id": one.get("booking_id") or int(booking_id_raw),
                "name": one.get("name") or "",
                "date": one.get("date") or "",
                "time": one.get("time") or "",
                "location": one.get("location") or "",
                "event_id": one.get("event_id"),
                "error": (one.get("error") or "")[:200],
            },
        )
        err = (one.get("error") or "")[:200]
        if back == "user" and user_key:
            q = {
                "tab": "users",
                "u": user_key,
                "saved": "resend",
                "ok": "1" if one.get("ok") else "0",
                "fail": "0" if one.get("ok") else "1",
            }
            if err and not one.get("ok"):
                q["err"] = err
            raise web.HTTPFound(f"/admin?{urlencode(q)}")
        q = {
            "tab": "events",
            "ef": event_format,
            "saved": "resend",
            "ok": "1" if one.get("ok") else "0",
            "fail": "0" if one.get("ok") else "1",
        }
        if tickets:
            q["tickets"] = tickets
        if err and not one.get("ok"):
            q["err"] = err
        raise web.HTTPFound(f"/admin?{urlencode(q)}")

    raise web.HTTPFound(f"/admin?tab=events&ef={quote(event_format)}")


async def events_save_page(request: web.Request) -> web.Response:
    config = request.app["config"]
    if not _check_auth(request, config):
        return web.Response(text=render_login_html(), status=200, content_type="text/html")
    from bot.admin.events_tab import parse_events_form
    from bot.db.events_admin import list_events_for_admin, save_events_batch
    from urllib.parse import quote

    post = await request.post()
    event_format, rows = parse_events_form(post)
    # Guest cancel/notify mailing is owner-only (UI hidden for manager/client).
    if _can_resend_tickets(request, config):
        notify_message = (post.get("notify_message") or "").strip()
        notify_audience = (post.get("notify_audience") or "").strip()
    else:
        notify_message = ""
        notify_audience = ""
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, save_events_batch, event_format, rows)
    actor = _admin_role(request, config)
    from bot.db.admin_audit import log_admin_action

    touched_ids = [
        str(r.get("id"))
        for r in rows
        if r.get("id") and (r.get("delete") or r.get("purge") or r.get("date"))
    ]
    hidden_items = (result.get("hidden_items") or [])[:40]
    actions = (result.get("actions") or [])[:40]
    # Не пишем пустой «сохранил 17», если реально ничего не меняли.
    if actions or result.get("errors") or (notify_message and notify_audience and result.get("hidden_ids")):
        log_admin_action(
            actor_role=actor,
            action="events_save",
            entity_type="afisha",
            entity_id=event_format,
            details={
                "format": event_format,
                "saved": result.get("saved"),
                "added": result.get("added"),
                "changed": result.get("changed"),
                "hidden": result.get("hidden"),
                "deleted": result.get("deleted"),
                "errors": len(result.get("errors") or []),
                "notify_audience": notify_audience or "",
                "notify": bool(notify_message and notify_audience),
                "actions": actions,
                "hidden_items": hidden_items,
                "deleted_items": (result.get("deleted_items") or [])[:40],
            },
        )
    notify_result = None
    cancel_result = None
    hidden_ids = [int(x) for x in (result.get("hidden_ids") or []) if x]
    # Notify while bookings are still active, then cancel them.
    if (
        notify_message
        and notify_audience in {"booked", "confirmed", "both"}
        and hidden_ids
    ):
        from bot.admin.event_notify import notify_event_guests_async

        notify_result = await notify_event_guests_async(
            hidden_ids, notify_message, notify_audience
        )
        log_admin_action(
            actor_role=actor,
            action="events_cancel_notify",
            entity_type="event",
            entity_id=",".join(str(i) for i in hidden_ids),
            details={
                "ok": notify_result.get("ok"),
                "fail": notify_result.get("fail"),
                "skipped": notify_result.get("skipped"),
                "audience": notify_result.get("audience") or notify_audience,
                "message_preview": notify_result.get("message_preview") or "",
                "events": notify_result.get("events") or hidden_items,
                "items": (notify_result.get("items") or [])[:40],
                "errors": (notify_result.get("errors") or [])[:20],
            },
        )
    if hidden_ids:
        from bot.admin.event_notify import cancel_event_bookings_async

        cancel_result = await cancel_event_bookings_async(hidden_ids)
        log_admin_action(
            actor_role=actor,
            action="events_cancel_bookings",
            entity_type="event",
            entity_id=",".join(str(i) for i in hidden_ids),
            details={
                "cancelled": cancel_result.get("cancelled"),
                "fail": cancel_result.get("fail"),
                "skipped": cancel_result.get("skipped"),
                "events": cancel_result.get("events") or hidden_items,
                "items": (cancel_result.get("items") or [])[:40],
                "errors": (cancel_result.get("errors") or [])[:20],
            },
        )
    if result.get("errors"):
        can_view_db = _can_view_db(request, config)
        can_view_ops = _can_view_ops(request, config)
        can_resend = _can_resend_tickets(request, config)
        filters = {
            "tab": "events",
            "ef": event_format,
            "status": "",
            "date": "",
            "event": "",
            "format": "",
            "u": "",
            "table": "",
            "page": "1",
            "sort": "",
            "order": "",
            "channel": "",
            "date_from": "",
            "date_to": "",
            "all": "",
            "tickets": "",
        }
        empty_dashboard = {
            "events": [],
            "bookings": [],
            "users": {},
            "totals": {"events": 0, "bookings": 0, "reserved_guests": 0, "confirmed_guests": 0},
        }
        events_bundle = await loop.run_in_executor(None, list_events_for_admin, event_format)
        flash = (
            f"Частично сохранено: {result.get('saved', 0)} · скрыто: {result.get('hidden', 0)}"
            f" · удалено: {result.get('deleted', 0)}"
            if result.get("saved") or result.get("hidden") or result.get("deleted")
            else ""
        )
        if notify_result and not notify_result.get("skipped"):
            flash = (flash + " " if flash else "") + (
                f"Рассылка об отмене: успешно {notify_result.get('ok', 0)}, "
                f"ошибок {notify_result.get('fail', 0)}."
            )
        if cancel_result and not cancel_result.get("skipped"):
            flash = (flash + " " if flash else "") + (
                f"Отменено броней/билетов: {cancel_result.get('cancelled', 0)}"
                + (
                    f", ошибок {cancel_result.get('fail', 0)}"
                    if cancel_result.get("fail")
                    else ""
                )
                + "."
            )
        source_label = "PostgreSQL" if _use_postgres(config) else f"SQLite ({config.db_path})"
        return web.Response(
            text=render_admin_html(
                empty_dashboard,
                filters,
                source_label,
                None,
                can_view_db,
                None,
                None,
                events_bundle,
                flash,
                result.get("errors") or [],
                None,
                can_view_ops=can_view_ops,
                can_resend_tickets=can_resend,
            ),
            content_type="text/html",
        )
    q = f"/admin?tab=events&ef={quote(event_format)}&saved=1"
    if notify_result and not notify_result.get("skipped"):
        q += f"&n_ok={int(notify_result.get('ok') or 0)}&n_fail={int(notify_result.get('fail') or 0)}"
    if cancel_result and not cancel_result.get("skipped"):
        q += (
            f"&c_ok={int(cancel_result.get('cancelled') or 0)}"
            f"&c_fail={int(cancel_result.get('fail') or 0)}"
        )
    raise web.HTTPFound(q)


async def events_restore_page(request: web.Request) -> web.Response:
    config = request.app["config"]
    if not _check_auth(request, config):
        return web.Response(text=render_login_html(), status=200, content_type="text/html")
    from bot.db.events_admin import restore_events
    from bot.db.admin_audit import log_admin_action
    from urllib.parse import quote

    post = await request.post()
    event_format = (post.get("ef") or "best").strip()
    if event_format not in {"best", "proverka", "hitloto"}:
        event_format = "best"
    ids = post.getall("e_restore") if hasattr(post, "getall") else []
    result = await asyncio.get_running_loop().run_in_executor(
        None, restore_events, event_format, list(ids or [])
    )
    log_admin_action(
        actor_role=_admin_role(request, config),
        action="events_restore",
        entity_type="afisha",
        entity_id=event_format,
        details={
            "format": event_format,
            "restored": result.get("restored"),
            "errors": len(result.get("errors") or []),
            "ids": list(ids or []),
            "actions": (result.get("actions") or [])[:40],
            "restored_items": (result.get("restored_items") or [])[:40],
        },
    )
    if result.get("errors") and not result.get("restored"):
        raise web.HTTPFound(f"/admin?tab=events&ef={quote(event_format)}&saved=0")
    raise web.HTTPFound(f"/admin?tab=events&ef={quote(event_format)}&saved=1")


async def raffle_screen_page(request: web.Request) -> web.Response:
    """Проксирует скрин заявки из Telegram по file_id — без сохранения на диск сервера."""
    config = request.app["config"]
    if not _check_auth(request, config):
        raise web.HTTPFound("/admin")
    if not _can_view_ops(request, config):
        raise web.HTTPForbidden(text="Недостаточно прав")

    try:
        submission_id = int(request.match_info["submission_id"])
    except (TypeError, ValueError, KeyError):
        raise web.HTTPBadRequest(text="Некорректный id заявки") from None

    from bot.db.crud import get_raffle_submission

    row = await asyncio.get_running_loop().run_in_executor(
        None, get_raffle_submission, submission_id
    )
    if not row:
        raise web.HTTPNotFound(text="Заявка не найдена")

    # id, telegram_id, username, full_name, kind, status, photo_file_id, ...
    file_id = (row[6] or "").strip() if len(row) > 6 else ""
    if not _looks_like_telegram_file_id(file_id):
        raise web.HTTPNotFound(
            text=(
                "Превью недоступно: в заявке нет Telegram file_id. "
                "Для старых VK-заявок откройте скрин в чате модерации."
            )
        )

    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        raise web.HTTPServiceUnavailable(text="BOT_TOKEN не задан на сервере админки")

    from aiogram import Bot

    bot = Bot(token=token)
    try:
        tg_file = await bot.get_file(file_id)
        if not tg_file or not tg_file.file_path:
            raise web.HTTPNotFound(text="Файл не найден в Telegram")
        buf = await bot.download_file(tg_file.file_path)
        data = buf.read() if hasattr(buf, "read") else bytes(buf or b"")
        if not data:
            raise web.HTTPNotFound(text="Пустой файл")
        path = (tg_file.file_path or "").lower()
        if path.endswith(".png"):
            ctype = "image/png"
        elif path.endswith(".webp"):
            ctype = "image/webp"
        else:
            ctype = "image/jpeg"
        return web.Response(
            body=data,
            content_type=ctype,
            headers={"Cache-Control": "private, max-age=300"},
        )
    except web.HTTPException:
        raise
    except Exception:
        raise web.HTTPNotFound(text="Не удалось загрузить скрин из Telegram") from None
    finally:
        await bot.session.close()


async def login_page(request: web.Request) -> web.Response:
    config = request.app["config"]
    data = await request.post()
    token = (data.get("token") or "").strip()
    if not _token_matches(token, config):
        return web.Response(
            text=render_login_html("Неверный токен"),
            status=200,
            content_type="text/html",
        )
    # Manager → only events; client/owner → main admin
    if _is_manager_token(token, config) and not _is_owner_token(token, config) and not _is_client_token(token, config):
        dest = "/admin?tab=events"
        role = "manager"
    elif _is_owner_token(token, config):
        dest = "/admin"
        role = "owner"
    elif _is_client_token(token, config):
        dest = "/admin"
        role = "client"
    else:
        dest = "/admin"
        role = "unknown"
    from bot.db.admin_audit import log_admin_action

    log_admin_action(actor_role=role, action="login", entity_type="admin", entity_id="")
    return _login_success_response(request, token, dest)


async def logout_page(request: web.Request) -> web.Response:
    response = web.HTTPFound("/admin")
    response.del_cookie(ADMIN_COOKIE_NAME, path="/")
    raise response


async def index_page(request: web.Request) -> web.Response:
    raise web.HTTPFound("/admin")


async def events_hide_preview_page(request: web.Request) -> web.Response:
    config = request.app["config"]
    if not _check_auth(request, config):
        # 403, не 401: иначе браузер может снова спросить nginx basic auth.
        return web.json_response({"error": "auth"}, status=403)
    raw_ids = (request.query.get("ids") or "").strip()
    audience = (request.query.get("audience") or "").strip()
    if not _can_resend_tickets(request, config):
        # Managers may preview cancel impact, but not message audience.
        audience = ""
    elif not audience:
        audience = ""
    ids = [p for p in raw_ids.split(",") if p.strip()]
    from bot.admin.event_notify import build_hide_impact

    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, build_hide_impact, ids, audience)
    return web.json_response(
        {
            "event_ids": data.get("event_ids") or [],
            "cancel_count": data.get("cancel_count") or 0,
            "ticket_count": data.get("ticket_count") or 0,
            "notify_count": data.get("notify_count") or 0,
            "html": data.get("html") or "",
        }
    )


def _mailing_filters_from_form(form) -> dict:
    statuses: list[str] = []
    try:
        if hasattr(form, "getall"):
            raw_statuses = form.getall("booking_statuses")
        else:
            one = form.get("booking_statuses")
            raw_statuses = [] if one is None else [one]
        for item in raw_statuses or []:
            if item is None or hasattr(item, "file"):
                continue
            text = str(item).strip()
            if text:
                statuses.append(text)
    except Exception:
        statuses = []
    date_from = (form.get("date_from") or form.get("booking_date_from") or "").strip()
    date_to = (form.get("date_to") or form.get("booking_date_to") or "").strip()
    return {
        "booking_statuses": statuses,
        "date_mode": (form.get("date_mode") or "event").strip(),
        "date_from": date_from,
        "date_to": date_to,
        "has_phone": (form.get("has_phone") or "") in {"1", "on", "true", "yes"},
        "exclude_blocked": (form.get("exclude_blocked") or "") in {"1", "on", "true", "yes"},
        "exclude_sent_days": (form.get("exclude_sent_days") or "0").strip(),
        "batch_limit": (form.get("batch_limit") or "").strip(),
    }


def _parse_interval_sec(raw) -> float:
    text = str(raw or "0.1").strip().replace(",", ".") or "0.1"
    try:
        return max(0.0, min(float(text), 60.0))
    except ValueError:
        return 0.1


async def mailing_preview_page(request: web.Request) -> web.Response:
    config = request.app["config"]
    if not _check_auth(request, config) or not _can_resend_tickets(request, config):
        raise web.HTTPFound("/admin/login")
    form = await request.post()
    channel = (form.get("channel") or "telegram").strip()
    interval = _parse_interval_sec(form.get("interval_sec"))
    from bot.db.mailing import estimate_duration_sec, format_duration, preview_audience

    try:
        filters = _mailing_filters_from_form(form)
        data = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: preview_audience(channel, filters),
        )
    except Exception as exc:
        return web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=400)
    capped = int(data.get("capped_total") or 0)
    data["eta"] = format_duration(estimate_duration_sec(capped, interval))
    data["interval_sec"] = interval
    return web.json_response(data)


async def mailing_users_search_page(request: web.Request) -> web.Response:
    config = request.app["config"]
    if not _check_auth(request, config) or not _can_resend_tickets(request, config):
        return web.json_response({"error": "Нет доступа"}, status=403)
    q = (request.query.get("q") or "").strip()
    channel = (request.query.get("channel") or "").strip()
    if channel not in ("telegram", "vkontakte", ""):
        channel = ""
    from bot.db.mailing import search_users_for_test

    try:
        users = await asyncio.get_running_loop().run_in_executor(
            None, lambda: search_users_for_test(q, channel=channel, limit=20)
        )
    except Exception as exc:
        return web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=400)
    return web.json_response({"users": users})


async def mailing_test_page(request: web.Request) -> web.Response:
    config = request.app["config"]
    if not _check_auth(request, config) or not _can_resend_tickets(request, config):
        return web.json_response({"error": "Нет доступа"}, status=403)
    form = await request.post()
    user_id_raw = (form.get("user_id") or "").strip()
    if not user_id_raw.isdigit():
        return web.json_response({"error": "Не выбран пользователь"}, status=400)
    channel_pref = (form.get("channel") or "telegram").strip()
    body_html = (form.get("body_html") or "").strip()
    button_text = (form.get("button_text") or "").strip()
    button_url = (form.get("button_url") or "").strip()
    followup_html = (form.get("followup_html") or "").strip()

    photo_path = None
    photo = form.get("photo")
    if photo is not None and getattr(photo, "file", None):
        raw = photo.file.read()
        if raw:
            from pathlib import Path

            root = Path(__file__).resolve().parents[2]
            media_dir = root / "data" / "mailing"
            media_dir.mkdir(parents=True, exist_ok=True)
            filename = getattr(photo, "filename", "") or "photo.jpg"
            ext = Path(filename).suffix.lower()
            if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
                ext = ".jpg"
            dest = media_dir / f"test_{user_id_raw}{ext}"
            dest.write_bytes(raw)
            photo_path = str(dest)

    if not body_html and not photo_path:
        return web.json_response({"error": "Нужен текст или картинка"}, status=400)

    from bot.admin.mailing_worker import send_one
    from bot.db.admin_audit import log_admin_action
    from bot.db.mailing import create_followup_stub, get_user_for_mailing

    user = await asyncio.get_running_loop().run_in_executor(
        None, get_user_for_mailing, int(user_id_raw)
    )
    if not user:
        return web.json_response({"error": "Пользователь не найден"}, status=404)

    send_channel = None
    peer_id = None
    if channel_pref == "telegram" and user.get("telegram_id"):
        send_channel, peer_id = "telegram", int(user["telegram_id"])
    elif channel_pref == "vkontakte" and user.get("vk_id"):
        send_channel, peer_id = "vkontakte", int(user["vk_id"])
    elif channel_pref == "both":
        if user.get("telegram_id"):
            send_channel, peer_id = "telegram", int(user["telegram_id"])
        elif user.get("vk_id"):
            send_channel, peer_id = "vkontakte", int(user["vk_id"])
    else:
        if user.get("telegram_id"):
            send_channel, peer_id = "telegram", int(user["telegram_id"])
        elif user.get("vk_id"):
            send_channel, peer_id = "vkontakte", int(user["vk_id"])

    if not send_channel or not peer_id:
        return web.json_response(
            {"error": "У пользователя нет id выбранного канала"},
            status=400,
        )

    campaign_id = 0
    if button_text and followup_html and not button_url:
        campaign_id = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: create_followup_stub(
                followup_html=followup_html,
                body_html=body_html,
                created_by=_admin_role(request, config) or "owner",
            ),
        )

    campaign = {
        "id": campaign_id,
        "body_html": body_html,
        "photo_path": photo_path,
        "button_text": button_text,
        "button_url": button_url,
        "followup_html": followup_html,
    }
    recipient = {
        "channel": send_channel,
        "peer_id": peer_id,
        "user_id": int(user_id_raw),
    }
    try:
        await send_one(campaign, recipient)
    except Exception as exc:
        return web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=400)

    log_admin_action(
        actor_role=_admin_role(request, config) or "owner",
        action="mailing_test",
        entity_type="user",
        entity_id=str(user_id_raw),
        details={"channel": send_channel, "peer_id": peer_id},
    )
    return web.json_response(
        {"ok": True, "user_id": int(user_id_raw), "channel": send_channel, "peer_id": peer_id}
    )


async def mailing_create_page(request: web.Request) -> web.Response:
    config = request.app["config"]
    if not _check_auth(request, config) or not _can_resend_tickets(request, config):
        raise web.HTTPFound("/admin/login")
    form = await request.post()
    channel = (form.get("channel") or "telegram").strip()
    title = (form.get("title") or "").strip()
    body_html = (form.get("body_html") or "").strip()
    button_text = (form.get("button_text") or "").strip()
    button_url = (form.get("button_url") or "").strip()
    followup_html = (form.get("followup_html") or "").strip()
    interval = _parse_interval_sec(form.get("interval_sec"))

    photo_path = None
    photo = form.get("photo")
    if photo is not None and getattr(photo, "file", None):
        raw = photo.file.read()
        if raw:
            from pathlib import Path

            root = Path(__file__).resolve().parents[2]
            media_dir = root / "data" / "mailing"
            media_dir.mkdir(parents=True, exist_ok=True)
            filename = getattr(photo, "filename", "") or "photo.jpg"
            ext = Path(filename).suffix.lower()
            if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
                ext = ".jpg"
            tmp_name = f"upload_{int(asyncio.get_running_loop().time() * 1000)}{ext}"
            dest = media_dir / tmp_name
            dest.write_bytes(raw)
            photo_path = str(dest)

    from pathlib import Path
    from urllib.parse import quote

    from bot.db.admin_audit import log_admin_action
    from bot.db.mailing import create_campaign, set_campaign_photo

    try:
        filters = _mailing_filters_from_form(form)
        campaign = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: create_campaign(
                title=title,
                channel=channel,
                body_html=body_html,
                interval_sec=interval,
                filters=filters,
                photo_path=photo_path,
                button_text=button_text,
                button_url=button_url,
                followup_html=followup_html,
                created_by=_admin_role(request, config) or "owner",
                start=True,
            ),
        )
    except Exception as exc:
        if photo_path:
            try:
                Path(photo_path).unlink(missing_ok=True)
            except Exception:
                pass
        raise web.HTTPFound(
            f"/admin?tab=mailing&m_err={quote(f'{type(exc).__name__}: {exc}'[:200])}"
        )

    if photo_path and campaign.get("id"):
        root = Path(__file__).resolve().parents[2]
        final = root / "data" / "mailing" / f"{int(campaign['id'])}{Path(photo_path).suffix}"
        try:
            Path(photo_path).replace(final)
            await asyncio.get_running_loop().run_in_executor(
                None, set_campaign_photo, int(campaign["id"]), str(final)
            )
            campaign["photo_path"] = str(final)
        except Exception:
            pass

    log_admin_action(
        actor_role=_admin_role(request, config) or "owner",
        action="mailing_create",
        entity_type="mailing_campaign",
        entity_id=str(campaign.get("id")),
        details={
            "channel": channel,
            "total": campaign.get("total_count"),
            "interval_sec": interval,
        },
    )
    flash = quote(
        f"Кампания #{campaign.get('id')} запущена · {campaign.get('total_count')} получателей"
    )
    raise web.HTTPFound(f"/admin?tab=mailing&m_flash={flash}")


async def mailing_status_page(request: web.Request) -> web.Response:
    config = request.app["config"]
    if not _check_auth(request, config) or not _can_resend_tickets(request, config):
        raise web.HTTPFound("/admin/login")
    form = await request.post()
    cid = (form.get("campaign_id") or "").strip()
    status = (form.get("status") or "").strip()
    if not cid.isdigit():
        raise web.HTTPFound("/admin?tab=mailing")
    from bot.db.admin_audit import log_admin_action
    from bot.db.mailing import set_campaign_status

    allowed = {"paused", "queued", "cancelled"}
    if status not in allowed:
        raise web.HTTPFound("/admin?tab=mailing")
    await asyncio.get_running_loop().run_in_executor(
        None, set_campaign_status, int(cid), status
    )
    log_admin_action(
        actor_role=_admin_role(request, config) or "owner",
        action="mailing_status",
        entity_type="mailing_campaign",
        entity_id=cid,
        details={"status": status},
    )
    raise web.HTTPFound(f"/admin?tab=mailing&campaign={cid}")


def create_app(config: AdminConfig | None = None) -> web.Application:
    from bot.admin import vk_entry
    from bot.admin.mailing_worker import start_mailing_worker
    from bot.db.mailing import ensure_mailing_tables

    app = web.Application(client_max_size=20 * 1024 * 1024)
    app["config"] = config or load_config()
    ensure_mailing_tables()
    start_mailing_worker(app)
    app.router.add_get("/", index_page)
    app.router.add_get("/admin", admin_page)
    app.router.add_get("/admin/raffle-screen/{submission_id}", raffle_screen_page)
    app.router.add_get("/admin/events/hide-preview", events_hide_preview_page)
    app.router.add_post("/admin/events/save", events_save_page)
    app.router.add_post("/admin/events/restore", events_restore_page)
    app.router.add_post("/admin/events/resend-ticket", events_resend_ticket_page)
    app.router.add_post("/admin/users/anonymize", users_anonymize_page)
    app.router.add_post("/admin/mailing/preview", mailing_preview_page)
    app.router.add_post("/admin/mailing/create", mailing_create_page)
    app.router.add_post("/admin/mailing/status", mailing_status_page)
    app.router.add_get("/admin/mailing/users-search", mailing_users_search_page)
    app.router.add_post("/admin/mailing/test", mailing_test_page)
    app.router.add_post("/admin/login", login_page)
    app.router.add_get("/admin/logout", logout_page)
    # Публичные VK-ленды (без admin auth); nginx не закрывает /vk/*
    vk_entry.register_routes(app)
    return app


def run():
    host = os.getenv("ADMIN_HOST", "127.0.0.1")
    port = int(os.getenv("ADMIN_PORT", "8080"))
    web.run_app(create_app(), host=host, port=port)


if __name__ == "__main__":
    run()
