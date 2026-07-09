import csv
import aiohttp
from datetime import datetime
from io import StringIO
from bot.config import CSV_URL


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
