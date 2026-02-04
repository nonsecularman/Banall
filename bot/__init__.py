import asyncio
import logging

from pyrogram import Client, filters
from pyrogram.errors import FloodWait, ChatAdminRequired
from pyrogram.types import Message

from .config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)

# -------------------
# Clients
# -------------------

bot = None
user = None

if Config.TELEGRAM_TOKEN:
    bot = Client(
        "bot",
        api_id=Config.TELEGRAM_APP_ID,
        api_hash=Config.TELEGRAM_APP_HASH,
        bot_token=Config.TELEGRAM_TOKEN
    )

if Config.PYRO_SESSION:
    user = Client(
        "user",
        api_id=Config.TELEGRAM_APP_ID,
        api_hash=Config.TELEGRAM_APP_HASH,
        session_string=Config.PYRO_SESSION
    )

# -------------------
# BOT COMMANDS
# -------------------

if bot:
    @bot.on_message(filters.command(["start", "ping"]))
    async def start_cmd(_, message: Message):
        await message.reply(
            "Hello 👋\n\n"
            "I am BanAll Bot.\n"
            "Make me admin and use /banall carefully."
        )

    @bot.on_message(filters.command("banall"))
    async def ban_all(_, message: Message):
        chat_id = message.chat.id
        await message.reply("🚨 BanAll started...")

        async for member in bot.get_chat_members(chat_id):
            try:
                if member.user.is_bot:
                    continue
                await bot.ban_chat_member(chat_id, member.user.id)
                await asyncio.sleep(0.2)
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except ChatAdminRequired:
                await message.reply("❌ I need ban permission.")
                return
            except Exception:
                continue

        await message.reply("✅ BanAll completed.")

# -------------------
# USER SESSION COMMANDS (OPTIONAL)
# -------------------

if user:
    @user.on_message(filters.command("banall"))
    async def user_ban_all(_, message: Message):
        chat_id = message.chat.id

        async for member in user.get_chat_members(chat_id):
            try:
                await user.ban_chat_member(chat_id, member.user.id)
                await asyncio.sleep(0.2)
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception:
                continue

# -------------------
# START CLIENTS
# -------------------

async def main():
    if bot:
        await bot.start()
        print("🤖 Bot started")

    if user:
        await user.start()
        print("👤 User session started")

    await asyncio.Event().wait()

