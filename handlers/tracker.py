"""
Telegram Bot API se kisi group ke saare members ki list seedha nahi mil sakti
(privacy restriction — bots sirf admins ki list ya ek specific known user_id
check kar sakte hain). Isliye members ki list khud DB mein banani padti hai:
jab bhi koi user group mein message bhejta hai ya naya join karta hai, uski
ID save kar lete hain. /banall isi tracked list pe kaam karta hai.

register_tracker_handlers(app) ko apni main bot file mein call kar dena,
jaha application build hoti hai.
"""

import logging
from telegram import Update
from telegram.ext import (
    Application,
    ChatMemberHandler,
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


def register_tracker_handlers(app: Application):
    # Har normal group message pe sender ko track karo
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, on_group_message))
    # Join/leave/promote/demote events pe bhi update karo
    app.add_handler(ChatMemberHandler(on_chat_member_update, ChatMemberHandler.CHAT_MEMBER))

