"""Mailing campaigns: schema, audience filters, queue CRUD."""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from bot.config import BOOKINGS_SOURCE, DATABASE_URL
from bot.utils.ticket import MSK, now_msk

logger = logging.getLogger(__name__)

BOOKING_STATUSES = ("booked", "confirmed", "cancelled", "annulled")
CHANNELS = ("telegram", "vkontakte", "both")
CAMPAIGN_STATUSES = ("draft", "queued", "running", "paused", "done", "cancelled")
# Не слать служебные аккаунты в массовых рассылках.
MAIL_SKIP_USERNAMES = ("nastya_stand_up", "ccoverr")

MAILING_TEMPLATE_SPECS = (
    {
        "key": "best_tg",
        "title": "BEST · Telegram",
        "channel": "telegram",
        "button_text": "",
        "followup_html": "",
        "uses_show": True,
        "body_html": (
            "Привет! 😊 Дарим 15 бесплатных билетов на {концерт} первым пятнадцати, "
            "написавшим в личку "
            '<a href="https://t.me/ccoverr?text=%D0%A5%D0%BE%D1%87%D1%83%20%D0%B1%D0%B8%D0%BB%D0%B5%D1%82">'
            "<b>@ccoverr</b></a> \"хочу билет\" !!!\n"
            "\n"
            "<b>Шоу {когда} в {время} в {площадка} ☝️</b>\n"
            "\n"
            "Пиши прямо сейчас, не откладывай 😊\n"
            "Посмотреть, кто сегодня выступает, можно в нашем канале 😊\n"
            "https://t.me/MoscowStandupShow"
        ),
    },
    {
        "key": "best_vk",
        "title": "BEST · VK",
        "channel": "vkontakte",
        "button_text": "хочу билет",
        "uses_show": True,
        "body_html": (
            "Привет! 😊 Дарим 15 бесплатных билетов на {концерт} первым пятнадцати, "
            "кто нажмет кнопку \"хочу билет\"!!!\n"
            "\n"
            "<b>Шоу {когда} в {время} в {площадка} ☝️</b>\n"
            "\n"
            "Нажимай кнопку прямо сейчас, не откладывай 😊\n"
            "Посмотреть, кто сегодня выступает, можно в нашем сообществе 😊\n"
            "<b>https://vk.ru/moscowstandupshow</b>"
        ),
        "followup_html": (
            "Здравствуйте, начало {когда} в {время}, успеваете? 😊 \n"
            "\n"
            "Если да, напишите Ваш номер телефона, имя и один билет нужен или два 😊\n"
            "Я внесу в списки, администратору на входе назовёте имя, он посадит 😊\n"
            "\n"
            "📍 Адрес {адрес}\n"
            "<b>Посещение мероприятия предполагает обязательный заказ минимум одной позиции по меню 🍽 ☝️😊</b>"
        ),
    },
    {
        "key": "hitloto_tg",
        "title": "Музлото · Telegram",
        "channel": "telegram",
        "button_text": "",
        "followup_html": "",
        "body_html": (
            "<b>Бесплатные билеты на музыкальное лото с комиком от Moscow StandUp Show!!</b>\n"
            "\n"
            "Привет!! СЕГОДНЯ, <b>01 августа в 20:30</b> пройдёт 🔥Хитлото by Moscow StandUp Show 🔥\n"
            "\n"
            "📍Адрес - TEMPLE (м.Курская) Нижний Сусальный переулок, дом 5, стр. 4а\n"
            "\n"
            "В программе розыгрыш билетов на стендап, сертификаты на массаж и квизы, "
            "подарки от заведения, и лучшие хиты всех времен!!! 🎼\n"
            "\n"
            "Вход бесплатный, но как и всегда, обязательное условие- заказ минимум одной "
            "позиции по меню заведения ☝️\n"
            "\n"
            "Чтобы попасть на мероприятие пиши в личку нашему менеджеру "
            '<a href="https://t.me/ccoverr?text=%D0%A5%D0%BE%D1%87%D1%83%20%D0%B1%D0%B8%D0%BB%D0%B5%D1%82">'
            "<b>@ccoverr</b></a> \"хочу билет\" !!!"
        ),
    },
    {
        "key": "hitloto_vk",
        "title": "Музлото · VK",
        "channel": "vkontakte",
        "button_text": "хочу билет",
        "body_html": (
            "<b>Бесплатные билеты на музыкальное лото с комиком от Moscow StandUp Show!!</b>\n"
            "\n"
            "Привет!! СЕГОДНЯ, <b>01 августа в 20:30</b> пройдёт 🔥Хитлото by Moscow StandUp Show 🔥\n"
            "\n"
            "📍Адрес - TEMPLE (м.Курская) Нижний Сусальный переулок, дом 5, стр. 4а\n"
            "\n"
            "В программе розыгрыш билетов на стендап, сертификаты на массаж и квизы, "
            "подарки от заведения, и лучшие хиты всех времен!!! 🎼\n"
            "\n"
            "Вход бесплатный, но как и всегда, обязательное условие- заказ минимум одной "
            "позиции по меню заведения ☝️\n"
            "\n"
            "Чтобы попасть на мероприятие <b>нажми кнопку \"хочу билет\":</b>"
        ),
        "followup_html": (
            "Здравствуйте, начало сегодня в 20:30, успеваете? 😊 \n"
            "\n"
            "Если да, напишите Ваш номер телефона, имя и один билет нужен или два 😊\n"
            "Я внесу в списки, администратору на входе назовёте имя, он посадит 😊\n"
            "\n"
            "📍 Адрес TEMPLE (м.Курская) Нижний Сусальный переулок, дом 5, стр. 4а\n"
            "<b>Посещение мероприятия предполагает обязательный заказ минимум одной позиции по меню 🍽 ☝️😊</b>"
        ),
    },
)
MAILING_TEMPLATE_KEYS = tuple(item["key"] for item in MAILING_TEMPLATE_SPECS)
MAILING_SHOW_PLACEHOLDERS = ("{концерт}", "{когда}", "{время}", "{площадка}", "{адрес}")
_WEEKDAY_SHORT = {
    "понедельник": "пн",
    "вторник": "вт",
    "среда": "ср",
    "четверг": "чт",
    "пятница": "пт",
    "суббота": "сб",
    "воскресенье": "вс",
    "monday": "пн",
    "tuesday": "вт",
    "wednesday": "ср",
    "thursday": "чт",
    "friday": "пт",
    "saturday": "сб",
    "sunday": "вс",
}
_MONTHS_GENITIVE = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


def mailing_date_phrase(event_date: date | None, date_display: str = "") -> str:
    d = event_date
    if d is None:
        raw = (date_display or "").strip()
        try:
            d = datetime.strptime(raw, "%d.%m.%Y").date()
        except ValueError:
            return raw
    month = _MONTHS_GENITIVE.get(d.month)
    if not month:
        return date_display or d.isoformat()
    return f"{d.day} {month}"


def mailing_place_phrase(location: str = "", address: str = "") -> str:
    blob = f"{location or ''} {address or ''}".casefold()
    if "escobar" in blob:
        return "Эскобаре на м.Площадь Ильича"
    if "temple" in blob:
        return "Temple Bar на м.Курская"
    loc = (location or "").strip()
    if loc:
        return loc
    addr = (address or "").strip()
    return addr.split(",")[0].strip() if addr else "площадке"


def mailing_show_fields(event: dict, *, today: date | None = None) -> dict[str, str]:
    """Подстановки для шаблона BEST по записи афиши."""
    today = today or now_msk().date()
    date_iso = str(event.get("date_iso") or "").strip()
    event_date = None
    if date_iso:
        try:
            event_date = date.fromisoformat(date_iso)
        except ValueError:
            event_date = None
    is_today = event_date == today
    when = "сегодня" if is_today else mailing_date_phrase(event_date, event.get("date_display") or "")
    concert = "СЕГОДНЯШНИЙ концерт" if is_today else f"концерт {when}"
    time = str(event.get("time") or "").strip() or "20:00"
    place = mailing_place_phrase(event.get("location") or "", event.get("address") or "")
    address = str(event.get("address") or "").strip() or place
    return {
        "{концерт}": concert,
        "{когда}": when,
        "{время}": time,
        "{площадка}": place,
        "{адрес}": address,
    }


def apply_mailing_show_fields(text: str | None, fields: dict[str, str] | None) -> str:
    out = text or ""
    if not fields:
        return out
    for key, value in fields.items():
        out = out.replace(key, str(value or ""))
    return out


def ensure_best_placeholders(text: str | None) -> str:
    """Старые BEST-тексты с Эскобаром/20:00 переводим на плейсхолдеры."""
    out = text or ""
    if "{концерт}" not in out:
        out = out.replace("на СЕГОДНЯШНИЙ концерт", "на {концерт}")
    if "{площадка}" not in out:
        out = re.sub(
            r"Шоу сегодня в \d{1,2}:\d{2} в [^<\n]+",
            "Шоу {когда} в {время} в {площадка}",
            out,
            count=1,
        )
    if "начало {когда}" not in out:
        out = re.sub(
            r"начало сегодня в \d{1,2}:\d{2}",
            "начало {когда} в {время}",
            out,
            count=1,
        )
    if "{адрес}" not in out:
        out = re.sub(r"📍 Адрес [^\n<]+", "📍 Адрес {адрес}", out, count=1)
    return out


def _weekday_short(value: str) -> str:
    return _WEEKDAY_SHORT.get((value or "").strip().casefold(), "")


def list_mailing_best_shows() -> list[dict]:
    """Ближайшие активные BEST из афиши — для выбора шоу в письме."""
    try:
        from bot.db.events_admin import list_events_for_admin

        events = list_events_for_admin("best").get("active") or []
    except Exception:
        logger.exception("list_mailing_best_shows failed")
        return []
    today = now_msk().date()
    out: list[dict] = []
    for ev in events:
        event_id = str(ev.get("id") or "").strip()
        if not event_id:
            continue
        fields = mailing_show_fields(ev, today=today)
        date_iso = str(ev.get("date_iso") or "")
        is_today = date_iso == today.isoformat()
        loc = str(ev.get("location") or "").strip()
        time = str(ev.get("time") or "").strip()
        date_display = str(ev.get("date_display") or "").strip()
        label_date = date_display[:5] if len(date_display) >= 5 else date_display
        wd_short = _weekday_short(str(ev.get("weekday") or ""))
        date_bit = f"{label_date} ({wd_short})" if wd_short else label_date
        label = " · ".join(part for part in (date_bit, loc, time) if part)
        out.append(
            {
                "id": event_id,
                "label": label,
                "is_today": is_today,
                "title": f"BEST · {fields['{когда}']}",
                "subs": fields,
            }
        )
    return out


# Отбивка, если нажали кнопку после даты актуальности рассылки.
FOLLOWUP_EXPIRED_TEXT = (
    "Здравствуйте! Это предложение уже неактуально — мероприятие прошло 😊\n"
    "Следите за новостями в нашем сообществе: там появляются свежие анонсы и розыгрыши ☝️"
)
# Старый маркер кнопки сольника 15.09. Новые кампании его не ставят;
# клик по старым кнопкам отдаёт FOLLOWUP_EXPIRED_TEXT.
MAIL_FLOW_BOOKING_RESIDENT = "__flow:booking_resident__"


def followup_is_booking_flow(text: str | None) -> bool:
    return (text or "").strip() == MAIL_FLOW_BOOKING_RESIDENT


def followup_preview_label(text: str | None) -> str:
    raw = (text or "").strip()
    if followup_is_booking_flow(raw):
        return "Сценарий брони (шоу уже прошло)"
    return raw


def form_starts_booking(value: str | None) -> bool:
    return (value or "").strip().casefold() in {
        "1",
        "on",
        "true",
        "yes",
        "booking_resident",
    }


def resolve_mailing_button_fields(
    *,
    starts_booking: bool,
    button_url: str | None,
    followup_html: str | None,
    followup_until: str | None = None,
) -> tuple[str, str, str | None]:
    """URL / follow-up / дата кнопки. Для брони URL очищаем, follow-up — служебный маркер."""
    until = (followup_until or "").strip() or None
    if not starts_booking:
        return (button_url or "").strip(), (followup_html or "").strip(), until
    if not until:
        until = "2026-09-15"
    return "", MAIL_FLOW_BOOKING_RESIDENT, until
# Повтор того же текста после кнопки рассылки — не чаще чем раз в 30 мин.
MAIL_FOLLOWUP_DEDUPE_SEC = 1800.0


def claim_mail_followup_send(*, user_id: int, text: str) -> bool:
    """True — можно отправить; False — такой же текст уже уходил этому user_id < 30 мин."""
    if not user_id or not (text or "").strip():
        return True
    import hashlib

    from bot.vk.entry_dedupe import claim_flow_send

    digest = hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:20]
    return claim_flow_send(
        int(user_id),
        f"mail_fu:{digest}",
        ttl_sec=MAIL_FOLLOWUP_DEDUPE_SEC,
    )


def _use_postgres() -> bool:
    return BOOKINGS_SOURCE == "postgres" and bool(DATABASE_URL)


def ensure_mailing_tables() -> None:
    if not _use_postgres():
        return
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mailing_campaigns (
                        id BIGSERIAL PRIMARY KEY,
                        title TEXT NOT NULL DEFAULT '',
                        channel TEXT NOT NULL
                            CHECK (channel IN ('telegram', 'vkontakte', 'both')),
                        status TEXT NOT NULL DEFAULT 'draft'
                            CHECK (status IN (
                                'draft', 'queued', 'running', 'paused', 'done', 'cancelled'
                            )),
                        body_html TEXT NOT NULL DEFAULT '',
                        photo_path TEXT,
                        button_text TEXT,
                        button_url TEXT,
                        followup_html TEXT,
                        interval_sec NUMERIC(8, 3) NOT NULL DEFAULT 0.100,
                        batch_limit INTEGER,
                        filters JSONB NOT NULL DEFAULT '{}'::jsonb,
                        total_count INTEGER NOT NULL DEFAULT 0,
                        sent_count INTEGER NOT NULL DEFAULT 0,
                        failed_count INTEGER NOT NULL DEFAULT 0,
                        skipped_count INTEGER NOT NULL DEFAULT 0,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        started_at TIMESTAMPTZ,
                        finished_at TIMESTAMPTZ,
                        created_by TEXT NOT NULL DEFAULT 'owner'
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mailing_recipients (
                        id BIGSERIAL PRIMARY KEY,
                        campaign_id BIGINT NOT NULL
                            REFERENCES mailing_campaigns(id) ON DELETE CASCADE,
                        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        channel TEXT NOT NULL CHECK (channel IN ('telegram', 'vkontakte')),
                        peer_id BIGINT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'sent', 'failed', 'skipped')),
                        error TEXT,
                        sent_at TIMESTAMPTZ,
                        UNIQUE (campaign_id, channel, peer_id)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_mailing_recipients_pending
                    ON mailing_recipients (campaign_id, id)
                    WHERE status = 'pending'
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_mailing_recipients_user_sent
                    ON mailing_recipients (user_id, channel, sent_at)
                    WHERE status = 'sent'
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_mailing_campaigns_status
                    ON mailing_campaigns (status, id)
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS is_blocked BOOLEAN NOT NULL DEFAULT false
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE mailing_campaigns
                    ADD COLUMN IF NOT EXISTS disable_link_preview BOOLEAN NOT NULL DEFAULT false
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE mailing_campaigns
                    ADD COLUMN IF NOT EXISTS followup_until DATE
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE mailing_campaigns
                    ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMPTZ
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_mailing_campaigns_queued_at
                    ON mailing_campaigns (scheduled_at, id)
                    WHERE status = 'queued'
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mailing_templates (
                        key TEXT PRIMARY KEY,
                        title TEXT NOT NULL DEFAULT '',
                        channel TEXT NOT NULL
                            CHECK (channel IN ('telegram', 'vkontakte', 'both')),
                        body_html TEXT NOT NULL DEFAULT '',
                        button_text TEXT NOT NULL DEFAULT '',
                        followup_html TEXT NOT NULL DEFAULT '',
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                for spec in MAILING_TEMPLATE_SPECS:
                    cur.execute(
                        """
                        INSERT INTO mailing_templates (
                            key, title, channel, body_html, button_text, followup_html
                        )
                        VALUES (
                            %(key)s, %(title)s, %(channel)s,
                            %(body_html)s, %(button_text)s, %(followup_html)s
                        )
                        ON CONFLICT (key) DO UPDATE
                        SET title = EXCLUDED.title,
                            channel = EXCLUDED.channel,
                            body_html = CASE
                                WHEN BTRIM(mailing_templates.body_html) = ''
                                THEN EXCLUDED.body_html
                                ELSE mailing_templates.body_html
                            END,
                            button_text = CASE
                                WHEN BTRIM(mailing_templates.button_text) = ''
                                THEN EXCLUDED.button_text
                                ELSE mailing_templates.button_text
                            END,
                            followup_html = CASE
                                WHEN BTRIM(mailing_templates.followup_html) = ''
                                THEN EXCLUDED.followup_html
                                ELSE mailing_templates.followup_html
                            END
                        """,
                        {
                            "key": spec["key"],
                            "title": spec["title"],
                            "channel": spec["channel"],
                            "body_html": spec.get("body_html") or "",
                            "button_text": spec.get("button_text") or "",
                            "followup_html": spec.get("followup_html") or "",
                        },
                    )
            conn.commit()
    except Exception:
        logger.exception("ensure_mailing_tables failed")


def _parse_iso_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_admin_datetime(value: Any) -> datetime | None:
    """Дата-время из админки (datetime-local) как московское время."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            return dt.replace(tzinfo=MSK)
        return dt.astimezone(MSK)
    text = str(value).strip().replace(" ", "T")
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=MSK)
        except ValueError:
            continue
    return None


def format_admin_datetime(value: Any) -> str:
    dt = parse_admin_datetime(value)
    if not dt:
        return ""
    return dt.strftime("%d.%m.%Y %H:%M")


def to_datetime_local_value(value: Any) -> str:
    dt = parse_admin_datetime(value)
    if not dt:
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M")


def is_campaign_scheduled(row: dict | None) -> bool:
    if not row or (row.get("status") or "") != "queued":
        return False
    at = parse_admin_datetime(row.get("scheduled_at"))
    return bool(at and at > now_msk())


def campaign_followup_until(campaign: dict | None) -> date | None:
    """Дата, до которой (включительно) кнопка follow-up актуальна."""
    if not campaign:
        return None
    until = _parse_iso_date(campaign.get("followup_until"))
    if until:
        return until
    filters = campaign.get("filters") or {}
    if isinstance(filters, str):
        try:
            filters = json.loads(filters)
        except Exception:
            filters = {}
    if not isinstance(filters, dict):
        return None
    return _parse_iso_date(filters.get("date_to")) or _parse_iso_date(filters.get("date_from"))


def normalize_filters(raw: dict | None) -> dict[str, Any]:
    data = dict(raw or {})
    statuses = data.get("booking_statuses") or []
    if isinstance(statuses, str):
        statuses = [s.strip() for s in statuses.split(",") if s.strip()]
    # «active» = бронь или билет (частый смысл «активная бронь» у оператора).
    expanded: list[str] = []
    for status in statuses:
        if status == "active":
            expanded.extend(["booked", "confirmed"])
        elif status in BOOKING_STATUSES:
            expanded.append(status)
    statuses = list(dict.fromkeys(expanded))
    try:
        exclude_days = int(data.get("exclude_sent_days") or 0)
    except (TypeError, ValueError):
        exclude_days = 0
    exclude_days = max(0, min(exclude_days, 3650))
    try:
        batch_limit = data.get("batch_limit")
        batch_limit = int(batch_limit) if batch_limit not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        batch_limit = None
    if batch_limit is not None:
        batch_limit = max(1, min(batch_limit, 100_000))
    # Только дата шоу (event_date). Одна заполненная дата = конкретный день.
    date_from = (data.get("date_from") or data.get("booking_date_from") or "").strip() or None
    date_to = (data.get("date_to") or data.get("booking_date_to") or "").strip() or None
    if date_from and not date_to:
        date_to = date_from
    elif date_to and not date_from:
        date_from = date_to
    return {
        "booking_statuses": statuses,
        "date_mode": "event",
        "date_from": date_from,
        "date_to": date_to,
        "has_phone": bool(data.get("has_phone")),
        "exclude_blocked": bool(data.get("exclude_blocked", True)),
        "exclude_today_bookings": bool(data.get("exclude_today_bookings")),
        "exclude_sent_days": exclude_days,
        "batch_limit": batch_limit,
    }


def _status_ts_sql() -> str:
    """Timestamp of the booking status change (fallback: created_at)."""
    return """
        CASE b.status
            WHEN 'confirmed' THEN COALESCE(b.confirmed_at, b.created_at)
            WHEN 'cancelled' THEN COALESCE(b.cancelled_at, b.updated_at, b.created_at)
            WHEN 'annulled' THEN COALESCE(b.annulled_at, b.updated_at, b.created_at)
            ELSE b.created_at
        END
    """


def _audience_sql(channel: str, filters: dict) -> tuple[str, dict]:
    """Build SELECT user_id, channel, peer_id for one messenger channel."""
    if channel not in ("telegram", "vkontakte"):
        raise ValueError("bad channel")
    params: dict[str, Any] = {
        "msg_channel": channel,
        "skip_usernames": list(MAIL_SKIP_USERNAMES),
    }
    where = [
        "TRUE",
        "NOT (LOWER(BTRIM(COALESCE(u.username, ''))) = ANY(%(skip_usernames)s))",
    ]
    if channel == "telegram":
        where.append("u.telegram_id IS NOT NULL")
        peer_expr = "u.telegram_id"
        if filters.get("exclude_blocked"):
            where.append("COALESCE(u.is_blocked, false) = false")
    else:
        where.append("u.vk_id IS NOT NULL")
        peer_expr = "u.vk_id"

    if filters.get("has_phone"):
        where.append("NULLIF(TRIM(COALESCE(u.phone, '')), '') IS NOT NULL")

    statuses = list(filters.get("booking_statuses") or [])
    date_from = filters.get("date_from") or filters.get("booking_date_from")
    date_to = filters.get("date_to") or filters.get("booking_date_to")
    if statuses or date_from or date_to:
        booking_where = [
            "b.user_id = u.id",
            # Розыгрыш никогда не входит в аудиторию по статусу/дате брони.
            "LOWER(TRIM(COALESCE(b.format, ''))) <> 'rozygrysh'",
        ]
        if statuses:
            params["booking_statuses"] = list(statuses)
            booking_where.append("b.status = ANY(%(booking_statuses)s)")
        use_event_dates = bool(date_from or date_to)
        if date_from:
            params["date_from"] = str(date_from)
            booking_where.append("e.event_date >= %(date_from)s::date")
        if date_to:
            params["date_to"] = str(date_to)
            booking_where.append("e.event_date <= %(date_to)s::date")
        from_sql = (
            "bookings b JOIN events e ON e.id = b.event_id"
            if use_event_dates
            else "bookings b"
        )
        where.append(
            "EXISTS (SELECT 1 FROM " + from_sql + " WHERE " + " AND ".join(booking_where) + ")"
        )

    exclude_days = int(filters.get("exclude_sent_days") or 0)
    if exclude_days > 0:
        params["exclude_days"] = exclude_days
        where.append(
            """
            NOT EXISTS (
                SELECT 1
                FROM mailing_recipients mr
                WHERE mr.user_id = u.id
                  AND mr.channel = %(msg_channel)s
                  AND mr.status = 'sent'
                  AND mr.sent_at >= NOW() - (%(exclude_days)s || ' days')::interval
            )
            """
        )

    if filters.get("exclude_today_bookings"):
        params["today_msk"] = now_msk().date()
        where.append(
            """
            NOT EXISTS (
                SELECT 1
                FROM bookings b
                JOIN events e ON e.id = b.event_id
                WHERE b.user_id = u.id
                  AND b.status IN ('booked', 'confirmed')
                  AND e.event_date = %(today_msk)s::date
            )
            """
        )

    where_sql = " AND ".join(where)
    # channel — только whitelist-значение, в SELECT безопасно как литерал.
    sql = f"""
        SELECT u.id AS user_id, '{channel}'::text AS channel, {peer_expr} AS peer_id
        FROM users u
        WHERE {where_sql}
    """
    return sql, params


def preview_audience(channel: str, filters: dict | None) -> dict[str, Any]:
    """Count recipients per channel without inserting."""
    ensure_mailing_tables()
    filters = normalize_filters(filters)
    if channel not in CHANNELS:
        raise ValueError("bad channel")
    if not _use_postgres():
        raise RuntimeError(
            "Рассылка недоступна: нужен PostgreSQL "
            "(BOOKINGS_SOURCE=postgres и DATABASE_URL)."
        )

    counts = {"telegram": 0, "vkontakte": 0}
    db_totals = {"telegram": 0, "vkontakte": 0}
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE telegram_id IS NOT NULL)::int AS telegram,
                    COUNT(*) FILTER (WHERE vk_id IS NOT NULL)::int AS vkontakte
                FROM users
                """
            )
            row = cur.fetchone() or {}
            db_totals["telegram"] = int(row.get("telegram") or 0)
            db_totals["vkontakte"] = int(row.get("vkontakte") or 0)

            # Всегда оба канала в ответе — чтобы в UI не казалось, что VK «пустой»,
            # когда выбран только Telegram.
            for ch in ("telegram", "vkontakte"):
                sql, params = _audience_sql(ch, filters)
                cur.execute(f"SELECT COUNT(*) AS n FROM ({sql}) AS aud", params)
                counts[ch] = int(cur.fetchone()["n"] or 0)

    if channel == "both":
        total = counts["telegram"] + counts["vkontakte"]
    else:
        total = counts[channel]
    limit = filters.get("batch_limit")
    capped = min(total, limit) if limit else total
    return {
        "telegram": counts["telegram"],
        "vkontakte": counts["vkontakte"],
        "total": total,
        "capped_total": capped,
        "batch_limit": limit,
        "filters": filters,
        "db_totals": db_totals,
        "selected_channel": channel,
    }


def estimate_duration_sec(count: int, interval_sec: float) -> float:
    n = max(0, int(count))
    delay = max(0.0, float(interval_sec))
    if n <= 0:
        return 0.0
    return max(0.0, (n - 1) * delay)


def format_duration(seconds: float) -> str:
    sec = int(round(seconds))
    if sec < 60:
        return f"{sec} сек"
    minutes, rem = divmod(sec, 60)
    if minutes < 60:
        return f"{minutes} мин {rem} сек" if rem else f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if minutes:
        return f"{hours} ч {minutes} мин"
    return f"{hours} ч"


def create_campaign(
    *,
    title: str,
    channel: str,
    body_html: str,
    interval_sec: float,
    filters: dict | None,
    photo_path: str | None = None,
    button_text: str | None = None,
    button_url: str | None = None,
    followup_html: str | None = None,
    followup_until: date | str | None = None,
    disable_link_preview: bool = False,
    created_by: str = "owner",
    start: bool = True,
    scheduled_at: datetime | str | None = None,
) -> dict:
    ensure_mailing_tables()
    if not _use_postgres():
        raise RuntimeError("PostgreSQL required for mailing")
    if channel not in CHANNELS:
        raise ValueError("bad channel")
    body_html = (body_html or "").strip()
    if not body_html and not photo_path:
        raise ValueError("Нужен текст или картинка")
    interval_sec = max(0.0, min(float(interval_sec), 60.0))
    filters_n = normalize_filters(filters)
    button_text = (button_text or "").strip() or None
    button_url = (button_url or "").strip() or None
    followup_html = (followup_html or "").strip() or None
    if button_text and not button_url and not followup_html:
        raise ValueError("Для кнопки укажите ссылку или текст follow-up")
    until = _parse_iso_date(followup_until)
    if until is None and followup_html:
        # Если явно не указали — берём дату шоу из фильтров аудитории.
        until = _parse_iso_date(filters_n.get("date_to")) or _parse_iso_date(
            filters_n.get("date_from")
        )
    when = parse_admin_datetime(scheduled_at)
    if when and when <= now_msk():
        raise ValueError("Время рассылки уже прошло — выберите другое или отправьте сразу")

    preview = preview_audience(channel, filters_n)
    capped = int(preview["capped_total"])
    if capped <= 0:
        raise ValueError("По фильтрам никого нет")

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mailing_campaigns (
                    title, channel, status, body_html, photo_path,
                    button_text, button_url, followup_html, followup_until,
                    disable_link_preview, interval_sec, batch_limit, filters,
                    total_count, created_by, scheduled_at
                )
                VALUES (
                    %(title)s, %(channel)s, %(status)s, %(body_html)s, %(photo_path)s,
                    %(button_text)s, %(button_url)s, %(followup_html)s, %(followup_until)s,
                    %(disable_link_preview)s, %(interval_sec)s, %(batch_limit)s, %(filters)s,
                    0, %(created_by)s, %(scheduled_at)s
                )
                RETURNING *
                """,
                {
                    "title": (title or "").strip() or "Рассылка",
                    "channel": channel,
                    "status": "queued" if start else "draft",
                    "body_html": body_html,
                    "photo_path": photo_path,
                    "button_text": button_text,
                    "button_url": button_url,
                    "followup_html": followup_html,
                    "followup_until": until,
                    "disable_link_preview": bool(disable_link_preview),
                    "interval_sec": interval_sec,
                    "batch_limit": filters_n.get("batch_limit"),
                    "filters": Json(filters_n),
                    "created_by": created_by or "owner",
                    "scheduled_at": when,
                },
            )
            campaign = dict(cur.fetchone())
            campaign_id = int(campaign["id"])

            channels = ["telegram", "vkontakte"] if channel == "both" else [channel]
            inserted = 0
            for ch in channels:
                sql, params = _audience_sql(ch, filters_n)
                params = dict(params)
                params["campaign_id"] = campaign_id
                limit_sql = ""
                remaining = None
                if filters_n.get("batch_limit"):
                    remaining = max(0, int(filters_n["batch_limit"]) - inserted)
                    if remaining <= 0:
                        break
                    limit_sql = f" LIMIT {int(remaining)}"
                cur.execute(
                    f"""
                    INSERT INTO mailing_recipients (campaign_id, user_id, channel, peer_id)
                    SELECT %(campaign_id)s, aud.user_id, aud.channel, aud.peer_id
                    FROM ({sql}) AS aud
                    ORDER BY aud.user_id
                    {limit_sql}
                    ON CONFLICT (campaign_id, channel, peer_id) DO NOTHING
                    """,
                    params,
                )
                inserted += cur.rowcount or 0

            cur.execute(
                """
                UPDATE mailing_campaigns
                SET total_count = (
                    SELECT COUNT(*) FROM mailing_recipients WHERE campaign_id = %(id)s
                )
                WHERE id = %(id)s
                RETURNING *
                """,
                {"id": campaign_id},
            )
            campaign = dict(cur.fetchone())
        conn.commit()
    return campaign


def _previous_full_queue(limited: dict) -> dict | None:
    """Ближняя более крупная очередь того же канала — обычно отменённая 21k перед лимитом 11k."""
    channel = (limited.get("channel") or "").strip()
    if channel not in CHANNELS:
        return None
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM mailing_campaigns
                WHERE id < %(id)s
                  AND channel = %(channel)s
                  AND title IS DISTINCT FROM 'test-followup'
                  AND total_count > %(total)s
                ORDER BY id DESC
                LIMIT 1
                """,
                {
                    "id": int(limited["id"]),
                    "channel": channel,
                    "total": int(limited.get("total_count") or 0),
                },
            )
            row = cur.fetchone()
    return dict(row) if row else None


def remainder_after_limit(limited_id: int) -> dict:
    """Сколько человек из полной очереди не попали в ограниченную рассылку."""
    ensure_mailing_tables()
    limited = get_campaign(limited_id)
    if not limited:
        raise ValueError("Кампания не найдена")
    full = _previous_full_queue(limited)
    if not full:
        return {"count": 0, "full_id": None, "limited_id": int(limited_id)}
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM mailing_recipients r
                WHERE r.campaign_id = %(full_id)s
                  AND r.status IN ('pending', 'skipped')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM mailing_recipients s
                      WHERE s.campaign_id = %(limited_id)s
                        AND s.user_id = r.user_id
                        AND s.channel = r.channel
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM users xu
                      WHERE xu.id = r.user_id
                        AND LOWER(BTRIM(COALESCE(xu.username, ''))) = ANY(%(skip_usernames)s)
                  )
                """,
                {"full_id": int(full["id"]), "limited_id": int(limited_id), "skip_usernames": list(MAIL_SKIP_USERNAMES)},
            )
            count = int(cur.fetchone()["n"] or 0)
    return {
        "count": count,
        "full_id": int(full["id"]),
        "limited_id": int(limited_id),
        "full_title": full.get("title") or "",
        "full_total": int(full.get("total_count") or 0),
    }


def create_remainder_campaign(limited_id: int, *, created_by: str = "owner") -> dict:
    """Новая рассылка: текст/кнопка/фото из ограниченной, люди — из полной очереди минус уже взятые."""
    ensure_mailing_tables()
    if not _use_postgres():
        raise RuntimeError("PostgreSQL required for mailing")
    limited = get_campaign(limited_id)
    if not limited:
        raise ValueError("Кампания не найдена")
    info = remainder_after_limit(limited_id)
    full_id = info.get("full_id")
    if not full_id or int(info.get("count") or 0) <= 0:
        raise ValueError(
            "Не кого досылать: нет предыдущей более крупной очереди или все из неё уже были в этой рассылке"
        )
    if not (limited.get("body_html") or "").strip() and not (limited.get("photo_path") or "").strip():
        raise ValueError("У этой рассылки нет текста и картинки")
    title = (limited.get("title") or "Рассылка").strip()
    if "остаток" not in title.casefold():
        title = f"{title} · остаток"
    title = title[:120]
    channel = limited.get("channel") or "telegram"
    if channel not in CHANNELS:
        channel = "telegram"
    filters_payload = {
        "remainder_of": int(limited_id),
        "remainder_from": int(full_id),
    }
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mailing_campaigns (
                    title, channel, status, body_html, photo_path,
                    button_text, button_url, followup_html, followup_until,
                    disable_link_preview, interval_sec, batch_limit, filters,
                    total_count, created_by
                )
                VALUES (
                    %(title)s, %(channel)s, 'queued', %(body_html)s, %(photo_path)s,
                    %(button_text)s, %(button_url)s, %(followup_html)s, %(followup_until)s,
                    %(disable_link_preview)s, %(interval_sec)s, NULL, %(filters)s,
                    0, %(created_by)s
                )
                RETURNING *
                """,
                {
                    "title": title,
                    "channel": channel,
                    "body_html": limited.get("body_html") or "",
                    "photo_path": limited.get("photo_path"),
                    "button_text": limited.get("button_text"),
                    "button_url": limited.get("button_url"),
                    "followup_html": limited.get("followup_html"),
                    "followup_until": limited.get("followup_until"),
                    "disable_link_preview": bool(limited.get("disable_link_preview")),
                    "interval_sec": limited.get("interval_sec") or 0.1,
                    "filters": Json(filters_payload),
                    "created_by": created_by or "owner",
                },
            )
            campaign = dict(cur.fetchone())
            new_id = int(campaign["id"])
            cur.execute(
                """
                INSERT INTO mailing_recipients (campaign_id, user_id, channel, peer_id)
                SELECT %(new_id)s, r.user_id, r.channel, r.peer_id
                FROM mailing_recipients r
                WHERE r.campaign_id = %(full_id)s
                  AND r.status IN ('pending', 'skipped')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM mailing_recipients s
                      WHERE s.campaign_id = %(limited_id)s
                        AND s.user_id = r.user_id
                        AND s.channel = r.channel
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM users xu
                      WHERE xu.id = r.user_id
                        AND LOWER(BTRIM(COALESCE(xu.username, ''))) = ANY(%(skip_usernames)s)
                  )
                ON CONFLICT (campaign_id, channel, peer_id) DO NOTHING
                """,
                {
                    "new_id": new_id,
                    "full_id": int(full_id),
                    "limited_id": int(limited_id),
                    "skip_usernames": list(MAIL_SKIP_USERNAMES),
                },
            )
            cur.execute(
                """
                UPDATE mailing_campaigns
                SET total_count = (
                    SELECT COUNT(*) FROM mailing_recipients WHERE campaign_id = %(id)s
                )
                WHERE id = %(id)s
                RETURNING *
                """,
                {"id": new_id},
            )
            campaign = dict(cur.fetchone())
        conn.commit()
    if int(campaign.get("total_count") or 0) <= 0:
        raise ValueError("Не удалось собрать остаток очереди")
    return campaign


def list_campaigns(limit: int = 40) -> list[dict]:
    ensure_mailing_tables()
    if not _use_postgres():
        return []
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM mailing_campaigns
                WHERE title IS DISTINCT FROM 'test-followup'
                ORDER BY id DESC
                LIMIT %(limit)s
                """,
                {"limit": max(1, min(int(limit), 200))},
            )
            return [dict(r) for r in cur.fetchall()]


def get_campaign(campaign_id: int) -> dict | None:
    ensure_mailing_tables()
    if not _use_postgres():
        return None
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM mailing_campaigns WHERE id = %s",
                (int(campaign_id),),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def get_campaign_followup(campaign_id: int) -> str | None:
    """Текст после кнопки: обычный follow-up или «уже неактуально», если дата прошла."""
    row = get_campaign(campaign_id)
    if not row:
        return None
    text = (row.get("followup_html") or "").strip()
    if not text:
        return None
    until = campaign_followup_until(row)
    if until and now_msk().date() > until:
        return FOLLOWUP_EXPIRED_TEXT
    return text


def set_campaign_followup_until(campaign_id: int, until: date | str | None) -> dict | None:
    """Проставить/сбросить дату актуальности кнопки для уже созданной кампании."""
    ensure_mailing_tables()
    parsed = _parse_iso_date(until) if until not in (None, "") else None
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE mailing_campaigns
                SET followup_until = %(until)s
                WHERE id = %(id)s
                RETURNING *
                """,
                {"id": int(campaign_id), "until": parsed},
            )
            row = cur.fetchone()
            conn.commit()
            return dict(row) if row else None


def set_campaign_status(campaign_id: int, status: str) -> dict | None:
    if status not in CAMPAIGN_STATUSES:
        raise ValueError("bad status")
    ensure_mailing_tables()
    extras = ""
    if status == "running":
        extras = ", started_at = COALESCE(started_at, NOW())"
    if status in ("done", "cancelled"):
        extras = ", finished_at = NOW()"
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE mailing_campaigns
                SET status = %(status)s {extras}
                WHERE id = %(id)s
                RETURNING *
                """,
                {"id": int(campaign_id), "status": status},
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


def set_campaign_schedule(
    campaign_id: int,
    *,
    scheduled_at: datetime | str | None = None,
    send_now: bool = False,
) -> dict | None:
    """Перенести запланированную рассылку или отправить сразу."""
    ensure_mailing_tables()
    row = get_campaign(campaign_id)
    if not row:
        raise ValueError("Кампания не найдена")
    if (row.get("status") or "") not in ("queued", "paused"):
        raise ValueError("Перенести можно только очередь или паузу")
    if send_now:
        when = None
        status = "queued"
    else:
        when = parse_admin_datetime(scheduled_at)
        if not when:
            raise ValueError("Укажите дату и время")
        if when <= now_msk():
            raise ValueError("Время уже прошло")
        status = row.get("status") or "queued"
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE mailing_campaigns
                SET scheduled_at = %(when)s,
                    status = %(status)s,
                    finished_at = NULL
                WHERE id = %(id)s
                RETURNING *
                """,
                {"id": int(campaign_id), "when": when, "status": status},
            )
            updated = cur.fetchone()
        conn.commit()
    return dict(updated) if updated else None


def list_mailing_templates() -> list[dict]:
    ensure_mailing_tables()
    rows: dict[str, dict] = {}
    if _use_postgres():
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM mailing_templates")
                for raw in cur.fetchall() or []:
                    row = dict(raw)
                    rows[str(row.get("key") or "")] = row
    out = []
    for spec in MAILING_TEMPLATE_SPECS:
        stored = rows.get(spec["key"]) or {}
        body = (stored.get("body_html") or "").strip() or spec.get("body_html") or ""
        followup = (stored.get("followup_html") or "").strip() or spec.get("followup_html") or ""
        if spec.get("uses_show"):
            body = ensure_best_placeholders(body)
            followup = ensure_best_placeholders(followup)
        out.append(
            {
                "key": spec["key"],
                "title": spec["title"],
                "channel": spec["channel"],
                "uses_show": bool(spec.get("uses_show")),
                "body_html": body,
                "button_text": (stored.get("button_text") or "").strip()
                or spec.get("button_text")
                or "",
                "followup_html": followup,
            }
        )
    return out


def save_mailing_templates(items: list[dict]) -> list[dict]:
    ensure_mailing_tables()
    if not _use_postgres():
        raise RuntimeError("PostgreSQL required for mailing")
    by_key = {str(item.get("key") or ""): item for item in items if item}
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            for spec in MAILING_TEMPLATE_SPECS:
                payload = by_key.get(spec["key"]) or {}
                cur.execute(
                    """
                    INSERT INTO mailing_templates (
                        key, title, channel, body_html, button_text, followup_html, updated_at
                    )
                    VALUES (
                        %(key)s, %(title)s, %(channel)s, %(body_html)s,
                        %(button_text)s, %(followup_html)s, NOW()
                    )
                    ON CONFLICT (key) DO UPDATE
                    SET title = EXCLUDED.title,
                        channel = EXCLUDED.channel,
                        body_html = EXCLUDED.body_html,
                        button_text = EXCLUDED.button_text,
                        followup_html = EXCLUDED.followup_html,
                        updated_at = NOW()
                    """,
                    {
                        "key": spec["key"],
                        "title": spec["title"],
                        "channel": spec["channel"],
                        "body_html": (payload.get("body_html") or "").strip(),
                        "button_text": (payload.get("button_text") or "").strip(),
                        "followup_html": (payload.get("followup_html") or "").strip(),
                    },
                )
        conn.commit()
    return list_mailing_templates()


def claim_next_campaign() -> dict | None:
    """Pick queued campaign that is due, or continue a running one."""
    ensure_mailing_tables()
    if not _use_postgres():
        return None
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM mailing_campaigns
                WHERE status = 'running'
                ORDER BY id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    """
                    SELECT id FROM mailing_campaigns
                    WHERE status = 'queued'
                      AND (scheduled_at IS NULL OR scheduled_at <= NOW())
                    ORDER BY COALESCE(scheduled_at, created_at), id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """
                )
                row = cur.fetchone()
                if not row:
                    conn.commit()
                    return None
                cur.execute(
                    """
                    UPDATE mailing_campaigns
                    SET status = 'running',
                        started_at = COALESCE(started_at, NOW())
                    WHERE id = %s
                    RETURNING *
                    """,
                    (int(row["id"]),),
                )
            else:
                cur.execute(
                    "SELECT * FROM mailing_campaigns WHERE id = %s",
                    (int(row["id"]),),
                )
            campaign = dict(cur.fetchone())
        conn.commit()
    return campaign


def fetch_pending_recipients(campaign_id: int, limit: int = 25) -> list[dict]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM mailing_recipients
                WHERE campaign_id = %s AND status = 'pending'
                ORDER BY id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (int(campaign_id), max(1, min(int(limit), 100))),
            )
            rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
    return rows


def mark_recipient(
    recipient_id: int,
    *,
    status: str,
    error: str | None = None,
) -> None:
    if status not in ("sent", "failed", "skipped"):
        raise ValueError("bad recipient status")
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE mailing_recipients
                SET status = %(status)s,
                    error = %(error)s,
                    sent_at = CASE WHEN %(status)s = 'sent' THEN NOW() ELSE sent_at END
                WHERE id = %(id)s AND status = 'pending'
                """,
                {
                    "id": int(recipient_id),
                    "status": status,
                    "error": (error or "")[:500] or None,
                },
            )
            cur.execute(
                """
                UPDATE mailing_campaigns c
                SET
                    sent_count = (
                        SELECT COUNT(*) FROM mailing_recipients r
                        WHERE r.campaign_id = c.id AND r.status = 'sent'
                    ),
                    failed_count = (
                        SELECT COUNT(*) FROM mailing_recipients r
                        WHERE r.campaign_id = c.id AND r.status = 'failed'
                    ),
                    skipped_count = (
                        SELECT COUNT(*) FROM mailing_recipients r
                        WHERE r.campaign_id = c.id AND r.status = 'skipped'
                    )
                WHERE c.id = (
                    SELECT campaign_id FROM mailing_recipients WHERE id = %(id)s
                )
                """,
                {"id": int(recipient_id)},
            )
        conn.commit()


def finalize_if_complete(campaign_id: int) -> dict | None:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM mailing_recipients
                WHERE campaign_id = %s AND status = 'pending'
                """,
                (int(campaign_id),),
            )
            pending = int(cur.fetchone()["n"] or 0)
            if pending > 0:
                cur.execute(
                    "SELECT * FROM mailing_campaigns WHERE id = %s",
                    (int(campaign_id),),
                )
                row = cur.fetchone()
                conn.commit()
                return dict(row) if row else None
            cur.execute(
                """
                UPDATE mailing_campaigns
                SET status = 'done', finished_at = NOW()
                WHERE id = %s AND status = 'running'
                RETURNING *
                """,
                (int(campaign_id),),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else get_campaign(campaign_id)


def list_recipients(
    campaign_id: int,
    *,
    status: str = "",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    ensure_mailing_tables()
    if not _use_postgres():
        return {"rows": [], "total": 0, "page": 1, "pages": 1}
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 100))
    where = ["r.campaign_id = %(campaign_id)s"]
    params: dict[str, Any] = {"campaign_id": int(campaign_id)}
    if status in ("pending", "sent", "failed", "skipped"):
        where.append("r.status = %(status)s")
        params["status"] = status
    where_sql = " AND ".join(where)
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) AS n FROM mailing_recipients r WHERE {where_sql}",
                params,
            )
            total = int(cur.fetchone()["n"] or 0)
            pages = max(1, (total + page_size - 1) // page_size)
            if page > pages:
                page = pages
            params["limit"] = page_size
            params["offset"] = (page - 1) * page_size
            cur.execute(
                f"""
                SELECT r.*, u.name, u.username, u.phone
                FROM mailing_recipients r
                JOIN users u ON u.id = r.user_id
                WHERE {where_sql}
                ORDER BY r.id
                LIMIT %(limit)s OFFSET %(offset)s
                """,
                params,
            )
            rows = [dict(r) for r in cur.fetchall()]
    return {"rows": rows, "total": total, "page": page, "pages": pages}


def set_campaign_photo(campaign_id: int, photo_path: str | None) -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE mailing_campaigns SET photo_path = %s WHERE id = %s",
                (photo_path, int(campaign_id)),
            )
        conn.commit()


def iso(dt: Any) -> str:
    if not dt:
        return ""
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return str(dt)


def search_users_for_test(q: str, *, channel: str = "", limit: int = 20) -> list[dict]:
    """Find users by name / @username / phone for test mailing."""
    ensure_mailing_tables()
    if not _use_postgres():
        return []
    q = (q or "").strip()
    if len(q) < 1:
        return []
    q_user = q[1:].strip() if q.startswith("@") else q
    phone_digits = "".join(ch for ch in q if ch.isdigit())
    where = [
        "("
        "COALESCE(u.name, '') ILIKE %(q_like)s"
        " OR COALESCE(u.username, '') ILIKE %(q_user_like)s"
        " OR COALESCE(u.phone, '') ILIKE %(q_like)s"
        + (
            " OR regexp_replace(COALESCE(u.phone, ''), '\\D', '', 'g') LIKE %(q_phone_digits)s"
            if len(phone_digits) >= 3
            else ""
        )
        + ")"
    ]
    params: dict[str, Any] = {
        "q_like": f"%{q}%",
        "q_user_like": f"%{q_user}%",
        "limit": max(1, min(int(limit), 50)),
    }
    if len(phone_digits) >= 3:
        params["q_phone_digits"] = f"%{phone_digits}%"
    if channel == "telegram":
        where.append("u.telegram_id IS NOT NULL")
    elif channel == "vkontakte":
        where.append("u.vk_id IS NOT NULL")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT u.id, u.name, u.username, u.phone, u.telegram_id, u.vk_id, u.source
                FROM users u
                WHERE {" AND ".join(where)}
                ORDER BY u.id DESC
                LIMIT %(limit)s
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]


def create_followup_stub(
    *,
    followup_html: str,
    body_html: str = "",
    channel: str = "telegram",
    created_by: str = "owner",
) -> int:
    """Store follow-up text so test/callback buttons can resolve mail_fu:<id>."""
    ensure_mailing_tables()
    if not _use_postgres():
        raise RuntimeError("PostgreSQL required")
    ch = channel if channel in ("telegram", "vkontakte", "both") else "telegram"
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mailing_campaigns (
                    title, channel, status, body_html, followup_html,
                    interval_sec, filters, total_count, created_by,
                    started_at, finished_at
                )
                VALUES (
                    'test-followup', %(channel)s, 'done', %(body)s, %(followup)s,
                    0, '{}'::jsonb, 0, %(created_by)s, NOW(), NOW()
                )
                RETURNING id
                """,
                {
                    "channel": ch,
                    "body": body_html or "",
                    "followup": (followup_html or "").strip(),
                    "created_by": created_by or "owner",
                },
            )
            cid = int(cur.fetchone()["id"])
        conn.commit()
    return cid


def get_user_for_mailing(user_id: int) -> dict | None:
    ensure_mailing_tables()
    if not _use_postgres():
        return None
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, username, phone, telegram_id, vk_id, source
                FROM users
                WHERE id = %s
                """,
                (int(user_id),),
            )
            row = cur.fetchone()
            return dict(row) if row else None
