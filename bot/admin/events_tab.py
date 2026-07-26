"""HTML + form parsing for admin «Мероприятия» tab."""

from __future__ import annotations

from urllib.parse import urlencode

from bot.db.events_admin import AFISHA_FORMAT_LABELS, AFISHA_FORMATS


def _h(value) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _events_link(fmt: str = "best", **extra) -> str:
    q = {"tab": "events", "ef": fmt}
    q.update({k: v for k, v in extra.items() if v not in (None, "")})
    return "/admin?" + urlencode(q)


def parse_events_form(post) -> tuple[str, list[dict]]:
    """Parse multipart/urlencoded save form into (format, rows)."""
    event_format = (post.get("ef") or post.get("format") or "best").strip()
    if event_format not in AFISHA_FORMATS:
        event_format = "best"

    ids = post.getall("e_id") if hasattr(post, "getall") else [post.get("e_id")]
    if ids is None:
        ids = []
    # aiohttp MultiDictProxy getall
    def all_vals(name: str) -> list:
        if hasattr(post, "getall"):
            return list(post.getall(name, []))
        v = post.get(name)
        return [v] if v is not None else []

    ids = all_vals("e_id")
    n = len(ids)
    dates = all_vals("e_date")
    times = all_vals("e_time")
    locations = all_vals("e_location")
    addresses = all_vals("e_address")
    descriptions = all_vals("e_description")
    images = all_vals("e_image")
    seats = all_vals("e_seats")
    prices = all_vals("e_price")
    payments = all_vals("e_payment")
    hosts = all_vals("e_host")
    deletes = set(all_vals("e_delete"))

    def at(seq, i, default=""):
        return seq[i] if i < len(seq) else default

    rows = []
    for i in range(n):
        raw_id = (at(ids, i) or "").strip()
        event_id = int(raw_id) if raw_id.isdigit() else None
        rows.append(
            {
                "id": event_id,
                "date": at(dates, i),
                "time": at(times, i),
                "location": at(locations, i),
                "address": at(addresses, i),
                "description": at(descriptions, i),
                "image_url": at(images, i),
                "max_seats": at(seats, i),
                "price": at(prices, i),
                "payment_url": at(payments, i),
                "host": at(hosts, i),
                "delete": raw_id in deletes or str(event_id) in deletes,
            }
        )
    return event_format, rows


def _row_html(
    event: dict | None,
    paid: bool,
    blank: bool = False,
    fmt: str = "best",
    *,
    show_tickets: bool = True,
) -> str:
    e = event or {}
    eid = "" if blank else str(e.get("id") or "")
    date_val = "" if blank else (e.get("date_iso") or "")
    time_val = "" if blank else (e.get("time") or "")
    loc = "" if blank else (e.get("location") or "")
    addr = "" if blank else (e.get("address") or "")
    desc = "" if blank else (e.get("description") or "")
    image = "" if blank else (e.get("image_url") or "")
    seats = "" if blank else str(e.get("max_seats") if e.get("max_seats") is not None else "")
    price = "" if blank else str(e.get("price") if e.get("price") is not None else "")
    pay = "" if blank else (e.get("payment_url") or "")
    host = "" if blank else (e.get("host") or "")
    weekday = "" if blank else (e.get("weekday") or "")

    paid_cells = ""
    if paid:
        paid_cells = (
            f'<td><input name="e_price" type="number" min="0" step="1" value="{_h(price)}" placeholder="0"></td>'
            f'<td><input name="e_payment" value="{_h(pay)}" placeholder="https://…"></td>'
            f'<td><input name="e_host" value="{_h(host)}" placeholder="Кто в составе"></td>'
        )
    else:
        paid_cells = (
            '<td class="muted">—<input type="hidden" name="e_price" value="0">'
            '<input type="hidden" name="e_payment" value="">'
            '<input type="hidden" name="e_host" value=""></td>'
            '<td class="muted">—</td><td class="muted">—</td>'
        )

    if eid:
        tickets_link = ""
        if show_tickets:
            tickets_link = (
                f'<br><a class="pill events-tickets-link" '
                f'href="{_events_link(fmt, tickets=eid)}">билеты</a>'
            )
        delete_cell = (
            f'<td class="events-del">'
            f'<label><input type="checkbox" name="e_delete" value="{_h(eid)}"> скрыть</label>'
            f"{tickets_link}</td>"
        )
    else:
        delete_cell = '<td class="muted">новая</td>'
    return (
        "<tr>"
        f'<td class="events-id"><input type="hidden" name="e_id" value="{_h(eid)}">'
        f'<span class="muted">{_h(eid or "—")}</span>'
        f'<div class="muted events-weekday">{_h(weekday)}</div></td>'
        f'<td><input name="e_date" type="date" value="{_h(date_val)}"></td>'
        f'<td><input name="e_time" type="time" value="{_h(time_val)}"></td>'
        f'<td><input name="e_location" value="{_h(loc)}" placeholder="Площадка"></td>'
        f'<td><input name="e_address" value="{_h(addr)}" placeholder="Адрес"></td>'
        f'<td><input name="e_seats" type="number" min="0" value="{_h(seats)}" placeholder="мест"></td>'
        f"{paid_cells}"
        f'<td><input name="e_description" value="{_h(desc)}" placeholder="Описание"></td>'
        f'<td><input name="e_image" value="{_h(image)}" placeholder="URL картинки"></td>'
        f"{delete_cell}"
        "</tr>"
    )


def _table(
    events: list[dict],
    paid: bool,
    blank_rows: int = 2,
    fmt: str = "best",
    *,
    show_tickets: bool = True,
) -> str:
    head_paid = "<th>Цена</th><th>Оплата</th><th>Состав</th>" if paid else "<th></th><th></th><th></th>"
    body = "".join(_row_html(e, paid, fmt=fmt, show_tickets=show_tickets) for e in events)
    body += "".join(
        _row_html(None, paid, blank=True, fmt=fmt, show_tickets=show_tickets)
        for _ in range(blank_rows)
    )
    return (
        '<div class="table-wrap events-table-wrap"><table class="events-edit">'
        "<thead><tr>"
        "<th>ID</th><th>Дата</th><th>Время</th><th>Площадка</th><th>Адрес</th><th>Мест</th>"
        f"{head_paid}"
        "<th>Описание</th><th>Картинка</th><th></th>"
        "</tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


def render_events_tab(
    event_format: str,
    bundle: dict | None,
    *,
    flash: str = "",
    errors: list[str] | None = None,
    tickets_event_id: str = "",
    ticket_holders: list[dict] | None = None,
    can_resend_tickets: bool = True,
) -> str:
    fmt = event_format if event_format in AFISHA_FORMATS else "best"
    bundle = bundle or {"active": [], "past": [], "hidden": []}
    paid = fmt in {"best", "hitloto"}
    sub = "".join(
        f'<a class="pill {"active" if key == fmt else ""}" href="{_events_link(key)}">{label}</a>'
        for key, label in AFISHA_FORMAT_LABELS.items()
    )
    flash_html = f'<p class="events-flash">{_h(flash)}</p>' if flash else ""
    err_html = ""
    if errors:
        err_html = (
            '<div class="events-errors"><b>Не сохранено полностью:</b><ul>'
            + "".join(f"<li>{_h(e)}</li>" for e in errors)
            + "</ul></div>"
        )

    if can_resend_tickets:
        note = (
            '<p class="muted">BEST — платные шоу; даты отсюда же использует розыгрыш. '
            "Пустые нижние строки — для быстрого добавления.<br>"
            "<b>Смена времени или площадки:</b> правьте ту же строку и сохраните — брони "
            "остаются на этом шоу и подтянут новые данные.<br>"
            "«билеты» — кто уже получил билет и переотправка. "
            "«Скрыть» убирает из бота; вернуть — в блоке «Скрытые».</p>"
            if fmt == "best"
            else '<p class="muted">Пустые нижние строки — быстро добавить шоу.<br>'
            "<b>Смена времени/площадки:</b> правьте ту же строку. "
            "«билеты» — список получивших и переотправка.</p>"
        )
    else:
        note = (
            '<p class="muted">BEST — платные шоу; даты отсюда же использует розыгрыш. '
            "Пустые нижние строки — для быстрого добавления.<br>"
            "<b>Смена времени или площадки:</b> правьте ту же строку и сохраните — брони "
            "остаются на этом шоу.<br>"
            "«Скрыть» убирает из бота; вернуть — в блоке «Скрытые».</p>"
            if fmt == "best"
            else '<p class="muted">Пустые нижние строки — быстро добавить шоу.<br>'
            "<b>Смена времени/площадки:</b> правьте ту же строку. "
            "«Скрыть» убирает из бота; вернуть — в блоке «Скрытые».</p>"
        )

    holders = ticket_holders or []
    tickets_panel = ""
    if can_resend_tickets and tickets_event_id:
        rows = []
        for h in holders:
            tid = h.get("telegram_id") or "—"
            uname = f"@{h.get('username')}" if h.get("username") else "—"
            got = "да" if h.get("has_ticket_msg") else "нет msg id"
            rows.append(
                "<tr>"
                f"<td>{_h(h.get('booking_id'))}</td>"
                f"<td>{_h(h.get('name') or '—')}<br><span class='muted'>{_h(uname)}</span></td>"
                f"<td>{_h(tid)}</td>"
                f"<td>{_h(h.get('phone') or '—')}</td>"
                f"<td>{_h(h.get('guests'))}</td>"
                f"<td>{_h(got)}</td>"
                f"<td>"
                f'<form method="post" action="/admin/events/resend-ticket" class="inline-form">'
                f'<input type="hidden" name="ef" value="{_h(fmt)}">'
                f'<input type="hidden" name="tickets" value="{_h(tickets_event_id)}">'
                f'<input type="hidden" name="booking_id" value="{_h(h.get("booking_id"))}">'
                f'<input type="hidden" name="updated" value="1">'
                '<button type="submit">Переотправить</button>'
                "</form></td>"
                "</tr>"
            )
        body = "".join(rows) or '<tr><td colspan="7" class="muted">Подтверждённых билетов на это шоу нет</td></tr>'
        tickets_panel = f"""
    <section class="card analytics-section events-tickets-panel">
      <h2>Билеты по шоу #{_h(tickets_event_id)}</h2>
      <p class="muted">Показаны только брони со статусом «подтверждено» (билет получен).
      Переотправка шлёт новый билет с текущими датой/временем/местом из афиши.</p>
      <div class="events-toolbar">
        <form method="post" action="/admin/events/resend-ticket">
          <input type="hidden" name="ef" value="{_h(fmt)}">
          <input type="hidden" name="tickets" value="{_h(tickets_event_id)}">
          <input type="hidden" name="event_id" value="{_h(tickets_event_id)}">
          <input type="hidden" name="updated" value="1">
          <button type="submit" {"disabled" if not holders else ""}>
            Переотправить всем ({len(holders)})
          </button>
        </form>
        <a class="pill" href="{_events_link(fmt)}">Закрыть список</a>
      </div>
      <div class="table-wrap"><table>
        <thead><tr>
          <th>booking</th><th>Гость</th><th>telegram_id</th><th>Телефон</th>
          <th>Гости</th><th>Уже слали</th><th></th>
        </tr></thead>
        <tbody>{body}</tbody>
      </table></div>
    </section>
    """

    def _archive_block(title: str, items: list[dict], persist_key: str, hint: str) -> str:
        if not items:
            return ""
        rows_html = "".join(
            "<tr>"
            f'<td><label><input type="checkbox" name="e_restore" value="{_h(e.get("id"))}"> '
            f'{_h(e.get("id"))}</label></td>'
            f"<td>{_h(e.get('date_display'))}</td>"
            f"<td>{_h(e.get('time'))}</td>"
            f"<td>{_h(e.get('location'))}</td>"
            f"<td>{_h(e.get('address'))}</td>"
            f"<td>{_h(e.get('max_seats'))}</td>"
            f"<td>{_h(e.get('price'))}</td>"
            "</tr>"
            for e in items
        )
        return (
            f'<section class="card details-card analytics-section events-past">'
            f'<details data-persist-key="{_h(persist_key)}">'
            '<summary class="details-summary"><div>'
            f"<strong>{_h(title)} ({len(items)})</strong>"
            f'<span class="muted">{_h(hint)}</span>'
            "</div>"
            '<span class="details-action"><span class="closed-label">Развернуть</span>'
            '<span class="open-label">Свернуть</span></span></summary>'
            '<div class="details-body">'
            f'<form method="post" action="/admin/events/restore">'
            f'<input type="hidden" name="ef" value="{_h(fmt)}">'
            '<div class="events-toolbar">'
            '<button type="submit">Вернуть выбранные в афишу</button>'
            "</div>"
            '<div class="table-wrap"><table><thead><tr>'
            "<th>Вернуть · ID</th><th>Дата</th><th>Время</th><th>Площадка</th>"
            "<th>Адрес</th><th>Мест</th><th>Цена</th>"
            f"</tr></thead><tbody>{rows_html}</tbody></table></div>"
            "</form></div></details></section>"
        )

    past_block = _archive_block(
        "Прошедшие",
        bundle.get("past") or [],
        f"events:past:{fmt}",
        "Дата уже прошла · можно вернуть, если поправите дату в актуальных после возврата",
    )
    hidden_block = _archive_block(
        "Скрытые",
        bundle.get("hidden") or [],
        f"events:hidden:{fmt}",
        "Убраны из бота · отметьте и нажмите «Вернуть»",
    )

    return f"""
    <div class="filters events-filters">
      <div class="events-subtabs">{sub}</div>
    </div>
    {flash_html}{err_html}
    {tickets_panel}
    <section class="card analytics-section">
      <h2>Афиша · {_h(AFISHA_FORMAT_LABELS.get(fmt, fmt))}</h2>
      {note}
      <form method="post" action="/admin/events/save" class="events-form">
        <input type="hidden" name="ef" value="{_h(fmt)}">
        <div class="events-toolbar">
          <button type="submit">Сохранить / обновить</button>
          <a class="pill" href="{_events_link(fmt)}">Отменить правки</a>
          <span class="muted">Актуальных: <b>{len(bundle.get("active") or [])}</b></span>
        </div>
        {_table(bundle.get("active") or [], paid=paid, blank_rows=3, fmt=fmt, show_tickets=can_resend_tickets)}
        <div class="events-toolbar events-toolbar-bottom">
          <button type="submit">Сохранить / обновить</button>
        </div>
      </form>
    </section>
    {hidden_block}
    {past_block}
    """
