import os
import logging

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

BOT_TOKEN = os.getenv("BOT_TOKEN")
CSV_URL = os.getenv(
    "CSV_URL",
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vQTZS9GmN4Gkffl6xrUt7W_dDIksHB7z4xAjFDVeR-x4rgWeGJLJPGVfMfY5eQESZcXfBH-ZbrUeMXh/pub?gid=907191184&single=true&output=csv",
)
BEST_CSV_URL = os.getenv(
    "BEST_CSV_URL",
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vQTZS9GmN4Gkffl6xrUt7W_dDIksHB7z4xAjFDVeR-x4rgWeGJLJPGVfMfY5eQESZcXfBH-ZbrUeMXh/pub?gid=0&single=true&output=csv",
)
MANAGER_LINK = os.getenv("MANAGER_LINK", "https://t.me/ccoverr")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/MoscowStandupShow")
MANAGER_PHONE = os.getenv("MANAGER_PHONE", "89648772410")
DB_PATH = os.getenv("DB_PATH", "bookings.db")
DATABASE_URL = os.getenv("DATABASE_URL")
EVENTS_SOURCE = os.getenv("EVENTS_SOURCE", "postgres" if DATABASE_URL else "sheets")
BOOKINGS_SOURCE = os.getenv("BOOKINGS_SOURCE", "postgres" if DATABASE_URL else "sqlite")
TICKET_TEMPLATE = os.getenv("TICKET_TEMPLATE", "photo_2023-06-26_15-06-46.jpg")
MODERATION_CHAT_ID = os.getenv("MODERATION_CHAT_ID")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "MoscowStandupShow")
AFISHA_REVIEW_URL = os.getenv(
    "AFISHA_REVIEW_URL",
    "https://afisha.yandex.ru/moscow/standup/stand-up-ot-komikov-iz-tv-i-youtube-proektov?source=rubric",
)
ROZYGRYSH_STICKER_FILE_ID = os.getenv("ROZYGRYSH_STICKER_FILE_ID", "")
SITE_URL = os.getenv("SITE_URL", "https://MoscowStandUpshow.ru")
PAID_BEST_START = "afisha_plat"
# Временно: 1 = не проверять подписку на канал (пока тестовый бот не админ канала)
ROZYGRYSH_SKIP_SUB_CHECK = os.getenv("ROZYGRYSH_SKIP_SUB_CHECK", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
# Telegram id через запятую — кто может /reset_rozygrysh даже без тестового режима.
# В тестовом режиме (ROZYGRYSH_SKIP_SUB_CHECK=1) сброс доступен всем в личке.
_TEST_ADMIN_RAW = os.getenv("TEST_ADMIN_IDS", "")
TEST_ADMIN_IDS = {
    int(part.strip())
    for part in _TEST_ADMIN_RAW.split(",")
    if part.strip().isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Create .env from .env.example and fill in the token.")

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
