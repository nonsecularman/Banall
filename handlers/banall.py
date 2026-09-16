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

# PTB v21.3+ me constant ka naam "OWNER" hai, purane versions me "CREATOR".
# Dono ke saath kaam kare isliye getattr fallback:
_OWNER_STATUS = getattr(ChatMember, "OWNER", getattr(ChatMember, "CREATOR", "creator"))
_SKIP_STATUSES = {
    ChatMember.ADMINISTRATOR,
    _OWNER_STATUS,
    ChatMember.BANNED,
    ChatMember.LEFT,
}


async def _backfill_admins(chat, context) -> int:
    """
    getChatAdministrators se admins ko tracked list me add karta hai.
    (Bot API full member list nahi deta, par admins ki list allowed hai —
    kam se kam wo DB me aa jayenge.)
    """
    added = 0
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        for admin in admins:
            u = admin.user
            if not u.is_bot:
                await db.track_member(chat.id, u.id)
                added += 1
    except TelegramError as e:
        logger.error(f"Admin backfill failed in chat {chat.id}: {e}")
    return added


async def banall_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or not update.message:
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

    # --- Target list DB se lo (Bot API "list all members" deta hi nahi) ---
    target_ids = await db.get_tracked_members(chat.id)

    # List khali hai to kam-se-kam admins ko auto-backfill karo
    if not target_ids:
        backfilled = await _backfill_admins(chat, context)
        if backfilled:
            target_ids = await db.get_tracked_members(chat.id)

    if not target_ids:
        # 🔴 FIX: pehle yaha return hone par finally-block "Finished 0/0/0/0"
        # likh kar is warning ko overwrite kar deta tha — isliye lagta tha
        # kuch hua hi nahi. Ab lock yahi release karke clean message dete hain.
        await db.set_job_inactive(chat.id)
        await db.log_action(user.id, chat.id, "BANALL_END", "No tracked members found")
        await update.message.reply_text(
            "⚠️ No tracked members found for this group.\n\n"
            "Telegram Bot API group ka poora member list nahi deta — bot sirf "
            "un users ko track kar sakta hai jo uske saamne message bhejte hain "
            "ya join karte hain.\n\n"
            "Fix:\n"
            "1. Bot ko group me ADMIN banao (ban rights ke saath).\n"
            "2. Kuch log normal messages bheje — DB apne aap bharega.\n"
            "3. /trackstats chala kar dekho kitne members track hue hain."
        )
        return

    total = len(target_ids)
    bot_id = context.bot.id

    status_msg = None
    processed = 0
    banned = 0
    skipped = 0
    errors = 0

    try:
        status_msg = await update.message.reply_text(
            f"🔨 **Ban-All Started**\n\n"
            f"Targets: {total}\n"
            f"Processed: 0\n"
            f"Banned: 0\n"
            f"Skipped: 0\n"
            f"Errors: 0\n\n"
            f"Telegram limits are being respected.",
            parse_mode="Markdown",
        )

        for target_id in target_ids:
            if chat.id in CANCELLATION_REQUESTS:
                logger.info(f"Ban-all process cancelled by user in chat {chat.id}")
                break

            processed += 1

            if target_id == bot_id or target_id == user.id:
                skipped += 1
                continue

            # Ban karne se pehle current status check karo
            try:
                member = await chat.get_member(target_id)
            except BadRequest:
                # User already left / not a member anymore
                skipped += 1
                continue
            except TelegramError as e:
                logger.error(f"Could not fetch member {target_id}: {e}")
                errors += 1
                continue

            # 🔴 FIX: member.user.is_deleted hata diya — PTB ke User object me
            # aisa koi attribute hai hi nahi, AttributeError phenk ke pura loop
            # crash kar deta tha. Bots ko bhi skip karte hain.
            if member.status in _SKIP_STATUSES or member.user.is_bot:
                skipped += 1
                continue

            if await is_protected_user(chat.id, target_id, bot_id, context):
                skipped += 1
                continue

            # Execute ban attempt with FloodWait handling loop
            ban_success = False
            while not ban_success:
                try:
                    # revoke_messages=False => sirf ban, messages delete nahi hote.
                    # (True karna ho to unke saare group messages bhi delete ho jayenge)
                    await chat.ban_member(user_id=target_id, revoke_messages=False)
                    banned += 1
                    ban_success = True
                    # 🔴 FIX: ban ho chuke log ko tracked list se bhi hata do,
                    # warna DB purane banned members ka zakhira banata rahega.
                    await db.remove_tracked_member(chat.id, target_id)
                    await asyncio.sleep(0.2)  # controlled rate-limiting delay
                except RetryAfter as e:
                    wait_for = e.retry_after + 2
                    logger.warning(f"Telegram FloodWait encountered: Sleeping for {wait_for}s")
                    await db.log_action(user.id, chat.id, "FLOODWAIT", f"RetryAfter {e.retry_after}s")
                    await asyncio.sleep(wait_for)
                except (Forbidden, BadRequest) as e:
                    logger.error(f"Failed to ban user {target_id}: {e}")
                    errors += 1
                    ban_success = True  # non-retriable error — retry loop todo
                except TelegramError as e:
                    logger.error(f"Telegram Error for user {target_id}: {e}")
                    errors += 1
                    ban_success = True

            if processed % 25 == 0 or processed == total:
                try:
                    await status_msg.edit_text(
                        f"🔨 **Ban-All In Progress**\n\n"
                        f"Processed: {processed}/{total}\n"
                        f"Banned: {banned}\n"
                        f"Skipped: {skipped}\n"
                        f"Errors: {errors}\n\n"
                        f"Telegram limits are being respected.",
                        parse_mode="Markdown",
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
        await db.log_action(
            user.id, chat.id, "BANALL_END",
            f"Completed. Banned: {banned}, Skipped: {skipped}, Errors: {errors}",
        )

        summary = (
            f"{header}\n\n"
            f"Targets: {total}\n"
            f"Processed: {processed}\n"
            f"Banned: {banned}\n"
            f"Skipped: {skipped}\n"
            f"Errors: {errors}"
        )
        if status_msg is not None:
            try:
                await status_msg.edit_text(summary, parse_mode="Markdown")
            except Exception:
                await chat.send_message(summary)
        else:
            try:
                await chat.send_message(summary)
            except Exception:
                pass


async def stopban_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or not update.message:
        return

    if not await is_authorized(user.id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return

    if not await db.is_job_active(chat.id):
        await update.message.reply_text("⚠️ No active Ban-All job is running in this group.")
        return

    CANCELLATION_REQUESTS.add(chat.id)
    await update.message.reply_text("🛑 Stopping Ban-All process safely... Please wait for current iteration to complete.")
