import sqlite3
from bot.config import BOOKINGS_SOURCE, DB_PATH


def init_db():
    if BOOKINGS_SOURCE == "postgres":
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
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
            created_at TEXT
        )
    """)
    c.execute("PRAGMA table_info(bookings)")
    columns = {row[1] for row in c.fetchall()}
    migrations = {
        "reminder_24h_sent": "ALTER TABLE bookings ADD COLUMN reminder_24h_sent INTEGER DEFAULT 0",
        "reminder_day_sent": "ALTER TABLE bookings ADD COLUMN reminder_day_sent INTEGER DEFAULT 0",
        "annulled_at": "ALTER TABLE bookings ADD COLUMN annulled_at TEXT",
        "ticket_message_id": "ALTER TABLE bookings ADD COLUMN ticket_message_id INTEGER",
        "confirm_message_id": "ALTER TABLE bookings ADD COLUMN confirm_message_id INTEGER",
    }
    for column, sql in migrations.items():
        if column not in columns:
            c.execute(sql)
    conn.commit()
    conn.close()
