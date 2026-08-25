"""Public VK entry landings on go.moscowstandupshow.ru (no admin auth).

Без VK ID One Tap (браузерный логин режет конверсию).

Путь:
1) виджет «Разрешить сообщения» → получаем vk_id
2) сразу шлём нужную ветку бота в личку
3) пробуем открыть приложение VK (не сайт vk.com)

GET  /vk/booking | /vk/raffle | /vk/offline-gift
POST /vk/entry
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from html import escape
from typing import Any
from urllib.parse import parse_qsl, urlencode

from aiohttp import web

from bot.vk.client import VKAPIError, VKClient
from bot.vk.config import load_vk_settings
from bot.vk.keyboards import VKKeyboardBuilder

logger = logging.getLogger(__name__)

FLOWS: dict[str, dict[str, Any]] = {
    "booking": {
        "title": "Бронирование",
        "headline": "Забронировать места",
        "button": "Забронировать места",
        "lead": "Разрешите сообщения — пришлём информацию о бронировании в личку VK.",
        "ref": "standup_book",
    },
    "raffle": {
        "title": "Розыгрыш",
        "headline": "Участвовать в розыгрыше",
        "button": "Участвовать в розыгрыше",
        "lead": "Разрешите сообщения — пришлём старт розыгрыша в личку VK.",
        "ref": "standup_rozygr",
    },
    "offline_gift": {
        "title": "Подарок",
        "headline": "Участвовать в розыгрыше на шоу",
        "button": "Подарок на шоу",
        "lead": "Разрешите сообщения — пришлём список на подарок в личку VK.",
        "ref": "offline_gift",
    },
}

# После модерации снова показываем все входные сценарии mini app.
MINI_APP_VISIBLE_FLOWS: tuple[str, ...] = ("booking", "raffle", "offline_gift")

_ENTRY_COOLDOWN_SEC = 45.0
_MINI_FLOW_HANDOFF_TTL_SEC = 300.0
_last_entry: dict[tuple[int, str], float] = {}
_mini_flow_handoff: dict[str, tuple[str, float]] = {}


def _env_app_id() -> str:
    return (os.getenv("VK_OPENAPI_APP_ID") or "").strip()


def _env_mini_app_id() -> str:
    return (os.getenv("VK_MINI_APP_ID") or "").strip()


def _env_mini_app_secret() -> str:
    return (os.getenv("VK_MINI_APP_SECRET") or "").strip()


def _request_ip(request: web.Request) -> str:
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    return forwarded or (request.remote or "")


def _remember_mini_flow(request: web.Request, flow_key: str) -> None:
    ip = _request_ip(request)
    if not ip:
        return
    now = time.monotonic()
    _mini_flow_handoff[ip] = (flow_key, now)
    cutoff = now - _MINI_FLOW_HANDOFF_TTL_SEC
    for key, (_, ts) in list(_mini_flow_handoff.items()):
        if ts < cutoff:
            _mini_flow_handoff.pop(key, None)


def _mini_flow_from_handoff(request: web.Request) -> str:
    ip = _request_ip(request)
    if not ip:
        return ""
    item = _mini_flow_handoff.get(ip)
    if not item:
        return ""
    flow_key, ts = item
    _mini_flow_handoff.pop(ip, None)
    if time.monotonic() - ts > _MINI_FLOW_HANDOFF_TTL_SEC:
        return ""
    return flow_key if flow_key in FLOWS else ""


def _mini_app_vk_url(
    settings,
    flow_key: str = "",
    *,
    short: bool = False,
    cid: str = "",
) -> str:
    """Ссылка mini app внутри VK.

    short=True → vk.ru/app{id}#flow=… (лучше на телефоне; без _-group).
    short=False → vk.com/app{id}_-{group}#flow=… (канон, на ПК hash часто режется).
    """
    app_id = _env_mini_app_id() or "54704296"
    if short:
        host = "https://vk.ru"
        group_suffix = ""
    else:
        host = "https://vk.com"
        group_suffix = f"_-{int(settings.group_id)}" if settings.group_id else ""
    url = f"{host}/app{app_id}{group_suffix}"
    parts: list[str] = []
    if flow_key and flow_key in FLOWS:
        parts.append(f"flow={flow_key}")
    cid_value = (cid or "").strip()
    if cid_value:
        parts.append(f"cid={cid_value}")
    if parts:
        url = f"{url}#{'&'.join(parts)}"
    return url


def _request_cid(request: web.Request) -> str:
    return str(
        request.query.get("cid")
        or request.query.get("client_id")
        or request.query.get("ym_cid")
        or ""
    ).strip()


def _is_mobile_request(request: web.Request) -> bool:
    ua = (request.headers.get("User-Agent") or "").lower()
    return any(
        token in ua
        for token in ("android", "iphone", "ipad", "ipod", "mobile", "opera mini", "webos")
    )


def _mobile_vk_jump_html(flow_key: str, target_url: str) -> str:
    """Быстрый уход с go-моста в рабочую короткую VK-ссылку (телефон)."""
    flow = FLOWS.get(flow_key) or {}
    headline = escape(str(flow.get("headline") or "Moscow StandUp Show"))
    target = escape(target_url, quote=True)
    target_js = json.dumps(target_url)
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
  <meta http-equiv="refresh" content="0; url={target}">
  <title>VK · Moscow StandUp Show</title>
  <style>
    body {{
      margin: 0; min-height: 100vh; color: #f7f3ea;
      font-family: Manrope, system-ui, sans-serif;
      background: linear-gradient(180deg, #12080c 0%, #070708 100%);
    }}
    main {{ width: min(440px, calc(100% - 32px)); margin: 0 auto; padding: 40px 0; }}
    h1 {{ font-size: 28px; margin: 0 0 12px; }}
    .lead {{ color: #d2c4b0; line-height: 1.45; }}
    .cta {{
      display: block; margin-top: 20px; text-align: center; text-decoration: none;
      padding: 14px 16px; border-radius: 14px; font-weight: 700; color: #1a1208;
      background: linear-gradient(180deg, #f0d48a 0%, #c9a227 100%);
    }}
  </style>
</head>
<body>
  <main>
    <h1>{headline}</h1>
    <p class="lead">Открываем VK… Если не открылось — нажмите кнопку.</p>
    <a class="cta" id="open" href="{target}">Открыть в VK</a>
  </main>
  <script>
  (function () {{
    var url = {target_js};
    try {{ window.location.replace(url); }} catch (_) {{
      window.location.href = url;
    }}
  }})();
  </script>
</body>
</html>"""


def _mini_start_bridge_html(flow_key: str, target_url: str) -> str:
    """Промежуточная страница для /vk-mini/start/{{flow}}.

    На телефоне не делаем intent/vk:// + запасные таймеры — из‑за них
    клиент «болтает» между браузером и VK и не доходит до диалога.
    Мобилка: одна кнопка с обычной https-ссылкой. Десктоп: один replace.
    """
    flow = FLOWS.get(flow_key) or {}
    headline = escape(str(flow.get("headline") or "Moscow StandUp Show"))
    target = escape(target_url, quote=True)
    target_js = json.dumps(target_url)
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
  <title>VK · Moscow StandUp Show</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@500;600;700&family=Pacifico&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg0: #070708; --gold: #e8c56a; --gold-deep: #c9a227;
      --text: #f7f3ea; --muted: #d2c4b0;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; min-height: 100vh; color: var(--text);
      font-family: Manrope, sans-serif;
      background:
        radial-gradient(ellipse 90% 55% at 50% 100%, rgba(180, 30, 45, .45) 0%, transparent 55%),
        linear-gradient(180deg, #12080c 0%, var(--bg0) 45%, #14060a 100%);
    }}
    main {{ width: min(440px, calc(100% - 32px)); margin: 0 auto; padding: 40px 0 72px; }}
    .brand {{
      margin: 0 0 18px; font-family: Pacifico, cursive; font-size: 22px;
      color: var(--gold); line-height: 1.2;
    }}
    h1 {{
      margin: 0 0 14px; font-size: clamp(28px, 7vw, 38px); font-weight: 700;
      line-height: 1.15; letter-spacing: -0.02em;
    }}
    .lead {{ margin: 0 0 22px; font-size: 16px; line-height: 1.45; color: var(--muted); }}
    .cta {{
      display: block; width: 100%; min-height: 52px; border: 0; border-radius: 14px;
      padding: 14px 16px; cursor: pointer; text-align: center; text-decoration: none;
      background: linear-gradient(180deg, #f0d48a 0%, var(--gold-deep) 100%);
      color: #1a1208; font: 700 16px Manrope, sans-serif;
      box-shadow: 0 8px 28px rgba(201, 162, 39, .28);
    }}
    .status {{
      font-size: 14px; margin: 14px 0 0; padding: 12px 14px; border-radius: 12px;
      background: rgba(255,255,255,.06); border: 1px solid rgba(232, 197, 106, .18);
      color: #d8f5d0;
    }}
  </style>
</head>
<body>
  <main>
    <p class="brand">Moscow StandUp Show</p>
    <h1>{headline}</h1>
    <p class="lead" id="lead">Нажмите кнопку — откроется мини-приложение в VK.</p>
    <a class="cta" id="open" href="{target}">Открыть в VK</a>
    <p class="status" id="status">Дальше продолжение будет в личке сообщества.</p>
  </main>
  <script>
  (function () {{
    var url = {target_js};
    var openBtn = document.getElementById("open");
    var leadEl = document.getElementById("lead");
    var ua = navigator.userAgent || "";
    var mobile = /Android|iPhone|iPad|iPod|Mobile/i.test(ua);
    openBtn.href = url;
    // Никаких intent/vk:// и повторных location через setTimeout —
    // они как раз дают «болтанку» и не доводят до диалога.
    if (!mobile) {{
      if (leadEl) leadEl.textContent = "Открываем мини-приложение в VK…";
      window.location.replace(url);
    }}
  }})();
  </script>
</body>
</html>"""


def _gift_format_label(value: str) -> str:
    return {
        "proverka": "Проверка",
        "best": "BEST",
        "hitloto": "Хитлото",
    }.get(value or "", value or "Шоу")


def _gift_event_label(event: dict[str, Any]) -> str:
    """Как в VK-боте: «19:00 · Temple Bar · BEST»."""
    label = " · ".join(
        part
        for part in [
            str(event.get("time") or "").strip(),
            str(event.get("location") or "").strip(),
            _gift_format_label(str(event.get("format") or "")),
        ]
        if part
    )
    return label or "шоу"


async def _send_flow_chain(client: VKClient, settings, flow_key: str, vk_id: int) -> None:
    """Сразу ветка бота — без сообщения «нажмите кнопку ниже»."""
    from bot.db.analytics import EVENT_BOT_START, track_event
    from bot.vk import raffle as vk_raffle
    from bot.vk.app import in_evening_offline_gift_window
    from bot.vk.entry_dedupe import claim_flow_send, clear_flow_send

    # Вечером на шоу вход в «розыгрыш» = офлайн-подарок.
    if flow_key == "raffle" and in_evening_offline_gift_window():
        logger.info(
            "Evening window: mini-app raffle → offline gift vk_id=%s",
            vk_id,
        )
        flow_key = "offline_gift"

    flow = FLOWS.get(flow_key) or {}
    # Общий антидубль с VK-ботом (разные процессы / воркеры).
    # Офлайн-розыгрыш: 30 мин без дубля карточки; повтор = действие «участвовать/выбрать».
    ttl = 1800.0 if flow_key == "offline_gift" else None
    claimed = (
        claim_flow_send(int(vk_id), flow_key, ttl_sec=ttl)
        if ttl is not None
        else claim_flow_send(int(vk_id), flow_key)
    )
    if not claimed:
        logger.info("VK entry deduped vk_id=%s flow=%s", vk_id, flow_key)
        if flow_key == "offline_gift":
            await _offline_gift_repeat_action(client, vk_id)
        return

    track_event(
        EVENT_BOT_START,
        vk_id=int(vk_id),
        channel="vkontakte",
        props={
            "payload": str(flow.get("ref") or flow_key),
            "via": "mini_app_flow",
            "flow": flow_key,
        },
    )

    try:
        await _send_flow_chain_body(client, settings, flow_key, vk_id, vk_raffle)
    except Exception:
        clear_flow_send(int(vk_id), flow_key)
        raise


async def _offline_gift_repeat_action(client: VKClient, vk_id: int) -> None:
    """Повторный запуск в окне 30 мин: как «участвовать» / «выберите шоу»."""
    from bot.db.crud import (
        get_offline_gift_today_events,
        record_offline_gift_entry,
        set_offline_gift_pending,
    )

    events = get_offline_gift_today_events()
    if not events:
        await client.send_message(
            vk_id,
            (
                "🎁 <b>Розыгрыш подарка</b>\n\n"
                "На сегодня активных шоу не найдено. "
                "Покажи это сообщение администратору или попробуй позже."
            ),
        )
        return
    if len(events) == 1:
        event = events[0]
        event_id = int(event["id"])
        try:
            subscribed = await client.is_group_member(int(vk_id))
        except Exception:
            logger.exception("offline gift sub check failed vk_id=%s", vk_id)
            subscribed = False
        if subscribed:
            try:
                name = await client.get_user_display_name(int(vk_id))
            except Exception:
                name = ""
            record_offline_gift_entry(event_id=event_id, vk_id=int(vk_id), full_name=name or "")
            kb = VKKeyboardBuilder(inline=True)
            kb.button("В главное меню", {"cmd": "main_menu"})
            kb.adjust(1)
            await client.send_message(
                vk_id,
                (
                    "🎁 <b>Зафиксировал в списке участников ✅</b>\n\n"
                    f"<b>Шоу:</b> {_gift_event_label(event)}\n\n"
                    "Ведущий выберет победителя во время шоу. Удачи!"
                ),
                keyboard=kb.as_json(),
            )
            return
        set_offline_gift_pending(vk_id=int(vk_id), event_id=event_id)
        kb = VKKeyboardBuilder(inline=True)
        settings = load_vk_settings()
        if settings.community_link:
            kb.button("Перейти в сообщество", link=settings.community_link)
        kb.button(
            "Готово",
            {"cmd": "ogift_sub_check", "event_id": event_id},
            color="primary",
        )
        kb.adjust(1)
        await client.send_message(
            vk_id,
            (
                "🎁 <b>Ты пока не в списке участников.</b>\n\n"
                "Выполни задание ведущего — и будешь в списке.\n\n"
                f"<b>Шоу:</b> {_gift_event_label(event)}"
            ),
            keyboard=kb.as_json(),
        )
        return
    kb = VKKeyboardBuilder(inline=True)
    for event in events[:8]:
        kb.button(
            _gift_event_label(event)[:40],
            {"cmd": "ogift_event", "event_id": int(event["id"])},
            color="primary",
        )
    kb.adjust(1)
    await client.send_message(
        vk_id,
        (
            "🎁 Чтобы попасть в список участников, выберите мероприятие, "
            "на котором вы сейчас находитесь 👇"
        ),
        keyboard=kb.as_json(),
    )


async def _booking_cover_attachment(client: VKClient, vk_id: int, settings) -> str | None:
    """Случайная обложка шоу — кэш VK или загрузка из фото/."""
    from bot.vk.media import resolve_booking_cover_attachment

    return await resolve_booking_cover_attachment(client, vk_id, settings)


async def _send_flow_chain_body(client: VKClient, settings, flow_key: str, vk_id: int, vk_raffle) -> None:
    from bot.db.analytics import EVENT_BRANCH_PROVERKA, track_event

    if flow_key == "raffle":
        ok, reason, booking_id = vk_raffle.can_enter_raffle(vk_id)
        if not ok:
            await client.send_message(
                vk_id,
                reason,
                keyboard=vk_raffle.blocked_keyboard(booking_id),
            )
            return
        await client.send_message(
            vk_id,
            vk_raffle.start_text(settings.community_link),
            keyboard=vk_raffle.start_keyboard(),
        )
        return

    if flow_key == "booking":
        track_event(
            EVENT_BRANCH_PROVERKA,
            vk_id=int(vk_id),
            channel="vkontakte",
            props={"via": "mini_app_flow"},
        )
        from bot.vk.app import CHECK_ENTRY_TEXT, event_search_keyboard

        cover = await _booking_cover_attachment(client, vk_id, settings)
        await client.send_message(
            vk_id,
            CHECK_ENTRY_TEXT,
            keyboard=event_search_keyboard(
                "check_date_page",
                "check_venues",
                dates_label="📅 Выбрать по дате",
                venues_label="📍 Выбор по площадке",
            ),
            attachment=cover,
        )
        return

    from bot.db.crud import get_offline_gift_today_events

    events = get_offline_gift_today_events()
    if not events:
        await client.send_message(
            vk_id,
            (
                "🎁 <b>Розыгрыш подарка</b>\n\n"
                "На сегодня активных шоу не найдено. "
                "Покажи это сообщение администратору или попробуй позже."
            ),
        )
        return
    if len(events) == 1:
        event = events[0]
        kb = VKKeyboardBuilder(inline=True)
        kb.button(
            "Участвовать в розыгрыше",
            {"cmd": "ogift_event", "event_id": int(event["id"])},
            color="primary",
        )
        kb.adjust(1)
        await client.send_message(
            vk_id,
            (
                "🎁 <b>Розыгрыш подарка на шоу</b>\n\n"
                f"<b>Сегодня:</b> {_gift_event_label(event)}\n\n"
                "Нажми кнопку ниже, чтобы попасть в список участников."
            ),
            keyboard=kb.as_json(),
        )
        return
    kb = VKKeyboardBuilder(inline=True)
    for event in events[:8]:
        label = _gift_event_label(event)[:40]
        kb.button(
            label,
            {"cmd": "ogift_event", "event_id": int(event["id"])},
            color="primary",
        )
    kb.adjust(1)
    await client.send_message(
        vk_id,
        (
            "🎁 <b>Розыгрыш подарка на шоу</b>\n\n"
            "Чтобы мы могли внести вас в нужный список, выберите мероприятие, "
            "на котором вы сейчас находитесь:"
        ),
        keyboard=kb.as_json(),
    )


def _landing_html(flow_key: str, *, cid: str = "") -> str:
    flow = FLOWS[flow_key]
    settings = load_vk_settings()
    app_id = _env_app_id()
    group_id = settings.group_id or 0
    ready = bool(app_id and group_id and settings.group_token)
    cid_value = (cid or "").strip()

    if not ready:
        body = """
        <p class="warn">Страница ещё настраивается. Загляните чуть позже или напишите менеджеру.</p>
        """
        widget_js = ""
    else:
        write_url = f"https://vk.com/write-{int(group_id)}?ref={flow['ref']}"
        if cid_value:
            # cid в ref для будущей Метрики; бот пока читает базовый standup_book*
            write_url = f"{write_url}_c{cid_value}"
        body = f"""
        <p class="lead">Нажмите кнопку — откроется диалог VK, и бот пришлёт сценарий бронирования.</p>
        <a class="cta" id="openChat" href="{escape(write_url, quote=True)}">Продолжить в VK</a>
        <p class="hint">Если сообщество ещё не может писать вам — сначала нажмите виджет ниже и разрешите сообщения, затем снова «Продолжить в VK».</p>
        <div id="vk_allow_messages_from_community" class="widget"></div>
        <p id="status" class="status" hidden></p>
        """
        ref_value = str(flow["ref"])
        widget_js = f"""
<script src="https://vk.com/js/api/openapi.js?169"></script>
<script>
(function () {{
  var appId = {int(app_id)};
  var groupId = {int(group_id)};
  var writeUrl = {json.dumps(write_url)};
  var statusEl = document.getElementById("status");
  var openChat = document.getElementById("openChat");

  function setStatus(text, ok) {{
    if (!statusEl) return;
    statusEl.hidden = !text;
    statusEl.textContent = text || "";
    statusEl.className = "status " + (ok ? "ok" : "err");
  }}

  if (openChat) {{
    openChat.addEventListener("click", function () {{
      setStatus("Открываем диалог VK…", true);
    }});
  }}

  // Автопереход на ПК — надёжнее, чем ждать колбэк виджета.
  setTimeout(function () {{
    try {{ window.location.href = writeUrl; }} catch (_) {{}}
  }}, 600);

  try {{
    VK.init({{ apiId: appId, onlyWidgets: true }});
    if (VK.Observer && VK.Observer.subscribe) {{
      VK.Observer.subscribe("widgets.allowMessagesFromCommunity.allowed", function () {{
        setStatus("Сообщения разрешены. Открываем диалог…", true);
        window.location.href = writeUrl;
      }});
      VK.Observer.subscribe("widgets.allowMessagesFromCommunity.denied", function () {{
        setStatus("Разрешите сообщения сообществу, иначе бот не сможет написать.", false);
      }});
    }}
    VK.Widgets.AllowMessagesFromCommunity(
      "vk_allow_messages_from_community",
      {{ height: 30 }},
      groupId
    );
  }} catch (e) {{
    setStatus("Виджет VK не загрузился — используйте кнопку «Продолжить в VK».", false);
  }}
}})();
</script>
"""

    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
  <meta http-equiv="Pragma" content="no-cache">
  <title>{escape(flow["title"])} · Moscow StandUp Show</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@500;600;700&family=Pacifico&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg0: #070708;
      --gold: #e8c56a;
      --gold-deep: #c9a227;
      --text: #f7f3ea;
      --muted: #d2c4b0;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; min-height: 100vh; color: var(--text);
      font-family: Manrope, sans-serif;
      background:
        radial-gradient(ellipse 90% 55% at 50% 100%, rgba(180, 30, 45, .45) 0%, transparent 55%),
        radial-gradient(ellipse 70% 40% at 50% 0%, rgba(80, 10, 20, .55) 0%, transparent 50%),
        linear-gradient(180deg, #12080c 0%, var(--bg0) 45%, #14060a 100%);
    }}
    main {{
      width: min(440px, calc(100% - 32px)); margin: 0 auto; padding: 40px 0 72px;
    }}
    .brand {{
      margin: 0 0 18px; font-family: Pacifico, cursive; font-size: 22px;
      color: var(--gold); line-height: 1.2; text-shadow: 0 0 24px rgba(232, 197, 106, .25);
    }}
    h1 {{
      margin: 0 0 14px; font-size: clamp(28px, 7vw, 38px); font-weight: 700;
      line-height: 1.15; letter-spacing: -0.02em;
    }}
    .lead {{
      margin: 0 0 22px; font-size: 16px; line-height: 1.45; color: var(--muted);
    }}
    .hint {{
      margin: 0 0 14px; font-size: 13px; line-height: 1.4; color: #b9a994;
    }}
    .widget {{ margin: 0 0 14px; min-height: 36px; }}
    .cta {{
      display: block; width: 100%; min-height: 52px; border: 0; border-radius: 14px;
      margin: 0 0 12px; padding: 14px 16px; cursor: pointer; text-align: center;
      text-decoration: none;
      background: linear-gradient(180deg, #f0d48a 0%, var(--gold-deep) 100%);
      color: #1a1208; font: 700 16px Manrope, sans-serif;
      box-shadow: 0 8px 28px rgba(201, 162, 39, .28);
    }}
    [hidden] {{ display: none !important; }}
    .status {{
      font-size: 14px; margin: 0 0 14px; padding: 12px 14px; border-radius: 12px;
      background: rgba(255,255,255,.06); border: 1px solid rgba(232, 197, 106, .18);
    }}
    .status.ok {{ color: #d8f5d0; }}
    .status.err {{ color: #ffd0d0; }}
    .warn {{
      background: rgba(232, 197, 106, .1); border: 1px solid rgba(232, 197, 106, .35);
      padding: 14px 16px; border-radius: 12px; color: #ffe6b0;
    }}
  </style>
</head>
<body>
  <main>
    <p class="brand">Moscow StandUp Show</p>
    <h1>{escape(flow["headline"])}</h1>
    {body}
  </main>
  {widget_js}
</body>
</html>"""


def _html_response(text: str) -> web.Response:
    """Лендинги/миниапп не должны залипать в кеше VK WebView."""
    return web.Response(
        text=text,
        content_type="text/html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


async def landing_booking(request: web.Request) -> web.Response:
    """Одна простая ссылка: всегда уводим в рабочий mini app #flow=booking."""
    cid = _request_cid(request)
    settings = load_vk_settings()
    target = _mini_app_vk_url(settings, "booking", short=True, cid=cid)
    return _html_response(_mobile_vk_jump_html("booking", target))


async def landing_raffle(request: web.Request) -> web.Response:
    cid = _request_cid(request)
    settings = load_vk_settings()
    target = _mini_app_vk_url(settings, "raffle", short=True, cid=cid)
    return _html_response(_mobile_vk_jump_html("raffle", target))


async def landing_offline_gift(request: web.Request) -> web.Response:
    cid = _request_cid(request)
    settings = load_vk_settings()
    target = _mini_app_vk_url(settings, "offline_gift", short=True, cid=cid)
    return _html_response(_mobile_vk_jump_html("offline_gift", target))


def _vk_me_path(community_link: str, group_id: int) -> str:
    """Путь для vk.me / write: screen_name или club{{id}}."""
    link = (community_link or "").strip().rstrip("/")
    for host in ("vk.com/", "vk.ru/", "m.vk.com/", "m.vk.ru/"):
        if host in link:
            name = link.split(host, 1)[-1].split("?")[0].split("/")[0].strip()
            if name and name not in {"club", "public", "write"}:
                if name.startswith("write-"):
                    break
                return name
    return f"club{int(group_id)}" if group_id else ""


def _mini_app_html(default_flow: str = "") -> str:
    settings = load_vk_settings()
    group_id = int(settings.group_id or 0)
    vk_me = _vk_me_path(settings.community_link, group_id)
    ready = bool(group_id and settings.group_token)
    visible = [key for key in MINI_APP_VISIBLE_FLOWS if key in FLOWS]
    flow_labels = {
        key: {
            "headline": value["headline"],
            "button": value["button"],
            "lead": value["lead"],
        }
        for key, value in FLOWS.items()
        if key in visible
    }
    if default_flow and default_flow not in visible:
        default_flow = visible[0] if visible else ""

    if not ready:
        body = """
        <p class="warn">Мини-приложение ещё настраивается. Загляните чуть позже или напишите менеджеру.</p>
        """
        app_js = ""
    else:
        buttons_html = "\n".join(
            (
                f'          <button type="button" class="cta" data-flow="{escape(key)}">'
                f'{escape(FLOWS[key]["button"])}</button>'
            )
            for key in visible
        )
        body = f"""
        <p id="lead" class="lead">Забронируйте места на шоу — продолжение в личке VK.</p>
        <div class="actions" id="actions">
{buttons_html}
        </div>
        <p id="status" class="status" hidden></p>
        """
        app_js = f"""
<script>
(function () {{
  function createFallbackBridge() {{
    var webFrameId = null;
    var seq = 0;
    var pending = {{}};

    function handleEvent(event) {{
      var raw = event && (event.data || (event.detail && event.detail));
      if (!raw) return;
      if (typeof raw === "string") {{
        try {{ raw = JSON.parse(raw); }} catch (_) {{ return; }}
      }}
      var type = raw.type || (raw.detail && raw.detail.type);
      var data = raw.data || (raw.detail && raw.detail.data) || {{}};
      if (type === "VKWebAppSettings") {{
        webFrameId = raw.frameId || data.frameId || webFrameId;
        return;
      }}
      var requestId = data && data.request_id;
      if (!requestId || !pending[requestId]) return;
      var callbacks = pending[requestId];
      delete pending[requestId];
      if (data.error_type || data.error_data || data.error_reason) {{
        callbacks.reject(data);
      }} else {{
        callbacks.resolve(data);
      }}
    }}

    window.addEventListener("message", handleEvent);
    document.addEventListener("VKWebAppEvent", handleEvent);

    return {{
      send: function (method, params) {{
        return new Promise(function (resolve, reject) {{
          var requestId = String(++seq) + "_" + Math.random().toString(36).slice(2);
          var payload = Object.assign({{}}, params || {{}}, {{ request_id: requestId }});
          pending[requestId] = {{ resolve: resolve, reject: reject }};
          try {{
            if (window.AndroidBridge && typeof window.AndroidBridge[method] === "function") {{
              window.AndroidBridge[method](JSON.stringify(payload));
              return;
            }}
            if (
              window.webkit &&
              window.webkit.messageHandlers &&
              window.webkit.messageHandlers[method] &&
              typeof window.webkit.messageHandlers[method].postMessage === "function"
            ) {{
              window.webkit.messageHandlers[method].postMessage(payload);
              return;
            }}
            if (window.ReactNativeWebView && typeof window.ReactNativeWebView.postMessage === "function") {{
              window.ReactNativeWebView.postMessage(JSON.stringify({{ handler: method, params: payload }}));
              return;
            }}
            if (window.parent && window.parent !== window && typeof window.parent.postMessage === "function") {{
              window.parent.postMessage({{
                handler: method,
                params: payload,
                type: "vk-connect",
                webFrameId: webFrameId,
                connectVersion: "3.0.2"
              }}, "*");
              return;
            }}
            delete pending[requestId];
            reject({{ error_description: "Откройте приложение внутри VK." }});
          }} catch (error) {{
            delete pending[requestId];
            reject(error);
          }}
        }});
      }}
    }};
  }}

  var bridge = (window.vkBridge && (window.vkBridge.send ? window.vkBridge : window.vkBridge.default)) || createFallbackBridge();
  var groupId = {group_id};
  var vkMePath = {json.dumps(vk_me)};
  var serverFlow = {json.dumps(default_flow if default_flow in FLOWS else "")};
  var flowLabels = {json.dumps(flow_labels, ensure_ascii=False)};
  var currentFlow = null;
  var sending = false;
  var autoStarted = false;
  var permissionRequested = false;
  var dialogReady = false;
  var dialogOpened = false;
  var openAfterSendFromTap = false;
  var launchParamsQuery = window.location.search || "";
  var statusEl = document.getElementById("status");
  var leadEl = document.getElementById("lead");
  var actionsEl = document.getElementById("actions");
  var titleEl = document.getElementById("title");

  function dialogUrl() {{
    // Только прямой peer чата. vk.me/* даёт промежуточный экран
    // «Написать сообщение / Перейти к странице» — его не показываем.
    return "https://vk.com/im?sel=-" + groupId;
  }}

  function openViaWindow(url) {{
    try {{
      var w = window.open(url, "_blank", "noopener,noreferrer");
      if (w) return true;
    }} catch (_) {{}}
    try {{
      var a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      return true;
    }} catch (_) {{}}
    try {{
      if (window.top && window.top !== window) {{
        window.top.location.href = url;
        return true;
      }}
      window.location.href = url;
      return true;
    }} catch (_) {{}}
    return false;
  }}

  function setDialogLinkButton() {{
    var url = dialogUrl();
    uiBusy = false;
    var mobile = isMobilePlatform();
    actionsEl.querySelectorAll("[data-flow]").forEach(function (button) {{
      if (button.hidden || button.style.display === "none") return;
      if (button.tagName === "A") {{
        button.href = url;
        // На мобилке внутри VK надёжнее тот же webview, не _blank.
        if (mobile) {{
          button.removeAttribute("target");
        }} else {{
          button.target = "_blank";
          button.rel = "noopener noreferrer";
        }}
        button.textContent = "Открыть диалог VK";
        button.style.pointerEvents = "";
        button.style.opacity = "";
        button.removeAttribute("aria-busy");
        return;
      }}
      var a = document.createElement("a");
      a.className = button.className || "cta";
      a.href = url;
      if (!mobile) {{
        a.target = "_blank";
        a.rel = "noopener noreferrer";
      }}
      a.setAttribute("data-flow", button.getAttribute("data-flow") || "");
      a.textContent = "Открыть диалог VK";
      a.style.pointerEvents = "";
      a.style.opacity = "";
      button.replaceWith(a);
    }});
  }}

  function rememberLaunchParams(raw) {{
    if (!raw) return;
    if (typeof raw === "string") {{
      var s = raw.trim();
      if (!s) return;
      launchParamsQuery = s.charAt(0) === "?" ? s : ("?" + s);
      return;
    }}
    if (typeof raw !== "object") return;
    var parts = [];
    Object.keys(raw).forEach(function (key) {{
      var value = raw[key];
      if (value == null) return;
      if (!(key === "sign" || key.indexOf("vk_") === 0)) return;
      parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(String(value)));
    }});
    if (parts.length) {{
      launchParamsQuery = "?" + parts.join("&");
    }}
  }}

  rememberLaunchParams(window.location.search);

  function setStatus(text, ok) {{
    statusEl.hidden = !text;
    statusEl.textContent = text || "";
    statusEl.className = "status " + (ok ? "ok" : "err");
  }}

  function isMobilePlatform() {{
    var platform = new URLSearchParams(window.location.search || "").get("vk_platform") || "";
    var ua = navigator.userAgent || "";
    return /mobile_android|mobile_iphone|mobile_ipad|android|iphone|ipad/i.test(platform + " " + ua);
  }}

  function isAndroidPlatform() {{
    var platform = new URLSearchParams(window.location.search || "").get("vk_platform") || "";
    var ua = navigator.userAgent || "";
    return /mobile_android|android/i.test(platform + " " + ua);
  }}

  var FLOW_ALIASES = {{
    book: "booking",
    booking: "booking",
    standup_book: "booking",
    raffle: "raffle",
    rozygr: "raffle",
    rozygrysh: "raffle",
    standup_rozygr: "raffle",
    gift: "offline_gift",
    offline_gift: "offline_gift"
  }};

  function normalizeFlow(value) {{
    if (!value) return "";
    var key = String(value).trim();
    if (flowLabels[key]) return key;
    var aliased = FLOW_ALIASES[key] || "";
    return flowLabels[aliased] ? aliased : "";
  }}

  function parseHashParams(value) {{
    if (!value) return new URLSearchParams();
    var raw = String(value).replace(/^#/, "").replace(/^\\?/, "");
    try {{ raw = decodeURIComponent(raw); }} catch (_) {{}}
    raw = raw.replace(/^\\/+/, "").replace(/^\\?/, "");
    // "booking&cid=1" / "flow=booking&cid=1"
    if (raw && raw.indexOf("=") === -1 && raw.indexOf("&") === -1) {{
      return new URLSearchParams("flow=" + raw);
    }}
    if (raw && raw.indexOf("=") === -1 && raw.indexOf("&") !== -1) {{
      var parts = raw.split("&");
      parts[0] = "flow=" + parts[0];
      raw = parts.join("&");
    }}
    return new URLSearchParams(raw);
  }}

  function parseFlowValue(value) {{
    if (!value) return "";
    var params = parseHashParams(value);
    var direct = normalizeFlow(String(value).replace(/^#/, "").replace(/^\\?/, "").split("&")[0]);
    if (direct) return direct;
    return normalizeFlow(
      params.get("flow") || params.get("start") || params.get("start_param") || ""
    );
  }}

  function parseCidValue(value) {{
    if (!value) return "";
    var params = parseHashParams(value);
    return String(
      params.get("cid") || params.get("client_id") || params.get("ym_cid") || ""
    ).trim();
  }}

  function flowFromLocation() {{
    var search = new URLSearchParams(window.location.search || "");
    return (
      parseFlowValue(window.location.hash || "") ||
      parseFlowValue(search.get("flow") || "") ||
      parseFlowValue(search.get("hash") || "") ||
      parseFlowValue(search.get("vk_hash") || "") ||
      parseFlowValue(search.get("start_param") || "")
    );
  }}

  function cidFromLocation() {{
    var search = new URLSearchParams(window.location.search || "");
    return (
      parseCidValue(window.location.hash || "") ||
      String(search.get("cid") || search.get("client_id") || "").trim() ||
      parseCidValue(search.get("hash") || "") ||
      parseCidValue(search.get("vk_hash") || "") ||
      parseCidValue(search.get("start_param") || "")
    );
  }}

  function flowFromLaunchParams(data) {{
    if (!data) return "";
    return (
      parseFlowValue(data.hash || "") ||
      parseFlowValue(data.vk_hash || "") ||
      parseFlowValue(data.start_param || "") ||
      parseFlowValue(data.flow || "")
    );
  }}

  function cidFromLaunchParams(data) {{
    if (!data) return "";
    return (
      parseCidValue(data.hash || "") ||
      parseCidValue(data.vk_hash || "") ||
      parseCidValue(data.start_param || "") ||
      String(data.cid || data.client_id || "").trim()
    );
  }}

  var detectedCid = "";

  function resolveFlow(launchData) {{
    // Приоритет: hash/launch params из VK, и только потом server handoff.
    detectedCid = cidFromLaunchParams(launchData) || cidFromLocation() || detectedCid;
    return (
      flowFromLaunchParams(launchData) ||
      flowFromLocation() ||
      parseFlowValue(serverFlow)
    );
  }}

  function showCidDebug(source) {{
    var search = new URLSearchParams(window.location.search || "");
    var wantDebug =
      search.get("debug") === "1" ||
      (window.location.hash || "").indexOf("debug=1") !== -1;
    if (!wantDebug) return;
    detectedCid = detectedCid || cidFromLocation();
    var msg;
    if (!detectedCid) {{
      msg =
        "DEBUG: cid не найден. hash=" + (window.location.hash || "(пусто)") +
          " · src=" + (source || "");
      if (leadEl) leadEl.textContent = msg;
      setStatus(msg, false);
      return;
    }}
    msg =
      "DEBUG: cid=" + detectedCid +
        " · hash=" + (window.location.hash || "(пусто)") +
        " · src=" + (source || "location");
    if (leadEl) leadEl.textContent = msg;
    setStatus(msg, true);
  }}

  function showOnlyFlow(flow) {{
    var buttons = actionsEl.querySelectorAll("[data-flow]");
    buttons.forEach(function (button) {{
      var match = button.getAttribute("data-flow") === flow;
      button.hidden = !match;
      button.style.display = match ? "" : "none";
      if (match) {{
        button.textContent = flowLabels[flow].button;
      }}
    }});
  }}

  function setVisibleButtonText(text) {{
    var buttons = actionsEl.querySelectorAll("[data-flow]");
    buttons.forEach(function (button) {{
      if (!button.hidden && button.style.display !== "none") {{
        button.textContent = text;
      }}
    }});
  }}

  var uiBusy = false;

  function setUiBusy(on, label) {{
    uiBusy = !!on;
    actionsEl.querySelectorAll("[data-flow]").forEach(function (button) {{
      if (button.hidden || button.style.display === "none") return;
      if (on) {{
        button.textContent = label || "Отправляем…";
        button.style.pointerEvents = "none";
        button.style.opacity = "0.72";
        button.setAttribute("aria-busy", "true");
      }} else {{
        button.style.pointerEvents = "";
        button.style.opacity = "";
        button.removeAttribute("aria-busy");
      }}
    }});
  }}

  function setFlow(flow, singleButton) {{
    if (!flowLabels[flow]) return false;
    currentFlow = flow;
    titleEl.textContent = flowLabels[flow].headline;
    leadEl.textContent = flowLabels[flow].lead;
    actionsEl.hidden = false;
    if (singleButton) showOnlyFlow(flow);
    return true;
  }}

  function normalizeError(error) {{
    if (!error) return "Не удалось выполнить действие. Попробуйте ещё раз.";
    if (error.error_data && error.error_data.error_reason === "User denied") {{
      return "Вы запретили сообщения. Нажмите кнопку ещё раз и разрешите сообщения от сообщества.";
    }}
    return error.error_description || error.error_reason || error.message ||
      "Не удалось выполнить действие. Попробуйте ещё раз.";
  }}

  function openDialog(opts) {{
    opts = opts || {{}};
    var fromUserTap = !!opts.fromUserTap;
    if (!groupId) {{
      setStatus("Не задан id сообщества. Откройте диалог вручную.", false);
      return;
    }}
    // Только прямой peer чата — без vk.me / write- / intent / vk:// / VKWebAppClose.
    // Close без реального перехода возвращает к источнику ссылки (файл/пост).
    var chatUrl = "https://vk.com/im?sel=-" + groupId;
    var chatUrlRu = "https://vk.ru/im?sel=-" + groupId;
    var urls = [chatUrl, chatUrlRu];

    function viaBridge(url, ms) {{
      return withTimeout(bridge.send("VKWebAppOpenURL", {{ url: url }}), ms || 2500);
    }}

    var syncOk = false;
    var skipSync = !!opts.skipSync;
    // Десктоп: жест клика → window.open / <a>.
    if (fromUserTap && !skipSync && !isMobilePlatform()) {{
      syncOk = openViaWindow(chatUrl);
      if (syncOk) {{
        dialogOpened = true;
        setStatus("Открыли диалог. Если вкладка не появилась — разрешите всплывающие окна браузера.", true);
      }}
    }}

    // Мобилка: только OpenURL. Без Close — иначе выкидывает туда, откуда открыли ссылку.
    if (isMobilePlatform()) {{
      var waitMs = fromUserTap ? 1600 : 500;
      viaBridge(chatUrl, waitMs)
        .catch(function () {{ return viaBridge(chatUrlRu, waitMs); }})
        .then(function () {{
          dialogOpened = true;
        }})
        .catch(function () {{
          if (fromUserTap) {{
            try {{
              window.location.replace(chatUrl);
            }} catch (_) {{}}
          }}
          setDialogLinkButton();
          setStatus(
            "Сообщение уже в личке. Нажмите «Открыть диалог VK».",
            true
          );
        }});
      return;
    }}

    function tryUrl(index) {{
      if (index >= urls.length) {{
        if (fromUserTap && !syncOk && !dialogOpened) {{
          setStatus(
            "Не удалось открыть диалог автоматически. Нажмите «Открыть диалог VK» — текст уже в личке.",
            false
          );
        }}
        return;
      }}
      viaBridge(urls[index], 2500)
        .then(function () {{
          dialogOpened = true;
        }})
        .catch(function () {{
          tryUrl(index + 1);
        }});
    }}

    tryUrl(0);
  }}

  function markDialogReady(statusText) {{
    dialogReady = true;
    setDialogLinkButton();
    setStatus(
      statusText ||
        "Готово! Сообщение уже в личке. Если диалог не открылся — нажмите «Открыть диалог VK».",
      true
    );
  }}

  function requestPermissionAndSend(flow) {{
    if (permissionRequested || entrySent) return;
    permissionRequested = true;
    sending = false;
    setStatus("Открываем запрос VK…", true);
    var slowTimer = setTimeout(function () {{
      setStatus("Ждём ответ VK…", true);
    }}, 2200);
    withTimeout(bridge.send("VKWebAppAllowMessagesFromGroup", {{
      group_id: groupId,
      key: flow + ":" + Date.now()
    }}), 8000)
      .then(function (data) {{
        clearTimeout(slowTimer);
        // После «Разрешить» открываем диалог из того же жеста, потом шлём текст.
        if (data && data.result) {{
          openDialog({{ fromUserTap: true }});
          sendEntry(false);
          return;
        }}
        setStatus("Проверяем разрешение и отправляем сообщение…", true);
        openDialog({{ fromUserTap: true }});
        sendEntry(false);
      }})
      .catch(function (error) {{
        clearTimeout(slowTimer);
        if (isUserDenied(error)) {{
          setStatus(normalizeError(error), false);
          return;
        }}
        setStatus("VK не вернул ответ на разрешение. Проверяем и отправляем сообщение…", true);
        openDialog({{ fromUserTap: true }});
        sendEntry(false);
      }});
  }}

  function sendEntry(allowPermissionFallback) {{
    if (!currentFlow || sending || entrySent) return;
    sending = true;
    setUiBusy(true, "Отправляем…");
    setStatus("Отправляем в личку и открываем диалог…", true);
    if (!launchParamsQuery || launchParamsQuery.indexOf("vk_") === -1) {{
      sending = false;
      setUiBusy(false);
      setStatus(
        "Нет параметров запуска VK. Откройте приложение внутри VK (не в обычном браузере): vk.com/app" +
          "{_env_mini_app_id() or '54704296'}" +
          (groupId ? ("_-" + groupId) : ""),
        false
      );
      return;
    }}
    fetch("/vk-mini/entry", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{
        flow: currentFlow,
        cid: detectedCid || cidFromLocation() || "",
        hash: window.location.hash || "",
        launch_params: launchParamsQuery
      }})
    }})
      .then(function (r) {{
        return r.json().then(function (j) {{ return {{ ok: r.ok, status: r.status, j: j }}; }});
      }})
      .then(function (res) {{
        sending = false;
        if (res.ok && res.j && res.j.ok) {{
          entrySent = true;
          var fromTap = openAfterSendFromTap;
          openAfterSendFromTap = false;
          markDialogReady(
            "Готово! Сообщение в личке. Открываем диалог… Если не открылся — нажмите кнопку ниже."
          );
          // Сразу пробуем OpenURL; Close больше не зовём (возвращал к файлу/посту).
          if (!dialogOpened) {{
            openDialog({{ fromUserTap: !!fromTap }});
          }}
          return;
        }}
        if (allowPermissionFallback && res.status === 403) {{
          requestPermissionAndSend(currentFlow);
          return;
        }}
        setUiBusy(false);
        if (currentFlow && flowLabels[currentFlow]) {{
          setVisibleButtonText(flowLabels[currentFlow].button);
        }}
        setStatus((res.j && res.j.error) || "Не удалось отправить сообщение.", false);
      }})
      .catch(function () {{
        sending = false;
        setUiBusy(false);
        if (currentFlow && flowLabels[currentFlow]) {{
          setVisibleButtonText(flowLabels[currentFlow].button);
        }}
        setStatus("Сеть недоступна. Попробуйте ещё раз.", false);
      }});
  }}

  function isUserDenied(error) {{
    return !!(
      error &&
      (
        error.error_reason === "User denied" ||
        error.error_description === "User denied" ||
        (error.error_data && error.error_data.error_reason === "User denied")
      )
    );
  }}

  function withTimeout(promise, ms) {{
    return new Promise(function (resolve, reject) {{
      var timer = setTimeout(function () {{
        reject({{ error_description: "VK не ответил на запрос разрешения." }});
      }}, ms);
      promise.then(function (value) {{
        clearTimeout(timer);
        resolve(value);
      }}).catch(function (error) {{
        clearTimeout(timer);
        reject(error);
      }});
    }});
  }}

  var pendingStartTimer = null;
  var entrySent = false;

  function start(flow, opts) {{
    opts = opts || {{}};
    if (sending || entrySent) return;
    if (!setFlow(flow, true)) {{
      setStatus("Неизвестный сценарий.", false);
      return;
    }}
    // С тапа не открываем диалог сразу — сначала шлём текст, потом openDialog.
    // Иначе повторный тап во время автостарта уводит в «общий раздел».
    if (opts.fromUserTap) {{
      openAfterSendFromTap = true;
    }}
    setUiBusy(true, "Отправляем…");
    sendEntry(true);
  }}

  function autoStart(flow) {{
    if (!flow || entrySent || sending) return;
    if (!flowLabels[flow]) return;
    // До отправки можно поправить flow, если hash/launch params пришли позже handoff.
    if (autoStarted && currentFlow === flow) return;
    if (autoStarted && currentFlow && currentFlow !== flow && pendingStartTimer) {{
      clearTimeout(pendingStartTimer);
      pendingStartTimer = null;
    }} else if (autoStarted && currentFlow && currentFlow !== flow && !pendingStartTimer) {{
      // Уже ушли в send другого сценария — не перебиваем.
      if (sending || dialogReady) return;
    }}
    autoStarted = true;
    if (!setFlow(flow, true)) return;
    // Сразу гасим CTA, чтобы за ожидание не жали «Участвовать…» повторно.
    setUiBusy(true, "Отправляем…");
    setStatus("Отправляем в личку и открываем диалог…", true);
    if (pendingStartTimer) clearTimeout(pendingStartTimer);
    pendingStartTimer = setTimeout(function () {{
      pendingStartTimer = null;
      if (entrySent || sending) return;
      start(flow, {{ fromUserTap: false }});
    }}, 80);
  }}

  bridge.send("VKWebAppInit").catch(function () {{}});

  function handleFragmentEvent(event) {{
    var raw = event && (event.data || (event.detail && event.detail));
    if (!raw) return;
    if (typeof raw === "string") {{
      try {{ raw = JSON.parse(raw); }} catch (_) {{ return; }}
    }}
    var type = raw.type || (raw.detail && raw.detail.type);
    var data = raw.data || (raw.detail && raw.detail.data) || {{}};
    if (type !== "VKWebAppLocationChanged" && type !== "VKWebAppChangeFragment") return;
    var flow = parseFlowValue(data.location || data.hash || data.fragment || "");
    var fragCid = parseCidValue(data.location || data.hash || data.fragment || "");
    if (fragCid) detectedCid = fragCid;
    if (flow) {{
      showCidDebug("fragment");
      autoStart(flow);
    }}
  }}

  window.addEventListener("message", handleFragmentEvent);
  document.addEventListener("VKWebAppEvent", handleFragmentEvent);

  actionsEl.addEventListener("click", function (event) {{
    var button = event.target.closest("[data-flow]");
    if (!button) return;
    if (dialogReady) {{
      if (button.tagName === "A") {{
        openDialog({{ fromUserTap: true, skipSync: true }});
        return;
      }}
      event.preventDefault();
      openDialog({{ fromUserTap: true }});
      return;
    }}
    // Пока идёт автоотправка — игнорируем тапы (иначе уносит в общий раздел).
    if (uiBusy || sending || entrySent || autoStarted) {{
      event.preventDefault();
      return;
    }}
    event.preventDefault();
    start(button.getAttribute("data-flow"), {{ fromUserTap: true }});
  }});

  function showAllFlows() {{
    titleEl.textContent = "Moscow StandUp Show";
    leadEl.textContent = "Выберите действие — продолжение в личке VK.";
    actionsEl.hidden = false;
    actionsEl.querySelectorAll("[data-flow]").forEach(function (button) {{
      var flow = button.getAttribute("data-flow");
      if (!flowLabels[flow]) {{
        button.hidden = true;
        button.style.display = "none";
        return;
      }}
      button.hidden = false;
      button.style.display = "";
      button.textContent = flowLabels[flow].button;
    }});
  }}

  // Не стартуем сразу из server handoff: в VK hash часто приходит чуть позже
  // через GetLaunchParams, иначе все ссылки уезжают в booking.
  var earlyFlow = flowFromLocation();
  detectedCid = cidFromLocation();
  if (earlyFlow) {{
    setFlow(earlyFlow, true);
    showCidDebug("early");
  }} else {{
    showAllFlows();
    if (detectedCid || (window.location.hash || "").indexOf("cid=") !== -1) {{
      showCidDebug("early-no-flow");
    }}
  }}
  bridge.send("VKWebAppGetLaunchParams").then(function (data) {{
    rememberLaunchParams(data);
    var flow = resolveFlow(data) || currentFlow;
    showCidDebug("launch");
    if (flow) {{
      autoStart(flow);
    }} else {{
      showAllFlows();
    }}
  }}).catch(function () {{
    var flow = flowFromLocation() || currentFlow || parseFlowValue(serverFlow);
    detectedCid = detectedCid || cidFromLocation();
    showCidDebug("launch-fallback");
    if (flow) autoStart(flow);
    else showAllFlows();
  }});
}})();
</script>
"""

    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
  <meta http-equiv="Pragma" content="no-cache">
  <title>VK · Moscow StandUp Show</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@500;600;700&family=Pacifico&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg0: #070708;
      --gold: #e8c56a;
      --gold-deep: #c9a227;
      --text: #f7f3ea;
      --muted: #d2c4b0;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; min-height: 100vh; color: var(--text);
      font-family: Manrope, sans-serif;
      background:
        radial-gradient(ellipse 90% 55% at 50% 100%, rgba(180, 30, 45, .45) 0%, transparent 55%),
        radial-gradient(ellipse 70% 40% at 50% 0%, rgba(80, 10, 20, .55) 0%, transparent 50%),
        linear-gradient(180deg, #12080c 0%, var(--bg0) 45%, #14060a 100%);
    }}
    main {{
      width: min(440px, calc(100% - 32px)); margin: 0 auto; padding: 40px 0 72px;
    }}
    .brand {{
      margin: 0 0 18px; font-family: Pacifico, cursive; font-size: 22px;
      color: var(--gold); line-height: 1.2; text-shadow: 0 0 24px rgba(232, 197, 106, .25);
    }}
    h1 {{
      margin: 0 0 14px; font-size: clamp(28px, 7vw, 38px); font-weight: 700;
      line-height: 1.15; letter-spacing: -0.02em;
    }}
    .lead {{
      margin: 0 0 22px; font-size: 16px; line-height: 1.45; color: var(--muted);
    }}
    .actions {{ display: grid; gap: 12px; margin: 0 0 14px; }}
    [hidden] {{ display: none !important; }}
    .cta {{
      display: block; width: 100%; min-height: 52px; border: 0; border-radius: 14px;
      padding: 14px 16px; cursor: pointer; text-align: center; text-decoration: none;
      background: linear-gradient(180deg, #f0d48a 0%, var(--gold-deep) 100%);
      color: #1a1208; font: 700 16px Manrope, sans-serif;
      box-shadow: 0 8px 28px rgba(201, 162, 39, .28);
    }}
    .status {{
      font-size: 14px; margin: 0 0 14px; padding: 12px 14px; border-radius: 12px;
      background: rgba(255,255,255,.06); border: 1px solid rgba(232, 197, 106, .18);
    }}
    .status.ok {{ color: #d8f5d0; }}
    .status.err {{ color: #ffd0d0; }}
    .warn {{
      background: rgba(232, 197, 106, .1); border: 1px solid rgba(232, 197, 106, .35);
      padding: 14px 16px; border-radius: 12px; color: #ffe6b0;
    }}
  </style>
</head>
<body>
  <main>
    <p class="brand">Moscow StandUp Show</p>
    <h1 id="title">Бронирование</h1>
    {body}
  </main>
  <script src="https://unpkg.com/@vkontakte/vk-bridge/dist/browser.min.js"></script>
  {app_js}
</body>
</html>"""


async def mini_app_page(request: web.Request) -> web.Response:
    query_flow = str(request.query.get("flow") or "").strip()
    default_flow = query_flow if query_flow in FLOWS else _mini_flow_from_handoff(request)
    return _html_response(_mini_app_html(default_flow))


async def mini_app_start(request: web.Request) -> web.Response:
    flow_key = str(request.match_info.get("flow") or "").strip()
    if flow_key not in FLOWS:
        raise web.HTTPNotFound(text="Unknown VK Mini App flow")
    cid = _request_cid(request)
    _remember_mini_flow(request, flow_key)
    settings = load_vk_settings()
    target = _mini_app_vk_url(settings, flow_key, short=True, cid=cid)
    return _html_response(_mobile_vk_jump_html(flow_key, target))


def _verify_mini_launch_params(raw_query: str) -> tuple[bool, dict[str, str], str]:
    secret = _env_mini_app_secret()
    if not secret:
        return False, {}, "VK Mini App secret не настроен."

    query = (raw_query or "").strip()
    if query.startswith("?"):
        query = query[1:]
    params = dict(parse_qsl(query, keep_blank_values=True))
    sign = params.get("sign") or ""
    vk_params = sorted((k, v) for k, v in params.items() if k.startswith("vk_"))
    if not sign or not vk_params:
        return False, params, (
            "Нет подписанных параметров запуска VK. "
            "Откройте приложение из VK (vk.com/app…), не через обычный браузер."
        )

    expected_bytes = hmac.new(
        secret.encode("utf-8"),
        urlencode(vk_params).encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected = base64.urlsafe_b64encode(expected_bytes).decode("utf-8").rstrip("=")
    if not hmac.compare_digest(sign, expected):
        return False, params, "Некорректная подпись запуска VK."

    app_id = _env_mini_app_id()
    if app_id and str(params.get("vk_app_id") or "") != app_id:
        return False, params, "Mini App ID не совпадает с настройками сервера."

    try:
        vk_ts = int(params.get("vk_ts") or 0)
    except (TypeError, ValueError):
        vk_ts = 0
    ttl = int(os.getenv("VK_MINI_LAUNCH_TTL_SEC", "86400") or "86400")
    if vk_ts and ttl > 0 and abs(int(time.time()) - vk_ts) > ttl:
        return False, params, "Сессия VK устарела. Откройте приложение заново."

    return True, params, ""


def _cooldown_hit(vk_id: int, flow: str) -> bool:
    key = (int(vk_id), flow)
    now = time.monotonic()
    prev = _last_entry.get(key)
    if prev is not None and (now - prev) < _ENTRY_COOLDOWN_SEC:
        return True
    _last_entry[key] = now
    if len(_last_entry) > 5000:
        cutoff = now - _ENTRY_COOLDOWN_SEC
        for k, ts in list(_last_entry.items()):
            if ts < cutoff:
                _last_entry.pop(k, None)
    return False


def _clear_cooldown(vk_id: int, flow: str) -> None:
    _last_entry.pop((int(vk_id), flow), None)


async def entry_post(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Некорректный запрос."}, status=400)

    flow_key = str((data or {}).get("flow") or "").strip()
    flow = FLOWS.get(flow_key)
    if not flow:
        return web.json_response({"ok": False, "error": "Неизвестный сценарий."}, status=400)

    try:
        vk_id = int((data or {}).get("vk_id") or 0)
    except (TypeError, ValueError):
        vk_id = 0
    cid = str((data or {}).get("cid") or "").strip()
    logger.info("VK entry request vk_id=%s flow=%s cid=%s", vk_id, flow_key, cid or "-")
    if vk_id <= 0:
        return web.json_response({"ok": False, "error": "Не удалось определить VK id."}, status=400)

    if _cooldown_hit(vk_id, flow_key):
        return web.json_response(
            {"ok": True, "ok_soft": True, "error": "Сообщение уже отправляли — проверьте личку VK."}
        )

    settings = load_vk_settings()
    if not settings.is_configured:
        logger.error("VK entry: group token/id not configured")
        return web.json_response(
            {"ok": False, "error": "Сервис временно недоступен."},
            status=503,
        )

    client = VKClient(settings)
    try:
        await _send_flow_chain(client, settings, flow_key, vk_id)
    except VKAPIError as exc:
        _clear_cooldown(vk_id, flow_key)
        err = str(exc).lower()
        logger.warning("VK entry send failed vk_id=%s flow=%s: %s", vk_id, flow_key, exc)
        if "permission" in err or "901" in err or "deny" in err or "can't send" in err:
            return web.json_response(
                {
                    "ok": False,
                    "error": "Не можем написать вам. Разрешите сообщения сообществу и попробуйте снова.",
                },
                status=403,
            )
        return web.json_response(
            {
                "ok": False,
                "error": "Не удалось отправить сообщение. Попробуйте позже.",
                "detail": str(exc)[:180],
            },
            status=502,
        )
    except Exception:
        _clear_cooldown(vk_id, flow_key)
        logger.exception("VK entry unexpected vk_id=%s flow=%s", vk_id, flow_key)
        return web.json_response(
            {"ok": False, "error": "Не удалось отправить сообщение. Попробуйте позже."},
            status=502,
        )

    logger.info("VK entry ok vk_id=%s flow=%s cid=%s", vk_id, flow_key, cid or "-")
    return web.json_response({"ok": True, "cid": cid or None})


async def mini_entry_post(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Некорректный запрос."}, status=400)

    flow_key = str((data or {}).get("flow") or "").strip()
    if flow_key not in FLOWS:
        return web.json_response({"ok": False, "error": "Неизвестный сценарий."}, status=400)
    if flow_key not in MINI_APP_VISIBLE_FLOWS:
        return web.json_response(
            {"ok": False, "error": "Этот сценарий сейчас недоступен."},
            status=400,
        )

    ok, params, error = _verify_mini_launch_params(str((data or {}).get("launch_params") or ""))
    if not ok:
        logger.warning("VK mini entry bad launch params flow=%s error=%s", flow_key, error)
        return web.json_response({"ok": False, "error": error}, status=403)

    try:
        vk_id = int(params.get("vk_user_id") or 0)
    except (TypeError, ValueError):
        vk_id = 0
    cid = str((data or {}).get("cid") or "").strip()
    raw_hash = str((data or {}).get("hash") or "").strip()
    logger.info(
        "VK mini entry request vk_id=%s flow=%s cid=%s hash=%s",
        vk_id,
        flow_key,
        cid or "-",
        raw_hash or "-",
    )
    if vk_id <= 0:
        return web.json_response({"ok": False, "error": "Не удалось определить VK id."}, status=400)

    if _cooldown_hit(vk_id, flow_key):
        return web.json_response(
            {"ok": True, "ok_soft": True, "error": "Сообщение уже отправляли — проверьте личку VK."}
        )

    settings = load_vk_settings()
    if not settings.is_configured:
        logger.error("VK mini entry: group token/id not configured")
        return web.json_response(
            {"ok": False, "error": "Сервис временно недоступен."},
            status=503,
        )

    client = VKClient(settings)
    try:
        await _send_flow_chain(client, settings, flow_key, vk_id)
    except VKAPIError as exc:
        _clear_cooldown(vk_id, flow_key)
        err = str(exc).lower()
        logger.warning("VK mini entry send failed vk_id=%s flow=%s: %s", vk_id, flow_key, exc)
        if "permission" in err or "901" in err or "deny" in err or "can't send" in err:
            return web.json_response(
                {
                    "ok": False,
                    "error": "Не можем написать вам. Разрешите сообщения сообществу и попробуйте снова.",
                },
                status=403,
            )
        return web.json_response(
            {"ok": False, "error": "Не удалось отправить сообщение. Попробуйте позже."},
            status=502,
        )
    except Exception:
        _clear_cooldown(vk_id, flow_key)
        logger.exception("VK mini entry unexpected vk_id=%s flow=%s", vk_id, flow_key)
        return web.json_response(
            {"ok": False, "error": "Не удалось отправить сообщение. Попробуйте позже."},
            status=502,
        )

    logger.info("VK mini entry ok vk_id=%s flow=%s cid=%s", vk_id, flow_key, cid or "-")
    return web.json_response({"ok": True, "cid": cid or None})


def register_routes(app: web.Application) -> None:
    app.router.add_get("/vk-mini/start/{flow}", mini_app_start)
    app.router.add_get("/vk-mini", mini_app_page)
    app.router.add_get("/vk/booking", landing_booking)
    app.router.add_get("/vk/raffle", landing_raffle)
    app.router.add_get("/vk/offline-gift", landing_offline_gift)
    app.router.add_post("/vk/entry", entry_post)
    app.router.add_post("/vk-mini/entry", mini_entry_post)
