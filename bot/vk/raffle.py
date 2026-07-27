"""VK raffle (розыгрыш): entry + post/review branches.

Subscription check, dates/booking — later steps.
Screenshot → TG moderation: handled from app + notify helpers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bot.config import AFISHA_REVIEW_URL, SITE_URL
from bot.db.crud import (
    get_active_raffle_booking,
    get_pending_raffle_submission,
    get_rozygrysh_used,
)
from bot.utils.ticket import now_msk, parse_event_datetime
from bot.vk.formatting import format_vk_text
from bot.vk.keyboards import VKKeyboardBuilder
from bot.vk.media import download_image_bytes


USED_RAFFLE_TEXT = (
    "Ты уже использовал(а) возможность получить бесплатный билет по розыгрышу 😊"
)
ACTIVE_BOOKING_TEXT = (
    "У тебя уже есть активная бронь по розыгрышу. "
    "Дождись шоу или отмени бронь, если планы поменялись 😊"
)
TICKET_ISSUED_BLOCK_TEXT = (
    "Вы уже забронировали и получили билет по розыгрышу. "
    "Дождись шоу или отмени бронь, если планы поменялись 😊"
)
PENDING_SCREEN_TEXT = "Ваш скрин на модерации, ожидайте ⏳"
SCREEN_OK_TEXT = (
    "Супер, проверю твой скрин и вернусь обратно 👌\n\n"
    "Менеджер проверит скрин в течение часа, ожидайте."
)
NOT_IMAGE_TEXT = "Нужен именно скрин-картинка 📷 Пришли фото или изображение ещё раз 👇"
ALBUM_TEXT_POST = "Принимаем только 1 скрин поста — пришли одно фото 📷"
ALBUM_TEXT_REVIEW = "Принимаем только 1 скрин отзыва — пришли одно фото 📷"
SCREEN_ACCEPTED_TEXT = (
    "Класс, скрин принят. Теперь проверим подписку на сообщество 👌"
)


def start_text(community_link: str) -> str:
    link = (community_link or "").strip() or "сообщество VK"
    html = (
        "Привет-привет 🥳 😊\n\n"
        "Что нужно сделать, чтобы получить билетик?\n\n"
        f"1. Быть подписанным на наше <a href=\"{link}\">сообщество ВКонтакте</a>\n"
        "2. Выложить в соцсети <b>пост со ссылкой на наш сайт</b> или <b>оставить отзыв</b> 😊\n\n"
        "Выбирай, какой вариант тебе ближе 👇"
    )
    return format_vk_text(html)


POST_TEXT = format_vk_text(
    f"Выкладываем в соцсети пост со ссылкой на наш сайт <b>MoscowStandUpshow.ru</b> 😊\n\n"
    "Если в Instagram* — обязательно сделай ссылку в сторис кликабельной 😉\n\n"
    "Затем нажимай кнопку ниже, отправляй скрин поста <b>одним фото</b> "
    "и выбирай любую дату ☺️ "
    "(после выбора даты билеты переносу не подлежат)\n\n"
    "🎫 За 1 пост полагается 1 билет\n\n"
    "____________________\n"
    "<i>*запрещено в РФ</i>"
)

REVIEW_TEXT = format_vk_text(
    f"Оставляем отзыв по ссылке:\n{AFISHA_REVIEW_URL}\n\n"
    "И обязательно нажать на вот эти кнопочки как на фото 😻\n\n"
    "Затем нажимайте кнопку ниже, отправляйте скрин отзыва <b>одним фото</b> "
    "и выбирайте любую дату 😻\n\n"
    "🎟️ После выбора даты билеты переносу не подлежат\n"
    "🎫 За 1 отзыв полагается 1 билет"
)

POST_REJECT_TEXT = format_vk_text(
    "К сожалению скрин не прошел модерацию. 😔\n\n"
    "Необходимо выложить в соцсети пост со ссылкой на наш сайт :\n\n"
    f"<b>{SITE_URL.replace('https://', '').replace('http://', '')}</b> 😊\n\n"
    "Если в инстаграм* — обязательно сделать ссылку в сторис кликабельной 👌\n\n"
    "Отправь скрин с отметкой еще раз 👇\n\n"
    "____________________\n"
    "<i>*запрещено в РФ</i>"
)


def _payload(value: str, **extra) -> dict[str, Any]:
    data = {"cmd": value}
    data.update(extra)
    return data


def start_keyboard() -> str:
    kb = VKKeyboardBuilder(inline=True)
    kb.button("Билет за пост", _payload("rz_post"), color="primary")
    kb.button("Билет за отзыв", _payload("rz_review"), color="primary")
    kb.adjust(1)
    return kb.as_json()


def post_keyboard() -> str:
    """Крест / скрин — без «В главное меню»."""
    kb = VKKeyboardBuilder(inline=True)
    kb.button("Я выложил, вот те крест", _payload("rz_post_cross"), color="positive")
    kb.button("Я выложил, вот те скрин", _payload("rz_post_screen"), color="negative")
    kb.adjust(1)
    return kb.as_json()


def review_keyboard() -> str:
    """Кнопка отправки скрина отзыва — без «В главное меню»."""
    kb = VKKeyboardBuilder(inline=True)
    kb.button("Отправить скрин", _payload("rz_review_send"), color="positive")
    kb.adjust(1)
    return kb.as_json()


def retry_screenshot_keyboard(kind: str) -> str:
    """После отказа — снова кнопка скрина, без главного меню."""
    kb = VKKeyboardBuilder(inline=True)
    if kind == "review":
        kb.button("Отправить скрин", _payload("rz_review_send"), color="positive")
    else:
        kb.button("Я выложил, вот те скрин", _payload("rz_post_screen"), color="primary")
    kb.adjust(1)
    return kb.as_json()


def blocked_keyboard(booking_id: int | None = None) -> str:
    kb = VKKeyboardBuilder(inline=True)
    if booking_id:
        kb.button(
            "Отменить бронирование",
            _payload("mb_cancel_confirm", booking_id=int(booking_id)),
            color="negative",
        )
    kb.adjust(1)
    return kb.as_json()


def subscribe_keyboard(community_link: str, *, manual_attempts: int = 0) -> str:
    kb = VKKeyboardBuilder(inline=True)
    link = (community_link or "").strip()
    if link:
        kb.button("Подписаться", link=link)
    # Кнопку оставляем всегда — иначе после одной неудачной проверки пользователь застревает.
    kb.button("Подписка есть 🤝", _payload("rz_sub_check", attempts=int(manual_attempts or 0)))
    kb.adjust(1)
    return kb.as_json()


async def send_raffle_dates_message(vk_id: int) -> bool:
    """Сразу список дат BEST после успешной проверки подписки (как в TG)."""
    import logging

    logger = logging.getLogger(__name__)
    try:
        events = await future_best_events()
        dates = sorted(
            {e["date"] for e in events if e.get("date")},
            key=lambda d: datetime.strptime(d, "%d.%m.%Y"),
        )
    except Exception:
        logger.exception("future_best_events failed for vk_id=%s", vk_id)
        return await send_vk_text(
            vk_id,
            "Отлично, подписка на сообщество есть 🙌\n\n"
            "Не удалось загрузить даты. Напиши «розыгрыш» чуть позже или менеджеру.",
        )
    if not dates:
        return await send_vk_text(
            vk_id,
            "Отлично, подписка на сообщество есть 🙌\n\n"
            "Пока нет доступных дат для бесплатного билета 😔 Загляни позже!",
        )
    try:
        keyboard = dates_keyboard(dates, page=0)
    except Exception:
        logger.exception("dates_keyboard failed for vk_id=%s", vk_id)
        keyboard = None
    return await send_vk_text(
        vk_id,
        "Отлично, подписка на сообщество есть 🙌\n\n"
        "Теперь выбирай дату, на которую хочешь получить бесплатный билет 😉",
        keyboard=keyboard,
    )


def _raffle_event_passed(booking_row) -> bool:
    if not booking_row:
        return False
    event_dt = parse_event_datetime(booking_row[5], booking_row[6])
    if not event_dt:
        return False
    return event_dt <= now_msk().replace(tzinfo=None)


def can_enter_raffle(vk_id: int) -> tuple[bool, str, int | None]:
    """(ok, reason, active_booking_id или None для кнопки отмены)."""
    if get_pending_raffle_submission(vk_id=vk_id):
        return False, PENDING_SCREEN_TEXT, None

    active = get_active_raffle_booking(vk_id=vk_id)
    if active:
        booking_id = int(active[0])
        if _raffle_event_passed(active):
            return False, USED_RAFFLE_TEXT, None
        status = active[10]
        if status == "confirmed" or get_rozygrysh_used(vk_id=vk_id):
            return False, TICKET_ISSUED_BLOCK_TEXT, booking_id
        return False, ACTIVE_BOOKING_TEXT, booking_id

    if get_rozygrysh_used(vk_id=vk_id):
        return False, USED_RAFFLE_TEXT, None
    return True, "", None


def guard_raffle_action(vk_id: int) -> bool:
    """False = уже использовал и нет активной брони."""
    if get_rozygrysh_used(vk_id=vk_id) and not get_active_raffle_booking(vk_id=vk_id):
        return False
    return True


def largest_photo_url(photo: dict[str, Any]) -> str | None:
    sizes = photo.get("sizes") or []
    if not sizes:
        return None
    best = max(sizes, key=lambda s: int(s.get("width") or 0) * int(s.get("height") or 0))
    url = (best.get("url") or "").strip()
    return url or None


def _is_image_doc(doc: dict[str, Any]) -> bool:
    if doc.get("type") == 4:
        return True
    return str(doc.get("ext") or "").lower() in {"jpg", "jpeg", "png", "webp", "gif"}


def count_image_attachments(message: dict[str, Any]) -> int:
    attachments = message.get("attachments") or []
    total = 0
    for att in attachments:
        if not isinstance(att, dict):
            continue
        if att.get("type") == "photo":
            total += 1
        elif att.get("type") == "doc" and _is_image_doc(att.get("doc") or {}):
            total += 1
    return total


def extract_photo_from_message(message: dict[str, Any]) -> tuple[str | None, str | None]:
    """Returns (download_url, photo_ref) or (None, None / 'album')."""
    if count_image_attachments(message) > 1:
        return None, "album"
    attachments = message.get("attachments") or []
    photos = [a for a in attachments if isinstance(a, dict) and a.get("type") == "photo"]
    if not photos:
        for att in attachments:
            if not isinstance(att, dict) or att.get("type") != "doc":
                continue
            doc = att.get("doc") or {}
            if not _is_image_doc(doc):
                continue
            url = (doc.get("url") or "").strip()
            if url:
                ref = f"vk_doc:{doc.get('owner_id')}_{doc.get('id')}"
                return url, ref
        return None, None
    photo = photos[0].get("photo") or {}
    url = largest_photo_url(photo)
    if not url:
        return None, None
    ref = f"vk_photo:{photo.get('owner_id')}_{photo.get('id')}"
    return url, ref


async def download_screenshot_bytes(url: str) -> bytes:
    return await download_image_bytes(url)


async def send_vk_text(vk_id: int, text: str, *, keyboard: str | None = None) -> bool:
    """DM из TG-бота/админки в VK (нужны VK_GROUP_* в .env процесса)."""
    import asyncio
    import logging

    from bot.vk.client import VKClient, VKAPIError
    from bot.vk.config import load_vk_settings

    logger = logging.getLogger(__name__)
    settings = load_vk_settings()
    if not settings.is_configured:
        logger.error("VK settings not configured — cannot DM vk_id=%s", vk_id)
        return False
    client = VKClient(settings)

    async def _once(kb: str | None) -> None:
        await client.send_message(int(vk_id), text, keyboard=kb)

    try:
        await _once(keyboard)
        return True
    except Exception as exc:
        logger.exception("send_vk_text failed vk_id=%s", vk_id)
        msg = str(exc).lower()
        if "flood" in msg or "too many" in msg or isinstance(exc, VKAPIError):
            try:
                await asyncio.sleep(1.5)
                await _once(keyboard)
                return True
            except Exception:
                logger.exception("send_vk_text retry failed vk_id=%s", vk_id)
        # Клавиатуру не выкидываем молча: пробуем отдельным сообщением только с кнопками.
        if keyboard:
            try:
                await asyncio.sleep(0.5)
                await client.send_message(
                    int(vk_id),
                    "Выбери дату 👇",
                    keyboard=keyboard,
                )
                return True
            except Exception:
                logger.exception("send_vk_text keyboard-only failed vk_id=%s", vk_id)
            try:
                await _once(None)
            except Exception:
                logger.exception("send_vk_text text-only failed vk_id=%s", vk_id)
                return False
            return False
        return False


RAFFLE_DATES_PAGE_SIZE = 4  # 2×2 + стрелки ≤ 6 рядов inline-клавиатуры VK
RAFFLE_RULES_TEXT = format_vk_text(
    "<b>Порядок посещения шоу:</b>\n\n"
    "1. Сбор гостей начинается за полчаса до начала шоу\n\n"
    "2. Рассадка осуществляется администратором рассадки на ближайшие к сцене свободные места. "
    "Возможна подсадка за один стол других гостей для небольших компаний.\n"
    "❗ ВНИМАНИЕ, ваш билет на одного человека, если вы хотите пойти с друзьями, они могут "
    "купить билеты на выбранное Вами шоу через систему бронирования.\n\n"
    "3. Обратите внимание, что при посещении шоу заказ минимум одной позиции по меню является обязательным.\n\n"
    "4. Если поменяются планы и Вы не сможете присутствовать, пожалуйста, ОБЯЗАТЕЛЬНО ПРЕДУПРЕДИТЕ 😊\n\n"
    "5. После выбора даты билеты переносу не подлежат."
)


async def future_best_events() -> list[dict]:
    from bot.services.sheets import load_events

    today = now_msk().date()
    events = await load_events("best")
    result = []
    for e in events:
        try:
            d = datetime.strptime(e["date"], "%d.%m.%Y").date()
        except ValueError:
            continue
        if d > today:
            result.append(e)
    return result


def _date_label(date: str) -> str:
    from bot.utils.ticket import MONTHS

    try:
        d = datetime.strptime(date, "%d.%m.%Y")
        # Не используем strftime("%B"): на сервере локаль может быть ru_RU.
        month_en = (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        )[d.month - 1]
        return f"{d.day:02d} " + MONTHS[month_en]
    except Exception:
        return date


def dates_keyboard(dates: list[str], page: int = 0) -> str:
    """Кнопки дат: по 2 в ряд, без превышения лимита inline (≤10 кнопок, ≤6 рядов)."""
    page = max(int(page or 0), 0)
    start = page * RAFFLE_DATES_PAGE_SIZE
    end = start + RAFFLE_DATES_PAGE_SIZE
    shown = dates[start:end]
    kb = VKKeyboardBuilder(inline=True)
    for date in shown:
        kb.button(_date_label(date), _payload("rz_date", date=date), color="primary")
    if page > 0:
        kb.button("⬅️", _payload("rz_dates_page", page=page - 1))
    if end < len(dates):
        kb.button("➡️", _payload("rz_dates_page", page=page + 1))
    # Ряды: по 2 даты, затем стрелки.
    widths: list[int] = [2] * (len(shown) // 2)
    if len(shown) % 2:
        widths.append(1)
    nav = int(page > 0) + int(end < len(dates))
    if nav:
        widths.append(nav)
    if not widths:
        widths = [1]
    kb.adjust(*widths)
    return kb.as_json()


def events_keyboard(events: list[dict], date: str) -> str:
    kb = VKKeyboardBuilder(inline=True)
    for event in events[:8]:
        label = f"{event.get('time') or ''} · {event.get('location') or 'шоу'}".strip(" ·")
        kb.button(label or "Шоу", _payload("rz_event", event_id=event["id"]), color="primary")
    kb.button("◀️ Назад к датам", _payload("rz_dates"))
    kb.adjust(1)
    return kb.as_json()


def event_card_keyboard(event_id: int) -> str:
    kb = VKKeyboardBuilder(inline=True)
    kb.button("🎟 Забронировать билет", _payload("rz_book", event_id=event_id), color="primary")
    kb.button("📋 Правила бронирования", _payload("rz_rules", event_id=event_id))
    kb.button("◀️ Назад", _payload("rz_dates"))
    kb.adjust(1)
    return kb.as_json()


def event_card_text(event: dict) -> str:
    from bot.utils.ticket import format_date

    lines = [
        format_date(event.get("date") or ""),
        event.get("weekday") or "",
        "",
        event.get("time") or "",
        event.get("address") or "",
        event.get("description") or "",
    ]
    return "\n".join(line for line in lines if line is not None).strip()


def get_active_raffle_booking_safe(vk_id: int):
    return get_active_raffle_booking(vk_id=vk_id)


def _skip_community_sub_check() -> bool:
    """Читаем env напрямую — без import bot.config (в VK-боте может не быть BOT_TOKEN)."""
    import os

    return os.getenv("ROZYGRYSH_SKIP_SUB_CHECK", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def is_community_member(vk_id: int) -> bool:
    import logging

    from bot.vk.client import VKClient
    from bot.vk.config import load_vk_settings

    logger = logging.getLogger(__name__)
    if _skip_community_sub_check():
        logger.info("ROZYGRYSH_SKIP_SUB_CHECK=1 — skip VK community check for %s", vk_id)
        return True
    settings = load_vk_settings()
    if not settings.is_configured:
        logger.error("VK not configured — cannot check membership for %s", vk_id)
        return False
    client = VKClient(settings)
    try:
        return await client.is_group_member(int(vk_id))
    except Exception:
        logger.exception("groups.isMember failed for vk_id=%s", vk_id)
        return False


async def continue_after_subscribe_check(vk_id: int, *, manual_attempts: int = 0) -> bool:
    """После принятия скрина: проверка подписки на сообщество VK.

    Returns True if dates (or empty-dates notice) were delivered.
    """
    import logging

    from bot.db.analytics import EVENT_RAFFLE_SUB_FAILED, EVENT_RAFFLE_SUBSCRIBED, track_event
    from bot.vk.config import load_vk_settings

    logger = logging.getLogger(__name__)
    settings = load_vk_settings()
    try:
        subscribed = await is_community_member(vk_id)
    except Exception:
        logger.exception("is_community_member failed vk_id=%s", vk_id)
        subscribed = False

    if subscribed:
        try:
            track_event(
                EVENT_RAFFLE_SUBSCRIBED,
                vk_id=int(vk_id),
                channel="vkontakte",
                props={"manual_attempts": manual_attempts},
            )
        except Exception:
            logger.exception("track subscribed failed vk_id=%s", vk_id)
        ok = await send_raffle_dates_message(int(vk_id))
        if ok:
            return True
        logger.error("send_raffle_dates_message returned False vk_id=%s", vk_id)
        # Не молчим: даём кнопку, чтобы VK-бот отправил даты своим клиентом.
        await send_vk_text(
            vk_id,
            "Подписка есть, но список дат не отправился. Нажми кнопку ниже 👇",
            keyboard=subscribe_keyboard(
                settings.community_link,
                manual_attempts=manual_attempts,
            ),
        )
        return False

    try:
        track_event(
            EVENT_RAFFLE_SUB_FAILED,
            vk_id=int(vk_id),
            channel="vkontakte",
            props={"manual_attempts": manual_attempts},
        )
    except Exception:
        logger.exception("track sub failed vk_id=%s", vk_id)
    await send_vk_text(
        vk_id,
        "Не видим вашей подписки. Подпишись на сообщество и нажми кнопку ниже 👇",
        keyboard=subscribe_keyboard(
            settings.community_link,
            manual_attempts=manual_attempts,
        ),
    )
    return False
