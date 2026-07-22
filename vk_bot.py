import asyncio
import logging

from bot.vk.app import VKBotApp
from bot.vk.client import VKClient
from bot.vk.config import load_vk_settings


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    settings = load_vk_settings()
    if not settings.enabled:
        raise RuntimeError("VK_ENABLED is not set. Put VK_ENABLED=1 into .env for the VK test bot.")
    if not settings.is_configured:
        raise RuntimeError("VK_GROUP_ID and VK_GROUP_TOKEN are required for the VK test bot.")

    app = VKBotApp(VKClient(settings), settings)
    await app.run()


if __name__ == "__main__":
    asyncio.run(main())
