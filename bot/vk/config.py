import os
from dataclasses import dataclass


def _load_env_file(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    _load_env_file()


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class VKSettings:
    enabled: bool
    group_id: int | None
    group_token: str
    api_version: str
    admin_peer_id: int | None
    manager_link: str
    community_link: str
    system_images_cache: str

    @property
    def is_configured(self) -> bool:
        return bool(self.group_id and self.group_token)


def load_vk_settings() -> VKSettings:
    return VKSettings(
        enabled=_as_bool(os.getenv("VK_ENABLED"), default=False),
        group_id=_as_int(os.getenv("VK_GROUP_ID")),
        group_token=os.getenv("VK_GROUP_TOKEN", "").strip(),
        api_version=os.getenv("VK_API_VERSION", "5.199").strip() or "5.199",
        admin_peer_id=_as_int(os.getenv("VK_ADMIN_PEER_ID")),
        manager_link=os.getenv("VK_MANAGER_LINK", "https://vk.com/").strip(),
        community_link=os.getenv("VK_COMMUNITY_LINK", "https://vk.com/").strip(),
        system_images_cache=os.getenv(
            "VK_SYSTEM_IMAGES_CACHE",
            "data/storage/vk_system_images.json",
        ).strip(),
    )
