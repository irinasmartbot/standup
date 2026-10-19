"""Human-readable Telegram transport journal.

Usage on the bot VPS:

  ssh standup@31.128.47.4
  cd /home/standup/app
  ./venv/bin/python scripts/telegram_transport_report.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.utils.telegram_relay import (  # noqa: E402
    format_transport_summary,
    telegram_transport_journal_path,
    telegram_transport_summary_path,
)


def main() -> int:
    summary_path = telegram_transport_summary_path()
    journal_path = telegram_transport_journal_path()
    if summary_path.is_file():
        text = summary_path.read_text(encoding="utf-8")
    else:
        text = format_transport_summary()
    print(text.rstrip())
    print()
    print(f"Сводка: {summary_path}")
    print(f"Журнал: {journal_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
