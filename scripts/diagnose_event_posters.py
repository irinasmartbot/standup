#!/usr/bin/env python3
"""Диагностика постеров афиши и VK-кэша картинок на VPS.

  cd /home/standup/app   # или vk-app
  venv/bin/python scripts/diagnose_event_posters.py
  venv/bin/python scripts/diagnose_event_posters.py --vk-upload --peer-id YOUR_VK_ID
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


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vk-upload", action="store_true")
    parser.add_argument("--peer-id", type=int, default=0)
    args = parser.parse_args()

    from bot.services.sheets import load_events
    from bot.utils.event_poster import download_poster_bytes
    from bot.vk.media import (
        _photos_dirs,
        load_vk_system_images_cache,
        pick_random_cover_file,
        resolve_vk_system_images_cache_path,
    )

    print("cwd:", Path.cwd())
    print("root:", ROOT)
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

    print("photo dirs:")
    for path in _photos_dirs():
        files = list(path.glob("*"))[:5]
        print(f"  {path} files={len(list(path.iterdir())) if path.is_dir() else 0} sample={[p.name for p in files]}")
    cover = pick_random_cover_file()
    print("pick_random_cover_file:", cover)

    cache_path = resolve_vk_system_images_cache_path("data/storage/vk_system_images.json")
    cache = load_vk_system_images_cache(str(cache_path))
    print(f"system images cache: {cache_path} items={len(cache.all())}")
    for key in ("temple_bar", "escobar", "nebar", "show_cover", "hitloto_start"):
        print(f"  cache[{key}]={'yes' if cache.get(key) else 'NO'}")

    event_cache = cache_path.with_name("vk_event_images.json")
    print(f"event images cache exists: {event_cache.exists()} path={event_cache}")

    sample_url = ""
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
            if not sample_url:
                sample_url = url
            print(f"  try {event.get('date')} {event.get('location')}: {url[:100]}")
            try:
                data = await download_poster_bytes(url)
                print(f"    OK bytes={len(data)} jpeg_sig={data[:3].hex()}")
            except Exception as exc:
                print(f"    FAIL {exc}")

    if args.vk_upload:
        if not args.peer_id:
            print("--vk-upload needs --peer-id")
            return 2
        if not sample_url:
            print("No sample image_url to upload")
            return 2
        from bot.utils.event_poster import download_poster_bytes
        from bot.vk.client import VKClient
        from bot.vk.config import load_vk_settings

        settings = load_vk_settings()
        client = VKClient(settings)
        data = await download_poster_bytes(sample_url)
        print(f"VK upload test peer_id={args.peer_id} bytes={len(data)}")
        try:
            att = await client.upload_message_photo(args.peer_id, data, filename="diagnose.jpg")
            print("VK upload OK:", att)
            mid = await client.send_message(
                args.peer_id,
                "Диагностика: тестовый постер афиши",
                attachment=att,
            )
            print("VK send OK message_id:", mid)
        except Exception as exc:
            print("VK upload/send FAIL:", exc)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
