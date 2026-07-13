import argparse
import os
import sqlite3
from datetime import date, datetime

import psycopg


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
    clean = (value or "").strip().replace(".", ":")
    for fmt in ("%H:%M", "%H"):
        try:
            return datetime.strptime(clean, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Invalid event time: {value}")


def parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def load_sqlite_bookings(sqlite_path):
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM bookings ORDER BY id").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def is_future_active_booking(row):
    if row.get("status") not in {"booked", "confirmed"}:
        return False
    try:
        return parse_date(row["event_date"]) >= date.today()
    except (KeyError, TypeError, ValueError):
        return False


UPSERT_USER_SQL = """
INSERT INTO users (telegram_id, username, name, phone, source, created_at, last_active_at)
VALUES (%(telegram_id)s, %(username)s, %(name)s, %(phone)s, 'telegram', %(created_at)s, %(last_active_at)s)
ON CONFLICT (telegram_id)
DO UPDATE SET
    username = COALESCE(EXCLUDED.username, users.username),
    name = COALESCE(EXCLUDED.name, users.name),
    phone = COALESCE(EXCLUDED.phone, users.phone),
    last_active_at = GREATEST(users.last_active_at, EXCLUDED.last_active_at)
RETURNING id;
"""


FIND_EVENT_SQL = """
SELECT id
FROM events
WHERE format = 'proverka'
  AND event_date = %(event_date)s
  AND event_time = %(event_time)s
  AND location = %(location)s
LIMIT 1;
"""


UPSERT_BOOKING_SQL = """
INSERT INTO bookings (
    user_id, event_id, guests, format, source, status, created_at, confirmed_at,
    annulled_at, reminder_24h_sent, reminder_day_sent, ticket_message_id, confirm_message_id
)
VALUES (
    %(user_id)s, %(event_id)s, %(guests)s, 'proverka', 'telegram', %(status)s, %(created_at)s,
    %(confirmed_at)s, %(annulled_at)s, %(reminder_24h_sent)s, %(reminder_day_sent)s,
    %(ticket_message_id)s, %(confirm_message_id)s
)
ON CONFLICT (user_id, event_id)
WHERE status IN ('booked', 'confirmed')
DO UPDATE SET
    guests = EXCLUDED.guests,
    status = EXCLUDED.status,
    reminder_24h_sent = EXCLUDED.reminder_24h_sent,
    reminder_day_sent = EXCLUDED.reminder_day_sent,
    ticket_message_id = EXCLUDED.ticket_message_id,
    confirm_message_id = EXCLUDED.confirm_message_id,
    updated_at = now();
"""


def migrate_bookings(database_url, bookings):
    imported = 0
    skipped = []

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            for row in bookings:
                if not is_future_active_booking(row):
                    skipped.append((row.get("id"), "not active future booking"))
                    continue

                try:
                    event_date = parse_date(row["event_date"])
                    event_time = parse_time(row["event_time"])
                except (KeyError, TypeError, ValueError) as exc:
                    skipped.append((row.get("id"), str(exc)))
                    continue

                created_at = parse_datetime(row.get("created_at")) or datetime.now()
                user_params = {
                    "telegram_id": row.get("telegram_id"),
                    "username": row.get("username") or None,
                    "name": row.get("name") or None,
                    "phone": row.get("phone") or None,
                    "created_at": created_at,
                    "last_active_at": created_at,
                }
                cur.execute(UPSERT_USER_SQL, user_params)
                user_id = cur.fetchone()[0]

                cur.execute(
                    FIND_EVENT_SQL,
                    {
                        "event_date": event_date,
                        "event_time": event_time,
                        "location": row.get("event_location"),
                    },
                )
                event = cur.fetchone()
                if not event:
                    skipped.append((row.get("id"), "matching event not found"))
                    continue

                booking_params = {
                    "user_id": user_id,
                    "event_id": event[0],
                    "guests": row.get("guests") or 1,
                    "status": row.get("status"),
                    "created_at": created_at,
                    "confirmed_at": created_at if row.get("status") == "confirmed" else None,
                    "annulled_at": parse_datetime(row.get("annulled_at")),
                    "reminder_24h_sent": bool(row.get("reminder_24h_sent", 0)),
                    "reminder_day_sent": bool(row.get("reminder_day_sent", 0)),
                    "ticket_message_id": row.get("ticket_message_id"),
                    "confirm_message_id": row.get("confirm_message_id"),
                }
                cur.execute(UPSERT_BOOKING_SQL, booking_params)
                imported += 1

        conn.commit()

    return imported, skipped


def main():
    load_env_file()
    parser = argparse.ArgumentParser(description="Migrate active future bookings from SQLite to PostgreSQL.")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--sqlite-path", default=os.getenv("DB_PATH", "bookings.db"))
    args = parser.parse_args()

    if not args.database_url:
        raise SystemExit("DATABASE_URL is not set. Add it to .env or pass --database-url.")
    if not os.path.exists(args.sqlite_path):
        raise SystemExit(f"SQLite database not found: {args.sqlite_path}")

    bookings = load_sqlite_bookings(args.sqlite_path)
    imported, skipped = migrate_bookings(args.database_url, bookings)

    print(f"Imported bookings: {imported}")
    print(f"Skipped bookings: {len(skipped)}")
    for booking_id, reason in skipped[:20]:
        print(f"- booking {booking_id}: {reason}")
    if len(skipped) > 20:
        print(f"... and {len(skipped) - 20} more skipped bookings")


if __name__ == "__main__":
    main()
