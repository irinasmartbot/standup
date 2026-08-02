import argparse
import asyncio
import os
import ssl
import sys
import time
from pathlib import Path

import aiohttp


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.vk.client import VKAPIError, VKClient
from bot.vk.config import load_vk_settings
from bot.vk.media import VKSystemImageCache


def _connector() -> aiohttp.TCPConnector:
    try:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
        return aiohttp.TCPConnector(ssl=context)
    except Exception:
        return aiohttp.TCPConnector()


# Always keep these keyed uploads (venues / hitloto / reviews / legacy cover).
DEFAULT_IMAGE_NAMES = [
    "temple_bar.jpg",
    "escobar.jpg",
    "nebar.jpg",
    "hitloto_start.png",
    "rozygrysh_otzyv_1.jpg",
    "rozygrysh_otzyv_2.jpg",
]

DEFAULT_IMAGE_KEYS = {
    "show_cover": "фото/IMG_20220511_201818.jpg",
}

# Same exclusions as TG random covers: venues, ticket, hitloto, reviews.
# photo_2026-… — тот же арт хитлото, что hitloto_start.png (другое имя файла).
_EXCLUDED_RANDOM_NAMES = {
    "temple_bar.jpg",
    "escobar.jpg",
    "nebar.jpg",
    "ticket_template.jpg",
    "photo_2026-07-21_01-59-43.jpg",
}
# VK message photo upload is picky; keep under ~5 MB to avoid empty photo responses.
_MAX_RANDOM_PHOTO_SIZE = 5 * 1024 * 1024


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _parse_image_arg(value: str) -> tuple[str, Path]:
    if "=" not in value:
        path = Path(value)
        return path.stem, path
    key, path = value.split("=", 1)
    return key.strip(), Path(path.strip())


def _attachment_from_saved_photo(photo: dict) -> str:
    owner_id = photo["owner_id"]
    photo_id = photo["id"]
    access_key = photo.get("access_key")
    attachment = f"photo{owner_id}_{photo_id}"
    if access_key:
        attachment += f"_{access_key}"
    return attachment


def _is_random_cover_candidate(path: Path) -> bool:
    name = path.name.lower()
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        return False
    if name in _EXCLUDED_RANDOM_NAMES:
        return False
    if name.startswith("hitloto") or name.startswith("rozygrysh_otzyv"):
        return False
    if "ticket" in name:
        return False
    try:
        if path.stat().st_size > _MAX_RANDOM_PHOTO_SIZE:
            return False
    except OSError:
        return False
    return True


async def upload_image(client: VKClient, peer_id: int, path: Path) -> str:
    server = await client.api("photos.getMessagesUploadServer", peer_id=peer_id)
    upload_url = server["upload_url"]

    form = aiohttp.FormData()
    with path.open("rb") as f:
        form.add_field(
            "photo",
            f,
            filename=path.name,
            content_type="application/octet-stream",
        )
        async with aiohttp.ClientSession(connector=_connector()) as session:
            async with session.post(upload_url, data=form) as resp:
                uploaded = await resp.json(content_type=None)

    if not isinstance(uploaded, dict):
        raise RuntimeError(f"Bad upload response for {path.name}: {uploaded!r}")
    photo = uploaded.get("photo")
    server_id = uploaded.get("server")
    photo_hash = uploaded.get("hash")
    if not photo or photo in {"", "[]", "null"} or server_id is None or not photo_hash:
        raise RuntimeError(
            f"VK rejected upload for {path.name} "
            f"(size={path.stat().st_size} bytes, response={uploaded!r})"
        )

    saved = await client.api(
        "photos.saveMessagesPhoto",
        photo=photo,
        server=server_id,
        hash=photo_hash,
    )
    if not saved:
        raise RuntimeError(f"VK did not return saved photo for {path}")
    return _attachment_from_saved_photo(saved[0])


def collect_images(args) -> list[tuple[str, Path]]:
    root = _project_root()
    if args.image:
        items = [_parse_image_arg(value) for value in args.image]
    else:
        photos_dir = root / args.photos_dir
        items = [(Path(name).stem, photos_dir / name) for name in DEFAULT_IMAGE_NAMES]
        items.extend((key, Path(path)) for key, path in DEFAULT_IMAGE_KEYS.items())
        # All other show photos → random cover pool (key = filename stem).
        if photos_dir.is_dir():
            known = {path.resolve() for _, path in items if path.exists()}
            for path in sorted(photos_dir.iterdir()):
                if not path.is_file() or not _is_random_cover_candidate(path):
                    if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                        try:
                            size = path.stat().st_size
                        except OSError:
                            size = -1
                        if size > _MAX_RANDOM_PHOTO_SIZE:
                            print(f"skip too large (>5MB): {path.name} ({size} bytes)")
                    continue
                if path.resolve() in known:
                    continue
                items.append((path.stem, path))

    result = []
    for key, path in items:
        if not path.is_absolute():
            path = root / path
        if path.exists() and path.is_file():
            result.append((key, path))
        else:
            print(f"skip missing image: {path}")
    return result


async def main():
    parser = argparse.ArgumentParser(description="Upload system images to VK and cache attachment ids.")
    parser.add_argument("--peer-id", type=int, default=None, help="VK peer_id for upload context")
    parser.add_argument("--photos-dir", default="фото", help="Relative directory with default images")
    parser.add_argument(
        "--image",
        action="append",
        help="Image to upload, either path or key=path. Can be repeated.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-upload even if key already exists in cache",
    )
    args = parser.parse_args()

    settings = load_vk_settings()
    peer_id = args.peer_id or settings.admin_peer_id
    if not settings.is_configured:
        raise RuntimeError("VK_GROUP_ID and VK_GROUP_TOKEN are required.")
    if not peer_id:
        raise RuntimeError("Pass --peer-id or set VK_ADMIN_PEER_ID.")

    cache = VKSystemImageCache(settings.system_images_cache)
    client = VKClient(settings)

    ok = 0
    skipped = 0
    failed = 0
    for key, path in collect_images(args):
        if not args.force and cache.get(key):
            print(f"skip cached: {key}")
            skipped += 1
            continue
        try:
            attachment = await upload_image(client, peer_id, path)
            cache.set(key, os.path.relpath(path, _project_root()), attachment)
            print(f"{key}: {attachment}")
            ok += 1
            await asyncio.sleep(0.35)
        except (VKAPIError, RuntimeError, OSError, aiohttp.ClientError) as exc:
            failed += 1
            print(f"FAIL {key} ({path.name}): {exc}")
            continue

    print(f"done: uploaded={ok} skipped_cached={skipped} failed={failed}")


if __name__ == "__main__":
    asyncio.run(main())
