import os

from aiogram import Router
from aiogram.types import CallbackQuery, FSInputFile
from aiogram.utils.media_group import MediaGroupBuilder
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.config import MANAGER_LINK, CHANNEL_LINK

router = Router()

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WELCOME_MARKER = "Здесь ты сможешь узнать о нас побольше и забронировать места:"

FORMATS_TEXT = """🎭 <b>Наши форматы шоу:</b>

<b>Формат StandUp BEST:</b>
Только лучший, уже проверенный стэндап материал от троих комиков, именитых участников многочисленных телевизионных проектов. Вы не услышите ни одной несмешной шутки, только BEST!!
Билеты - от 500 рублей.

<b>Формат StandUp Проверка материала:</b>
5-7 опытных комиков, участников известных проектов ТНТ и YouTube, рассказывают по 10-15 минут свежих, но не проверенных шуток. Вы услышите настоящий эксклюзив и поможете комикам понять, что смешно, а что стоит убрать из материала 🐒
Вход бесплатный."""

RULES_TEXT = """📋 <b>Правила посещения шоу:</b>

1️⃣ <b>Возраст</b>
На наших мероприятиях действует возрастное ограничение 18+

2️⃣ <b>Время</b>
Сбор гостей начинается за полчаса до времени начала мероприятия.

3️⃣ <b>Обязательный заказ</b>
Все шоу проходят в заведениях в центре Москвы, посещение шоу предполагает обязательный заказ минимум одной позиции по меню заведения.

4️⃣ <b>Рассадка</b>
Рассадка осуществляется администратором на площадке.
Для формата Проверка материала: рассадка осуществляется по мере прихода, начиная от сцены.
Для формата StandUp Best: рассадка осуществляется в соответствии с местом в билете, при опоздании более чем на 10 минут посетитель теряет право на место.

5️⃣ <b>Тишина</b>
Во время шоу запрещено громко разговаривать, выкрикивать с места, говорить по телефону. При многократном нарушении администратор может попросить Вас удалиться из зала без возможности возврата средств."""


def _nav_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Забронировать места", callback_data="book")
    kb.button(text="🎭 Наши форматы ШОУ", callback_data="formats")
    kb.button(text="📍 Наши площадки", callback_data="venues")
    kb.button(text="📋 Правила посещения шоу", callback_data="rules")
    kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
    kb.button(text="📢 Заглянуть на наш канал анонсов", url=CHANNEL_LINK)
    kb.adjust(1)
    return kb.as_markup()


async def _delete_previous_menu_message(call: CallbackQuery):
    text = call.message.text or call.message.caption or ""
    if WELCOME_MARKER in text:
        return
    try:
        await call.message.delete()
    except Exception:
        pass


@router.callback_query(lambda c: c.data == "formats")
async def show_formats(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Бронь Формат StandUp BEST", callback_data="best")
    kb.button(text="🎟 Бронь Формат StandUp Проверка материала", callback_data="check")
    kb.button(text="◀️ Назад в меню", callback_data="main_menu")
    kb.adjust(1)
    await call.message.answer(FORMATS_TEXT, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(lambda c: c.data == "venues")
async def show_venues(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    photo_files = ["temple_bar.jpg", "escobar.jpg", "nebar.jpg"]
    text = """Мероприятия проходят в заведениях, где каждый найдёт что-то на свой вкус: для любителей вкусно покушать — рестораны с изысканной кухней разных народов мира, для поклонников шумных вечеринок — бары, для любителей попеть — заведения с караоке, везде можно остаться после шоу.

Наши площадки:

<b>Temple Bar</b> - это английская респектабельность, ирландское жизнелюбие и русское гостеприимство в одном ресторане, где каждый гость будет чувствовать демократическую атмосферу, и сможет насладиться великолепными стейками, большим ассортиментом коктейлей, а также отменными блюдами из мяса и овощей на мангале.

<b>Escobar</b> - бар с неординарной кухней, расположенный в комплексе исторических зданий 18-19 веков, брутальный дизайн в эстетике фильмов Квентина Тарантино, с легким оттенком латиноамериканской расслабленности.

<b>Небар</b> - один из самых популярных и громких баров столицы с уникальным стилем. Авторская коктейльная карта для тех, кто любит эксперименты, насчитывает 13 сезонных коктейлей на любой вкус, названных в честь известных городов мира."""
    kb = _nav_kb()

    media = MediaGroupBuilder()
    for photo_file in photo_files:
        path = os.path.join(_PROJECT_ROOT, photo_file)
        if os.path.exists(path):
            media.add_photo(FSInputFile(path))
    album = media.build()
    if album:
        await call.message.answer_media_group(media=album)

    await call.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await call.answer()


@router.callback_query(lambda c: c.data == "rules")
async def show_rules(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    await call.message.answer(RULES_TEXT, reply_markup=_nav_kb(), parse_mode="HTML")
    await call.answer()


@router.callback_query(lambda c: c.data == "book")
async def book(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    kb = InlineKeyboardBuilder()
    kb.button(text="STANDUP BEST", callback_data="best")
    kb.button(text="StandUp Проверка материала", callback_data="check")
    kb.adjust(1)
    await call.message.answer(
        "Выбирай формат шоу 👇\n\n"
        "<b>Формат StandUp BEST:</b>\n"
        "Только лучший, уже проверенный стэндап материал от троих комиков, "
        "именитых участников многочисленных телевизионных проектов. "
        "Вы не услышите ни одной несмешной шутки, только BEST!!\n"
        "Билеты - от 500 рублей.\n\n"
        "<b>Формат StandUp проверка материала:</b>\n"
        "5-7 опытных комиков, участников известных проектов ТНТ и YouTube, "
        "рассказывают по 10-15 минут свежих, но не проверенных шуток, "
        "Вы услышите настоящий эксклюзив и поможете комикам понять, "
        "что смешно, а что стоит убрать из материала 🙈\n"
        "Вход бесплатный.",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(lambda c: c.data == "best")
async def best_format(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
    kb.button(text="◀️ Назад в меню", callback_data="main_menu")
    kb.adjust(1)
    await call.message.answer(
        "Формат <b>StandUp BEST</b> — платные шоу с билетами от 500 ₽.\n\n"
        "Бронирование через бот для этого формата скоро появится. "
        "Сейчас можно забронировать через менеджера 👇",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await call.answer()
