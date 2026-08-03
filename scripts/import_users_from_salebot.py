"""Import Salebot CSV exports into PostgreSQL users.

Rules:
- one client per messenger id (telegram_id OR vk_id);
- do NOT merge TG/VK by phone;
- phone/name/username are optional extras.

Default is dry-run (no DB writes). Pass --apply to upsert into Postgres.

Examples:
  python scripts/import_users_from_salebot.py
  python scripts/import_users_from_salebot.py база/*.csv
  python scripts/import_users_from_salebot.py --apply
"""

from __future__ import annotations

import argparse
import csv
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
# Cyrillic folder name via escapes — safer on Windows source encodings.
DEFAULT_DIR = ROOT / "\u0431\u0430\u0437\u0430"

ID_KEY = "\u0418\u0434\u0435\u043d\u0442\u0438\u0444\u0438\u043a\u0430\u0442\u043e\u0440 \u0432\u043d\u0443\u0442\u0440\u0438 \u043c\u0435\u0441\u0441\u0435\u043d\u0434\u0436\u0435\u0440\u0430"
MSG_KEY = "\u041c\u0435\u0441\u0441\u0435\u043d\u0434\u0436\u0435\u0440"
PHONE_KEYS = (
    "phone_reg [client]",
    "phone [client]",
    "Phone",
    "phone [order]",
    "phone_reg",
)
NAME_KEYS = (
    "name_reg [client]",
    "\u0438\u043c\u044f [order]",
    "\u0418\u043c\u044f",
)
USERNAME_KEY = "tg_username [client]"

UPSERT_TG_SQL = """
INSERT INTO users (telegram_id, username, name, phone, source, created_at, last_active_at)
VALUES (%(telegram_id)s, %(username)s, %(name)s, %(phone)s, %(source)s, %(now)s, %(now)s)
ON CONFLICT (telegram_id)
DO UPDATE SET
    username = COALESCE(EXCLUDED.username, users.username),
    name = COALESCE(EXCLUDED.name, users.name),
    phone = COALESCE(EXCLUDED.phone, users.phone),
    last_active_at = EXCLUDED.last_active_at
RETURNING (xmax = 0) AS inserted;
"""

UPSERT_VK_SQL = """
INSERT INTO users (vk_id, username, name, phone, source, created_at, last_active_at)
VALUES (%(vk_id)s, %(username)s, %(name)s, %(phone)s, %(source)s, %(now)s, %(now)s)
ON CONFLICT (vk_id)
DO UPDATE SET
    username = COALESCE(EXCLUDED.username, users.username),
    name = COALESCE(EXCLUDED.name, users.name),
    phone = COALESCE(EXCLUDED.phone, users.phone),
    last_active_at = EXCLUDED.last_active_at
RETURNING (xmax = 0) AS inserted;
"""


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


def open_csv(path: Path):
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1251", "utf-8"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise UnicodeDecodeError("utf-8", raw, 0, 1, f"Cannot decode {path}")
    # csv needs an iterator of lines
    return csv.DictReader(text.splitlines(), delimiter=";")


def norm_phone(row: dict) -> str | None:
    for key in PHONE_KEYS:
        value = (row.get(key) or "").strip()
        if not value:
            continue
        digits = "".join(ch for ch in value if ch.isdigit())
        if len(digits) >= 10:
            return digits
    return None


def norm_name(row: dict) -> str | None:
    for key in NAME_KEYS:
        value = (row.get(key) or "").strip()
        if value:
            return value
    return None


def norm_username(row: dict, messenger: str) -> str | None:
    if messenger != "telegram":
        return None
    value = (row.get(USERNAME_KEY) or "").strip()
    if not value:
        return None
    return value.lstrip("@") or None


def norm_messenger(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    if "telegram" in text or text == "tg":
        return "telegram"
    # "вконтакте" / "контакт"
    if "\u043a\u043e\u043d\u0442\u0430\u043a\u0442" in text or text in {"vk", "vkontakte"}:
        return "vkontakte"
    return None


def prefer(old: str | None, new: str | None) -> str | None:
    return old or new


def parse_files(paths: list[Path]) -> tuple[list[dict], Counter]:
    """Parse CSVs into unique clients keyed by (messenger, id)."""
    clients: dict[tuple[str, int], dict] = {}
    stats: Counter = Counter()

    for path in paths:
        stats["files"] += 1
        reader = open_csv(path)
        for row in reader:
            stats["rows"] += 1
            raw_id = (row.get(ID_KEY) or "").strip()
            messenger = norm_messenger(row.get(MSG_KEY))
            if not raw_id:
                stats["skip_empty_id"] += 1
                continue
            if not raw_id.isdigit():
                stats["skip_bad_id"] += 1
                continue
            if messenger is None:
                stats["skip_bad_messenger"] += 1
                continue

            mid = int(raw_id)
            key = (messenger, mid)
            phone = norm_phone(row)
            name = norm_name(row)
            username = norm_username(row, messenger)

            if key in clients:
                stats["duplicate_rows_merged"] += 1
                current = clients[key]
                current["phone"] = prefer(current.get("phone"), phone)
                current["name"] = prefer(current.get("name"), name)
                current["username"] = prefer(current.get("username"), username)
                continue

            clients[key] = {
                "messenger": messenger,
                "messenger_id": mid,
                "telegram_id": mid if messenger == "telegram" else None,
                "vk_id": mid if messenger == "vkontakte" else None,
                "username": username,
                "name": name,
                "phone": phone,
                "source": "import",
            }
            stats[f"unique_{messenger}"] += 1
            if phone:
                stats[f"with_phone_{messenger}"] += 1
            else:
                stats[f"without_phone_{messenger}"] += 1

    return list(clients.values()), stats


def apply_users(database_url: str, clients: list[dict]) -> Counter:
    result: Counter = Counter()
    now = datetime.now()
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            for client in clients:
                params = {
                    "telegram_id": client["telegram_id"],
                    "vk_id": client["vk_id"],
                    "username": client["username"],
                    "name": client["name"],
                    "phone": client["phone"],
                    "source": client["source"],
                    "now": now,
                }
                if client["messenger"] == "telegram":
                    cur.execute(UPSERT_TG_SQL, params)
                else:
                    cur.execute(UPSERT_VK_SQL, params)
                inserted = bool(cur.fetchone()[0])
                if inserted:
                    result["inserted"] += 1
                    result[f"inserted_{client['messenger']}"] += 1
                else:
                    result["updated"] += 1
                    result[f"updated_{client['messenger']}"] += 1
        conn.commit()
    return result


def discover_default_csvs() -> list[Path]:
    """Find Salebot CSVs even when Windows mangles Cyrillic folder names."""
    if DEFAULT_DIR.is_dir():
        paths = sorted(DEFAULT_DIR.glob("*.csv"))
        if paths:
            return paths
    for base in (Path.cwd(), ROOT):
        paths = sorted(base.glob("**/report_*.csv"))
        if paths:
            return paths
        for child in base.iterdir():
            if child.is_dir():
                nested = sorted(child.glob("report_*.csv"))
                if nested:
                    return nested
    return []


def resolve_paths(raw_paths: list[str]) -> list[Path]:
    if not raw_paths:
        paths = discover_default_csvs()
        if not paths:
            raise SystemExit(
                "No Salebot CSV found. Put report_*.csv under ./база or pass file paths."
            )
        return paths

    paths: list[Path] = []
    for item in raw_paths:
        path = Path(item)
        if path.is_dir():
            paths.extend(sorted(path.glob("*.csv")))
        else:
            matches = sorted(Path().glob(item)) if any(ch in item for ch in "*?[") else [path]
            for match in matches:
                if match.is_file():
                    paths.append(match)
    if not paths:
        raise SystemExit("No CSV files matched.")
    return paths


def print_stats(stats: Counter, apply_stats: Counter | None = None) -> None:
    print("=== parse ===")
    print(f"files: {stats['files']}")
    print(f"rows: {stats['rows']}")
    print(f"unique telegram: {stats['unique_telegram']}")
    print(f"unique vkontakte: {stats['unique_vkontakte']}")
    print(f"with phone telegram: {stats['with_phone_telegram']}")
    print(f"with phone vkontakte: {stats['with_phone_vkontakte']}")
    print(f"without phone telegram: {stats['without_phone_telegram']}")
    print(f"without phone vkontakte: {stats['without_phone_vkontakte']}")
    print(f"skip empty id: {stats['skip_empty_id']}")
    print(f"skip bad id: {stats['skip_bad_id']}")
    print(f"skip bad messenger: {stats['skip_bad_messenger']}")
    print(f"duplicate rows merged: {stats['duplicate_rows_merged']}")
    if apply_stats is not None:
        print("=== apply ===")
        print(f"inserted: {apply_stats['inserted']}")
        print(f"updated: {apply_stats['updated']}")
        print(f"inserted telegram: {apply_stats['inserted_telegram']}")
        print(f"inserted vkontakte: {apply_stats['inserted_vkontakte']}")
        print(f"updated telegram: {apply_stats['updated_telegram']}")
        print(f"updated vkontakte: {apply_stats['updated_vkontakte']}")


def main() -> None:
    load_env_file(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Import Salebot users CSV into PostgreSQL.")
    parser.add_argument(
        "paths",
        nargs="*",
        help="CSV files or folders (default: ./база/*.csv)",
    )
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write to Postgres. Without this flag only dry-run stats are printed.",
    )
    args = parser.parse_args()

    paths = resolve_paths(args.paths)
    clients, stats = parse_files(paths)
    print_stats(stats)

    if not args.apply:
        print("dry-run only (pass --apply to write into DATABASE_URL)")
        return

    if not args.database_url:
        raise SystemExit("DATABASE_URL is not set. Add it to .env or pass --database-url.")

    apply_stats = apply_users(args.database_url, clients)
    print_stats(stats, apply_stats)


if __name__ == "__main__":
    main()
