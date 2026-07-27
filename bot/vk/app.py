import json
import logging
from datetime import datetime
from typing import Any

from bot.config import DATABASE_URL, EVENTS_SOURCE
from bot.db.analytics import (
    EVENT_BOT_START,
    EVENT_BRANCH_BEST,
    EVENT_BRANCH_HITLOTO,
    EVENT_BRANCH_PROVERKA,
    EVENT_CMD_BUY_TICKET,
    EVENT_CMD_MAIN_MENU,
    EVENT_SHOW_CARD,
    track_event,
)
from bot.db.crud import ensure_user
from bot.handlers.formats import (
    BUY_TICKET_TEXT as TG_BUY_TICKET_TEXT,
    FORMATS_TEXT as TG_FORMATS_TEXT,
    RULES_TEXT as TG_RULES_TEXT,
    VENUE_CARDS,
    VENUES_INTRO_TEXT as TG_VENUES_INTRO_TEXT,
)
from bot.handlers.start import WELCOME_TEXT as TG_WELCOME_TEXT
from bot.services.sheets import load_events
from bot.utils.phone import normalize_phone
from bot.utils.ticket import MONTHS, format_date
from bot.vk import booking as vk_booking
from bot.vk.client import VKClient
from bot.vk.config import VKSettings
from bot.vk.formatting import format_vk_text
from bot.vk.keyboards import VKKeyboardBuilder
from bot.vk.media import VKSystemImageCache

logger = logging.getLogger(__name__)

DATES_PAGE_SIZE = 6
VK_CHANNEL = "vkontakte"

WELCOME_TEXT = format_vk_text(TG_WELCOME_TEXT)
FORMATS_TEXT = format_vk_text(TG_FORMATS_TEXT)
BUY_TICKET_TEXT = format_vk_text(TG_BUY_TICKET_TEXT)
RULES_TEXT = format_vk_text(TG_RULES_TEXT)
VENUES_INTRO_TEXT = format_vk_text(TG_VENUES_INTRO_TEXT)

CHECK_ENTRY_TEXT = format_vk_text(
    "Привет! 😊 Я помогу тебе забронировать места на <b>Проверку материала</b> "
    "от Moscow StandUp Show 🎤\n\nВыбирай формат поиска мероприятий 👇"
)
BEST_ENTRY_TEXT = format_vk_text(
    "Привет 😊 Я помогу тебе выбрать билеты на <b>StandUp BEST</b> "
    "от Moscow StandUp Show 🎤\n\nВыбирай формат поиска мероприятий 👇"
)
HITLOTO_ENTRY_TEXT = format_vk_text(
    "Привет 😊 Я помогу тебе выбрать билеты на <b>Хитлото</b> "
    "от Moscow StandUp Show 🎤\n\nВыбирай дату 👇"
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
    kb.button("Купить билет", _payload("buy_ticket"), color="primary")
    kb.button("Наши форматы ШОУ", _payload("formats"))
    kb.button("Наши площадки", _payload("venues"))
    kb.button("Правила посещения шоу", _payload("rules"))
    kb.button("Задать вопрос менеджеру", link=settings.manager_link)
    kb.button("Канал анонсов", link=settings.community_link)
    # VK: max 6 rows — две ссылки в одном ряду
    kb.adjust(1, 1, 1, 1, 1, 2)
    return kb.as_json()


def formats_keyboard() -> str:
    kb = VKKeyboardBuilder()
    kb.button("STANDUP BEST", _payload("best"), color="primary")
    kb.button("Хитлото", _payload("hitloto"), color="primary")
    kb.button("StandUp Проверка материала", _payload("check"), color="primary")
    kb.button("В главное меню", _payload("main_menu"))
    kb.adjust(1)
    return kb.as_json()


def paid_formats_keyboard() -> str:
    kb = VKKeyboardBuilder()
    kb.button("STANDUP BEST", _payload("best"), color="primary")
    kb.button("Хитлото", _payload("hitloto"), color="primary")
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
        self.peer_browse: dict[int, str] = {}
        self.booking_sessions: dict[int, dict] = {}
        self._ticket_in_progress: set[int] = set()

    def _vk_id(self, message: dict[str, Any], peer_id: int) -> int:
        from_id = message.get("from_id")
        try:
            return int(from_id if from_id is not None else peer_id)
        except (TypeError, ValueError):
            return int(peer_id)

    def _track(self, vk_id: int, name: str, **kwargs) -> None:
        track_event(name, vk_id=vk_id, channel=VK_CHANNEL, **kwargs)

    def _ensure_user(self, vk_id: int) -> None:
        ensure_user(vk_id=vk_id, source=VK_CHANNEL)

    async def _load_events(self, event_format: str) -> list[dict[str, Any]]:
        if EVENTS_SOURCE != "postgres" or not DATABASE_URL:
            raise RuntimeError(
                "VK bot requires EVENTS_SOURCE=postgres and DATABASE_URL. "
                "Google Sheets is not used for VK."
            )
        return await load_events(event_format)

    async def send_menu(self, peer_id: int, *, vk_id: int | None = None, is_start: bool = False) -> None:
        user_id = vk_id or peer_id
        self._ensure_user(user_id)
        vk_booking.clear_session(self.booking_sessions, user_id)
        if is_start:
            self._track(user_id, EVENT_BOT_START)
        else:
            self._track(user_id, EVENT_CMD_MAIN_MENU)
        await self.client.send_message(
            peer_id,
            WELCOME_TEXT,
            keyboard=main_menu_keyboard(self.settings),
        )

    async def _start_check_booking(self, peer_id: int, vk_id: int, event_id: Any) -> None:
        event = await vk_booking.find_event(event_id)
        if not event:
            await self.client.send_message(
                peer_id,
                "Мероприятие уже недоступно.",
                keyboard=main_menu_keyboard(self.settings),
            )
            return
        vk_booking.start_session(self.booking_sessions, vk_id, event)
        await self.client.send_message(
            peer_id,
            "Напишите, пожалуйста, ваше имя.",
            keyboard=vk_booking.booking_cancel_keyboard(),
        )

    async def _ask_phone(self, peer_id: int, vk_id: int, session: dict) -> None:
        saved = vk_booking.saved_phone_for(vk_id)
        if saved:
            session["phone"] = saved
            session["step"] = vk_booking.STEP_PHONE
            await self.client.send_message(
                peer_id,
                f"Ваш номер телефона: {saved}\nИспользовать его?",
                keyboard=vk_booking.phone_saved_keyboard(saved),
            )
            return
        session["step"] = vk_booking.STEP_PHONE
        await self.client.send_message(
            peer_id,
            vk_booking.PHONE_ASK_TEXT,
            keyboard=vk_booking.booking_cancel_keyboard(),
        )

    async def _ask_guests(self, peer_id: int, session: dict) -> None:
        session["step"] = vk_booking.STEP_GUESTS
        name = session.get("name") or ""
        await self.client.send_message(
            peer_id,
            f"{name}, напишите цифрой или выберите кнопкой, на какое количество человек бронируете?\n\n"
            "Внимание: бронь на один билет максимум 4 человека.",
            keyboard=vk_booking.guests_keyboard(),
        )

    async def _finish_booking(self, peer_id: int, vk_id: int, session: dict, guests: int) -> None:
        try:
            await vk_booking.complete_booking(
                client=self.client,
                peer_id=peer_id,
                vk_id=vk_id,
                session=session,
                guests=guests,
                manager_link=self.settings.manager_link,
                community_link=self.settings.community_link,
            )
        except RuntimeError as exc:
            msg = str(exc)
            if msg == "no_seats":
                await self.client.send_message(
                    peer_id,
                    "К сожалению, на это мероприятие места закончились. Выбери другую дату.",
                    keyboard=main_menu_keyboard(self.settings),
                )
                vk_booking.clear_session(self.booking_sessions, vk_id)
                return
            if msg.startswith("only:"):
                available = msg.split(":", 1)[1]
                await self.client.send_message(
                    peer_id,
                    f"К сожалению, доступно только {available} мест. Укажите меньшее количество гостей.",
                    keyboard=vk_booking.guests_keyboard(),
                )
                return
            raise
        except Exception:
            logger.exception("VK booking create failed vk_id=%s", vk_id)
            await self.client.send_message(
                peer_id,
                "Не удалось создать бронь. Попробуй ещё раз чуть позже.",
                keyboard=main_menu_keyboard(self.settings),
            )
        vk_booking.clear_session(self.booking_sessions, vk_id)

    async def _issue_ticket(self, peer_id: int, booking_id: int) -> None:
        if booking_id in self._ticket_in_progress:
            await self.client.send_message(peer_id, "Билет уже формируется.")
            return
        self._ticket_in_progress.add(booking_id)
        try:
            await vk_booking.issue_ticket(
                client=self.client,
                peer_id=peer_id,
                booking_id=booking_id,
                manager_link=self.settings.manager_link,
            )
        except Exception:
            logger.exception("VK ticket issue failed booking_id=%s", booking_id)
            await self.client.send_message(
                peer_id,
                "Не удалось отправить билет картинкой. Напишите менеджеру — поможем вручную.",
                keyboard=main_menu_keyboard(self.settings),
            )
        finally:
            self._ticket_in_progress.discard(booking_id)

    async def _handle_booking_flow(
        self,
        peer_id: int,
        vk_id: int,
        *,
        text: str,
        cmd: str | None,
        payload: dict[str, Any],
    ) -> bool:
        session = self.booking_sessions.get(vk_id)
        if cmd == "booking_cancel":
            vk_booking.clear_session(self.booking_sessions, vk_id)
            await self.send_menu(peer_id, vk_id=vk_id)
            return True
        if cmd == "booking_get_ticket":
            await self._issue_ticket(peer_id, int(payload.get("booking_id") or 0))
            return True
        if cmd == "check_booking_start":
            await self._start_check_booking(peer_id, vk_id, payload.get("event_id"))
            return True
        if not session:
            return False

        if cmd == "booking_phone_use":
            phone = normalize_phone(session.get("phone"))
            if not phone:
                await self.client.send_message(
                    peer_id,
                    vk_booking.PHONE_INVALID_TEXT,
                    keyboard=vk_booking.booking_cancel_keyboard(),
                )
                session["step"] = vk_booking.STEP_PHONE
                return True
            session["phone"] = phone
            await self._ask_guests(peer_id, session)
            return True
        if cmd == "booking_phone_change":
            session["phone"] = ""
            session["step"] = vk_booking.STEP_PHONE
            await self.client.send_message(
                peer_id,
                vk_booking.PHONE_ASK_TEXT,
                keyboard=vk_booking.booking_cancel_keyboard(),
            )
            return True
        if cmd == "booking_guests":
            guests = int(payload.get("guests") or 0)
            if guests < 1 or guests > 4:
                await self.client.send_message(
                    peer_id,
                    "Максимум 4 человека на одну бронь. Выберите число от 1 до 4.",
                    keyboard=vk_booking.guests_keyboard(),
                )
                return True
            await self._finish_booking(peer_id, vk_id, session, guests)
            return True

        step = session.get("step")
        if step == vk_booking.STEP_NAME and text:
            session["name"] = text.strip()
            await self._ask_phone(peer_id, vk_id, session)
            return True
        if step == vk_booking.STEP_PHONE and text:
            phone = normalize_phone(text)
            if not phone:
                await self.client.send_message(
                    peer_id,
                    vk_booking.PHONE_INVALID_TEXT,
                    keyboard=vk_booking.booking_cancel_keyboard(),
                )
                return True
            session["phone"] = phone
            await self._ask_guests(peer_id, session)
            return True
        if step == vk_booking.STEP_GUESTS and text:
            if not text.isdigit():
                await self.client.send_message(
                    peer_id,
                    "Пожалуйста, напишите цифрой количество гостей (от 1 до 4) или нажмите кнопку.",
                    keyboard=vk_booking.guests_keyboard(),
                )
                return True
            guests = int(text)
            if guests < 1 or guests > 4:
                await self.client.send_message(
                    peer_id,
                    "Максимум 4 человека на одну бронь. Напишите цифру от 1 до 4.",
                    keyboard=vk_booking.guests_keyboard(),
                )
                return True
            await self._finish_booking(peer_id, vk_id, session, guests)
            return True

        if step == vk_booking.STEP_NAME:
            await self.client.send_message(
                peer_id,
                "Напишите, пожалуйста, ваше имя.",
                keyboard=vk_booking.booking_cancel_keyboard(),
            )
            return True
        if step == vk_booking.STEP_PHONE:
            await self.client.send_message(
                peer_id,
                vk_booking.PHONE_ASK_TEXT,
                keyboard=vk_booking.booking_cancel_keyboard(),
            )
            return True
        if step == vk_booking.STEP_GUESTS:
            await self.client.send_message(
                peer_id,
                "Выберите количество гостей кнопкой или цифрой от 1 до 4.",
                keyboard=vk_booking.guests_keyboard(),
            )
            return True
        return False

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
        vk_id = self._vk_id(message, peer_id)
        text = (message.get("text") or "").strip()
        payload = _parse_payload(message.get("payload"))
        cmd = payload.get("cmd")
        logger.info("VK message peer_id=%s vk_id=%s cmd=%s text=%r", peer_id, vk_id, cmd, text[:80])
        text_key = text.casefold()
        if not cmd:
            context = self.peer_context.get(peer_id)
            text_commands = {
                "забронировать места": "book",
                "купить билет": "buy_ticket",
                "наши форматы шоу": "formats",
                "форматы шоу": "formats",
                "наши площадки": "venues",
                "площадки": "venues",
                "правила посещения шоу": "rules",
                "правила посещения": "rules",
                "standup best": "best",
                "хитлото": "hitloto",
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
                cmd = f"{context}_date_page" if context in {"best", "check", "hitloto"} else None
            if not cmd and text_key in {
                "📍 выбор по площадке",
                "выбор по площадке",
                "📍 выбор по локации",
                "выбор по локации",
            }:
                cmd = f"{context}_venues" if context in {"best", "check"} else None

        if await self._handle_booking_flow(
            peer_id,
            vk_id,
            text=text,
            cmd=cmd,
            payload=payload,
        ):
            return

        if text.lower() in {"/start", "start", "начать"} or cmd == "main_menu":
            await self.send_menu(
                peer_id,
                vk_id=vk_id,
                is_start=text.lower() in {"/start", "start", "начать"},
            )
            return
        if cmd == "formats":
            await self.client.send_message(peer_id, FORMATS_TEXT, keyboard=formats_keyboard())
            return
        if cmd == "buy_ticket":
            self._track(vk_id, EVENT_CMD_BUY_TICKET, props={"via": "menu"})
            await self.client.send_message(peer_id, BUY_TICKET_TEXT, keyboard=paid_formats_keyboard())
            return
        if cmd == "book":
            # Как в TG: бесплатная бронь сразу открывает Проверку материала
            self.peer_context[peer_id] = "check"
            self._track(vk_id, EVENT_BRANCH_PROVERKA)
            await self.client.send_message(
                peer_id,
                CHECK_ENTRY_TEXT,
                keyboard=event_search_keyboard(
                    "check_date_page",
                    "check_venues",
                    dates_label="📅 Выбрать по дате",
                    venues_label="📍 Выбор по площадке",
                ),
                attachment=self.images.get("show_cover"),
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
                self._track(vk_id, EVENT_BRANCH_PROVERKA)
                await self.client.send_message(
                    peer_id,
                    CHECK_ENTRY_TEXT,
                    keyboard=event_search_keyboard(
                        "check_date_page",
                        "check_venues",
                        dates_label="📅 Выбрать по дате",
                        venues_label="📍 Выбор по площадке",
                    ),
                    attachment=self.images.get("show_cover"),
                )
                return
            await self._send_check_dates(peer_id, page)
            return
        if cmd == "check_venues":
            self.peer_browse[peer_id] = "venue"
            await self._send_check_venues(peer_id)
            return
        if cmd == "check_venue":
            self.peer_browse[peer_id] = "venue"
            await self._send_check_venue(peer_id, payload.get("venue") or "")
            return
        if cmd == "check_date":
            self.peer_browse[peer_id] = "date"
            await self._send_check_date(peer_id, payload.get("date") or "", vk_id=vk_id)
            return
        if cmd == "check_event":
            await self._send_check_event(peer_id, payload.get("event_id"), vk_id=vk_id)
            return
        if cmd in {"best", "best_date_page"}:
            page = int(payload.get("page") or 0)
            if cmd == "best":
                self.peer_context[peer_id] = "best"
                self._track(vk_id, EVENT_BRANCH_BEST)
                await self.client.send_message(
                    peer_id,
                    BEST_ENTRY_TEXT,
                    keyboard=event_search_keyboard(
                        "best_date_page",
                        "best_venues",
                        dates_label="📅 Выбрать по дате",
                        venues_label="📍 Выбор по локации",
                    ),
                    attachment=self.images.get("show_cover"),
                )
                return
            await self._send_best_dates(peer_id, page)
            return
        if cmd == "best_venues":
            self.peer_browse[peer_id] = "venue"
            await self._send_best_venues(peer_id)
            return
        if cmd == "best_venue":
            self.peer_browse[peer_id] = "venue"
            await self._send_best_venue(peer_id, payload.get("venue") or "")
            return
        if cmd == "best_date":
            self.peer_browse[peer_id] = "date"
            await self._send_best_date(peer_id, payload.get("date") or "", vk_id=vk_id)
            return
        if cmd == "best_event":
            await self._send_best_event(peer_id, payload.get("event_id"), vk_id=vk_id)
            return
        if cmd in {"hitloto", "hitloto_date_page"}:
            page = int(payload.get("page") or 0)
            if cmd == "hitloto":
                self.peer_context[peer_id] = "hitloto"
                self.peer_browse[peer_id] = "date"
                self._track(vk_id, EVENT_BRANCH_HITLOTO)
                await self._send_hitloto_dates(peer_id, page, entry=True)
                return
            await self._send_hitloto_dates(peer_id, page)
            return
        if cmd == "hitloto_date":
            self.peer_browse[peer_id] = "date"
            await self._send_hitloto_date(peer_id, payload.get("date") or "", vk_id=vk_id)
            return
        if cmd == "hitloto_event":
            await self._send_hitloto_event(peer_id, payload.get("event_id"), vk_id=vk_id)
            return

        await self.client.send_message(
            peer_id,
            "Пожалуйста, выбери вариант из кнопок ниже.",
            keyboard=main_menu_keyboard(self.settings),
        )

    async def _send_venues(self, peer_id: int) -> None:
        await self.client.send_message(
            peer_id,
            VENUES_INTRO_TEXT,
            keyboard=main_menu_keyboard(self.settings),
        )
        for card in VENUE_CARDS:
            key = card["file"].rsplit(".", 1)[0]
            attachment = self.images.get(key)
            caption = format_vk_text(card.get("fallback_html") or "")
            if attachment:
                await self.client.send_message(peer_id, caption or key, attachment=attachment)
            elif caption:
                await self.client.send_message(peer_id, caption)

    async def _send_check_dates(self, peer_id: int, page: int = 0) -> None:
        self.peer_context[peer_id] = "check"
        logger.info("Loading check dates for peer_id=%s page=%s", peer_id, page)
        try:
            events = await self._load_events("proverka")
        except Exception:
            logger.exception("Failed to load check events")
            await self.client.send_message(
                peer_id,
                "Не удалось загрузить даты. Попробуй ещё раз через минуту.",
                keyboard=event_search_keyboard(
                    "check_date_page",
                    "check_venues",
                    dates_label="📅 Выбрать по дате",
                    venues_label="📍 Выбор по площадке",
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
        events = await self._load_events("proverka")
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
            [e for e in await self._load_events("proverka") if e.get("location") == venue],
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

    async def _send_check_date(self, peer_id: int, date: str, *, vk_id: int | None = None) -> None:
        events = [e for e in await self._load_events("proverka") if e["date"] == date]
        if not events:
            await self.client.send_message(peer_id, "Эта дата уже недоступна.", keyboard=main_menu_keyboard(self.settings))
            return
        if len(events) == 1:
            await self._send_check_event(peer_id, events[0]["id"], vk_id=vk_id or peer_id)
            return
        await self.client.send_message(
            peer_id,
            f"Шоу на {_date_label(date)}:",
            keyboard=_events_keyboard(events, "check_event", "check"),
        )

    async def _send_check_event(self, peer_id: int, event_id: Any, *, vk_id: int | None = None) -> None:
        event = next((e for e in await self._load_events("proverka") if str(e["id"]) == str(event_id)), None)
        if not event:
            await self.client.send_message(peer_id, "Мероприятие уже недоступно.", keyboard=main_menu_keyboard(self.settings))
            return
        user_id = vk_id or peer_id
        self._track(
            user_id,
            EVENT_SHOW_CARD,
            event_id=event.get("id"),
            props={
                "format": "proverka",
                "browse": self.peer_browse.get(peer_id, "date"),
                "date": event.get("date"),
                "time": event.get("time"),
                "location": event.get("location"),
            },
        )
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
            events = await self._load_events("best")
        except Exception:
            logger.exception("Failed to load BEST events")
            await self.client.send_message(
                peer_id,
                "Не удалось загрузить даты. Попробуй ещё раз через минуту.",
                keyboard=event_search_keyboard(
                    "best_date_page",
                    "best_venues",
                    dates_label="📅 Выбрать по дате",
                    venues_label="📍 Выбор по локации",
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
        events = await self._load_events("best")
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
            [e for e in await self._load_events("best") if e.get("location") == venue],
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

    async def _send_best_date(self, peer_id: int, date: str, *, vk_id: int | None = None) -> None:
        events = [e for e in await self._load_events("best") if e["date"] == date]
        if not events:
            await self.client.send_message(peer_id, "Эта дата уже недоступна.", keyboard=main_menu_keyboard(self.settings))
            return
        if len(events) == 1:
            await self._send_best_event(peer_id, events[0]["id"], vk_id=vk_id or peer_id)
            return
        await self.client.send_message(
            peer_id,
            f"BEST на {_date_label(date)}:",
            keyboard=_events_keyboard(events, "best_event", "best"),
        )

    async def _send_best_event(self, peer_id: int, event_id: Any, *, vk_id: int | None = None) -> None:
        event = next((e for e in await self._load_events("best") if str(e["id"]) == str(event_id)), None)
        if not event:
            await self.client.send_message(peer_id, "Мероприятие уже недоступно.", keyboard=main_menu_keyboard(self.settings))
            return
        user_id = vk_id or peer_id
        payment_url = event.get("payment_url") or ""
        self._track(
            user_id,
            EVENT_SHOW_CARD,
            event_id=event.get("id"),
            props={
                "format": "best",
                "browse": self.peer_browse.get(peer_id, "date"),
                "date": event.get("date"),
                "time": event.get("time"),
                "location": event.get("location"),
                "has_payment": bool(payment_url),
            },
        )
        kb = VKKeyboardBuilder()
        if payment_url:
            kb.button("Купить билет", link=payment_url)
        else:
            kb.button("Задать вопрос менеджеру", link=self.settings.manager_link)
        kb.button("Назад к датам", _payload("best"))
        kb.adjust(1)
        await self.client.send_message(peer_id, format_vk_text(_event_text(event)), keyboard=kb.as_json())

    async def _send_hitloto_dates(self, peer_id: int, page: int = 0, *, entry: bool = False) -> None:
        self.peer_context[peer_id] = "hitloto"
        try:
            events = await self._load_events("hitloto")
        except Exception:
            logger.exception("Failed to load Hitloto events")
            await self.client.send_message(
                peer_id,
                "Не удалось загрузить даты. Попробуй ещё раз через минуту.",
                keyboard=paid_formats_keyboard(),
            )
            return
        dates = sorted({e["date"] for e in events}, key=lambda d: datetime.strptime(d, "%d.%m.%Y"))
        if not dates:
            kb = VKKeyboardBuilder()
            kb.button("Задать вопрос менеджеру", link=self.settings.manager_link)
            kb.button("В главное меню", _payload("main_menu"))
            kb.adjust(1)
            await self.client.send_message(
                peer_id,
                "Пока нет актуальных мероприятий Хитлото. Можно уточнить расписание у менеджера.",
                keyboard=kb.as_json(),
            )
            return
        text = HITLOTO_ENTRY_TEXT if entry else "Выбирай дату 👇"
        keyboard = _dates_keyboard(dates, "hitloto_date", page, "main_menu")
        attachment = self.images.get("hitloto_start") or self.images.get("show_cover")
        try:
            await self.client.send_message(peer_id, text, keyboard=keyboard, attachment=attachment)
        except Exception:
            logger.exception("Failed to send Hitloto dates with attachment, retrying without it")
            await self.client.send_message(peer_id, text, keyboard=keyboard)

    async def _send_hitloto_date(self, peer_id: int, date: str, *, vk_id: int | None = None) -> None:
        events = [e for e in await self._load_events("hitloto") if e["date"] == date]
        if not events:
            await self.client.send_message(peer_id, "Эта дата уже недоступна.", keyboard=main_menu_keyboard(self.settings))
            return
        if len(events) == 1:
            await self._send_hitloto_event(peer_id, events[0]["id"], vk_id=vk_id or peer_id)
            return
        await self.client.send_message(
            peer_id,
            f"Хитлото на {_date_label(date)}:",
            keyboard=_events_keyboard(events, "hitloto_event", "hitloto"),
        )

    async def _send_hitloto_event(self, peer_id: int, event_id: Any, *, vk_id: int | None = None) -> None:
        event = next((e for e in await self._load_events("hitloto") if str(e["id"]) == str(event_id)), None)
        if not event:
            await self.client.send_message(peer_id, "Мероприятие уже недоступно.", keyboard=main_menu_keyboard(self.settings))
            return
        user_id = vk_id or peer_id
        payment_url = event.get("payment_url") or ""
        self._track(
            user_id,
            EVENT_SHOW_CARD,
            event_id=event.get("id"),
            props={
                "format": "hitloto",
                "browse": self.peer_browse.get(peer_id, "date"),
                "date": event.get("date"),
                "time": event.get("time"),
                "location": event.get("location"),
                "has_payment": bool(payment_url),
            },
        )
        kb = VKKeyboardBuilder()
        if payment_url:
            kb.button("Купить билет", link=payment_url)
        else:
            kb.button("Задать вопрос менеджеру", link=self.settings.manager_link)
        kb.button("Назад к датам", _payload("hitloto"))
        kb.adjust(1)
        await self.client.send_message(peer_id, format_vk_text(_event_text(event)), keyboard=kb.as_json())

    async def run(self) -> None:
        if EVENTS_SOURCE != "postgres" or not DATABASE_URL:
            raise RuntimeError(
                "VK bot requires EVENTS_SOURCE=postgres and DATABASE_URL. "
                "Google Sheets is not used for VK."
            )
        logger.info(
            "VK bot long polling started for group_id=%s events_source=%s",
            self.settings.group_id,
            EVENTS_SOURCE,
        )
        async for update in self.client.long_poll():
            try:
                await self.handle_update(update)
            except Exception:
                logger.exception("Failed to handle VK update")
