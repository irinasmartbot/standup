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
        "lead": "Разрешите сообщения сообществу — сразу пришлём в VK выбор формата шоу.",
        "ref": "standup_book",
    },
    "raffle": {
        "title": "Розыгрыш",
        "headline": "Участвовать в розыгрыше",
        "button": "Участвовать в розыгрыше",
        "lead": "Разрешите сообщения сообществу — сразу пришлём в VK старт розыгрыша.",
        "ref": "standup_rozygr",
    },
    "offline_gift": {
        "title": "Подарок",
        "headline": "Участвовать в розыгрыше на шоу",
        "button": "Подарок на шоу",
        "lead": "Разрешите сообщения сообществу — сразу пришлём в VK список на подарок.",
        "ref": "offline_gift",
    },
}

_ENTRY_COOLDOWN_SEC = 20.0
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


def _mini_app_vk_url(settings) -> str:
    app_id = _env_mini_app_id()
    if not app_id:
        app_id = "54704296"
    group_suffix = f"_-{int(settings.group_id)}" if settings.group_id else ""
    return f"https://vk.com/app{app_id}{group_suffix}"


def _formats_keyboard() -> str:
    kb = VKKeyboardBuilder(inline=True)
    kb.button("STANDUP BEST", {"cmd": "best"}, color="primary")
    kb.button("Хитлото", {"cmd": "hitloto"}, color="primary")
    kb.button("StandUp Проверка материала", {"cmd": "check"}, color="primary")
    kb.button("В главное меню", {"cmd": "main_menu"})
    kb.adjust(1)
    return kb.as_json()


async def _send_flow_chain(client: VKClient, settings, flow_key: str, vk_id: int) -> None:
    """Сразу ветка бота — без сообщения «нажмите кнопку ниже»."""
    from bot.handlers.formats import FORMATS_TEXT
    from bot.vk import raffle as vk_raffle

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
        await client.send_message(vk_id, FORMATS_TEXT, keyboard=_formats_keyboard())
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
        title = str(event.get("title") or event.get("venue") or "шоу")
        await client.send_message(
            vk_id,
            (
                "🎁 <b>Розыгрыш подарка на шоу</b>\n\n"
                f"<b>Сегодня:</b> {title}\n\n"
                "Нажми кнопку ниже, чтобы попасть в список участников."
            ),
            keyboard=kb.as_json(),
        )
        return
    kb = VKKeyboardBuilder(inline=True)
    for event in events[:8]:
        label = str(event.get("title") or event.get("venue") or f"Шоу #{event.get('id')}")[:40]
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
            "Выбери мероприятие, на котором вы сейчас находитесь:"
        ),
        keyboard=kb.as_json(),
    )


def _landing_html(flow_key: str) -> str:
    flow = FLOWS[flow_key]
    settings = load_vk_settings()
    app_id = _env_app_id()
    group_id = settings.group_id or 0
    ready = bool(app_id and group_id and settings.group_token)

    if not ready:
        body = """
        <p class="warn">Страница ещё настраивается. Загляните чуть позже или напишите менеджеру.</p>
        """
        widget_js = ""
    else:
        body = f"""
        <p class="lead">{escape(flow["lead"])}</p>
        <div id="vk_allow_messages_from_community" class="widget"></div>
        <p id="status" class="status" hidden></p>
        <button type="button" id="openApp" class="cta" hidden>Открыть приложение VK</button>
        <p class="hint">Если сверху уже «Запретить уведомления» — нажмите это, затем снова разрешите. Так VK отдаёт ваш id без входа в браузере.</p>
        """
        ref_value = str(flow["ref"])
        widget_js = f"""
<script src="https://vk.com/js/api/openapi.js?169"></script>
<script src="https://unpkg.com/@vkontakte/vk-bridge/dist/browser.min.js"></script>
<script>
(function () {{
  var flow = {json.dumps(flow_key)};
  var appId = {int(app_id)};
  var groupId = {int(group_id)};
  var ref = {json.dumps(ref_value)};
  var vkId = null;
  var sending = false;
  var statusEl = document.getElementById("status");
  var openAppBtn = document.getElementById("openApp");

  function setStatus(text, ok) {{
    statusEl.hidden = !text;
    statusEl.textContent = text || "";
    statusEl.className = "status " + (ok ? "ok" : "err");
  }}

  function normalizeId(userId) {{
    if (userId == null) return null;
    if (typeof userId === "object") {{
      if (userId.user_id != null) return String(userId.user_id);
      if (userId.id != null) return String(userId.id);
      if (userId.mid != null) return String(userId.mid);
      return null;
    }}
    var s = String(userId).trim();
    return s && s !== "0" && s !== "undefined" && s !== "null" ? s : null;
  }}

  function openVkAppOnly() {{
    var ua = navigator.userAgent || "";
    if (/Android/i.test(ua)) {{
      window.location.href =
        "intent://vk.com/write-" + groupId +
        "#Intent;scheme=https;package=com.vkontakte.android;end";
      return;
    }}
    if (/iPhone|iPad|iPod/i.test(ua)) {{
      window.location.href = "vk://vk.com/write-" + groupId;
      return;
    }}
    // Только десктоп — сайт VK.
    window.location.href = "https://vk.com/write-" + groupId + "?ref=" + encodeURIComponent(ref);
  }}

  function afterSendOk() {{
    setStatus("Готово! Сообщение уже в приложении VK. Нажмите кнопку, если чат не открылся сам.", true);
    openAppBtn.hidden = false;
    setTimeout(openVkAppOnly, 400);
  }}

  openAppBtn.addEventListener("click", openVkAppOnly);

  function sendEntry() {{
    if (!vkId) {{
      setStatus("Не получили id от VK. Нажмите «Запретить уведомления», затем снова разрешите.", false);
      return;
    }}
    if (sending) return;
    sending = true;
    setStatus("Отправляем сценарий в VK…", true);
    fetch("/vk/entry", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ vk_id: Number(vkId), flow: flow }})
    }})
      .then(function (r) {{
        return r.json().then(function (j) {{ return {{ ok: r.ok, status: r.status, j: j }}; }});
      }})
      .then(function (res) {{
        sending = false;
        if (res.ok && res.j && res.j.ok) {{
          afterSendOk();
          return;
        }}
        var err = (res.j && res.j.error) ? res.j.error : "";
        setStatus(err || "Не удалось отправить. Попробуйте ещё раз.", false);
      }})
      .catch(function () {{
        sending = false;
        setStatus("Сеть недоступна. Попробуйте ещё раз.", false);
      }});
  }}

  function onAllowed(userId) {{
    var id = normalizeId(userId);
    console.log("allow-messages allowed", userId, id);
    if (!id) {{
      setStatus("VK не передал id. Нажмите «Запретить», затем снова «Разрешить».", false);
      return;
    }}
    vkId = id;
    sendEntry();
  }}

  VK.init({{ apiId: appId, onlyWidgets: true }});
  if (VK.Observer && VK.Observer.subscribe) {{
    VK.Observer.subscribe("widgets.allowMessagesFromCommunity.allowed", onAllowed);
    VK.Observer.subscribe("widgets.allowMessagesFromCommunity.denied", function () {{
      setStatus("Снова нажмите виджет и разрешите сообщения.", false);
    }});
    VK.Observer.subscribe("widgets.allowMessagesFromCommunity.declined", function () {{
      setStatus("Снова нажмите виджет и разрешите сообщения.", false);
    }});
  }}
  if (typeof VK.addCallback === "function") {{
    VK.addCallback("widgets.allowMessagesFromCommunity.allowed", onAllowed);
  }}
  VK.Widgets.AllowMessagesFromCommunity(
    "vk_allow_messages_from_community",
    {{ height: 30 }},
    groupId
  );
}})();
</script>
"""

    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
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
    .widget {{ margin: 0 0 14px; min-height: 36px; }}
    .hint {{
      margin: 16px 0 0; font-size: 13px; line-height: 1.4; color: #a89884;
    }}
    .cta {{
      display: block; width: 100%; min-height: 52px; border: 0; border-radius: 14px;
      margin: 0 0 12px; padding: 14px 16px; cursor: pointer;
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
    <h1>{escape(flow["headline"])}</h1>
    {body}
  </main>
  {widget_js}
</body>
</html>"""


async def landing_booking(_: web.Request) -> web.Response:
    return web.Response(text=_landing_html("booking"), content_type="text/html")


async def landing_raffle(_: web.Request) -> web.Response:
    return web.Response(text=_landing_html("raffle"), content_type="text/html")


async def landing_offline_gift(_: web.Request) -> web.Response:
    return web.Response(text=_landing_html("offline_gift"), content_type="text/html")


def _mini_app_html(default_flow: str = "") -> str:
    settings = load_vk_settings()
    group_id = int(settings.group_id or 0)
    ready = bool(group_id and settings.group_token)
    flow_labels = {
        key: {
            "headline": value["headline"],
            "button": value["button"],
            "lead": value["lead"],
        }
        for key, value in FLOWS.items()
    }

    if not ready:
        body = """
        <p class="warn">Мини-приложение ещё настраивается. Загляните чуть позже или напишите менеджеру.</p>
        """
        app_js = ""
    else:
        body = """
        <p id="lead" class="lead">Откройте корректную ссылку нужного сценария.</p>
        <div class="actions" id="actions" hidden>
          <button type="button" class="cta" data-flow="booking">Забронировать места</button>
          <button type="button" class="cta" data-flow="raffle">Участвовать в розыгрыше</button>
          <button type="button" class="cta" data-flow="offline_gift">Подарок на шоу</button>
        </div>
        <p id="status" class="status" hidden></p>
        <p class="hint">VK попросит разрешить сообщения от сообщества. После этого бот пришлёт продолжение в личку.</p>
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

  var bridge = window.vkBridge || createFallbackBridge();
  var groupId = {group_id};
  var serverFlow = {json.dumps(default_flow if default_flow in FLOWS else "")};
  var flowLabels = {json.dumps(flow_labels, ensure_ascii=False)};
  var currentFlow = null;
  var sending = false;
  var autoStarted = false;
  var permissionRequested = false;
  var statusEl = document.getElementById("status");
  var leadEl = document.getElementById("lead");
  var actionsEl = document.getElementById("actions");
  var titleEl = document.getElementById("title");

  function setStatus(text, ok) {{
    statusEl.hidden = !text;
    statusEl.textContent = text || "";
    statusEl.className = "status " + (ok ? "ok" : "err");
  }}

  function parseFlowValue(value) {{
    if (!value) return "";
    var raw = String(value).replace(/^#/, "").replace(/^\\?/, "");
    try {{ raw = decodeURIComponent(raw); }} catch (_) {{}}
    raw = raw.replace(/^\\/+/, "").replace(/^\\?/, "");
    if (flowLabels[raw]) return raw;
    var params = new URLSearchParams(raw);
    var flow = params.get("flow") || params.get("start") || params.get("start_param") || "";
    if (flowLabels[flow]) return flow;
    var aliases = {{
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
    return aliases[raw] || "";
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

  function flowFromLaunchParams(data) {{
    if (!data) return "";
    return (
      parseFlowValue(data.hash || "") ||
      parseFlowValue(data.vk_hash || "") ||
      parseFlowValue(data.start_param || "") ||
      parseFlowValue(data.flow || "")
    );
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

  function openDialog() {{
    var webUrl = "https://vk.com/write-" + groupId;
    var appUrl = "vk://vk.com/write-" + groupId;
    var platform = new URLSearchParams(window.location.search || "").get("vk_platform") || "";
    if (/mobile_android|mobile_iphone|mobile_ipad|android|iphone|ipad/i.test(platform)) {{
      window.location.href = appUrl;
    }} else {{
      try {{
        window.top.location.href = webUrl;
      }} catch (_) {{
        window.location.href = webUrl;
      }}
    }}
    setTimeout(function () {{
      bridge.send("VKWebAppClose", {{ status: "success" }}).catch(function () {{}});
    }}, 1200);
  }}

  function requestPermissionAndSend(flow) {{
    if (permissionRequested) return;
    permissionRequested = true;
    sending = false;
    setStatus("VK попросит разрешить сообщения. После разрешения сразу отправим продолжение…", true);
    var slowTimer = setTimeout(function () {{
      setStatus("Ждём разрешение VK. Обычно это занимает несколько секунд.", true);
    }}, 2200);
    withTimeout(bridge.send("VKWebAppAllowMessagesFromGroup", {{
      group_id: groupId,
      key: flow + ":" + Date.now()
    }}), 8000)
      .then(function (data) {{
        clearTimeout(slowTimer);
        if (data && data.result) {{
          sendEntry(false);
          return;
        }}
        setStatus("Разрешите сообщения, чтобы бот смог написать вам.", false);
      }})
      .catch(function (error) {{
        clearTimeout(slowTimer);
        if (isUserDenied(error)) {{
          setStatus(normalizeError(error), false);
          return;
        }}
        setStatus("VK не вернул ответ на разрешение. Нажмите кнопку ещё раз или проверьте личку.", false);
      }});
  }}

  function sendEntry(allowPermissionFallback) {{
    if (!currentFlow || sending) return;
    sending = true;
    setStatus("Отправляем сообщение в VK. Обычно оно приходит в течение пары секунд…", true);
    fetch("/vk-mini/entry", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{
        flow: currentFlow,
        launch_params: window.location.search || ""
      }})
    }})
      .then(function (r) {{
        return r.json().then(function (j) {{ return {{ ok: r.ok, status: r.status, j: j }}; }});
      }})
      .then(function (res) {{
        sending = false;
        if (res.ok && res.j && res.j.ok) {{
          setStatus("Готово! Сообщение уже отправлено в личку VK.", true);
          setTimeout(openDialog, 350);
          return;
        }}
        if (allowPermissionFallback && res.status === 403) {{
          requestPermissionAndSend(currentFlow);
          return;
        }}
        setStatus((res.j && res.j.error) || "Не удалось отправить сообщение.", false);
      }})
      .catch(function () {{
        sending = false;
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

  function messagesAlreadyAllowed() {{
    return new URLSearchParams(window.location.search || "").get("vk_are_notifications_enabled") === "1";
  }}

  function start(flow) {{
    if (!setFlow(flow, true)) {{
      setStatus("Неизвестный сценарий.", false);
      return;
    }}
    sendEntry(true);
  }}

  function autoStart(flow) {{
    if (!flow || autoStarted) return;
    autoStarted = true;
    setFlow(flow, true);
    setTimeout(function () {{
      start(flow);
    }}, 250);
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
    if (flow && !currentFlow) setFlow(flow, true);
  }}

  window.addEventListener("message", handleFragmentEvent);
  document.addEventListener("VKWebAppEvent", handleFragmentEvent);

  actionsEl.addEventListener("click", function (event) {{
    var button = event.target.closest("[data-flow]");
    if (!button) return;
    start(button.getAttribute("data-flow"));
  }});

  var initialFlow = flowFromLocation() || parseFlowValue(serverFlow);
  if (initialFlow) {{
    setFlow(initialFlow, true);
    autoStart(initialFlow);
  }}
  bridge.send("VKWebAppGetLaunchParams").then(function (data) {{
    var flow = flowFromLaunchParams(data);
    if (flow) autoStart(flow);
  }}).catch(function () {{}});
}})();
</script>
"""

    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
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
      padding: 14px 16px; cursor: pointer;
      background: linear-gradient(180deg, #f0d48a 0%, var(--gold-deep) 100%);
      color: #1a1208; font: 700 16px Manrope, sans-serif;
      box-shadow: 0 8px 28px rgba(201, 162, 39, .28);
    }}
    .hint {{
      margin: 16px 0 0; font-size: 13px; line-height: 1.4; color: #a89884;
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
    <h1 id="title">Быстрый вход в VK</h1>
    {body}
  </main>
  {app_js}
</body>
</html>"""


async def mini_app_page(request: web.Request) -> web.Response:
    query_flow = str(request.query.get("flow") or "").strip()
    default_flow = query_flow if query_flow in FLOWS else _mini_flow_from_handoff(request)
    return web.Response(text=_mini_app_html(default_flow), content_type="text/html")


async def mini_app_start(request: web.Request) -> web.Response:
    flow_key = str(request.match_info.get("flow") or "").strip()
    if flow_key not in FLOWS:
        raise web.HTTPNotFound(text="Unknown VK Mini App flow")
    _remember_mini_flow(request, flow_key)
    settings = load_vk_settings()
    raise web.HTTPFound(_mini_app_vk_url(settings))


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
        return False, params, "Нет подписанных параметров запуска VK."

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
    logger.info("VK entry request vk_id=%s flow=%s", vk_id, flow_key)
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
        logger.exception("VK entry unexpected vk_id=%s flow=%s", vk_id, flow_key)
        return web.json_response(
            {"ok": False, "error": "Не удалось отправить сообщение. Попробуйте позже."},
            status=502,
        )

    logger.info("VK entry ok vk_id=%s flow=%s", vk_id, flow_key)
    return web.json_response({"ok": True})


async def mini_entry_post(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Некорректный запрос."}, status=400)

    flow_key = str((data or {}).get("flow") or "").strip()
    if flow_key not in FLOWS:
        return web.json_response({"ok": False, "error": "Неизвестный сценарий."}, status=400)

    ok, params, error = _verify_mini_launch_params(str((data or {}).get("launch_params") or ""))
    if not ok:
        logger.warning("VK mini entry bad launch params flow=%s error=%s", flow_key, error)
        return web.json_response({"ok": False, "error": error}, status=403)

    try:
        vk_id = int(params.get("vk_user_id") or 0)
    except (TypeError, ValueError):
        vk_id = 0
    logger.info("VK mini entry request vk_id=%s flow=%s", vk_id, flow_key)
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
        logger.exception("VK mini entry unexpected vk_id=%s flow=%s", vk_id, flow_key)
        return web.json_response(
            {"ok": False, "error": "Не удалось отправить сообщение. Попробуйте позже."},
            status=502,
        )

    logger.info("VK mini entry ok vk_id=%s flow=%s", vk_id, flow_key)
    return web.json_response({"ok": True})


def register_routes(app: web.Application) -> None:
    app.router.add_get("/vk-mini/start/{flow}", mini_app_start)
    app.router.add_get("/vk-mini", mini_app_page)
    app.router.add_get("/vk/booking", landing_booking)
    app.router.add_get("/vk/raffle", landing_raffle)
    app.router.add_get("/vk/offline-gift", landing_offline_gift)
    app.router.add_post("/vk/entry", entry_post)
    app.router.add_post("/vk-mini/entry", mini_entry_post)
