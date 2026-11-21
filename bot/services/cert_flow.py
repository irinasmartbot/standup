"""Обычный сертификат: страница → фото → бронь на двоих → реестр сайта.

Джекпот, отметка прихода и неявки в эту ветку не входят.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from bot.db.cert_store import (
    CERT_CAP,
    SHOW_FORMATS,
    active_on_event,
    booking_context,
    certificate_booking_ids,
    close_drafts,
    get_draft,
    get_session,
    get_session_by_booking,
    insert_session,
    postgres_ready,
    update_session,
)
from bot.db.crud import create_booking, update_booking_status
from bot.services.cert_codes import (
    is_preview_code,
    normalize_certificate_code,
    parse_certificate_start,
    safe_ref,
)
from bot.services.cert_registry import (
    RegistryNotConfigured,
    RegistryRefusal,
    RegistryTemporary,
    registry,
)
from bot.services.cert_texts import (
    ALREADY_BOOKED_TEXT,
    APPROVED_TEXT,
    BRING_TEXT,
    CANCELLED_TEXT,
    CAP_TEXT,
    CONFIRMED_TEXT,
    DATE_ASK_TEXT,
    EMPTY_DATES_TEXT,
    EVENT_ASK_TEXT,
    FORMAT_ASK_TEXT,
    JACKPOT_TEXT,
    MISMATCH_TEXT,
    NAME_ASK_TEXT,
    NUMBER_ASK_TEXT,
    PENDING_TEXT,
    PHONE_ASK_TEXT,
    PHOTO_ASK_TEMPLATE,
    PREVIEW_TEXT,
    REJECT_TEXT,
    RESERVE_PENDING_TEXT,
    TRANSFER_TEXT,
    UNAVAILABLE_TEXT,
    WARN_TEXT,
)
from bot.services.sheets import get_event_by_id, load_events
from bot.utils.reminder_schedule import plan_booking_reminders
from bot.utils.show_formats import CERTIFICATE
from bot.utils.ticket import parse_event_datetime

logger = logging.getLogger(__name__)

FORMAT_LABELS = {
    "hitloto": "Хитлото",
    "best": "BEST",
    "elka": "Елка для взрослых",
}
_EXPIRE_OK = {
    "not_found",
    "already_expired",
    "already_cancelled",
    "booking_not_found",
    "expired",
    "cancelled",
}


def _step(text: str, *, expect: str = "none", buttons: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    return {"text": text, "expect": expect, "buttons": buttons or []}


def _guest_error(exc: Exception) -> str:
    if isinstance(exc, RegistryNotConfigured):
        return UNAVAILABLE_TEXT
    if isinstance(exc, RegistryTemporary):
        return "Сайт сертификатов не ответил. Повтори то же действие через минуту."
    if isinstance(exc, RegistryRefusal):
        if exc.http_status in {401, 403} or not exc.message:
            if exc.code in {"not_found", "unknown_code"}:
                return "Такой номер не найден."
            return exc.message or "Этот номер сейчас нельзя использовать."
        return exc.message
    return UNAVAILABLE_TEXT


def _identity(session: dict[str, Any]) -> dict[str, str]:
    channel = session["channel"]
    user_id = session["vk_id"] if channel == "vk" else session["telegram_id"]
    return {
        "code": session["code"],
        "channel": "vk" if channel == "vk" else "telegram",
        "user_id": str(int(user_id)),
        "booking_ref": session.get("booking_ref") or "",
    }


async def begin(
    *,
    channel: str,
    raw: str,
    telegram_id: int | None = None,
    vk_id: int | None = None,
    peer_id: int | None = None,
    ref_source: str = "",
) -> dict[str, Any]:
    if not postgres_ready() or not registry.configured():
        return _step(UNAVAILABLE_TEXT)
    token, code_raw = parse_certificate_start(raw)
    if not token and not code_raw and ref_source.casefold() == "mss_certificate":
        token = (raw or "").strip()
    close_drafts(telegram_id=telegram_id, vk_id=vk_id)
    session = insert_session(
        code="",
        channel="vk" if channel == "vk" else "telegram",
        telegram_id=telegram_id,
        vk_id=vk_id,
        peer_id=peer_id,
        state="await_number",
    )
    if token:
        try:
            data = await registry.resolve(handoff_token=token)
        except (RegistryNotConfigured, RegistryTemporary, RegistryRefusal) as exc:
            update_session(session["id"], state="closed")
            return _step(_guest_error(exc))
        return await _accept_resolved(session, data, typed="")
    if code_raw:
        return await submit_number(session, code_raw)
    return _step(NUMBER_ASK_TEXT, expect="number")


async def _accept_resolved(session: dict[str, Any], data: dict[str, Any], *, typed: str) -> dict[str, Any]:
    code = normalize_certificate_code(str(data.get("code") or typed))
    if not code or is_preview_code(code):
        update_session(session["id"], state="closed")
        return _step(PREVIEW_TEXT)
    environment = str(data.get("environment") or "").lower()
    if environment in {"test", "preview"}:
        update_session(session["id"], state="closed")
        return _step(PREVIEW_TEXT)
    kind = str(data.get("kind") or "").lower()
    if kind == "jackpot":
        update_session(session["id"], code=code, state="closed")
        return _step(JACKPOT_TEXT)
    if typed and normalize_certificate_code(typed) != code:
        return _step(MISMATCH_TEXT, expect="number")
    update_session(session["id"], code=code, state="await_photo")
    return _step(PHOTO_ASK_TEMPLATE.format(code=code), expect="photo")


async def submit_number(session: dict[str, Any], raw: str) -> dict[str, Any]:
    code = normalize_certificate_code(raw)
    if not code:
        return _step(NUMBER_ASK_TEXT, expect="number")
    if is_preview_code(code):
        return _step(PREVIEW_TEXT, expect="number")
    known = normalize_certificate_code(session.get("code") or "")
    if known and code != known:
        return _step(MISMATCH_TEXT, expect="number")
    try:
        data = await registry.resolve(code=code)
    except (RegistryNotConfigured, RegistryTemporary, RegistryRefusal) as exc:
        return _step(_guest_error(exc), expect="number")
    return await _accept_resolved(session, data, typed=code)


async def submit_photo(
    session: dict[str, Any],
    *,
    photo,
    photo_ref: str,
    username: str = "",
    full_name: str = "",
) -> dict[str, Any]:
    if session.get("state") != "await_photo":
        return _step("Сначала дождись проверки фото или начни сертификат по ссылке ещё раз.")
    code = session.get("code") or ""
    if not code:
        return _step(NUMBER_ASK_TEXT, expect="number")
    attempt = int(session.get("attempt") or 0) + 1
    channel = "vk" if session.get("channel") == "vk" else "telegram"
    request_ref = safe_ref(f"claim:{channel}:{session['id']}:{attempt}")
    update_session(
        session["id"],
        attempt=attempt,
        request_ref=request_ref,
        photo_ref=photo_ref[:500],
    )
    user_id = session["vk_id"] if channel == "vk" else session["telegram_id"]
    payload = {
        "code": code,
        "request_ref": request_ref,
        "channel": channel,
        "user_id": str(int(user_id)),
        "photo_ref": photo_ref[:500],
        "name": "",
        "phone": "",
    }
    try:
        data = await registry.claim(payload)
    except RegistryTemporary:
        return _step(
            "Фото не дошло до сайта. Пришли его ещё раз — это будет новая попытка.",
            expect="photo",
        )
    except (RegistryNotConfigured, RegistryRefusal) as exc:
        update_session(session["id"], state="await_photo")
        return _step(_guest_error(exc), expect="photo")

    claim_id = data.get("claim_id")
    if not claim_id:
        return _step("Сайт не вернул номер заявки. Пришли фото ещё раз.", expect="photo")
    from bot.services.cert_moderation import send_certificate_photo

    sent = await send_certificate_photo(
        session_id=int(session["id"]),
        code=code,
        channel=channel,
        telegram_id=session.get("telegram_id"),
        vk_id=session.get("vk_id"),
        username=username,
        full_name=full_name,
        photo=photo,
    )
    if not sent:
        return _step(UNAVAILABLE_TEXT, expect="photo")
    update_session(session["id"], claim_id=int(claim_id) if claim_id else None, state="pending_review")
    return _step(PENDING_TEXT)


async def review(
    session_id: int,
    *,
    approve: bool,
    reviewer_id: int,
    reason: str = "",
) -> tuple[dict[str, Any] | None, str]:
    session = get_session(session_id)
    if not session:
        return None, "Заявка не найдена"
    if session.get("state") != "pending_review":
        return session, "Уже обработано"
    if not session.get("claim_id"):
        return session, "Нет номера заявки на сайте"
    if not registry.configured(staff=True):
        return session, "Нет ключа сотрудника"
    payload = {
        "code": session["code"],
        "claim_id": int(session["claim_id"]),
        "decision": "approve" if approve else "reject",
        "kind": "pair",
        "reviewer": safe_ref(f"moderator:{int(reviewer_id)}"),
        "reason": (reason or "").strip()[:500],
    }
    try:
        await registry.review(payload)
    except RegistryTemporary:
        return session, "Сайт не ответил. Нажми кнопку ещё раз."
    except RegistryRefusal as exc:
        return session, exc.message or "Сайт не принял решение"
    except RegistryNotConfigured:
        return session, "Нет ключа сотрудника"
    if approve:
        session = update_session(session_id, state="need_name")
        return session, APPROVED_TEXT
    session = update_session(session_id, state="await_photo")
    return session, REJECT_TEXT


async def save_name(session: dict[str, Any], name: str) -> dict[str, Any]:
    clean = " ".join((name or "").split())
    if len(clean) < 2 or len(clean) > 80:
        return _step(NAME_ASK_TEXT, expect="name")
    update_session(session["id"], name=clean, state="need_phone")
    return _step(PHONE_ASK_TEXT, expect="phone")


async def save_phone(session: dict[str, Any], phone: str) -> dict[str, Any]:
    from bot.utils.phone import normalize_phone

    clean = normalize_phone(phone)
    if not clean:
        return _step(PHONE_ASK_TEXT, expect="phone")
    update_session(session["id"], phone=clean, state="need_format")
    return await ask_format()


async def ask_format() -> dict[str, Any]:
    buttons = [(FORMAT_LABELS[fmt], f"cert_fmt:{fmt}") for fmt in SHOW_FORMATS]
    return _step(FORMAT_ASK_TEXT, expect="buttons", buttons=buttons)


async def ask_dates(session: dict[str, Any], event_format: str) -> dict[str, Any]:
    if event_format not in SHOW_FORMATS:
        return await ask_format()
    events = await load_events(event_format)
    dates: list[str] = []
    for event in events:
        day = event.get("date") or ""
        if day and day not in dates:
            dates.append(day)
    update_session(session["id"], event_format=event_format, state="need_date")
    if not dates:
        buttons = [(FORMAT_LABELS[fmt], f"cert_fmt:{fmt}") for fmt in SHOW_FORMATS]
        return _step(EMPTY_DATES_TEXT, expect="buttons", buttons=buttons)
    buttons = [(day, f"cert_day:{event_format}:{day}") for day in dates[:12]]
    buttons.append(("Другой формат", "cert_fmt:back"))
    return _step(DATE_ASK_TEXT, expect="buttons", buttons=buttons)


async def ask_events(session: dict[str, Any], event_format: str, day: str) -> dict[str, Any]:
    events = await load_events(event_format)
    matched = [event for event in events if event.get("date") == day]
    if not matched:
        return await ask_dates(session, event_format)
    update_session(session["id"], state="need_event")
    buttons = []
    for event in matched[:8]:
        label = f"{event.get('time') or ''} {event.get('location') or ''}".strip()
        buttons.append((label[:40] or "Шоу", f"cert_ev:{int(event['id'])}"))
    buttons.append(("Другая дата", f"cert_fmt:{event_format}"))
    return _step(EVENT_ASK_TEXT, expect="buttons", buttons=buttons)


def _event_iso(date_str: str, time_str: str) -> tuple[str, str]:
    event_dt = parse_event_datetime(date_str, time_str)
    if not event_dt:
        raise RuntimeError("bad event datetime")
    start = event_dt.strftime("%Y-%m-%dT%H:%M:%S+03:00")
    plan = plan_booking_reminders(datetime.now(), event_dt)
    confirm_by = plan["annul_at"].strftime("%Y-%m-%dT%H:%M:%S+03:00")
    return start, confirm_by


async def reserve_event(session: dict[str, Any], event_id: int) -> dict[str, Any]:
    event = await get_event_by_id(int(event_id))
    if not event or event.get("format") not in SHOW_FORMATS:
        return await ask_format()
    if session.get("state") not in {"need_event", "need_date", "need_format", "booked"}:
        if not session.get("replace_booking_id"):
            return _step("Сначала выбери формат и дату.")
    existing = active_on_event(
        event_id=int(event_id),
        telegram_id=session.get("telegram_id"),
        vk_id=session.get("vk_id") if session.get("channel") == "vk" else None,
    )
    if existing and int(existing["id"]) != int(session.get("booking_id") or 0):
        return _step(ALREADY_BOOKED_TEXT)
    reuse_id = None
    if (
        session.get("booking_id")
        and session.get("state") == "booked"
        and int(session.get("event_id") or 0) == int(event_id)
        and not session.get("registry_confirmed")
    ):
        reuse_id = int(session["booking_id"])

    if reuse_id:
        booking_id = reuse_id
    else:
        booking_id = create_booking(
            session.get("telegram_id"),
            None,
            session.get("name") or "",
            session.get("phone") or "",
            event["date"],
            event["time"],
            event.get("address") or "",
            event.get("location") or "",
            2,
            booking_format=CERTIFICATE,
            event_format=event["format"],
            event_id=int(event["id"]),
            vk_id=session.get("vk_id") if session.get("channel") == "vk" else None,
            source="vkontakte" if session.get("channel") == "vk" else "telegram",
        )
    ids = certificate_booking_ids(int(event["id"]))
    if int(booking_id) not in ids[:CERT_CAP]:
        update_booking_status(booking_id, "cancelled")
        return _step(CAP_TEXT)

    booking_ref = safe_ref(f"booking:{int(booking_id)}")
    seats_ref = safe_ref(f"seat_hold:{int(booking_id)}")
    event_ref = safe_ref(f"event:{int(event['id'])}")
    try:
        event_start, confirm_by = _event_iso(event["date"], event["time"])
    except RuntimeError:
        update_booking_status(booking_id, "cancelled")
        return _step("Не разобрал дату шоу. Выбери другое.")
    title = (event.get("description") or "").strip() or FORMAT_LABELS.get(event["format"], "Шоу")
    channel = "vk" if session.get("channel") == "vk" else "telegram"
    user_id = session["vk_id"] if channel == "vk" else session["telegram_id"]
    payload = {
        "code": session["code"],
        "channel": channel,
        "user_id": str(int(user_id)),
        "booking_ref": booking_ref,
        "event_ref": event_ref,
        "event_title": title[:300],
        "event_start": event_start,
        "confirm_by": confirm_by,
        "seats_ref": seats_ref,
        "name": session.get("name") or "",
        "phone": session.get("phone") or "",
    }
    replace_id = session.get("replace_booking_id")
    if replace_id:
        payload["replace_booking_ref"] = safe_ref(f"booking:{int(replace_id)}")
    try:
        await registry.reserve(payload)
    except RegistryTemporary:
        try:
            await registry.booking_status(
                {
                    "code": session["code"],
                    "channel": channel,
                    "user_id": str(int(user_id)),
                    "booking_ref": booking_ref,
                }
            )
        except RegistryTemporary:
            update_session(
                session["id"],
                booking_id=int(booking_id),
                booking_ref=booking_ref,
                event_id=int(event["id"]),
                event_format=event["format"],
                confirm_by=confirm_by,
                state="booked",
            )
            return _step(RESERVE_PENDING_TEXT)
        except (RegistryRefusal, RegistryNotConfigured):
            update_booking_status(booking_id, "cancelled")
            return _step("Не удалось записать бронь на сайте. Выбери дату ещё раз.")
    except (RegistryRefusal, RegistryNotConfigured) as exc:
        update_booking_status(booking_id, "cancelled")
        return _step(_guest_error(exc) if not isinstance(exc, RegistryNotConfigured) else UNAVAILABLE_TEXT)

    if replace_id:
        old = booking_context(int(replace_id))
        if old and old.get("status") in {"booked", "confirmed"} and old.get("status") != "confirmed":
            update_booking_status(int(replace_id), "cancelled")
    update_session(
        session["id"],
        booking_id=int(booking_id),
        booking_ref=booking_ref,
        event_id=int(event["id"]),
        event_format=event["format"],
        confirm_by=confirm_by,
        replace_booking_id=None,
        state="booked",
        registry_confirmed=False,
    )
    when = f"{event['date']} в {event['time']}"
    where = ", ".join(part for part in (event.get("location"), event.get("address")) if part)
    text = (
        f"Бронь на двоих записана: {when}.\n{where}\n\n"
        f"{WARN_TEXT}\n\n{BRING_TEXT}"
    )
    return _step(
        text,
        expect="buttons",
        buttons=_booking_buttons(int(booking_id)),
    )


def _booking_buttons(booking_id: int) -> list[tuple[str, str]]:
    from bot.services.cert_texts import CANCEL_BUTTON, CONFIRM_BUTTON, MOVE_BUTTON

    return [
        (CONFIRM_BUTTON, f"cert_cf:{int(booking_id)}"),
        (CANCEL_BUTTON, f"cert_cx:{int(booking_id)}"),
        (MOVE_BUTTON, f"cert_mv:{int(booking_id)}"),
    ]


def _owns(session: dict[str, Any] | None, booking: dict[str, Any], *, telegram_id: int | None, vk_id: int | None) -> bool:
    if booking.get("format") != CERTIFICATE:
        return False
    if vk_id is not None:
        return int(booking.get("vk_id") or 0) == int(vk_id)
    return int(booking.get("telegram_id") or 0) == int(telegram_id or 0)


def _booking_payload(session: dict[str, Any], booking_id: int) -> dict[str, Any]:
    channel = "vk" if session.get("channel") == "vk" else "telegram"
    user_id = session["vk_id"] if channel == "vk" else session["telegram_id"]
    return {
        "code": session["code"],
        "channel": channel,
        "user_id": str(int(user_id)),
        "booking_ref": session.get("booking_ref") or safe_ref(f"booking:{int(booking_id)}"),
    }


async def confirm_booking(booking_id: int, *, telegram_id: int | None = None, vk_id: int | None = None) -> dict[str, Any]:
    booking = booking_context(booking_id)
    session = get_session_by_booking(booking_id)
    if not booking or not session or not _owns(session, booking, telegram_id=telegram_id, vk_id=vk_id):
        return _step("Бронь не найдена.")
    if booking["status"] == "confirmed" and session.get("registry_confirmed"):
        return _step(CONFIRMED_TEXT)
    if booking["status"] not in {"booked", "confirmed"}:
        return _step("Эта бронь уже неактивна.")
    payload = _booking_payload(session, booking_id)
    try:
        await registry.confirm(payload)
    except RegistryTemporary:
        return _step("Сайт не ответил. Нажми «Подтвердить бронь» ещё раз. Вторую бронь создавать не нужно.")
    except (RegistryRefusal, RegistryNotConfigured) as exc:
        if booking["status"] == "confirmed":
            update_booking_status(booking_id, "booked")
        return _step(_guest_error(exc) if not isinstance(exc, RegistryNotConfigured) else UNAVAILABLE_TEXT)
    if booking["status"] != "confirmed":
        update_booking_status(booking_id, "confirmed")
    update_session(session["id"], registry_confirmed=True, state="booked")
    return _step(CONFIRMED_TEXT)


async def cancel_booking(booking_id: int, *, telegram_id: int | None = None, vk_id: int | None = None) -> dict[str, Any]:
    booking = booking_context(booking_id)
    session = get_session_by_booking(booking_id)
    if not booking or not session or not _owns(session, booking, telegram_id=telegram_id, vk_id=vk_id):
        return _step("Бронь не найдена.")
    if booking["status"] == "confirmed" or session.get("registry_confirmed"):
        return _step("После подтверждения эту бронь уже не отменить. Сертификат списан.")
    if booking["status"] != "booked":
        return _step("Эта бронь уже неактивна.")
    payload = _booking_payload(session, booking_id)
    try:
        await registry.cancel(payload)
    except RegistryTemporary:
        return _step("Сайт не ответил. Нажми отмену ещё раз. Номер пока не освобождён.")
    except (RegistryRefusal, RegistryNotConfigured) as exc:
        return _step(_guest_error(exc) if not isinstance(exc, RegistryNotConfigured) else UNAVAILABLE_TEXT)
    update_booking_status(booking_id, "cancelled")
    update_session(session["id"], state="need_format", booking_id=None, booking_ref=None, registry_confirmed=False)
    return _step(CANCELLED_TEXT + "\n\n" + FORMAT_ASK_TEXT, expect="buttons", buttons=(await ask_format())["buttons"])


async def begin_transfer(booking_id: int, *, telegram_id: int | None = None, vk_id: int | None = None) -> dict[str, Any]:
    booking = booking_context(booking_id)
    session = get_session_by_booking(booking_id)
    if not booking or not session or not _owns(session, booking, telegram_id=telegram_id, vk_id=vk_id):
        return _step("Бронь не найдена.")
    if booking["status"] != "booked" or session.get("registry_confirmed"):
        return _step("После подтверждения дату уже не перенести.")
    update_session(session["id"], replace_booking_id=int(booking_id), state="need_format")
    step = await ask_format()
    step["text"] = TRANSFER_TEXT + "\n\n" + step["text"]
    return step


async def expire_booking(booking_id: int) -> str:
    """ok — можно аннулировать локально; retry — сайт не ответил."""
    session = get_session_by_booking(booking_id)
    booking = booking_context(booking_id)
    if not booking or booking.get("format") != CERTIFICATE:
        return "skip"
    if booking.get("status") != "booked":
        return "skip"
    if not session:
        logger.error("certificate expire without session booking_id=%s", booking_id)
        return "retry"
    payload = _booking_payload(session, booking_id)
    try:
        await registry.expire(payload)
    except RegistryRefusal as exc:
        if exc.code in _EXPIRE_OK:
            return "ok"
        logger.error("certificate expire refused booking_id=%s code=%s", booking_id, exc.code)
        return "retry"
    except (RegistryTemporary, RegistryNotConfigured):
        logger.warning("certificate expire temporary booking_id=%s", booking_id)
        return "retry"
    update_session(session["id"], state="need_format", booking_id=None, booking_ref=None)
    return "ok"


async def release_unconfirmed(booking_id: int) -> str:
    """When admin hides a show: free the number if the guest has not confirmed.

    ok — registry released, local cancel is safe
    spent — already confirmed, number stays used, local cancel is still allowed
    skip — not a certificate booking
    retry — do not cancel locally, registry did not accept
    """
    booking = booking_context(booking_id)
    session = get_session_by_booking(booking_id)
    if not booking or booking.get("format") != CERTIFICATE:
        return "skip"
    if booking.get("status") == "confirmed" or (session and session.get("registry_confirmed")):
        return "spent"
    if booking.get("status") != "booked":
        return "skip"
    if not session:
        logger.error("certificate staff cancel without session booking_id=%s", booking_id)
        return "retry"
    try:
        await registry.cancel(_booking_payload(session, booking_id))
    except RegistryRefusal as exc:
        if exc.code in _EXPIRE_OK:
            update_session(
                session["id"],
                state="need_format",
                booking_id=None,
                booking_ref=None,
                registry_confirmed=False,
            )
            return "ok"
        logger.error("certificate staff cancel refused booking_id=%s code=%s", booking_id, exc.code)
        return "retry"
    except (RegistryTemporary, RegistryNotConfigured):
        logger.warning("certificate staff cancel temporary booking_id=%s", booking_id)
        return "retry"
    update_session(
        session["id"],
        state="need_format",
        booking_id=None,
        booking_ref=None,
        registry_confirmed=False,
    )
    return "ok"


def draft_for(*, telegram_id: int | None = None, vk_id: int | None = None) -> dict[str, Any] | None:
    return get_draft(telegram_id=telegram_id, vk_id=vk_id)
