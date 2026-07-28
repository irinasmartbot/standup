"""Seed a few demo Telegram users with coherent funnel analytics.

Safe to re-run: uses fixed telegram_id range 900000001..900000006
and replaces their analytics events each time.

Usage on VPS (from /home/standup/app):
  ssh standup@31.128.47.4
  cd /home/standup/app
  source .venv/bin/activate   # if used
  set -a && source .env && set +a
  python scripts/seed_demo_users.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import BOOKINGS_SOURCE, DATABASE_URL  # noqa: E402
from bot.db.analytics import (  # noqa: E402
    EVENT_BOOKING_CONFIRMED,
    EVENT_BOOKING_CREATED,
    EVENT_BOT_START,
    EVENT_BRANCH_BEST,
    EVENT_BRANCH_PROVERKA,
    EVENT_BUY_CLICK,
    EVENT_SHOW_CARD,
)


DEMO = [
    {
        "telegram_id": 900000001,
        "name": "Демо · только старт",
        "username": "demo_start_only",
        "events": [EVENT_BOT_START],
    },
    {
        "telegram_id": 900000002,
        "name": "Демо · смотрел BEST",
        "username": "demo_browse_best",
        "events": [EVENT_BOT_START, EVENT_BRANCH_BEST, EVENT_SHOW_CARD],
    },
    {
        "telegram_id": 900000003,
        "name": "Демо · клик Купить",
        "username": "demo_buy_click",
        "events": [EVENT_BOT_START, EVENT_BRANCH_BEST, EVENT_SHOW_CARD, EVENT_BUY_CLICK],
    },
    {
        "telegram_id": 900000004,
        "name": "Демо · бронь без билета",
        "username": "demo_booked",
        "events": [
            EVENT_BOT_START,
            EVENT_BRANCH_PROVERKA,
            EVENT_SHOW_CARD,
            EVENT_BUY_CLICK,
            EVENT_BOOKING_CREATED,
        ],
    },
    {
        "telegram_id": 900000005,
        "name": "Демо · билет получен",
        "username": "demo_ticket",
        "events": [
            EVENT_BOT_START,
            EVENT_BRANCH_BEST,
            EVENT_SHOW_CARD,
            EVENT_BUY_CLICK,
            EVENT_BOOKING_CREATED,
            EVENT_BOOKING_CONFIRMED,
        ],
    },
    {
        "telegram_id": 900000006,
        "name": "Демо · VK-стиль (telegram id)",
        "username": "demo_mixed",
        "events": [EVENT_BOT_START, EVENT_BRANCH_BEST],
    },
]


def main() -> int:
    if BOOKINGS_SOURCE != "postgres" or not DATABASE_URL:
        print("Need BOOKINGS_SOURCE=postgres and DATABASE_URL")
        return 1

    import psycopg

    now = datetime.now()
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            for i, person in enumerate(DEMO):
                tid = person["telegram_id"]
                cur.execute(
                    """
                    INSERT INTO users (telegram_id, username, name, source, created_at, last_active_at)
                    VALUES (%s, %s, %s, 'telegram', %s, %s)
                    ON CONFLICT (telegram_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        name = EXCLUDED.name,
                        last_active_at = EXCLUDED.last_active_at
                    RETURNING id
                    """,
                    (tid, person["username"], person["name"], now - timedelta(days=7 - i), now - timedelta(hours=i)),
                )
                user_id = cur.fetchone()[0]
                cur.execute(
                    "DELETE FROM analytics_events WHERE telegram_id = %s",
                    (tid,),
                )
                for step, name in enumerate(person["events"]):
                    created = now - timedelta(hours=len(person["events"]) - step, minutes=i * 3)
                    props = {"demo": True, "seed": "seed_demo_users"}
                    if name == EVENT_SHOW_CARD:
                        props["format"] = "best" if "BEST" in person["name"] or "билет" in person["name"] else "proverka"
                    cur.execute(
                        """
                        INSERT INTO analytics_events (
                            created_at, name, channel, telegram_id, user_id, props
                        ) VALUES (%s, %s, 'telegram', %s, %s, %s::jsonb)
                        """,
                        (created, name, tid, user_id, json.dumps(props, ensure_ascii=False)),
                    )
                print(f"ok user_id={user_id} tg={tid} · {person['name']} · {len(person['events'])} events")
        conn.commit()
    print("Done. Open Админ → Пользователи / Аналитика.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
