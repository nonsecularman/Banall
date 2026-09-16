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
_OWNER_STATUS = getattr(ChatMember, "OWNER", getattr(ChatMember, "CREATOR", "creator"))

# Summary me max itne hi reasons dikhayenge (Telegram 4096 char limit)
MAX_SHOWN = 15


def _mention(member) -> str:
    """Member ka readable naam banao (username ya first_name ya id)."""
    u = member.user
    if u.username:
        return f"@{u.username}"
    return u.first_name or str(u.id)


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

    if not target_ids:
        # Lock yahi release karo, warna finally-block is message ko
        # "Finished 0/0/0/0" se overwrite kar deta.
        await db.set_job_inactive(chat.id)
        await db.log_action(user.id, chat.id, "BANALL_END", "No tracked members found")
        await update.message.reply_text(
            "⚠️ Is group ke liye koi tracked member nahi mila.\n\n"
            "Telegram Bot API poora member list nahi deta — bot sirf un users ko "
            "dekh sakta hai jo bot ke saamne message bhejte hain ya join karte hain.\n\n"
            "Kya karein:\n"
            "1. /trackstats chalao — kitne members track hain pata chalega.\n"
            "2. Jitne log message bhejenge / join karenge, sab DB me add honge.\n"
            "3. Uske baad /banall chalao."
        )
        return

    total = len(target_ids)
    bot_id = context.bot.id

    status_msg = None
    processed = 0
    banned = 0
    skipped = 0
    errors = 0
    skip_reasons: list[str] = []   # har skip ka exact reason
    banned_list: list[str] = []    # kaun-kaun ban hua

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

            # 1) Bot khud ko kabhi ban nahi karte
            if target_id == bot_id:
                skipped += 1
                skip_reasons.append("bot khud — skip")
                continue

            # 2) Command chalane wala khud (tum) — skip
            if target_id == user.id:
                skipped += 1
                skip_reasons.append("aap khud (command issuer) — skip")
                continue

            # 3) Ban se pehle current status check karo
            try:
                member = await chat.get_member(target_id)
            except BadRequest:
                skipped += 1
                skip_reasons.append(f"{target_id} — group me nahi hai (left ya invalid id)")
                logger.info(f"[BANALL] SKIP {target_id}: not in group / invalid id")
                await db.remove_tracked_member(chat.id, target_id)
                continue
            except TelegramError as e:
                logger.error(f"Could not fetch member {target_id}: {e}")
                errors += 1
                skip_reasons.append(f"❌ {target_id} — status check fail: {type(e).__name__}")
                continue

            who = _mention(member)

            # 4) Har status ka alag-alag clear reason — ab andaze se nahi pata chalega
            if member.status == _OWNER_STATUS:
                skipped += 1
                skip_reasons.append(f"{who} — group OWNER hai (Telegram me owner ko koi bhi ban nahi kar sakta)")
                logger.info(f"[BANALL] SKIP {target_id}: group owner")
                continue

            if member.status == ChatMember.ADMINISTRATOR:
                skipped += 1
                skip_reasons.append(f"{who} — ADMIN hai (bot admin ko ban nahi kar sakta)")
                logger.info(f"[BANALL] SKIP {target_id}: admin")
                continue

            if member.status == ChatMember.BANNED:
                skipped += 1
                skip_reasons.append(f"{who} — pehle se banned hai")
                logger.info(f"[BANALL] SKIP {target_id}: already banned")
                await db.remove_tracked_member(chat.id, target_id)
                continue

            if member.status == ChatMember.LEFT:
                skipped += 1
                skip_reasons.append(f"{who} — group chhod chuka hai")
                logger.info(f"[BANALL] SKIP {target_id}: left the group")
                await db.remove_tracked_member(chat.id, target_id)
                continue

            if member.user.is_bot:
                skipped += 1
                skip_reasons.append(f"{who} — bot account hai")
                logger.info(f"[BANALL] SKIP {target_id}: is a bot")
                await db.remove_tracked_member(chat.id, target_id)
                continue

            # 5) Protected/sudo user check
            if await is_protected_user(chat.id, target_id, bot_id, context):
                skipped += 1
                skip_reasons.append(f"{who} — protected user hai (sudo/protected list)")
                logger.info(f"[BANALL] SKIP {target_id}: protected user")
                continue

            # --- Sab checks pass: ab BAN karo (FloodWait handling ke saath) ---
            ban_success = False
            while not ban_success:
                try:
                    # revoke_messages=False => sirf ban, messages delete nahi hote
                    await chat.ban_member(user_id=target_id, revoke_messages=False)
                    banned += 1
                    banned_list.append(who)
                    ban_success = True
                    # Ban ho gaya to tracked list se bhi hata do
                    await db.remove_tracked_member(chat.id, target_id)
                    await asyncio.sleep(0.2)  # controlled rate-limiting delay
                except RetryAfter as e:
                    wait_for = e.retry_after + 2
                    logger.warning(f"Telegram FloodWait: sleeping {wait_for}s")
                    await db.log_action(user.id, chat.id, "FLOODWAIT", f"RetryAfter {e.retry_after}s")
                    await asyncio.sleep(wait_for)
                except Forbidden:
                    errors += 1
                    ban_success = True
                    skip_reasons.append(
                        f"❌ {who} — ban FAIL (Forbidden): bot ke paas 'Ban users' "
                        f"permission nahi hai — bot ko admin banao with Ban rights"
                    )
                    logger.error(f"Failed to ban {target_id}: Forbidden (no ban rights?)")
                except BadRequest:
                    errors += 1
                    ban_success = True
                    skip_reasons.append(f"❌ {who} — ban FAIL (BadRequest): user ab group me nahi hai")
                    await db.remove_tracked_member(chat.id, target_id)
                    logger.error(f"Failed to ban {target_id}: BadRequest")
                except TelegramError as e:
                    errors += 1
                    ban_success = True
                    skip_reasons.append(f"❌ {who} — ban FAIL: {type(e).__name__}")
                    logger.error(f"Telegram error banning {target_id}: {e}")

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

        header = "⏹️ Ban-All Stopped" if is_cancelled else "✅ Ban-All Finished"
        await db.log_action(
            user.id, chat.id, "BANALL_END",
            f"Completed. Banned: {banned}, Skipped: {skipped}, Errors: {errors}",
        )

        # Plain text summary — user names me Markdown-breaking chars aa sakte hain
        parts = [
            header,
            "",
            f"Targets: {total}",
            f"Processed: {processed}",
            f"Banned: {banned}",
            f"Skipped: {skipped}",
            f"Errors: {errors}",
        ]

        if banned_list:
            parts.append("")
            parts.append("🔨 Banned:")
            parts += [f"• {w}" for w in banned_list[:MAX_SHOWN]]
            if len(banned_list) > MAX_SHOWN:
                parts.append(f"• ... aur {len(banned_list) - MAX_SHOWN}")

        if skip_reasons:
            parts.append("")
            parts.append("ℹ️ Skip reasons (har skipped target ka exact kaaran):")
            parts += [f"• {r}" for r in skip_reasons[:MAX_SHOWN]]
            if len(skip_reasons) > MAX_SHOWN:
                parts.append(f"• ... aur {len(skip_reasons) - MAX_SHOWN}")

        summary = "\n".join(parts)

        if status_msg is not None:
            try:
                await status_msg.edit_text(summary)
            except Exception:
                try:
                    await chat.send_message(summary)
                except Exception:
                    pass
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
