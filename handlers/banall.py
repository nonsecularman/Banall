"""
handlers/banall.py — Bot-API based mass-ban (Telegram policy ke andar).

⚠️ HONEST LIMITATION (Telegram ki policy — fix nahi ho sakti):
   Telegram Bot API me kisi bhi library (python-telegram-bot, aiogram,
   pyrogram-bot) ke liye "group ke saare members ki list" method hi nahi hai.
   Telegram ne BLOCK kiya hai. Bot ko wahi users dikhte hain jo:
     (a) bot ke saamne message bhej chuke hon (DB tracked)
     (b) group me join/leave event trigger kar chuke hon
     (c) explicit ID ya @username diya jaaye
   "iter_participants" jaisi method bot-API me define hi nahi — sirf MTProto
   user-sessions (Telethon/Pyrogram account login) me chalti hai.

✅ Is file me 4 TARGETING MODE hain — har mode me jitna ban ho sakta hai
   utna ho jaayega:

   /banall                                → reply-target mode
   /banall all (-all / --all / tracked)   → DB-tracked + auto-admin backfill
   /banall 123456789 987654321 ...        → explicit numeric IDs
   /banall @user1 @user2 ...              → explicit @usernames
"""

import asyncio
import logging
from telegram import Update, ChatMember
from telegram.error import RetryAfter, Forbidden, BadRequest, TelegramError
from telegram.ext import ContextTypes

from database import db
from moderation import is_authorized, check_bot_permissions, is_protected_user

logger = logging.getLogger(__name__)

CANCELLATION_REQUESTS: set[int] = set()

_OWNER_STATUS = getattr(ChatMember, "OWNER", getattr(ChatMember, "CREATOR", "creator"))
MAX_SHOWN = 15  # summary me max kitne reasons dikhayenge


def _mention(member) -> str:
    u = member.user
    if u.username:
        return f"@{u.username}"
    return u.first_name or str(u.id)


def _mention_reply(user) -> str:
    if getattr(user, "username", None):
        return f"@{user.username}"
    return user.first_name or str(user.id)


def _parse_explicit_ids(args):
    """ /banall 123456789 987654321 @user1 @user2 junkStr → (ids, usernames, invalid) """
    user_ids, usernames, invalid = [], [], []
    for raw in args:
        arg = raw.strip()
        if not arg:
            continue
        if arg.startswith("@"):
            usernames.append(arg[1:])
            continue
        try:
            user_ids.append(int(arg))
        except ValueError:
            invalid.append(arg)
    return user_ids, usernames, invalid


async def _resolve_usernames(context, usernames):
    """@usernames → numeric ids. Bot-API me sirf get_chat() se resolve hota hai."""
    resolved = {}
    for uname in usernames:
        try:
            chat = await context.bot.get_chat(f"@{uname}")
            if getattr(chat, "id", None):
                resolved[uname] = int(chat.id)
        except (BadRequest, TelegramError) as e:
            logger.warning(f"@{uname} resolve nahi ho paya: {e}")
    return resolved


async def _backfill_admins(chat, context):
    """get_chat_administrators se admins ko tracked list me add karo (Bot API allowed)."""
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

    if not await db.set_job_active(chat.id, user.id):
        await update.message.reply_text("⚠️ Ban-All is already running.")
        return

    # ---- Mode detection ----
    args = context.args or []
    reply_msg = update.message.reply_to_message
    target_ids, bad_args = [], []
    mode_label = ""

    if reply_msg and not args:
        replied_user = reply_msg.from_user
        if not replied_user:
            await db.set_job_inactive(chat.id)
            await update.message.reply_text("⚠️ Reply ki gayi message ka user identify nahi hua.")
            return
        target_ids = [replied_user.id]
        mode_label = f"Reply-target ({_mention_reply(replied_user)})"

    elif args and args[0].lower() in ("all", "-all", "--all", "tracked"):
        target_ids = await db.get_tracked_members(chat.id)
        if not target_ids:
            backed = await _backfill_admins(chat, context)
            if backed:
                target_ids = await db.get_tracked_members(chat.id)
        mode_label = "Tracked members (DB) + admins backfilled"

    else:
        nids, unames, invalid = _parse_explicit_ids(args)
        bad_args = invalid
        if invalid and not nids and not unames:
            await db.set_job_inactive(chat.id)
            await update.message.reply_text(
                "❌ Arguments galat hain.\n\n"
                "Sahi tarike:\n"
                "• `/banall` reply kisi message pe → sirf ek user ban\n"
                "• `/banall all` → DB-tracked sab ban\n"
                "• `/banall 123456789 987654321` → numeric IDs\n"
                "• `/banall @user1 @user2` → @usernames"
            )
            return
        if unames:
            resolved = await _resolve_usernames(context, unames)
            for u in unames:
                if u in resolved:
                    nids.append(resolved[u])
                else:
                    bad_args.append(f"@{u}")
        target_ids = nids
        mode_label = f"Explicit ({len(target_ids)} ID{'s' if len(target_ids) != 1 else ''})"

    if not target_ids:
        await db.set_job_inactive(chat.id)
        await db.log_action(user.id, chat.id, "BANALL_END", "No targets")
        await update.message.reply_text(
            "⚠️ Is command ke liye koi target nahi mila.\n\n"
            "Sender ID, tracked members, ya explicit ID/username me se kuch na "
            "kuch pass karo.\n\n"
            "Note: Telegram Bot API group ka poora member list nahi deta — ye "
            "hard rule hai. Jo ban ho sakta hai (tracked + reply + explicit ID) "
            "wohi ho sakta hai."
        )
        return

    await db.log_action(user.id, chat.id, "BANALL_START",
                        f"mode={mode_label}, targets={len(target_ids)}")
    CANCELLATION_REQUESTS.discard(chat.id)

    total = len(target_ids)
    bot_id = context.bot.id
    status_msg = None
    processed = banned = skipped = errors = 0
    skip_reasons, banned_list = [], []

    try:
        status_msg = await update.message.reply_text(
            f"🔨 **Ban-All Started**\n\n"
            f"Mode: {mode_label}\n"
            f"Targets: {total}\n"
            f"Processed: 0\n"
            f"Banned: 0\n"
            f"Skipped: 0\n"
            f"Errors: 0",
            parse_mode="Markdown",
        )

        for target_id in target_ids:
            if chat.id in CANCELLATION_REQUESTS:
                logger.info(f"Ban-all cancelled by user in chat {chat.id}")
                break

            processed += 1

            # 1) Bot khud
            if target_id == bot_id:
                skipped += 1
                skip_reasons.append("bot khud — skip")
                continue

            # 2) Issuer (khud) — Telegram rule: self-ban blocked.
            if target_id == user.id:
                skipped += 1
                skip_reasons.append("aap khud (command issuer) — skip")
                continue

            # 3) Real-time status check
            try:
                member = await chat.get_member(target_id)
            except BadRequest:
                skipped += 1
                skip_reasons.append(f"{target_id} — group me nahi hai (left/invalid)")
                await db.remove_tracked_member(chat.id, target_id)
                continue
            except TelegramError as e:
                errors += 1
                skip_reasons.append(f"❌ {target_id} — status fetch fail: {type(e).__name__}")
                continue

            who = _mention(member)

            # 4) Hard skip statuses (har ek ka alag reason)
            if member.status == _OWNER_STATUS:
                skipped += 1
                skip_reasons.append(f"{who} — group OWNER (koi ban nahi kar sakta)")
                continue
            if member.status == ChatMember.ADMINISTRATOR:
                skipped += 1
                skip_reasons.append(f"{who} — ADMIN hai")
                continue
            if member.status == ChatMember.BANNED:
                skipped += 1
                skip_reasons.append(f"{who} — already banned")
                await db.remove_tracked_member(chat.id, target_id)
                continue
            if member.status == ChatMember.LEFT:
                skipped += 1
                skip_reasons.append(f"{who} — group chhod chuka hai")
                await db.remove_tracked_member(chat.id, target_id)
                continue
            if member.user.is_bot:
                skipped += 1
                skip_reasons.append(f"{who} — bot account")
                await db.remove_tracked_member(chat.id, target_id)
                continue

            # 5) Protected user
            if await is_protected_user(chat.id, target_id, bot_id, context):
                skipped += 1
                skip_reasons.append(f"{who} — protected user")
                continue

            # 6) Ban attempt with FloodWait retry
            ban_success = False
            while not ban_success:
                try:
                    # revoke_messages=True → unke purane messages bhi delete
                    await chat.ban_member(user_id=target_id, revoke_messages=True)
                    banned += 1
                    banned_list.append(who)
                    ban_success = True
                    await db.remove_tracked_member(chat.id, target_id)
                    await db.log_action(user.id, chat.id, "BAN",
                                        f"Banned {who} ({target_id})")
                    await asyncio.sleep(0.3)
                except RetryAfter as e:
                    wait = e.retry_after + 2
                    logger.warning(f"FloodWait: sleeping {wait}s for {target_id}")
                    await db.log_action(user.id, chat.id, "FLOODWAIT",
                                        f"RetryAfter {e.retry_after}s")
                    await asyncio.sleep(wait)
                except Forbidden:
                    errors += 1
                    ban_success = True
                    skip_reasons.append(
                        f"❌ {who} — Forbidden: bot ke paas 'Ban users' "
                        f"permission nahi. Group me admin + Ban-users ON karo."
                    )
                    logger.error(f"Forbidden banning {target_id}")
                except BadRequest:
                    errors += 1
                    ban_success = True
                    skip_reasons.append(f"❌ {who} — BadRequest (user ab group me nahi)")
                    await db.remove_tracked_member(chat.id, target_id)
                except TelegramError as e:
                    errors += 1
                    ban_success = True
                    skip_reasons.append(f"❌ {who} — {type(e).__name__}: {e}")

            if processed % 10 == 0 or processed == total:
                try:
                    await status_msg.edit_text(
                        f"🔨 **Ban-All In Progress**\n\n"
                        f"Mode: {mode_label}\n"
                        f"Processed: {processed}/{total}\n"
                        f"Banned: {banned}\n"
                        f"Skipped: {skipped}\n"
                        f"Errors: {errors}",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

    except Exception as e:
        logger.exception(f"Unexpected error in banall: {e}")
        errors += 1
    finally:
        await db.set_job_inactive(chat.id)
        is_cancelled = chat.id in CANCELLATION_REQUESTS
        CANCELLATION_REQUESTS.discard(chat.id)

        header = "⏹️ Ban-All Stopped" if is_cancelled else "✅ Ban-All Finished"
        await db.log_action(
            user.id, chat.id, "BANALL_END",
            f"Banned: {banned}, Skipped: {skipped}, Errors: {errors}, mode={mode_label}",
        )

        parts = [
            header, "",
            f"Mode: {mode_label}",
            f"Targets: {total}",
            f"Processed: {processed}",
            f"Banned: {banned}",
            f"Skipped: {skipped}",
            f"Errors: {errors}",
        ]
        if banned_list:
            parts.append("")
            parts.append("🔨 Banned:")
            parts += [f"• {n}" for n in banned_list[:MAX_SHOWN]]
            if len(banned_list) > MAX_SHOWN:
                parts.append(f"  …aur {len(banned_list) - MAX_SHOWN}")
        if skip_reasons:
            parts.append("")
            parts.append("ℹ️ Skip reasons (har skipped target ka exact kaaran):")
            parts += [f"• {r}" for r in skip_reasons[:MAX_SHOWN]]
            if len(skip_reasons) > MAX_SHOWN:
                parts.append(f"  …aur {len(skip_reasons) - MAX_SHOWN}")
        if bad_args:
            parts.append("")
            parts.append("⚠️ Invalid args:")
            parts += [f"• {a}" for a in bad_args]

        summary = "\n".join(parts)
        if status_msg is not None:
            try:
                await status_msg.edit_text(summary)
            except Exception:
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
    await update.message.reply_text("🛑 Stopping Ban-All safely — current iteration ke baad ruk jaayega.")
"""
handlers/banall.py — Bot-API based mass-ban (Telegram policy ke andar).

⚠️ HONEST LIMITATION (Telegram ki policy — fix nahi ho sakti):
   Telegram Bot API me kisi bhi library (python-telegram-bot, aiogram,
   pyrogram-bot) ke liye "group ke saare members ki list" method hi nahi hai.
   Telegram ne BLOCK kiya hai. Bot ko wahi users dikhte hain jo:
     (a) bot ke saamne message bhej chuke hon (DB tracked)
     (b) group me join/leave event trigger kar chuke hon
     (c) explicit ID ya @username diya jaaye
   "iter_participants" jaisi method bot-API me define hi nahi — sirf MTProto
   user-sessions (Telethon/Pyrogram account login) me chalti hai.

✅ Is file me 4 TARGETING MODE hain — har mode me jitna ban ho sakta hai
   utna ho jaayega:

   /banall                                → reply-target mode
   /banall all (-all / --all / tracked)   → DB-tracked + auto-admin backfill
   /banall 123456789 987654321 ...        → explicit numeric IDs
   /banall @user1 @user2 ...              → explicit @usernames
"""

import asyncio
import logging
from telegram import Update, ChatMember
from telegram.error import RetryAfter, Forbidden, BadRequest, TelegramError
from telegram.ext import ContextTypes

from database import db
from moderation import is_authorized, check_bot_permissions, is_protected_user

logger = logging.getLogger(__name__)

CANCELLATION_REQUESTS: set[int] = set()

_OWNER_STATUS = getattr(ChatMember, "OWNER", getattr(ChatMember, "CREATOR", "creator"))
MAX_SHOWN = 15  # summary me max kitne reasons dikhayenge


def _mention(member) -> str:
    u = member.user
    if u.username:
        return f"@{u.username}"
    return u.first_name or str(u.id)


def _mention_reply(user) -> str:
    if getattr(user, "username", None):
        return f"@{user.username}"
    return user.first_name or str(user.id)


def _parse_explicit_ids(args):
    """ /banall 123456789 987654321 @user1 @user2 junkStr → (ids, usernames, invalid) """
    user_ids, usernames, invalid = [], [], []
    for raw in args:
        arg = raw.strip()
        if not arg:
            continue
        if arg.startswith("@"):
            usernames.append(arg[1:])
            continue
        try:
            user_ids.append(int(arg))
        except ValueError:
            invalid.append(arg)
    return user_ids, usernames, invalid


async def _resolve_usernames(context, usernames):
    """@usernames → numeric ids. Bot-API me sirf get_chat() se resolve hota hai."""
    resolved = {}
    for uname in usernames:
        try:
            chat = await context.bot.get_chat(f"@{uname}")
            if getattr(chat, "id", None):
                resolved[uname] = int(chat.id)
        except (BadRequest, TelegramError) as e:
            logger.warning(f"@{uname} resolve nahi ho paya: {e}")
    return resolved


async def _backfill_admins(chat, context):
    """get_chat_administrators se admins ko tracked list me add karo (Bot API allowed)."""
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

    if not await db.set_job_active(chat.id, user.id):
        await update.message.reply_text("⚠️ Ban-All is already running.")
        return

    # ---- Mode detection ----
    args = context.args or []
    reply_msg = update.message.reply_to_message
    target_ids, bad_args = [], []
    mode_label = ""

    if reply_msg and not args:
        replied_user = reply_msg.from_user
        if not replied_user:
            await db.set_job_inactive(chat.id)
            await update.message.reply_text("⚠️ Reply ki gayi message ka user identify nahi hua.")
            return
        target_ids = [replied_user.id]
        mode_label = f"Reply-target ({_mention_reply(replied_user)})"

    elif args and args[0].lower() in ("all", "-all", "--all", "tracked"):
        target_ids = await db.get_tracked_members(chat.id)
        if not target_ids:
            backed = await _backfill_admins(chat, context)
            if backed:
                target_ids = await db.get_tracked_members(chat.id)
        mode_label = "Tracked members (DB) + admins backfilled"

    else:
        nids, unames, invalid = _parse_explicit_ids(args)
        bad_args = invalid
        if invalid and not nids and not unames:
            await db.set_job_inactive(chat.id)
            await update.message.reply_text(
                "❌ Arguments galat hain.\n\n"
                "Sahi tarike:\n"
                "• `/banall` reply kisi message pe → sirf ek user ban\n"
                "• `/banall all` → DB-tracked sab ban\n"
                "• `/banall 123456789 987654321` → numeric IDs\n"
                "• `/banall @user1 @user2` → @usernames"
            )
            return
        if unames:
            resolved = await _resolve_usernames(context, unames)
            for u in unames:
                if u in resolved:
                    nids.append(resolved[u])
                else:
                    bad_args.append(f"@{u}")
        target_ids = nids
        mode_label = f"Explicit ({len(target_ids)} ID{'s' if len(target_ids) != 1 else ''})"

    if not target_ids:
        await db.set_job_inactive(chat.id)
        await db.log_action(user.id, chat.id, "BANALL_END", "No targets")
        await update.message.reply_text(
            "⚠️ Is command ke liye koi target nahi mila.\n\n"
            "Sender ID, tracked members, ya explicit ID/username me se kuch na "
            "kuch pass karo.\n\n"
            "Note: Telegram Bot API group ka poora member list nahi deta — ye "
            "hard rule hai. Jo ban ho sakta hai (tracked + reply + explicit ID) "
            "wohi ho sakta hai."
        )
        return

    await db.log_action(user.id, chat.id, "BANALL_START",
                        f"mode={mode_label}, targets={len(target_ids)}")
    CANCELLATION_REQUESTS.discard(chat.id)

    total = len(target_ids)
    bot_id = context.bot.id
    status_msg = None
    processed = banned = skipped = errors = 0
    skip_reasons, banned_list = [], []

    try:
        status_msg = await update.message.reply_text(
            f"🔨 **Ban-All Started**\n\n"
            f"Mode: {mode_label}\n"
            f"Targets: {total}\n"
            f"Processed: 0\n"
            f"Banned: 0\n"
            f"Skipped: 0\n"
            f"Errors: 0",
            parse_mode="Markdown",
        )

        for target_id in target_ids:
            if chat.id in CANCELLATION_REQUESTS:
                logger.info(f"Ban-all cancelled by user in chat {chat.id}")
                break

            processed += 1

            # 1) Bot khud
            if target_id == bot_id:
                skipped += 1
                skip_reasons.append("bot khud — skip")
                continue

            # 2) Issuer (khud) — Telegram rule: self-ban blocked.
            if target_id == user.id:
                skipped += 1
                skip_reasons.append("aap khud (command issuer) — skip")
                continue

            # 3) Real-time status check
            try:
                member = await chat.get_member(target_id)
            except BadRequest:
                skipped += 1
                skip_reasons.append(f"{target_id} — group me nahi hai (left/invalid)")
                await db.remove_tracked_member(chat.id, target_id)
                continue
            except TelegramError as e:
                errors += 1
                skip_reasons.append(f"❌ {target_id} — status fetch fail: {type(e).__name__}")
                continue

            who = _mention(member)

            # 4) Hard skip statuses (har ek ka alag reason)
            if member.status == _OWNER_STATUS:
                skipped += 1
                skip_reasons.append(f"{who} — group OWNER (koi ban nahi kar sakta)")
                continue
            if member.status == ChatMember.ADMINISTRATOR:
                skipped += 1
                skip_reasons.append(f"{who} — ADMIN hai")
                continue
            if member.status == ChatMember.BANNED:
                skipped += 1
                skip_reasons.append(f"{who} — already banned")
                await db.remove_tracked_member(chat.id, target_id)
                continue
            if member.status == ChatMember.LEFT:
                skipped += 1
                skip_reasons.append(f"{who} — group chhod chuka hai")
                await db.remove_tracked_member(chat.id, target_id)
                continue
            if member.user.is_bot:
                skipped += 1
                skip_reasons.append(f"{who} — bot account")
                await db.remove_tracked_member(chat.id, target_id)
                continue

            # 5) Protected user
            if await is_protected_user(chat.id, target_id, bot_id, context):
                skipped += 1
                skip_reasons.append(f"{who} — protected user")
                continue

            # 6) Ban attempt with FloodWait retry
            ban_success = False
            while not ban_success:
                try:
                    # revoke_messages=True → unke purane messages bhi delete
                    await chat.ban_member(user_id=target_id, revoke_messages=True)
                    banned += 1
                    banned_list.append(who)
                    ban_success = True
                    await db.remove_tracked_member(chat.id, target_id)
                    await db.log_action(user.id, chat.id, "BAN",
                                        f"Banned {who} ({target_id})")
                    await asyncio.sleep(0.3)
                except RetryAfter as e:
                    wait = e.retry_after + 2
                    logger.warning(f"FloodWait: sleeping {wait}s for {target_id}")
                    await db.log_action(user.id, chat.id, "FLOODWAIT",
                                        f"RetryAfter {e.retry_after}s")
                    await asyncio.sleep(wait)
                except Forbidden:
                    errors += 1
                    ban_success = True
                    skip_reasons.append(
                        f"❌ {who} — Forbidden: bot ke paas 'Ban users' "
                        f"permission nahi. Group me admin + Ban-users ON karo."
                    )
                    logger.error(f"Forbidden banning {target_id}")
                except BadRequest:
                    errors += 1
                    ban_success = True
                    skip_reasons.append(f"❌ {who} — BadRequest (user ab group me nahi)")
                    await db.remove_tracked_member(chat.id, target_id)
                except TelegramError as e:
                    errors += 1
                    ban_success = True
                    skip_reasons.append(f"❌ {who} — {type(e).__name__}: {e}")

            if processed % 10 == 0 or processed == total:
                try:
                    await status_msg.edit_text(
                        f"🔨 **Ban-All In Progress**\n\n"
                        f"Mode: {mode_label}\n"
                        f"Processed: {processed}/{total}\n"
                        f"Banned: {banned}\n"
                        f"Skipped: {skipped}\n"
                        f"Errors: {errors}",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

    except Exception as e:
        logger.exception(f"Unexpected error in banall: {e}")
        errors += 1
    finally:
        await db.set_job_inactive(chat.id)
        is_cancelled = chat.id in CANCELLATION_REQUESTS
        CANCELLATION_REQUESTS.discard(chat.id)

        header = "⏹️ Ban-All Stopped" if is_cancelled else "✅ Ban-All Finished"
        await db.log_action(
            user.id, chat.id, "BANALL_END",
            f"Banned: {banned}, Skipped: {skipped}, Errors: {errors}, mode={mode_label}",
        )

        parts = [
            header, "",
            f"Mode: {mode_label}",
            f"Targets: {total}",
            f"Processed: {processed}",
            f"Banned: {banned}",
            f"Skipped: {skipped}",
            f"Errors: {errors}",
        ]
        if banned_list:
            parts.append("")
            parts.append("🔨 Banned:")
            parts += [f"• {n}" for n in banned_list[:MAX_SHOWN]]
            if len(banned_list) > MAX_SHOWN:
                parts.append(f"  …aur {len(banned_list) - MAX_SHOWN}")
        if skip_reasons:
            parts.append("")
            parts.append("ℹ️ Skip reasons (har skipped target ka exact kaaran):")
            parts += [f"• {r}" for r in skip_reasons[:MAX_SHOWN]]
            if len(skip_reasons) > MAX_SHOWN:
                parts.append(f"  …aur {len(skip_reasons) - MAX_SHOWN}")
        if bad_args:
            parts.append("")
            parts.append("⚠️ Invalid args:")
            parts += [f"• {a}" for a in bad_args]

        summary = "\n".join(parts)
        if status_msg is not None:
            try:
                await status_msg.edit_text(summary)
            except Exception:
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
    await update.message.reply_text("🛑 Stopping Ban-All safely — current iteration ke baad ruk jaayega.")
