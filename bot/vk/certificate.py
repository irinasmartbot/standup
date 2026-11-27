"""VK: тот же сертификат, без импорта Telegram-роутера."""

from __future__ import annotations

import logging
from typing import Any

from bot.db.cert_store import get_draft
from bot.services.cert_codes import looks_like_certificate_start, normalize_certificate_code
from bot.services.cert_flow import (
    ask_dates,
    ask_events,
    ask_format,
    ask_phone_manual,
    begin,
    begin_transfer,
    cancel_booking,
    confirm_booking,
    number_hint_photo_path,
    reserve_event,
    save_name,
    save_phone,
    show_event_card,
    submit_number,
    submit_photo,
    use_saved_phone,
)
from bot.services.cert_texts import NUMBER_HINT_SCREEN, PHOTO_ASK_TEMPLATE
from bot.vk.formatting import format_vk_text
from bot.vk.keyboards import VKKeyboardBuilder
from bot.vk.raffle import download_screenshot_bytes, extract_photo_from_message

logger = logging.getLogger(__name__)


def _keyboard(step: dict) -> str | None:
    buttons = list(step.get("buttons") or [])
    if not buttons:
        return None
    layout = step.get("layout")
    if layout == "dates" and len(buttons) > 9:
        buttons = buttons[:8] + buttons[-1:]
    kb = VKKeyboardBuilder(inline=True)
    for label, data in buttons:
        kind, _, rest = data.partition(":")
        payload: dict[str, Any] = {"cmd": kind}
        if kind == "cert_fmt":
            payload["fmt"] = rest
        elif kind == "cert_day":
            fmt, _, day = rest.partition(":")
            payload["fmt"] = fmt
            payload["date"] = day
        elif kind == "cert_ph":
            payload["choice"] = rest
        elif kind in {"cert_ev", "cert_card", "cert_cf", "cert_cx", "cert_mv"}:
            payload["id"] = int(rest)
        kb.button(label[:40], payload)
    if layout == "dates" and len(buttons) > 1:
        n = len(buttons) - 1
        widths = [2] * (n // 2)
        if n % 2:
            widths.append(1)
        widths.append(1)
        kb.adjust(*widths)
    else:
        kb.adjust(1)
    return kb.as_json()


async def _send(client, peer_id: int, step: dict) -> None:
    text = format_vk_text(step.get("text") or "")
    keyboard = _keyboard(step)
    if step.get("screen") == NUMBER_HINT_SCREEN:
        path = number_hint_photo_path()
        if path:
            try:
                attachment = await client.upload_message_photo(
                    peer_id,
                    path.read_bytes(),
                    filename=path.name,
                )
                await client.send_message(
                    peer_id,
                    text,
                    keyboard=keyboard,
                    attachment=attachment,
                )
                return
            except Exception:
                logger.exception("certificate number hint photo failed")
    await client.send_message(peer_id, text, keyboard=keyboard)


def is_certificate_ref(raw_ref: str, ref_source: str = "") -> bool:
    if looks_like_certificate_start(raw_ref):
        return True
    return (ref_source or "").strip().casefold() == "mss_certificate" and bool((raw_ref or "").strip())


async def begin_from_ref(client, peer_id: int, vk_id: int, raw_ref: str, ref_source: str = "") -> None:
    step = await begin(
        channel="vk",
        raw=raw_ref,
        vk_id=vk_id,
        peer_id=peer_id,
        ref_source=ref_source,
    )
    await _send(client, peer_id, step)


async def handle_incoming(
    client,
    peer_id: int,
    vk_id: int,
    *,
    message: dict,
    text: str,
    cmd: str | None,
    payload: dict | None = None,
) -> bool:
    """True — сообщение сертификата обработано и дальше по меню идти не нужно."""
    if cmd in {
        "cert_fmt",
        "cert_day",
        "cert_card",
        "cert_ev",
        "cert_cf",
        "cert_cx",
        "cert_mv",
        "cert_ph",
    }:
        await _on_command(client, peer_id, vk_id, cmd, payload or {})
        return True
    session = get_draft(vk_id=vk_id)
    if not session and (text or "").strip().casefold() in {"сертификат", "сертификаты"}:
        step = await begin(
            channel="vk",
            raw="certificate",
            vk_id=vk_id,
            peer_id=peer_id,
        )
        await _send(client, peer_id, step)
        return True
    if not session or cmd:
        return False
    state = session.get("state")
    photo_url, photo_ref = extract_photo_from_message(message)
    if state == "await_photo" and (photo_url or photo_ref == "album"):
        if photo_ref == "album" or not photo_url:
            await client.send_message(peer_id, "Пришли одно фото, где номер читается.")
            return True
        try:
            blob = await download_screenshot_bytes(photo_url)
        except Exception:
            logger.exception("certificate vk photo download failed")
            await client.send_message(peer_id, "Не скачал фото. Пришли его ещё раз.")
            return True
        step = await submit_photo(
            session,
            photo=blob,
            photo_ref=photo_ref or "vk_photo",
            full_name="",
        )
        await _send(client, peer_id, step)
        return True
    if not text:
        return False
    if state in {"await_number", "await_photo"} and normalize_certificate_code(text):
        step = await submit_number(session, text)
        await _send(client, peer_id, step)
        return True
    if state == "await_photo":
        code = session.get("code") or ""
        await client.send_message(
            peer_id,
            format_vk_text(
                PHOTO_ASK_TEMPLATE.format(code=code) if code else "Пришли фото сертификата 📸"
            ),
        )
        return True
    if state == "need_name":
        step = await save_name(session, text)
        await _send(client, peer_id, step)
        return True
    if state == "need_phone":
        step = await save_phone(session, text)
        await _send(client, peer_id, step)
        return True
    if state == "pending_review":
        await client.send_message(peer_id, "Фото ещё на проверке.")
        return True
    return False


async def _on_command(client, peer_id: int, vk_id: int, cmd: str, payload: dict) -> None:
    session = get_draft(vk_id=vk_id)
    if cmd == "cert_ph":
        if not session:
            await client.send_message(peer_id, "Начни сертификат по ссылке со страницы ещё раз.")
            return
        if str(payload.get("choice") or "") == "ok":
            step = await use_saved_phone(session)
        else:
            step = ask_phone_manual()
        await _send(client, peer_id, step)
        return
    if cmd == "cert_fmt":
        if not session:
            await client.send_message(peer_id, "Начни сертификат по ссылке со страницы ещё раз.")
            return
        fmt = str(payload.get("fmt") or "")
        step = await ask_format() if fmt == "back" else await ask_dates(session, fmt)
        await _send(client, peer_id, step)
        return
    if cmd == "cert_day":
        if not session:
            await client.send_message(peer_id, "Начни сертификат по ссылке со страницы ещё раз.")
            return
        step = await ask_events(session, str(payload.get("fmt") or ""), str(payload.get("date") or ""))
        await _send(client, peer_id, step)
        return
    if cmd == "cert_card":
        if not session:
            await client.send_message(peer_id, "Начни сертификат по ссылке со страницы ещё раз.")
            return
        step = await show_event_card(session, int(payload.get("id") or 0))
        await _send(client, peer_id, step)
        return
    if cmd == "cert_ev":
        if not session:
            await client.send_message(peer_id, "Начни сертификат по ссылке со страницы ещё раз.")
            return
        step = await reserve_event(session, int(payload.get("id") or 0))
        await _send(client, peer_id, step)
        return
    if cmd == "cert_cf":
        step = await confirm_booking(int(payload.get("id") or 0), vk_id=vk_id)
        await _send(client, peer_id, step)
        return
    if cmd == "cert_cx":
        step = await cancel_booking(int(payload.get("id") or 0), vk_id=vk_id)
        await _send(client, peer_id, step)
        return
    if cmd == "cert_mv":
        step = await begin_transfer(int(payload.get("id") or 0), vk_id=vk_id)
        await _send(client, peer_id, step)


async def cancel_booking_message(client, peer_id: int, vk_id: int, booking_id: int) -> None:
    step = await cancel_booking(booking_id, vk_id=vk_id)
    await _send(client, peer_id, step)
