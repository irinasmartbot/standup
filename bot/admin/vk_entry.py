"""Public VK entry landings on go.moscowstandupshow.ru (no admin auth).

Упрощённый вход без OpenAPI-виджета и без ИП:
кнопка открывает диалог сообщества с ref → бот ловит Start и ведёт в ветку.

GET /vk/booking | /vk/raffle | /vk/offline-gift
"""

from __future__ import annotations

import logging
from html import escape
from typing import Any
from urllib.parse import quote

from aiohttp import web

from bot.vk.config import load_vk_settings

logger = logging.getLogger(__name__)

# ref должен совпадать с обработкой в bot/vk/app.py
FLOWS: dict[str, dict[str, Any]] = {
    "booking": {
        "title": "Бронирование",
        "headline": "Забронировать места",
        "lead": (
            "Нажмите кнопку — откроется диалог с нашим сообществом во ВКонтакте. "
            "Там нажмите «Начать» (или напишите любое сообщение), и бот сразу откроет бронь."
        ),
        "cta": "Открыть VK и забронировать",
        "ref": "standup_book",
    },
    "raffle": {
        "title": "Розыгрыш",
        "headline": "Участвовать в розыгрыше",
        "lead": (
            "Нажмите кнопку — откроется диалог с сообществом. "
            "Нажмите «Начать», и бот пришлёт инструкцию розыгрыша."
        ),
        "cta": "Открыть VK и участвовать",
        "ref": "standup_rozygr",
    },
    "offline_gift": {
        "title": "Подарок",
        "headline": "Офлайн-розыгрыш подарка",
        "lead": (
            "Нажмите кнопку — откроется диалог с сообществом. "
            "Нажмите «Начать», чтобы попасть в список (нужна подписка на сообщество)."
        ),
        "cta": "Открыть VK и вступить",
        "ref": "offline_gift",
    },
}


def _screen_name_from_community_link(link: str) -> str:
    value = (link or "").strip().rstrip("/")
    if not value:
        return ""
    name = value.rsplit("/", 1)[-1]
    if not name or name in {"vk.com", "vk.ru"}:
        return ""
    return name


def entry_write_link(*, group_id: int | None, community_link: str, ref: str) -> str:
    """Ссылка, с которой ref доходит до бота (write- / vk.me, не club…)."""
    ref_q = quote(ref, safe="")
    if group_id:
        return f"https://vk.com/write-{int(group_id)}?ref={ref_q}"
    name = _screen_name_from_community_link(community_link)
    if name:
        return f"https://vk.me/{name}?ref={ref_q}"
    return ""


def _landing_html(flow_key: str) -> str:
    flow = FLOWS[flow_key]
    settings = load_vk_settings()
    href = entry_write_link(
        group_id=settings.group_id,
        community_link=settings.community_link,
        ref=flow["ref"],
    )

    if not href:
        body = """
        <p class="warn">Страница ещё настраивается (не задан VK_GROUP_ID). Загляните позже.</p>
        """
    else:
        body = f"""
        <p class="lead">{escape(flow["lead"])}</p>
        <ol class="steps">
          <li>Нажмите кнопку ниже</li>
          <li>В VK нажмите «Начать»</li>
          <li>Следуйте сообщениям бота</li>
        </ol>
        <a class="cta" href="{escape(href)}" target="_blank" rel="noopener">
          {escape(flow["cta"])}
        </a>
        <p class="hint">Если диалог уже открыт — просто нажмите «Начать» или напишите «начать».</p>
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
    .lead {{
      margin: 0 0 20px; font-family: system-ui, sans-serif; font-size: 16px;
      line-height: 1.45; color: #e8dfd2;
    }}
    .steps {{
      margin: 0 0 24px; padding-left: 1.2em; font-family: system-ui, sans-serif;
      font-size: 15px; line-height: 1.5; color: #e8dfd2;
    }}
    .cta {{
      display: flex; align-items: center; justify-content: center; width: 100%;
      min-height: 52px; border-radius: 10px; background: #e8a87c; color: #1a120c;
      font: 600 16px system-ui, sans-serif; text-decoration: none; text-align: center;
      padding: 12px 16px;
    }}
    .hint {{
      margin: 16px 0 0; font-family: system-ui, sans-serif; font-size: 13px;
      color: #c4b5a0; line-height: 1.4;
    }}
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
</body>
</html>"""


async def landing_booking(_: web.Request) -> web.Response:
    return web.Response(text=_landing_html("booking"), content_type="text/html")


async def landing_raffle(_: web.Request) -> web.Response:
    return web.Response(text=_landing_html("raffle"), content_type="text/html")


async def landing_offline_gift(_: web.Request) -> web.Response:
    return web.Response(text=_landing_html("offline_gift"), content_type="text/html")


def register_routes(app: web.Application) -> None:
    app.router.add_get("/vk/booking", landing_booking)
    app.router.add_get("/vk/raffle", landing_raffle)
    app.router.add_get("/vk/offline-gift", landing_offline_gift)
