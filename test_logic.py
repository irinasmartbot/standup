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


async def main():
    test_callback_parsing()
    test_db_schema()
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
