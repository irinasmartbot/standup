#!/usr/bin/env python3
"""Разовое восстановление VK-билета: подтвердить бронь, снять напоминания, отправить билет.

Запуск на сервере из /home/standup/app или /home/standup/vk-app:

    venv/bin/python scripts/fix_vk_ticket_booking.py 201
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass


async def _send_ticket(booking_id: int) -> None:
    from bot.admin.ticket_resend import get_booking_for_ticket_resend, _resend_ticket_vk, _ticket_bytes
    from bot.db.crud import update_booking_status, update_reminder_flag, save_ticket_message_id
    from bot.vk.client import VKAPIError, VKClient
    from bot.vk.config import load_vk_settings
    from bot.vk.formatting import format_vk_text
    from bot.utils.ticket import guests_word
    from bot.vk.booking import manage_ticket_keyboard

    row = get_booking_for_ticket_resend(booking_id)
    if not row:
        raise SystemExit(f"booking {booking_id} not found")

    print(
        f"booking={booking_id} status={row.get('status')} "
        f"vk_id={row.get('vk_id')} name={row.get('name')!r} "
        f"{row.get('event_date')} {row.get('event_time')} {row.get('location')}"
    )

    # 2) подтверждение — сразу, чтобы не сработало аннулирование/напоминания
    if row.get("status") != "confirmed":
        update_booking_status(booking_id, "confirmed")
        print("status -> confirmed")
    else:
        print("status already confirmed")

    # 3) снять напоминания (флаги «уже отправляли» — цикл больше не шлёт)
    update_reminder_flag(booking_id, "reminder_24h_sent")
    update_reminder_flag(booking_id, "reminder_day_sent")
    print("reminders marked sent (day + 24h)")

    # 1) билет
    row = get_booking_for_ticket_resend(booking_id) or row
    vk_id = int(row.get("vk_id") or 0)
    if not vk_id:
        raise SystemExit("no vk_id on booking")

    settings = load_vk_settings()
    if not settings.is_configured:
        raise SystemExit("VK not configured in .env")

    client = VKClient(settings)
    place = f"{row.get('location') or ''}, {row.get('address') or ''}".strip(", ")
    caption = format_vk_text(
        "<b>Отлично!</b>\n\n"
        "<b>Данные по билету:</b>\n\n"
        f"<b>Ваше имя:</b> {row.get('name') or ''}\n"
        f"<b>Дата:</b> {row.get('event_date') or ''}\n"
        f"<b>Время:</b> {row.get('event_time') or ''}\n"
        f"<b>Место:</b> {place}\n"
        f"<b>Количество гостей:</b> {guests_word(int(row.get('guests') or 1))}\n\n"
        "Ждем вас на мероприятии ❤️"
    )
    keyboard = manage_ticket_keyboard(booking_id, settings.manager_link)

    try:
        result = await _resend_ticket_vk(
            row,
            updated=False,
            extra_note="",
        )
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "resend failed")
        # resend VK path doesn't attach manage keyboard — send follow-up with buttons
        await client.send_message(
            vk_id,
            "Билет выше. Если нужно — отменить или изменить дату:",
            keyboard=keyboard,
        )
        print(f"ticket sent ok booking_id={booking_id}")
        return
    except Exception as exc:
        print(f"photo resend failed: {exc}")
        print("fallback: text ticket + retry photo with stronger compression")

    # Fallback: text first so guest is not left without confirmation
    await client.send_message(vk_id, caption, keyboard=keyboard)

    try:
        from bot.vk.client import _prepare_vk_message_jpeg

        raw = _ticket_bytes(row)
        for quality in (50, 40, 30, 20):
            payload = _prepare_vk_message_jpeg(raw, quality=quality)
            try:
                attachment = await client.upload_message_photo(
                    vk_id, payload, filename=f"ticket_{booking_id}_q{quality}.jpg"
                )
                msg_id = await client.send_message(
                    vk_id, "Ваш билет 👇", attachment=attachment, keyboard=keyboard
                )
                save_ticket_message_id(booking_id, msg_id)
                print(f"ticket photo sent at quality={quality} msg_id={msg_id}")
                return
            except VKAPIError as e:
                print(f"  q={quality} failed: {e}")
                continue
        print("WARNING: confirmed + text sent, but photo still failed")
    except Exception as exc:
        print(f"WARNING: photo fallback failed: {exc}")


def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        print("Usage: fix_vk_ticket_booking.py <booking_id>", file=sys.stderr)
        return 2
    booking_id = int(sys.argv[1])
    asyncio.run(_send_ticket(booking_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
