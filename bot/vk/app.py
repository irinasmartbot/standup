import json
import logging
from datetime import datetime
from typing import Any

from bot.services.sheets import load_events
from bot.utils.ticket import MONTHS, format_date
from bot.vk.client import VKClient
from bot.vk.config import VKSettings
from bot.vk.formatting import format_vk_text
from bot.vk.keyboards import VKKeyboardBuilder
from bot.vk.media import VKSystemImageCache

logger = logging.getLogger(__name__)

DATES_PAGE_SIZE = 6

WELCOME_TEXT = (
    "Привет! Это Moscow StandUp Show! Мы делаем шоу в различных заведениях в центре Москвы каждый день!\n\n"
    "Только опытные комики, участники проектов ТНТ и YouTube, харизматичные ведущие, интерактив со зрителями, "
    "атмосферные залы, подарки на каждом мероприятии - это всё мы!\n\n"
    "Здесь ты сможешь узнать о нас побольше и забронировать места:"
)

FORMATS_TEXT = """🎭 Наши форматы шоу:

Формат StandUp BEST:
Только лучший, уже проверенный стэндап материал от троих комиков, именитых участников многочисленных телевизионных проектов. Вы не услышите ни одной несмешной шутки, только BEST!!
Билеты - от 500 рублей.

Формат Хитлото от Moscow StandUp Show:
Музыкальное лото в компании стендап-комика! Веселись, пой, пей, зачеркивай прозвучавшие песни в своём бланке, и выигрывай призы 🔥
Билеты - от 990 рублей.

Формат StandUp Проверка материала:
5-7 опытных комиков, участников известных проектов ТНТ и YouTube, рассказывают по 10-15 минут свежих, но не проверенных шуток. Вы услышите настоящий эксклюзив и поможете комикам понять, что смешно, а что стоит убрать из материала 🐒
Вход бесплатный."""

CHECK_ENTRY_TEXT = (
    "Привет! 😊 Я помогу тебе забронировать места на Проверку материала "
    "от Moscow StandUp Show 🎤\n\nВыбирай формат поиска мероприятий 👇"
)

BEST_ENTRY_TEXT = (
    "Привет 😊 Я помогу тебе выбрать билеты на StandUp BEST "
    "от Moscow StandUp Show 🎤\n\nВыбирай формат поиска мероприятий 👇"
)

RULES_TEXT = """Правила посещения шоу:

1. На наших мероприятиях действует возрастное ограничение 18+.
2. Сбор гостей начинается за полчаса до времени начала мероприятия.
3. Все шоу проходят в заведениях, посещение предполагает обязательный заказ минимум одной позиции по меню.
4. Рассадка осуществляется администратором на площадке.
5. Во время шоу запрещено громко разговаривать и мешать выступлению."""

TG_FORMATS_TEXT = """🎭 Наши форматы шоу:

Формат StandUp BEST:
Только лучший, уже проверенный стэндап материал от троих комиков, именитых участников многочисленных телевизионных проектов. Вы не услышите ни одной несмешной шутки, только BEST!!
Билеты - от 500 рублей.

Формат Хитлото от Moscow StandUp Show:
Музыкальное лото в компании стендап-комика! Веселись, пой, пей, зачеркивай прозвучавшие песни в своём бланке, и выигрывай призы 🔥
Билеты - от 990 рублей.

Формат StandUp Проверка материала:
5-7 опытных комиков, участников известных проектов ТНТ и YouTube, рассказывают по 10-15 минут свежих, но не проверенных шуток. Вы услышите настоящий эксклюзив и поможете комикам понять, что смешно, а что стоит убрать из материала 🐒
Вход бесплатный."""

BOOK_TEXT = """Выбирай формат шоу 👇

Формат StandUp BEST:
Только лучший, уже проверенный стэндап материал от троих комиков, именитых участников многочисленных телевизионных проектов. Вы не услышите ни одной несмешной шутки, только BEST!!
Билеты - от 500 рублей.

Формат Хитлото от Moscow StandUp Show:
Музыкальное лото в компании стендап-комика! Веселись, пой, пей, зачеркивай прозвучавшие песни в своём бланке, и выигрывай призы 🔥
Билеты - от 990 рублей.

Формат StandUp проверка материала:
5-7 опытных комиков, участников известных проектов ТНТ и YouTube, рассказывают по 10-15 минут свежих, но не проверенных шуток, Вы услышите настоящий эксклюзив и поможете комикам понять, что смешно, а что стоит убрать из материала 🙈
Вход бесплатный."""

CHECK_ENTRY_TEXT = (
    "Привет! 😊 Я помогу тебе забронировать места на Проверку материала "
    "от Moscow StandUp Show 🎤\n\nВыбирай формат поиска мероприятий 👇"
)

BEST_ENTRY_TEXT = (
    "Привет 😊 Я помогу тебе выбрать билеты на StandUp BEST "
    "от Moscow StandUp Show 🎤\n\nВыбирай формат поиска мероприятий 👇"
)


def _payload(value: str, **extra) -> dict[str, Any]:
    return {"cmd": value, **extra}


def _parse_payload(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _date_label(date: str) -> str:
    try:
        d = datetime.strptime(date, "%d.%m.%Y")
        return d.strftime("%d ") + MONTHS[d.strftime("%B")]
    except Exception:
        return date


def main_menu_keyboard(settings: VKSettings) -> str:
    kb = VKKeyboardBuilder()
    kb.button("Забронировать места", _payload("book"), color="primary")
    kb.button("Площадки", _payload("venues"))
    kb.button("Форматы ШОУ", _payload("formats"))
    kb.button("Правила посещения", _payload("rules"))
    kb.button("Задать вопрос менеджеру", link=settings.manager_link)
    kb.button("Канал анонсов", link=settings.community_link)
    kb.adjust(1, 2, 1, 1, 1)
    return kb.as_json()


def formats_keyboard() -> str:
    kb = VKKeyboardBuilder()
    kb.button("STANDUP BEST", _payload("best"), color="primary")
    kb.button("StandUp Проверка материала", _payload("check"), color="primary")
    kb.button("В главное меню", _payload("main_menu"))
    kb.adjust(1)
    return kb.as_json()


def event_search_keyboard(
    dates_cmd: str,
    venues_cmd: str,
    *,
    dates_label: str = "📅 Выбрать по дате",
    venues_label: str = "📍 Выбор по площадке",
) -> str:
    kb = VKKeyboardBuilder()
    kb.button(dates_label, _payload(dates_cmd), color="primary")
    kb.button(venues_label, _payload(venues_cmd), color="primary")
    kb.button("В главное меню", _payload("main_menu"))
    kb.adjust(1)
    return kb.as_json()


def _dates_keyboard(
    dates: list[str],
    command_prefix: str,
    page: int,
    back_cmd: str,
    venues_cmd: str | None = None,
) -> str:
    start = page * DATES_PAGE_SIZE
    end = start + DATES_PAGE_SIZE
    shown = dates[start:end]
    kb = VKKeyboardBuilder()
    for date in shown:
        kb.button(_date_label(date), _payload(command_prefix, date=date), color="primary")
    if page > 0:
        kb.button("⬅️", _payload(f"{command_prefix}_page", page=page - 1))
    if end < len(dates):
        kb.button("➡️", _payload(f"{command_prefix}_page", page=page + 1))
    if venues_cmd:
        kb.button("🗓 Выбрать по площадкам", _payload(venues_cmd))
    kb.button("В главное меню", _payload(back_cmd))
    widths = [2] * (len(shown) // 2)
    if len(shown) % 2:
        widths.append(1)
    nav_count = int(page > 0) + int(end < len(dates))
    if nav_count:
        widths.append(nav_count)
    if venues_cmd:
        widths.append(1)
    widths.append(1)
    kb.adjust(*widths)
    return kb.as_json()


def _events_keyboard(events: list[dict[str, Any]], command: str, back_cmd: str) -> str:
    kb = VKKeyboardBuilder()
    for event in events[:8]:
        label = f"{event.get('time', '')} - {event.get('location', '')}".strip(" -")
        kb.button(label, _payload(command, event_id=event["id"]), color="primary")
    kb.button("Назад", _payload(back_cmd))
    kb.adjust(1)
    return kb.as_json()


def _venues_keyboard(venues: list[str], command: str, back_cmd: str) -> str:
    kb = VKKeyboardBuilder()
    for venue in venues[:8]:
        kb.button(venue, _payload(command, venue=venue), color="primary")
    kb.button("Назад к датам", _payload(back_cmd))
    kb.button("В главное меню", _payload("main_menu"))
    kb.adjust(1)
    return kb.as_json()


def _event_text(event: dict[str, Any]) -> str:
    return "\n".join(
        [
            format_date(event["date"]),
            event.get("weekday") or "",
            "",
            event.get("time") or "",
            event.get("address") or "",
            event.get("description") or "",
        ]
    ).strip()


class VKBotApp:
    def __init__(self, client: VKClient, settings: VKSettings):
        self.client = client
        self.settings = settings
        self.images = VKSystemImageCache(settings.system_images_cache)
        self.peer_context: dict[int, str] = {}

    async def send_menu(self, peer_id: int) -> None:
        await self.client.send_message(
            peer_id,
            WELCOME_TEXT,
            keyboard=main_menu_keyboard(self.settings),
        )

    async def handle_update(self, update: dict[str, Any]) -> None:
        if update.get("type") != "message_new":
            return
        obj = update.get("object") or {}
        message = obj.get("message") if isinstance(obj.get("message"), dict) else obj
        if not isinstance(message, dict):
            return
        peer_id = message.get("peer_id")
        if not peer_id:
            return
        text = (message.get("text") or "").strip()
        payload = _parse_payload(message.get("payload"))
        cmd = payload.get("cmd")
        logger.info("VK message peer_id=%s cmd=%s text=%r", peer_id, cmd, text[:80])
        text_key = text.casefold()
        if not cmd:
            context = self.peer_context.get(peer_id)
            text_commands = {
                "забронировать места": "book",
                "форматы шоу": "formats",
                "форматы ШОУ".casefold(): "formats",
                "площадки": "venues",
                "правила посещения": "rules",
                "standup best": "best",
                "standup проверка материала": "check",
                "📅 даты best": "best_date_page",
                "даты best": "best_date_page",
                "📍 площадки best": "best_venues",
                "площадки best": "best_venues",
                "📅 даты проверки": "check_date_page",
                "даты проверки": "check_date_page",
                "📍 площадки проверки": "check_venues",
                "площадки проверки": "check_venues",
                "в главное меню": "main_menu",
            }
            cmd = text_commands.get(text_key)
            if not cmd and text_key in {"📅 выбрать по дате", "выбрать по дате"}:
                cmd = f"{context}_date_page" if context in {"best", "check"} else None
            if not cmd and text_key in {"📍 выбор по площадке", "выбор по площадке"}:
                cmd = f"{context}_venues" if context in {"best", "check"} else None

        if text.lower() in {"/start", "start", "начать"} or cmd == "main_menu":
            await self.send_menu(peer_id)
            return
        if cmd == "formats":
            await self.client.send_message(peer_id, TG_FORMATS_TEXT, keyboard=formats_keyboard())
            return
        if cmd == "book":
            await self.client.send_message(
                peer_id,
                BOOK_TEXT,
                keyboard=formats_keyboard(),
            )
            return
        if cmd == "rules":
            await self.client.send_message(
                peer_id,
                RULES_TEXT,
                keyboard=main_menu_keyboard(self.settings),
            )
            return
        if cmd == "venues":
            await self._send_venues(peer_id)
            return
        if cmd in {"check", "check_date_page"}:
            page = int(payload.get("page") or 0)
            if cmd == "check":
                self.peer_context[peer_id] = "check"
                await self.client.send_message(
                    peer_id,
                    CHECK_ENTRY_TEXT,
                    keyboard=event_search_keyboard(
                        "check_date_page",
                        "check_venues",
                        dates_label="📅 Даты Проверки",
                        venues_label="📍 Площадки Проверки",
                    ),
                    attachment=self.images.get("show_cover"),
                )
                return
            await self._send_check_dates(peer_id, page)
            return
        if cmd == "check_venues":
            await self._send_check_venues(peer_id)
            return
        if cmd == "check_venue":
            await self._send_check_venue(peer_id, payload.get("venue") or "")
            return
        if cmd == "check_date":
            await self._send_check_date(peer_id, payload.get("date") or "")
            return
        if cmd == "check_event":
            await self._send_check_event(peer_id, payload.get("event_id"))
            return
        if cmd == "check_booking_start":
            await self.client.send_message(
                peer_id,
                "Бронирование Проверки материала в VK подключим следующим шагом: имя, телефон и количество гостей.",
                keyboard=main_menu_keyboard(self.settings),
            )
            return
        if cmd in {"best", "best_date_page"}:
            page = int(payload.get("page") or 0)
            if cmd == "best":
                self.peer_context[peer_id] = "best"
                await self.client.send_message(
                    peer_id,
                    BEST_ENTRY_TEXT,
                    keyboard=event_search_keyboard(
                        "best_date_page",
                        "best_venues",
                        dates_label="📅 Даты BEST",
                        venues_label="📍 Площадки BEST",
                    ),
                    attachment=self.images.get("show_cover"),
                )
                return
            await self._send_best_dates(peer_id, page)
            return
        if cmd == "best_venues":
            await self._send_best_venues(peer_id)
            return
        if cmd == "best_venue":
            await self._send_best_venue(peer_id, payload.get("venue") or "")
            return
        if cmd == "best_date":
            await self._send_best_date(peer_id, payload.get("date") or "")
            return
        if cmd == "best_event":
            await self._send_best_event(peer_id, payload.get("event_id"))
            return

        await self.client.send_message(
            peer_id,
            "Пожалуйста, выбери вариант из кнопок ниже.",
            keyboard=main_menu_keyboard(self.settings),
        )

    async def _send_venues(self, peer_id: int) -> None:
        text = (
            "Наши площадки:\n\n"
            "Temple Bar - английский паб с демократичной атмосферой, стейками и коктейлями.\n\n"
            "Escobar - бар с неординарной кухней и брутальным дизайном.\n\n"
            "Небар - популярный бар с авторской коктейльной картой."
        )
        attachments = [
            self.images.get("temple_bar"),
            self.images.get("escobar"),
            self.images.get("nebar"),
        ]
        attachment = ",".join(item for item in attachments if item)
        await self.client.send_message(
            peer_id,
            text,
            keyboard=main_menu_keyboard(self.settings),
            attachment=attachment or None,
        )

    async def _send_check_dates(self, peer_id: int, page: int = 0) -> None:
        self.peer_context[peer_id] = "check"
        logger.info("Loading check dates for peer_id=%s page=%s", peer_id, page)
        try:
            events = await load_events("proverka")
        except Exception:
            logger.exception("Failed to load check events")
            await self.client.send_message(
                peer_id,
                "Не удалось загрузить даты. Попробуй ещё раз через минуту.",
                keyboard=event_search_keyboard(
                    "check_date_page",
                    "check_venues",
                    dates_label="📅 Даты Проверки",
                    venues_label="📍 Площадки Проверки",
                ),
            )
            return
        dates = sorted({e["date"] for e in events}, key=lambda d: datetime.strptime(d, "%d.%m.%Y"))
        if not dates:
            await self.client.send_message(peer_id, "Пока нет актуальных дат.", keyboard=main_menu_keyboard(self.settings))
            return
        keyboard = _dates_keyboard(dates, "check_date", page, "main_menu", venues_cmd="check_venues")
        try:
            await self.client.send_message(
                peer_id,
                "Проверка материала. Выбирай дату:",
                keyboard=keyboard,
                attachment=self.images.get("show_cover"),
            )
        except Exception:
            logger.exception("Failed to send check dates with attachment, retrying without it")
            await self.client.send_message(
                peer_id,
                "Проверка материала. Выбирай дату:",
                keyboard=keyboard,
            )
        logger.info("Sent check dates: %s items page=%s", len(dates), page)

    async def _send_check_venues(self, peer_id: int) -> None:
        self.peer_context[peer_id] = "check"
        events = await load_events("proverka")
        venues = sorted({e["location"] for e in events if e.get("location")})
        if not venues:
            await self.client.send_message(peer_id, "Пока нет актуальных площадок.", keyboard=main_menu_keyboard(self.settings))
            return
        await self.client.send_message(
            peer_id,
            "Выбирай площадку:",
            keyboard=_venues_keyboard(venues, "check_venue", "check"),
        )

    async def _send_check_venue(self, peer_id: int, venue: str) -> None:
        events = sorted(
            [e for e in await load_events("proverka") if e.get("location") == venue],
            key=lambda e: datetime.strptime(e["date"], "%d.%m.%Y"),
        )
        if not events:
            await self.client.send_message(peer_id, "На этой площадке пока нет актуальных дат.", keyboard=main_menu_keyboard(self.settings))
            return
        await self.client.send_message(
            peer_id,
            f"Мероприятия: {venue}",
            keyboard=_events_keyboard(events, "check_event", "check_venues"),
        )

    async def _send_check_date(self, peer_id: int, date: str) -> None:
        events = [e for e in await load_events("proverka") if e["date"] == date]
        if not events:
            await self.client.send_message(peer_id, "Эта дата уже недоступна.", keyboard=main_menu_keyboard(self.settings))
            return
        if len(events) == 1:
            await self._send_check_event(peer_id, events[0]["id"])
            return
        await self.client.send_message(
            peer_id,
            f"Шоу на {_date_label(date)}:",
            keyboard=_events_keyboard(events, "check_event", "check"),
        )

    async def _send_check_event(self, peer_id: int, event_id: Any) -> None:
        event = next((e for e in await load_events("proverka") if str(e["id"]) == str(event_id)), None)
        if not event:
            await self.client.send_message(peer_id, "Мероприятие уже недоступно.", keyboard=main_menu_keyboard(self.settings))
            return
        kb = VKKeyboardBuilder()
        kb.button("Забронировать", _payload("check_booking_start", event_id=event["id"]), color="primary")
        kb.button("Правила бронирования", _payload("rules"))
        kb.button("Назад к датам", _payload("check"))
        kb.adjust(1)
        await self.client.send_message(peer_id, format_vk_text(_event_text(event)), keyboard=kb.as_json())

    async def _send_best_dates(self, peer_id: int, page: int = 0) -> None:
        self.peer_context[peer_id] = "best"
        logger.info("Loading BEST dates for peer_id=%s page=%s", peer_id, page)
        try:
            events = await load_events("best")
        except Exception:
            logger.exception("Failed to load BEST events")
            await self.client.send_message(
                peer_id,
                "Не удалось загрузить даты. Попробуй ещё раз через минуту.",
                keyboard=event_search_keyboard(
                    "best_date_page",
                    "best_venues",
                    dates_label="📅 Даты BEST",
                    venues_label="📍 Площадки BEST",
                ),
            )
            return
        dates = sorted({e["date"] for e in events}, key=lambda d: datetime.strptime(d, "%d.%m.%Y"))
        if not dates:
            await self.client.send_message(peer_id, "Пока нет актуальных мероприятий BEST.", keyboard=main_menu_keyboard(self.settings))
            return
        keyboard = _dates_keyboard(dates, "best_date", page, "main_menu", venues_cmd="best_venues")
        try:
            await self.client.send_message(
                peer_id,
                "StandUp BEST. Выбирай дату:",
                keyboard=keyboard,
                attachment=self.images.get("show_cover"),
            )
        except Exception:
            logger.exception("Failed to send BEST dates with attachment, retrying without it")
            await self.client.send_message(
                peer_id,
                "StandUp BEST. Выбирай дату:",
                keyboard=keyboard,
            )
        logger.info("Sent BEST dates: %s items page=%s", len(dates), page)

    async def _send_best_venues(self, peer_id: int) -> None:
        self.peer_context[peer_id] = "best"
        events = await load_events("best")
        venues = sorted({e["location"] for e in events if e.get("location")})
        if not venues:
            await self.client.send_message(peer_id, "Пока нет актуальных площадок BEST.", keyboard=main_menu_keyboard(self.settings))
            return
        await self.client.send_message(
            peer_id,
            "BEST: выбирай площадку:",
            keyboard=_venues_keyboard(venues, "best_venue", "best"),
        )

    async def _send_best_venue(self, peer_id: int, venue: str) -> None:
        events = sorted(
            [e for e in await load_events("best") if e.get("location") == venue],
            key=lambda e: datetime.strptime(f"{e['date']} {e['time']}", "%d.%m.%Y %H:%M"),
        )
        if not events:
            await self.client.send_message(peer_id, "На этой площадке пока нет актуальных BEST.", keyboard=main_menu_keyboard(self.settings))
            return
        await self.client.send_message(
            peer_id,
            f"BEST: {venue}",
            keyboard=_events_keyboard(events, "best_event", "best_venues"),
        )

    async def _send_best_date(self, peer_id: int, date: str) -> None:
        events = [e for e in await load_events("best") if e["date"] == date]
        if not events:
            await self.client.send_message(peer_id, "Эта дата уже недоступна.", keyboard=main_menu_keyboard(self.settings))
            return
        if len(events) == 1:
            await self._send_best_event(peer_id, events[0]["id"])
            return
        await self.client.send_message(
            peer_id,
            f"BEST на {_date_label(date)}:",
            keyboard=_events_keyboard(events, "best_event", "best"),
        )

    async def _send_best_event(self, peer_id: int, event_id: Any) -> None:
        event = next((e for e in await load_events("best") if str(e["id"]) == str(event_id)), None)
        if not event:
            await self.client.send_message(peer_id, "Мероприятие уже недоступно.", keyboard=main_menu_keyboard(self.settings))
            return
        kb = VKKeyboardBuilder()
        payment_url = event.get("payment_url") or ""
        if payment_url:
            kb.button("Купить билет", link=payment_url)
        else:
            kb.button("Задать вопрос менеджеру", link=self.settings.manager_link)
        kb.button("Назад к датам", _payload("best"))
        kb.adjust(1)
        await self.client.send_message(peer_id, format_vk_text(_event_text(event)), keyboard=kb.as_json())

    async def run(self) -> None:
        logger.info("VK bot long polling started for group_id=%s", self.settings.group_id)
        async for update in self.client.long_poll():
            try:
                await self.handle_update(update)
            except Exception:
                logger.exception("Failed to handle VK update")
