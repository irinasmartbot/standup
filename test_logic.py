"""Offline checks for bot logic (no Telegram token required)."""
import asyncio
import csv
import sqlite3
import tempfile
from datetime import datetime
from io import StringIO

import aiohttp

CSV_URL = (
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vQTZS9GmN4Gkffl6xrUt7W_dDIksHB7z4xAjFDVeR-x4rgWeGJLJPGVfMfY5eQESZcXfBH-ZbrUeMXh/"
    "pub?gid=907191184&single=true&output=csv"
)


async def load_events():
    async with aiohttp.ClientSession() as session:
        async with session.get(CSV_URL) as resp:
            text = await resp.text(encoding="utf-8-sig")
    reader = csv.reader(StringIO(text))
    rows = list(reader)
    events = []
    for row in rows[1:]:
        if len(row) < 17:
            continue
        if row[16].strip() != "Актуально":
            continue
        try:
            date = datetime.strptime(row[1].strip(), "%d.%m.%Y")
        except ValueError:
            continue
        if date.date() < datetime.now().date():
            continue
        try:
            extra = int(row[9].strip()) if row[9].strip() else 0
        except ValueError:
            extra = 0
        max_seats = 60 + abs(extra)
        events.append(
            {
                "date": row[1].strip(),
                "weekday": row[2].strip(),
                "time": row[3].strip(),
                "address": row[4].strip(),
                "description": row[5].strip(),
                "image": row[6].strip(),
                "location": row[10].strip(),
                "max_seats": max_seats,
            }
        )
    return events


def test_callback_parsing():
    samples = [
        ("book_event_04.07.2026_19:30", ("04.07.2026", "19:30")),
        ("event_11.07.2026_19:30", ("11.07.2026", "19:30")),
    ]
    for raw, expected in samples:
        prefix = "book_event_" if raw.startswith("book_event_") else "event_"
        date_part, time_part = raw.replace(prefix, "", 1).split("_", 1)
        assert date_part == expected[0], f"{raw}: date {date_part} != {expected[0]}"
        assert time_part == expected[1], f"{raw}: time {time_part} != {expected[1]}"
    print("callback parsing: OK")


def test_mailing_booking_button_fields():
    from bot.db.mailing import (
        MAIL_FLOW_BOOKING_RESIDENT,
        MAIL_FLOW_RESIDENTS_DATES,
        followup_is_booking_flow,
        followup_is_residents_booking_flow,
        followup_is_residents_dates_flow,
        mailing_residents_flow_marker,
        parse_mailing_residents_event_id,
        resolve_mailing_button_fields,
    )

    url, follow, until = resolve_mailing_button_fields(
        starts_booking=True,
        button_url="https://example.com",
        followup_html="hello",
        followup_until="",
    )
    assert url == ""
    assert follow == MAIL_FLOW_BOOKING_RESIDENT
    assert followup_is_booking_flow(follow)
    assert until == "2026-09-15"

    url2, follow2, until2 = resolve_mailing_button_fields(
        starts_booking=False,
        button_url="https://example.com",
        followup_html="hello",
        followup_until="2026-10-01",
    )
    assert url2 == "https://example.com"
    assert follow2 == "hello"
    assert until2 == "2026-10-01"

    url3, follow3, until3 = resolve_mailing_button_fields(
        starts_booking=False,
        button_url="https://example.com",
        followup_html="hello",
        followup_until="2026-10-07",
        residents_event_id="42",
    )
    assert url3 == ""
    assert follow3 == mailing_residents_flow_marker("42")
    assert parse_mailing_residents_event_id(follow3) == "42"
    assert followup_is_residents_booking_flow(follow3)
    assert not followup_is_booking_flow(follow3)
    assert until3 == "2026-10-07"
    assert parse_mailing_residents_event_id("__flow:booking_resident__") is None
    assert parse_mailing_residents_event_id("__flow:residents:__") is None
    assert parse_mailing_residents_event_id("hello") is None

    url4, follow4, until4 = resolve_mailing_button_fields(
        starts_booking=False,
        button_url="https://example.com",
        followup_html="hello",
        followup_until="2026-10-28",
        residents_dates=True,
    )
    assert url4 == ""
    assert follow4 == MAIL_FLOW_RESIDENTS_DATES
    assert followup_is_residents_dates_flow(follow4)
    assert not followup_is_residents_booking_flow(follow4)
    assert until4 is None
    assert parse_mailing_residents_event_id(MAIL_FLOW_RESIDENTS_DATES) is None
    print("mailing booking button fields: OK")


def test_mailing_schedule_and_templates():
    from datetime import timedelta

    from bot.db.mailing import (
        MAILING_TEMPLATE_KEYS,
        format_admin_datetime,
        is_campaign_scheduled,
        parse_admin_datetime,
        to_datetime_local_value,
    )
    from bot.utils.ticket import now_msk

    dt = parse_admin_datetime("2026-09-20T19:30")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.hour == 19 and dt.minute == 30
    assert format_admin_datetime(dt) == "20.09.2026 19:30"
    assert to_datetime_local_value(dt) == "2026-09-20T19:30"

    future = now_msk() + timedelta(hours=2)
    past = now_msk() - timedelta(hours=2)
    assert is_campaign_scheduled({"status": "queued", "scheduled_at": future})
    assert not is_campaign_scheduled({"status": "queued", "scheduled_at": past})
    assert not is_campaign_scheduled({"status": "running", "scheduled_at": future})
    assert not is_campaign_scheduled({"status": "queued", "scheduled_at": None})
    assert MAILING_TEMPLATE_KEYS == ("best_tg", "best_vk", "hitloto_tg", "hitloto_vk")
    from bot.db.mailing import MAILING_TEMPLATE_SPECS

    by_key = {item["key"]: item for item in MAILING_TEMPLATE_SPECS}
    assert "хочу билет" in by_key["best_vk"]["body_html"]
    assert by_key["best_vk"]["button_text"] == "Хочу билет"
    assert "{адрес}" in by_key["best_vk"]["followup_html"]
    assert "{начало_vk}" in by_key["best_vk"]["followup_html"]
    assert "{напишите_vk}" in by_key["best_vk"]["followup_html"]
    assert "успеваете" not in by_key["best_vk"]["followup_html"]
    assert "{время}" in by_key["best_vk"]["body_html"]
    assert "{когда}" not in by_key["best_vk"]["body_html"]
    assert "{когда}" not in by_key["best_vk"]["followup_html"]
    assert "Шоу в {время}" in by_key["best_vk"]["body_html"]
    assert "{концерт}" in by_key["best_tg"]["body_html"]
    assert "{концерт_дата}" not in by_key["best_tg"]["body_html"]
    assert "{концерт_дата}" in by_key["best_vk"]["body_html"]
    assert by_key["best_tg"]["uses_show"] is True
    assert "@ccoverr" in by_key["best_tg"]["body_html"]
    assert not by_key["best_tg"]["followup_html"]
    assert "01 августа" in by_key["hitloto_vk"]["body_html"]
    assert "20:30" in by_key["hitloto_vk"]["followup_html"]
    assert "@ccoverr" in by_key["hitloto_tg"]["body_html"]
    assert not by_key["hitloto_tg"]["followup_html"]
    print("mailing schedule and templates: OK")


def test_mailing_best_show_fill():
    from datetime import date

    from bot.db.mailing import (
        MAILING_TEMPLATE_SPECS,
        apply_mailing_show_fields,
        ensure_best_placeholders,
        ensure_best_vk_once,
        mailing_place_phrase,
        mailing_show_fields,
    )

    assert mailing_place_phrase("Escobar", "") == "Эскобаре на м.Площадь Ильича"
    assert mailing_place_phrase("Temple Bar", "") == "Temple Bar на м.Курская"

    today = date(2026, 9, 17)
    thursday = mailing_show_fields(
        {
            "date_iso": "2026-09-17",
            "date_display": "17.09.2026",
            "time": "20:00",
            "location": "Escobar",
            "address": "ESCOBAR, м. Площадь Ильича, ул. Сергия Радонежского, 15-17с17",
        },
        today=today,
    )
    assert thursday["{когда}"] == "сегодня"
    assert thursday["{концерт}"] == "СЕГОДНЯШНИЙ концерт"
    assert thursday["{концерт_дата}"] == "СЕГОДНЯШНИЙ концерт"
    assert thursday["{начало_vk}"] == "начало в 20:00, успеваете?"
    assert thursday["{напишите_vk}"] == "Если да, напишите"
    assert "Эскобаре" in thursday["{площадка}"]
    assert "Сергия Радонежского" in thursday["{адрес}"]

    sunday = mailing_show_fields(
        {
            "date_iso": "2026-09-20",
            "date_display": "20.09.2026",
            "time": "19:00",
            "location": "Temple Bar",
            "address": "Temple Bar, м. Курская, Нижний Сусальный переулок, дом 5, стр. 4а",
        },
        today=today,
    )
    assert sunday["{когда}"] == "20 сентября"
    assert sunday["{концерт}"] == "концерт"
    assert sunday["{концерт_дата}"] == "концерт 20 сентября"
    assert sunday["{время}"] == "19:00"
    assert sunday["{начало_vk}"] == "начало в 19:00, 20 сентября"
    assert sunday["{напишите_vk}"] == "Напишите"
    assert "успеваете" not in sunday["{начало_vk}"]
    assert "Temple Bar" in sunday["{площадка}"]

    old = (
        "билетов на СЕГОДНЯШНИЙ концерт первым\n"
        "<b>Шоу сегодня в 20:00 в Эскобаре на м.Площадь Ильича ☝️</b>\n"
        "начало сегодня в 20:00, успеваете?\n"
        "📍 Адрес ESCOBAR, м. Площадь Ильича, ул. Сергия Радонежского, 15-17с17"
    )
    marked = ensure_best_placeholders(old)
    assert "{концерт}" in marked
    assert "{время}" in marked
    assert "{адрес}" in marked
    filled = apply_mailing_show_fields(marked, sunday)
    assert "на концерт первым" in filled
    assert "Шоу 20 сентября" in filled
    assert "на концерт 20 сентября" not in filled
    assert "19:00" in filled
    assert "Temple Bar" in filled
    assert "Сусальный" in filled
    assert "20:00" not in filled

    vk = {item["key"]: item for item in MAILING_TEMPLATE_SPECS}["best_vk"]
    vk_body = apply_mailing_show_fields(ensure_best_vk_once(vk["body_html"]), sunday)
    vk_follow = apply_mailing_show_fields(ensure_best_vk_once(vk["followup_html"]), sunday)
    assert vk_body.count("20 сентября") == 1
    assert "на концерт 20 сентября" in vk_body
    assert "начало в 19:00, 20 сентября" in vk_follow
    assert "успеваете" not in vk_follow
    assert "Если да" not in vk_follow
    assert vk_follow.startswith("Здравствуйте, начало в 19:00, 20 сентября")
    assert "Напишите Ваш номер" in vk_follow
    assert "Шоу в 19:00" in vk_body

    vk_follow_today = apply_mailing_show_fields(ensure_best_vk_once(vk["followup_html"]), thursday)
    assert "успеваете" in vk_follow_today
    assert "Если да, напишите" in vk_follow_today
    assert "17 сентября" not in vk_follow_today

    tg = {item["key"]: item for item in MAILING_TEMPLATE_SPECS}["best_tg"]
    tg_body = apply_mailing_show_fields(tg["body_html"], sunday)
    assert "на концерт 20 сентября" not in tg_body
    assert "на концерт первым" in tg_body
    assert "Шоу 20 сентября в 19:00" in tg_body
    print("mailing BEST show fill: OK")


def test_mailing_vk_denied_and_defaults():
    from bot.db.mailing import (
        MAIL_FOLLOWUP_DEDUPE_SEC,
        MAILING_TEST_USER_IDS,
        mailing_error_is_vk_denied,
    )

    assert mailing_error_is_vk_denied(
        "messages.send: Can't send messages for users without permission"
    )
    assert mailing_error_is_vk_denied("VKAPIError 901")
    assert not mailing_error_is_vk_denied("messages.send: Contact not found")
    assert not mailing_error_is_vk_denied("")
    assert not mailing_error_is_vk_denied(None)
    assert MAILING_TEST_USER_IDS["telegram"] == 243
    assert MAILING_TEST_USER_IDS["vkontakte"] == 20622
    assert MAIL_FOLLOWUP_DEDUPE_SEC == 900.0
    print("mailing vk denied and defaults: OK")


def test_mailing_followup_expires_after_show_start():
    from datetime import date, datetime, time

    from bot.db.mailing import (
        MAIL_FLOW_RESIDENTS_DATES,
        campaign_followup_expired,
        parse_mailing_show_clock,
    )
    from bot.utils.ticket import MSK

    assert parse_mailing_show_clock("Здравствуйте, начало в 20:00, успеваете?") == time(20, 0)
    assert parse_mailing_show_clock("<b>Шоу в 20:00 в Эскобаре на м.Площадь Ильича") == time(20, 0)
    assert parse_mailing_show_clock("начало сегодня в 20:30, успеваете?") == time(20, 30)
    assert parse_mailing_show_clock("СЕГОДНЯ, <b>01 августа в 20:30</b> пройдёт") == time(20, 30)
    assert parse_mailing_show_clock("без времени") is None

    campaign = {
        "followup_until": date(2026, 10, 5),
        "followup_html": "Здравствуйте, начало в 20:00, успеваете? 😊",
        "body_html": "<b>Шоу в 20:00 в Эскобаре на м.Площадь Ильича ☝️</b>",
        "filters": {},
    }
    before = datetime(2026, 10, 5, 17, 15, tzinfo=MSK)
    at_start = datetime(2026, 10, 5, 20, 0, tzinfo=MSK)
    after = datetime(2026, 10, 5, 22, 5, tzinfo=MSK)
    next_day = datetime(2026, 10, 6, 0, 1, tzinfo=MSK)
    assert not campaign_followup_expired(campaign, now=before)
    assert not campaign_followup_expired(campaign, now=at_start)
    assert campaign_followup_expired(campaign, now=after)
    assert campaign_followup_expired(campaign, now=next_day)

    no_clock = {
        "followup_until": date(2026, 10, 5),
        "followup_html": "Напишите имя и телефон",
        "body_html": "",
        "filters": {},
    }
    assert not campaign_followup_expired(no_clock, now=after)
    assert campaign_followup_expired(no_clock, now=next_day)

    dates_flow = {
        "followup_until": date(2026, 10, 7),
        "followup_html": MAIL_FLOW_RESIDENTS_DATES,
        "body_html": "",
        "filters": {},
    }
    week_later = datetime(2026, 10, 14, 12, 0, tzinfo=MSK)
    assert not campaign_followup_expired(
        dates_flow,
        now=week_later,
        residents_shows=[{"date_iso": "2026-10-14"}, {"date_iso": "2026-11-11"}],
    )
    assert campaign_followup_expired(dates_flow, now=week_later, residents_shows=[])
    print("mailing followup expires after show start: OK")


def test_db_schema():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        path = tmp.name
    conn = sqlite3.connect(path)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            username TEXT,
            name TEXT,
            phone TEXT,
            event_date TEXT,
            event_time TEXT,
            event_address TEXT,
            event_location TEXT,
            guests INTEGER,
            status TEXT DEFAULT 'booked',
            created_at TEXT,
            reminder_24h_sent INTEGER DEFAULT 0,
            reminder_day_sent INTEGER DEFAULT 0,
            annulled_at TEXT
        )
        """
    )
    c.execute(
        """
        INSERT INTO bookings (telegram_id, username, name, phone, event_date, event_time,
            event_address, event_location, guests, status, created_at)
        VALUES (1, 'u', 'Иван', '+7999', '04.07.2026', '19:30', 'адрес', 'Escobar', 2, 'booked', '2026-06-29')
        """
    )
    row = c.execute("SELECT * FROM bookings WHERE id=1").fetchone()
    conn.close()
    # indices: 5=date, 6=time, 7=address, 8=location, 9=guests
    assert row[5] == "04.07.2026"
    assert row[6] == "19:30"
    assert row[8] == "Escobar"
    assert row[9] == 2
    print("db schema indices: OK")


def test_vk_message_item_has_photo():
    from bot.vk.client import VKClient

    assert VKClient.message_item_has_photo(
        {"attachments": [{"type": "photo", "photo": {"id": 1, "owner_id": -123}}]}
    )
    assert not VKClient.message_item_has_photo({"attachments": []})
    assert not VKClient.message_item_has_photo({"attachments": [{"type": "doc", "doc": {"id": 1}}]})
    assert not VKClient.message_item_has_photo(None)
    print("vk message photo check: OK")


def test_residents_format():
    from bot.db.events_admin import AFISHA_FORMATS, AFISHA_FORMAT_LABELS
    from bot.handlers.formats import FREE_FORMATS_TEXT, FORMATS_TEXT
    from bot.utils.show_formats import (
        BOOKING_FORMAT_CHECK,
        EVENT_FORMAT_CHECK,
        MANAGER_STATA_FORMATS,
        MY_BOOKINGS_FORMATS,
        RESIDENTS,
        RESIDENTS_LABEL,
    )

    assert RESIDENTS == "residents"
    assert RESIDENTS in AFISHA_FORMATS
    assert AFISHA_FORMAT_LABELS[RESIDENTS] == "Резиденты"
    assert RESIDENTS in EVENT_FORMAT_CHECK
    assert RESIDENTS in BOOKING_FORMAT_CHECK
    assert RESIDENTS in MANAGER_STATA_FORMATS
    assert RESIDENTS in MY_BOOKINGS_FORMATS
    assert RESIDENTS_LABEL == "StandUp Сольники от резидентов"
    assert "StandUp Сольники от резидентов" in FORMATS_TEXT
    assert "StandUp Проверка материала" in FREE_FORMATS_TEXT
    assert "StandUp Сольники от резидентов" in FREE_FORMATS_TEXT
    print("residents format: OK")


def test_vk_residents_deeplink_back_button():
    import json
    from pathlib import Path

    from bot.vk.app import _dates_keyboard, _keyboard_has_menu_labeled_book

    deep = json.loads(
        _dates_keyboard(
            ["07.10.2026", "14.10.2026"],
            "residents_date",
            0,
            "residents_home",
            back_label="В главное меню",
            payload_extra={"rdl": 1},
        )
    )
    deep_flat = [btn for row in deep["buttons"] for btn in row]
    deep_back = deep_flat[-1]
    assert deep_back["action"]["label"] == "В главное меню"
    assert json.loads(deep_back["action"]["payload"]) == {"cmd": "residents_home", "rdl": 1}
    assert json.loads(deep_flat[0]["action"]["payload"]).get("rdl") == 1

    inner = json.loads(
        _dates_keyboard(
            ["07.10.2026", "14.10.2026"],
            "residents_date",
            0,
            "book",
            back_label="◀️ Назад",
        )
    )
    inner_flat = [btn for row in inner["buttons"] for btn in row]
    inner_back = inner_flat[-1]
    assert inner_back["action"]["label"] == "◀️ Назад"
    assert json.loads(inner_back["action"]["payload"]) == {"cmd": "book"}
    assert "rdl" not in json.loads(inner_flat[0]["action"]["payload"])
    assert not _keyboard_has_menu_labeled_book(inner)

    mailing = json.loads(
        _dates_keyboard(
            ["07.10.2026", "14.10.2026", "21.10.2026", "28.10.2026"],
            "residents_date",
            0,
            "residents_home",
            back_label="В главное меню",
            payload_extra={"rml": 1},
        )
    )
    mailing_flat = [btn for row in mailing["buttons"] for btn in row]
    assert json.loads(mailing_flat[0]["action"]["payload"]).get("rml") == 1
    assert mailing_flat[-1]["action"]["label"] == "В главное меню"
    assert json.loads(mailing_flat[-1]["action"]["payload"]) == {
        "cmd": "residents_home",
        "rml": 1,
    }

    legacy = json.loads(
        _dates_keyboard(
            ["07.10.2026", "14.10.2026"],
            "residents_date",
            0,
            "book",
        )
    )
    assert _keyboard_has_menu_labeled_book(legacy)

    src = Path("bot/vk/app.py").read_text(encoding="utf-8")
    send_menu = src[src.find("async def send_menu") : src.find("async def _leave_offline_gift_to_menu")]
    assert "force_new=True" in send_menu
    assert "residents_home" in src
    assert 'if cmd in {"residents_home"}' in src or "cmd in {\"residents_home\"}" in src
    print("vk residents deeplink back button: OK")


def test_vk_main_menu_keeps_ticket_message():
    from pathlib import Path

    from bot.vk.app import clicked_ref_is_protected, filter_deletable_message_ids

    assert clicked_ref_is_protected({161528}, cmid=161528)
    assert clicked_ref_is_protected({161528}, cmid=99, message={"id": 161528})
    assert clicked_ref_is_protected({161528}, cmid=99, message={"conversation_message_id": 161528})
    assert not clicked_ref_is_protected({161528}, cmid=99, message={"id": 1})
    assert not clicked_ref_is_protected(set(), cmid=161528)

    assert filter_deletable_message_ids([10, 161528, 11, 161528], {161528}) == [10, 11]
    assert filter_deletable_message_ids([161528], {161528, 161527}) == []
    assert filter_deletable_message_ids([5], set()) == [5]

    from bot.vk.app import message_is_issued_ticket
    import json

    ticket_kb = json.dumps(
        {
            "buttons": [
                [
                    {
                        "action": {
                            "type": "text",
                            "label": "Отменить бронь",
                            "payload": json.dumps({"cmd": "mb_cancel_confirm", "booking_id": 1}),
                        }
                    }
                ],
                [
                    {
                        "action": {
                            "type": "text",
                            "label": "Что, если я хочу прийти не один?",
                            "payload": json.dumps({"cmd": "rz_not_alone"}),
                        }
                    }
                ],
            ]
        }
    )
    assert message_is_issued_ticket({"keyboard": ticket_kb, "id": 9, "conversation_message_id": 3})
    dates_kb = json.dumps(
        {
            "buttons": [
                [
                    {
                        "action": {
                            "type": "text",
                            "label": "5 октября",
                            "payload": json.dumps({"cmd": "rz_date", "date": "05.10.2026"}),
                        }
                    }
                ]
            ]
        }
    )
    assert not message_is_issued_ticket({"keyboard": dates_kb})
    assert not message_is_issued_ticket(None)

    src = Path("bot/vk/app.py").read_text(encoding="utf-8")
    assert "message_is_issued_ticket" in src
    assert "_keyboard_is_main_menu" in src
    menu_fn = src.find("async def send_menu")
    menu_chunk = src[menu_fn : src.find("async def _leave_offline_gift")]
    assert "replace_nav=False" in menu_chunk
    assert "replace_nav=True" not in menu_chunk
    send_fn = src.find("async def _send_text")
    send_chunk = src[send_fn : src.find("def _remember_dates_attachment")]
    assert "if _keyboard_is_main_menu(keyboard):" in send_chunk
    assert "replace_nav = False" in send_chunk
    assert src.count("delete_ticket_message") == 2
    assert "if ticket_mid:" in src
    booking_src = Path("bot/handlers/booking.py").read_text(encoding="utf-8")
    assert booking_src.count("await _delete_ticket(") == 2
    raffle_src = Path("bot/handlers/rozygrysh.py").read_text(encoding="utf-8")
    assert "delete_ticket=True" in raffle_src
    start_src = Path("bot/handlers/start.py").read_text(encoding="utf-8")
    mm = start_src.find('F.data == "main_menu"')
    mm_chunk = start_src[mm : mm + 450]
    assert "_delete_previous_menu_message" not in mm_chunk
    print("vk/tg keep ticket except cancel/date change: OK")


def test_tg_keeps_ticket_message():
    from pathlib import Path

    from bot.utils import nav_messages as nm

    nm._EXTRA_TICKET_MSG_BY_BOOKING.clear()
    nm._EXTRA_TICKET_MSG_BY_CHAT.clear()
    nm.remember_issued_ticket_message(1001, 702, 555)
    assert nm.is_protected_ticket_message(1001, 555)
    extras = nm.pop_extra_ticket_messages(702, chat_id=1001)
    assert extras == {555}
    assert 555 not in nm._EXTRA_TICKET_MSG_BY_CHAT.get(1001, set())

    start_src = Path("bot/handlers/start.py").read_text(encoding="utf-8")
    assert "remember_issued_ticket_message" in start_src
    assert "is_protected_ticket_message(call.from_user.id, old_id)" in start_src
    booking_src = Path("bot/handlers/booking.py").read_text(encoding="utf-8")
    assert "pop_extra_ticket_messages" in booking_src
    assert "remember_issued_ticket_message" in booking_src
    raffle_src = Path("bot/handlers/rozygrysh.py").read_text(encoding="utf-8")
    assert "is_protected_ticket_message(telegram_id, mid)" in raffle_src
    formats_src = Path("bot/handlers/formats.py").read_text(encoding="utf-8")
    assert "is_protected_ticket_message" in formats_src
    print("tg keep ticket except cancel/date change: OK")


def test_vk_platka_flow():
    from pathlib import Path

    from bot.admin.vk_entry import FLOWS, MINI_APP_DIRECT_FLOWS

    assert "platka" in FLOWS
    assert FLOWS["platka"]["ref"] == "standup_platka"
    assert "platka" in MINI_APP_DIRECT_FLOWS

    entry_src = Path("bot/admin/vk_entry.py").read_text(encoding="utf-8")
    assert 'if flow_key == "platka"' in entry_src
    assert "landing_platka" in entry_src
    assert 'platka: "platka"' in entry_src
    assert "platka_entry_link" in Path("bot/vk/app.py").read_text(encoding="utf-8")
    assert 'add_get("/vk/platka"' in entry_src

    app_src = Path("bot/vk/app.py").read_text(encoding="utf-8")
    assert "_is_platka_ref" in app_src
    assert 'cmd == "platka"' in app_src
    assert "paid_formats_keyboard()" in app_src
    print("vk platka flow: OK")


def test_vk_raffle_vs_show_gift_routing():
    from datetime import datetime
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from bot.vk.app import in_evening_offline_gift_window

    msk = ZoneInfo("Europe/Moscow")
    assert in_evening_offline_gift_window(datetime(2026, 10, 5, 19, 0, tzinfo=msk))
    assert in_evening_offline_gift_window(datetime(2026, 10, 5, 21, 59, tzinfo=msk))
    assert not in_evening_offline_gift_window(datetime(2026, 10, 5, 22, 0, tzinfo=msk))
    assert not in_evening_offline_gift_window(datetime(2026, 10, 5, 18, 59, tzinfo=msk))

    app_src = Path("bot/vk/app.py").read_text(encoding="utf-8")
    assert '"розыгрыш": "raffle"' in app_src
    assert '"участвовать в розыгрыше": "raffle"' in app_src
    assert '"розыгрыш": "offline_gift"' not in app_src
    assert "Evening window: raffle start → offline gift" not in app_src
    assert "Ignore offline_gift deeplink outside" not in app_src
    assert 'text_key == "подарок" and in_evening_offline_gift_window()' in app_src
    assert "is_start_text and in_evening_offline_gift_window()" in app_src

    entry_src = Path("bot/admin/vk_entry.py").read_text(encoding="utf-8")
    assert "mini-app raffle → offline gift" not in entry_src
    assert "flow_key == \"raffle\" and in_evening_offline_gift_window()" not in entry_src
    assert "giftSession && flow === \"raffle\"" in entry_src
    assert "_remember_mini_flow(request, \"offline_gift\")" in entry_src

    from bot.admin.vk_entry import remap_raffle_button_in_gift_session

    assert remap_raffle_button_in_gift_session("raffle", "#flow=offline_gift") == "offline_gift"
    assert remap_raffle_button_in_gift_session("raffle", "offline_gift") == "offline_gift"
    assert remap_raffle_button_in_gift_session("raffle", "#flow=raffle") == "raffle"
    assert remap_raffle_button_in_gift_session("booking", "#flow=offline_gift") == "booking"
    print("vk raffle vs show gift routing: OK")


def test_vk_raffle_screenshot_avoids_tg_handler_import():
    from pathlib import Path

    app_src = Path("bot/vk/app.py").read_text(encoding="utf-8")
    assert "from bot.handlers.rozygrysh import" not in app_src
    assert "from bot.services.raffle_moderation import send_to_moderation" in app_src
    assert "Не удалось отправить скрин. Пришли фото ещё раз одним фото" in app_src

    mod_src = Path("bot/services/raffle_moderation.py").read_text(encoding="utf-8")
    assert "from bot.utils.nav_messages" not in mod_src
    assert "import nav_messages" not in mod_src
    assert "from bot.handlers.rozygrysh" not in mod_src
    assert "async def send_to_moderation" in mod_src

    raffle_src = Path("bot/handlers/rozygrysh.py").read_text(encoding="utf-8")
    assert "from bot.services.raffle_moderation import send_to_moderation" in raffle_src
    print("vk raffle screenshot avoids tg handler import: OK")


def test_vk_same_day_warning_import():
    from pathlib import Path

    src = Path("bot/vk/app.py").read_text(encoding="utf-8")
    assert "from bot.utils.booking_texts import same_day_booking_warning" in src
    from bot.utils.booking_texts import same_day_booking_warning

    assert callable(same_day_booking_warning)
    print("vk same-day warning import: OK")


def test_vk_dispatch_ensures_user_name():
    from pathlib import Path

    src = Path("bot/vk/app.py").read_text(encoding="utf-8")
    start = src.find("async def _dispatch_message")
    chunk = src[start : start + 400]
    assert "await self._ensure_user(vk_id)" in chunk
    print("vk dispatch ensures user name: OK")


async def main():
    test_callback_parsing()
    test_mailing_booking_button_fields()
    test_mailing_schedule_and_templates()
    test_mailing_best_show_fill()
    test_mailing_vk_denied_and_defaults()
    test_mailing_followup_expires_after_show_start()
    test_db_schema()
    test_vk_message_item_has_photo()
    test_residents_format()
    test_vk_residents_deeplink_back_button()
    test_vk_main_menu_keeps_ticket_message()
    test_tg_keeps_ticket_message()
    test_vk_platka_flow()
    test_vk_raffle_vs_show_gift_routing()
    test_vk_raffle_screenshot_avoids_tg_handler_import()
    test_vk_same_day_warning_import()
    test_vk_dispatch_ensures_user_name()
    events = await load_events()
    print(f"events loaded: {len(events)}")
    if not events:
        print("WARNING: no aktual events — check CSV URL or status column encoding")
        return
    for e in events[:3]:
        print(f"  {e['date']} {e['time']} @ {e['location']} (max {e['max_seats']})")
    print("all checks passed")


if __name__ == "__main__":
    asyncio.run(main())
