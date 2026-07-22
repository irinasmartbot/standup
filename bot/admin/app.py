import asyncio
import hmac
import html
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

import psycopg
from psycopg.rows import dict_row
from aiohttp import web


STATUSES = ("booked", "confirmed", "cancelled", "annulled")
ACTIVE_STATUSES = {"booked", "confirmed"}
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


def load_config() -> AdminConfig:
    database_url = os.getenv("DATABASE_URL", "")
    return AdminConfig(
        database_url=database_url,
        db_path=os.getenv("DB_PATH", "bookings.db"),
        bookings_source=os.getenv("BOOKINGS_SOURCE", "postgres" if database_url else "sqlite"),
        admin_token=os.getenv("ADMIN_TOKEN", ""),
    )


def _use_postgres(config: AdminConfig) -> bool:
    return config.bookings_source == "postgres" and bool(config.database_url)


def _parse_int(value, default=0):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _short_dt(value):
    if not value:
        return ""
    text = str(value)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
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


def _fetch_postgres_rows(config: AdminConfig, filters: dict) -> list[dict]:
    where = ["e.event_date >= CURRENT_DATE - INTERVAL '1 day'"]
    params = {}
    if filters.get("format"):
        where.append("e.format = %(format)s")
        params["format"] = filters["format"]
    if filters.get("date"):
        where.append("e.event_date = to_date(%(date)s, 'DD.MM.YYYY')")
        params["date"] = filters["date"]

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
            u.telegram_id,
            u.vk_id,
            u.username,
            u.name,
            u.phone
        FROM events e
        LEFT JOIN bookings b ON b.event_id = e.id
        LEFT JOIN users u ON u.id = b.user_id
        WHERE {" AND ".join(where)}
        ORDER BY e.event_date, e.event_time, e.location, b.created_at DESC NULLS LAST
    """
    with psycopg.connect(config.database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


def _sqlite_columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _fetch_sqlite_rows(config: AdminConfig, filters: dict) -> list[dict]:
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
        if filters.get("date"):
            where.append("event_date = ?")
            params.append(filters["date"])
        if filters.get("status"):
            where.append("status = ?")
            params.append(filters["status"])
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        has_annulled_at = "annulled_at" in columns
        annulled_expr = "annulled_at" if has_annulled_at else "NULL"
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
                {annulled_expr} AS annulled_at
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


def fetch_admin_rows(config: AdminConfig, filters: dict) -> list[dict]:
    if _use_postgres(config):
        rows = _fetch_postgres_rows(config, filters)
        status = filters.get("status")
        if status:
            rows = [row for row in rows if row.get("booking_id") is not None and row.get("status") == status]
        return rows
    return _fetch_sqlite_rows(config, filters)


def build_dashboard(rows: list[dict]) -> dict:
    events = {}
    activity = []
    totals = {"events": 0, "bookings": 0, "active_guests": 0}

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
                "active_guests": 0,
            },
        )

        if not row.get("booking_id"):
            continue

        status = _normalize_status(row.get("status"))
        guests = _parse_int(row.get("guests"))
        changed_at = (
            row.get("cancelled_at")
            or row.get("annulled_at")
            or row.get("confirmed_at")
            or row.get("updated_at")
            or row.get("created_at")
        )
        booking = {
            "id": row.get("booking_id"),
            "status": status,
            "status_label": STATUS_LABELS[status],
            "guests": guests,
            "source": row.get("source") or "",
            "format": row.get("booking_format") or row.get("event_format") or "",
            "created_at": _short_dt(row.get("created_at")),
            "changed_at": _short_dt(changed_at),
            "name": row.get("name") or "",
            "username": row.get("username") or "",
            "phone": row.get("phone") or "",
            "telegram_id": row.get("telegram_id") or "",
            "vk_id": row.get("vk_id") or "",
            "event": event,
        }
        event["bookings"].append(booking)
        event["status_counts"][status] += 1
        event["status_guests"][status] += guests
        if status in ACTIVE_STATUSES:
            event["active_guests"] += guests
            totals["active_guests"] += guests
        totals["bookings"] += 1
        activity.append(booking)

    for event in events.values():
        event["bookings"].sort(key=lambda b: (b["changed_at"], str(b["id"])), reverse=True)
    activity.sort(key=lambda b: (b["changed_at"], str(b["id"])), reverse=True)
    totals["events"] = len(events)
    return {"events": list(events.values()), "activity": activity[:20], "totals": totals}


def _h(value) -> str:
    return html.escape(str(value or ""))


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


def _capacity_bar(event: dict) -> str:
    max_seats = event["max_seats"]
    if max_seats <= 0:
        return '<div class="capacity muted">Лимит мест не указан</div>'
    active = event["active_guests"]
    percent = min(100, active / max_seats * 100)
    free = max(0, max_seats - active)
    return (
        f'<div class="capacity-line"><span>{active}/{max_seats} гостей</span>'
        f'<span>{free} свободно</span></div>'
        f'<div class="capacity-bar"><span style="width:{percent:.1f}%"></span></div>'
    )


def _booking_table(bookings: list[dict]) -> str:
    if not bookings:
        return '<p class="muted">Броней пока нет.</p>'
    rows = []
    for booking in bookings:
        contact = _h(booking["phone"])
        if booking["username"]:
            contact += f'<br><span class="muted">@{_h(booking["username"])}</span>'
        rows.append(
            "<tr>"
            f"<td>#{_h(booking['id'])}</td>"
            f"<td>{_status_badge(booking['status'])}</td>"
            f"<td><b>{_h(booking['name'])}</b><br><span class='muted'>{_h(booking['source'])}</span></td>"
            f"<td>{contact}</td>"
            f"<td>{_h(booking['guests'])}</td>"
            f"<td>{_h(booking['created_at'])}</td>"
            f"<td>{_h(booking['changed_at'])}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>ID</th><th>Статус</th><th>Клиент</th>"
        "<th>Контакт</th><th>Гости</th><th>Создана</th><th>Изменена</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render_admin_html(dashboard: dict, filters: dict, source_label: str) -> str:
    event_cards = []
    for event in dashboard["events"]:
        counts = " ".join(
            f'<span class="counter">{_h(STATUS_LABELS[s])}: <b>{event["status_counts"].get(s, 0)}</b></span>'
            for s in STATUSES
        )
        event_cards.append(
            '<section class="card">'
            '<div class="event-head">'
            f'<div><h2>{_h(event["date"])} в {_h(event["time"])} · {_h(event["location"])}</h2>'
            f'<p>{_h(event["address"])}</p></div>'
            f'<span class="format">{_h(event["format"])}</span>'
            '</div>'
            f'{_capacity_bar(event)}'
            f'{_status_bar(event)}'
            f'<div class="counters">{counts}</div>'
            f'{_booking_table(event["bookings"])}'
            '</section>'
        )

    activity_rows = []
    for booking in dashboard["activity"]:
        event = booking["event"]
        activity_rows.append(
            "<tr>"
            f"<td>{_h(booking['changed_at'])}</td>"
            f"<td>{_status_badge(booking['status'])}</td>"
            f"<td>{_h(event['date'])} {_h(event['time'])}<br><span class='muted'>{_h(event['location'])}</span></td>"
            f"<td>{_h(booking['name'])}</td>"
            f"<td>{_h(booking['guests'])}</td>"
            "</tr>"
        )
    empty_activity = '<tr><td colspan="5" class="muted">Изменений пока нет</td></tr>'
    activity = (
        "<table><thead><tr><th>Когда</th><th>Статус</th><th>Мероприятие</th><th>Клиент</th><th>Гости</th></tr></thead>"
        f"<tbody>{''.join(activity_rows) or empty_activity}</tbody></table>"
    )

    filter_links = " ".join(
        [
            f'<a class="pill {"active" if not filters.get("status") else ""}" href="{_query_link(filters, status="")}">Все</a>',
            *[
                f'<a class="pill {"active" if filters.get("status") == status else ""}" '
                f'href="{_query_link(filters, status=status)}">{_h(STATUS_LABELS[status])}</a>'
                for status in STATUSES
            ],
        ]
    )
    totals = dashboard["totals"]
    format_options = "".join(
        f'<option value="{fmt}" {"selected" if filters.get("format") == fmt else ""}>{fmt}</option>'
        for fmt in ("proverka", "best", "rozygrysh", "1plus1")
    )
    hidden_status = (
        f'<input type="hidden" name="status" value="{_h(filters.get("status"))}">'
        if filters.get("status")
        else ""
    )
    reset_link = _query_link({})
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>Админка броней</title>
  <style>
    :root {{ color-scheme: light; --bg:#f4f6fb; --card:#fff; --text:#111827; --muted:#667085; --line:#e5e7eb; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family: Inter, Arial, sans-serif; background:var(--bg); color:var(--text); }}
    header {{ padding:28px 32px; background:#111827; color:white; }}
    header h1 {{ margin:0 0 8px; font-size:30px; }}
    header p {{ margin:0; color:#cbd5e1; }}
    header a {{ color:white; }}
    main {{ max-width:1280px; margin:0 auto; padding:24px; }}
    .summary {{ display:grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap:16px; margin-bottom:20px; }}
    .metric, .card, .filters {{ background:var(--card); border:1px solid var(--line); border-radius:18px; box-shadow:0 8px 30px rgba(15,23,42,.05); }}
    .metric {{ padding:18px; }}
    .metric span {{ display:block; color:var(--muted); font-size:14px; }}
    .metric b {{ display:block; margin-top:8px; font-size:30px; }}
    .filters {{ padding:16px; margin-bottom:20px; display:flex; gap:12px; flex-wrap:wrap; align-items:center; }}
    .filters form {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-left:auto; }}
    input, select, button {{ border:1px solid var(--line); border-radius:10px; padding:9px 11px; background:white; font:inherit; }}
    button {{ background:#111827; color:white; cursor:pointer; }}
    .pill {{ padding:9px 12px; border-radius:999px; border:1px solid var(--line); color:#111827; text-decoration:none; }}
    .pill.active {{ background:#111827; color:white; border-color:#111827; }}
    .card {{ padding:20px; margin-bottom:18px; }}
    .event-head {{ display:flex; justify-content:space-between; gap:16px; align-items:start; }}
    h2 {{ margin:0 0 6px; font-size:22px; }}
    .event-head p {{ margin:0; color:var(--muted); }}
    .format {{ background:#eef2ff; color:#3730a3; padding:7px 10px; border-radius:999px; font-weight:700; }}
    .capacity-line {{ display:flex; justify-content:space-between; margin-top:16px; font-weight:700; }}
    .capacity-bar, .status-bar {{ overflow:hidden; height:14px; background:#e5e7eb; border-radius:999px; margin-top:8px; display:flex; }}
    .capacity-bar span {{ display:block; background:#2563eb; }}
    .status-bar span {{ display:block; }}
    .status-bar.empty {{ background:#eef2f7; }}
    .counters {{ display:flex; gap:8px; flex-wrap:wrap; margin:14px 0; }}
    .counter {{ background:#f8fafc; border:1px solid var(--line); border-radius:999px; padding:7px 10px; color:#334155; }}
    table {{ width:100%; border-collapse:collapse; margin-top:12px; }}
    th, td {{ padding:11px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
    th {{ color:#475467; font-size:13px; background:#f8fafc; }}
    .badge {{ display:inline-block; color:white; border-radius:999px; padding:5px 9px; font-size:12px; font-weight:700; }}
    .muted {{ color:var(--muted); }}
    .activity {{ margin-bottom:22px; }}
    @media (max-width: 780px) {{
      header {{ padding:22px 18px; }}
      main {{ padding:16px; }}
      .summary {{ grid-template-columns:1fr; }}
      .event-head {{ display:block; }}
      .filters form {{ margin-left:0; width:100%; }}
      table {{ display:block; overflow-x:auto; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Админка броней</h1>
    <p>Автообновление каждые 30 секунд · источник данных: {_h(source_label)} · <a href="/admin/logout">выйти</a></p>
  </header>
  <main>
    <div class="summary">
      <div class="metric"><span>Мероприятий в выдаче</span><b>{totals["events"]}</b></div>
      <div class="metric"><span>Всего броней</span><b>{totals["bookings"]}</b></div>
      <div class="metric"><span>Активных гостей</span><b>{totals["active_guests"]}</b></div>
    </div>
    <div class="filters">
      {filter_links}
      <form method="get" action="/admin">
        <input name="date" placeholder="Дата: 25.07.2026" value="{_h(filters.get("date", ""))}">
        <select name="format">
          <option value="">Все форматы</option>
          {format_options}
        </select>
        {hidden_status}
        <button type="submit">Показать</button>
        <a class="pill" href="{reset_link}">Сбросить</a>
      </form>
    </div>
    <section class="card activity">
      <h2>Последние изменения</h2>
      {activity}
    </section>
    {''.join(event_cards) or '<section class="card"><p class="muted">Нет данных по выбранным фильтрам.</p></section>'}
  </main>
</body>
</html>"""


def _filters_from_request(request: web.Request) -> dict:
    return {
        "status": request.query.get("status", "").strip(),
        "date": request.query.get("date", "").strip(),
        "format": request.query.get("format", "").strip(),
    }


def _token_matches(candidate: str, config: AdminConfig) -> bool:
    if not candidate or not config.admin_token:
        return False
    return hmac.compare_digest(candidate, config.admin_token)


def _check_auth(request: web.Request, config: AdminConfig) -> bool:
    if not config.admin_token:
        return True
    token = (
        request.query.get("token")
        or request.headers.get("X-Admin-Token")
        or request.cookies.get(ADMIN_COOKIE_NAME)
        or ""
    )
    return _token_matches(token, config)


def render_login_html(error: str = "") -> str:
    error_html = f'<p class="error">{_h(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Вход в админку</title>
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
    <h1>Вход в админку</h1>
    <p>Введите админ-токен из переменной <b>ADMIN_TOKEN</b>.</p>
    {error_html}
    <input name="token" type="password" autofocus placeholder="ADMIN_TOKEN">
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
    if config.admin_token and _token_matches(request.query.get("token", ""), config):
        response = web.HTTPFound(_redirect_without_token(request))
        response.set_cookie(
            ADMIN_COOKIE_NAME,
            config.admin_token,
            max_age=ADMIN_COOKIE_MAX_AGE,
            httponly=True,
            samesite="Lax",
        )
        raise response
    filters = _filters_from_request(request)
    if filters.get("status") and filters["status"] not in STATUSES:
        filters["status"] = ""
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, fetch_admin_rows, config, filters)
    dashboard = build_dashboard(rows)
    source_label = "PostgreSQL" if _use_postgres(config) else f"SQLite ({config.db_path})"
    return web.Response(text=render_admin_html(dashboard, filters, source_label), content_type="text/html")


async def login_page(request: web.Request) -> web.Response:
    config = request.app["config"]
    data = await request.post()
    token = (data.get("token") or "").strip()
    if not _token_matches(token, config):
        return web.Response(
            text=render_login_html("Неверный токен"),
            status=401,
            content_type="text/html",
        )
    response = web.HTTPFound("/admin")
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        config.admin_token,
        max_age=ADMIN_COOKIE_MAX_AGE,
        httponly=True,
        samesite="Lax",
    )
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
