import os
import logging
from telegram.ext import ApplicationBuilder, CommandHandler

from config import BOT_TOKEN, LOG_FILE
from database import db
from handlers.admin import start_handler, help_handler
from handlers.sudo import addsudo_handler, delsudo_handler, sudolist_handler
from handlers.banall import banall_handler, stopban_handler

# Configure logging without printing sensitive credentials
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

async def post_init(application):
    logger.info("Initializing Database...")
    await db.init_db()
    logger.info("Database Initialized successfully.")

def main():
    logger.info("Starting Telegram Moderation Bot...")
    
    application = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # Admin Command Handlers
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("help", help_handler))

    # Sudo Management Handlers (Owner Only)
    application.add_handler(CommandHandler("addsudo", addsudo_handler))
    application.add_handler(CommandHandler("delsudo", delsudo_handler))
    application.add_handler(CommandHandler("sudolist", sudolist_handler))

    # Moderation Action Handlers (Owner + Sudo)
    application.add_handler(CommandHandler("banall", banall_handler))
    application.add_handler(CommandHandler("stopban", stopban_handler))

    application.run_polling()

if __name__ == "__main__":
    main()
