import asyncio
import logging

from bot.config import DATABASE_URL, EVENTS_SOURCE
from bot.db.analytics import ensure_analytics_tables
from bot.vk.app import VKBotApp
from bot.vk.client import VKClient
from bot.vk.config import load_vk_settings
from bot.vk.reminders import vk_reminder_loop


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    settings = load_vk_settings()
    if not settings.enabled:
        raise RuntimeError("VK_ENABLED is not set. Put VK_ENABLED=1 into .env for the VK test bot.")
    if not settings.is_configured:
        raise RuntimeError("VK_GROUP_ID and VK_GROUP_TOKEN are required for the VK test bot.")
    if EVENTS_SOURCE != "postgres" or not DATABASE_URL:
        raise RuntimeError(
            "VK bot requires EVENTS_SOURCE=postgres and DATABASE_URL. "
            "Google Sheets is not used for VK."
        )

    ensure_analytics_tables()
    logger.info("VK events source=%s", EVENTS_SOURCE)
    client = VKClient(settings)
    asyncio.create_task(
        vk_reminder_loop(
            client,
            community_link=settings.community_link,
            manager_link=settings.manager_link,
        )
    )
    app = VKBotApp(client, settings)
    await app.run()


if __name__ == "__main__":
    asyncio.run(main())
