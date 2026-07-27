import asyncio
import json
import logging
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from bot.config import BOT_TOKEN, DATABASE_URL, EVENTS_SOURCE, HELP_CHAT_ID
from bot.db.analytics import (
    EVENT_BOT_START,
    EVENT_BRANCH_BEST,
    EVENT_BRANCH_HITLOTO,
    EVENT_BRANCH_PROVERKA,
    EVENT_BOOKING_START,
    EVENT_BROWSE_DATES,
    EVENT_BROWSE_VENUES,
    EVENT_CMD_BUY_TICKET,
    EVENT_CMD_MAIN_MENU,
    EVENT_CMD_MY_BOOKINGS,
    EVENT_HELP_QUESTION,
    EVENT_RAFFLE_BRANCH,
    EVENT_RAFFLE_ENTER,
    EVENT_RAFFLE_SCREENSHOT,
    EVENT_RAFFLE_SUB_FAILED,
    EVENT_RAFFLE_SUBSCRIBED,
    EVENT_SHOW_CARD,
    track_event,
)
from bot.db.crud import (
    create_help_request,
    ensure_help_tables,
    ensure_user,
    get_booking,
    get_last_phone,
    update_booking_status,
)
from bot.handlers.booking import BOOKING_RULES_TEXT as TG_BOOKING_RULES_TEXT
from bot.handlers.formats import (
    BUY_TICKET_TEXT as TG_BUY_TICKET_TEXT,
    FORMATS_TEXT as TG_FORMATS_TEXT,
    RULES_TEXT as TG_RULES_TEXT,
    VENUE_CARDS,
    VENUES_INTRO_TEXT as TG_VENUES_INTRO_TEXT,
)
from bot.handlers.start import WELCOME_TEXT as TG_WELCOME_TEXT
from bot.services.sheets import load_events
from bot.utils.booking_texts import same_day_booking_warning
from bot.utils.free_text import is_meaningful_free_text
from bot.utils.phone import normalize_phone
from bot.utils.ticket import MONTHS, format_date, now_msk
from bot.vk import booking as vk_booking
from bot.vk import my_bookings as vk_mb
from bot.vk import raffle as vk_raffle
from bot.vk.client import VKClient
from bot.vk.config import VKSettings
from bot.vk.formatting import format_vk_text
from bot.vk.keyboards import VKKeyboardBuilder, empty_inline_keyboard, empty_keyboard
from bot.vk.media import VKRemoteImageCache, VKSystemImageCache, resolve_image_attachment

import aiohttp

logger = logging.getLogger(__name__)

DATES_PAGE_SIZE = 6
VK_CHANNEL = "vkontakte"
# Как в TG: не крутим в рандоме карточки площадок / хитлото / билет / отзывы.
_EXCLUDED_RANDOM_COVER_KEYS = frozenset({"temple_bar", "escobar", "nebar", "hitloto_start"})
# Deep link: vk.com/write-{group_id}?ref=standup_rozygr или vk.me/{screen_name}?ref=...
# (ссылка вида vk.com/club...?ref= НЕ передаёт ref в message_new).
_RAFFLE_REF_VALUES = frozenset({"standup_rozygr", "rozygrysh", "raffle", "розыгрыш"})


def raffle_entry_link(settings: VKSettings) -> str:
    """Рабочая ссылка входа в розыгрыш (ref доходит до бота только через write-/vk.me)."""
    if settings.group_id:
        return f"https://vk.com/write-{int(settings.group_id)}?ref=standup_rozygr"
    link = (settings.community_link or "").strip().rstrip("/")
    if link:
        # best-effort: screen_name из community_link
        name = link.rsplit("/", 1)[-1]
        if name and not name.startswith("club") and name not in {"vk.com", "vk.ru"}:
            return f"https://vk.me/{name}?ref=standup_rozygr"
    return "напиши «розыгрыш»"

WELCOME_TEXT = format_vk_text(TG_WELCOME_TEXT)
FORMATS_TEXT = format_vk_text(TG_FORMATS_TEXT)
BUY_TICKET_TEXT = format_vk_text(TG_BUY_TICKET_TEXT)
RULES_TEXT = format_vk_text(TG_RULES_TEXT)
BOOKING_RULES_TEXT = format_vk_text(TG_BOOKING_RULES_TEXT)
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


def main_menu_keyboard(settings: VKSettings, *, show_my_bookings: bool = False) -> str:
    kb = VKKeyboardBuilder(inline=True)
    kb.button("Забронировать места", _payload("book"), color="primary")
    kb.button("Купить билет", _payload("buy_ticket"), color="primary")
    if show_my_bookings:
        kb.button("Мои брони", _payload("my_bookings"))
    kb.button("Наши форматы ШОУ", _payload("formats"))
    kb.button("Наши площадки", _payload("venues"))
    kb.button("Правила посещения шоу", _payload("rules"))
    kb.button("Задать вопрос менеджеру", link=settings.manager_link)
    kb.button("Канал анонсов", link=settings.community_link)
    # Как на скрине TG/VK: площадки|правила и менеджер|канал в рядах по 2.
    # VK: max 6 rows / 10 buttons. Розыгрыш — только по ссылке / слову «розыгрыш».
    if show_my_bookings:
        kb.adjust(1, 1, 1, 1, 2, 2)
    else:
        kb.adjust(1, 1, 1, 2, 2)
    return kb.as_json()


def _venues_intro_keyboard() -> str:
    kb = VKKeyboardBuilder(inline=True)
    kb.button("Узнать подробнее", _payload("venues_details"), color="primary")
    kb.button("⬅️ В главное меню", _payload("main_menu"))
    kb.adjust(1)
    return kb.as_json()


def _venues_card_keyboard(index: int) -> str:
    next_index = (int(index) + 1) % len(VENUE_CARDS)
    kb = VKKeyboardBuilder(inline=True)
    kb.button("Смотреть ещё", _payload("venues_card", index=next_index), color="primary")
    kb.button("⬅️ В главное меню", _payload("main_menu"))
    kb.adjust(1)
    return kb.as_json()


def _venue_card_attachment_key(card: dict[str, Any]) -> str:
    return str(card.get("file") or "").rsplit(".", 1)[0]


def formats_keyboard() -> str:
    kb = VKKeyboardBuilder(inline=True)
    kb.button("STANDUP BEST", _payload("best"), color="primary")
    kb.button("Хитлото", _payload("hitloto"), color="primary")
    kb.button("StandUp Проверка материала", _payload("check"), color="primary")
    kb.button("В главное меню", _payload("main_menu"))
    kb.adjust(1)
    return kb.as_json()


def paid_formats_keyboard() -> str:
    kb = VKKeyboardBuilder(inline=True)
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
    kb = VKKeyboardBuilder(inline=True)
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
    kb = VKKeyboardBuilder(inline=True)
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


def _venue_dates_keyboard(dates: list[str], command: str, venue: str, back_cmd: str) -> str:
    shown = dates[:8]
    kb = VKKeyboardBuilder(inline=True)
    for date in shown:
        kb.button(_date_label(date), _payload(command, venue=venue, date=date), color="primary")
    kb.button("Назад к площадкам", _payload(back_cmd))
    kb.button("В главное меню", _payload("main_menu"))
    widths = [2] * (len(shown) // 2)
    if len(shown) % 2:
        widths.append(1)
    widths.extend([1, 1])
    kb.adjust(*widths)
    return kb.as_json()


def _events_keyboard(
    events: list[dict[str, Any]],
    command: str,
    back_cmd: str,
    **back_extra: Any,
) -> str:
    """Кнопки выбора шоу, когда на одну дату несколько слотов."""
    kb = VKKeyboardBuilder(inline=True)
    dates = {e.get("date") for e in events[:8]}
    multi_date = len(dates) > 1
    for event in events[:8]:
        time_part = (event.get("time") or "").strip()
        location = (event.get("location") or "").strip()
        if multi_date:
            label = f"{_date_label(event.get('date') or '')} {time_part}".strip()
        else:
            # На одной дате разные площадки/слоты — не дублируем одно и то же «19:00 - Escobar».
            label = time_part or location or "Шоу"
            if location and time_part and location not in label:
                # Если времена совпадают, отличим по площадке.
                same_time = sum(
                    1
                    for other in events[:8]
                    if (other.get("time") or "").strip() == time_part
                )
                if same_time > 1:
                    label = f"{time_part} · {location}"
        kb.button(label or "Шоу", _payload(command, event_id=event["id"]), color="primary")
    kb.button("Назад", _payload(back_cmd, **back_extra))
    kb.adjust(1)
    return kb.as_json()


def _venues_keyboard(venues: list[str], command: str, back_cmd: str, *, inline: bool = True) -> str:
    kb = VKKeyboardBuilder(inline=inline)
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


def _best_event_text_vk(event: dict[str, Any]) -> str:
    location_line = " ".join(
        part for part in [event.get("time") or "", event.get("location") or ""] if part
    ).strip()
    parts = [
        format_date(event["date"]),
        event.get("weekday") or "",
        "",
        location_line,
        event.get("address") or "",
        event.get("description") or "",
    ]
    host = (event.get("host") or "").strip()
    if host:
        parts.extend(["", "Кто выступает:", host])
    return "\n".join(part for part in parts if part is not None).strip()


def _best_carousel_keyboard(
    venue: str,
    index: int,
    total: int,
    *,
    payment_url: str = "",
    manager_link: str = "",
) -> str:
    kb = VKKeyboardBuilder(inline=True)
    if payment_url:
        kb.button("Купить билет", link=payment_url, color="primary")
    else:
        kb.button("Задать вопрос менеджеру", link=manager_link or "https://vk.com", color="primary")

    nav_count = 0
    if index > 0:
        kb.button("⬅️", _payload("best_carousel", venue=venue, index=index - 1), color="primary")
        nav_count += 1
    kb.button(f"{index + 1}/{total}", _payload("best_carousel_pos", venue=venue, index=index))
    nav_count += 1
    if index < total - 1:
        kb.button("➡️", _payload("best_carousel", venue=venue, index=index + 1), color="primary")
        nav_count += 1

    kb.button("Назад", _payload("best_venues"))
    kb.button("В главное меню", _payload("main_menu"))
    kb.adjust(1, nav_count, 1, 1)
    return kb.as_json()


class VKBotApp:
    def __init__(self, client: VKClient, settings: VKSettings):
        self.client = client
        self.settings = settings
        self.images = VKSystemImageCache(settings.system_images_cache)
        remote_cache = Path(settings.system_images_cache).with_name("vk_event_images.json")
        self.event_images = VKRemoteImageCache(str(remote_cache))
        self.peer_context: dict[int, str] = {}
        self.peer_browse: dict[int, str] = {}
        self.booking_sessions: dict[int, dict] = {}
        self.manage_sessions: dict[int, dict] = {}
        # Розыгрыш: kind / awaiting screenshot (до шага модерации).
        self.raffle_sessions: dict[int, dict[str, Any]] = {}
        # attachment последнего сообщения с кнопками розыгрыша (чтобы edit не стирал фото).
        self.raffle_msg_attachment: dict[int, str] = {}
        # Антиспам: несколько фото подряд после «кидай скрин».
        self._raffle_photo_burst: dict[int, float] = {}
        self._ticket_in_progress: set[int] = set()
        self.peer_nav_message_ids: dict[int, list[int]] = {}
        self._pending_delete_ids: dict[int, list[int]] = {}
        self.peer_carousel_message_ids: dict[int, int] = {}
        self.peer_my_bookings_message_ids: dict[int, int] = {}
        self.peer_dates_message_ids: dict[int, int] = {}
        self.peer_venues_message_ids: dict[int, int] = {}
        # cmid кнопки из текущего message_event — переживает рестарт бота.
        self._peer_event_cmid: dict[int, int] = {}
        self._seen_event_ids: dict[str, float] = {}
        self._seen_message_ids: dict[int, float] = {}
        self._peer_cmd_cooldown: dict[tuple[int, str], float] = {}
        self._peer_locks: dict[int, asyncio.Lock] = {}

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

    def _main_menu_kb(self, vk_id: int | None = None) -> str:
        show_my_bookings = False
        if vk_id is not None:
            try:
                show_my_bookings = bool(vk_mb.list_rows(int(vk_id)))
            except Exception:
                logger.exception("Failed to check VK bookings for menu vk_id=%s", vk_id)
        return main_menu_keyboard(self.settings, show_my_bookings=show_my_bookings)

    def _cover_attachment(self, *keys: str) -> str | None:
        for key in keys:
            attachment = self.images.get(key)
            if attachment:
                return attachment
        return None

    def _random_cover_attachment(self) -> str | None:
        """Случайная обложка шоу (как в TG). Не трогает площадки / хитлото / билет."""
        pool: list[str] = []
        for img in self.images.all():
            key = (img.key or "").strip().lower()
            if not key or not img.attachment:
                continue
            if key in _EXCLUDED_RANDOM_COVER_KEYS:
                continue
            if key.startswith("hitloto") or key.startswith("rozygrysh_otzyv"):
                continue
            if "ticket" in key:
                continue
            pool.append(img.attachment)
        if pool:
            return random.choice(pool)
        return self._cover_attachment("show_cover")

    async def _event_poster_attachment(self, peer_id: int, event: dict[str, Any]) -> str | None:
        poster = await resolve_image_attachment(
            self.client,
            peer_id,
            event.get("image"),
            self.event_images,
        )
        if poster:
            return poster
        return self._random_cover_attachment() or self._cover_attachment("hitloto_start")

    def _queue_delete(self, peer_id: int, *message_ids: Any) -> None:
        bucket = self._pending_delete_ids.setdefault(int(peer_id), [])
        for mid in message_ids:
            try:
                value = int(mid)
            except (TypeError, ValueError):
                continue
            if value and value not in bucket:
                bucket.append(value)

    async def _delete_nav(self, peer_id: int, *, extra_ids: list[int] | None = None) -> None:
        peer = int(peer_id)
        ids = list(self.peer_nav_message_ids.pop(peer, []))
        ids.extend(self._pending_delete_ids.pop(peer, []))
        if extra_ids:
            ids.extend(extra_ids)
        # Не чистим всю историю с клавиатурами: после рестарта память пустая,
        # и такой wipe убивал старые меню — кнопки «мертвели», пока пользователь
        # снова не писал «Начать».
        unique: list[int] = []
        for mid in ids:
            try:
                value = int(mid)
            except (TypeError, ValueError):
                continue
            if value and value not in unique:
                unique.append(value)
        if unique:
            await self.client.delete_messages(peer, unique)

    def _callback_cmid(self, peer_id: int) -> int | None:
        value = self._peer_event_cmid.get(int(peer_id))
        return int(value) if value else None

    async def _disable_callback_buttons(
        self,
        peer_id: int,
        text: str,
        *,
        attachment: str | None = None,
    ) -> bool:
        """Оставляет сообщение и вложения, убирает только inline-кнопки."""
        peer = int(peer_id)
        if attachment is None:
            keep_att = self.raffle_msg_attachment.get(peer)
        else:
            keep_att = attachment or None
        return await self._edit_card(
            peer_id,
            text,
            stored_message_id=None,
            keyboard=empty_inline_keyboard(),
            attachment=keep_att,
        )

    def _remember_raffle_attachment(self, peer_id: int, attachment: str | None) -> None:
        peer = int(peer_id)
        if attachment:
            self.raffle_msg_attachment[peer] = attachment
        else:
            self.raffle_msg_attachment.pop(peer, None)

    async def _edit_card(
        self,
        peer_id: int,
        text: str,
        *,
        stored_message_id: int | None,
        keyboard: str | None = None,
        attachment: str | None = None,
    ) -> bool:
        """Edit by remembered message_id or by cmid from the clicked button."""
        cmid = self._callback_cmid(peer_id)
        if stored_message_id:
            ok = await self.client.edit_message(
                peer_id,
                text,
                message_id=int(stored_message_id),
                keyboard=keyboard,
                attachment=attachment,
            )
            if ok:
                await self._delete_pending(peer_id)
                return True
        if cmid:
            ok = await self.client.edit_message(
                peer_id,
                text,
                conversation_message_id=int(cmid),
                keyboard=keyboard,
                attachment=attachment,
            )
            if ok:
                await self._delete_pending(peer_id)
                return True
        return False

    async def _delete_pending(self, peer_id: int) -> None:
        peer = int(peer_id)
        ids = self._pending_delete_ids.pop(peer, [])
        if ids:
            await self.client.delete_messages(peer, ids)

    def _remember_nav(self, peer_id: int, message_id: int | None) -> None:
        if not message_id:
            return
        peer = int(peer_id)
        bucket = self.peer_nav_message_ids.setdefault(peer, [])
        mid = int(message_id)
        if mid not in bucket:
            bucket.append(mid)
        # Keep only recent nav messages to avoid huge delete batches.
        if len(bucket) > 6:
            self.peer_nav_message_ids[peer] = bucket[-6:]

    async def _clear_reply_keyboard(self, peer_id: int) -> int | None:
        """Сбрасывает нижнюю reply-клавиатуру. Возвращает id служебного сообщения."""
        try:
            return await self.client.send_message(
                peer_id,
                "\u2060",
                keyboard=empty_keyboard(),
            )
        except Exception:
            logger.exception("Failed to clear VK reply keyboard peer_id=%s", peer_id)
            return None

    def _keyboard_is_inline(self, keyboard: str | None) -> bool:
        if not keyboard:
            return False
        try:
            data = json.loads(keyboard)
        except (TypeError, json.JSONDecodeError):
            return False
        return bool(isinstance(data, dict) and data.get("inline"))

    async def _send_text(
        self,
        peer_id: int,
        text: str,
        *,
        keyboard: str | None = None,
        attachment: str | None = None,
        replace_nav: bool = True,
    ) -> int | None:
        if replace_nav:
            await self._delete_nav(peer_id)
        clear_id: int | None = None
        if self._keyboard_is_inline(keyboard):
            # Важно: сброс reply-клавиатуры должен остаться до отправки inline-карточки.
            # Если удалить служебное сообщение слишком рано, VK снова показывает старое меню.
            clear_id = await self._clear_reply_keyboard(peer_id)
        message_id: int | None = None
        try:
            message_id = await self.client.send_message(
                peer_id,
                text,
                keyboard=keyboard,
                attachment=attachment,
            )
        except Exception:
            if not attachment:
                raise
            logger.exception("Failed to send VK message with attachment, retrying without it")
            message_id = await self.client.send_message(peer_id, text, keyboard=keyboard)
        if clear_id:
            try:
                await asyncio.sleep(0.25)
                await self.client.delete_messages(peer_id, [int(clear_id)])
            except Exception:
                logger.exception("Failed to delete VK reply-keyboard clear message peer_id=%s", peer_id)
        if replace_nav:
            self._remember_nav(peer_id, message_id)
        return message_id

    async def _send_or_edit_dates_card(
        self,
        peer_id: int,
        text: str,
        *,
        keyboard: str,
        attachment: str | None = None,
        edit: bool = False,
    ) -> None:
        """Листание дат: правим ту же карточку, без новых сообщений в чате."""
        peer = int(peer_id)
        existing_id = self.peer_dates_message_ids.get(peer)
        if edit and await self._edit_card(
            peer_id,
            text,
            stored_message_id=existing_id,
            keyboard=keyboard,
            # Не трогаем вложение: повторная картинка/пустой attachment даёт мигание.
            attachment=None,
        ):
            if existing_id:
                self.peer_dates_message_ids[peer] = int(existing_id)
            return
        if edit and (existing_id or self._callback_cmid(peer_id)):
            logger.warning(
                "Dates list edit failed peer_id=%s msg_id=%s cmid=%s, falling back to send",
                peer_id,
                existing_id,
                self._callback_cmid(peer_id),
            )
        mid = await self._send_text(
            peer_id,
            text,
            keyboard=keyboard,
            attachment=attachment,
        )
        if mid:
            self.peer_dates_message_ids[peer] = int(mid)

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
        vk_mb.clear_manage_session(self.manage_sessions, user_id)
        self._clear_raffle_screenshot_wait(int(user_id))
        self.peer_carousel_message_ids.pop(int(peer_id), None)
        self.peer_my_bookings_message_ids.pop(int(peer_id), None)
        self.peer_dates_message_ids.pop(int(peer_id), None)
        self.peer_venues_message_ids.pop(int(peer_id), None)
        if is_start:
            self._track(user_id, EVENT_BOT_START)
        else:
            self._track(user_id, EVENT_CMD_MAIN_MENU)
        await self._send_text(
            peer_id,
            WELCOME_TEXT,
            keyboard=self._main_menu_kb(user_id),
        )

    async def _start_check_booking(self, peer_id: int, vk_id: int, event_id: Any) -> None:
        event = await vk_booking.find_event(event_id)
        if not event:
            await self._send_text(
                peer_id,
                "Мероприятие уже недоступно.",
                keyboard=self._main_menu_kb(peer_id),
            )
            return

        event_date = event.get("date") or ""
        event_time = event.get("time") or ""
        try:
            if datetime.strptime(event_date, "%d.%m.%Y").date() < now_msk().date():
                await self._send_text(
                    peer_id,
                    "Это мероприятие уже прошло 😊 Выбери новую дату!",
                    keyboard=self._main_menu_kb(peer_id),
                )
                return
        except Exception:
            pass

        # Как в TG: нельзя повторно на тот же слот (дата+время).
        existing = get_booking(None, event_date, event_time, vk_id=vk_id)
        if existing:
            date_str = format_date(event_date)
            await self._send_text(
                peer_id,
                (
                    "⚠️ ВНИМАНИЕ, мы уже внесли Вас в списки гостей:\n\n"
                    f"Дата: {date_str}\n"
                    f"Время: {existing[6]}\n"
                    f"Локация: {existing[8]}\n"
                    f"Количество гостей: {existing[9]} чел.\n\n"
                    "Вы не можете забронировать повторный билет на данное мероприятие"
                ),
                keyboard=vk_booking.manage_ticket_keyboard(
                    int(existing[0]),
                    self.settings.manager_link,
                ),
            )
            return

        vk_booking.start_session(self.booking_sessions, vk_id, event)
        self._track(
            vk_id,
            EVENT_BOOKING_START,
            event_id=event.get("id"),
            props={
                "format": "proverka",
                "browse": self.peer_browse.get(peer_id, "date"),
                "date": event_date,
                "time": event_time,
                "location": event.get("location"),
            },
        )

        # Как в TG: на ту же дату другое шоу — предупреждаем, но бронировать можно.
        same_day_alert = same_day_booking_warning(
            None,
            event_date,
            exclude_time=event_time,
            for_alert=True,
            vk_id=vk_id,
        )
        if same_day_alert:
            await self.client.send_message(peer_id, same_day_alert)

        await self._ask_name(peer_id, vk_id, self.booking_sessions[vk_id])

    async def _ask_name(self, peer_id: int, vk_id: int, session: dict) -> None:
        """Как в TG: предложить имя из профиля VK или попросить ввести."""
        session["step"] = vk_booking.STEP_NAME
        try:
            name = (await self.client.get_user_display_name(vk_id) or "").strip()
        except Exception:
            logger.exception("Failed to load VK display name vk_id=%s", vk_id)
            name = ""
        if name:
            session["name"] = name
            await self._send_text(
                peer_id,
                vk_booking.name_confirm_text(name),
                keyboard=vk_booking.name_confirm_keyboard(name),
                replace_nav=False,
            )
            return
        session["name"] = ""
        await self._send_text(peer_id, vk_booking.NAME_ASK_TEXT, replace_nav=False)

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
                    keyboard=self._main_menu_kb(peer_id),
                )
                vk_booking.clear_session(self.booking_sessions, vk_id)
                return
            if msg == "already_booked":
                existing = get_booking(
                    None,
                    session.get("event_date"),
                    session.get("event_time"),
                    vk_id=vk_id,
                )
                if existing:
                    await self.client.send_message(
                        peer_id,
                        (
                            "⚠️ ВНИМАНИЕ, мы уже внесли Вас в списки гостей.\n"
                            "Вы не можете забронировать повторный билет на данное мероприятие"
                        ),
                        keyboard=vk_booking.manage_ticket_keyboard(
                            int(existing[0]),
                            self.settings.manager_link,
                        ),
                    )
                else:
                    await self.client.send_message(
                        peer_id,
                        "Вы уже забронировали это мероприятие.",
                        keyboard=self._main_menu_kb(peer_id),
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
                keyboard=self._main_menu_kb(peer_id),
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
                community_link=self.settings.community_link,
            )
        except Exception:
            logger.exception("VK ticket issue failed booking_id=%s", booking_id)
            await self.client.send_message(
                peer_id,
                "Не удалось отправить билет картинкой. Напишите менеджеру — поможем вручную.",
                keyboard=self._main_menu_kb(peer_id),
            )
        finally:
            self._ticket_in_progress.discard(booking_id)

    async def _send_my_bookings(
        self,
        peer_id: int,
        vk_id: int,
        *,
        page: int = 0,
        edit: bool = False,
    ) -> None:
        vk_booking.clear_session(self.booking_sessions, vk_id)
        vk_mb.clear_manage_session(self.manage_sessions, vk_id)
        # Не путаем с BEST-каруселью: у каждой свой message_id для edit.
        self.peer_carousel_message_ids.pop(int(peer_id), None)
        rows = vk_mb.list_rows(vk_id)
        if not rows:
            self.peer_my_bookings_message_ids.pop(int(peer_id), None)
            await self._send_text(
                peer_id,
                vk_mb.empty_bookings_text(),
                keyboard=self._main_menu_kb(vk_id),
            )
            return
        page = page % len(rows)
        text = vk_mb.booking_card_text(rows[page], page=page, total=len(rows))
        keyboard = vk_mb.bookings_keyboard(rows[page], page=page, total=len(rows))
        peer = int(peer_id)
        existing_id = self.peer_my_bookings_message_ids.get(peer)

        if edit and await self._edit_card(
            peer_id,
            text,
            stored_message_id=existing_id,
            keyboard=keyboard,
        ):
            if existing_id:
                self.peer_my_bookings_message_ids[peer] = int(existing_id)
            return
        if edit and (existing_id or self._callback_cmid(peer_id)):
            logger.warning(
                "My bookings carousel edit failed peer_id=%s msg_id=%s cmid=%s, falling back to send",
                peer_id,
                existing_id,
                self._callback_cmid(peer_id),
            )

        mid = await self._send_text(peer_id, text, keyboard=keyboard)
        if mid:
            self.peer_my_bookings_message_ids[peer] = int(mid)

    async def _send_my_booking_ticket(self, peer_id: int, vk_id: int, page: int) -> None:
        rows = vk_mb.list_rows(vk_id)
        if not rows:
            await self._send_text(
                peer_id,
                vk_mb.empty_bookings_text(),
                keyboard=self._main_menu_kb(vk_id),
            )
            return
        page = page % len(rows)
        row = rows[page]
        if row[2] != "confirmed":
            await self.client.send_message(peer_id, "Билет ещё не подтверждён.")
            return
        attachment = await self.client.upload_message_photo(
            peer_id,
            vk_mb.ticket_bytes(row),
            filename=f"ticket_{row[0]}.jpg",
        )
        await self._send_text(
            peer_id,
            vk_mb.ticket_caption(row),
            keyboard=vk_mb.ticket_view_keyboard(page),
            attachment=attachment,
        )

    async def _mb_actionable(self, peer_id: int, vk_id: int, booking_id: int):
        booking, err = vk_mb.actionable_booking(booking_id, vk_id)
        if err == "past":
            kb = VKKeyboardBuilder(inline=True)
            kb.button("📅 Посмотреть актуальные даты", _payload("check_date_page"))
            kb.button("В главное меню", _payload("main_menu"))
            kb.adjust(1)
            await self._send_text(
                peer_id,
                "К сожалению, это мероприятие уже прошло. Посмотри актуальное расписание 👇",
                keyboard=kb.as_json(),
            )
            return None
        if err or not booking:
            await self.client.send_message(peer_id, err or "Эта бронь уже отменена или не найдена.")
            return None
        return booking

    async def _handle_my_bookings_flow(
        self,
        peer_id: int,
        vk_id: int,
        *,
        text: str,
        cmd: str | None,
        payload: dict[str, Any],
    ) -> bool:
        manage = self.manage_sessions.get(vk_id)

        if cmd == "my_bookings":
            self._track(vk_id, EVENT_CMD_MY_BOOKINGS)
            await self._send_my_bookings(
                peer_id,
                vk_id,
                page=int(payload.get("page") or 0),
                edit=False,
            )
            return True
        if cmd == "mb_noop":
            return True
        if cmd == "mb_page":
            await self._send_my_bookings(
                peer_id,
                vk_id,
                page=int(payload.get("page") or 0),
                edit=True,
            )
            return True
        if cmd == "mb_ticket":
            await self._send_my_booking_ticket(peer_id, vk_id, int(payload.get("page") or 0))
            return True

        if cmd == "mb_cancel_confirm":
            booking_id = int(payload.get("booking_id") or 0)
            booking = await self._mb_actionable(peer_id, vk_id, booking_id)
            if not booking:
                return True
            date_label = f"{format_date(booking[5])} {booking[6]}"
            await self._send_text(
                peer_id,
                format_vk_text(
                    f"Для подтверждения отмены брони на <b>{date_label}</b> нажмите кнопку ниже"
                ),
                keyboard=vk_mb.confirm_keyboard("mb_cancel_do", booking_id),
                replace_nav=False,
            )
            return True
        if cmd == "mb_cancel_do":
            booking_id = int(payload.get("booking_id") or 0)
            booking = await self._mb_actionable(peer_id, vk_id, booking_id)
            if not booking:
                return True
            await vk_mb.delete_ticket_message(self.client, peer_id, booking_id)
            update_booking_status(booking_id, "cancelled")
            try:
                from bot.db.crud import get_active_raffle_booking, set_rozygrysh_used

                # Если отменили розыгрыш — снова можно участвовать.
                if not get_active_raffle_booking(vk_id=vk_id):
                    set_rozygrysh_used(vk_id=vk_id, used=False)
            except Exception:
                logger.exception("Failed to clear raffle flag after cancel vk_id=%s", vk_id)
            vk_mb.clear_manage_session(self.manage_sessions, vk_id)
            await self._send_text(
                peer_id,
                vk_mb.cancel_done_text(
                    community_link=self.settings.community_link,
                    manager_link=self.settings.manager_link,
                ),
                keyboard=vk_mb.after_cancel_keyboard(self.settings.community_link),
            )
            return True

        if cmd == "mb_change_date_confirm":
            booking_id = int(payload.get("booking_id") or 0)
            booking = await self._mb_actionable(peer_id, vk_id, booking_id)
            if not booking:
                return True
            date_label = f"{format_date(booking[5])} {booking[6]}"
            await self._send_text(
                peer_id,
                format_vk_text(
                    f"Для подтверждения изменения даты брони на <b>{date_label}</b> нажмите кнопку ниже"
                ),
                keyboard=vk_mb.confirm_keyboard("mb_change_date_do", booking_id),
                replace_nav=False,
            )
            return True
        if cmd == "mb_change_date_do":
            booking_id = int(payload.get("booking_id") or 0)
            booking = await self._mb_actionable(peer_id, vk_id, booking_id)
            if not booking:
                return True
            await vk_mb.delete_ticket_message(self.client, peer_id, booking_id)
            update_booking_status(booking_id, "cancelled")
            vk_mb.clear_manage_session(self.manage_sessions, vk_id)
            self.peer_context[peer_id] = "check"
            await self._send_text(
                peer_id,
                "Бронь отменена. Выбери новую дату 👇",
                keyboard=event_search_keyboard(
                    "check_date_page",
                    "check_venues",
                    dates_label="📅 Выбрать по дате",
                    venues_label="📍 Выбор по площадке",
                ),
            )
            return True

        if cmd == "mb_change_guests_confirm":
            booking_id = int(payload.get("booking_id") or 0)
            booking = await self._mb_actionable(peer_id, vk_id, booking_id)
            if not booking:
                return True
            date_label = f"{format_date(booking[5])} {booking[6]}"
            await self._send_text(
                peer_id,
                format_vk_text(
                    f"Для подтверждения изменения количества гостей на бронь <b>{date_label}</b> "
                    "нажмите кнопку ниже 👇"
                ),
                keyboard=vk_mb.confirm_keyboard("mb_change_guests_do", booking_id),
                replace_nav=False,
            )
            return True
        if cmd == "mb_change_guests_do":
            booking_id = int(payload.get("booking_id") or 0)
            booking = await self._mb_actionable(peer_id, vk_id, booking_id)
            if not booking:
                return True
            vk_booking.clear_session(self.booking_sessions, vk_id)
            self.manage_sessions[vk_id] = {
                "step": vk_mb.STEP_NEW_GUESTS,
                "booking_id": booking_id,
            }
            await self._send_text(
                peer_id,
                format_vk_text(
                    f"{booking[3]}, напишите пожалуйста цифрой, на какое количество человек бронируете?\n\n"
                    f"<b>Внимание, бронь на один билет максимум 4 человека</b>"
                ),
                keyboard=vk_mb.guests_pick_keyboard(booking_id),
            )
            return True
        if cmd == "mb_change_guests_set":
            booking_id = int(payload.get("booking_id") or 0)
            guests = int(payload.get("guests") or 0)
            if not await self._mb_actionable(peer_id, vk_id, booking_id):
                return True
            ok, msg = await vk_mb.apply_new_guests(booking_id=booking_id, guests=guests)
            if not ok:
                await self.client.send_message(
                    peer_id,
                    msg,
                    keyboard=vk_mb.guests_pick_keyboard(booking_id),
                )
                return True
            vk_mb.clear_manage_session(self.manage_sessions, vk_id)
            await self._send_text(
                peer_id,
                msg,
                keyboard=vk_mb.change_guests_done_keyboard(booking_id),
            )
            return True

        if manage and manage.get("step") == vk_mb.STEP_NEW_GUESTS and text:
            booking_id = int(manage.get("booking_id") or 0)
            if not text.isdigit():
                await self.client.send_message(
                    peer_id,
                    "Пожалуйста, напишите цифрой количество гостей (от 1 до 4) или нажмите кнопку.",
                    keyboard=vk_mb.guests_pick_keyboard(booking_id),
                )
                return True
            guests = int(text)
            if not await self._mb_actionable(peer_id, vk_id, booking_id):
                vk_mb.clear_manage_session(self.manage_sessions, vk_id)
                return True
            ok, msg = await vk_mb.apply_new_guests(booking_id=booking_id, guests=guests)
            if not ok:
                await self.client.send_message(
                    peer_id,
                    msg,
                    keyboard=vk_mb.guests_pick_keyboard(booking_id),
                )
                return True
            vk_mb.clear_manage_session(self.manage_sessions, vk_id)
            await self._send_text(
                peer_id,
                msg,
                keyboard=vk_mb.change_guests_done_keyboard(booking_id),
            )
            return True

        return False

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
        if cmd in {"booking_name_ok", "booking_name_change"}:
            if not session:
                await self._send_text(
                    peer_id,
                    "Сессия бронирования сброшена. Выбери дату ещё раз 😊",
                    replace_nav=False,
                )
                return True
            if cmd == "booking_name_change":
                session["name"] = ""
                session["step"] = vk_booking.STEP_NAME
                await self._send_text(peer_id, vk_booking.NAME_ASK_TEXT, replace_nav=False)
                return True
            name = (payload.get("name") or session.get("name") or "").strip()
            if not name:
                session["step"] = vk_booking.STEP_NAME
                await self._send_text(peer_id, vk_booking.NAME_ASK_TEXT, replace_nav=False)
                return True
            session["name"] = name
            try:
                await self._ask_phone(peer_id, vk_id, session)
            except Exception:
                logger.exception("ask_phone failed after name_ok vk_id=%s", vk_id)
                await self._send_text(
                    peer_id,
                    vk_booking.PHONE_ASK_TEXT,
                    replace_nav=False,
                )
                session["step"] = vk_booking.STEP_PHONE
            return True
        if await self._handle_my_bookings_flow(
            peer_id,
            vk_id,
            text=text,
            cmd=cmd,
            payload=payload,
        ):
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
                )
                session["step"] = vk_booking.STEP_PHONE
                return True
            session["phone"] = phone
            if session.get("guests_fixed") == 1 or session.get("booking_format") == "rozygrysh":
                await self._finish_booking(peer_id, vk_id, session, 1)
            else:
                await self._ask_guests(peer_id, session)
            return True
        if cmd == "booking_phone_change":
            session["phone"] = ""
            session["step"] = vk_booking.STEP_PHONE
            await self.client.send_message(
                peer_id,
                vk_booking.PHONE_ASK_TEXT,
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
                )
                return True
            session["phone"] = phone
            if session.get("guests_fixed") == 1 or session.get("booking_format") == "rozygrysh":
                await self._finish_booking(peer_id, vk_id, session, 1)
            else:
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
                vk_booking.NAME_ASK_TEXT,
            )
            return True
        if step == vk_booking.STEP_PHONE:
            await self.client.send_message(
                peer_id,
                vk_booking.PHONE_ASK_TEXT,
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

    def _peer_lock(self, peer_id: int) -> asyncio.Lock:
        peer = int(peer_id)
        lock = self._peer_locks.get(peer)
        if lock is None:
            lock = asyncio.Lock()
            self._peer_locks[peer] = lock
        return lock

    def _prune_seen(self, now: float) -> None:
        ttl = 120.0
        if len(self._seen_event_ids) > 500:
            self._seen_event_ids = {k: v for k, v in self._seen_event_ids.items() if now - v < ttl}
        if len(self._seen_message_ids) > 500:
            self._seen_message_ids = {k: v for k, v in self._seen_message_ids.items() if now - v < ttl}
        if len(self._peer_cmd_cooldown) > 500:
            self._peer_cmd_cooldown = {
                k: v for k, v in self._peer_cmd_cooldown.items() if now - v < ttl
            }

    def _already_handled(self, update: dict[str, Any], message: dict[str, Any]) -> bool:
        now = time.monotonic()
        self._prune_seen(now)
        event_id = str(update.get("event_id") or "").strip()
        if event_id:
            if event_id in self._seen_event_ids:
                return True
            self._seen_event_ids[event_id] = now
        try:
            mid = int(message.get("id") or 0)
        except (TypeError, ValueError):
            mid = 0
        if mid:
            if mid in self._seen_message_ids:
                return True
            self._seen_message_ids[mid] = now
        return False

    def _cmd_on_cooldown(self, peer_id: int, cmd: str | None) -> bool:
        if not cmd:
            return False
        # Листание карусели / броней / дат должно быть мгновенным.
        if cmd in {
            "best_carousel",
            "best_carousel_pos",
            "mb_page",
            "mb_noop",
            "check_date_page",
            "best_date_page",
            "hitloto_date_page",
            "rz_dates_page",
            "booking_name_ok",
            "booking_name_change",
            "booking_phone_use",
            "booking_phone_change",
            "venues_details",
            "venues_card",
        }:
            return False
        now = time.monotonic()
        key = (int(peer_id), str(cmd))
        prev = self._peer_cmd_cooldown.get(key)
        self._peer_cmd_cooldown[key] = now
        return prev is not None and (now - prev) < 1.5

    async def handle_update(self, update: dict[str, Any]) -> None:
        utype = update.get("type")
        if utype == "message_event":
            await self._handle_message_event(update)
            return
        if utype != "message_new":
            return
        obj = update.get("object") or {}
        message = obj.get("message") if isinstance(obj.get("message"), dict) else obj
        if not isinstance(message, dict):
            return
        try:
            if int(message.get("out") or 0) == 1:
                return
        except (TypeError, ValueError):
            pass
        try:
            from_id = int(message.get("from_id") or 0)
        except (TypeError, ValueError):
            from_id = 0
        if from_id < 0:
            return
        if self._already_handled(update, message):
            logger.info(
                "Skip duplicate VK update event_id=%s msg_id=%s",
                update.get("event_id"),
                message.get("id"),
            )
            return
        peer_id = message.get("peer_id")
        if not peer_id:
            return
        async with self._peer_lock(peer_id):
            await self._dispatch_message(message, int(peer_id))

    async def _handle_message_event(self, update: dict[str, Any]) -> None:
        """Callback-кнопки: без сообщения «➡️» в чате."""
        obj = update.get("object") or {}
        if not isinstance(obj, dict):
            return
        dedupe_key = str(update.get("event_id") or obj.get("event_id") or "").strip()
        if dedupe_key:
            now = time.monotonic()
            self._prune_seen(now)
            if dedupe_key in self._seen_event_ids:
                return
            self._seen_event_ids[dedupe_key] = now

        event_id = str(obj.get("event_id") or "").strip()
        try:
            peer_id = int(obj.get("peer_id") or 0)
            user_id = int(obj.get("user_id") or peer_id)
        except (TypeError, ValueError):
            return
        if not peer_id or not event_id:
            return

        # Always answer quickly so VK stops the loading animation on the button.
        try:
            await self.client.send_message_event_answer(event_id, user_id, peer_id)
        except Exception:
            logger.exception("sendMessageEventAnswer failed peer_id=%s", peer_id)

        raw_payload = obj.get("payload")
        if isinstance(raw_payload, dict):
            payload = raw_payload
        else:
            payload = _parse_payload(raw_payload if isinstance(raw_payload, str) else None)
        try:
            cmid = int(obj.get("conversation_message_id") or 0) or None
        except (TypeError, ValueError):
            cmid = None
        message = {
            "peer_id": peer_id,
            "from_id": user_id,
            "text": "",
            "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            "id": None,
            "conversation_message_id": cmid,
        }
        async with self._peer_lock(peer_id):
            if cmid:
                self._peer_event_cmid[peer_id] = cmid
            try:
                await self._dispatch_message(message, peer_id)
            finally:
                self._peer_event_cmid.pop(peer_id, None)

    async def _dispatch_message(self, message: dict[str, Any], peer_id: int) -> None:
        vk_id = self._vk_id(message, peer_id)
        text = (message.get("text") or "").strip()
        payload = _parse_payload(message.get("payload"))
        cmd = payload.get("cmd")
        # Button clicks create a user message — remove it with previous bot nav screens.
        if payload.get("cmd") and message.get("id") is not None:
            self._queue_delete(peer_id, message.get("id"))
        logger.info("VK message peer_id=%s vk_id=%s cmd=%s text=%r", peer_id, vk_id, cmd, text[:80])
        text_key = text.casefold()
        if text_key in {"сброс розыгрыш", "сброс розыгрыша", "/reset_rozygrysh"}:
            await self._reset_raffle_for_test(peer_id, vk_id)
            return
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
                "правила бронирования": "booking_rules",
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
                "⬅️ в главное меню": "main_menu",
                "мои брони": "my_bookings",
                "розыгрыш": "raffle",
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

        if self._cmd_on_cooldown(peer_id, cmd):
            logger.info("Skip VK cmd cooldown peer_id=%s cmd=%s", peer_id, cmd)
            return

        if await self._handle_booking_flow(
            peer_id,
            vk_id,
            text=text,
            cmd=cmd,
            payload=payload,
        ):
            return

        if not cmd:
            kind = self._resolve_raffle_screenshot_kind(vk_id)
            if kind:
                await self._handle_raffle_screenshot(peer_id, vk_id, message)
                return
            # VK часто шлёт альбом отдельными message_new — после первого скрина
            # остальные в окне 8с просто игнорируем (уже ответили / на модерации).
            if vk_raffle.count_image_attachments(message) > 0:
                last_burst = self._raffle_photo_burst.get(int(vk_id)) or 0.0
                if time.monotonic() - last_burst < 8.0:
                    return

        # Запуск розыгрыша по ссылке: ?ref=standup_rozygr (или слово «розыгрыш»).
        ref = str(message.get("ref") or payload.get("ref") or "").strip().casefold()
        if ref in _RAFFLE_REF_VALUES or cmd == "raffle":
            await self._send_raffle_start(peer_id, vk_id)
            return

        if text.lower() in {"/start", "start", "начать"} or cmd == "main_menu":
            await self.send_menu(
                peer_id,
                vk_id=vk_id,
                is_start=text.lower() in {"/start", "start", "начать"},
            )
            return
        if cmd == "formats":
            await self._send_text(peer_id, FORMATS_TEXT, keyboard=formats_keyboard())
            return
        if cmd == "buy_ticket":
            self._track(vk_id, EVENT_CMD_BUY_TICKET, props={"via": "menu"})
            await self._send_text(peer_id, BUY_TICKET_TEXT, keyboard=paid_formats_keyboard())
            return
        if cmd == "book":
            # Как в TG: бесплатная бронь сразу открывает Проверку материала
            self.peer_context[peer_id] = "check"
            self._track(vk_id, EVENT_BRANCH_PROVERKA)
            await self._send_text(
                peer_id,
                CHECK_ENTRY_TEXT,
                keyboard=event_search_keyboard(
                    "check_date_page",
                    "check_venues",
                    dates_label="📅 Выбрать по дате",
                    venues_label="📍 Выбор по площадке",
                ),
                attachment=self._random_cover_attachment(),
            )
            return
        if cmd == "rules":
            await self._send_text(
                peer_id,
                RULES_TEXT,
                keyboard=self._main_menu_kb(peer_id),
            )
            return
        if cmd == "booking_rules":
            event_id = payload.get("event_id")
            kb = VKKeyboardBuilder(inline=True)
            if event_id is not None:
                kb.button("Назад к карточке", _payload("check_event", event_id=event_id))
            else:
                kb.button("Назад", _payload("check"))
            kb.adjust(1)
            # Keep event card visible, like Telegram; only remove the button-click message.
            await self._delete_pending(peer_id)
            await self._send_text(peer_id, BOOKING_RULES_TEXT, keyboard=kb.as_json(), replace_nav=False)
            return
        if cmd == "venues":
            await self._send_venues(peer_id)
            return
        if cmd == "venues_details":
            await self._send_venue_card(peer_id, 0, edit=False)
            return
        if cmd == "venues_card":
            await self._send_venue_card(
                peer_id,
                int(payload.get("index") or 0),
                edit=True,
            )
            return
        if cmd in {"check", "check_date_page"}:
            page = int(payload.get("page") or 0)
            if cmd == "check":
                self.peer_context[peer_id] = "check"
                self.peer_dates_message_ids.pop(int(peer_id), None)
                self._track(vk_id, EVENT_BRANCH_PROVERKA)
                await self._send_text(
                    peer_id,
                    CHECK_ENTRY_TEXT,
                    keyboard=event_search_keyboard(
                        "check_date_page",
                        "check_venues",
                        dates_label="📅 Выбрать по дате",
                        venues_label="📍 Выбор по площадке",
                    ),
                    attachment=self._random_cover_attachment(),
                )
                return
            await self._send_check_dates(peer_id, page, edit="page" in payload)
            return
        if cmd == "check_venues":
            self.peer_browse[peer_id] = "venue"
            self.peer_dates_message_ids.pop(int(peer_id), None)
            await self._send_check_venues(peer_id)
            return
        if cmd == "check_venue":
            self.peer_browse[peer_id] = "venue"
            await self._send_check_venue(peer_id, payload.get("venue") or "", vk_id=vk_id)
            return
        if cmd == "check_venue_date":
            self.peer_browse[peer_id] = "venue"
            await self._send_check_venue_date(
                peer_id,
                payload.get("venue") or "",
                payload.get("date") or "",
                vk_id=vk_id,
            )
            return
        if cmd == "check_date":
            self.peer_browse[peer_id] = "date"
            self.peer_dates_message_ids.pop(int(peer_id), None)
            await self._send_check_date(peer_id, payload.get("date") or "", vk_id=vk_id)
            return
        if cmd == "check_event":
            await self._send_check_event(peer_id, payload.get("event_id"), vk_id=vk_id)
            return
        if cmd in {"best", "best_date_page"}:
            page = int(payload.get("page") or 0)
            if cmd == "best":
                self.peer_context[peer_id] = "best"
                self.peer_dates_message_ids.pop(int(peer_id), None)
                self._track(vk_id, EVENT_BRANCH_BEST)
                await self._send_text(
                    peer_id,
                    BEST_ENTRY_TEXT,
                    keyboard=event_search_keyboard(
                        "best_date_page",
                        "best_venues",
                        dates_label="📅 Выбрать по дате",
                        venues_label="📍 Выбор по локации",
                    ),
                    attachment=self._random_cover_attachment(),
                )
                return
            await self._send_best_dates(peer_id, page, edit="page" in payload)
            return
        if cmd == "best_venues":
            self.peer_browse[peer_id] = "venue"
            self.peer_dates_message_ids.pop(int(peer_id), None)
            await self._send_best_venues(peer_id)
            return
        if cmd == "best_venue":
            self.peer_browse[peer_id] = "venue"
            await self._send_best_venue(peer_id, payload.get("venue") or "", vk_id=vk_id)
            return
        if cmd == "best_carousel":
            self.peer_browse[peer_id] = "venue"
            await self._send_best_venue_carousel(
                peer_id,
                payload.get("venue") or "",
                int(payload.get("index") or 0),
                vk_id=vk_id,
                edit=True,
            )
            return
        if cmd == "best_carousel_pos":
            # Position marker button — keep current card.
            await self._delete_pending(peer_id)
            return
        if cmd == "best_venue_date":
            # Legacy path: open carousel around that date instead of a flat list.
            self.peer_browse[peer_id] = "venue"
            venue = payload.get("venue") or ""
            date = payload.get("date") or ""
            events = await self._best_venue_events(venue)
            index = next((i for i, e in enumerate(events) if e.get("date") == date), 0)
            await self._send_best_venue_carousel(peer_id, venue, index, vk_id=vk_id, edit=False)
            return
        if cmd == "best_date":
            self.peer_browse[peer_id] = "date"
            self.peer_dates_message_ids.pop(int(peer_id), None)
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
                self.peer_dates_message_ids.pop(int(peer_id), None)
                self._track(vk_id, EVENT_BRANCH_HITLOTO)
                await self._send_hitloto_dates(peer_id, page, entry=True, edit=False)
                return
            await self._send_hitloto_dates(peer_id, page, edit="page" in payload)
            return
        if cmd == "hitloto_date":
            self.peer_browse[peer_id] = "date"
            self.peer_dates_message_ids.pop(int(peer_id), None)
            await self._send_hitloto_date(peer_id, payload.get("date") or "", vk_id=vk_id)
            return
        if cmd == "hitloto_event":
            await self._send_hitloto_event(peer_id, payload.get("event_id"), vk_id=vk_id)
            return

        if await self._handle_raffle_flow(peer_id, vk_id, cmd=cmd, payload=payload):
            return

        # Как в TG unknown_message:
        # — абракадабра / короткий спам → молчим
        # — осмысленный текст → в техподдержку + «Спасибо!»
        if text and not cmd:
            await self._handle_unknown_free_text(peer_id, vk_id, text)
            return

    async def _handle_raffle_flow(
        self,
        peer_id: int,
        vk_id: int,
        *,
        cmd: str | None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        payload = payload or {}
        if cmd not in {
            "raffle",
            "rz_post",
            "rz_review",
            "rz_post_cross",
            "rz_post_screen",
            "rz_review_send",
            "rz_sub_check",
            "rz_dates",
            "rz_dates_page",
            "rz_date",
            "rz_event",
            "rz_book",
            "rz_rules",
        }:
            return False

        self._ensure_user(vk_id)

        if cmd == "raffle":
            await self._send_raffle_start(peer_id, vk_id)
            return True

        if cmd == "rz_sub_check":
            attempts = int(payload.get("attempts") or 0) + 1
            # Даты шлём через VK long-poll клиент — надёжнее, чем отдельный send_vk_text.
            if await vk_raffle.is_community_member(vk_id):
                self._track(
                    vk_id,
                    EVENT_RAFFLE_SUBSCRIBED,
                    props={"manual_attempts": attempts},
                )
                await self._send_raffle_dates(peer_id, vk_id, edit=False)
            else:
                self._track(
                    vk_id,
                    EVENT_RAFFLE_SUB_FAILED,
                    props={"manual_attempts": attempts},
                )
                await self._send_text(
                    peer_id,
                    "Не видим вашей подписки. Подпишись на сообщество и нажми кнопку ниже 👇",
                    keyboard=vk_raffle.subscribe_keyboard(
                        self.settings.community_link,
                        manual_attempts=attempts,
                    ),
                )
            return True

        if cmd == "rz_rules":
            # Как booking_rules: карточку шоу не удаляем.
            kb = VKKeyboardBuilder(inline=True)
            event_id = payload.get("event_id")
            if event_id is not None:
                kb.button("◀️ Назад к карточке", _payload("rz_event", event_id=event_id))
            else:
                kb.button("◀️ Назад к датам", _payload("rz_dates"))
            kb.adjust(1)
            await self._send_text(
                peer_id,
                vk_raffle.RAFFLE_RULES_TEXT,
                keyboard=kb.as_json(),
                replace_nav=False,
            )
            return True

        if cmd in {"rz_dates", "rz_dates_page"}:
            # На листании не дёргаем groups.isMember каждый раз.
            if cmd == "rz_dates" and not await vk_raffle.is_community_member(vk_id):
                await vk_raffle.continue_after_subscribe_check(vk_id)
                return True
            page = int(payload.get("page") or 0)
            await self._send_raffle_dates(peer_id, vk_id, page=page, edit=cmd == "rz_dates_page")
            return True

        if cmd == "rz_date":
            if not vk_raffle.guard_raffle_action(vk_id):
                await self._send_text(peer_id, vk_raffle.USED_RAFFLE_TEXT)
                return True
            if vk_raffle.get_active_raffle_booking_safe(vk_id):
                await self._send_text(
                    peer_id,
                    vk_raffle.ACTIVE_BOOKING_TEXT,
                    keyboard=vk_raffle.blocked_keyboard(
                        (vk_raffle.get_active_raffle_booking_safe(vk_id) or [None])[0]
                    ),
                )
                return True
            await self._send_raffle_date(peer_id, vk_id, payload.get("date") or "")
            return True

        if cmd == "rz_event":
            await self._send_raffle_event(peer_id, vk_id, payload.get("event_id"))
            return True

        if cmd == "rz_book":
            await self._start_raffle_booking(peer_id, vk_id, payload.get("event_id"))
            return True

        if not vk_raffle.guard_raffle_action(vk_id):
            await self._send_text(
                peer_id,
                vk_raffle.USED_RAFFLE_TEXT,
                keyboard=vk_raffle.blocked_keyboard(),
            )
            return True

        ok, reason, booking_id = vk_raffle.can_enter_raffle(vk_id)
        if not ok and cmd in {"rz_post", "rz_review"}:
            await self._send_text(
                peer_id,
                reason,
                keyboard=vk_raffle.blocked_keyboard(booking_id),
            )
            return True

        if cmd == "rz_post":
            self._track(vk_id, EVENT_RAFFLE_BRANCH, props={"kind": "post"})
            await self._disable_callback_buttons(
                peer_id,
                vk_raffle.start_text(self.settings.community_link),
            )
            post_att = self._random_cover_attachment()
            self._remember_raffle_attachment(peer_id, post_att)
            await self._send_text(
                peer_id,
                vk_raffle.POST_TEXT,
                keyboard=vk_raffle.post_keyboard(),
                attachment=post_att,
                replace_nav=False,
            )
            return True

        if cmd == "rz_review":
            self._track(vk_id, EVENT_RAFFLE_BRANCH, props={"kind": "review"})
            await self._disable_callback_buttons(
                peer_id,
                vk_raffle.start_text(self.settings.community_link),
            )
            await self._send_raffle_review(peer_id)
            return True

        if cmd == "rz_post_cross":
            self._arm_raffle_screenshot(vk_id, "post")
            await self._disable_callback_buttons(peer_id, vk_raffle.POST_TEXT)
            await self._send_text(
                peer_id,
                "Спасибо, но ждём скрин поста (одним фото) 😉 Кидай ниже 👇",
                replace_nav=False,
            )
            return True

        if cmd == "rz_post_screen":
            self._arm_raffle_screenshot(vk_id, "post")
            await self._disable_callback_buttons(peer_id, vk_raffle.POST_TEXT)
            await self._send_text(
                peer_id,
                "Супер, кидай сюда скрин (одним фото) 👇",
                replace_nav=False,
            )
            return True

        if cmd == "rz_review_send":
            self._arm_raffle_screenshot(vk_id, "review")
            # Примеры отзыва — отдельным сообщением выше; тут только текст с кнопкой.
            await self._disable_callback_buttons(peer_id, vk_raffle.REVIEW_TEXT)
            await self._send_text(
                peer_id,
                "Супер, кидай сюда скрин отзыва (одним фото) 👇",
                replace_nav=False,
            )
            return True

        return False

    def _arm_raffle_screenshot(self, vk_id: int, kind: str) -> None:
        from bot.db.crud import ensure_raffle_tables, set_raffle_vk_awaiting_screenshot

        ensure_raffle_tables()
        set_raffle_vk_awaiting_screenshot(int(vk_id), kind)
        self.raffle_sessions[int(vk_id)] = {
            "kind": kind,
            "screen_requested": True,
        }

    def _clear_raffle_screenshot_wait(self, vk_id: int) -> None:
        from bot.db.crud import clear_raffle_vk_awaiting_screenshot

        self.raffle_sessions.pop(int(vk_id), None)
        clear_raffle_vk_awaiting_screenshot(int(vk_id))

    async def _reset_raffle_for_test(self, peer_id: int, vk_id: int) -> None:
        """Сброс своей ветки розыгрыша для повторного теста (как TG /reset_rozygrysh)."""
        from bot.config import ROZYGRYSH_SKIP_SUB_CHECK
        from bot.db.crud import reset_raffle_for_user

        if not ROZYGRYSH_SKIP_SUB_CHECK:
            await self._send_text(peer_id, "Команда недоступна.", replace_nav=False)
            return

        stats = reset_raffle_for_user(vk_id=int(vk_id))
        vk_booking.clear_session(self.booking_sessions, vk_id)
        vk_mb.clear_manage_session(self.manage_sessions, vk_id)
        self._clear_raffle_screenshot_wait(int(vk_id))
        self._raffle_photo_burst.pop(int(vk_id), None)
        self.raffle_msg_attachment.pop(int(peer_id), None)

        entry = raffle_entry_link(self.settings)
        await self._send_text(
            peer_id,
            (
                "Розыгрыш сброшен для тебя ✅\n\n"
                "• флаг использован: сброшен\n"
                f"• отменено броней: {stats.get('bookings_cancelled', 0)}\n"
                f"• снято заявок на модерации: {stats.get('submissions_cancelled', 0)}\n\n"
                "Можно снова открыть словом «розыгрыш» или по ссылке:\n"
                f"{entry}\n\n"
                "После перехода напиши любое сообщение или нажми «Начать» — "
                "иначе VK не передаст параметр ссылки."
            ),
            replace_nav=False,
        )

    def _resolve_raffle_screenshot_kind(self, vk_id: int) -> str | None:
        """kind из памяти или БД (после отказа модератором / рестарта VK-бота)."""
        from bot.db.crud import get_raffle_vk_awaiting_screenshot

        session = self.raffle_sessions.get(int(vk_id)) or {}
        kind = session.get("kind") if session.get("screen_requested") else None
        if kind in {"post", "review"}:
            return kind
        kind = get_raffle_vk_awaiting_screenshot(int(vk_id))
        if kind in {"post", "review"}:
            self.raffle_sessions[int(vk_id)] = {
                "kind": kind,
                "screen_requested": True,
            }
            return kind
        return None

    async def _send_raffle_dates(
        self,
        peer_id: int,
        vk_id: int,
        *,
        page: int = 0,
        edit: bool = False,
    ) -> None:
        events = await vk_raffle.future_best_events()
        dates = sorted(
            {e["date"] for e in events},
            key=lambda d: datetime.strptime(d, "%d.%m.%Y"),
        )
        if not dates:
            await self._send_text(
                peer_id,
                "Отлично, подписка на сообщество есть 🙌\n\n"
                "Пока нет доступных дат для бесплатного билета 😔 Загляни позже!",
                keyboard=self._main_menu_kb(vk_id),
            )
            return
        text = (
            "Отлично, подписка на сообщество есть 🙌\n\n"
            "Теперь выбирай дату, на которую хочешь получить бесплатный билет 😉"
        )
        try:
            keyboard = vk_raffle.dates_keyboard(dates, page=page)
        except Exception:
            logger.exception("raffle dates_keyboard failed vk_id=%s", vk_id)
            await self._send_text(
                peer_id,
                text + "\n\nНе удалось показать кнопки дат. Напиши «розыгрыш» ещё раз.",
            )
            return
        # Без картинки: вложение + inline keyboard у VK иногда уходит без кнопок.
        peer = int(peer_id)
        existing = self.peer_dates_message_ids.get(peer)
        if edit:
            # Важно: даты мог прислать TG-бот — message_id у VK-бота нет,
            # тогда листаем по cmid кнопки (иначе уйдёт второе сообщение).
            ok = await self._edit_card(
                peer_id,
                text,
                stored_message_id=existing,
                keyboard=keyboard,
                attachment=None,
            )
            if ok:
                if existing:
                    self.peer_dates_message_ids[peer] = int(existing)
                return
            logger.warning(
                "Raffle dates edit failed peer_id=%s msg_id=%s cmid=%s — send once",
                peer_id,
                existing,
                self._callback_cmid(peer_id),
            )
        mid = await self._send_text(
            peer_id,
            text,
            keyboard=keyboard,
            attachment=None,
        )
        if mid:
            self.peer_dates_message_ids[peer] = int(mid)

    async def _send_raffle_date(self, peer_id: int, vk_id: int, date: str) -> None:
        dates_text = (
            "Отлично, подписка на сообщество есть 🙌\n\n"
            "Теперь выбирай дату, на которую хочешь получить бесплатный билет 😉"
        )
        await self._disable_callback_buttons(peer_id, dates_text)
        self.peer_dates_message_ids.pop(int(peer_id), None)

        events = [e for e in await vk_raffle.future_best_events() if e.get("date") == date]
        if not events:
            await self._send_text(peer_id, "Эта дата уже недоступна. Выбери другую 👇")
            await self._send_raffle_dates(peer_id, vk_id, edit=False)
            return
        if len(events) == 1:
            await self._send_raffle_event(peer_id, vk_id, events[0]["id"])
            return
        await self._send_text(
            peer_id,
            f"Шоу на {format_date(date)} 👇",
            keyboard=vk_raffle.events_keyboard(events, date),
            replace_nav=False,
        )

    async def _send_raffle_event(self, peer_id: int, vk_id: int, event_id: Any) -> None:
        try:
            eid = int(event_id)
        except (TypeError, ValueError):
            await self._send_text(peer_id, "Мероприятие недоступно.")
            return
        event = next(
            (e for e in await vk_raffle.future_best_events() if int(e.get("id") or 0) == eid),
            None,
        )
        if not event:
            await self._send_text(peer_id, "Мероприятие недоступно.")
            return
        attachment = await self._event_poster_attachment(peer_id, event)
        await self._send_text(
            peer_id,
            vk_raffle.event_card_text(event),
            keyboard=vk_raffle.event_card_keyboard(eid),
            attachment=attachment,
        )

    async def _start_raffle_booking(self, peer_id: int, vk_id: int, event_id: Any) -> None:
        ok, reason, booking_id = vk_raffle.can_enter_raffle(vk_id)
        if not ok:
            await self._send_text(
                peer_id,
                reason,
                keyboard=vk_raffle.blocked_keyboard(booking_id),
            )
            return
        try:
            eid = int(event_id)
        except (TypeError, ValueError):
            await self._send_text(peer_id, "Мероприятие недоступно.")
            return
        event = next(
            (e for e in await vk_raffle.future_best_events() if int(e.get("id") or 0) == eid),
            None,
        )
        if not event:
            await self._send_text(peer_id, "Мероприятие недоступно.")
            return

        warn = same_day_booking_warning(
            event_date=event.get("date") or "",
            vk_id=vk_id,
        )
        if warn:
            await self._send_text(peer_id, format_vk_text(warn))

        session = vk_booking.start_session(self.booking_sessions, vk_id, event)
        session["booking_format"] = "rozygrysh"
        session["event_format"] = "best"
        session["guests_fixed"] = 1
        self._track(vk_id, EVENT_BOOKING_START, props={"format": "rozygrysh"})
        await self._ask_name_or_phone_raffle(peer_id, vk_id, session)

    async def _ask_name_or_phone_raffle(self, peer_id: int, vk_id: int, session: dict) -> None:
        await self._ask_name(peer_id, vk_id, session)

    async def _send_raffle_start(self, peer_id: int, vk_id: int) -> None:
        self._track(vk_id, EVENT_RAFFLE_ENTER)
        ok, reason, booking_id = vk_raffle.can_enter_raffle(vk_id)
        if not ok:
            await self._send_text(
                peer_id,
                reason,
                keyboard=vk_raffle.blocked_keyboard(booking_id),
            )
            return
        self._clear_raffle_screenshot_wait(vk_id)
        start_att = self._random_cover_attachment()
        self._remember_raffle_attachment(peer_id, start_att)
        await self._send_text(
            peer_id,
            vk_raffle.start_text(self.settings.community_link),
            keyboard=vk_raffle.start_keyboard(),
            attachment=start_att,
            replace_nav=False,
        )

    async def _send_raffle_review(self, peer_id: int) -> None:
        attachments: list[str] = []
        for key in ("rozygrysh_otzyv_1", "rozygrysh_otzyv_2"):
            att = self._cover_attachment(key)
            if att:
                attachments.append(att)
        # Картинки-примеры отдельным сообщением — не пропадают при снятии кнопок с текста.
        if attachments:
            await self._send_text(
                peer_id,
                "Пример, какие кнопочки нажать на отзыве 👇",
                attachment=",".join(attachments),
                replace_nav=False,
            )
        self._remember_raffle_attachment(peer_id, None)
        await self._send_text(
            peer_id,
            vk_raffle.REVIEW_TEXT,
            keyboard=vk_raffle.review_keyboard(),
            replace_nav=False,
        )

    async def _handle_raffle_screenshot(
        self,
        peer_id: int,
        vk_id: int,
        message: dict[str, Any],
    ) -> None:
        from aiogram.types import BufferedInputFile

        from bot.db.crud import (
            cancel_raffle_submission,
            create_raffle_submission,
            ensure_raffle_tables,
            get_pending_raffle_submission,
        )
        from bot.handlers.rozygrysh import _send_to_moderation

        kind = self._resolve_raffle_screenshot_kind(vk_id)
        if kind not in {"post", "review"}:
            self._clear_raffle_screenshot_wait(vk_id)
            return

        url, ref = vk_raffle.extract_photo_from_message(message)
        if ref == "album":
            text = (
                vk_raffle.ALBUM_TEXT_REVIEW
                if kind == "review"
                else vk_raffle.ALBUM_TEXT_POST
            )
            await self._send_text(peer_id, text)
            return
        if not url:
            # Нет фото — если есть осмысленный текст, пусть уйдёт в help; иначе подсказка.
            text = (message.get("text") or "").strip()
            if text and is_meaningful_free_text(text):
                await self._handle_unknown_free_text(peer_id, vk_id, text)
            else:
                await self._send_text(peer_id, vk_raffle.NOT_IMAGE_TEXT)
            return

        # Несколько сообщений с фото подряд (VK часто шлёт альбом отдельными message_new).
        now = time.monotonic()
        last_burst = self._raffle_photo_burst.get(int(vk_id)) or 0.0
        if now - last_burst < 8.0:
            text = (
                vk_raffle.ALBUM_TEXT_REVIEW
                if kind == "review"
                else vk_raffle.ALBUM_TEXT_POST
            )
            await self._send_text(peer_id, text)
            return
        self._raffle_photo_burst[int(vk_id)] = now

        ensure_raffle_tables()
        pending = get_pending_raffle_submission(vk_id=vk_id)
        if pending:
            if not pending[4]:
                cancel_raffle_submission(pending[0], reason="stale_undelivered")
            else:
                self._clear_raffle_screenshot_wait(vk_id)
                await self._send_text(peer_id, vk_raffle.PENDING_SCREEN_TEXT)
                return

        # Сразу снимаем ожидание — повтор/гонка не отправят второй скрин.
        self._clear_raffle_screenshot_wait(vk_id)

        try:
            image_bytes = await vk_raffle.download_screenshot_bytes(url)
        except Exception:
            logger.exception("Failed to download VK raffle screenshot vk_id=%s", vk_id)
            self._arm_raffle_screenshot(vk_id, kind)
            await self._send_text(
                peer_id,
                "Не удалось скачать скрин. Пришли фото ещё раз 👇",
            )
            return

        try:
            full_name = await self.client.get_user_display_name(vk_id) or "Гость"
        except Exception:
            logger.exception("Failed to load VK name for raffle vk_id=%s", vk_id)
            full_name = "Гость"

        photo_ref = ref or f"vk_bytes:{vk_id}"
        try:
            submission_id = create_raffle_submission(
                None,
                None,
                full_name,
                kind,
                photo_ref,
                vk_id=vk_id,
                source_chat_id=peer_id,
                source_message_id=message.get("id"),
            )
        except Exception:
            logger.exception("Failed to create VK raffle submission vk_id=%s", vk_id)
            self._arm_raffle_screenshot(vk_id, kind)
            await self._send_text(
                peer_id,
                "Не удалось отправить скрин на проверку. Попробуй позже или напиши менеджеру.",
            )
            return

        self._track(
            vk_id,
            EVENT_RAFFLE_SCREENSHOT,
            props={"kind": kind, "submission_id": submission_id},
        )

        photo = BufferedInputFile(image_bytes, filename=f"raffle_{submission_id}.jpg")
        sent_ok = await _send_to_moderation(
            submission_id,
            None,
            None,
            full_name,
            kind,
            photo,
            vk_id=vk_id,
        )
        if sent_ok:
            await self._send_text(peer_id, vk_raffle.SCREEN_OK_TEXT)
            return

        cancel_raffle_submission(submission_id, reason="moderation_send_failed")
        self._arm_raffle_screenshot(vk_id, kind)
        await self._send_text(
            peer_id,
            "Не удалось отправить скрин менеджеру 😔\nПришли скрин ещё раз одним фото.",
        )

    async def _handle_unknown_free_text(self, peer_id: int, vk_id: int, text: str) -> None:
        if not is_meaningful_free_text(text):
            return
        self._track(vk_id, EVENT_HELP_QUESTION, props={"chars": len(text.strip())})
        forwarded = await self._forward_vk_help_question(vk_id, text)
        kb = VKKeyboardBuilder(inline=True)
        if not forwarded:
            kb.button("Задать вопрос менеджеру", link=self.settings.manager_link)
        kb.button("В главное меню", _payload("main_menu"))
        kb.adjust(1)
        await self._send_text(
            peer_id,
            "Спасибо! Передали вопрос в техподдержку, скоро ответим.",
            keyboard=kb.as_json(),
        )

    async def _forward_vk_help_question(self, vk_id: int, text: str) -> bool:
        """Карточка в Telegram HELP_CHAT + запись help_requests (как TG /help)."""
        try:
            help_chat_id = int(HELP_CHAT_ID) if HELP_CHAT_ID else 0
        except (TypeError, ValueError):
            help_chat_id = 0
        if not BOT_TOKEN or not help_chat_id:
            return False

        ensure_help_tables()
        phone = ""
        try:
            phone = get_last_phone(vk_id=vk_id) or ""
        except Exception:
            logger.exception("Failed to load phone for VK help vk_id=%s", vk_id)

        from bot.handlers.start import _help_card_text

        body = _help_card_text(
            telegram_id=None,
            full_name=None,
            username=None,
            question=text.strip(),
            phone=phone or None,
            vk_id=int(vk_id),
        )
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json={
                        "chat_id": help_chat_id,
                        "text": body,
                        "parse_mode": "HTML",
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    data = await resp.json(content_type=None)
            if not data.get("ok"):
                logger.warning("TG help forward failed for VK vk_id=%s: %s", vk_id, data)
                return False
            message_id = ((data.get("result") or {}) or {}).get("message_id")
            if not message_id:
                logger.warning("TG help forward without message_id vk_id=%s: %s", vk_id, data)
                return False
            create_help_request(
                None,
                None,
                None,
                text.strip(),
                help_chat_id,
                int(message_id),
                vk_id=int(vk_id),
            )
            return True
        except Exception:
            logger.exception("TG help forward error for VK vk_id=%s", vk_id)
            return False

    async def _send_venues(self, peer_id: int) -> None:
        self.peer_venues_message_ids.pop(int(peer_id), None)
        await self._send_text(
            peer_id,
            VENUES_INTRO_TEXT,
            keyboard=_venues_intro_keyboard(),
        )

    async def _send_venue_card(self, peer_id: int, index: int = 0, *, edit: bool = False) -> None:
        """Карусель из 3 карточек площадок — как в TG («Смотреть ещё»)."""
        if not VENUE_CARDS:
            await self._send_text(peer_id, "Пока нет карточек площадок.", keyboard=self._main_menu_kb(peer_id))
            return
        index = int(index or 0) % len(VENUE_CARDS)
        card = VENUE_CARDS[index]
        text = format_vk_text(card.get("fallback_html") or "")
        keyboard = _venues_card_keyboard(index)
        key = _venue_card_attachment_key(card)
        attachment = self.images.get(key) if key else None
        peer = int(peer_id)
        existing_id = self.peer_venues_message_ids.get(peer)

        if edit and await self._edit_card(
            peer_id,
            text,
            stored_message_id=existing_id,
            keyboard=keyboard,
            attachment=attachment or None,
        ):
            if existing_id:
                self.peer_venues_message_ids[peer] = int(existing_id)
            return
        if edit and (existing_id or self._callback_cmid(peer_id)):
            logger.warning(
                "Venue card edit failed peer_id=%s msg_id=%s cmid=%s, falling back to send",
                peer_id,
                existing_id,
                self._callback_cmid(peer_id),
            )

        mid = await self._send_text(
            peer_id,
            text,
            keyboard=keyboard,
            attachment=attachment,
        )
        if mid:
            self.peer_venues_message_ids[peer] = int(mid)

    async def _send_check_dates(self, peer_id: int, page: int = 0, *, edit: bool = False) -> None:
        self.peer_context[peer_id] = "check"
        logger.info("Loading check dates for peer_id=%s page=%s", peer_id, page)
        try:
            events = await self._load_events("proverka")
        except Exception:
            logger.exception("Failed to load check events")
            self.peer_dates_message_ids.pop(int(peer_id), None)
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
            self.peer_dates_message_ids.pop(int(peer_id), None)
            await self.client.send_message(peer_id, "Пока нет актуальных дат.", keyboard=self._main_menu_kb(peer_id))
            return
        max_page = max(0, (len(dates) - 1) // DATES_PAGE_SIZE)
        page = max(0, min(int(page or 0), max_page))
        keyboard = _dates_keyboard(dates, "check_date", page, "main_menu", venues_cmd="check_venues")
        self._track(peer_id, EVENT_BROWSE_DATES, props={"format": "proverka", "page": page})
        await self._send_or_edit_dates_card(
            peer_id,
            "Проверка материала. Выбирай дату:",
            keyboard=keyboard,
            attachment=self._random_cover_attachment(),
            edit=edit,
        )
        logger.info("Sent check dates: %s items page=%s", len(dates), page)

    async def _send_check_venues(self, peer_id: int) -> None:
        self.peer_context[peer_id] = "check"
        events = await self._load_events("proverka")
        venues = sorted({e["location"] for e in events if e.get("location")})
        if not venues:
            await self.client.send_message(peer_id, "Пока нет актуальных площадок.", keyboard=self._main_menu_kb(peer_id))
            return
        await self._send_text(
            peer_id,
            "Выбирай площадку:",
            keyboard=_venues_keyboard(venues, "check_venue", "check"),
            attachment=self._random_cover_attachment(),
        )
        self._track(peer_id, EVENT_BROWSE_VENUES, props={"format": "proverka"})

    async def _send_check_venue(self, peer_id: int, venue: str, *, vk_id: int | None = None) -> None:
        events = sorted(
            [e for e in await self._load_events("proverka") if e.get("location") == venue],
            key=lambda e: datetime.strptime(e["date"], "%d.%m.%Y"),
        )
        if not events:
            await self.client.send_message(peer_id, "На этой площадке пока нет актуальных дат.", keyboard=self._main_menu_kb(peer_id))
            return
        if len(events) == 1:
            await self._send_check_event(peer_id, events[0]["id"], vk_id=vk_id or peer_id)
            return
        dates = sorted({e["date"] for e in events}, key=lambda d: datetime.strptime(d, "%d.%m.%Y"))
        await self._send_text(
            peer_id,
            f"Мероприятия в {venue}",
            keyboard=_venue_dates_keyboard(dates, "check_venue_date", venue, "check_venues"),
            attachment=self._random_cover_attachment(),
        )

    async def _send_check_venue_date(
        self,
        peer_id: int,
        venue: str,
        date: str,
        *,
        vk_id: int | None = None,
    ) -> None:
        events = sorted(
            [
                e
                for e in await self._load_events("proverka")
                if e.get("location") == venue and e.get("date") == date
            ],
            key=lambda e: e.get("time") or "",
        )
        if not events:
            await self.client.send_message(peer_id, "Эта дата уже недоступна.")
            await self._send_check_venues(peer_id)
            return
        if len(events) == 1:
            await self._send_check_event(peer_id, events[0]["id"], vk_id=vk_id or peer_id)
            return
        await self._send_text(
            peer_id,
            f"Шоу на {_date_label(date)}:",
            keyboard=_events_keyboard(events, "check_event", "check_venue", venue=venue),
            attachment=self._random_cover_attachment(),
        )

    async def _send_check_date(self, peer_id: int, date: str, *, vk_id: int | None = None) -> None:
        events = [e for e in await self._load_events("proverka") if e["date"] == date]
        if not events:
            await self.client.send_message(peer_id, "Эта дата уже недоступна.", keyboard=self._main_menu_kb(peer_id))
            return
        if len(events) == 1:
            await self._send_check_event(peer_id, events[0]["id"], vk_id=vk_id or peer_id)
            return
        await self._send_text(
            peer_id,
            f"Шоу на {_date_label(date)}:",
            keyboard=_events_keyboard(events, "check_event", "check"),
            attachment=self._random_cover_attachment(),
        )

    async def _send_check_event(self, peer_id: int, event_id: Any, *, vk_id: int | None = None) -> None:
        event = next((e for e in await self._load_events("proverka") if str(e["id"]) == str(event_id)), None)
        if not event:
            await self.client.send_message(peer_id, "Мероприятие уже недоступно.", keyboard=self._main_menu_kb(peer_id))
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
        kb = VKKeyboardBuilder(inline=True)
        kb.button("Забронировать", _payload("check_booking_start", event_id=event["id"]), color="primary")
        kb.button("Правила бронирования", _payload("booking_rules", event_id=event["id"]))
        kb.button("Назад к датам", _payload("check"))
        kb.adjust(1)
        attachment = await self._event_poster_attachment(peer_id, event)
        await self._send_text(
            peer_id,
            format_vk_text(_event_text(event)),
            keyboard=kb.as_json(),
            attachment=attachment,
        )

    async def _send_best_dates(self, peer_id: int, page: int = 0, *, edit: bool = False) -> None:
        self.peer_context[peer_id] = "best"
        logger.info("Loading BEST dates for peer_id=%s page=%s", peer_id, page)
        try:
            events = await self._load_events("best")
        except Exception:
            logger.exception("Failed to load BEST events")
            self.peer_dates_message_ids.pop(int(peer_id), None)
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
            self.peer_dates_message_ids.pop(int(peer_id), None)
            await self.client.send_message(peer_id, "Пока нет актуальных мероприятий BEST.", keyboard=self._main_menu_kb(peer_id))
            return
        max_page = max(0, (len(dates) - 1) // DATES_PAGE_SIZE)
        page = max(0, min(int(page or 0), max_page))
        keyboard = _dates_keyboard(dates, "best_date", page, "main_menu", venues_cmd="best_venues")
        self._track(peer_id, EVENT_BROWSE_DATES, props={"format": "best", "page": page})
        await self._send_or_edit_dates_card(
            peer_id,
            "StandUp BEST. Выбирай дату:",
            keyboard=keyboard,
            attachment=self._random_cover_attachment(),
            edit=edit,
        )
        logger.info("Sent BEST dates: %s items page=%s", len(dates), page)

    async def _send_best_venues(self, peer_id: int) -> None:
        self.peer_context[peer_id] = "best"
        self.peer_carousel_message_ids.pop(int(peer_id), None)
        events = await self._load_events("best")
        venues = sorted({e["location"] for e in events if e.get("location")})
        if not venues:
            await self.client.send_message(peer_id, "Пока нет актуальных площадок BEST.", keyboard=self._main_menu_kb(peer_id))
            return
        await self._send_text(
            peer_id,
            "BEST: выбирай площадку:",
            keyboard=_venues_keyboard(venues, "best_venue", "best"),
            attachment=self._random_cover_attachment(),
        )
        self._track(peer_id, EVENT_BROWSE_VENUES, props={"format": "best"})

    async def _best_venue_events(self, venue: str) -> list[dict[str, Any]]:
        return sorted(
            [e for e in await self._load_events("best") if e.get("location") == venue],
            key=lambda e: datetime.strptime(f"{e['date']} {e.get('time') or '00:00'}", "%d.%m.%Y %H:%M"),
        )

    async def _send_best_venue(self, peer_id: int, venue: str, *, vk_id: int | None = None) -> None:
        events = await self._best_venue_events(venue)
        if not events:
            await self.client.send_message(
                peer_id,
                "На этой площадке пока нет актуальных BEST.",
                keyboard=self._main_menu_kb(peer_id),
            )
            return
        await self._send_best_venue_carousel(peer_id, venue, 0, vk_id=vk_id, edit=False)

    async def _send_best_venue_carousel(
        self,
        peer_id: int,
        venue: str,
        index: int = 0,
        *,
        vk_id: int | None = None,
        edit: bool = False,
    ) -> None:
        # Не путаем с каруселью «Мои брони».
        self.peer_my_bookings_message_ids.pop(int(peer_id), None)
        events = await self._best_venue_events(venue)
        if not events:
            self.peer_carousel_message_ids.pop(int(peer_id), None)
            await self.client.send_message(
                peer_id,
                "На этой площадке пока нет актуальных BEST.",
                keyboard=self._main_menu_kb(peer_id),
            )
            return
        index = max(0, min(int(index or 0), len(events) - 1))
        event = events[index]
        user_id = vk_id or peer_id
        payment_url = event.get("payment_url") or ""
        self._track(
            user_id,
            EVENT_SHOW_CARD,
            event_id=event.get("id"),
            props={
                "format": "best",
                "browse": "venue",
                "carousel": True,
                "index": index,
                "total": len(events),
                "date": event.get("date"),
                "time": event.get("time"),
                "location": event.get("location"),
                "has_payment": bool(payment_url),
            },
        )
        text = format_vk_text(_best_event_text_vk(event))
        keyboard = _best_carousel_keyboard(
            venue,
            index,
            len(events),
            payment_url=payment_url,
            manager_link=self.settings.manager_link,
        )
        attachment = await self._event_poster_attachment(peer_id, event)
        peer = int(peer_id)
        existing_id = self.peer_carousel_message_ids.get(peer)

        if edit and await self._edit_card(
            peer_id,
            text,
            stored_message_id=existing_id,
            keyboard=keyboard,
            # Пустой attachment снимает картинку и даёт мигание; None — не трогаем.
            attachment=attachment or None,
        ):
            if existing_id:
                self.peer_carousel_message_ids[peer] = int(existing_id)
            return
        if edit and (existing_id or self._callback_cmid(peer_id)):
            logger.warning(
                "BEST carousel edit failed peer_id=%s msg_id=%s cmid=%s, falling back to send",
                peer_id,
                existing_id,
                self._callback_cmid(peer_id),
            )

        mid = await self._send_text(
            peer_id,
            text,
            keyboard=keyboard,
            attachment=attachment,
        )
        if mid:
            self.peer_carousel_message_ids[peer] = int(mid)

    async def _send_best_date(self, peer_id: int, date: str, *, vk_id: int | None = None) -> None:
        events = [e for e in await self._load_events("best") if e["date"] == date]
        if not events:
            await self.client.send_message(peer_id, "Эта дата уже недоступна.", keyboard=self._main_menu_kb(peer_id))
            return
        if len(events) == 1:
            await self._send_best_event(peer_id, events[0]["id"], vk_id=vk_id or peer_id)
            return
        await self._send_text(
            peer_id,
            f"BEST на {_date_label(date)}:",
            keyboard=_events_keyboard(events, "best_event", "best"),
            attachment=self._random_cover_attachment(),
        )

    async def _send_best_event(self, peer_id: int, event_id: Any, *, vk_id: int | None = None) -> None:
        event = next((e for e in await self._load_events("best") if str(e["id"]) == str(event_id)), None)
        if not event:
            await self.client.send_message(peer_id, "Мероприятие уже недоступно.", keyboard=self._main_menu_kb(peer_id))
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
        kb = VKKeyboardBuilder(inline=True)
        if payment_url:
            kb.button("Купить билет", link=payment_url)
        else:
            kb.button("Задать вопрос менеджеру", link=self.settings.manager_link)
        kb.button("Назад к датам", _payload("best"))
        kb.adjust(1)
        attachment = await self._event_poster_attachment(peer_id, event)
        await self._send_text(
            peer_id,
            format_vk_text(_best_event_text_vk(event)),
            keyboard=kb.as_json(),
            attachment=attachment,
        )

    async def _send_hitloto_dates(
        self,
        peer_id: int,
        page: int = 0,
        *,
        entry: bool = False,
        edit: bool = False,
    ) -> None:
        self.peer_context[peer_id] = "hitloto"
        try:
            events = await self._load_events("hitloto")
        except Exception:
            logger.exception("Failed to load Hitloto events")
            self.peer_dates_message_ids.pop(int(peer_id), None)
            await self.client.send_message(
                peer_id,
                "Не удалось загрузить даты. Попробуй ещё раз через минуту.",
                keyboard=paid_formats_keyboard(),
            )
            return
        dates = sorted({e["date"] for e in events}, key=lambda d: datetime.strptime(d, "%d.%m.%Y"))
        if not dates:
            self.peer_dates_message_ids.pop(int(peer_id), None)
            kb = VKKeyboardBuilder(inline=True)
            kb.button("Задать вопрос менеджеру", link=self.settings.manager_link)
            kb.button("В главное меню", _payload("main_menu"))
            kb.adjust(1)
            await self.client.send_message(
                peer_id,
                "Пока нет актуальных мероприятий Хитлото. Можно уточнить расписание у менеджера.",
                keyboard=kb.as_json(),
            )
            return
        max_page = max(0, (len(dates) - 1) // DATES_PAGE_SIZE)
        page = max(0, min(int(page or 0), max_page))
        text = HITLOTO_ENTRY_TEXT if entry else "Выбирай дату 👇"
        keyboard = _dates_keyboard(dates, "hitloto_date", page, "main_menu")
        await self._send_or_edit_dates_card(
            peer_id,
            text,
            keyboard=keyboard,
            attachment=self._cover_attachment("hitloto_start", "show_cover"),
            edit=edit,
        )

    async def _send_hitloto_date(self, peer_id: int, date: str, *, vk_id: int | None = None) -> None:
        events = [e for e in await self._load_events("hitloto") if e["date"] == date]
        if not events:
            await self.client.send_message(peer_id, "Эта дата уже недоступна.", keyboard=self._main_menu_kb(peer_id))
            return
        if len(events) == 1:
            await self._send_hitloto_event(peer_id, events[0]["id"], vk_id=vk_id or peer_id)
            return
        await self._send_text(
            peer_id,
            f"Хитлото на {_date_label(date)}:",
            keyboard=_events_keyboard(events, "hitloto_event", "hitloto"),
            attachment=self._cover_attachment("hitloto_start", "show_cover"),
        )

    async def _send_hitloto_event(self, peer_id: int, event_id: Any, *, vk_id: int | None = None) -> None:
        event = next((e for e in await self._load_events("hitloto") if str(e["id"]) == str(event_id)), None)
        if not event:
            await self.client.send_message(peer_id, "Мероприятие уже недоступно.", keyboard=self._main_menu_kb(peer_id))
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
        kb = VKKeyboardBuilder(inline=True)
        if payment_url:
            kb.button("Купить билет", link=payment_url)
        else:
            kb.button("Задать вопрос менеджеру", link=self.settings.manager_link)
        kb.button("Назад к датам", _payload("hitloto"))
        kb.adjust(1)
        attachment = await self._event_poster_attachment(peer_id, event)
        await self._send_text(
            peer_id,
            format_vk_text(_event_text(event)),
            keyboard=kb.as_json(),
            attachment=attachment,
        )

    async def run(self) -> None:
        if EVENTS_SOURCE != "postgres" or not DATABASE_URL:
            raise RuntimeError(
                "VK bot requires EVENTS_SOURCE=postgres and DATABASE_URL. "
                "Google Sheets is not used for VK."
            )
        await self.client.ensure_long_poll_events()
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
