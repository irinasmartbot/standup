"""Public VK entry landings on go.moscowstandupshow.ru (no admin auth).

Короткий путь:
1) виджет «Разрешить сообщения» → если VK отдал vk_id, сразу шлём ветку бота
2) иначе один клик VK ID (без «войти в другой аккаунт»)
3) открываем приложение VK

GET  /vk/booking | /vk/raffle | /vk/offline-gift
POST /vk/entry
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
        "lead": "Разрешите сообщения — сразу откроем VK с выбором формата шоу.",
        "ref": "standup_book",
        "onetap": "GET",
    },
    "raffle": {
        "title": "Розыгрыш",
        "headline": "Участвовать в розыгрыше",
        "lead": "Разрешите сообщения — сразу откроем VK со стартом розыгрыша.",
        "ref": "standup_rozygr",
        "onetap": "PARTICIPATE",
    },
    "offline_gift": {
        "title": "Подарок",
        "headline": "Офлайн-розыгрыш подарка",
        "lead": "Разрешите сообщения — сразу откроем VK со списком на подарок.",
        "ref": "offline_gift",
        "onetap": "PARTICIPATE",
    },
}

_ENTRY_COOLDOWN_SEC = 20.0
_last_entry: dict[tuple[int, str], float] = {}


def _env_app_id() -> str:
    return (os.getenv("VK_OPENAPI_APP_ID") or "").strip()


def _formats_keyboard() -> str:
    kb = VKKeyboardBuilder(inline=True)
    kb.button("STANDUP BEST", {"cmd": "best"}, color="primary")
    kb.button("Хитлото", {"cmd": "hitloto"}, color="primary")
    kb.button("StandUp Проверка материала", {"cmd": "check"}, color="primary")
    kb.button("В главное меню", {"cmd": "main_menu"})
    kb.adjust(1)
    return kb.as_json()


async def _send_flow_chain(client: VKClient, settings, flow_key: str, vk_id: int) -> None:
    """Сразу старт ветки бота — без промежуточной кнопки на лендинге."""
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
        # Как «Наши форматы шоу» в меню бота
        await client.send_message(
            vk_id,
            FORMATS_TEXT,
            keyboard=_formats_keyboard(),
        )
        return

    # offline_gift — сразу список шоу / одно шоу с кнопкой участия в чате
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
    path_by_flow = {
        "booking": "/vk/booking",
        "raffle": "/vk/raffle",
        "offline_gift": "/vk/offline-gift",
    }
    redirect_url = "https://go.moscowstandupshow.ru" + path_by_flow[flow_key]

    if not ready:
        body = """
        <p class="warn">Страница ещё настраивается. Загляните чуть позже или напишите менеджеру.</p>
        """
        widget_js = ""
    else:
        body = f"""
        <p class="lead">{escape(flow["lead"])}</p>
        <div id="vk_allow_messages_from_community" class="widget"></div>
        <button type="button" id="alreadyAllowed" class="linkish">Уже разрешали сообщения? Продолжить</button>
        <div id="VkIdSdkOneTap" class="onetap" hidden></div>
        <p id="status" class="status" hidden></p>
        <button type="button" id="openApp" class="cta" hidden>Открыть приложение VK</button>
        """
        ref_value = str(flow["ref"])
        onetap_content = flow.get("onetap") or "SIGN_IN"
        widget_js = f"""
<script src="https://vk.com/js/api/openapi.js?169"></script>
<script src="https://unpkg.com/@vkid/sdk@2.5.1/dist-sdk/umd/index.js"></script>
<script>
(function () {{
  var flow = {json.dumps(flow_key)};
  var redirectUrl = {json.dumps(redirect_url)};
  var appId = {int(app_id)};
  var groupId = {int(group_id)};
  var ref = {json.dumps(ref_value)};
  var vkId = null;
  var sending = false;
  var oneTapReady = false;
  var statusEl = document.getElementById("status");
  var openAppBtn = document.getElementById("openApp");
  var oneTapBox = document.getElementById("VkIdSdkOneTap");
  var alreadyBtn = document.getElementById("alreadyAllowed");

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
    window.location.href = "https://vk.com/write-" + groupId + "?ref=" + encodeURIComponent(ref);
  }}

  function afterSendOk() {{
    setStatus("Готово! Откройте приложение VK — сценарий уже в сообщениях.", true);
    openAppBtn.hidden = false;
    setTimeout(openVkAppOnly, 350);
  }}

  openAppBtn.addEventListener("click", openVkAppOnly);
  alreadyBtn.addEventListener("click", function () {{
    showOneTapFallback();
  }});

  function sendEntryThenDialog() {{
    if (!vkId) {{
      showOneTapFallback();
      return;
    }}
    if (sending) return;
    sending = true;
    setStatus("Запускаем сценарий в VK…", true);
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
        if (res.status === 403 || /разреш/i.test(err)) {{
          setStatus("Разрешите сообщения кнопкой VK выше — и всё продолжится само.", false);
          return;
        }}
        setStatus(err || "Не удалось отправить. Попробуйте ещё раз.", false);
      }})
      .catch(function () {{
        sending = false;
        setStatus("Сеть недоступна. Попробуйте ещё раз.", false);
      }});
  }}

  function showOneTapFallback() {{
    alreadyBtn.hidden = true;
    oneTapBox.hidden = false;
    setStatus("Нажмите одну кнопку ниже — без выбора другого аккаунта.", true);
    initOneTap();
  }}

  function onMessagesAllowed(userId) {{
    var id = normalizeId(userId);
    console.log("allow-messages allowed", userId, id);
    if (id) {{
      vkId = id;
      sendEntryThenDialog();
      return;
    }}
    // Виджет разрешил, но id не отдал — один клик VK ID без выбора «другой аккаунт».
    showOneTapFallback();
  }}

  try {{
    VK.init({{ apiId: appId, onlyWidgets: true }});
    if (VK.Observer && VK.Observer.subscribe) {{
      VK.Observer.subscribe("widgets.allowMessagesFromCommunity.allowed", onMessagesAllowed);
      VK.Observer.subscribe("widgets.allowMessagesFromCommunity.denied", function () {{
        setStatus("Нужно разрешить сообщения сообществу.", false);
      }});
      VK.Observer.subscribe("widgets.allowMessagesFromCommunity.declined", function () {{
        setStatus("Нужно разрешить сообщения сообществу.", false);
      }});
    }}
    if (typeof VK.addCallback === "function") {{
      VK.addCallback("widgets.allowMessagesFromCommunity.allowed", onMessagesAllowed);
    }}
    VK.Widgets.AllowMessagesFromCommunity(
      "vk_allow_messages_from_community",
      {{ height: 30 }},
      groupId
    );
  }} catch (e) {{
    console.log("OpenAPI widget init error", e);
  }}

  function initOneTap() {{
    if (oneTapReady) return;
    if (!("VKIDSDK" in window)) {{
      setStatus("Не загрузился VK ID. Обновите страницу.", false);
      return;
    }}
    oneTapReady = true;
    var VKID = window.VKIDSDK;
    VKID.Config.init({{
      app: appId,
      redirectUrl: redirectUrl,
      responseMode: VKID.ConfigResponseMode.Callback,
      source: VKID.ConfigSource.LOWCODE
    }});

    var contentId = VKID.OneTapContentId.{onetap_content};
    var oneTap = new VKID.OneTap();
    oneTap.render({{
      container: oneTapBox,
      scheme: VKID.Scheme.DARK,
      lang: VKID.Languages.RUS,
      showAlternativeLogin: false,
      fastAuthEnabled: true,
      contentId: contentId,
      styles: {{ borderRadius: 14, height: 48 }}
    }})
      .on(VKID.WidgetEvents.ERROR, function (err) {{
        console.log("VK ID OneTap error", err);
        setStatus("Не удалось показать кнопку входа. Обновите страницу.", false);
      }})
      .on(VKID.OneTapInternalEvents.LOGIN_SUCCESS, function (payload) {{
        setStatus("Запускаем сценарий…", true);
        VKID.Auth.exchangeCode(payload.code, payload.device_id)
          .then(function (data) {{
            var id = normalizeId(data) || normalizeId(data && data.user_id);
            if (!id) {{
              setStatus("Не получили id пользователя VK. Попробуйте ещё раз.", false);
              return;
            }}
            vkId = id;
            sendEntryThenDialog();
          }})
          .catch(function (err) {{
            console.log("VK ID exchange error", err);
            setStatus("Не удалось войти через VK ID. Попробуйте ещё раз.", false);
          }});
      }});
  }}

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
    .widget {{ margin: 0 0 12px; min-height: 36px; }}
    .onetap {{ margin: 0 0 14px; min-height: 48px; }}
    .linkish {{
      display: block; width: 100%; margin: 0 0 14px; padding: 0; border: 0;
      background: transparent; color: var(--gold); font: 600 14px Manrope, sans-serif;
      text-align: left; text-decoration: underline; cursor: pointer;
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


def register_routes(app: web.Application) -> None:
    app.router.add_get("/vk/booking", landing_booking)
    app.router.add_get("/vk/raffle", landing_raffle)
    app.router.add_get("/vk/offline-gift", landing_offline_gift)
    app.router.add_post("/vk/entry", entry_post)
