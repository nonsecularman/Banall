"""
Telegram Bot API se kisi group ke saare members ki list seedha nahi mil sakti
(privacy restriction — bots sirf admins ki list ya ek specific known user_id
check kar sakte hain). Isliye members ki list khud DB mein banani padti hai:
jab bhi koi user group mein message bhejta hai ya naya join karta hai, uski
ID save kar lete hain. /banall isi tracked list pe kaam karta hai.

⚠️ ZAROORI SHARTEIN (warna tracking kaam nahi karegi):
1. bot.py me run_polling(allowed_updates=Update.ALL_TYPES) hona chahiye —
   default me Telegram "chat_member" updates nahi bhejta.
2. Bot ko group me ADMIN banana chahiye — admin na hone par bot ko normal
   messages milte hi nahi (privacy mode), to tracking khali rahegi.
   (Ya BotFather me /setprivacy -> Disable kar do.)
"""

import logging
from telegram import Update
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from database import db

logger = logging.getLogger(__name__)


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or user.is_bot:
        return
    if chat.type not in ("group", "supergroup"):
        return

    try:
        await db.track_member(chat.id, user.id)
    except Exception as e:
        logger.error(f"Failed to track member {user.id} in {chat.id}: {e}")


async def on_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Naye join hone waale ya status update hone waale members ko bhi track karo."""
    result = update.chat_member
    if not result:
        return

    chat_id = result.chat.id
    new_status = result.new_chat_member.status
    target_user = result.new_chat_member.user

    if target_user.is_bot:
        return

    try:
        if new_status in ("left", "kicked"):
            await db.remove_tracked_member(chat_id, target_user.id)
        else:
            await db.track_member(chat_id, target_user.id)
    except Exception as e:
        logger.error(f"Failed to update tracked member {target_user.id} in {chat_id}: {e}")


async def trackstats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Diagnostics command: /trackstats
    Dikhaata hai ki DB me kitne members track hue hain, aur admins ko
    backfill karta hai (getChatAdministrators Bot API me allowed hai).
    """
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or not update.message:
        return
    if chat.type not in ("group", "supergroup"):
        return

    before = await db.get_tracked_members(chat.id)

    backfilled = 0
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        for admin in admins:
            if not admin.user.is_bot:
                await db.track_member(chat.id, admin.user.id)
                backfilled += 1
    except Exception as e:
        logger.error(f"Admin backfill failed in chat {chat.id}: {e}")

    after = await db.get_tracked_members(chat.id)

    await update.message.reply_text(
        "📊 **Member Tracking Stats**\n\n"
        f"Pehle tracked: {len(before)}\n"
        f"Admins backfilled: {backfilled}\n"
        f"Ab total tracked: {len(after)}\n\n"
        "ℹ️ Bot API poora member list nahi de sakta. DB me wahi users aate hain "
        "jo bot ke saamne message bhejte hain ya join karte hain.",
        parse_mode="Markdown",
    )


def register_tracker_handlers(app: Application):
    # 🔴 FIX: ~filters.COMMAND zaroori hai — warna ye MessageHandler command
    # messages ko bhi pakad leta hai, aur PTB me same handler group me se
    # sirf EK handler chalta hai. Iske bina /banall, /stopban jaise commands
    # kabhi fire hi nahi hote (ya tracker ke baad register karne parte hain).
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, on_group_message))
    # Join/leave/promote/demote events pe bhi update karo
    # (bot.py me allowed_updates=Update.ALL_TYPES hona ANIVARYA hai)
    app.add_handler(ChatMemberHandler(on_chat_member_update, ChatMemberHandler.CHAT_MEMBER))
    # Diagnostics command bhi yahin register kar dete hain (ek jagah sab)
    app.add_handler(CommandHandler("trackstats", trackstats_handler))
