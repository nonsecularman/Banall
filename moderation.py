from telegram import Update, ChatMember
from telegram.ext import ContextTypes
from config import BOT_OWNER_ID
from database import db

async def is_authorized(user_id: int) -> bool:
    if user_id == BOT_OWNER_ID:
        return True
    return await db.is_sudo(user_id)

async def check_bot_permissions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[bool, str]:
    chat = update.effective_chat
    if not chat or chat.type not in ["group", "supergroup"]:
        return False, "❌ This command can only be executed inside groups or supergroups."

    bot_member = await chat.get_member(context.bot.id)
    if bot_member.status != ChatMember.ADMINISTRATOR:
        return False, "❌ I must be an Administrator in this group to perform moderation actions."

    if not getattr(bot_member, "can_restrict_members", False):
        return False, "❌ I don't have permission to restrict/ban members in this group."

    return True, ""

async def is_protected_user(chat_id: int, user_id: int, bot_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if user_id in [bot_id, BOT_OWNER_ID]:
        return True
    if await db.is_sudo(user_id):
        return True

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
            return True
    except Exception:
        pass

    return False
