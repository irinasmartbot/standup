import argparse
import os
import time
from datetime import datetime

from import_events_from_sheets import (
    DEFAULT_BEST_CSV_URL,
    DEFAULT_CSV_URL,
    fetch_csv,
    import_events,
    load_env_file,
    parse_events,
)


def sync_once(database_url, csv_url, event_format, source_sheet):
    csv_text = fetch_csv(csv_url)
    events = parse_events(csv_text, event_format, source_sheet)
    import_events(database_url, events)
    return len(events)


def sync_sources(database_url, sources):
    counts = {}
    for event_format, csv_url, source_sheet in sources:
        counts[event_format] = sync_once(database_url, csv_url, event_format, source_sheet)
    return counts


def main():
    load_env_file()
    parser = argparse.ArgumentParser(description="Sync Google Sheets events into PostgreSQL every hour.")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--csv-url", default=os.getenv("CSV_URL", DEFAULT_CSV_URL))
    parser.add_argument("--best-csv-url", default=os.getenv("BEST_CSV_URL", DEFAULT_BEST_CSV_URL))
    parser.add_argument("--format", default="proverka", choices=["proverka", "1plus1", "best", "masterclass", "hitloto"])
    parser.add_argument("--source-sheet", default="Проверка материала")
    parser.add_argument("--interval-seconds", type=int, default=3600)
    args = parser.parse_args()

    if not args.database_url:
        raise SystemExit("DATABASE_URL is not set. Add it to .env or pass --database-url.")

    sources = [
        ("proverka", args.csv_url, "Проверка материала"),
        ("best", args.best_csv_url, "StandUp BEST"),
    ]
    if args.format not in ("", "proverka"):
        sources = [(args.format, args.csv_url, args.source_sheet)]

    while True:
        try:
            counts = sync_sources(args.database_url, sources)
            summary = ", ".join(f"{event_format}: {count}" for event_format, count in counts.items())
            print(f"{datetime.now().isoformat(timespec='seconds')} synced events: {summary}", flush=True)
        except Exception as exc:
            print(f"{datetime.now().isoformat(timespec='seconds')} sync failed: {exc}", flush=True)

        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
