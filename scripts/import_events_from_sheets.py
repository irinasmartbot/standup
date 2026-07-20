import argparse
import csv
import os
from datetime import date, datetime
from io import StringIO
from urllib.request import urlopen

import psycopg


DEFAULT_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vQTZS9GmN4Gkffl6xrUt7W_dDIksHB7z4xAjFDVeR-x4rgWeGJLJPGVfMfY5eQESZcXfBH-ZbrUeMXh/"
    "pub?gid=907191184&single=true&output=csv"
)
DEFAULT_BEST_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vQTZS9GmN4Gkffl6xrUt7W_dDIksHB7z4xAjFDVeR-x4rgWeGJLJPGVfMfY5eQESZcXfBH-ZbrUeMXh/"
    "pub?gid=0&single=true&output=csv"
)
DEFAULT_HITLOTO_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vQTZS9GmN4Gkffl6xrUt7W_dDIksHB7z4xAjFDVeR-x4rgWeGJLJPGVfMfY5eQESZcXfBH-ZbrUeMXh/"
    "pub?gid=1362946936&single=true&output=csv"
)


def load_env_file(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_date(value):
    return datetime.strptime(value.strip(), "%d.%m.%Y").date()


def parse_time(value):
    clean = value.strip().replace(".", ":")
    for fmt in ("%H:%M", "%H"):
        try:
            return datetime.strptime(clean, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Invalid event time: {value}")


def parse_int(value, default=0):
    try:
        return int((value or "").strip())
    except ValueError:
        return default


def fetch_csv(csv_url):
    with urlopen(csv_url, timeout=30) as response:
        return response.read().decode("utf-8-sig")


def _parse_proverka_row(row, row_number, event_format, source_sheet):
    extra = parse_int(row[9])
    return {
        "format": event_format,
        "event_date": parse_date(row[1]),
        "weekday": row[2].strip(),
        "event_time": parse_time(row[3]),
        "address": row[4].strip(),
        "location": row[10].strip(),
        "description": row[5].strip(),
        "image_url": row[6].strip(),
        "price": 0,
        "payment_url": None,
        "host": None,
        "max_seats": 60 + abs(extra),
        "status": "active",
        "source_sheet": source_sheet,
        "source_row": row_number,
    }


def _parse_best_row(row, row_number, event_format, source_sheet):
    return {
        "format": event_format,
        "event_date": parse_date(row[1]),
        "weekday": row[2].strip(),
        "event_time": parse_time(row[3]),
        "address": row[10].strip(),
        "location": row[4].strip(),
        "description": row[5].strip(),
        "image_url": row[7].strip(),
        "price": parse_int(row[6]),
        "payment_url": row[9].strip() or None,
        "host": row[11].strip() or None,
        "max_seats": parse_int(row[0]),
        "status": "active",
        "source_sheet": source_sheet,
        "source_row": row_number,
    }


def parse_event_row(row, row_number, event_format, source_sheet):
    if event_format in {"best", "hitloto"}:
        return _parse_best_row(row, row_number, event_format, source_sheet)
    return _parse_proverka_row(row, row_number, event_format, source_sheet)


def parse_events(csv_text, event_format, source_sheet):
    reader = csv.reader(StringIO(csv_text))
    rows = list(reader)
    today = date.today()
    events = []

    for row_number, row in enumerate(rows[1:], start=2):
        if len(row) < 17:
            continue
        if row[16].strip() != "Актуально":
            continue

        try:
            event = parse_event_row(row, row_number, event_format, source_sheet)
        except (IndexError, ValueError):
            continue

        event_date = event["event_date"]
        if event_date < today:
            continue

        events.append(event)

    return events


UPSERT_EVENT_SQL = """
INSERT INTO events (
    format, event_date, weekday, event_time, address, location, description,
    image_url, price, payment_url, host, max_seats, status, source_sheet, source_row
)
VALUES (
    %(format)s, %(event_date)s, %(weekday)s, %(event_time)s, %(address)s, %(location)s,
    %(description)s, %(image_url)s, %(price)s, %(payment_url)s, %(host)s,
    %(max_seats)s, %(status)s, %(source_sheet)s, %(source_row)s
)
ON CONFLICT (format, event_date, event_time, location)
DO UPDATE SET
    weekday = EXCLUDED.weekday,
    address = EXCLUDED.address,
    description = EXCLUDED.description,
    image_url = EXCLUDED.image_url,
    price = EXCLUDED.price,
    payment_url = EXCLUDED.payment_url,
    host = EXCLUDED.host,
    max_seats = EXCLUDED.max_seats,
    status = EXCLUDED.status,
    source_sheet = EXCLUDED.source_sheet,
    source_row = EXCLUDED.source_row,
    updated_at = now();
"""


def import_events(database_url, events):
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            for event in events:
                cur.execute(UPSERT_EVENT_SQL, event)
        conn.commit()


def main():
    load_env_file()
    parser = argparse.ArgumentParser(description="Import Google Sheets events into PostgreSQL.")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--csv-url")
    parser.add_argument("--format", default="proverka", choices=["proverka", "1plus1", "best", "masterclass", "hitloto"])
    parser.add_argument("--source-sheet", default="Проверка материала")
    args = parser.parse_args()

    if not args.database_url:
        raise SystemExit("DATABASE_URL is not set. Add it to .env or pass --database-url.")

    csv_url = args.csv_url
    if not csv_url:
        csv_url = {
            "best": os.getenv("BEST_CSV_URL", DEFAULT_BEST_CSV_URL),
            "hitloto": os.getenv("HITLOTO_CSV_URL", DEFAULT_HITLOTO_CSV_URL),
        }.get(args.format, os.getenv("CSV_URL", DEFAULT_CSV_URL))

    csv_text = fetch_csv(csv_url)
    events = parse_events(csv_text, args.format, args.source_sheet)
    import_events(args.database_url, events)
    print(f"Imported events: {len(events)}")


if __name__ == "__main__":
    main()
