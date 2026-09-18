"""Admin UI for mailing campaigns."""

from __future__ import annotations

import json

from bot.db.mailing import (
    estimate_duration_sec,
    format_admin_datetime,
    format_duration,
    is_campaign_scheduled,
    list_campaigns,
    list_mailing_best_shows,
    list_mailing_templates,
    remainder_after_limit,
    to_datetime_local_value,
)


def _h(value) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


STATUS_LABELS = {
    "draft": "черновик",
    "queued": "в очереди",
    "running": "идёт",
    "paused": "пауза",
    "done": "готово",
    "cancelled": "отменена",
}
CHANNEL_LABELS = {
    "telegram": "телеграм",
    "vkontakte": "вк",
    "both": "оба",
}


def _channel_label(value) -> str:
    raw = str(value or "").strip()
    return CHANNEL_LABELS.get(raw, raw)


def render_mailing_tab(
    *,
    flash: str = "",
    error: str = "",
    can_send: bool = False,
    campaigns: list[dict] | None = None,
    detail: dict | None = None,
    recipients: dict | None = None,
) -> str:
    if not can_send:
        return (
            '<section class="card empty-state">'
            "<h2>Рассылка</h2>"
            '<p class="muted">Доступно только владельцу (owner).</p>'
            "</section>"
        )

    campaigns = campaigns if campaigns is not None else list_campaigns()
    flash_html = f'<p class="events-flash">{_h(flash)}</p>' if flash else ""
    error_html = f'<p class="events-error">{_h(error)}</p>' if error else ""

    rows = []
    for c in campaigns:
        cid = c.get("id")
        total = int(c.get("total_count") or 0)
        sent = int(c.get("sent_count") or 0)
        failed = int(c.get("failed_count") or 0)
        status = c.get("status") or ""
        interval = float(c.get("interval_sec") or 0)
        left = max(0, total - sent - failed - int(c.get("skipped_count") or 0))
        eta = format_duration(estimate_duration_sec(left, interval)) if status == "running" else "—"
        status_label = STATUS_LABELS.get(status, status)
        when_label = format_admin_datetime(c.get("scheduled_at")) or "—"
        if is_campaign_scheduled(c):
            status_label = "запланирована"
        actions = []
        actions.append(
            f'<a class="pill" href="/admin?tab=mailing&campaign={cid}">Текст и статистика</a>'
        )
        if status in ("queued", "paused"):
            local_val = to_datetime_local_value(c.get("scheduled_at"))
            actions.append(
                f'<form method="post" action="/admin/mailing/schedule" class="inline-form mailing-reschedule">'
                f'<input type="hidden" name="campaign_id" value="{cid}">'
                f'<input type="datetime-local" name="scheduled_at" value="{_h(local_val)}" required>'
                f'<button type="submit">Перенести</button></form>'
            )
            actions.append(
                f'<form method="post" action="/admin/mailing/schedule" class="inline-form">'
                f'<input type="hidden" name="campaign_id" value="{cid}">'
                f'<input type="hidden" name="send_now" value="1">'
                f'<button type="submit" onclick="return confirm('
                f"'Отправить рассылку #{cid} сейчас?');\">Сейчас</button></form>"
            )
        if status in ("queued", "running"):
            actions.append(
                f'<form method="post" action="/admin/mailing/status" class="inline-form">'
                f'<input type="hidden" name="campaign_id" value="{cid}">'
                f'<input type="hidden" name="status" value="paused">'
                f'<button type="submit">Пауза</button></form>'
            )
        if status == "paused":
            actions.append(
                f'<form method="post" action="/admin/mailing/status" class="inline-form">'
                f'<input type="hidden" name="campaign_id" value="{cid}">'
                f'<input type="hidden" name="status" value="queued">'
                f'<button type="submit">Продолжить</button></form>'
            )
        if status in ("queued", "running", "paused"):
            actions.append(
                f'<form method="post" action="/admin/mailing/status" class="inline-form">'
                f'<input type="hidden" name="campaign_id" value="{cid}">'
                f'<input type="hidden" name="status" value="cancelled">'
                f'<button type="submit" onclick="return confirm('
                f"'Остановить рассылку #{cid}?');\">Стоп</button></form>"
            )
        rows.append(
            "<tr>"
            f"<td data-label=\"id\">{cid}</td>"
            f"<td data-label=\"Название\">{_h(c.get('title'))}<br>"
            f"<span class='muted'>{_h(_channel_label(c.get('channel')))}</span></td>"
            f"<td data-label=\"Статус / время\">{_h(status_label)}"
            f"<br><span class='muted'>{_h(when_label)}</span></td>"
            f"<td data-label=\"Прогресс\">{sent}/{total}"
            f"<br><span class='muted'>ошибки {failed}</span></td>"
            f"<td data-label=\"Осталось ≈\">{_h(eta)}</td>"
            f"<td class='mailing-actions'>{''.join(actions)}</td>"
            "</tr>"
        )

    history = (
        '<section class="card mailing-history">'
        '<details data-persist-key="mailing:history">'
        "<summary><strong>История рассылок</strong>"
        '<span class="details-action"><span class="closed-label">Развернуть</span>'
        '<span class="open-label">Свернуть</span></span></summary>'
        '<div class="table-wrap"><table class="users">'
        "<thead><tr><th>id</th><th>Название</th><th>Статус / время</th>"
        "<th>Прогресс</th><th>Осталось ≈</th><th></th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=\"6\" class=\"muted\">Пока пусто</td></tr>'}</tbody>"
        "</table></div>"
        "</details>"
        "</section>"
    )

    detail_html = ""
    if detail and recipients is not None:
        cid = detail.get("id")
        rstatus = (recipients.get("filter_status") or "") if isinstance(recipients, dict) else ""
        # recipients is dict from list_recipients + maybe filter_status
        page = int(recipients.get("page") or 1)
        pages = int(recipients.get("pages") or 1)
        total = int(recipients.get("total") or 0)
        prev = (
            f'<a class="pill" href="/admin?tab=mailing&campaign={cid}&rpage={page-1}&rstatus={_h(rstatus)}">←</a>'
            if page > 1
            else ""
        )
        next_ = (
            f'<a class="pill" href="/admin?tab=mailing&campaign={cid}&rpage={page+1}&rstatus={_h(rstatus)}">→</a>'
            if page < pages
            else ""
        )
        status_pills = []
        for key, label in (
            ("", "Все"),
            ("pending", "Ждут"),
            ("sent", "Отправлено"),
            ("failed", "Ошибки"),
        ):
            active = "active" if rstatus == key else ""
            status_pills.append(
                f'<a class="pill {active}" href="/admin?tab=mailing&campaign={cid}&rstatus={key}">'
                f"{label}</a>"
            )
        rrows = []
        for r in recipients.get("rows") or []:
            uname = (r.get("username") or "").strip()
            contact = f"@{_h(uname)}" if uname else _h(r.get("phone") or "—")
            rrows.append(
                "<tr>"
                f"<td>{_h(r.get('user_id'))}</td>"
                f"<td>{_h(r.get('name') or '—')}<br><span class='muted'>{contact}</span></td>"
                f"<td>{_h(r.get('channel'))} · {_h(r.get('peer_id'))}</td>"
                f"<td>{_h(r.get('status'))}</td>"
                f"<td class='muted'>{_h(r.get('error') or '')}</td>"
                "</tr>"
            )
        sent = int(detail.get("sent_count") or 0)
        failed = int(detail.get("failed_count") or 0)
        total_c = int(detail.get("total_count") or 0)
        rem = {"count": 0}
        try:
            rem = remainder_after_limit(int(cid))
        except Exception:
            rem = {"count": 0}
        rem_n = int(rem.get("count") or 0)
        rem_form = ""
        if rem_n > 0 and (detail.get("status") or "") in ("done", "cancelled", "paused"):
            full_id = rem.get("full_id")
            rem_form = (
                '<form method="post" action="/admin/mailing/retry-remainder" '
                'class="inline-form" style="margin:12px 0">'
                f'<input type="hidden" name="campaign_id" value="{int(cid)}">'
                f'<button type="submit" onclick="return confirm('
                f"'Запустить досылку на {rem_n} человек из очереди #{full_id}? "
                f"Кто уже был в #{int(cid)}, не получат повторно.');\">"
                f"Дослать оставшимся из #{full_id} ({rem_n} чел.)</button>"
                "<p class='muted' style='margin:8px 0 0'>"
                "Берёт людей из полной очереди до лимита и вычитает тех, кто уже попал в эту рассылку. "
                "Текст, картинка и кнопка — как в этом письме. Живые фильтры «заблокировал / уже слали» не используются."
                "</p></form>"
            )
        btn = (detail.get("button_text") or "").strip()
        btn_url = (detail.get("button_url") or "").strip()
        follow = (detail.get("followup_html") or "").strip()
        from bot.db.mailing import followup_is_booking_flow, followup_preview_label
        body = detail.get("body_html") or ""
        preview_off = bool(detail.get("disable_link_preview"))
        until_raw = detail.get("followup_until")
        until_val = ""
        if until_raw:
            until_val = str(until_raw)[:10]
        msg_meta = (
            f"<p class='muted'>Кнопка: {_h(btn) or '—'} · "
            f"ссылка: {_h(btn_url) or 'нет'} · "
            f"превью ссылок: {'выкл' if preview_off else 'вкл'} · "
            f"кнопка до: {_h(until_val) or 'не задано'}</p>"
        )
        follow_block = ""
        if follow:
            if followup_is_booking_flow(follow):
                follow_block = (
                    f"<p><b>После кнопки:</b> {_h(followup_preview_label(follow))}</p>"
                )
            else:
                follow_block = (
                    f"<p><b>После кнопки:</b></p>"
                    f"<div class='mailing-msg-preview'>{follow}</div>"
                )
        until_form = ""
        if follow:
            until_form = (
                '<form method="post" action="/admin/mailing/followup-until" class="inline-form" '
                'style="margin:12px 0;display:flex;flex-wrap:wrap;gap:8px;align-items:end">'
                f'<input type="hidden" name="campaign_id" value="{int(cid)}">'
                "<label>Кнопка актуальна до (дата шоу)"
                f'<input type="date" name="followup_until" value="{_h(until_val)}"></label>'
                '<button type="submit">Сохранить дату</button>'
                "<p class='muted' style='margin:0;flex-basis:100%'>"
                "После этой даты по кнопке уйдёт текст «мероприятие уже неактуально».</p>"
                "</form>"
            )
        detail_html = (
            '<section class="card mailing-detail">'
            f"<h2>Кампания #{_h(cid)} · {_h(detail.get('title'))}</h2>"
            f"<p class='muted'>Статус: {_h(STATUS_LABELS.get(detail.get('status'), detail.get('status')))} · "
            f"канал {_h(_channel_label(detail.get('channel')))} · интервал {_h(detail.get('interval_sec'))} сек"
            f"{(' · план ' + format_admin_datetime(detail.get('scheduled_at'))) if detail.get('scheduled_at') else ''}</p>"
            f"<p><b>Статистика:</b> отправлено {sent} / {total_c} · ошибки {failed}</p>"
            f"{rem_form}"
            "<p><b>Текст сообщения:</b></p>"
            f"<div class='mailing-msg-preview'>{body or '<span class=\"muted\">(пусто)</span>'}</div>"
            f"{msg_meta}{follow_block}{until_form}"
            f'<div class="counters">{"".join(status_pills)}</div>'
            f'<div class="users-pager">{prev}'
            f'<span class="muted">стр. {page}/{pages} · {total}</span>{next_}</div>'
            '<div class="table-wrap"><table class="users">'
            "<thead><tr><th>user</th><th>Клиент</th><th>Куда</th><th>Статус</th><th>Ошибка</th></tr></thead>"
            f"<tbody>{''.join(rrows) or '<tr><td colspan=\"5\" class=\"muted\">Пусто</td></tr>'}</tbody>"
            "</table></div>"
            f'<p><a class="pill" href="/admin?tab=mailing">← к списку</a></p>'
            "</section>"
        )

    templates = list_mailing_templates()
    shows = list_mailing_best_shows()
    tpl_json = json.dumps(
        {t["key"]: t for t in templates},
        ensure_ascii=False,
    ).replace("<", "\\u003c")
    shows_json = json.dumps(shows, ensure_ascii=False).replace("<", "\\u003c")
    tpl_opts = ['<option value="">Без шаблона — вставить вручную</option>']
    editor_blocks = []
    for t in templates:
        key = _h(t["key"])
        tpl_opts.append(f'<option value="{key}">{_h(t["title"])}</option>')
        editor_blocks.append(
            '<div class="mailing-tpl-edit">'
            f"<h3>{_h(t['title'])}</h3>"
            "<label>Текст"
            f'<textarea name="tpl_{key}_body" rows="6">{_h(t.get("body_html"))}</textarea></label>'
            "<label>Текст кнопки"
            f'<input type="text" name="tpl_{key}_button" value="{_h(t.get("button_text"))}" maxlength="40"></label>'
            "<label>После кнопки"
            f'<textarea name="tpl_{key}_followup" rows="2">{_h(t.get("followup_html"))}</textarea></label>'
            "</div>"
        )
    show_opts = ['<option value="">Выберите шоу BEST</option>']
    default_show_id = ""
    for item in shows:
        if item.get("is_today"):
            default_show_id = str(item.get("id") or "")
            break
    if not default_show_id and shows:
        default_show_id = str(shows[0].get("id") or "")
    for item in shows:
        sid = str(item.get("id") or "")
        selected = " selected" if sid and sid == default_show_id else ""
        show_opts.append(
            f'<option value="{_h(sid)}"{selected}>{_h(item.get("label"))}</option>'
        )

    form = f"""
<section class="card mailing-compose">
  <h2>Новая рассылка</h2>
  <p class="muted">Можно выделить жирным, курсивом или поставить ссылку. Телеграм и ВК.</p>
  <details data-persist-key="mailing:templates-edit">
    <summary><strong>Редактировать шаблоны текстов</strong>
      <span class="details-action"><span class="closed-label">Развернуть</span>
      <span class="open-label">Свернуть</span></span></summary>
    <p class="muted">Сохраните 4 варианта один раз — потом только выбирайте шаблон в форме.
      В BEST оставьте {'{концерт}'}, {'{когда}'}, {'{время}'}, {'{площадка}'}, {'{адрес}'} — они заполнятся из выбранного шоу.</p>
    <form method="post" action="/admin/mailing/templates" class="mailing-form">
      {''.join(editor_blocks)}
      <div class="mailing-actions">
        <button type="submit">Сохранить шаблоны</button>
      </div>
    </form>
  </details>
  <form method="post" action="/admin/mailing/create" enctype="multipart/form-data" class="mailing-form" id="mailing-form">
    <div class="mailing-grid">
      <label>Шаблон текста
        <select id="mail-template-key">{''.join(tpl_opts)}</select>
      </label>
      <label id="mail-show-wrap" hidden>Шоу для письма
        <select id="mail-show-id" name="mail_show_id">{''.join(show_opts)}</select>
      </label>
      <div class="mailing-actions" style="align-items:end">
        <button type="button" id="mail-template-apply" class="mail-secondary-btn">Подставить шаблон</button>
        <button type="button" id="mail-template-clear" class="mail-secondary-btn">Сбросить текст</button>
      </div>
    </div>
    <p class="muted" id="mail-show-hint" hidden>Время и адрес возьмутся из этого шоу в афише BEST.</p>
    <label>Название
      <input type="text" name="title" placeholder="Анонс пятницы" maxlength="120">
    </label>
    <fieldset class="mailing-row">
      <legend>Канал</legend>
      <label><input type="radio" name="channel" value="telegram" checked> Телеграм</label>
      <label><input type="radio" name="channel" value="vkontakte"> ВК</label>
      <label><input type="radio" name="channel" value="both"> Оба</label>
      <p class="muted" style="margin:6px 0 0">Бронь ВК попадёт в расчёт только если выбран <b>ВК</b> или <b>Оба</b>.</p>
    </fieldset>
    <label>Текст сообщения
      <textarea name="body_html" rows="8" placeholder="Привет! В эту пятницу..."></textarea>
    </label>
    <label>Картинка (необязательно)
      <input type="file" name="photo" accept="image/jpeg,image/png,image/webp">
    </label>
    <div class="mailing-grid">
      <label>Текст кнопки
        <input type="text" name="button_text" maxlength="40" placeholder="Забронировать">
      </label>
      <label>Ссылка кнопки
        <input type="url" name="button_url" placeholder="https://…">
      </label>
    </div>
    <label>После нажатия кнопки (если нет ссылки) — доп. текст
      <textarea name="followup_html" rows="3" placeholder="Отлично! Вот детали..."></textarea>
    </label>
    <label>Кнопка актуальна до (дата шоу)
      <input type="date" name="followup_until">
      <span class="muted">После этой даты по кнопке — «мероприятие уже неактуально». Если пусто, возьмём дату шоу из фильтров ниже.</span>
    </label>
    <fieldset class="mailing-row">
      <legend>Превью ссылок в Телеграме</legend>
      <label><input type="checkbox" name="disable_link_preview" value="1"> Отключить превью ссылки в письме</label>
      <p class="muted" style="margin:6px 0 0">Работает для Телеграма. В ВК превью управляет сам мессенджер.</p>
    </fieldset>
    <div class="mailing-grid">
      <label>Интервал, сек
        <input type="number" name="interval_sec" value="0.1" min="0" max="60" step="0.05" id="mail-interval" lang="en">
      </label>
      <label>Лимит за запуск (напр. 5000)
        <input type="number" name="batch_limit" min="1" max="100000" placeholder="все" lang="en">
      </label>
      <label>Не слать, если уже слали за N дней
        <input type="number" name="exclude_sent_days" value="0" min="0" max="3650" lang="en">
      </label>
    </div>
    <fieldset class="mailing-row">
      <legend>Когда отправить</legend>
      <label><input type="radio" name="send_when" value="now" checked> Сразу</label>
      <label><input type="radio" name="send_when" value="later"> Запланировать</label>
      <label>Дата и время (МСК)
        <input type="datetime-local" name="scheduled_at" id="mail-scheduled-at">
      </label>
      <p class="muted" style="margin:6px 0 0">Аудитория считается сейчас, письма уйдут в выбранное время. Можно отменить или перенести в истории.</p>
    </fieldset>
    <details class="mailing-cut" data-persist-key="mailing:audience-filters">
      <summary><strong>Фильтры аудитории</strong>
        <span class="details-action"><span class="closed-label">Развернуть</span>
        <span class="open-label">Свернуть</span></span></summary>
      <p class="muted">Нужны редко: статус брони, дата шоу, телефон, «не слать у кого сегодня шоу».</p>
      <fieldset class="mailing-row">
        <legend>Фильтры аудитории</legend>
        <label><input type="checkbox" name="exclude_blocked" value="1" checked> Исключить заблокировавших TG-бота</label>
        <label><input type="checkbox" name="has_phone" value="1"> Только с телефоном</label>
        <label><input type="checkbox" name="exclude_today_bookings" value="1"> Не слать тем, у кого сегодня шоу (активная бронь или билет)</label>
      </fieldset>
      <fieldset class="mailing-row">
        <legend>Статус брони</legend>
        <p class="muted" style="margin:0 0 8px">Если ничего не выбрано — вся база канала (без фильтра по броням). Брони <b>розыгрыша</b> в эти статусы не входят.</p>
        <label><input type="checkbox" name="booking_statuses" value="active"> Активная (бронь или билет)</label>
        <label><input type="checkbox" name="booking_statuses" value="booked"> Только бронь без билета</label>
        <label><input type="checkbox" name="booking_statuses" value="confirmed"> Только подтверждённый билет</label>
        <label><input type="checkbox" name="booking_statuses" value="cancelled"> Отмена</label>
        <label><input type="checkbox" name="booking_statuses" value="annulled"> Аннулировано</label>
      </fieldset>
      <fieldset class="mailing-row">
        <legend>Дата шоу для статуса выше</legend>
        <p class="muted" style="margin:0 0 8px">Выберите дату или диапазон дат (день мероприятия в афише). Одна дата = только этот день.</p>
        <div class="mailing-grid">
          <label>Дата
            <input type="date" name="date_from">
          </label>
          <label>По дату (если диапазон)
            <input type="date" name="date_to">
          </label>
        </div>
      </fieldset>
    </details>
    <div class="mailing-preview" id="mail-preview">
      <span class="muted">Нажмите «Посчитать аудиторию», чтобы увидеть число и примерное время.</span>
    </div>
    <div class="mailing-actions">
      <button type="button" id="mail-preview-btn">Посчитать аудиторию</button>
      <button type="button" id="mail-reset-filters-btn" class="mail-secondary-btn">Сбросить фильтры</button>
      <button type="submit" id="mail-submit-btn">Запустить рассылку</button>
    </div>
  </form>
</section>

<section class="card mailing-test">
  <h2>Протестировать</h2>
  <p class="muted">Отправит <b>текущий текст/кнопку/картинку</b> из формы выше одному человеку — без запуска полной рассылки.</p>
  <div class="mailing-grid">
    <label style="grid-column:1/-1">Найти пользователя (имя, @username или телефон)
      <input type="search" id="mail-test-q" placeholder="Например: Ира или @username или 8900…" autocomplete="off">
    </label>
  </div>
  <div id="mail-test-results" class="mail-test-results muted">Введите запрос и нажмите «Найти».</div>
  <input type="hidden" id="mail-test-user-id" value="">
  <div class="mailing-actions" style="margin-top:12px">
    <button type="button" id="mail-test-search-btn">Найти</button>
    <button type="button" id="mail-test-send-btn">Отправить тест выбранному</button>
  </div>
  <div id="mail-test-status" class="mailing-preview" style="margin-top:12px"></div>
</section>
"""

    from bot.admin.raffle_cancel_notify import DEFAULT_CANCEL_TEXT

    raffle_cancel = f"""
<section class="card mailing-raffle-cancel">
  <details data-persist-key="mailing:raffle-cancel">
  <summary><strong>Отмена шоу + даты розыгрыша (Телеграм)</strong>
    <span class="details-action"><span class="closed-label">Развернуть</span>
    <span class="open-label">Свернуть</span></span></summary>
  <p class="muted">
    Сбрасывает блок розыгрыша у гостя, шлёт ваш текст и меню дат <b>BEST</b>
    (без сегодняшней даты) — как в боте розыгрыша. Только Телеграм.
    Можно слать по одному человеку.
  </p>
  <label>Текст сообщения
    <textarea id="rz-cancel-body" rows="4">{_h(DEFAULT_CANCEL_TEXT)}</textarea>
  </label>
  <label style="margin-top:12px">Найти и добавить получателей (имя, @username, телефон)
    <input type="search" id="rz-cancel-q" placeholder="Например: @username" autocomplete="off">
  </label>
  <div class="mailing-actions" style="margin-top:8px">
    <button type="button" id="rz-cancel-search-btn">Найти</button>
  </div>
  <div id="rz-cancel-search-results" class="mail-test-results muted" style="margin-top:8px">Найдите гостей и нажмите «Добавить».</div>
  <div id="rz-cancel-selected" class="rz-cancel-selected" style="margin-top:12px"></div>
  <label class="rz-cancel-reset" style="display:flex;gap:8px;align-items:center;margin-top:12px">
    <input type="checkbox" id="rz-cancel-reset" checked>
    Сбросить флаг/активные брони розыгрыша перед отправкой
  </label>
  <div class="mailing-actions" style="margin-top:12px">
    <button type="button" id="rz-cancel-send-btn">Отправить выбранным</button>
    <button type="button" id="rz-cancel-clear-btn" class="mail-secondary-btn">Очистить список</button>
  </div>
  <div id="rz-cancel-status" class="mailing-preview" style="margin-top:12px"></div>
</section>
<script>
(function(){{
  var qEl = document.getElementById('rz-cancel-q');
  var searchBtn = document.getElementById('rz-cancel-search-btn');
  var searchBox = document.getElementById('rz-cancel-search-results');
  var selectedBox = document.getElementById('rz-cancel-selected');
  var sendBtn = document.getElementById('rz-cancel-send-btn');
  var clearBtn = document.getElementById('rz-cancel-clear-btn');
  var statusEl = document.getElementById('rz-cancel-status');
  var bodyEl = document.getElementById('rz-cancel-body');
  var resetEl = document.getElementById('rz-cancel-reset');
  if (!searchBtn || !sendBtn) return;
  var selected = [];

  function esc(s){{
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }}

  function renderSelected(){{
    if (!selected.length) {{
      selectedBox.innerHTML = '<span class="muted">Список получателей пуст</span>';
      return;
    }}
    var html = '<div class="rz-cancel-chips">';
    for (var i = 0; i < selected.length; i++){{
      var u = selected[i];
      var uname = u.username ? ('@' + u.username) : '';
      html += '<span class="rz-cancel-chip" data-id="' + u.id + '">' +
        '<b>' + esc(u.name || 'Без имени') + '</b>' +
        (uname ? (' · ' + esc(uname)) : '') +
        ' · id ' + u.id +
        ' <button type="button" class="rz-cancel-chip-x" data-id="' + u.id + '" title="Убрать">×</button>' +
        '</span>';
    }}
    html += '</div>';
    selectedBox.innerHTML = html;
    selectedBox.querySelectorAll('.rz-cancel-chip-x').forEach(function(btn){{
      btn.addEventListener('click', function(){{
        var id = parseInt(btn.getAttribute('data-id'), 10);
        selected = selected.filter(function(x){{ return x.id !== id; }});
        renderSelected();
      }});
    }});
  }}

  function addUser(u){{
    if (!u || !u.id) return;
    if (!u.telegram_id) {{
      statusEl.innerHTML = '<span class="events-error">У этого гостя нет id Телеграма</span>';
      return;
    }}
    if (selected.some(function(x){{ return x.id === u.id; }})) return;
    selected.push({{
      id: u.id,
      name: u.name || '',
      username: u.username || '',
      telegram_id: u.telegram_id
    }});
    renderSelected();
  }}

  function renderSearch(rows){{
    if (!rows || !rows.length) {{
      searchBox.innerHTML = '<span class="muted">Никого не найдено</span>';
      return;
    }}
    var html = '<div class="mail-test-list">';
    for (var i = 0; i < rows.length; i++){{
      var u = rows[i];
      var uname = u.username ? ('@' + u.username) : '';
      var disabled = !u.telegram_id;
      html += '<div class="mail-test-item" style="justify-content:space-between">' +
        '<span><b>' + esc(u.name || 'Без имени') + '</b> · id ' + u.id +
        (uname ? (' · ' + esc(uname)) : '') +
        (u.telegram_id ? ' · TG' : ' · <span class="events-error">нет TG</span>') +
        '</span>' +
        '<button type="button" class="rz-cancel-add" data-idx="' + i + '"' +
        (disabled ? ' disabled' : '') + '>Добавить</button></div>';
    }}
    html += '</div>';
    searchBox.innerHTML = html;
    searchBox.querySelectorAll('.rz-cancel-add').forEach(function(btn){{
      btn.addEventListener('click', function(){{
        var idx = parseInt(btn.getAttribute('data-idx'), 10);
        addUser(rows[idx]);
      }});
    }});
  }}

  searchBtn.addEventListener('click', function(){{
    var q = (qEl.value || '').trim();
    if (!q) {{ searchBox.innerHTML = '<span class="events-error">Введите запрос</span>'; return; }}
    searchBox.innerHTML = '<span class="muted">Ищем…</span>';
    fetch('/admin/mailing/users-search?q=' + encodeURIComponent(q) + '&channel=telegram', {{credentials:'same-origin'}})
      .then(function(r){{ return r.json(); }})
      .then(function(d){{
        if (d.error) {{ searchBox.innerHTML = '<span class="events-error">' + esc(d.error) + '</span>'; return; }}
        renderSearch(d.users || []);
      }})
      .catch(function(){{ searchBox.innerHTML = '<span class="events-error">Ошибка поиска</span>'; }});
  }});
  qEl.addEventListener('keydown', function(ev){{
    if (ev.key === 'Enter') {{ ev.preventDefault(); searchBtn.click(); }}
  }});
  clearBtn.addEventListener('click', function(){{ selected = []; renderSelected(); }});
  renderSelected();

  sendBtn.addEventListener('click', function(){{
    var body = (bodyEl.value || '').trim();
    if (!body) {{ statusEl.innerHTML = '<span class="events-error">Введите текст сообщения</span>'; return; }}
    if (!selected.length) {{ statusEl.innerHTML = '<span class="events-error">Добавьте хотя бы одного получателя</span>'; return; }}
    if (!confirm('Отправить уведомление и даты розыгрыша ' + selected.length + ' чел.?')) return;
    statusEl.innerHTML = '<span class="muted">Отправляем…</span>';
    sendBtn.disabled = true;
    fetch('/admin/mailing/raffle-cancel-notify', {{
      method: 'POST',
      credentials: 'same-origin',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        body: body,
        reset_raffle: !!(resetEl && resetEl.checked),
        user_ids: selected.map(function(u){{ return u.id; }})
      }})
    }})
      .then(function(r){{
        return r.text().then(function(raw){{
          var d = {{}};
          try {{ d = raw ? JSON.parse(raw) : {{}}; }}
          catch (e) {{
            throw new Error('Сервер ответил не JSON (HTTP ' + r.status + ')');
          }}
          return {{ok: r.ok, d: d}};
        }});
      }})
      .then(function(res){{
        sendBtn.disabled = false;
        var d = res.d || {{}};
        if (!res.ok || d.error) {{
          statusEl.innerHTML = '<span class="events-error">' + esc(d.error || 'Не удалось отправить') + '</span>';
          return;
        }}
        var lines = ['<b>Готово:</b> успешно ' + (d.ok||0) + ', ошибок ' + (d.fail||0)];
        (d.items || []).forEach(function(it){{
          var who = (it.username ? ('@' + it.username) : '') || it.name || ('id ' + it.user_id);
          if (it.ok) lines.push('✓ ' + esc(who));
          else lines.push('✗ ' + esc(who) + ': ' + esc(it.error || 'ошибка'));
        }});
        statusEl.innerHTML = lines.join('<br>');
      }})
      .catch(function(err){{
        sendBtn.disabled = false;
        statusEl.innerHTML = '<span class="events-error">' + esc(err && err.message ? err.message : 'Ошибка сети') + '</span>';
      }});
  }});
}})();
</script>
  </details>
</section>
"""

    form = (
        form
        + """
<script>
var MAIL_TEMPLATES = """
        + tpl_json
        + """;
var MAIL_BEST_SHOWS = """
        + shows_json
        + """;
(function(){
  var form = document.getElementById('mailing-form');
  var btn = document.getElementById('mail-preview-btn');
  var box = document.getElementById('mail-preview');
  if (!form || !btn || !box) return;
  var STORAGE_KEY = 'admin-mailing-draft-v7';
  try {
    sessionStorage.removeItem('admin-mailing-draft-v1');
    sessionStorage.removeItem('admin-mailing-draft-v2');
    sessionStorage.removeItem('admin-mailing-draft-v3');
    sessionStorage.removeItem('admin-mailing-draft-v4');
    sessionStorage.removeItem('admin-mailing-draft-v5');
    sessionStorage.removeItem('admin-mailing-draft-v6');
  } catch (e) {}

  function saveDraft(){
    var data = {};
    var els = form.querySelectorAll('input, textarea, select');
    for (var i = 0; i < els.length; i++){
      var el = els[i];
      if (!el.name || el.type === 'file') continue;
      if (el.type === 'radio') {
        if (el.checked) data[el.name] = el.value;
        continue;
      }
      if (el.type === 'checkbox') {
        if (!data[el.name]) data[el.name] = [];
        if (el.checked) data[el.name].push(el.value);
        continue;
      }
      data[el.name] = el.value;
    }
    if (box && box.dataset.previewHtml && box.dataset.previewHtml.indexOf('events-error') === -1) {
      data.__preview = box.dataset.previewHtml;
    }
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(data)); } catch (e) {}
  }

  function restoreDraft(){
    var raw;
    try { raw = sessionStorage.getItem(STORAGE_KEY); } catch (e) { return; }
    if (!raw) return;
    var data;
    try { data = JSON.parse(raw); } catch (e) { return; }
    if (!data || typeof data !== 'object') return;
    var els = form.querySelectorAll('input, textarea, select');
    for (var i = 0; i < els.length; i++){
      var el = els[i];
      if (!el.name || el.type === 'file') continue;
      if (el.type === 'radio') {
        el.checked = data[el.name] === el.value;
        continue;
      }
      if (el.type === 'checkbox') {
        var list = data[el.name];
        if (!Array.isArray(list)) list = [];
        el.checked = list.indexOf(el.value) !== -1;
        continue;
      }
      if (Object.prototype.hasOwnProperty.call(data, el.name) && typeof data[el.name] === 'string') {
        el.value = data[el.name];
      }
    }
    // Старое превью не восстанавливаем — легко принять нули за свежий расчёт.
  }

  function fmt(sec){
    sec = Math.round(sec||0);
    if (sec < 60) return sec + ' сек';
    var m = Math.floor(sec/60), s = sec % 60;
    if (m < 60) return m + ' мин' + (s ? (' ' + s + ' сек') : '');
    var h = Math.floor(m/60); m = m % 60;
    return h + ' ч' + (m ? (' ' + m + ' мин') : '');
  }

  function parseInterval(raw){
    var s = String(raw == null ? '0.1' : raw).trim().replace(',', '.');
    var n = parseFloat(s);
    return isFinite(n) ? n : 0.1;
  }

  restoreDraft();
  form.addEventListener('input', saveDraft);
  form.addEventListener('change', saveDraft);

  var tplSel = document.getElementById('mail-template-key');
  var tplBtn = document.getElementById('mail-template-apply');
  var showWrap = document.getElementById('mail-show-wrap');
  var showHint = document.getElementById('mail-show-hint');
  var showSel = document.getElementById('mail-show-id');
  function showById(id){
    var sid = String(id || '');
    for (var i = 0; i < MAIL_BEST_SHOWS.length; i++){
      if (String(MAIL_BEST_SHOWS[i].id) === sid) return MAIL_BEST_SHOWS[i];
    }
    return null;
  }
  function fillSubs(text, subs){
    var out = text || '';
    if (!subs) return out;
    Object.keys(subs).forEach(function(key){
      out = out.split(key).join(subs[key] || '');
    });
    return out;
  }
  function syncShowWrap(){
    var t = MAIL_TEMPLATES[tplSel && tplSel.value];
    var on = !!(t && t.uses_show);
    if (showWrap) showWrap.hidden = !on;
    if (showHint) showHint.hidden = !on;
  }
  if (tplSel) tplSel.addEventListener('change', syncShowWrap);
  syncShowWrap();
  function clearTemplateFields(){
    var body = form.querySelector('[name=body_html]');
    var fu = form.querySelector('[name=followup_html]');
    var title = form.querySelector('[name=title]');
    var btnEl = form.querySelector('[name=button_text]');
    var urlEl = form.querySelector('[name=button_url]');
    var untilEl = form.querySelector('[name=followup_until]');
    var photoEl = form.querySelector('[name=photo]');
    var hasText = !!(
      (body && body.value.trim()) ||
      (fu && fu.value.trim()) ||
      (title && title.value.trim()) ||
      (btnEl && btnEl.value.trim())
    );
    if (hasText && !confirm('Очистить текст письма и поля шаблона?')) return false;
    if (tplSel) tplSel.value = '';
    if (body) body.value = '';
    if (fu) fu.value = '';
    if (title) title.value = '';
    if (btnEl) btnEl.value = '';
    if (urlEl) urlEl.value = '';
    if (untilEl) untilEl.value = '';
    if (photoEl) photoEl.value = '';
    var tg = form.querySelector('input[name=channel][value="telegram"]');
    if (tg) tg.checked = true;
    syncShowWrap();
    saveDraft();
    return true;
  }
  if (tplBtn && tplSel) {
    tplBtn.addEventListener('click', function(){
      var key = tplSel.value;
      if (!key) {
        clearTemplateFields();
        return;
      }
      var t = MAIL_TEMPLATES[key];
      if (!t) return;
      var show = null;
      if (t.uses_show) {
        if (!MAIL_BEST_SHOWS.length) {
          alert('В афише BEST нет ближайших шоу. Время и адрес не подставятся.');
        } else {
          show = showById(showSel && showSel.value);
          if (!show) {
            alert('Выберите шоу для письма');
            return;
          }
        }
      }
      var body = form.querySelector('[name=body_html]');
      if (body && body.value.trim() && !confirm('Заменить текущий текст шаблоном «' + (t.title || key) + '»?')) return;
      var subs = show && show.subs;
      if (body) body.value = fillSubs(t.body_html || '', subs);
      var ch = form.querySelector('input[name=channel][value="' + t.channel + '"]');
      if (ch) ch.checked = true;
      var btnEl = form.querySelector('[name=button_text]');
      if (btnEl && t.button_text) btnEl.value = t.button_text;
      var fu = form.querySelector('[name=followup_html]');
      if (fu) fu.value = fillSubs(t.followup_html || '', subs);
      var title = form.querySelector('[name=title]');
      if (title && !title.value.trim()) title.value = (show && show.title) || t.title || '';
      saveDraft();
    });
  }
  var tplClear = document.getElementById('mail-template-clear');
  if (tplClear) tplClear.addEventListener('click', clearTemplateFields);

  function syncSendWhen(){
    var later = form.querySelector('input[name=send_when][value=later]');
    var submit = document.getElementById('mail-submit-btn');
    var at = document.getElementById('mail-scheduled-at');
    var isLater = !!(later && later.checked);
    if (at) at.required = isLater;
    if (submit) submit.textContent = isLater ? 'Запланировать рассылку' : 'Запустить рассылку';
  }
  form.querySelectorAll('input[name=send_when]').forEach(function(el){
    el.addEventListener('change', syncSendWhen);
  });
  syncSendWhen();

  var resetBtn = document.getElementById('mail-reset-filters-btn');
  if (resetBtn) {
    resetBtn.addEventListener('click', function(){
      form.querySelectorAll('input[name="booking_statuses"]').forEach(function(el){ el.checked = false; });
      form.querySelectorAll('input[name="has_phone"]').forEach(function(el){ el.checked = false; });
      form.querySelectorAll('input[name="exclude_today_bookings"]').forEach(function(el){ el.checked = false; });
      form.querySelectorAll('input[name="exclude_blocked"]').forEach(function(el){ el.checked = true; });
      ['date_from','date_to','batch_limit'].forEach(function(name){
        var el = form.querySelector('[name="'+name+'"]');
        if (el) el.value = '';
      });
      var ex = form.querySelector('[name="exclude_sent_days"]');
      if (ex) ex.value = '0';
      var both = form.querySelector('input[name="channel"][value="both"]');
      if (both) both.checked = true;
      box.innerHTML = '<span class="muted">Фильтры сброшены. Нажмите «Посчитать аудиторию».</span>';
      delete box.dataset.previewHtml;
      saveDraft();
    });
  }

  btn.addEventListener('click', function(){
    var fd = new FormData(form);
    // Без картинки — обычный urlencoded надёжнее для preview.
    fd.delete('photo');
    box.innerHTML = '<span class="muted">Считаем…</span>';
    fetch('/admin/mailing/preview', {method:'POST', body: fd, credentials:'same-origin'})
      .then(function(r){
        return r.text().then(function(text){
          var d = null;
          try { d = text ? JSON.parse(text) : {}; } catch (e) { d = null; }
          return {ok:r.ok, status:r.status, d:d, text:text};
        });
      })
      .then(function(res){
        var d = res.d;
        if (!d) {
          box.innerHTML = '<span class="events-error">Ответ не JSON (HTTP ' + res.status +
            '). Обновите страницу или перезапустите admin.</span>';
          delete box.dataset.previewHtml;
          return;
        }
        if (!res.ok || d.error) {
          box.innerHTML = '<span class="events-error">' + (d.error || ('Ошибка HTTP ' + res.status)) + '</span>';
          delete box.dataset.previewHtml;
          return;
        }
        var interval = parseInterval(fd.get('interval_sec'));
        var n = d.capped_total || 0;
        var eta = (n > 0) ? fmt((n-1)*interval) : '0 сек';
        var sel = d.selected_channel || fd.get('channel') || 'telegram';
        var chLabel = {telegram:'телеграм', vkontakte:'вк', both:'оба'}[sel] || sel;
        var f = d.filters || {};
        var statuses = (f.booking_statuses || []).join(', ') || 'без фильтра по броням';
        var df = f.date_from || '';
        var dt = f.date_to || '';
        var dateLabel = df ? (df === dt || !dt ? df : (df + ' … ' + dt)) : 'без даты шоу';
        var db = d.db_totals || {};
        var hint = '';
        if (n === 0 && ((db.telegram||0) + (db.vkontakte||0)) > 0) {
          hint = '<br><span class="events-error">В базе есть пользователи (телеграм ' +
            (db.telegram||0) + ' · вк ' + (db.vkontakte||0) +
            '), но фильтры отсеяли всех. Нажмите «Сбросить фильтры».</span>';
        } else if (n === 0) {
          hint = '<br><span class="events-error">В базе 0 пользователей с id телеграма или вк — проверьте DATABASE_URL у admin.</span>';
        }
        var html =
          '<b>К отправке (' + chLabel + '): ' + n + '</b>' +
          ' <span class="muted">(по фильтрам телеграм ' + (d.telegram||0) + ' · вк ' + (d.vkontakte||0) +
          (d.batch_limit ? (', лимит ' + d.batch_limit) : '') +
          '; в базе всего телеграм ' + (db.telegram||0) + ' · вк ' + (db.vkontakte||0) +
          ')</span><br>Примерное время: <b>' + eta + '</b> при интервале ' + interval + ' сек' +
          '<br><span class="muted">Время ≈ только паузы между сообщениями. Если телеграм попросит подождать — растянется. Картинка грузится один раз на всю рассылку.</span>' +
          '<br><span class="muted">Считаем по: ' + statuses + ' · ' + dateLabel +
          ' · exclude ' + (f.exclude_sent_days || 0) + ' дн.' +
          (f.exclude_today_bookings ? ' · без сегодняшних шоу' : '') +
          '</span>' + hint;
        box.innerHTML = html;
        box.dataset.previewHtml = html;
        saveDraft();
      })
      .catch(function(err){
        box.innerHTML = '<span class="events-error">Не удалось посчитать: ' +
          (err && err.message ? err.message : 'сеть') + '</span>';
      });
  });

  form.addEventListener('submit', function(){
    try { sessionStorage.removeItem(STORAGE_KEY); } catch (e) {}
  });

  var testQ = document.getElementById('mail-test-q');
  var testResults = document.getElementById('mail-test-results');
  var testUserId = document.getElementById('mail-test-user-id');
  var testSearchBtn = document.getElementById('mail-test-search-btn');
  var testSendBtn = document.getElementById('mail-test-send-btn');
  var testStatus = document.getElementById('mail-test-status');

  function selectedChannel(){
    var el = form.querySelector('input[name="channel"]:checked');
    return el ? el.value : 'telegram';
  }

  function renderTestUsers(rows){
    if (!rows || !rows.length) {
      testResults.innerHTML = '<span class="muted">Никого не найдено</span>';
      return;
    }
    var html = '<div class="mail-test-list">';
    for (var i = 0; i < rows.length; i++){
      var u = rows[i];
      var uname = u.username ? ('@' + u.username) : '';
      var ch = [];
      if (u.telegram_id) ch.push('TG');
      if (u.vk_id) ch.push('VK');
      html += '<label class="mail-test-item">' +
        '<input type="radio" name="mail_test_pick" value="' + u.id + '">' +
        '<span><b>' + (u.name || 'Без имени') + '</b> · id ' + u.id +
        (uname ? (' · ' + uname) : '') +
        (u.phone ? (' · ' + u.phone) : '') +
        ' · ' + ch.join('/') +
        '</span></label>';
    }
    html += '</div>';
    testResults.innerHTML = html;
    testResults.querySelectorAll('input[name="mail_test_pick"]').forEach(function(r){
      r.addEventListener('change', function(){ testUserId.value = r.value; });
    });
  }

  testSearchBtn.addEventListener('click', function(){
    var q = (testQ.value || '').trim();
    if (!q) { testResults.innerHTML = '<span class="events-error">Введите имя, @username или телефон</span>'; return; }
    testResults.innerHTML = '<span class="muted">Ищем…</span>';
    var ch = selectedChannel();
    var url = '/admin/mailing/users-search?q=' + encodeURIComponent(q) +
      (ch === 'both' ? '' : ('&channel=' + encodeURIComponent(ch)));
    fetch(url, {credentials:'same-origin'})
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (d.error) { testResults.innerHTML = '<span class="events-error">' + d.error + '</span>'; return; }
        renderTestUsers(d.users || []);
      })
      .catch(function(){ testResults.innerHTML = '<span class="events-error">Ошибка поиска</span>'; });
  });

  testQ.addEventListener('keydown', function(ev){
    if (ev.key === 'Enter') { ev.preventDefault(); testSearchBtn.click(); }
  });

  testSendBtn.addEventListener('click', function(){
    var uid = (testUserId.value || '').trim();
    if (!uid) { testStatus.innerHTML = '<span class="events-error">Сначала выберите пользователя из списка</span>'; return; }
    var fd = new FormData(form);
    fd.set('user_id', uid);
    // Явно фиксируем канал (чтобы не уехать в дефолтный telegram).
    fd.set('channel', selectedChannel());
    testStatus.innerHTML = '<span class="muted">Отправляем тест в ' + selectedChannel() + '…</span>';
    fetch('/admin/mailing/test', {method:'POST', body: fd, credentials:'same-origin'})
      .then(function(r){ return r.json().then(function(d){ return {ok:r.ok, d:d}; }); })
      .then(function(res){
        var d = res.d || {};
        if (!res.ok || d.error) {
          testStatus.innerHTML = '<span class="events-error">' + (d.error || 'Не удалось отправить') + '</span>';
          return;
        }
        testStatus.innerHTML = '<b>Тест отправлен</b> <span class="muted">· ' + (d.channel || '') +
          ' · user_id ' + (d.user_id || uid) + '</span>';
      })
      .catch(function(){ testStatus.innerHTML = '<span class="events-error">Ошибка отправки теста</span>'; });
  });
})();
</script>
"""
    )

    styles = """
<style>
  .mailing-compose, .mailing-test, .mailing-raffle-cancel, .mailing-history, .mailing-detail {
    --mail-ink:#243328; --mail-muted:#6b7d6e; --mail-accent:#3b9a4c; --mail-accent-deep:#2d7a3b;
    --mail-blush:#f7faf6; --mail-wash:#f2f7f1; --mail-mint:#eef8f2; --mail-line:#d5e4d6;
    border-color:var(--mail-line);
    background:linear-gradient(180deg,#fff 0%, var(--mail-blush) 140%);
    box-shadow:0 10px 28px rgba(45,122,59,.08);
    padding:22px 22px 20px;
  }
  .mailing-compose h2, .mailing-test h2, .mailing-detail h2 { color:var(--mail-ink); letter-spacing:-0.02em; }
  .mailing-compose > .muted, .mailing-test > .muted, .mailing-raffle-cancel .muted, .mailing-detail .muted {
    color:var(--mail-muted);
  }
  .mailing-compose label, .mailing-test label { display:block; margin:12px 0; font-size:14px; font-weight:700; color:var(--mail-ink); }
  .mailing-compose label[hidden], .mailing-compose #mail-show-hint[hidden] { display:none !important; }
  .mailing-compose input[type=text],
  .mailing-compose input[type=url],
  .mailing-compose input[type=number],
  .mailing-compose input[type=date],
  .mailing-compose input[type=datetime-local],
  .mailing-compose select,
  .mailing-compose textarea,
  .mailing-compose input[type=file],
  .mailing-test input[type=search],
  .mailing-raffle-cancel textarea,
  .mailing-raffle-cancel input[type=search],
  .mailing-detail input[type=date] {
    display:block; width:100%; margin-top:6px; padding:11px 13px;
    border:1px solid var(--mail-line); border-radius:14px; font:inherit; font-weight:500;
    background:#fff; color:var(--mail-ink);
  }
  .mailing-compose textarea { min-height:88px; line-height:1.45; }
  .mailing-compose input:focus, .mailing-compose select:focus, .mailing-compose textarea:focus,
  .mailing-test input:focus, .mailing-raffle-cancel textarea:focus, .mailing-raffle-cancel input:focus,
  .mailing-detail input:focus {
    outline:0; border-color:#8ec896; box-shadow:0 0 0 4px rgba(59,154,76,.16);
  }
  .mailing-compose details { margin:12px 0; }
  .mailing-compose summary, .mailing-raffle-cancel summary, .mailing-history summary {
    cursor:pointer; display:flex; justify-content:space-between; align-items:center;
    gap:12px; list-style:none; padding:10px 12px; border-radius:14px;
    background:var(--mail-wash); border:1px solid var(--mail-line); color:var(--mail-ink);
  }
  .mailing-compose summary::-webkit-details-marker,
  .mailing-raffle-cancel summary::-webkit-details-marker,
  .mailing-history summary::-webkit-details-marker { display:none; }
  .mailing-compose .details-action, .mailing-raffle-cancel .details-action, .mailing-history .details-action {
    background:transparent; color:var(--mail-accent-deep); padding:0;
  }
  .mailing-tpl-edit {
    margin:12px 0; padding:14px; border:1px solid var(--mail-line); border-radius:16px; background:#fff;
  }
  .mailing-tpl-edit h3 { margin:0 0 8px; font-size:15px; color:var(--mail-ink); }
  .mailing-reschedule { display:inline-flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .mailing-reschedule input[type=datetime-local] {
    padding:8px 10px; border-radius:12px; border:1px solid var(--mail-line); font:inherit;
  }
  .mailing-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }
  .mailing-row {
    border:1px solid var(--mail-line); border-radius:16px; padding:12px 14px; margin:12px 0;
    background:var(--mail-wash);
  }
  .mailing-row legend { padding:0 6px; font-weight:800; color:var(--mail-ink); }
  .mailing-row label { display:inline-flex; gap:6px; align-items:center; margin:4px 14px 4px 0; font-weight:600; color:#355944; }
  .mailing-preview {
    margin:14px 0; padding:14px 16px; background:var(--mail-mint);
    border-radius:16px; border:1px solid #cfe8d6; color:#355944;
  }
  .mailing-actions { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
  .mailing-actions button, .inline-form button, .rz-cancel-add {
    padding:10px 16px; border-radius:999px; border:1px solid var(--mail-accent-deep,#2d7a3b);
    background:var(--mail-accent,#3b9a4c); color:#fff; cursor:pointer; font:inherit; font-weight:800;
  }
  .mailing-compose .mailing-actions button,
  .mailing-test .mailing-actions button,
  .mailing-raffle-cancel .mailing-actions button,
  .mailing-history .mailing-actions button,
  .mailing-detail .mailing-actions button,
  .mailing-history .inline-form button,
  .mailing-detail .inline-form button,
  .mailing-raffle-cancel .rz-cancel-add {
    border-color:var(--mail-accent-deep); background:var(--mail-accent); color:#fff;
  }
  .mailing-actions button.mail-secondary-btn {
    background:#fff; color:var(--mail-accent-deep); border-color:#cfe4d2;
  }
  .mailing-compose .mailing-actions button:not(.mail-secondary-btn),
  .mailing-test .mailing-actions button,
  .mailing-raffle-cancel .mailing-actions button:not(.mail-secondary-btn) {
    background:var(--mail-accent); color:#fff; border-color:var(--mail-accent-deep);
  }
  .inline-form { display:inline; margin:0; }
  .events-error { color:#b91c1c; }
  .mail-test-results { margin-top:10px; }
  .mail-test-list { display:flex; flex-direction:column; gap:6px; }
  .mail-test-item {
    display:flex; gap:10px; align-items:flex-start; margin:0; padding:10px 12px;
    border:1px solid var(--mail-line); border-radius:14px; background:#fff; cursor:pointer;
  }
  .mail-test-item input { margin-top:3px; }
  .mailing-msg-preview {
    margin:8px 0 14px; padding:12px 14px; background:var(--mail-mint);
    border:1px solid #cfe8d6; border-radius:16px; white-space:pre-wrap; word-break:break-word; font-size:14px;
  }
  .mailing-history .pill, .mailing-detail .pill {
    background:#fff; border-color:var(--mail-line); color:var(--mail-ink);
  }
  .mailing-history .table-wrap { overflow-x:auto; }
  .mailing-history table.users td { white-space:normal; }
  .mailing-history table.users td.mailing-actions { white-space:normal; }
  .mailing-raffle-cancel label { display:block; margin:10px 0; font-size:14px; font-weight:700; color:var(--mail-ink); }
  .rz-cancel-chips { display:flex; flex-wrap:wrap; gap:8px; }
  .rz-cancel-chip {
    display:inline-flex; align-items:center; gap:6px; padding:6px 10px;
    background:#e5f4e8; border:1px solid #cfe8d6; border-radius:999px; font-size:13px;
  }
  .rz-cancel-chip-x {
    border:0; background:transparent; cursor:pointer; font-size:16px; line-height:1; padding:0 2px;
  }
  @media (max-width:900px) {
    .mailing-compose, .mailing-test, .mailing-raffle-cancel, .mailing-history, .mailing-detail {
      padding:16px;
    }
    .mailing-grid { grid-template-columns:1fr; }
    .mailing-compose .mailing-actions, .mailing-test .mailing-actions, .mailing-raffle-cancel .mailing-actions {
      align-items:stretch;
    }
    .mailing-compose .mailing-actions button,
    .mailing-test .mailing-actions button,
    .mailing-raffle-cancel .mailing-actions button,
    .mailing-raffle-cancel .mailing-actions .mail-secondary-btn {
      width:100%; text-align:center;
    }
    .mailing-history table.users,
    .mailing-history thead,
    .mailing-history tbody,
    .mailing-history th,
    .mailing-history td,
    .mailing-history tr { display:block; width:100%; }
    .mailing-history thead { display:none; }
    .mailing-history tbody tr {
      margin:0 0 12px; padding:12px; border:1px solid var(--mail-line);
      border-radius:16px; background:#fff;
    }
    .mailing-history td { border:0; padding:6px 0; white-space:normal; }
    .mailing-history td[data-label]::before {
      content:attr(data-label); display:block; font-size:12px;
      color:var(--mail-muted); font-weight:700; margin-bottom:2px;
    }
    .mailing-history td.mailing-actions {
      display:flex; flex-direction:column; align-items:stretch; gap:8px; padding-top:10px;
    }
    .mailing-history td.mailing-actions .pill,
    .mailing-history td.mailing-actions .inline-form,
    .mailing-history td.mailing-actions button {
      width:100%; display:block; text-align:center; box-sizing:border-box;
    }
    .mailing-history td.mailing-actions .inline-form {
      display:flex; flex-direction:column; gap:8px;
    }
    .mailing-history .mailing-reschedule { width:100%; flex-direction:column; align-items:stretch; }
  }
</style>
"""
    return styles + flash_html + error_html + detail_html + form + raffle_cancel + history
