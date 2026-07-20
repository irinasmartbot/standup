import logging
import os
import random
from datetime import datetime
from html import escape

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    FSInputFile,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import (
    AFISHA_REVIEW_URL,
    CHANNEL_LINK,
    CHANNEL_USERNAME,
    MANAGER_LINK,
    MANAGER_PHONE,
    MODERATION_CHAT_ID,
    ROZYGRYSH_SKIP_SUB_CHECK,
    ROZYGRYSH_STICKER_FILE_ID,
    SITE_URL,
    TEST_ADMIN_IDS,
    TICKET_TEMPLATE,
    bot,
    dp,
)
from bot.db.crud import (
    clear_raffle_nav,
    create_booking,
    cancel_raffle_submission,
    create_raffle_submission,
    ensure_user,
    get_active_raffle_booking,
    get_booking_by_id,
    get_last_phone,
    get_pending_raffle_submission,
    get_raffle_nav,
    get_raffle_submission,
    get_raffle_submission_by_mod_message,
    get_rozygrysh_used,
    get_total_guests,
    reset_raffle_for_user,
    save_confirm_message_id,
    save_raffle_moderation_message,
    save_raffle_nav,
    save_ticket_message_id,
    set_rozygrysh_used,
    update_booking_status,
    update_raffle_submission_status,
)
from bot.services.sheets import load_events
from bot.utils.ticket import MONTHS, format_date, generate_ticket, guests_word, now_msk

router = Router()
logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PHOTOS_DIR = os.path.join(_PROJECT_ROOT, "фото")
VENUE_PHOTO_FILES = {"temple_bar.jpg", "escobar.jpg", "nebar.jpg"}
OTZYV_PHOTO_1 = os.path.join(PHOTOS_DIR, "rozygrysh_otzyv_1.jpg")
OTZYV_PHOTO_2 = os.path.join(PHOTOS_DIR, "rozygrysh_otzyv_2.jpg")
# запасные пути (локальная разработка, если ещё не скопировали в фото/)
_OTZYV_FALLBACK_1 = os.path.join(_PROJECT_ROOT, "photo_2024-04-09_12-50-28.jpg")
_OTZYV_FALLBACK_2 = os.path.join(_PROJECT_ROOT, "photo_2024-04-09_12-50-47.jpg")

# telegram_id -> message_ids с кнопкой «Подписка есть» (для очистки)
_SUB_CHECK_MESSAGES: dict[int, list[int]] = {}
# card_message_id -> {submission_id, prompt_message_id}
_PENDING_REJECT_BY_MSG: dict[int, dict] = {}
# media_group_id — уже предупредили про альбом
_ALBUM_WARNED: set[str] = set()


def is_pending_reject_reply(reply_to_message_id: int) -> bool:
    if reply_to_message_id in _PENDING_REJECT_BY_MSG:
        return True
    return any(
        data.get("prompt_message_id") == reply_to_message_id
        for data in _PENDING_REJECT_BY_MSG.values()
    )


def _pending_reject_lookup(message_id: int):
    """Вернуть (card_message_id, data) по id карточки или подсказки."""
    data = _PENDING_REJECT_BY_MSG.get(message_id)
    if data:
        return message_id, data
    for card_id, item in _PENDING_REJECT_BY_MSG.items():
        if item.get("prompt_message_id") == message_id:
            return card_id, item
    return None, None


async def _delete_mod_chat_messages(chat_id: int, *message_ids):
    """Чистит служебные сообщения в чате модерации (остаётся только карточка)."""
    for mid in message_ids:
        if not mid:
            continue
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass


START_TEXT = (
    "Привет-привет 🥳 😊\n\n"
    "Что нужно сделать, чтобы получить билетик?\n\n"
    f"1. Быть подписанным на наш <a href=\"{CHANNEL_LINK}\">канал в телеграм</a>\n"
    "2. Выложить в соцсети <b>пост со ссылкой на наш сайт</b> или <b>оставить отзыв</b> 😊\n\n"
    "Выбирай, какой вариант тебе ближе 👇"
)

POST_TEXT = (
    f"Выкладываем в соцсети пост со ссылкой на наш сайт <b>MoscowStandUpshow.ru</b> 😊\n\n"
    "Если в Instagram* — обязательно сделай ссылку в сторис кликабельной 😉\n\n"
    "Затем нажимай кнопку ниже, отправляй скрин поста и выбирай любую дату ☺️ "
    "(после выбора даты билеты переносу не подлежат)\n\n"
    "🎫 За 1 пост полагается 1 билет\n\n"
    "____________________\n"
    "<i>*запрещено в РФ</i>"
)

REVIEW_TEXT = (
    f"Оставляем отзыв по ссылке:\n{AFISHA_REVIEW_URL}\n\n"
    "И обязательно нажать на вот эти кнопочки как на фото 😻\n\n"
    "Затем нажимайте кнопку ниже, отправляйте скрин отзыва <b>одним фото</b> "
    "и выбирайте любую дату 😻\n\n"
    "🎟️ После выбора даты билеты переносу не подлежат\n"
    "🎫 За 1 отзыв полагается 1 билет"
)

PAID_BOOKING_LINK = "https://t.me/ira_test_stend_bot?start=afisha_plat"

RULES_TEXT = (
    "<b>Порядок посещения шоу:</b>\n\n"
    "1. Сбор гостей начинается за полчаса до начала шоу\n\n"
    "2. Рассадка осуществляется администратором рассадки на ближайшие к сцене свободные места. "
    "Возможна подсадка за один стол других гостей для небольших компаний.\n"
    "❗ <b>ВНИМАНИЕ, ваш билет на одного человека, если вы хотите пойти с друзьями, они могут "
    "купить билеты на выбранное Вами шоу через систему бронирования.</b>\n\n"
    "3. Обратите внимание, что при посещении шоу заказ минимум одной позиции по меню является обязательным.\n\n"
    "4. Если поменяются планы и Вы не сможете присутствовать, пожалуйста, ОБЯЗАТЕЛЬНО ПРЕДУПРЕДИТЕ 😊\n\n"
    "5. После выбора даты билеты переносу не подлежат."
)

NOT_ALONE_TEXT = (
    f"Ваши друзья могут купить билеты на выбранное Вами шоу через "
    f"<a href=\"{PAID_BOOKING_LINK}\">систему бронирования</a>.\n\n"
    f"После этого просто напишите нашему <a href=\"{MANAGER_LINK}\">менеджеру</a>, "
    f"на какие места и на какую дату они взяли билеты.\n\n"
    f"Мы уберём из продажи соседнее место специально для Вас и посадим туда 😉"
)

TICKET_ISSUED_TEXT = (
    "Ждем вас на мероприятии ❤️\n\n"
    "❗ <b>ВНИМАНИЕ, ваш билет на одного человека</b>, если вы хотите пойти с друзьями, "
    "чтобы вас посадили вместе — нажмите кнопку «Что, если я хочу прийти не один?» "
    "и узнайте информацию.\n"
    "В противном случае вы будете сидеть на месте, которое предложит администратор рассадки.\n\n"
    "Если поменяются планы, пожалуйста, ОБЯЗАТЕЛЬНО НАЖМИТЕ КНОПКУ «Отменить бронь» 😊\n\n"
    f"При возникновении вопросов — можно писать менеджеру {{manager}} "
    f"(если срочно — звоните {MANAGER_PHONE})\n\n"
    f"И не забудь заглянуть на наш <a href=\"{CHANNEL_LINK}\">канал анонсов</a> "
    "(там часто дарят бесплатные билеты на платные шоу 😉)"
)

SCREEN_OK_TEXT = (
    "Супер, проверю твой скрин и вернусь обратно 👌\n\n"
    "Менеджер проверит скрин в течение часа, ожидайте."
)

NOT_IMAGE_TEXT = "Нужен именно скрин-картинка 📷 Пришли фото или изображение ещё раз 👇"
ALBUM_TEXT_POST = "Принимаем только 1 скрин поста — пришли одно фото 📷"
ALBUM_TEXT_REVIEW = "Принимаем только 1 скрин отзыва — пришли одно фото 📷"


class RaffleState(StatesGroup):
    waiting_screenshot = State()
    waiting_name = State()
    waiting_phone = State()


def _manager_username():
    return "@" + MANAGER_LINK.rstrip("/").split("/")[-1]


def _full_name(user) -> str:
    parts = [user.first_name or "", user.last_name or ""]
    return " ".join(p for p in parts if p).strip() or "Гость"


def _random_photo():
    ticket_name = os.path.basename(TICKET_TEMPLATE)
    try:
        files = [
            f
            for f in os.listdir(PHOTOS_DIR)
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
            and f != ticket_name
            and f.lower() not in VENUE_PHOTO_FILES
            and not f.startswith("rozygrysh_otzyv")
        ]
    except FileNotFoundError:
        files = []
    if files:
        return FSInputFile(os.path.join(PHOTOS_DIR, random.choice(files)))
    return None


async def _answer_photo(message, text, reply_markup=None, parse_mode="HTML"):
    photo = _random_photo()
    if photo:
        try:
            return await message.answer_photo(
                photo=photo, caption=text, reply_markup=reply_markup, parse_mode=parse_mode
            )
        except Exception:
            pass
    return await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)


def _start_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Билет за пост", callback_data="rz_post")
    kb.button(text="Билет за отзыв", callback_data="rz_review")
    kb.adjust(1)
    return kb.as_markup()


async def _future_best_events():
    """BEST-события строго после сегодня."""
    today = datetime.now().date()
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


async def _dates_kb():
    events = await _future_best_events()
    dates = sorted({e["date"] for e in events}, key=lambda d: datetime.strptime(d, "%d.%m.%Y"))
    kb = InlineKeyboardBuilder()
    for date in dates:
        try:
            d = datetime.strptime(date, "%d.%m.%Y")
            label = d.strftime("%d ") + MONTHS[d.strftime("%B")]
        except Exception:
            label = date
        kb.button(text=label, callback_data=f"rz_date_{date}")
    n = len(dates)
    widths = [2] * (n // 2)
    if n % 2:
        widths.append(1)
    if widths:
        kb.adjust(*widths)
    return kb.as_markup(), dates


async def can_enter_raffle(telegram_id: int) -> tuple[bool, str, int | None]:
    """(ok, reason, active_booking_id или None)."""
    if get_pending_raffle_submission(telegram_id):
        return False, "Ваш скрин на модерации, ожидайте ⏳", None
    if get_rozygrysh_used(telegram_id):
        return False, "Ты уже использовал(а) возможность получить бесплатный билет по розыгрышу 😊", None
    active = get_active_raffle_booking(telegram_id)
    if active:
        return (
            False,
            "У тебя уже есть активная бронь по розыгрышу. Дождись шоу или отмени бронь 😊",
            int(active[0]),
        )
    return True, "", None


def _can_reset_raffle(telegram_id: int) -> bool:
    # В тестовом режиме — любой в личке; иначе только TEST_ADMIN_IDS
    if ROZYGRYSH_SKIP_SUB_CHECK:
        return True
    return telegram_id in TEST_ADMIN_IDS


@router.message(Command("reset_rozygrysh"), F.chat.type == "private")
async def reset_rozygrysh_cmd(message: Message, state: FSMContext):
    """Сброс своей ветки розыгрыша для повторного теста (без рестарта бота)."""
    if not _can_reset_raffle(message.from_user.id):
        await message.answer("Команда недоступна.")
        return

    stats = reset_raffle_for_user(message.from_user.id)
    _SUB_CHECK_MESSAGES.pop(message.from_user.id, None)
    await state.clear()
    await message.answer(
        "Розыгрыш сброшен для тебя ✅\n\n"
        f"• флаг использован: сброшен\n"
        f"• отменено броней: {stats['bookings_cancelled']}\n"
        f"• снято заявок на модерации: {stats['submissions_cancelled']}\n\n"
        "Можно снова открыть:\n"
        "https://t.me/StandUp_Show_bot?start=standup_rozygr\n\n"
        "Временная тест-ссылка:\n"
        "https://t.me/ira_test_stend_bot?start=standup_rozygr"
    )


async def send_raffle_start(message: Message, state: FSMContext):
    ensure_user(message.from_user.id, message.from_user.username, _full_name(message.from_user))
    ok, reason, booking_id = await can_enter_raffle(message.from_user.id)
    if not ok:
        markup = None
        if booking_id:
            kb = InlineKeyboardBuilder()
            kb.button(text="Отменить бронирование", callback_data=f"rz_cancel_{booking_id}")
            kb.adjust(1)
            markup = kb.as_markup()
        await message.answer(reason, reply_markup=markup)
        return
    await state.clear()
    await message.answer(START_TEXT, reply_markup=_start_kb(), parse_mode="HTML", disable_web_page_preview=True)


async def _guard_action(call: CallbackQuery) -> bool:
    """False = нельзя продолжать (уже использовано / нет доступа)."""
    if get_rozygrysh_used(call.from_user.id) and not get_active_raffle_booking(call.from_user.id):
        await call.answer("Возможность уже использована", show_alert=True)
        return False
    return True


async def _delete_call_message(call: CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass


# ─── вход / ветки пост и отзыв ────────────────────────────────────────────────


@router.callback_query(F.data == "rz_post")
async def rz_post(call: CallbackQuery, state: FSMContext):
    if not await _guard_action(call):
        return
    ok, reason, _ = await can_enter_raffle(call.from_user.id)
    if not ok:
        await call.answer(reason, show_alert=True)
        return
    await _delete_call_message(call)
    kb = InlineKeyboardBuilder()
    kb.button(text="Я выложил, вот те крест", callback_data="rz_post_cross", style="success")
    kb.button(text="Я выложил, вот те скрин", callback_data="rz_post_screen", style="danger")
    kb.adjust(1)
    await call.message.answer(POST_TEXT, reply_markup=kb.as_markup(), parse_mode="HTML")
    await state.update_data(rz_kind="post")
    await call.answer()


def _otzyv_photo_paths():
    paths = []
    for primary, fallback in (
        (OTZYV_PHOTO_1, _OTZYV_FALLBACK_1),
        (OTZYV_PHOTO_2, _OTZYV_FALLBACK_2),
    ):
        if os.path.exists(primary):
            paths.append(primary)
        elif os.path.exists(fallback):
            paths.append(fallback)
    return paths


@router.callback_query(F.data == "rz_review")
async def rz_review(call: CallbackQuery, state: FSMContext):
    if not await _guard_action(call):
        return
    ok, reason, _ = await can_enter_raffle(call.from_user.id)
    if not ok:
        await call.answer(reason, show_alert=True)
        return
    await _delete_call_message(call)
    kb = InlineKeyboardBuilder()
    kb.button(text="Отправить скрин", callback_data="rz_review_send")
    kb.adjust(1)
    for path in _otzyv_photo_paths():
        try:
            await call.message.answer_photo(FSInputFile(path))
        except Exception:
            logger.exception("Failed to send review instruction photo %s", path)
    await call.message.answer(
        REVIEW_TEXT,
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await state.update_data(rz_kind="review")
    await call.answer()


async def _arm_screenshot_wait(state: FSMContext, kind: str):
    """Включает приём скрина только после явной кнопки в ветке розыгрыша."""
    await state.update_data(rz_kind=kind, screen_requested=True, raffle_flow=True)
    await state.set_state(RaffleState.waiting_screenshot)


async def _arm_screenshot_wait_for_telegram_id(telegram_id: int, kind: str):
    """Как _arm_screenshot_wait, но по telegram_id клиента (нет своего FSMContext, например после отказа модератором)."""
    key = StorageKey(bot_id=bot.id, chat_id=telegram_id, user_id=telegram_id)
    ctx = FSMContext(storage=dp.storage, key=key)
    await ctx.update_data(rz_kind=kind, screen_requested=True, raffle_flow=True)
    await ctx.set_state(RaffleState.waiting_screenshot)


def _mod_chat_id():
    if not MODERATION_CHAT_ID:
        return None
    try:
        return int(MODERATION_CHAT_ID)
    except (TypeError, ValueError):
        return None


def _is_moderation_chat(chat_id: int) -> bool:
    mid = _mod_chat_id()
    return mid is not None and chat_id == mid


@router.callback_query(F.data == "rz_post_cross")
async def rz_post_cross(call: CallbackQuery, state: FSMContext):
    if not await _guard_action(call):
        return
    await _arm_screenshot_wait(state, "post")
    await call.message.answer("Спасибо, но ждём скрин поста 😉 Кидай ниже 👇")
    await call.answer()


@router.callback_query(F.data == "rz_post_screen")
async def rz_post_screen(call: CallbackQuery, state: FSMContext):
    if not await _guard_action(call):
        return
    await _arm_screenshot_wait(state, "post")
    await call.message.answer("Супер, кидай сюда 👇")
    await call.answer()


@router.callback_query(F.data == "rz_review_send")
async def rz_review_send(call: CallbackQuery, state: FSMContext):
    if not await _guard_action(call):
        return
    await _arm_screenshot_wait(state, "review")
    await call.message.answer("Супер, кидай сюда скрин (одним фото) 👇")
    await call.answer()


@router.message(RaffleState.waiting_screenshot, F.chat.type == "private")
async def rz_receive_screenshot(message: Message, state: FSMContext):
    data = await state.get_data()
    kind = data.get("rz_kind")
    # Только после кнопки в воронке розыгрыша — никакие «левые» сообщения/фото не уходят в модерацию
    if not data.get("screen_requested") or not data.get("raffle_flow") or kind not in {"post", "review"}:
        await state.clear()
        return

    if message.media_group_id:
        group_id = str(message.media_group_id)
        if group_id in _ALBUM_WARNED:
            return
        _ALBUM_WARNED.add(group_id)
        if len(_ALBUM_WARNED) > 200:
            _ALBUM_WARNED.clear()
        text = ALBUM_TEXT_REVIEW if kind == "review" else ALBUM_TEXT_POST
        await message.answer(text)
        return

    photo = None
    if message.photo:
        photo = message.photo[-1]
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        photo = message.document
    if not photo:
        await message.answer(NOT_IMAGE_TEXT)
        return

    pending = get_pending_raffle_submission(message.from_user.id)
    if pending:
        # если заявка «pending», но в чат так и не ушла — не блокируем повтор
        if not pending[4]:
            cancel_raffle_submission(pending[0], reason="stale_undelivered")
        else:
            await message.answer("Ваш скрин на модерации, ожидайте ⏳")
            await state.clear()
            return

    # сразу снимаем «ожидание», чтобы повтор/гонка не отправили второй пост
    await state.clear()

    file_id = photo.file_id
    full_name = _full_name(message.from_user)
    username = message.from_user.username or ""
    try:
        submission_id = create_raffle_submission(
            message.from_user.id, username, full_name, kind, file_id
        )
    except Exception:
        logger.exception("Failed to create raffle submission")
        await message.answer("Не удалось отправить скрин на проверку. Попробуй позже или напиши менеджеру.")
        return

    sent_ok = await _send_to_moderation(
        submission_id, message.from_user.id, username, full_name, kind, file_id
    )
    if sent_ok:
        await message.answer(SCREEN_OK_TEXT)
        return

    # иначе клиент думает, что всё ок, а в чате модерации пусто
    cancel_raffle_submission(submission_id, reason="moderation_send_failed")
    await message.answer(
        "Не удалось отправить скрин менеджеру 😔\n"
        "Попробуй ещё раз через кнопку ниже или напиши @ccoverr."
    )
    kb = InlineKeyboardBuilder()
    if kind == "review":
        kb.button(text="Отправить скрин", callback_data="rz_review_send")
    else:
        kb.button(text="Я выложил, вот те скрин", callback_data="rz_post_screen")
    kb.adjust(1)
    await message.answer("Можешь отправить скрин ещё раз 👇", reply_markup=kb.as_markup())


async def _send_to_moderation(submission_id, telegram_id, username, full_name, kind, file_id) -> bool:
    chat_id = _mod_chat_id()
    if not chat_id:
        logger.error("MODERATION_CHAT_ID is not set or invalid")
        return False
    if kind not in {"post", "review"}:
        logger.error("Refusing moderation post with invalid kind=%s", kind)
        return False
    kind_label = "отзыва" if kind == "review" else "поста"
    uname = f"@{username}" if username else f"id {telegram_id}"
    caption = (
        f"{escape(full_name)} {escape(uname)} прислал СКРИН {kind_label}\n"
        f"Заявка #{submission_id}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="ПРИНЯТЬ", callback_data=f"rz_mod_ok_{submission_id}", style="success")
    kb.button(
        text="ОТКЛОНИТЬ без комментария",
        callback_data=f"rz_mod_no_silent_{submission_id}",
        style="danger",
    )
    kb.button(
        text="ОТКЛОНИТЬ с комментарием",
        callback_data=f"rz_mod_no_reason_{submission_id}",
    )
    kb.adjust(1)
    try:
        # Только наша карточка модерации — без forward произвольных сообщений клиента
        sent = await bot.send_photo(
            chat_id=chat_id,
            photo=file_id,
            caption=caption,
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
        save_raffle_moderation_message(submission_id, chat_id, sent.message_id)
        return True
    except Exception:
        logger.exception("Failed to send screenshot to moderation chat %s", chat_id)
        return False


# ─── модерация ────────────────────────────────────────────────────────────────


def _client_label(row) -> str:
    uname = f"@{row[2]}" if row[2] else f"tg_id:{row[1]}"
    name = (row[3] or "").strip()
    return f"{name} {uname}".strip()


async def _mod_caption_fallback(row) -> str:
    kind_label = "отзыва" if row[4] == "review" else "поста"
    return (
        f"{_client_label(row)} прислал СКРИН {kind_label}\n"
        f"Заявка #{row[0]}"
    )


async def _set_mod_card_status(message_or_ids, row, status_block: str):
    """Убирает кнопки и пишет статус на карточке скрина."""
    if hasattr(message_or_ids, "edit_caption"):
        chat_id = message_or_ids.chat.id
        message_id = message_or_ids.message_id
        edit = message_or_ids.edit_caption
        try:
            await edit(
                caption=(await _mod_caption_fallback(row)) + status_block,
                reply_markup=None,
            )
            return
        except Exception:
            pass
    else:
        chat_id, message_id = message_or_ids
    try:
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=(await _mod_caption_fallback(row)) + status_block,
            reply_markup=None,
        )
    except Exception:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=None
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("rz_mod_ok_"))
async def rz_mod_ok(call: CallbackQuery, state: FSMContext):
    if not _is_moderation_chat(call.message.chat.id):
        await call.answer("Недоступно", show_alert=True)
        return
    # id заявки из кнопки этой карточки — не из «последнего» сообщения в чате
    submission_id = int(call.data.replace("rz_mod_ok_", ""))
    row = get_raffle_submission(submission_id)
    if not row:
        await call.answer("Заявка не найдена", show_alert=True)
        return
    if row[5] != "pending":
        await call.answer("Уже обработано", show_alert=True)
        return

    pending = _PENDING_REJECT_BY_MSG.pop(call.message.message_id, None)
    if pending:
        await _delete_mod_chat_messages(
            call.message.chat.id, pending.get("prompt_message_id")
        )
    update_raffle_submission_status(submission_id, "approved")
    now = now_msk().strftime("%d.%m.%Y в %H:%M")
    await _set_mod_card_status(
        call.message,
        row,
        f"\n\n✅ Скрин принят {now}",
    )

    # только клиент из этой заявки
    telegram_id = int(row[1])
    await bot.send_message(telegram_id, "Класс, скрин принят. Теперь проверим подписку на канал 👌")
    if ROZYGRYSH_STICKER_FILE_ID:
        try:
            await bot.send_sticker(telegram_id, ROZYGRYSH_STICKER_FILE_ID)
        except Exception:
            pass
    else:
        try:
            await bot.send_message(telegram_id, "🎉")
        except Exception:
            pass

    await _continue_after_subscribe_check(telegram_id)
    await call.answer()


async def _reject_submission(row, reason: str | None, card_ref, cleanup_chat_id=None, *cleanup_ids):
    """Отклоняет заявку, обновляет карточку, пишет клиенту, чистит служебные сообщения."""
    submission_id = int(row[0])
    update_raffle_submission_status(submission_id, "rejected", reject_reason=reason or None)
    now = now_msk().strftime("%d.%m.%Y в %H:%M")
    status_lines = f"\n\n❌ Скрин отклонен {now}"
    if reason:
        status_lines += f"\nПричина: {reason}"
    await _set_mod_card_status(card_ref, row, status_lines)
    if cleanup_chat_id:
        await _delete_mod_chat_messages(cleanup_chat_id, *cleanup_ids)

    kind = row[4]
    telegram_id = int(row[1])
    if kind == "review":
        text = "К сожалению скрин не прошел модерацию. 😔\nОтправь скрин отзыва еще раз 👇"
    else:
        text = "К сожалению скрин не прошел модерацию. 😔\nОтправь скрин поста еще раз 👇"
    if reason:
        text += f"\n\nКомментарий менеджера: {reason}"
    await bot.send_message(telegram_id, text)
    await _arm_screenshot_wait_for_telegram_id(telegram_id, kind)


@router.callback_query(F.data.startswith("rz_mod_no_silent_"))
async def rz_mod_no_silent(call: CallbackQuery, state: FSMContext):
    if not _is_moderation_chat(call.message.chat.id):
        await call.answer("Недоступно", show_alert=True)
        return
    submission_id = int(call.data.replace("rz_mod_no_silent_", ""))
    row = get_raffle_submission(submission_id)
    if not row:
        await call.answer("Заявка не найдена", show_alert=True)
        return
    if row[5] != "pending":
        await call.answer("Уже обработано", show_alert=True)
        return

    pending = _PENDING_REJECT_BY_MSG.pop(call.message.message_id, None)
    if pending:
        await _delete_mod_chat_messages(
            call.message.chat.id, pending.get("prompt_message_id")
        )
    await _reject_submission(row, None, call.message)
    await call.answer("Отклонено")


@router.callback_query(F.data.startswith("rz_mod_no_reason_"))
async def rz_mod_no_reason(call: CallbackQuery, state: FSMContext):
    if not _is_moderation_chat(call.message.chat.id):
        await call.answer("Недоступно", show_alert=True)
        return
    submission_id = int(call.data.replace("rz_mod_no_reason_", ""))
    row = get_raffle_submission(submission_id)
    if not row:
        await call.answer("Заявка не найдена", show_alert=True)
        return
    if row[5] != "pending":
        await call.answer("Уже обработано", show_alert=True)
        return

    card_msg_id = call.message.message_id
    await _set_mod_card_status(
        call.message,
        row,
        f"\n\n⏳ Ожидаем причину отказа…\nЗаявка #{submission_id}",
    )
    prompt = await call.message.reply(
        'Напишите причину отказа, нажав «ответить» на сообщение с данным скрином\n\n'
        f"Заявка #{submission_id} → {_client_label(row)}",
        parse_mode="HTML",
    )
    _PENDING_REJECT_BY_MSG[card_msg_id] = {
        "submission_id": submission_id,
        "prompt_message_id": prompt.message_id,
    }
    await call.answer()


@router.message(F.reply_to_message)
async def rz_mod_reject_reason(message: Message, state: FSMContext):
    """Причина отказа — reply на карточку после «ОТКЛОНИТЬ с комментарием»."""
    if not _is_moderation_chat(message.chat.id):
        raise SkipHandler
    replied_id = message.reply_to_message.message_id
    card_msg_id, pending = _pending_reject_lookup(replied_id)
    if not pending and message.reply_to_message.reply_to_message:
        card_msg_id, pending = _pending_reject_lookup(
            message.reply_to_message.reply_to_message.message_id
        )
    if not pending:
        raise SkipHandler

    submission_id = pending["submission_id"]
    prompt_message_id = pending.get("prompt_message_id")
    row = get_raffle_submission(submission_id)
    if not row:
        row = get_raffle_submission_by_mod_message(message.chat.id, card_msg_id)
    if not row or row[5] != "pending":
        _PENDING_REJECT_BY_MSG.pop(card_msg_id, None)
        err = await message.reply("Заявка уже обработана.")
        await _delete_mod_chat_messages(
            message.chat.id, prompt_message_id, message.message_id, err.message_id
        )
        return
    if int(row[0]) != int(submission_id):
        err = await message.reply(
            "Ошибка привязки заявки. Нажмите «ОТКЛОНИТЬ с комментарием» ещё раз на нужном скрине."
        )
        await _delete_mod_chat_messages(message.chat.id, err.message_id)
        return

    reason = (message.text or "").strip()
    if not reason:
        await message.reply(
            "Нужен текст причины. Ответьте реплаем на карточку со скрином."
        )
        return

    _PENDING_REJECT_BY_MSG.pop(card_msg_id, None)
    mod_chat_id = row[7] or message.chat.id
    mod_msg_id = row[8] or card_msg_id
    await _reject_submission(
        row,
        reason,
        (mod_chat_id, mod_msg_id),
        message.chat.id,
        prompt_message_id,
        message.message_id,
    )


# ─── подписка ─────────────────────────────────────────────────────────────────


async def _is_subscribed(telegram_id: int) -> bool:
    # Временная заглушка для теста, пока бот не админ канала
    if ROZYGRYSH_SKIP_SUB_CHECK:
        logger.info("ROZYGRYSH_SKIP_SUB_CHECK=1 — skip channel check for %s", telegram_id)
        return True
    try:
        member = await bot.get_chat_member(f"@{CHANNEL_USERNAME}", telegram_id)
        return member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.RESTRICTED,
        }
    except Exception:
        logger.exception("Subscription check failed for %s", telegram_id)
        return False


async def _continue_after_subscribe_check(telegram_id: int, manual_attempts: int = 0):
    if await _is_subscribed(telegram_id):
        await _send_subscribed_and_dates(telegram_id)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="Подписаться", url=CHANNEL_LINK)
    if manual_attempts < 1:
        kb.button(text="Подписка есть 🤝", callback_data=f"rz_sub_check_{manual_attempts}")
    kb.adjust(1)
    text = (
        "Кажется вы все еще не подписаны на наш канал. "
        "Для участия в розыгрыше, нужно подписаться."
    )
    sent = await bot.send_message(telegram_id, text, reply_markup=kb.as_markup())
    _SUB_CHECK_MESSAGES.setdefault(telegram_id, []).append(sent.message_id)


async def _send_subscribed_and_dates(telegram_id: int):
    # удалить старые сообщения с «Подписка есть»
    for mid in _SUB_CHECK_MESSAGES.pop(telegram_id, []):
        try:
            await bot.delete_message(telegram_id, mid)
        except Exception:
            pass

    await bot.send_message(
        telegram_id,
        "Отлично! Видим, что вы уже подписаны\n\n"
        f"Отправь ссылку для подписки другу или подруге: {CHANNEL_LINK}",
        disable_web_page_preview=True,
    )
    markup, dates = await _dates_kb()
    if not dates:
        await bot.send_message(telegram_id, "Пока нет доступных дат для бесплатного билета 😔 Загляни позже!")
        return
    sent = await bot.send_message(
        telegram_id,
        "Теперь выбирай дату, на которую хочешь получить бесплатный билет 😉",
    )
    # фото + даты
    photo = _random_photo()
    if photo:
        try:
            dates_msg = await bot.send_photo(
                telegram_id,
                photo=photo,
                caption="Выбирай дату 👇",
                reply_markup=markup,
            )
        except Exception:
            dates_msg = await bot.send_message(telegram_id, "Выбирай дату 👇", reply_markup=markup)
    else:
        dates_msg = await bot.send_message(telegram_id, "Выбирай дату 👇", reply_markup=markup)
    save_raffle_nav(
        telegram_id,
        prompt_message_id=sent.message_id,
        dates_message_id=dates_msg.message_id,
    )


@router.callback_query(F.data.startswith("rz_sub_check_"))
async def rz_sub_check(call: CallbackQuery):
    attempts = int(call.data.replace("rz_sub_check_", "") or "0")
    if await _is_subscribed(call.from_user.id):
        await call.answer()
        await _send_subscribed_and_dates(call.from_user.id)
        return

    next_attempts = attempts + 1
    await call.answer("Подписка не найдена", show_alert=True)

    # удалить все сообщения с кнопкой проверки
    for mid in _SUB_CHECK_MESSAGES.pop(call.from_user.id, []):
        try:
            await bot.delete_message(call.from_user.id, mid)
        except Exception:
            pass
    try:
        await call.message.delete()
    except Exception:
        pass

    await _continue_after_subscribe_check(call.from_user.id, manual_attempts=next_attempts)


@router.chat_member()
async def rz_channel_join(event: ChatMemberUpdated):
    """Автопродолжение после реальной подписки на канал."""
    username = (event.chat.username or "").lower()
    if username != CHANNEL_USERNAME.lower():
        return

    old = event.old_chat_member.status
    new = event.new_chat_member.status
    was_out = old in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
    is_in = new in {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.RESTRICTED,
    }
    if not (was_out and is_in):
        return

    user_id = event.new_chat_member.user.id
    if get_active_raffle_booking(user_id) or get_rozygrysh_used(user_id):
        return
    if get_pending_raffle_submission(user_id):
        return
    # продолжаем только если ждём подписку
    if user_id not in _SUB_CHECK_MESSAGES:
        return
    await _send_subscribed_and_dates(user_id)


# ─── даты / карточка ──────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("rz_date_"))
async def rz_date(call: CallbackQuery, state: FSMContext):
    if not await _guard_action(call):
        return
    if get_active_raffle_booking(call.from_user.id):
        await call.answer("У тебя уже есть активная бронь", show_alert=True)
        return

    date = call.data.replace("rz_date_", "", 1)
    events = [e for e in await _future_best_events() if e["date"] == date]
    if not events:
        await call.answer("Эта дата уже недоступна", show_alert=True)
        markup, _ = await _dates_kb()
        await call.message.answer("Выбери другую дату 👇", reply_markup=markup)
        await call.answer()
        return

    if len(events) == 1:
        await _send_event_card(call.message, events[0], call.from_user.id)
    else:
        kb = InlineKeyboardBuilder()
        for event in events:
            label = f"{event['time']} · {event.get('location') or 'шоу'}"
            kb.button(text=label, callback_data=f"rz_event_{event['id']}")
        kb.button(text="◀️ Назад к датам", callback_data="rz_dates")
        kb.adjust(1)
        await call.message.answer(f"Шоу на {format_date(date)} 👇", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data == "rz_dates")
async def rz_dates(call: CallbackQuery):
    if not await _guard_action(call):
        return
    markup, dates = await _dates_kb()
    if not dates:
        await call.answer("Нет доступных дат", show_alert=True)
        return
    sent = await call.message.answer("Выбирай дату 👇", reply_markup=markup)
    save_raffle_nav(call.from_user.id, dates_message_id=sent.message_id)
    await call.answer()


@router.callback_query(F.data.startswith("rz_event_"))
async def rz_event(call: CallbackQuery):
    if not await _guard_action(call):
        return
    event_id = int(call.data.replace("rz_event_", ""))
    event = next((e for e in await _future_best_events() if e["id"] == event_id), None)
    if not event:
        await call.answer("Мероприятие недоступно", show_alert=True)
        return
    await _send_event_card(call.message, event, call.from_user.id)
    await call.answer()


async def _send_event_card(message, event, telegram_id: int):
    text = "\n".join(
        [
            f"<b>{format_date(event['date'])}</b>",
            escape(event.get("weekday") or ""),
            "",
            f"<b>{escape(event.get('time') or '')}</b>",
            escape(event.get("address") or ""),
            escape(event.get("description") or ""),
        ]
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Забронировать билет", callback_data=f"rz_book_{event['id']}")
    kb.button(text="📋 Правила бронирования", callback_data="rz_rules")
    kb.button(text="◀️ Назад", callback_data="rz_dates")
    kb.adjust(1)
    image = event.get("image") or ""
    sent = None
    if image:
        try:
            sent = await message.answer_photo(
                photo=image, caption=text, reply_markup=kb.as_markup(), parse_mode="HTML"
            )
        except Exception:
            sent = None
    if sent is None:
        sent = await message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    save_raffle_nav(telegram_id, card_message_id=sent.message_id)


@router.callback_query(F.data == "rz_rules")
async def rz_rules(call: CallbackQuery):
    await call.message.answer(RULES_TEXT, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "rz_not_alone")
async def rz_not_alone(call: CallbackQuery):
    await call.message.answer(NOT_ALONE_TEXT, parse_mode="HTML", disable_web_page_preview=True)
    await call.answer()


# ─── бронь ────────────────────────────────────────────────────────────────────


def _phone_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


@router.callback_query(F.data.startswith("rz_book_"))
async def rz_book(call: CallbackQuery, state: FSMContext):
    if not await _guard_action(call):
        return
    if get_active_raffle_booking(call.from_user.id) or get_rozygrysh_used(call.from_user.id):
        await call.answer("Возможность уже использована или бронь активна", show_alert=True)
        return

    event_id = int(call.data.replace("rz_book_", ""))
    event = next((e for e in await _future_best_events() if e["id"] == event_id), None)
    if not event:
        await call.answer("Мероприятие недоступно", show_alert=True)
        return

    await state.update_data(
        event_id=event["id"],
        event_date=event["date"],
        event_time=event["time"],
        event_address=event.get("address") or "",
        event_location=event.get("location") or "",
        event_weekday=event.get("weekday") or "",
        max_seats=event.get("max_seats") or 0,
    )
    name = _full_name(call.from_user)
    await state.update_data(name=name)
    kb = InlineKeyboardBuilder()
    kb.button(text="Все верно 👌", callback_data="rz_name_ok")
    kb.button(text="Изменить", callback_data="rz_name_change")
    kb.adjust(2)
    await call.message.answer(
        "Для бронирования вам нужно заполнить некоторые данные\n\n"
        f"Ваше имя <b>{escape(name)}</b>, верно?",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "rz_name_ok")
async def rz_name_ok(call: CallbackQuery, state: FSMContext):
    await _ask_phone(call.message, state, call.from_user.id)
    await call.answer()


@router.callback_query(F.data == "rz_name_change")
async def rz_name_change(call: CallbackQuery, state: FSMContext):
    await call.message.answer("Напишите, пожалуйста, ваше имя.")
    await state.set_state(RaffleState.waiting_name)
    await call.answer()


@router.message(RaffleState.waiting_name)
async def rz_process_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Напишите имя текстом 🙂")
        return
    await state.update_data(name=name)
    await _ask_phone(message, state, message.from_user.id)


async def _ask_phone(message, state: FSMContext, telegram_id: int):
    saved = get_last_phone(telegram_id)
    if saved:
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Да, использовать", callback_data="rz_phone_saved")
        kb.button(text="✏️ Ввести другой номер", callback_data="rz_phone_change")
        kb.adjust(1)
        await message.answer(
            f"Ваш номер телефона: <b>{escape(saved)}</b>\nИспользовать его?",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
        await state.update_data(phone=saved)
    else:
        await message.answer(
            "Поделитесь номером телефона или введите вручную:",
            reply_markup=_phone_kb(),
        )
        await state.set_state(RaffleState.waiting_phone)


@router.callback_query(F.data == "rz_phone_saved")
async def rz_phone_saved(call: CallbackQuery, state: FSMContext):
    await _finish_booking(call.message, state, call.from_user)
    await call.answer()


@router.callback_query(F.data == "rz_phone_change")
async def rz_phone_change(call: CallbackQuery, state: FSMContext):
    await call.message.answer(
        "Поделитесь номером телефона или введите вручную:",
        reply_markup=_phone_kb(),
    )
    await state.set_state(RaffleState.waiting_phone)
    await call.answer()


@router.message(RaffleState.waiting_phone, F.contact)
async def rz_phone_contact(message: Message, state: FSMContext):
    await state.update_data(phone=message.contact.phone_number)
    await _finish_booking(message, state, message.from_user)


@router.message(RaffleState.waiting_phone)
async def rz_phone_text(message: Message, state: FSMContext):
    phone = (message.text or "").strip()
    if len(phone) < 5:
        await message.answer("Кажется, это не номер. Пришли контакт или номер ещё раз.")
        return
    await state.update_data(phone=phone)
    await _finish_booking(message, state, message.from_user)


def _manage_kb(booking_id, include_ticket=False):
    kb = InlineKeyboardBuilder()
    if include_ticket:
        kb.button(
            text="🎟 Получить билет 🎟",
            callback_data=f"rz_ticket_{booking_id}",
            style="success",
        )
    kb.button(text="Что, если я хочу прийти не один?", callback_data="rz_not_alone")
    kb.button(text="Отменить бронь", callback_data=f"rz_cancel_{booking_id}")
    kb.button(text="Задать вопрос менеджеру", url=MANAGER_LINK)
    kb.button(text="Заглянуть на наш канал анонсов", url=CHANNEL_LINK)
    kb.adjust(1)
    return kb.as_markup()


def _ticket_manage_kb(booking_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="Отменить бронь", callback_data=f"rz_cancel_{booking_id}")
    kb.button(text="Что, если я хочу прийти не один?", callback_data="rz_not_alone")
    kb.adjust(1)
    return kb.as_markup()


async def _finish_booking(message: Message, state: FSMContext, user):
    data = await state.get_data()
    event_date = data.get("event_date")
    event_time = data.get("event_time")
    name = data.get("name") or _full_name(user)
    phone = data.get("phone") or ""
    event_id = data.get("event_id")
    event_address = data.get("event_address") or ""
    event_location = data.get("event_location") or ""
    weekday = data.get("event_weekday") or ""
    max_seats = int(data.get("max_seats") or 0)
    guests = 1

    if get_active_raffle_booking(user.id) or get_rozygrysh_used(user.id):
        await message.answer("Бронь недоступна: розыгрыш уже использован или активная бронь есть.", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return

    if max_seats:
        total = get_total_guests(event_date, event_time)
        if total + guests > max_seats:
            await message.answer(
                "К сожалению, на это мероприятие места закончились 😔 Выбери другую дату!",
                reply_markup=ReplyKeyboardRemove(),
            )
            markup, _ = await _dates_kb()
            await message.answer("Выбирай дату 👇", reply_markup=markup)
            await state.clear()
            return

    try:
        booking_id = create_booking(
            user.id,
            user.username or "",
            name,
            phone,
            event_date,
            event_time,
            event_address,
            event_location,
            guests,
            booking_format="rozygrysh",
            event_format="best",
            event_id=event_id,
        )
    except Exception:
        logger.exception("Failed to create raffle booking")
        await message.answer(
            "Не удалось создать бронь. Попробуй другую дату или напиши менеджеру.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.clear()
        return

    await state.clear()
    date_str = format_date(event_date)
    try:
        days_until = (datetime.strptime(event_date, "%d.%m.%Y").date() - datetime.now().date()).days
    except Exception:
        days_until = 99

    location_line = f"📍 Локация {event_location}, {event_address}".strip(", ")
    if days_until <= 1:
        text = (
            f"Отлично!\n\n"
            f"❗ <b>Важная информация</b> — для того чтобы мы окончательно закрепили за Вами место "
            f"на дату и время:\n"
            f"<b>Дата:</b> {date_str}\n"
            f"<b>Время:</b> {event_time}\n\n"
            f"<b>ОБЯЗАТЕЛЬНО подтвердите бронь, нажав на кнопку «Получить билет»</b>\n\n"
            f"❗ Внимание, если Вы не успеете подтвердить бронь, она будет аннулирована.\n\n"
            f"Напоминаем, что :\n"
            f"1. Сбор гостей начинается за полчаса до начала шоу, старт в {event_time}\n"
            f"2. Рассадка осуществляется администратором рассадки на ближайшие к сцене свободные места. "
            f"Возможна подсадка за один стол других гостей для небольших компаний.\n"
            f"3. Обратите внимание, что при посещении шоу заказ минимум одной позиции по меню является обязательным.\n"
            f"4. {escape(location_line)}\n"
            f"5. Количество гостей - 1 чел.\n"
            f"6. Если поменяются планы, пожалуйста, ОБЯЗАТЕЛЬНО ПРЕДУПРЕДИТЕ 😊"
        )
        markup = _manage_kb(booking_id, include_ticket=True)
    else:
        text = (
            f"Отлично! Мы внесли Вас в списки гостей:\n\n"
            f"<b>Дата:</b> {date_str} ({escape(weekday)})\n"
            f"<b>Время:</b> {event_time}\n"
            f"<b>Локация:</b> {escape(event_address)}\n"
            f"<b>Количество гостей:</b> 1 чел.\n\n"
            f"<b>❗ Внимание, за сутки до мероприятия Вам придёт сообщение-напоминание с подробностями "
            f"и кнопкой «Получить билет». Обязательно нажмите кнопку, чтобы подтвердить бронь. "
            f"Если Вы не успеете подтвердить бронь, она будет аннулирована.</b>\n\n"
            f"Если поменяются планы, обязательно предупредите 😊"
        )
        markup = _manage_kb(booking_id, include_ticket=False)

    confirm = await message.answer(text, reply_markup=markup, parse_mode="HTML")
    save_confirm_message_id(booking_id, confirm.message_id)


# ─── билет / отмена ───────────────────────────────────────────────────────────


async def _delete_raffle_ui(telegram_id: int, booking_id=None, extra_message_ids=()):
    """Полностью удаляет сообщения выбора даты / карточки / брони / билета."""
    nav = get_raffle_nav(telegram_id)
    ids = []
    if nav:
        ids.extend(nav)
    if booking_id:
        booking = get_booking_by_id(booking_id)
        if booking:
            ticket_message_id = booking[-2]
            confirm_message_id = booking[-1]
            ids.extend([ticket_message_id, confirm_message_id])
    ids.extend(extra_message_ids)

    seen = set()
    for mid in ids:
        if not mid or mid in seen:
            continue
        seen.add(mid)
        try:
            await bot.delete_message(telegram_id, mid)
        except Exception:
            pass
    clear_raffle_nav(telegram_id)


@router.callback_query(F.data.startswith("rz_ticket_"))
async def rz_ticket(call: CallbackQuery):
    booking_id = int(call.data.replace("rz_ticket_", ""))
    row = get_booking_by_id(booking_id)
    if not row or row[1] != call.from_user.id:
        await call.answer("Бронь не найдена", show_alert=True)
        return
    if row[10] == "confirmed":
        await call.answer("Билет уже был выдан ранее.", show_alert=True)
        return
    if row[10] not in ("booked", "confirmed"):
        await call.answer("Бронь уже неактивна", show_alert=True)
        return

    name = row[3]
    event_date = row[5]
    event_time = row[6]
    event_address = row[7]
    event_location = row[8]
    guests = row[9]

    short_address = f"{event_location}, {event_address.split(',')[1] if ',' in event_address else event_address}"
    ticket_buf = generate_ticket(name, event_date, event_time, short_address, guests)
    update_booking_status(booking_id, "confirmed")
    set_rozygrysh_used(call.from_user.id, True)

    caption = TICKET_ISSUED_TEXT.format(manager=_manager_username())
    ticket_msg = await call.message.answer_photo(
        photo=BufferedInputFile(ticket_buf.getvalue(), filename=f"ticket_{booking_id}.jpg"),
        caption=caption,
        reply_markup=_ticket_manage_kb(booking_id),
        parse_mode="HTML",
    )
    save_ticket_message_id(booking_id, ticket_msg.message_id)

    # убрать кнопки с confirm
    confirm_message_id = row[-1]
    if confirm_message_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=call.from_user.id, message_id=confirm_message_id, reply_markup=None
            )
        except Exception:
            pass
    await call.answer()


@router.callback_query(
    F.data.startswith("rz_cancel_") & ~F.data.startswith("rz_cancel_do_")
)
async def rz_cancel(call: CallbackQuery):
    booking_id = int(call.data.replace("rz_cancel_", ""))
    row = get_booking_by_id(booking_id)
    if not row or row[1] != call.from_user.id:
        await call.answer("Бронь не найдена", show_alert=True)
        return
    if row[10] not in ("booked", "confirmed"):
        await call.answer("Бронь уже неактивна", show_alert=True)
        return

    # если бронь создана до фикса — запомним id сообщения с инфо о брони
    if not row[-1] and call.message and call.message.chat.type == "private":
        save_confirm_message_id(booking_id, call.message.message_id)

    kb = InlineKeyboardBuilder()
    kb.button(text="Подтверждаю", callback_data=f"rz_cancel_do_{booking_id}")
    kb.adjust(1)
    date_label = f"{format_date(row[5])} {row[6]}"
    await call.message.answer(
        f"Для подтверждения отмены брони на <b>{date_label}</b> нажмите кнопку ниже",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("rz_cancel_do_"))
async def rz_cancel_do(call: CallbackQuery):
    booking_id = int(call.data.replace("rz_cancel_do_", ""))
    row = get_booking_by_id(booking_id)
    if not row or row[1] != call.from_user.id:
        await call.answer("Бронь не найдена", show_alert=True)
        return
    if row[10] not in ("booked", "confirmed"):
        await call.answer("Бронь уже неактивна", show_alert=True)
        return

    # удаляем UI брони + подсказку «Подтверждаю»
    await _delete_raffle_ui(
        call.from_user.id,
        booking_id,
        extra_message_ids=(call.message.message_id,),
    )
    update_booking_status(booking_id, "cancelled")
    set_rozygrysh_used(call.from_user.id, False)

    kb = InlineKeyboardBuilder()
    kb.button(text="Перейти в главное меню", callback_data="main_menu")
    kb.adjust(1)
    await call.message.answer(
        f"Хорошо, спасибо, что предупредили 😊 Ждём Вас на других мероприятиях, "
        f"актуальная афиша всегда на нашем сайте: {SITE_URL.replace('https://', '')}\n\n"
        f"При возникновении вопросов - можно писать менеджеру {_manager_username()}\n\n"
        f"И не забудь заглянуть на наш <a href='{CHANNEL_LINK}'>канал анонсов</a> "
        f"(там часто дарят бесплатные билеты на платные шоу 😉)",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    # после отмены можно снова стартовать розыгрыш
    await call.message.answer(
        START_TEXT,
        reply_markup=_start_kb(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await call.answer()
