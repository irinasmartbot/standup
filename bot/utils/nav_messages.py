# Последнее сообщение выбора дат/локаций по чату (не карточки мероприятий).

_BOOKING_NAV_BY_CHAT = {}

# Сообщения, показанные командой /my_bookings (список, билет и т.п.)
_MY_BOOKINGS_MSGS_BY_CHAT: dict[int, set[int]] = {}

# Копии билета вне ticket_message_id (перевыпуск, /my_bookings).
_EXTRA_TICKET_MSG_BY_BOOKING: dict[int, set[int]] = {}
_EXTRA_TICKET_MSG_BY_CHAT: dict[int, set[int]] = {}


def remember_issued_ticket_message(
    chat_id: int | None,
    booking_id: int | None,
    message_id: int | None,
) -> None:
    """Запомнить фото билета, чтобы навигация его не стирала."""
    try:
        mid = int(message_id)
        bid = int(booking_id) if booking_id else 0
        cid = int(chat_id) if chat_id else 0
    except (TypeError, ValueError):
        return
    if not mid:
        return
    if bid:
        _EXTRA_TICKET_MSG_BY_BOOKING.setdefault(bid, set()).add(mid)
    if cid:
        _EXTRA_TICKET_MSG_BY_CHAT.setdefault(cid, set()).add(mid)


def pop_extra_ticket_messages(booking_id: int, *, chat_id: int | None = None) -> set[int]:
    """Снять защиту копий билета этой брони — только перед явной отменой/переносом."""
    try:
        bid = int(booking_id)
    except (TypeError, ValueError):
        return set()
    ids = set(_EXTRA_TICKET_MSG_BY_BOOKING.pop(bid, set()))
    if chat_id:
        bucket = _EXTRA_TICKET_MSG_BY_CHAT.get(int(chat_id))
        if bucket:
            for mid in list(ids):
                bucket.discard(mid)
            if not bucket:
                _EXTRA_TICKET_MSG_BY_CHAT.pop(int(chat_id), None)
    return ids


def is_protected_ticket_message(telegram_id: int | None, message_id: int | None) -> bool:
    """Билет/подтверждение активной брони не стираем навигацией."""
    if not message_id:
        return False
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        return False
    if not mid:
        return False
    if telegram_id:
        try:
            if mid in _EXTRA_TICKET_MSG_BY_CHAT.get(int(telegram_id), set()):
                return True
        except (TypeError, ValueError):
            pass
    for ids in _EXTRA_TICKET_MSG_BY_BOOKING.values():
        if mid in ids:
            return True
    if not telegram_id:
        return False
    try:
        from bot.db.crud import protected_booking_chat_message_ids

        protected = protected_booking_chat_message_ids(telegram_id=int(telegram_id))
        return mid in protected
    except Exception:
        return False


def remember_booking_nav(chat_id: int, message_id: int):
    _BOOKING_NAV_BY_CHAT[chat_id] = message_id


def forget_booking_nav(chat_id: int, message_id: int | None = None):
    current = _BOOKING_NAV_BY_CHAT.get(chat_id)
    if current is None:
        return
    if message_id is None or current == message_id:
        _BOOKING_NAV_BY_CHAT.pop(chat_id, None)


async def delete_booking_nav(bot, chat_id: int):
    message_id = _BOOKING_NAV_BY_CHAT.pop(chat_id, None)
    if not message_id:
        return
    if is_protected_ticket_message(chat_id, message_id):
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def remember_my_bookings_message(chat_id: int, message_id: int):
    _MY_BOOKINGS_MSGS_BY_CHAT.setdefault(chat_id, set()).add(message_id)


def forget_my_bookings_message(chat_id: int, message_id: int | None = None):
    ids = _MY_BOOKINGS_MSGS_BY_CHAT.get(chat_id)
    if not ids:
        return
    if message_id is None:
        _MY_BOOKINGS_MSGS_BY_CHAT.pop(chat_id, None)
        return
    ids.discard(message_id)
    if not ids:
        _MY_BOOKINGS_MSGS_BY_CHAT.pop(chat_id, None)


async def delete_my_bookings_messages(bot, chat_id: int):
    """Стирает старые сообщения /my_bookings у клиента (список, не билет брони)."""
    ids = _MY_BOOKINGS_MSGS_BY_CHAT.pop(chat_id, set())
    for message_id in ids:
        if is_protected_ticket_message(chat_id, message_id):
            continue
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            pass
