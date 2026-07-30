from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.db.crud import (
    draw_offline_gift_winner,
    get_offline_gift_dates,
    get_offline_gift_entries,
    get_offline_gift_event,
    get_offline_gift_events_for_date,
)

router = Router()

FORMAT_LABELS = {
    "proverka": "Проверка",
    "best": "BEST",
    "hitloto": "Хитлото",
}


def _format_label(value: str) -> str:
    return FORMAT_LABELS.get(value or "", value or "Шоу")


def _event_label(event: dict) -> str:
    return " · ".join(
        part
        for part in [
            event.get("time") or "",
            event.get("location") or "",
            _format_label(event.get("format") or ""),
        ]
        if part
    )


def _dates_keyboard(dates: list[dict]):
    kb = InlineKeyboardBuilder()
    for item in dates[:14]:
        label = f"{item['date']}"
        if int(item.get("events_count") or 0) > 1:
            label += f" · {item['events_count']} шоу"
        kb.button(text=label, callback_data=f"og_date:{item['date']}")
    kb.adjust(2)
    return kb.as_markup()


def _events_keyboard(events: list[dict]):
    kb = InlineKeyboardBuilder()
    for event in events[:20]:
        count = int(event.get("entries_count") or 0)
        kb.button(
            text=f"{_event_label(event)} · {count} чел.",
            callback_data=f"og_event:{event['id']}",
        )
    kb.button(text="⬅️ К датам", callback_data="og_dates")
    kb.adjust(1)
    return kb.as_markup()


def _event_keyboard(
    event_id: int,
    *,
    has_winner: bool = False,
    show_back_to_shows: bool = True,
):
    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 Выбрать победителя", callback_data=f"og_draw:{event_id}")
    if has_winner:
        kb.button(text="🔁 Перевыбрать", callback_data=f"og_redraw:{event_id}")
    if show_back_to_shows:
        kb.button(text="⬅️ К шоу", callback_data=f"og_back_event:{event_id}")
    kb.button(text="⬅️ К датам", callback_data="og_dates")
    kb.adjust(1)
    return kb.as_markup()


def _date_has_multiple_shows(event_date: str) -> bool:
    return len(get_offline_gift_events_for_date(event_date)) > 1


def _entries_text(event: dict, entries: list[dict]) -> str:
    lines = [
        "🎁 <b>VK чек-лист участников</b>",
        "",
        f"<b>Дата:</b> {escape(event.get('date') or '')}",
        f"<b>Шоу:</b> {escape(_event_label(event))}",
        "",
        f"<b>Участников:</b> {len(entries)}",
    ]
    winner = next((entry for entry in entries if entry.get("is_winner")), None)
    if winner:
        lines.append(f"<b>Победитель:</b> {escape(winner['full_name'])}")
    lines.append("")
    if not entries:
        lines.append("Пока никого нет в списке.")
    else:
        for idx, entry in enumerate(entries, start=1):
            name = escape(entry["full_name"])
            vk_id = int(entry["vk_id"])
            suffix = " 🏆" if entry.get("is_winner") else ""
            lines.append(f"{idx}. <a href=\"https://vk.com/id{vk_id}\">{name}</a>{suffix}")
    return "\n".join(lines)


async def send_check_list_start(message: Message):
    dates = get_offline_gift_dates()
    if not dates:
        await message.answer("Пока нет активных шоу в афише.")
        return
    await message.answer(
        "🎁 <b>Чек-лист VK участников</b>\n\nВыберите дату:",
        reply_markup=_dates_keyboard(dates),
        parse_mode="HTML",
    )


async def _send_events_for_date(message_or_call, event_date: str):
    events = get_offline_gift_events_for_date(event_date)
    target = message_or_call.message if isinstance(message_or_call, CallbackQuery) else message_or_call
    if not events:
        text = "На эту дату нет активных шоу."
        if isinstance(message_or_call, CallbackQuery):
            await target.edit_text(text)
        else:
            await target.answer(text)
        return

    if len(events) == 1 and isinstance(message_or_call, CallbackQuery):
        await _send_event_entries(message_or_call, int(events[0]["id"]))
        return

    text = f"Выберите шоу на <b>{escape(event_date)}</b>:"
    if isinstance(message_or_call, CallbackQuery):
        await target.edit_text(text, reply_markup=_events_keyboard(events), parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=_events_keyboard(events), parse_mode="HTML")


async def _send_event_entries(call: CallbackQuery, event_id: int):
    event = get_offline_gift_event(event_id)
    if not event:
        await call.answer("Шоу не найдено", show_alert=True)
        return
    entries = get_offline_gift_entries(event_id)
    show_back_to_shows = _date_has_multiple_shows(event.get("date") or "")
    text = _entries_text(event, entries)
    markup = _event_keyboard(
        event_id,
        has_winner=bool(event.get("winner_name")),
        show_back_to_shows=show_back_to_shows,
    )
    try:
        await call.message.edit_text(
            text,
            reply_markup=markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        # Тот же текст (один участник / тот же победитель) — Telegram ругается, кнопка «молчит».
        if "message is not modified" not in str(exc).lower():
            raise


@router.callback_query(F.data == "og_dates")
async def og_dates(call: CallbackQuery):
    dates = get_offline_gift_dates()
    await call.message.edit_text(
        "🎁 <b>Чек-лист VK участников</b>\n\nВыберите дату:",
        reply_markup=_dates_keyboard(dates),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("og_date:"))
async def og_date(call: CallbackQuery):
    event_date = call.data.split(":", 1)[1]
    await _send_events_for_date(call, event_date)
    await call.answer()


@router.callback_query(F.data.startswith("og_back_event:"))
async def og_back_event(call: CallbackQuery):
    event = get_offline_gift_event(int(call.data.split(":", 1)[1]))
    if not event:
        await call.answer("Шоу не найдено", show_alert=True)
        return
    await _send_events_for_date(call, event["date"])
    await call.answer()


@router.callback_query(F.data.startswith("og_event:"))
async def og_event(call: CallbackQuery):
    await _send_event_entries(call, int(call.data.split(":", 1)[1]))
    await call.answer()


@router.callback_query(F.data.startswith("og_draw:") & ~F.data.startswith("og_redraw:"))
async def og_draw(call: CallbackQuery):
    event_id = int(call.data.split(":", 1)[1])
    winner = draw_offline_gift_winner(event_id, redraw=False)
    if not winner:
        await call.answer("Пока нет участников", show_alert=True)
        return
    await _send_event_entries(call, event_id)
    await call.answer(f"Победитель: {winner['full_name']}", show_alert=True)


@router.callback_query(F.data.startswith("og_redraw:"))
async def og_redraw(call: CallbackQuery):
    event_id = int(call.data.split(":", 1)[1])
    entries = get_offline_gift_entries(event_id)
    if not entries:
        await call.answer("Пока нет участников", show_alert=True)
        return
    winner = draw_offline_gift_winner(event_id, redraw=True)
    if not winner:
        await call.answer("Пока нет участников", show_alert=True)
        return
    await _send_event_entries(call, event_id)
    if len(entries) == 1:
        await call.answer(
            f"Участник один — победитель тот же: {winner['full_name']}",
            show_alert=True,
        )
    else:
        await call.answer(f"Новый победитель: {winner['full_name']}", show_alert=True)
