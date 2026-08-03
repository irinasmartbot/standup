"""Admin UI for mailing campaigns."""

from __future__ import annotations

from bot.db.mailing import (
    estimate_duration_sec,
    format_duration,
    list_campaigns,
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
        actions = []
        actions.append(
            f'<a class="pill" href="/admin?tab=mailing&campaign={cid}">Получатели</a>'
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
            f"<td>{cid}</td>"
            f"<td>{_h(c.get('title'))}<br><span class='muted'>{_h(c.get('channel'))}</span></td>"
            f"<td>{_h(STATUS_LABELS.get(status, status))}</td>"
            f"<td>{sent}/{total}"
            f"<br><span class='muted'>ошибки {failed}</span></td>"
            f"<td>{_h(eta)}</td>"
            f"<td class='mailing-actions'>{''.join(actions)}</td>"
            "</tr>"
        )

    history = (
        '<section class="card">'
        "<h2>История рассылок</h2>"
        '<div class="table-wrap"><table class="users">'
        "<thead><tr><th>id</th><th>Название</th><th>Статус</th>"
        "<th>Прогресс</th><th>Осталось ≈</th><th></th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=\"6\" class=\"muted\">Пока пусто</td></tr>'}</tbody>"
        "</table></div>"
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
        detail_html = (
            '<section class="card">'
            f"<h2>Кампания #{_h(cid)} · {_h(detail.get('title'))}</h2>"
            f"<p class='muted'>Статус: {_h(STATUS_LABELS.get(detail.get('status'), detail.get('status')))} · "
            f"канал {_h(detail.get('channel'))} · интервал {_h(detail.get('interval_sec'))} сек</p>"
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

    form = """
<section class="card mailing-compose">
  <h2>Новая рассылка</h2>
  <p class="muted">HTML: &lt;b&gt;жирный&lt;/b&gt;, &lt;i&gt;курсив&lt;/i&gt;, &lt;a href="..."&gt;ссылка&lt;/a&gt;. TG и VK.</p>
  <form method="post" action="/admin/mailing/create" enctype="multipart/form-data" class="mailing-form" id="mailing-form">
    <label>Название
      <input type="text" name="title" placeholder="Анонс пятницы" maxlength="120">
    </label>
    <fieldset class="mailing-row">
      <legend>Канал</legend>
      <label><input type="radio" name="channel" value="telegram" checked> Telegram</label>
      <label><input type="radio" name="channel" value="vkontakte"> VK</label>
      <label><input type="radio" name="channel" value="both"> Оба</label>
    </fieldset>
    <label>Текст сообщения
      <textarea name="body_html" rows="8" placeholder="Привет! В эту пятницу..."></textarea>
    </label>
    <label>Картинка (необяз.)
      <input type="file" name="photo" accept="image/jpeg,image/png,image/webp">
    </label>
    <div class="mailing-grid">
      <label>Текст кнопки
        <input type="text" name="button_text" maxlength="40" placeholder="Подробнее">
      </label>
      <label>Ссылка кнопки (URL)
        <input type="url" name="button_url" placeholder="https://...">
      </label>
    </div>
    <label>После нажатия кнопки (если нет URL) — доп. текст
      <textarea name="followup_html" rows="3" placeholder="Отлично! Вот детали..."></textarea>
    </label>
    <div class="mailing-grid">
      <label>Интервал, сек
        <input type="number" name="interval_sec" value="0.1" min="0" max="60" step="0.05" id="mail-interval">
      </label>
      <label>Лимит за запуск (напр. 5000)
        <input type="number" name="batch_limit" min="1" max="100000" placeholder="все">
      </label>
      <label>Не слать, если уже слали за N дней
        <input type="number" name="exclude_sent_days" value="1" min="0" max="3650">
      </label>
    </div>
    <fieldset class="mailing-row">
      <legend>Фильтры аудитории</legend>
      <label><input type="checkbox" name="exclude_blocked" value="1" checked> Исключить заблокировавших TG-бота</label>
      <label><input type="checkbox" name="has_phone" value="1"> Только с телефоном</label>
    </fieldset>
    <fieldset class="mailing-row">
      <legend>Статус брони (если ничего не выбрано — вся база канала)</legend>
      <label><input type="checkbox" name="booking_statuses" value="booked"> Активная бронь</label>
      <label><input type="checkbox" name="booking_statuses" value="confirmed"> Подтверждённый билет</label>
      <label><input type="checkbox" name="booking_statuses" value="cancelled"> Отмена</label>
      <label><input type="checkbox" name="booking_statuses" value="annulled"> Аннулировано</label>
    </fieldset>
    <div class="mailing-grid">
      <label>Дата бронирования с
        <input type="date" name="booking_date_from">
      </label>
      <label>Дата бронирования по
        <input type="date" name="booking_date_to">
      </label>
    </div>
    <div class="mailing-preview" id="mail-preview">
      <span class="muted">Нажмите «Посчитать аудиторию», чтобы увидеть число и примерное время.</span>
    </div>
    <div class="mailing-actions">
      <button type="button" id="mail-preview-btn">Посчитать аудиторию</button>
      <button type="submit">Запустить рассылку</button>
    </div>
  </form>
</section>
<script>
(function(){
  var form = document.getElementById('mailing-form');
  var btn = document.getElementById('mail-preview-btn');
  var box = document.getElementById('mail-preview');
  if (!form || !btn || !box) return;
  var STORAGE_KEY = 'admin-mailing-draft-v1';

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
    if (box && box.dataset.previewHtml) data.__preview = box.dataset.previewHtml;
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
        var list = data[el.name] || [];
        el.checked = list.indexOf(el.value) !== -1;
        continue;
      }
      if (Object.prototype.hasOwnProperty.call(data, el.name)) {
        el.value = data[el.name];
      }
    }
    if (data.__preview) {
      box.innerHTML = data.__preview;
      box.dataset.previewHtml = data.__preview;
    }
  }

  function fmt(sec){
    sec = Math.round(sec||0);
    if (sec < 60) return sec + ' сек';
    var m = Math.floor(sec/60), s = sec % 60;
    if (m < 60) return m + ' мин' + (s ? (' ' + s + ' сек') : '');
    var h = Math.floor(m/60); m = m % 60;
    return h + ' ч' + (m ? (' ' + m + ' мин') : '');
  }

  restoreDraft();
  form.addEventListener('input', saveDraft);
  form.addEventListener('change', saveDraft);

  btn.addEventListener('click', function(){
    var fd = new FormData(form);
    box.innerHTML = '<span class="muted">Считаем…</span>';
    fetch('/admin/mailing/preview', {method:'POST', body: fd, credentials:'same-origin'})
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (d.error) { box.innerHTML = '<span class="events-error">' + d.error + '</span>'; return; }
        var interval = parseFloat(fd.get('interval_sec')||'0.1')||0;
        var n = d.capped_total || 0;
        var eta = (n > 0) ? fmt((n-1)*interval) : '0 сек';
        var html =
          '<b>К отправке: ' + n + '</b>' +
          ' <span class="muted">(TG ' + (d.telegram||0) + ' · VK ' + (d.vkontakte||0) +
          (d.batch_limit ? (', лимит ' + d.batch_limit) : '') +
          ')</span><br>Примерное время: <b>' + eta + '</b> при интервале ' + interval + ' сек';
        box.innerHTML = html;
        box.dataset.previewHtml = html;
        saveDraft();
      })
      .catch(function(){ box.innerHTML = '<span class="events-error">Не удалось посчитать</span>'; });
  });

  form.addEventListener('submit', function(){
    try { sessionStorage.removeItem(STORAGE_KEY); } catch (e) {}
  });
})();
</script>
"""

    styles = """
<style>
  .mailing-compose label { display:block; margin:10px 0; font-size:14px; }
  .mailing-compose input[type=text],
  .mailing-compose input[type=url],
  .mailing-compose input[type=number],
  .mailing-compose input[type=date],
  .mailing-compose textarea,
  .mailing-compose input[type=file] {
    display:block; width:100%; margin-top:4px; padding:8px 10px;
    border:1px solid var(--line); border-radius:10px; font:inherit;
  }
  .mailing-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }
  .mailing-row { border:1px solid var(--line); border-radius:12px; padding:10px 12px; margin:12px 0; }
  .mailing-row legend { padding:0 6px; font-weight:600; }
  .mailing-row label { display:inline-flex; gap:6px; align-items:center; margin:4px 12px 4px 0; }
  .mailing-preview { margin:14px 0; padding:12px 14px; background:#f8fafc; border-radius:12px; border:1px solid var(--line); }
  .mailing-actions { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
  .mailing-actions button, .inline-form button {
    padding:8px 12px; border-radius:10px; border:1px solid #111827; background:#111827; color:#fff; cursor:pointer;
  }
  .inline-form { display:inline; margin:0; }
  .inline-form button { background:#fff; color:#111827; }
  .events-error { color:#b91c1c; }
  @media (max-width:900px){ .mailing-grid { grid-template-columns:1fr; } }
</style>
"""
    return styles + flash_html + error_html + detail_html + form + history
