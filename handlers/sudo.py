from telegram import Update
from telegram.ext import ContextTypes
from config import BOT_OWNER_ID
from database import db

def extract_target_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        return update.message.reply_to_message.from_user.id

    if context.args:
        try:
            return int(context.args[0])
        except ValueError:
            return None
    return None

async def addsudo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != BOT_OWNER_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return

    target_id = extract_target_user_id(update, context)
    if not target_id:
        await update.message.reply_text("⚠️ Please reply to a user's message or specify a numeric User ID: `/addsudo 123456789`", parse_mode="Markdown")
        return

    added = await db.add_sudo(target_id)
    if added:
        await db.log_action(user.id, update.effective_chat.id, "ADD_SUDO", f"Added {target_id}")
        await update.message.reply_text(f"✅ User `{target_id}` added to sudo list.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠️ User `{target_id}` is already a sudo user.", parse_mode="Markdown")

async def delsudo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != BOT_OWNER_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return

    target_id = extract_target_user_id(update, context)
    if not target_id:
        await update.message.reply_text("⚠️ Please reply to a user's message or specify a numeric User ID: `/delsudo 123456789`", parse_mode="Markdown")
        return

    removed = await db.remove_sudo(target_id)
    if removed:
        await db.log_action(user.id, update.effective_chat.id, "DEL_SUDO", f"Removed {target_id}")
        await update.message.reply_text(f"✅ User `{target_id}` removed from sudo list.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠️ User `{target_id}` is not in sudo list.", parse_mode="Markdown")

async def sudolist_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != BOT_OWNER_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return

    sudoers = await db.get_sudo_list()
    if not sudoers:
        await update.message.reply_text("📋 Sudo List is currently empty.")
        return

    msg = "📋 **Sudo Users:**\n\n" + "\n".join([f"• `{uid}`" for uid in sudoers])
    await update.message.reply_text(msg, parse_mode="Markdown")
