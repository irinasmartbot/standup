import csv
import asyncio
import aiohttp
from datetime import datetime
from io import StringIO

import psycopg
from psycopg.rows import dict_row

from bot.config import CSV_URL, DATABASE_URL, EVENTS_SOURCE


EVENTS_FROM_POSTGRES_SQL = """
SELECT
    event_date,
    weekday,
    event_time,
    address,
    description,
    image_url,
    location,
    max_seats
FROM events
WHERE format = 'proverka'
  AND status = 'active'
  AND event_date >= CURRENT_DATE
ORDER BY event_date, event_time, location;
"""


def _format_event_time(value):
    return value.strftime("%H:%M")


def _row_to_event(row):
    return {
        "date": row["event_date"].strftime("%d.%m.%Y"),
        "weekday": row["weekday"] or "",
        "time": _format_event_time(row["event_time"]),
        "address": row["address"] or "",
        "description": row["description"] or "",
        "image": row["image_url"] or "",
        "location": row["location"] or "",
        "max_seats": row["max_seats"] or 0,
    }


def _load_events_from_postgres():
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(EVENTS_FROM_POSTGRES_SQL)
            return [_row_to_event(row) for row in cur.fetchall()]


async def load_events_from_postgres():
    return await asyncio.to_thread(_load_events_from_postgres)


async def load_events():
    if EVENTS_SOURCE == "postgres" and DATABASE_URL:
        return await load_events_from_postgres()

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
        events.append({
            "date": row[1].strip(),
            "weekday": row[2].strip(),
            "time": row[3].strip(),
            "address": row[4].strip(),
            "description": row[5].strip(),
            "image": row[6].strip(),
            "location": row[10].strip(),
            "max_seats": max_seats,
        })
    return events


async def get_event(event_date, event_time):
    events = await load_events()
    return next((e for e in events if e["date"] == event_date and e["time"] == event_time), None)
