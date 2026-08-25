import asyncio
import logging
import traceback

from bot.config import DATABASE_URL, EVENTS_SOURCE
from bot.db.analytics import ensure_analytics_tables
from bot.db.crud import ensure_help_tables, ensure_raffle_tables
from bot.db.mailing import ensure_mailing_tables
from bot.utils.tech_alerts import format_alert, notify_tech_sync
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
    ensure_help_tables()
    ensure_raffle_tables()
    ensure_mailing_tables()
    from bot.db.crud import ensure_offline_gift_tables

    ensure_offline_gift_tables()
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
    try:
        asyncio.run(main())
    except Exception as exc:
        logger.exception("VK bot crashed")
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        notify_tech_sync(
            format_alert("VK bot: процесс упал", tb, source="standup-vk-bot"),
            key="vk_crash",
            throttle_sec=60,
        )
        raise
