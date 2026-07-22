import csv
import asyncio
import logging
import aiohttp
from datetime import datetime
from io import StringIO

import psycopg
from psycopg.rows import dict_row

from bot.config import BEST_CSV_URL, CSV_URL, DATABASE_URL, EVENTS_SOURCE, HITLOTO_CSV_URL
from bot.utils.ticket import now_msk

logger = logging.getLogger(__name__)
POSTGRES_CONNECT_TIMEOUT = 5


EVENTS_FROM_POSTGRES_SQL = """
SELECT
    id,
    event_date,
    weekday,
    event_time,
    address,
    description,
    image_url,
    location,
    price,
    payment_url,
    host,
    max_seats,
    source_row
FROM events
WHERE format = %(event_format)s
  AND status = 'active'
  AND event_date >= CURRENT_DATE
ORDER BY event_date, event_time, location;
"""


def _format_event_time(value):
    return value.strftime("%H:%M")


def _row_to_event(row):
    return {
        "id": row["id"],
        "date": row["event_date"].strftime("%d.%m.%Y"),
        "weekday": row["weekday"] or "",
        "time": _format_event_time(row["event_time"]),
        "address": row["address"] or "",
        "description": row["description"] or "",
        "image": row["image_url"] or "",
        "location": row["location"] or "",
        "price": row["price"] or 0,
        "payment_url": row["payment_url"] or "",
        "host": row["host"] or "",
        "max_seats": row["max_seats"] or 0,
        "source_row": row["source_row"] or 0,
    }


def _load_events_from_postgres(event_format):
    with psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        connect_timeout=POSTGRES_CONNECT_TIMEOUT,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(EVENTS_FROM_POSTGRES_SQL, {"event_format": event_format})
            return [_row_to_event(row) for row in cur.fetchall()]


async def load_events_from_postgres(event_format="proverka"):
    return await asyncio.to_thread(_load_events_from_postgres, event_format)


def _parse_int(value, default=0):
    try:
        return int((value or "").strip())
    except ValueError:
        return default


def _event_from_proverka_row(row, source_row):
    extra = _parse_int(row[9])
    return {
        "id": source_row,
        "date": row[1].strip(),
        "weekday": row[2].strip(),
        "time": row[3].strip(),
        "address": row[4].strip(),
        "description": row[5].strip(),
        "image": row[6].strip(),
        "location": row[10].strip(),
        "price": 0,
        "payment_url": "",
        "host": "",
        "max_seats": 60 + abs(extra),
        "source_row": source_row,
    }


def _event_from_best_row(row, source_row):
    return {
        "id": source_row,
        "date": row[1].strip(),
        "weekday": row[2].strip(),
        "time": row[3].strip(),
        "address": row[10].strip(),
        "description": row[5].strip(),
        "image": row[7].strip(),
        "location": row[4].strip(),
        "price": _parse_int(row[6]),
        "payment_url": row[9].strip(),
        "host": row[11].strip(),
        "max_seats": _parse_int(row[0]),
        "source_row": source_row,
    }


def _event_from_csv_row(row, source_row, event_format):
    if event_format in {"best", "hitloto"}:
        return _event_from_best_row(row, source_row)
    return _event_from_proverka_row(row, source_row)


async def _load_events_from_sheets(event_format="proverka"):
    csv_urls = {
        "best": BEST_CSV_URL,
        "hitloto": HITLOTO_CSV_URL,
    }
    csv_url = csv_urls.get(event_format, CSV_URL)

    async with aiohttp.ClientSession() as session:
        async with session.get(csv_url) as resp:
            text = await resp.text(encoding="utf-8-sig")
    reader = csv.reader(StringIO(text))
    rows = list(reader)
    events = []
    for source_row, row in enumerate(rows[1:], start=2):
        if len(row) < 17:
            continue
        if row[16].strip() != "Актуально":
            continue
        try:
            date = datetime.strptime(row[1].strip(), "%d.%m.%Y")
        except ValueError:
            continue
        if date.date() < now_msk().date():
            continue
        try:
            events.append(_event_from_csv_row(row, source_row, event_format))
        except IndexError:
            continue
    return events


async def load_events(event_format="proverka"):
    if EVENTS_SOURCE == "postgres" and DATABASE_URL:
        return await load_events_from_postgres(event_format)

    return await _load_events_from_sheets(event_format)


async def get_event(event_date, event_time, event_format="proverka"):
    events = await load_events(event_format)
    return next((e for e in events if e["date"] == event_date and e["time"] == event_time), None)
