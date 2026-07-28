"""HTML + form parsing for admin «Мероприятия» tab."""

from __future__ import annotations

from urllib.parse import urlencode

from bot.db.events_admin import AFISHA_FORMAT_LABELS, AFISHA_FORMATS

TIME_PRESETS = ("19:00", "19:30", "20:00")
LOCATION_PRESETS = (
    ("Escobar", "ESCOBAR, м. Площадь Ильича, ул. Сергия Радонежского, 15-17с17"),
    ("Temple Bar", "Temple Bar, Москва"),
)


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
    purges = set(all_vals("e_purge"))

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
                "purge": raw_id in purges or str(event_id) in purges,
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
    show_seats: bool = False,
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
            f'<td class="events-col-price"><input name="e_price" type="number" min="0" step="1" value="{_h(price)}" placeholder="0"></td>'
            f'<td class="events-col-url"><input class="events-grow" name="e_payment" value="{_h(pay)}" placeholder="https://…" title="{_h(pay)}"></td>'
            f'<td class="events-col-host"><input class="events-grow" name="e_host" value="{_h(host)}" placeholder="Кто в составе" title="{_h(host)}"></td>'
        )
    else:
        paid_cells = (
            '<input type="hidden" name="e_price" value="0">'
            '<input type="hidden" name="e_payment" value="">'
            '<input type="hidden" name="e_host" value="">'
        )

    seats_cell = ""
    if show_seats:
        seats_cell = (
            f'<td class="events-col-seats">'
            f'<input name="e_seats" type="number" min="0" value="{_h(seats)}" placeholder="мест"></td>'
        )
    else:
        seats_cell = f'<input type="hidden" name="e_seats" value="{_h(seats or "0")}">'

    if eid:
        tickets_link = ""
        if show_tickets:
            tickets_link = (
                f'<a class="pill events-tickets-link" '
                f'href="{_events_link(fmt, tickets=eid)}">билеты</a>'
            )
        del_class = "events-del" + (" events-del-with-tickets" if show_tickets else "")
        delete_cell = (
            f'<td class="{del_class}">'
            f'<label><input type="checkbox" name="e_delete" value="{_h(eid)}"> скрыть</label>'
            f'<label class="events-purge"><input type="checkbox" name="e_purge" value="{_h(eid)}"> удалить</label>'
            f"{tickets_link}"
            f"</td>"
        )
    else:
        delete_cell = '<td class="muted">новая</td>'

    # datalist once per row is redundant; keep one global in table footer via first row only — use shared ids
    loc_presets = "".join(
        f'<button type="button" class="events-tpl" data-events-tpl="location" '
        f'data-location="{_h(name)}" data-address="{_h(address)}">{_h(name)}</button>'
        for name, address in LOCATION_PRESETS
    )
    time_presets = "".join(
        f'<button type="button" class="events-tpl" data-events-tpl="time" data-value="{_h(t)}">{_h(t)}</button>'
        for t in TIME_PRESETS
    )

    return (
        "<tr" + (' class="events-row-new"' if blank else "") + ">"
        f'<td class="events-id"><input type="hidden" name="e_id" value="{_h(eid)}">'
        f'<span class="muted">{_h(eid or "—")}</span>'
        f'<div class="muted events-weekday">{_h(weekday)}</div></td>'
        f'<td class="events-col-date"><input name="e_date" type="date" value="{_h(date_val)}"></td>'
        f'<td class="events-col-time events-time-cell">'
        f'<input name="e_time" type="time" value="{_h(time_val)}" list="events-time-presets">'
        f'<div class="events-tpls">{time_presets}</div></td>'
        f'<td class="events-col-loc events-loc-cell">'
        f'<input name="e_location" value="{_h(loc)}" placeholder="Площадка" list="events-location-presets">'
        f'<div class="events-tpls">{loc_presets}</div></td>'
        f'<td class="events-col-addr"><input class="events-grow" name="e_address" value="{_h(addr)}" placeholder="Адрес" title="{_h(addr)}"></td>'
        f"{seats_cell}"
        f"{paid_cells}"
        f'<td class="events-col-desc"><input class="events-grow" name="e_description" value="{_h(desc)}" placeholder="Описание" title="{_h(desc)}"></td>'
        f'<td class="events-col-url"><input class="events-grow" name="e_image" value="{_h(image)}" placeholder="URL картинки" title="{_h(image)}"></td>'
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
    show_seats: bool = False,
) -> str:
    head_paid = (
        '<th class="events-col-price">Цена</th>'
        '<th class="events-col-url">Оплата</th>'
        '<th class="events-col-host">Состав</th>'
        if paid
        else ""
    )
    head_seats = '<th class="events-col-seats">Мест</th>' if show_seats else ""
    blanks = "".join(
        _row_html(
            None, paid, blank=True, fmt=fmt, show_tickets=show_tickets, show_seats=show_seats
        )
        for _ in range(blank_rows)
    )
    body = blanks + "".join(
        _row_html(e, paid, fmt=fmt, show_tickets=show_tickets, show_seats=show_seats)
        for e in events
    )
    time_opts = "".join(f'<option value="{_h(t)}">' for t in TIME_PRESETS)
    loc_opts = "".join(f'<option value="{_h(name)}">' for name, _ in LOCATION_PRESETS)
    return (
        '<datalist id="events-time-presets">'
        f"{time_opts}</datalist>"
        '<datalist id="events-location-presets">'
        f"{loc_opts}</datalist>"
        '<div class="events-table-scroll">'
        '<div class="events-table-scroll-top" aria-hidden="true">'
        '<div class="events-table-scroll-top-inner"></div>'
        "</div>"
        '<div class="table-wrap events-table-wrap"><table class="events-edit">'
        "<thead><tr>"
        '<th class="events-id">ID</th>'
        '<th class="events-col-date">Дата</th>'
        '<th class="events-col-time">Время</th>'
        '<th class="events-col-loc">Площадка</th>'
        '<th class="events-col-addr">Адрес</th>'
        f"{head_seats}"
        f"{head_paid}"
        '<th class="events-col-desc">Описание</th>'
        '<th class="events-col-url">Картинка</th>'
        '<th class="events-del"></th>'
        "</tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
        "</div>"
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
    show_seats = fmt == "proverka"
    # Hit Loto: no per-row «билеты»; Update still available for all formats.
    show_tickets = can_resend_tickets and fmt in {"best", "proverka"}
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

    always_update = (
        '<p class="events-must-update">'
        "<b>Важно:</b> всегда нажимайте «Обновить», чтобы отправить применённые изменения в бот."
        "</p>"
    )
    if fmt == "best":
        note = (
            "<p class=\"muted\">BEST — платные шоу; даты отсюда же использует розыгрыш. "
            "Пустые <b>верхние</b> строки — для быстрого добавления (после «Обновить» дата встанет в общий список).<br>"
            "<b>Смена времени или площадки:</b> правьте нужную строку и нажмите «Обновить» — "
            "изменения появятся в боте.<br>"
            "«Скрыть» убирает из бота; «удалить» — насовсем (только если нет броней). "
            "Вернуть скрытые — в блоке «Скрытые».</p>"
            f"{always_update}"
        )
    else:
        note = (
            "<p class=\"muted\">Пустые <b>верхние</b> строки — быстро добавить шоу "
            "(после «Обновить» попадёт в общий список).<br>"
            "<b>Смена времени или площадки:</b> правьте нужную строку и нажмите «Обновить» — "
            "изменения появятся в боте.<br>"
            "«Скрыть» убирает из бота; «удалить» — насовсем (только если нет броней). "
            "Вернуть скрытые — в блоке «Скрытые».</p>"
            f"{always_update}"
        )

    holders = ticket_holders or []
    tickets_panel = ""
    if show_tickets and tickets_event_id:
        rows = []
        for h in holders:
            tid = h.get("telegram_id")
            vid = h.get("vk_id")
            source = (h.get("booking_source") or "").strip().lower()
            if source in {"vk", "vkontakte"} or (vid and not tid):
                channel = f"VK {_h(vid)}" if vid else "VK"
            elif tid:
                channel = f"TG {_h(tid)}"
            else:
                channel = "—"
            uname = f"@{h.get('username')}" if h.get("username") else "—"
            got = "да" if h.get("has_ticket_msg") else "нет msg id"
            is_raffle = (h.get("booking_format") or "") == "rozygrysh"
            guest = _h(h.get("name") or "—")
            if is_raffle:
                guest += ' <span class="badge events-raffle-badge">розыгрыш</span>'
            rows.append(
                "<tr>"
                f"<td>{_h(h.get('booking_id'))}</td>"
                f"<td>{guest}<br><span class='muted'>{_h(uname)}</span></td>"
                f"<td>{channel}</td>"
                f"<td>{_h(h.get('phone') or '—')}</td>"
                f"<td>{_h(h.get('guests'))}</td>"
                f"<td>{_h(got)}</td>"
                f"<td>"
                f'<form method="post" action="/admin/events/resend-ticket" class="inline-form ticket-resend-one">'
                f'<input type="hidden" name="ef" value="{_h(fmt)}">'
                f'<input type="hidden" name="tickets" value="{_h(tickets_event_id)}">'
                f'<input type="hidden" name="booking_id" value="{_h(h.get("booking_id"))}">'
                f'<input type="hidden" name="updated" value="1">'
                f'<input type="hidden" name="extra_note" value="">'
                '<button type="submit">Переотправить</button>'
                "</form></td>"
                "</tr>"
            )
        body = "".join(rows) or '<tr><td colspan="7" class="muted">Подтверждённых билетов на это шоу нет</td></tr>'
        tickets_panel = f"""
    <section class="card analytics-section events-tickets-panel">
      <h2>Билеты по шоу #{_h(tickets_event_id)}</h2>
      <p class="muted">Показаны только брони со статусом «подтверждено» (билет получен).
      Метка «розыгрыш» — билет выдан в рамках розыгрыша BEST.
      Переотправка шлёт новый билет с текущими датой/временем/местом из афиши
      (Telegram или VK — по каналу брони).</p>
      <label class="events-note-label" for="ticket-extra-note">
        Сообщение вместе с билетом <span class="muted">(необязательно)</span>
      </label>
      <textarea id="ticket-extra-note" class="events-extra-note" rows="3"
        placeholder="Например: дата и время изменились — вот актуальный билет"></textarea>
      <div class="events-toolbar">
        <form method="post" action="/admin/events/resend-ticket" class="ticket-resend-bulk">
          <input type="hidden" name="ef" value="{_h(fmt)}">
          <input type="hidden" name="tickets" value="{_h(tickets_event_id)}">
          <input type="hidden" name="event_id" value="{_h(tickets_event_id)}">
          <input type="hidden" name="updated" value="1">
          <input type="hidden" name="extra_note" value="">
          <button type="submit" {"disabled" if not holders else ""}>
            Переотправить всем ({len(holders)})
          </button>
        </form>
        <a class="pill" href="{_events_link(fmt)}">Закрыть список</a>
      </div>
      <div class="table-wrap"><table>
        <thead><tr>
          <th>booking</th><th>Гость</th><th>Канал</th><th>Телефон</th>
          <th>Гости</th><th>Уже слали</th><th></th>
        </tr></thead>
        <tbody>{body}</tbody>
      </table></div>
    </section>
    """

    def _archive_block(title: str, items: list[dict], persist_key: str, hint: str) -> str:
        if not items:
            return ""
        seat_th = "<th>Мест</th>" if show_seats else ""
        price_th = "<th>Цена</th>" if paid else ""
        rows_html = []
        for e in items:
            cells = [
                f'<td><label><input type="checkbox" name="e_restore" value="{_h(e.get("id"))}"> '
                f'{_h(e.get("id"))}</label></td>',
                f"<td>{_h(e.get('date_display'))}</td>",
                f"<td>{_h(e.get('time'))}</td>",
                f"<td>{_h(e.get('location'))}</td>",
                f"<td>{_h(e.get('address'))}</td>",
            ]
            if show_seats:
                cells.append(f"<td>{_h(e.get('max_seats'))}</td>")
            if paid:
                cells.append(f"<td>{_h(e.get('price'))}</td>")
            rows_html.append("<tr>" + "".join(cells) + "</tr>")
        rows_html = "".join(rows_html)
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
            f"<th>Адрес</th>{seat_th}{price_th}"
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

    toolbar = (
        f'<div class="events-toolbar">'
        f'<button type="submit" form="events-save-form" class="events-update-btn">Обновить</button>'
        f'<a class="pill" href="{_events_link(fmt)}">Отменить правки</a>'
        f'<span class="muted">Актуальных: <b>{len(bundle.get("active") or [])}</b></span>'
        f"</div>"
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
      <p class="events-edit-hint" id="events-edit-hint" hidden>
        <b>Вы начали редактирование.</b> Чтобы изменения появились в боте, нажмите синюю кнопку «Обновить».
      </p>
      {toolbar}
      <form method="post" action="/admin/events/save" class="events-form" id="events-save-form" data-events-draft-key="events-draft:{_h(fmt)}">
        <input type="hidden" name="ef" value="{_h(fmt)}">
        {_table(bundle.get("active") or [], paid=paid, blank_rows=5, fmt=fmt, show_tickets=show_tickets, show_seats=show_seats)}
        {"" if not can_resend_tickets else '''
        <div class="events-notify-box">
          <b>Сообщение гостям при скрытии / удалении</b>
          <span class="muted">Необязательно. Уйдёт только по строкам, где отмечено «скрыть» или «удалить».</span>
          <textarea name="notify_message" rows="3"
            placeholder="Например: внимание, это мероприятие отменено. Приносим извинения."></textarea>
          <div class="events-notify-audience">
            <label><input type="radio" name="notify_audience" value="" checked> не отправлять</label>
            <label><input type="radio" name="notify_audience" value="booked"> всем с активной бронью</label>
            <label><input type="radio" name="notify_audience" value="confirmed"> только у кого есть билет</label>
            <label><input type="radio" name="notify_audience" value="both"> бронь + билет</label>
          </div>
        </div>
        '''}
      </form>
      <div class="events-toolbar events-toolbar-bottom">
        <button type="submit" form="events-save-form" class="events-update-btn">Обновить</button>
        <a class="pill" href="{_events_link(fmt)}">Отменить правки</a>
        <span class="muted">Актуальных: <b>{len(bundle.get("active") or [])}</b></span>
      </div>
    </section>
    {hidden_block}
    {past_block}
    <script>
    (function () {{
      var COMPACT = {{
        e_address: 200,
        e_payment: 180,
        e_host: 160,
        e_description: 150,
        e_image: 150
      }};
      var EXPAND_MAX = 720;
      var editHint = document.getElementById("events-edit-hint");
      function showEditHint() {{
        if (editHint) editHint.hidden = false;
      }}
      function syncTitle(el) {{
        if (!el || el.tagName !== "INPUT") return;
        el.title = el.value || el.placeholder || "";
      }}
      function measure(el) {{
        var style = window.getComputedStyle(el);
        var canvas = measure._c || (measure._c = document.createElement("canvas"));
        var ctx = canvas.getContext("2d");
        ctx.font = style.font || "13px sans-serif";
        var text = el.value || el.placeholder || "";
        return Math.ceil(ctx.measureText(text).width + 36);
      }}
      function fitGrow(el, expanded) {{
        if (!el || !el.classList.contains("events-grow")) return;
        var compact = COMPACT[el.name] || 140;
        if (!expanded) {{
          el.style.maxWidth = "100%";
          el.style.width = compact + "px";
          el.classList.remove("events-grow-open");
          return;
        }}
        var w = measure(el);
        el.classList.add("events-grow-open");
        el.style.maxWidth = "none";
        el.style.width = Math.max(compact, Math.min(EXPAND_MAX, w)) + "px";
      }}
      function collectDraft(form) {{
        var rows = [];
        form.querySelectorAll("tbody tr").forEach(function (tr) {{
          var get = function (name) {{
            var el = tr.querySelector('[name="' + name + '"]');
            return el ? el.value : "";
          }};
          var delEl = tr.querySelector('input[name="e_delete"]');
          var purEl = tr.querySelector('input[name="e_purge"]');
          rows.push({{
            id: get("e_id"),
            date: get("e_date"),
            time: get("e_time"),
            location: get("e_location"),
            address: get("e_address"),
            seats: get("e_seats"),
            price: get("e_price"),
            payment: get("e_payment"),
            host: get("e_host"),
            description: get("e_description"),
            image: get("e_image"),
            delete: !!(delEl && delEl.checked),
            purge: !!(purEl && purEl.checked)
          }});
        }});
        var notify = form.querySelector('[name="notify_message"]');
        var audience = form.querySelector('input[name="notify_audience"]:checked');
        return {{
          rows: rows,
          notify_message: notify ? notify.value : "",
          notify_audience: audience ? audience.value : ""
        }};
      }}
      function applyDraft(form, draft) {{
        if (!draft || !draft.rows) return;
        var trs = form.querySelectorAll("tbody tr");
        draft.rows.forEach(function (row, idx) {{
          var tr = trs[idx];
          if (!tr) return;
          var set = function (name, val) {{
            var el = tr.querySelector('[name="' + name + '"]');
            if (el && val != null) el.value = val;
          }};
          set("e_date", row.date);
          set("e_time", row.time);
          set("e_location", row.location);
          set("e_address", row.address);
          set("e_seats", row.seats);
          set("e_price", row.price);
          set("e_payment", row.payment);
          set("e_host", row.host);
          set("e_description", row.description);
          set("e_image", row.image);
          var del = tr.querySelector('input[name="e_delete"]');
          var pur = tr.querySelector('input[name="e_purge"]');
          if (del) del.checked = !!row.delete;
          if (pur) pur.checked = !!row.purge;
        }});
        var notify = form.querySelector('[name="notify_message"]');
        if (notify && draft.notify_message != null) notify.value = draft.notify_message;
        if (draft.notify_audience) {{
          var rad = form.querySelector('input[name="notify_audience"][value="' + draft.notify_audience + '"]');
          if (rad) rad.checked = true;
        }}
        showEditHint();
      }}
      function saveDraft(form) {{
        var key = form.getAttribute("data-events-draft-key");
        if (!key) return;
        try {{
          localStorage.setItem(key, JSON.stringify(collectDraft(form)));
        }} catch (e) {{}}
      }}
      function clearDraft(form) {{
        var key = form.getAttribute("data-events-draft-key");
        if (!key) return;
        try {{ localStorage.removeItem(key); }} catch (e) {{}}
      }}
      function loadDraft(form) {{
        var key = form.getAttribute("data-events-draft-key");
        if (!key) return;
        try {{
          var raw = localStorage.getItem(key);
          if (!raw) return;
          applyDraft(form, JSON.parse(raw));
        }} catch (e) {{}}
      }}

      document.querySelectorAll(".events-form").forEach(function (form) {{
        form.querySelectorAll("input.events-grow").forEach(function (el) {{
          syncTitle(el);
          fitGrow(el, false);
        }});
        loadDraft(form);
        form.querySelectorAll("input.events-grow").forEach(function (el) {{
          syncTitle(el);
          fitGrow(el, false);
        }});
        form.querySelectorAll(".events-table-scroll").forEach(function (root) {{
          var top = root.querySelector(".events-table-scroll-top");
          var inner = root.querySelector(".events-table-scroll-top-inner");
          var bottom = root.querySelector(".events-table-wrap");
          var table = bottom ? bottom.querySelector("table") : null;
          if (!top || !inner || !bottom || !table) return;
          var syncing = false;
          function syncWidth() {{
            inner.style.width = table.scrollWidth + "px";
            top.style.display = table.scrollWidth > bottom.clientWidth + 1 ? "" : "none";
          }}
          function onTopScroll() {{
            if (syncing) return;
            syncing = true;
            bottom.scrollLeft = top.scrollLeft;
            syncing = false;
          }}
          function onBottomScroll() {{
            if (syncing) return;
            syncing = true;
            top.scrollLeft = bottom.scrollLeft;
            syncing = false;
          }}
          top.addEventListener("scroll", onTopScroll, {{ passive: true }});
          bottom.addEventListener("scroll", onBottomScroll, {{ passive: true }});
          syncWidth();
          if (window.ResizeObserver) {{
            var ro = new ResizeObserver(syncWidth);
            ro.observe(table);
            ro.observe(bottom);
          }} else {{
            window.addEventListener("resize", syncWidth);
          }}
        }});
        form.addEventListener("focusin", function (ev) {{
          var t = ev.target;
          if (!t) return;
          if (t.classList && t.classList.contains("events-grow")) fitGrow(t, true);
          if (t.matches && t.matches("input, textarea, select")) showEditHint();
        }});
        form.addEventListener("focusout", function (ev) {{
          if (ev.target && ev.target.classList.contains("events-grow")) fitGrow(ev.target, false);
        }});
        form.addEventListener("input", function (ev) {{
          showEditHint();
          if (ev.target) {{
            syncTitle(ev.target);
            if (ev.target.classList.contains("events-grow")) fitGrow(ev.target, true);
          }}
          saveDraft(form);
        }});
        form.addEventListener("change", function () {{
          showEditHint();
          saveDraft(form);
        }});
        form.addEventListener("submit", function () {{
          clearDraft(form);
        }});
        form.addEventListener("click", function (ev) {{
          var btn = ev.target.closest("[data-events-tpl]");
          if (!btn || !form.contains(btn)) return;
          ev.preventDefault();
          showEditHint();
          var cell = btn.closest("td");
          if (!cell) return;
          var kind = btn.getAttribute("data-events-tpl");
          if (kind === "time") {{
            var timeInput = cell.querySelector('input[name="e_time"]');
            if (timeInput) timeInput.value = btn.getAttribute("data-value") || "";
          }} else if (kind === "location") {{
            var locInput = cell.querySelector('input[name="e_location"]');
            var row = cell.closest("tr");
            var addrInput = row ? row.querySelector('input[name="e_address"]') : null;
            if (locInput) locInput.value = btn.getAttribute("data-location") || "";
            if (addrInput) {{
              addrInput.value = btn.getAttribute("data-address") || "";
              syncTitle(addrInput);
              fitGrow(addrInput, document.activeElement === addrInput);
            }}
          }}
          saveDraft(form);
        }});
      }});

      document.querySelectorAll('a.pill[href*="tab=events"]').forEach(function (a) {{
        if ((a.textContent || "").indexOf("Отменить") === -1) return;
        a.addEventListener("click", function () {{
          document.querySelectorAll(".events-form").forEach(clearDraft);
        }});
      }});

      var note = document.getElementById("ticket-extra-note");
      function fillTicketNotes() {{
        var text = note ? note.value : "";
        document.querySelectorAll("input[name='extra_note']").forEach(function (el) {{
          el.value = text;
        }});
      }}
      if (note) {{
        note.addEventListener("input", fillTicketNotes);
        document.querySelectorAll(".ticket-resend-one, .ticket-resend-bulk").forEach(function (f) {{
          f.addEventListener("submit", fillTicketNotes);
        }});
      }}
    }})();
    </script>
    """
