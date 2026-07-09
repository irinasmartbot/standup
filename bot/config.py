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
MANAGER_LINK = os.getenv("MANAGER_LINK", "https://t.me/ccoverr")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/MoscowStandupShow")
MANAGER_PHONE = os.getenv("MANAGER_PHONE", "89648772410")
DB_PATH = os.getenv("DB_PATH", "bookings.db")
TICKET_TEMPLATE = os.getenv("TICKET_TEMPLATE", "photo_2023-06-26_15-06-46.jpg")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Create .env from .env.example and fill in the token.")

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
