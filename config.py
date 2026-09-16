import os
import sys
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_OWNER_ID_RAW = os.getenv("BOT_OWNER_ID")
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/bot.db")
LOG_FILE = os.getenv("LOG_FILE", "logs/bot.log")

if not BOT_TOKEN:
    sys.exit("CRITICAL ERROR: 'BOT_TOKEN' is missing in environment variables.")

if not BOT_OWNER_ID_RAW:
    sys.exit("CRITICAL ERROR: 'BOT_OWNER_ID' is missing in environment variables.")

try:
    BOT_OWNER_ID = int(BOT_OWNER_ID_RAW)
except ValueError:
    sys.exit("CRITICAL ERROR: 'BOT_OWNER_ID' must be a valid numeric integer.")
