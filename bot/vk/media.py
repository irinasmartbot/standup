import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VKSystemImage:
    key: str
    path: str
    attachment: str


class VKSystemImageCache:
    """Stores pre-uploaded VK attachment ids for local system images.

    VK is faster and more reliable when the bot sends a saved attachment like
    photo123_456 instead of uploading the same file for every message.
    """

    def __init__(self, cache_path: str):
        self.cache_path = Path(cache_path)
        self._items = self._load()

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.cache_path.exists():
            return {}
        with self.cache_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {
            str(key): value
            for key, value in data.items()
            if isinstance(value, dict) and value.get("attachment")
        }

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cache_path.open("w", encoding="utf-8") as f:
            json.dump(self._items, f, ensure_ascii=False, indent=2)

    def get(self, key: str) -> str | None:
        item = self._items.get(key)
        return item.get("attachment") if item else None

    def set(self, key: str, path: str, attachment: str) -> None:
        self._items[key] = {
            "path": os.path.normpath(path),
            "attachment": attachment,
        }
        self.save()

    def all(self) -> list[VKSystemImage]:
        return [
            VKSystemImage(key=key, path=value.get("path", ""), attachment=value["attachment"])
            for key, value in self._items.items()
        ]
