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


def _row_html(event: dict | None, paid: bool, blank: bool = False) -> str:
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

    delete_cell = (
        f'<td class="events-del"><label><input type="checkbox" name="e_delete" value="{_h(eid)}"> скрыть</label></td>'
        if eid
        else '<td class="muted">новая</td>'
    )
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


def _table(events: list[dict], paid: bool, blank_rows: int = 2) -> str:
    head_paid = "<th>Цена</th><th>Оплата</th><th>Состав</th>" if paid else "<th></th><th></th><th></th>"
    body = "".join(_row_html(e, paid) for e in events)
    body += "".join(_row_html(None, paid, blank=True) for _ in range(blank_rows))
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
) -> str:
    fmt = event_format if event_format in AFISHA_FORMATS else "best"
    bundle = bundle or {"active": [], "past": []}
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

    note = (
        '<p class="muted">BEST — платные шоу; даты отсюда же использует розыгрыш. '
        "Пустые нижние строки — для быстрого добавления. "
        "«Скрыть» убирает шоу из бота (не удаляет из БД).</p>"
        if fmt == "best"
        else '<p class="muted">Пустые нижние строки — чтобы быстро добавить шоу. '
        "Галочка «скрыть» убирает из бота.</p>"
    )

    past = bundle.get("past") or []
    past_block = ""
    if past:
        past_rows = "".join(
            "<tr>"
            f"<td>{_h(e.get('id'))}</td>"
            f"<td>{_h(e.get('date_display'))}</td>"
            f"<td>{_h(e.get('time'))}</td>"
            f"<td>{_h(e.get('location'))}</td>"
            f"<td>{_h(e.get('address'))}</td>"
            f"<td>{_h(e.get('max_seats'))}</td>"
            f"<td>{_h(e.get('price'))}</td>"
            "</tr>"
            for e in past
        )
        past_block = (
            '<section class="card details-card analytics-section events-past">'
            '<details data-persist-key="events:past">'
            '<summary class="details-summary"><div>'
            f"<strong>Прошедшие ({len(past)})</strong>"
            '<span class="muted">Свёрнуты · дата уже прошла</span>'
            "</div>"
            '<span class="details-action"><span class="closed-label">Развернуть</span>'
            '<span class="open-label">Свернуть</span></span></summary>'
            '<div class="details-body">'
            '<div class="table-wrap"><table><thead><tr>'
            "<th>ID</th><th>Дата</th><th>Время</th><th>Площадка</th><th>Адрес</th><th>Мест</th><th>Цена</th>"
            f"</tr></thead><tbody>{past_rows}</tbody></table></div>"
            '<p class="muted">Чтобы вернуть шоу в афишу — поставьте будущую дату в актуальных '
            "(добавьте заново) или напишите разработчику.</p>"
            "</div></details></section>"
        )

    return f"""
    <div class="filters events-filters">
      <div class="events-subtabs">{sub}</div>
    </div>
    {flash_html}{err_html}
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
        {_table(bundle.get("active") or [], paid=paid, blank_rows=3)}
        <div class="events-toolbar events-toolbar-bottom">
          <button type="submit">Сохранить / обновить</button>
        </div>
      </form>
    </section>
    {past_block}
    """
