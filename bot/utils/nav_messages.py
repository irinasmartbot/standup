# Последнее сообщение выбора дат/локаций по чату (не карточки мероприятий).

_BOOKING_NAV_BY_CHAT = {}

# Сообщения, показанные командой /my_bookings (список, билет и т.п.)
_MY_BOOKINGS_MSGS_BY_CHAT: dict[int, set[int]] = {}


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
    """Стирает старые сообщения /my_bookings у клиента (список, билет из команды)."""
    ids = _MY_BOOKINGS_MSGS_BY_CHAT.pop(chat_id, set())
    for message_id in ids:
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            pass
