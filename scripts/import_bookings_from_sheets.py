"""Import active Proverka bookings from Google Sheets / CSV into PostgreSQL.

Expected columns (headers, any order):
  client_id, имя, счет, телефон, дата мероприятия, время мероприятия,
  локация, кол-во участников / по какой, Статус брони, источник

Rules:
- skip cancelled («бронь снята») and past dates;
- match existing users only (client_id is Salebot id, NOT tg/vk id);
- phone match: full digits + last 10 digits; prefer «источник» TG/VK;
- if no user found — skip (no new cards; pass --create-missing only if needed);
- guests from «N / бронь» (fallback: «счет»);
- match events by format=proverka + date + time + fuzzy location;
- default dry-run (no writes). Pass --apply to upsert.

Examples:
  python scripts/import_bookings_from_sheets.py --csv-url "https://docs.google.com/.../pub?gid=...&output=csv"
  python scripts/import_bookings_from_sheets.py ./bookings.csv --apply
"""

from __future__ import annotations

import argparse
import csv
import os
import re
from collections import Counter
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from urllib.request import urlopen

import psycopg

ROOT = Path(__file__).resolve().parents[1]

HEADER_ALIASES = {
    "client_id": ("client_id", "client id", "id клиента", "clientid"),
    "name": ("имя", "name", "name_reg"),
    "score": ("счет", "счёт", "score"),
    "phone": ("телефон", "phone", "phone_reg"),
    "event_date": ("дата мероприятия", "дата", "event_date", "date"),
    "event_time": ("время мероприятия", "время", "event_time", "time"),
    "location": ("локация", "location", "площадка", "address"),
    "guests_raw": (
        "кол-во участников / по какой",
        "кол-во участников",
        "количество участников",
        "guests",
    ),
    "status": ("статус брони", "статус", "status"),
    "source": ("источник", "source", "мессенджер"),
}


def load_env_file(path: str | Path = ".env") -> None:
    path = Path(path)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _norm_header(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _map_headers(fieldnames: list[str] | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not fieldnames:
        return mapping
    # Skip blank / junk headers (Sheets often has an empty first column).
    normalized = {
        _norm_header(name): name
        for name in fieldnames
        if name and _norm_header(name)
    }
    for key, aliases in HEADER_ALIASES.items():
        # 1) exact alias match
        for alias in aliases:
            alias_n = _norm_header(alias)
            real = normalized.get(alias_n)
            if real:
                mapping[key] = real
                break
        if key in mapping:
            continue
        # 2) longest header that starts with alias (min 4 chars) —
        # e.g. «кол-во участников / по какой цепочке брони»
        best: tuple[int, str] | None = None
        for alias in aliases:
            alias_n = _norm_header(alias)
            if len(alias_n) < 4:
                continue
            for header_n, original in normalized.items():
                if header_n.startswith(alias_n):
                    cand = (len(header_n), original)
                    if best is None or cand[0] > best[0]:
                        best = cand
        if best:
            mapping[key] = best[1]
    return mapping


def _cell(row: dict, mapping: dict[str, str], key: str) -> str:
    col = mapping.get(key)
    if not col:
        return ""
    return (row.get(col) or "").strip()


def parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%d.%m.%Y").date()


def parse_time(value: str):
    clean = (value or "").strip().replace(".", ":")
    for fmt in ("%H:%M", "%H"):
        try:
            return datetime.strptime(clean, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Invalid event time: {value}")


def norm_phone(value: str) -> str | None:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if len(digits) < 10:
        return None
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def phone_match_keys(phone: str) -> list[str]:
    """Variants to look up an existing user by phone."""
    keys: list[str] = []
    full = norm_phone(phone)
    if not full:
        return keys
    keys.append(full)
    last10 = full[-10:]
    if last10 not in keys:
        keys.append(last10)
    # 79… ↔ 89… for RU mobiles
    if len(full) == 11 and full.startswith("7"):
        alt = "8" + full[1:]
        if alt not in keys:
            keys.append(alt)
    return keys


def parse_guests(guests_raw: str, score_raw: str) -> int:
    text = (guests_raw or "").strip()
    match = re.match(r"^\s*(\d+)\s*(?:/|$)", text)
    if match:
        n = int(match.group(1))
        return max(1, min(4, n))
    digits = "".join(ch for ch in (score_raw or "") if ch.isdigit())
    if digits:
        return max(1, min(4, int(digits)))
    return 1


def parse_source(value: str) -> str | None:
    text = (value or "").strip().lower()
    if "telegram" in text or text == "tg":
        return "telegram"
    if "контакт" in text or text in {"vk", "vkontakte", "вк"}:
        return "vkontakte"
    return None


def is_cancelled(status: str) -> bool:
    text = (status or "").strip().lower()
    if not text:
        return False
    return any(token in text for token in ("снят", "отмен", "cancel", "annul"))


def venue_token(location: str) -> str:
    raw = (location or "").strip()
    if not raw:
        return ""
    head = raw.split(",", 1)[0].strip()
    return head


def fetch_csv(csv_url: str) -> str:
    with urlopen(csv_url, timeout=60) as response:
        return response.read().decode("utf-8-sig")


def load_rows_from_text(text: str) -> tuple[list[dict], dict[str, str]]:
    reader = csv.DictReader(StringIO(text))
    mapping = _map_headers(reader.fieldnames)
    return list(reader), mapping


def load_rows_from_file(path: Path) -> tuple[list[dict], dict[str, str]]:
    raw = path.read_bytes()
    text = None
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    # Auto-detect delimiter by header line
    sample = text.splitlines()[0] if text else ""
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(StringIO(text), delimiter=delimiter)
    mapping = _map_headers(reader.fieldnames)
    return list(reader), mapping


def parse_booking_row(row: dict, mapping: dict[str, str], stats: Counter) -> dict | None:
    stats["rows"] += 1
    status = _cell(row, mapping, "status")
    if is_cancelled(status):
        stats["skip_cancelled"] += 1
        return None

    client_raw = _cell(row, mapping, "client_id")
    client_id = int(client_raw) if client_raw.isdigit() else None

    source = parse_source(_cell(row, mapping, "source")) or "import"

    date_raw = _cell(row, mapping, "event_date")
    time_raw = _cell(row, mapping, "event_time")
    location_raw = _cell(row, mapping, "location")
    try:
        event_date = parse_date(date_raw)
        event_time = parse_time(time_raw)
    except ValueError:
        stats["skip_bad_datetime"] += 1
        return None

    if event_date < date.today():
        stats["skip_past"] += 1
        return None

    phone = norm_phone(_cell(row, mapping, "phone"))
    if not phone:
        stats["skip_no_phone"] += 1
        return None

    guests = parse_guests(_cell(row, mapping, "guests_raw"), _cell(row, mapping, "score"))
    name = _cell(row, mapping, "name") or None
    venue = venue_token(location_raw)

    return {
        "client_id": client_id,
        "source": source,
        "name": name,
        "phone": phone,
        "event_date": event_date,
        "event_time": event_time,
        "location_raw": location_raw,
        "venue": venue,
        "guests": guests,
    }


FIND_USER_BY_PHONE_SQL = """
SELECT id, telegram_id, vk_id, source, phone, name
FROM users
WHERE
  regexp_replace(COALESCE(phone, ''), '\\D', '', 'g') = ANY(%(phones)s)
  OR RIGHT(regexp_replace(COALESCE(phone, ''), '\\D', '', 'g'), 10) = ANY(%(phones10)s)
ORDER BY
  CASE
    WHEN %(prefer)s = 'telegram' AND telegram_id IS NOT NULL THEN 0
    WHEN %(prefer)s = 'vkontakte' AND vk_id IS NOT NULL THEN 0
    ELSE 1
  END,
  CASE
    WHEN telegram_id IS NOT NULL OR vk_id IS NOT NULL THEN 0
    ELSE 1
  END,
  CASE
    WHEN regexp_replace(COALESCE(phone, ''), '\\D', '', 'g') = %(exact)s THEN 0
    ELSE 1
  END,
  id
LIMIT 10
"""

INSERT_PHONE_USER_SQL = """
INSERT INTO users (name, phone, source, created_at, last_active_at)
VALUES (%(name)s, %(phone)s, 'import', now(), now())
RETURNING id;
"""

FIND_EVENT_SQL = """
SELECT id, location, address
FROM events
WHERE format = 'proverka'
  AND event_date = %(event_date)s
  AND event_time = %(event_time)s
  AND status = 'active'
ORDER BY id
"""

UPSERT_BOOKING_SQL = """
INSERT INTO bookings (
    user_id, event_id, guests, format, source, status, created_at, updated_at
)
VALUES (
    %(user_id)s, %(event_id)s, %(guests)s, 'proverka', %(source)s, 'booked', now(), now()
)
ON CONFLICT (user_id, event_id)
WHERE status IN ('booked', 'confirmed')
DO UPDATE SET
    guests = EXCLUDED.guests,
    source = EXCLUDED.source,
    format = 'proverka',
    updated_at = now()
RETURNING id, (xmax = 0) AS inserted;
"""


def find_event_id(cur, booking: dict) -> int | None:
    cur.execute(
        FIND_EVENT_SQL,
        {"event_date": booking["event_date"], "event_time": booking["event_time"]},
    )
    rows = cur.fetchall()
    if not rows:
        return None
    if len(rows) == 1:
        return int(rows[0][0])

    venue = (booking.get("venue") or "").lower()
    loc_raw = (booking.get("location_raw") or "").lower()
    scored: list[tuple[int, int]] = []
    for event_id, location, address in rows:
        loc = (location or "").lower()
        addr = (address or "").lower()
        score = 0
        if venue and loc == venue:
            score += 100
        if venue and (venue in loc or loc in venue):
            score += 50
        if venue and venue in addr:
            score += 40
        if loc and loc in loc_raw:
            score += 30
        scored.append((score, int(event_id)))
    scored.sort(reverse=True)
    if scored and scored[0][0] > 0:
        return scored[0][1]
    # Ambiguous same-slot shows — skip rather than guess wrong venue.
    return None


def _phone_query_params(phone: str, prefer: str) -> dict:
    keys = phone_match_keys(phone)
    last10 = sorted({k[-10:] for k in keys if len(k) >= 10})
    return {
        "phones": keys,
        "phones10": last10,
        "exact": keys[0] if keys else "",
        "prefer": prefer,
    }


def find_existing_user(cur, booking: dict) -> tuple[int | None, str]:
    """Return (user_id, note). Only existing users — no inserts."""
    prefer = booking["source"] if booking["source"] in {"telegram", "vkontakte"} else ""
    cur.execute(FIND_USER_BY_PHONE_SQL, _phone_query_params(booking["phone"], prefer))
    matches = cur.fetchall()
    if not matches:
        return None, "no_user"

    # Deduplicate by id while keeping order
    seen: set[int] = set()
    unique = []
    for row in matches:
        uid = int(row[0])
        if uid in seen:
            continue
        seen.add(uid)
        unique.append(row)

    user_id, telegram_id, vk_id, _source, _phone, _name = unique[0]
    note = "phone_match"
    if len(unique) > 1:
        note = f"phone_match_ambiguous:{len(unique)}"

    channel = (
        "telegram"
        if telegram_id is not None
        else "vkontakte"
        if vk_id is not None
        else booking["source"]
        if booking["source"] in {"telegram", "vkontakte"}
        else "import"
    )
    booking["booking_source"] = channel
    return int(user_id), note


def create_phone_user(cur, booking: dict) -> int:
    cur.execute(
        INSERT_PHONE_USER_SQL,
        {"name": booking["name"], "phone": booking["phone"]},
    )
    booking["booking_source"] = (
        booking["source"] if booking["source"] in {"telegram", "vkontakte", "import"} else "import"
    )
    return int(cur.fetchone()[0])


def run_import(
    rows: list[dict],
    mapping: dict[str, str],
    *,
    apply: bool,
    database_url: str,
    create_missing: bool = False,
) -> None:
    required = ("phone", "event_date", "event_time", "location")
    missing = [key for key in required if key not in mapping]
    if missing:
        raise SystemExit(
            "CSV headers missing mapped columns: "
            + ", ".join(missing)
            + f". Seen: {sorted(mapping.values())}"
        )

    stats: Counter = Counter()
    parsed: list[dict] = []
    for row in rows:
        item = parse_booking_row(row, mapping, stats)
        if item:
            parsed.append(item)
            stats[f"src_{item['source']}"] += 1

    print("=== parse ===")
    for key, value in stats.most_common():
        print(f"  {key}: {value}")
    print(f"  ready_to_import: {len(parsed)}")

    if not parsed:
        return

    users_inserted = 0
    users_matched = 0
    users_ambiguous = 0
    bookings_inserted = 0
    bookings_updated = 0
    skip_no_event = 0
    skip_no_user = 0
    errors = 0

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            for booking in parsed:
                event_id = find_event_id(cur, booking)
                if not event_id:
                    skip_no_event += 1
                    print(
                        "  NO_EVENT "
                        f"{booking['event_date'].strftime('%d.%m.%Y')} "
                        f"{booking['event_time'].strftime('%H:%M')} "
                        f"{booking['venue'] or booking['location_raw'][:40]} "
                        f"phone={booking['phone']} salebot={booking['client_id']}"
                    )
                    continue

                user_id, note = find_existing_user(cur, booking)
                if user_id is None:
                    if create_missing and apply:
                        pass  # create below in transaction
                    else:
                        skip_no_user += 1
                        print(
                            f"  NO_USER phone={booking['phone']} "
                            f"name={booking['name'] or '—'} "
                            f"salebot={booking['client_id']}"
                        )
                        continue

                if not apply:
                    if note.startswith("phone_match_ambiguous"):
                        users_ambiguous += 1
                    print(
                        f"  DRY phone={booking['phone']} → user id={user_id} ({note}) "
                        f"event={event_id} guests={booking['guests']}"
                    )
                    continue

                try:
                    with conn.transaction():
                        created = False
                        if user_id is None and create_missing:
                            user_id = create_phone_user(cur, booking)
                            created = True
                            note = "created_phone_only"
                        elif user_id is not None:
                            cur.execute(
                                """
                                UPDATE users
                                SET name = COALESCE(NULLIF(BTRIM(name), ''), %(name)s, name),
                                    last_active_at = now()
                                WHERE id = %(user_id)s
                                """,
                                {"user_id": int(user_id), "name": booking["name"]},
                            )

                        if created:
                            users_inserted += 1
                        else:
                            users_matched += 1
                        if note.startswith("phone_match_ambiguous"):
                            users_ambiguous += 1

                        booking_source = booking.get("booking_source") or "import"
                        if booking_source not in {"telegram", "vkontakte", "import"}:
                            booking_source = "import"
                        cur.execute(
                            UPSERT_BOOKING_SQL,
                            {
                                "user_id": user_id,
                                "event_id": event_id,
                                "guests": booking["guests"],
                                "source": booking_source,
                            },
                        )
                        _booking_id, inserted = cur.fetchone()
                        if inserted:
                            bookings_inserted += 1
                        else:
                            bookings_updated += 1
                except Exception as exc:
                    errors += 1
                    print(
                        f"  ERROR phone={booking['phone']} salebot={booking['client_id']} "
                        f"{booking['event_date']} {booking['event_time']}: {exc}"
                    )

        if apply:
            conn.commit()
        else:
            conn.rollback()

    print("=== result ===")
    print(f"  mode: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"  create_missing: {create_missing}")
    print(f"  skip_no_event: {skip_no_event}")
    print(f"  skip_no_user: {skip_no_user}")
    print(f"  phone_ambiguous: {users_ambiguous}")
    if apply:
        print(f"  users_matched: {users_matched}")
        print(f"  users_inserted: {users_inserted}")
        print(f"  bookings_inserted: {bookings_inserted}")
        print(f"  bookings_updated: {bookings_updated}")
        print(f"  errors: {errors}")
    else:
        matched = len(parsed) - skip_no_event - skip_no_user
        print(f"  would_import: {matched}")
        print("  (pass --apply to write)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import Proverka bookings from Sheets CSV.")
    parser.add_argument("csv_path", nargs="?", help="Local CSV file path")
    parser.add_argument("--csv-url", help="Published Google Sheets CSV URL")
    parser.add_argument("--apply", action="store_true", help="Write to Postgres (default: dry-run)")
    parser.add_argument(
        "--create-missing",
        action="store_true",
        help="Create phone-only users if not found (default: skip — expect Salebot import already done)",
    )
    parser.add_argument("--env", default=str(ROOT / ".env"), help="Path to .env with DATABASE_URL")
    args = parser.parse_args()

    load_env_file(args.env)
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is required")

    if args.csv_url:
        text = fetch_csv(args.csv_url)
        rows, mapping = load_rows_from_text(text)
        print(f"Loaded CSV url, rows={len(rows)}")
    elif args.csv_path:
        path = Path(args.csv_path)
        if not path.exists():
            raise SystemExit(f"File not found: {path}")
        rows, mapping = load_rows_from_file(path)
        print(f"Loaded {path}, rows={len(rows)}")
    else:
        raise SystemExit("Pass csv_path or --csv-url")

    print("Mapped columns:")
    for key, col in sorted(mapping.items()):
        print(f"  {key} <- {col}")

    run_import(
        rows,
        mapping,
        apply=args.apply,
        database_url=database_url,
        create_missing=args.create_missing,
    )


if __name__ == "__main__":
    main()
