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
        "lead": "Разрешите сообщения — откроем диалог в VK и сразу пришлём кнопку бронирования.",
        "cta": "Открыть диалог в VK",
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
        "lead": "Разрешите сообщения — откроем диалог в VK и сразу пришлём инструкцию розыгрыша.",
        "cta": "Открыть диалог в VK",
        "cmd": "raffle",
        "button": "Участвовать в розыгрыше",
        "message": "Нажмите кнопку ниже, чтобы начать участие в розыгрыше 👇",
    },
    "offline_gift": {
        "title": "Подарок",
        "headline": "Офлайн-розыгрыш подарка",
        "lead": "Разрешите сообщения — откроем диалог в VK и добавим вас в список (нужна подписка).",
        "cta": "Открыть диалог в VK",
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
        <div class="steps">
          <p><span>1</span> Разрешите сообщения сообществу</p>
          <p><span>2</span> Мы сразу откроем диалог в VK с кнопкой</p>
        </div>
        <div id="vk_allow_messages_from_community" class="widget"></div>
        <p id="status" class="status" hidden></p>
        <button type="button" id="cta" class="cta">{escape(flow["cta"])}</button>
        """
        # После успешной отправки — редирект в диалог (как у Salebot).
        dialog_url = f"https://vk.com/write-{int(group_id)}"
        widget_js = f"""
<script src="https://vk.com/js/api/openapi.js?169"></script>
<script>
(function () {{
  var flow = {json.dumps(flow_key)};
  var dialogUrl = {json.dumps(dialog_url)};
  var vkId = null;
  var statusEl = document.getElementById("status");
  var cta = document.getElementById("cta");
  var sending = false;

  function setStatus(text, ok) {{
    statusEl.hidden = !text;
    statusEl.textContent = text || "";
    statusEl.className = "status " + (ok ? "ok" : "err");
  }}

  function normalizeId(userId) {{
    if (userId == null) return null;
    if (typeof userId === "object" && userId.id != null) return String(userId.id);
    return String(userId);
  }}

  function goToDialog() {{
    setStatus("Готово! Открываем диалог в VK…", true);
    window.location.href = dialogUrl;
  }}

  function sendEntry() {{
    if (!vkId || sending) return;
    sending = true;
    cta.disabled = true;
    setStatus("Отправляем сообщение в VK…", true);
    fetch("/vk/entry", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ vk_id: Number(vkId), flow: flow }})
    }})
      .then(function (r) {{ return r.json().then(function (j) {{ return {{ ok: r.ok, j: j }}; }}); }})
      .then(function (res) {{
        sending = false;
        if (res.ok && res.j && res.j.ok) {{
          goToDialog();
          return;
        }}
        cta.disabled = false;
        var msg = (res.j && res.j.error) ? res.j.error : "Не удалось отправить. Нажмите кнопку ещё раз.";
        setStatus(msg, false);
      }})
      .catch(function () {{
        sending = false;
        cta.disabled = false;
        setStatus("Сеть недоступна. Нажмите кнопку ещё раз.", false);
      }});
  }}

  function onAllowed(userId) {{
    var id = normalizeId(userId);
    if (!id || id === "0") return;
    vkId = id;
    setStatus("Отправляем сообщение в VK…", true);
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
    setStatus("Нажмите кнопку VK выше и выберите «Разрешить».", false);
  }});

  try {{
    VK.Auth.getLoginStatus(function (resp) {{
      if (resp && resp.session && resp.session.mid) {{
        onAllowed(resp.session.mid);
      }}
    }});
  }} catch (e) {{}}

  cta.addEventListener("click", function () {{
    if (!vkId) {{
      setStatus("Сначала нажмите кнопку VK выше и разрешите сообщения.", false);
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
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@500;600;700&family=Pacifico&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg0: #070708;
      --bg1: #2a060c;
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
    .steps {{
      margin: 0 0 22px; display: grid; gap: 8px;
    }}
    .steps p {{
      margin: 0; display: flex; gap: 10px; align-items: center;
      font-size: 14px; color: var(--muted);
    }}
    .steps span {{
      flex: 0 0 22px; height: 22px; border-radius: 999px;
      display: inline-flex; align-items: center; justify-content: center;
      background: rgba(232, 197, 106, .15); color: var(--gold); font-size: 12px; font-weight: 700;
    }}
    .widget {{ margin: 0 0 14px; min-height: 36px; }}
    .cta {{
      display: block; width: 100%; min-height: 52px; border: 0; border-radius: 14px;
      padding: 14px 16px; cursor: pointer;
      background: linear-gradient(180deg, #f0d48a 0%, var(--gold-deep) 100%);
      color: #1a1208; font: 700 16px Manrope, sans-serif;
      box-shadow: 0 8px 28px rgba(201, 162, 39, .28);
    }}
    .cta:disabled {{ opacity: .45; cursor: not-allowed; }}
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
