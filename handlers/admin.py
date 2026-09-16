from telegram import Update
from telegram.ext import ContextTypes
from moderation import is_authorized

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    authorized = await is_authorized(user.id)
    auth_status = "Authorized (Owner/Sudo)" if authorized else "Standard User"

    text = (
        f"🤖 **Group Moderation Bot**\n\n"
        f"Your ID: `{user.id}`\n"
        f"Status: {auth_status}\n\n"
        f"Use `/help` to see available moderation commands."
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🛠 **Available Commands:**\n\n"
        "**Owner Commands:**\n"
        "• `/addsudo <user_id>` (or reply) - Add sudo user\n"
        "• `/delsudo <user_id>` (or reply) - Remove sudo user\n"
        "• `/sudolist` - List active sudo users\n\n"
        "**Owner & Sudo Commands:**\n"
        "• `/banall` - Safely mass-ban eligible members\n"
        "• `/stopban` - Abort active `/banall` job"
    )
    await update.message.reply_text(text, parse_mode="Markdown")
