import asyncio
import logging
from telegram import Update, ChatMember
from telegram.error import RetryAfter, Forbidden, BadRequest, TelegramError
from telegram.ext import ContextTypes

from database import db
from moderation import is_authorized, check_bot_permissions, is_protected_user

logger = logging.getLogger(__name__)

# Memory flags for active job cancellations per chat
CANCELLATION_REQUESTS: set[int] = set()

async def banall_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    if not await is_authorized(user.id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return

    is_valid, err_msg = await check_bot_permissions(update, context)
    if not is_valid:
        await update.message.reply_text(err_msg)
        return

    if await db.is_job_active(chat.id):
        await update.message.reply_text("⚠️ Ban-All is already running in this group.")
        return

    # Lock job in DB
    if not await db.set_job_active(chat.id, user.id):
        await update.message.reply_text("⚠️ Ban-All is already running.")
        return

    CANCELLATION_REQUESTS.discard(chat.id)
    await db.log_action(user.id, chat.id, "BANALL_START", "Initiated safe mass ban process")

    status_msg = await update.message.reply_text(
        "🔨 **Ban-All Started**\n\n"
        "Processed: 0\n"
        "Banned: 0\n"
        "Skipped: 0\n"
        "Errors: 0\n\n"
        "Telegram limits are being respected.",
        parse_mode="Markdown"
    )

    processed = 0
    banned = 0
    skipped = 0
    errors = 0

    try:
        # Collect available chat members safely
        target_members = []
        async for member in chat.get_members():
            target_members.append(member)

        bot_id = context.bot.id

        for member in target_members:
            if chat.id in CANCELLATION_REQUESTS:
                logger.info(f"Ban-all process cancelled by user in chat {chat.id}")
                break

            processed += 1
            target_id = member.user.id

            # Check protected users
            if member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER, ChatMember.BANNED] or member.user.is_deleted:
                skipped += 1
                continue

            if await is_protected_user(chat.id, target_id, bot_id, context):
                skipped += 1
                continue

            # Execute ban attempt with FloodWait handling loop
            ban_success = False
            while not ban_success:
                try:
                    await chat.ban_member(user_id=target_id)
                    banned += 1
                    ban_success = True
                    # Controlled rate-limiting delay between requests
                    await asyncio.sleep(0.2)
                except RetryAfter as e:
                    logger.warning(f"Telegram FloodWait encountered: Sleeping for {e.retry_after + 2}s")
                    await db.log_action(user.id, chat.id, "FLOODWAIT", f"RetryAfter {e.retry_after}s")
                    await asyncio.sleep(e.retry_after + 2)
                except (Forbidden, BadRequest) as e:
                    logger.error(f"Failed to ban user {target_id}: {e.message}")
                    errors += 1
                    ban_success = True  # Break inner retry loop on non-retriable error
                except TelegramError as e:
                    logger.error(f"Telegram Error for user {target_id}: {e}")
                    errors += 1
                    ban_success = True

            # Edit status message periodically
            if processed % 25 == 0 or processed == len(target_members):
                try:
                    await status_msg.edit_text(
                        f"🔨 **Ban-All In Progress**\n\n"
                        f"Processed: {processed}\n"
                        f"Banned: {banned}\n"
                        f"Skipped: {skipped}\n"
                        f"Errors: {errors}\n\n"
                        f"Telegram limits are being respected.",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass

    except Exception as e:
        logger.exception(f"Unexpected error in banall job: {e}")
        errors += 1
    finally:
        await db.set_job_inactive(chat.id)
        is_cancelled = chat.id in CANCELLATION_REQUESTS
        CANCELLATION_REQUESTS.discard(chat.id)

        header = "⏹️ **Ban-All Stopped**" if is_cancelled else "✅ **Ban-All Finished**"
        await db.log_action(user.id, chat.id, "BANALL_END", f"Completed. Banned: {banned}, Skipped: {skipped}, Errors: {errors}")

        try:
            await status_msg.edit_text(
                f"{header}\n\n"
                f"Processed: {processed}\n"
                f"Banned: {banned}\n"
                f"Skipped: {skipped}\n"
                f"Errors: {errors}",
                parse_mode="Markdown"
            )
        except Exception:
            await chat.send_message(
                f"{header}\n\nProcessed: {processed} | Banned: {banned} | Skipped: {skipped} | Errors: {errors}"
            )

async def stopban_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    if not await is_authorized(user.id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return

    if not await db.is_job_active(chat.id):
        await update.message.reply_text("⚠️ No active Ban-All job is running in this group.")
        return

    CANCELLATION_REQUESTS.add(chat.id)
    await update.message.reply_text("🛑 Stopping Ban-All process safely... Please wait for current iteration to complete.")
