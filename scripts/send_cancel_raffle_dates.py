#!/usr/bin/env python3
"""Разовая отправка: отмена шоу + меню дат BEST для розыгрыша (без сегодня).

Перед отправкой сбрасывает флаг/активные брони розыгрыша у получателя,
чтобы можно было сразу выбрать новую дату без алертов.

Примеры на сервере из /home/standup/app:

  venv/bin/python scripts/send_cancel_raffle_dates.py --test theastarta
  venv/bin/python scripts/send_cancel_raffle_dates.py --send LuzzettaA LynitaAd catemood
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

CANCEL_TEXT = (
    "Добрый день! По техническим обстоятельствам завтрашнее мероприятие "
    "Escobar 20:00 отменяется, Вы можете выбрать любую другую дату."
)

DATES_CAPTION = "Выбирай дату мероприятия в рамках розыгрыша 👇"


def _lookup_telegram_ids(usernames: list[str]) -> list[dict]:
    import os

    import psycopg
    from psycopg.rows import dict_row

    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit("DATABASE_URL не задан")

    cleaned = [u.strip().lstrip("@") for u in usernames if u and u.strip()]
    rows: list[dict] = []
    with psycopg.connect(url, row_factory=dict_row, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            for uname in cleaned:
                cur.execute(
                    """
                    SELECT id, telegram_id, username, name, phone
                    FROM users
                    WHERE lower(username) = lower(%s)
                    ORDER BY telegram_id NULLS LAST, id
                    LIMIT 5
                    """,
                    (uname,),
                )
                found = cur.fetchall()
                if not found:
                    rows.append(
                        {
                            "username": uname,
                            "telegram_id": None,
                            "name": None,
                            "error": "не найден в users",
                        }
                    )
                    continue
                with_tg = [r for r in found if r.get("telegram_id")]
                pick = with_tg[0] if with_tg else found[0]
                rows.append(
                    {
                        "username": pick.get("username") or uname,
                        "telegram_id": pick.get("telegram_id"),
                        "name": pick.get("name"),
                        "user_id": pick.get("id"),
                        "error": None
                        if pick.get("telegram_id")
                        else "нет telegram_id",
                    }
                )
    return rows


def _prepare_raffle_rebook(telegram_id: int) -> dict:
    """Снять активные брони розыгрыша и флаг used — чтобы выбрать новую дату."""
    from bot.db.crud import reset_raffle_for_user

    return reset_raffle_for_user(int(telegram_id))


async def _send_one(bot, telegram_id: int) -> None:
    from bot.handlers.rozygrysh import _dates_kb

    prep = _prepare_raffle_rebook(telegram_id)
    print(
        f"    raffle reset: used_cleared={prep.get('rozygrysh_used_cleared')} "
        f"bookings_cancelled={prep.get('bookings_cancelled')}"
    )

    await bot.send_message(chat_id=int(telegram_id), text=CANCEL_TEXT)
    markup, dates = await _dates_kb()
    if not dates:
        await bot.send_message(
            chat_id=int(telegram_id),
            text="Пока нет доступных дат для выбора 😔",
        )
        return
    # Не пишем raffle_nav — cleanup после шоу иначе сотрёт меню.
    await bot.send_message(
        chat_id=int(telegram_id),
        text=DATES_CAPTION,
        reply_markup=markup,
    )


async def _run(usernames: list[str], *, dry_run: bool) -> int:
    import os

    from aiogram import Bot

    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN не задан")

    looked = _lookup_telegram_ids(usernames)
    print("Получатели:")
    for row in looked:
        print(
            f"  @{row.get('username')} · tg={row.get('telegram_id')} · "
            f"name={row.get('name')!r} · {row.get('error') or 'ok'}"
        )

    missing = [r for r in looked if not r.get("telegram_id")]
    if missing:
        print("ОШИБКА: не у всех есть telegram_id — отправка отменена.")
        return 2

    if dry_run:
        print("dry-run: ничего не отправляли")
        return 0

    bot = Bot(token=token)
    try:
        for row in looked:
            tid = int(row["telegram_id"])
            print(f"Отправляю @{row['username']} ({tid})…")
            await _send_one(bot, tid)
            print(f"  ok @{row['username']}")
    finally:
        await bot.session.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--test", nargs="+", metavar="USERNAME", help="Тест одному/нескольким")
    g.add_argument("--send", nargs="+", metavar="USERNAME", help="Боевая отправка")
    g.add_argument("--lookup", nargs="+", metavar="USERNAME", help="Только найти в БД")
    args = p.parse_args()

    if args.lookup:
        for row in _lookup_telegram_ids(args.lookup):
            print(row)
        return 0
    if args.test:
        return asyncio.run(_run(args.test, dry_run=False))
    return asyncio.run(_run(args.send, dry_run=False))


if __name__ == "__main__":
    raise SystemExit(main())
