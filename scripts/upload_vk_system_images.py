import argparse
import asyncio
import os
import ssl
import sys
from pathlib import Path

import aiohttp


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.vk.client import VKClient
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
_EXCLUDED_RANDOM_NAMES = {
    "temple_bar.jpg",
    "escobar.jpg",
    "nebar.jpg",
    "ticket_template.jpg",
}
_MAX_RANDOM_PHOTO_SIZE = 10 * 1024 * 1024


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

    saved = await client.api(
        "photos.saveMessagesPhoto",
        photo=uploaded["photo"],
        server=uploaded["server"],
        hash=uploaded["hash"],
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
    args = parser.parse_args()

    settings = load_vk_settings()
    peer_id = args.peer_id or settings.admin_peer_id
    if not settings.is_configured:
        raise RuntimeError("VK_GROUP_ID and VK_GROUP_TOKEN are required.")
    if not peer_id:
        raise RuntimeError("Pass --peer-id or set VK_ADMIN_PEER_ID.")

    cache = VKSystemImageCache(settings.system_images_cache)
    client = VKClient(settings)

    for key, path in collect_images(args):
        attachment = await upload_image(client, peer_id, path)
        cache.set(key, os.path.relpath(path, _project_root()), attachment)
        print(f"{key}: {attachment}")


if __name__ == "__main__":
    asyncio.run(main())
