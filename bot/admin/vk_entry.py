"""Public VK entry landings on go.moscowstandupshow.ru (no admin auth).

Виджет OpenAPI «Разрешить сообщения» → POST /vk/entry → бот сам пишет
в личку кнопку нужной ветки (без «начать»).

GET  /vk/booking | /vk/raffle | /vk/offline-gift
POST /vk/entry   JSON { "vk_id": int, "flow": "booking"|"raffle"|"offline_gift" }
"""

from __future__ import annotations

import json
import logging
import os
import time
from html import escape
from typing import Any

from aiohttp import web

from bot.vk.client import VKAPIError, VKClient
from bot.vk.config import load_vk_settings
from bot.vk.keyboards import VKKeyboardBuilder

logger = logging.getLogger(__name__)

FLOWS: dict[str, dict[str, Any]] = {
    "booking": {
        "title": "Бронирование",
        "headline": "Забронировать места",
        "lead": (
            "Разрешите сообщения от сообщества — и мы сразу отправим "
            "в личку VK кнопку бронирования."
        ),
        "cta": "Отправить кнопку ещё раз",
        "cmd": "book",
        "button": "Забронировать места",
        "message": (
            "Отлично! Нажмите кнопку ниже, чтобы забронировать места "
            "на <b>Проверку материала</b> 👇"
        ),
    },
    "raffle": {
        "title": "Розыгрыш",
        "headline": "Участвовать в розыгрыше",
        "lead": (
            "Разрешите сообщения от сообщества — пришлём в личку "
            "инструкцию и кнопки розыгрыша."
        ),
        "cta": "Отправить инструкцию ещё раз",
        "cmd": "raffle",
        "button": "Участвовать в розыгрыше",
        "message": "Нажмите кнопку ниже, чтобы начать участие в розыгрыше 👇",
    },
    "offline_gift": {
        "title": "Подарок",
        "headline": "Офлайн-розыгрыш подарка",
        "lead": (
            "Разрешите сообщения — добавим вас в список участников "
            "(нужна подписка на сообщество)."
        ),
        "cta": "Отправить кнопку ещё раз",
        "cmd": "offline_gift",
        "button": "Участвовать в розыгрыше подарка",
        "message": (
            "Нажмите кнопку ниже, чтобы выбрать шоу и попасть в список на подарок 👇"
        ),
    },
}

_ENTRY_COOLDOWN_SEC = 20.0
_last_entry: dict[tuple[int, str], float] = {}


def _env_app_id() -> str:
    return (os.getenv("VK_OPENAPI_APP_ID") or "").strip()


def _payload_keyboard(cmd: str, label: str) -> str:
    kb = VKKeyboardBuilder(inline=True)
    kb.button(label, {"cmd": cmd}, color="primary")
    kb.adjust(1)
    return kb.as_json()


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
        <button type="button" id="cta" class="cta" disabled>{escape(flow["cta"])}</button>
        """
        widget_js = f"""
<script src="https://vk.com/js/api/openapi.js?169"></script>
<script>
(function () {{
  var flow = {json.dumps(flow_key)};
  var vkId = null;
  var statusEl = document.getElementById("status");
  var cta = document.getElementById("cta");
  var sending = false;

  function setStatus(text, ok) {{
    statusEl.hidden = !text;
    statusEl.textContent = text || "";
    statusEl.className = "status " + (ok ? "ok" : "err");
  }}

  function sendEntry() {{
    if (!vkId || sending) return;
    sending = true;
    cta.disabled = true;
    setStatus("Отправляем сообщение…", true);
    fetch("/vk/entry", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ vk_id: vkId, flow: flow }})
    }})
      .then(function (r) {{ return r.json().then(function (j) {{ return {{ ok: r.ok, j: j }}; }}); }})
      .then(function (res) {{
        sending = false;
        if (res.ok && res.j && res.j.ok) {{
          setStatus("Готово! Откройте личные сообщения с сообществом в VK.", true);
          cta.disabled = false;
        }} else {{
          var msg = (res.j && res.j.error) ? res.j.error : "Не удалось отправить. Попробуйте ещё раз.";
          setStatus(msg, false);
          cta.disabled = false;
        }}
      }})
      .catch(function () {{
        sending = false;
        setStatus("Сеть недоступна. Попробуйте ещё раз.", false);
        cta.disabled = false;
      }});
  }}

  function onAllowed(userId) {{
    if (!userId) return;
    vkId = userId;
    cta.disabled = false;
    setStatus("Сообщения разрешены — отправляем кнопку в VK…", true);
    sendEntry();
  }}

  VK.init({{ apiId: {int(app_id)} }});
  VK.Widgets.AllowMessagesFromCommunity(
    "vk_allow_messages_from_community",
    {{ height: 30 }},
    {int(group_id)}
  );
  VK.Observer.subscribe("widgets.allowMessagesFromCommunity.allowed", onAllowed);
  VK.Observer.subscribe("widgets.allowMessagesFromCommunity.declined", function () {{
    vkId = null;
    cta.disabled = false;
    setStatus("Снова нажмите виджет и разрешите сообщения — тогда отправим кнопку в VK.", false);
  }});

  // Если сообщения уже были разрешены раньше, событие allowed при загрузке
  // не приходит — пробуем сессию OpenAPI и подсказываем перещёлкнуть виджет.
  try {{
    VK.Auth.getLoginStatus(function (resp) {{
      if (resp && resp.session && resp.session.mid) {{
        onAllowed(resp.session.mid);
      }}
    }});
  }} catch (e) {{}}

  setTimeout(function () {{
    if (!vkId) {{
      cta.disabled = false;
      setStatus(
        "Если сверху уже «Запретить уведомления» — нажмите это, затем снова разрешите. После разрешения сообщение уйдёт само.",
        true
      );
    }}
  }}, 700);

  cta.addEventListener("click", function () {{
    if (!vkId) {{
      setStatus(
        "Сначала разрешите сообщения виджетом. Если уже разрешено — нажмите «Запретить», потом снова разрешите.",
        false
      );
      return;
    }}
    sendEntry();
  }});
}})();
</script>
"""

    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(flow["title"])} · Moscow StandUp Show</title>
  <style>
    :root {{ color-scheme: light; }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; min-height: 100vh; font-family: Georgia, "Times New Roman", serif;
      background: linear-gradient(165deg, #1a1f2e 0%, #2c1810 45%, #1a1f2e 100%);
      color: #f5f0e8;
    }}
    main {{
      width: min(440px, calc(100% - 32px)); margin: 0 auto; padding: 48px 0 64px;
    }}
    .brand {{
      font-family: system-ui, sans-serif; font-size: 13px; letter-spacing: .08em;
      text-transform: uppercase; color: #c4b5a0; margin: 0 0 20px;
    }}
    h1 {{
      margin: 0 0 12px; font-size: clamp(28px, 6vw, 36px); font-weight: 700;
      line-height: 1.15; color: #fffaf3;
    }}
    .lead {{ margin: 0 0 28px; font-family: system-ui, sans-serif; font-size: 16px;
      line-height: 1.45; color: #e8dfd2; }}
    .widget {{ margin: 0 0 16px; min-height: 36px; }}
    .cta {{
      display: block; width: 100%; height: 52px; border: 0; border-radius: 10px;
      background: #e8a87c; color: #1a120c; font: 600 16px system-ui, sans-serif;
      cursor: pointer;
    }}
    .cta:disabled {{ opacity: .45; cursor: not-allowed; }}
    .status {{
      font-family: system-ui, sans-serif; font-size: 14px; margin: 0 0 14px;
      padding: 10px 12px; border-radius: 8px; background: rgba(255,255,255,.08);
    }}
    .status.ok {{ color: #c8f0c8; }}
    .status.err {{ color: #ffc9c9; }}
    .warn {{
      font-family: system-ui, sans-serif; background: rgba(255,200,120,.12);
      border: 1px solid rgba(255,200,120,.35); padding: 14px 16px; border-radius: 10px;
      color: #ffe6c0;
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
    keyboard = _payload_keyboard(flow["cmd"], flow["button"])
    try:
        await client.send_message(vk_id, flow["message"], keyboard=keyboard)
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
            {"ok": False, "error": "Не удалось отправить сообщение. Попробуйте позже."},
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


def register_routes(app: web.Application) -> None:
    app.router.add_get("/vk/booking", landing_booking)
    app.router.add_get("/vk/raffle", landing_raffle)
    app.router.add_get("/vk/offline-gift", landing_offline_gift)
    app.router.add_post("/vk/entry", entry_post)
