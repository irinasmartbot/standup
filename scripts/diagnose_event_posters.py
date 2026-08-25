#!/usr/bin/env python3
"""Диагностика постеров афиши на VPS.

  cd /home/standup/app   # или vk-app
  venv/bin/python scripts/diagnose_event_posters.py
"""

from __future__ import annotations

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


async def main() -> int:
    from bot.services.sheets import load_events
    from bot.utils.event_poster import download_poster_bytes

    print("certifi:", end=" ")
    try:
        import certifi

        print(certifi.where())
    except Exception as exc:
        print("MISSING", exc)

    print("Pillow WEBP:", end=" ")
    try:
        from PIL import Image

        print(Image.registered_extensions().get(".webp", "NO"))
    except Exception as exc:
        print("FAIL", exc)

    for fmt in ("best", "proverka", "hitloto"):
        try:
            events = await load_events(fmt)
        except Exception as exc:
            print(f"[{fmt}] load_events FAILED: {exc}")
            continue
        with_url = [e for e in events if (e.get("image") or "").strip()]
        print(f"[{fmt}] events={len(events)} with_image_url={len(with_url)}")
        for event in with_url[:3]:
            url = (event.get("image") or "").strip()
            print(f"  try {event.get('date')} {event.get('location')}: {url[:100]}")
            try:
                data = await download_poster_bytes(url)
                print(f"    OK bytes={len(data)} jpeg_sig={data[:3].hex()}")
            except Exception as exc:
                print(f"    FAIL {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
