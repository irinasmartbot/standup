"""Free booking branch: Резиденты стендап (date-only, same flow as проверка)."""

from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import MANAGER_LINK
from bot.db.analytics import EVENT_BRANCH_RESIDENTS, EVENT_SHOW_CARD, track_event
from bot.db.crud import get_booking, has_pdn_consent
from bot.handlers.booking import (
    CB_CONSENT_BOOKING,
    _ask_booking_name,
    _delete_previous_menu_message,
    _send_pdn_consent_tg,
    reply_target,
)
from bot.services.sheets import load_events
from bot.utils.booking_texts import same_day_booking_warning
from bot.utils.nav_messages import remember_booking_nav
from bot.utils.show_formats import RESIDENTS, RESIDENTS_LABEL_SHORT
from bot.utils.ticket import MONTHS, format_date, now_msk

router = Router()

EMPTY_TEXT = "Скоро даты появятся 😊"
ENTRY_TEXT = (
    "Привет 😊 Я помогу тебе забронировать места на "
    f"<b>{RESIDENTS_LABEL_SHORT}</b> Moscow StandUp Show 🎤\n\nВыбирай дату 👇"
)


async def load_resident_events() -> list[dict]:
    return await load_events(RESIDENTS)


async def residents_dates_kb(
    *,
    back_callback: str | None = "book",
    back_label: str | None = None,
):
    events = await load_resident_events()
    dates = sorted({e["date"] for e in events}, key=lambda d: datetime.strptime(d, "%d.%m.%Y"))
    kb = InlineKeyboardBuilder()
    for date in dates:
        try:
            d = datetime.strptime(date, "%d.%m.%Y")
            label = d.strftime("%d ") + MONTHS[d.strftime("%B")]
        except Exception:
            label = date
        kb.button(text=label, callback_data=f"res_date_{date}")
    n = len(dates)
    widths = [2] * (n // 2)
    if n % 2:
        widths.append(1)
    if back_callback:
        label = back_label or (
            "В главное меню" if back_callback == "main_menu" else "◀️ Назад"
        )
        kb.button(text=label, callback_data=back_callback)
        widths.append(1)
    if widths:
        kb.adjust(*widths)
    return kb.as_markup(), dates


def _empty_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
    kb.button(text="◀️ Назад в меню", callback_data="main_menu")
    kb.adjust(1)
    return kb.as_markup()


async def residents_format_entry(message, *, telegram_id: int | None = None):
    tid = telegram_id or getattr(getattr(message, "from_user", None), "id", None)
    if tid:
        track_event(EVENT_BRANCH_RESIDENTS, telegram_id=tid)
    events = await load_resident_events()
    if not events:
        sent = await message.answer(
            EMPTY_TEXT,
            reply_markup=_empty_kb(),
        )
        remember_booking_nav(message.chat.id, sent.message_id)
        return
    kb, _ = await residents_dates_kb()
    from bot.handlers.formats import _answer_with_residents_photo

    await _answer_with_residents_photo(
        message,
        ENTRY_TEXT,
        reply_markup=kb,
        parse_mode="HTML",
        track_nav=True,
    )


async def send_residents_dates_from_mailing(message, *, telegram_id: int | None = None) -> None:
    """Список дат сольника без картинки. Письмо рассылки не трогаем."""
    tid = telegram_id or getattr(getattr(message, "from_user", None), "id", None)
    if tid:
        track_event(EVENT_BRANCH_RESIDENTS, telegram_id=tid, props={"via": "mailing"})
    events = await load_resident_events()
    if not events:
        sent = await message.answer(EMPTY_TEXT, reply_markup=_empty_kb())
        remember_booking_nav(message.chat.id, sent.message_id)
        return
    kb, _ = await residents_dates_kb(back_callback="main_menu")
    sent = await message.answer(ENTRY_TEXT, reply_markup=kb, parse_mode="HTML")
    remember_booking_nav(message.chat.id, sent.message_id)


async def send_residents_event_card(message, event, back_callback="residents", *, telegram_id=None):
    if telegram_id:
        track_event(
            EVENT_SHOW_CARD,
            telegram_id=telegram_id,
            event_id=event.get("id"),
            props={
                "format": RESIDENTS,
                "browse": "date",
                "date": event.get("date"),
                "time": event.get("time"),
                "location": event.get("location"),
            },
        )
    date_str = format_date(event["date"])
    text = (
        f"{date_str}\n{event.get('weekday') or ''}\n\n"
        f"{event.get('time') or ''}\n{event.get('address') or ''}\n"
        f"{event.get('description') or ''}"
    ).strip()
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🎟 Забронировать билеты",
        callback_data=f"res_book_{event['id']}",
    )
    kb.button(text="📋 Правила бронирования", callback_data="booking_rules")
    kb.button(text="◀️ Назад", callback_data=back_callback)
    kb.adjust(1)
    from bot.utils.event_poster import tg_send_event_card

    await tg_send_event_card(
        message,
        event.get("image"),
        caption=text,
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data == "residents")
async def residents_format(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    await residents_format_entry(reply_target(call), telegram_id=call.from_user.id)
    await call.answer()


@router.callback_query(F.data.startswith("res_date_"))
async def residents_date(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    date = call.data.replace("res_date_", "", 1)
    events = [e for e in await load_resident_events() if e["date"] == date]
    message = reply_target(call)
    if not events:
        kb, _ = await residents_dates_kb()
        await message.answer("Это мероприятие уже прошло 😊 Выбери новую дату!", reply_markup=kb)
        await call.answer()
        return
    if len(events) == 1:
        await send_residents_event_card(
            message, events[0], telegram_id=call.from_user.id
        )
        await call.answer()
        return
    kb = InlineKeyboardBuilder()
    for event in events:
        kb.button(
            text=f"🕐 {event['time']} — {event['location']}",
            callback_data=f"res_event_{event['id']}",
        )
    kb.button(text="◀️ Назад к датам", callback_data="residents")
    kb.adjust(1)
    await message.answer("На эту дату несколько мероприятий, выбери нужное 👇", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("res_event_"))
async def residents_event(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    event_id = call.data.replace("res_event_", "", 1)
    event = next(
        (e for e in await load_resident_events() if str(e.get("id")) == str(event_id)),
        None,
    )
    message = reply_target(call)
    if event:
        await send_residents_event_card(message, event, telegram_id=call.from_user.id)
    else:
        kb, _ = await residents_dates_kb()
        await message.answer("Мероприятие уже прошло 😊 Выбери новую дату!", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("res_book_"))
async def residents_start_booking(call: CallbackQuery, state: FSMContext):
    event_id = call.data.replace("res_book_", "", 1)
    await start_residents_booking_from_call(call, state, event_id)


async def start_residents_booking_from_call(
    call: CallbackQuery, state: FSMContext, event_id
) -> None:
    event = next(
        (e for e in await load_resident_events() if str(e.get("id")) == str(event_id)),
        None,
    )
    if not event:
        kb, _ = await residents_dates_kb()
        await reply_target(call).answer(
            "Это мероприятие уже прошло 😊 Выбери новую дату!",
            reply_markup=kb,
        )
        await call.answer()
        return
    event_date = event["date"]
    event_time = event["time"]
    try:
        if datetime.strptime(event_date, "%d.%m.%Y").date() < now_msk().date():
            kb, _ = await residents_dates_kb()
            await reply_target(call).answer(
                "Это мероприятие уже прошло 😊 Выбери новую дату!",
                reply_markup=kb,
            )
            await call.answer()
            return
    except Exception:
        pass

    existing = get_booking(call.from_user.id, event_date, event_time)
    if existing:
        date_str = format_date(event_date)
        kb = InlineKeyboardBuilder()
        kb.button(text="Отменить бронь", callback_data=f"cancel_confirm_{existing[0]}")
        kb.button(text="Выбрать другую дату", callback_data="residents", style="success")
        kb.adjust(1)
        await reply_target(call).answer(
            f"⚠️ ВНИМАНИЕ, мы уже внесли Вас в списки гостей:\n\n"
            f"Дата: {date_str}\n"
            f"Время: {existing[6]}\n"
            f"Локация: {existing[8]}\n"
            f"Количество гостей: {existing[9]} чел.\n\n"
            f"Вы не можете забронировать повторный билет на данное мероприятие",
            reply_markup=kb.as_markup(),
        )
        await call.answer()
        return

    name = " ".join(
        p for p in (call.from_user.first_name or "", call.from_user.last_name or "") if p
    ).strip()
    await state.update_data(
        event_date=event_date,
        event_time=event_time,
        event_id=event.get("id"),
        event_format=RESIDENTS,
        booking_format=RESIDENTS,
        name=name,
    )

    same_day_alert = same_day_booking_warning(
        call.from_user.id, event_date, exclude_time=event_time, for_alert=True
    )
    if not has_pdn_consent(telegram_id=call.from_user.id):
        await _send_pdn_consent_tg(call.message, CB_CONSENT_BOOKING)
        if same_day_alert:
            await call.answer(same_day_alert, show_alert=True)
        else:
            await call.answer()
        return

    await _ask_booking_name(call, state)
    if same_day_alert:
        await call.answer(same_day_alert, show_alert=True)
    else:
        await call.answer()
