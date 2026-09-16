from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from telegram import Update, ChatMember
from telegram.error import (
    RetryAfter,
    Forbidden,
    BadRequest,
    TelegramError,
)
from telegram.ext import ContextTypes

from database import db
from moderation import (
    is_authorized,
    check_bot_permissions,
    is_protected_user,
)

logger = logging.getLogger(__name__)

# chat_id -> cancellation event
_STOP_EVENTS: dict[int, asyncio.Event] = {}

# Small delay between normal API operations.
# This is NOT a rate-limit bypass; it simply avoids hammering the API.
NORMAL_DELAY = 0.15

# Retry only temporary Telegram errors a limited number of times.
MAX_RETRIES = 3


def _mention(user) -> str:
    name = getattr(user, "full_name", None) or getattr(user, "first_name", None) or str(user.id)
    return f'<a href="tg://user?id={user.id}">{name}</a>'


async def _safe_sleep(seconds: float, stop_event: asyncio.Event) -> bool:
    """
    Returns False if the job was cancelled while sleeping.
    """
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.0, seconds))
        return False
    except asyncio.TimeoutError:
        return True


async def _get_member(chat, user_id: int):
    """
    Get current membership state.
    """
    try:
        return await chat.get_member(user_id)
    except (BadRequest, Forbidden):
        return None
    except TelegramError:
        return None


def _is_skip_member(member: ChatMember | None) -> bool:
    if member is None:
        return True

    return member.status in {
        "administrator",
        "creator",
        "left",
        "kicked",
    }


async def _backfill_admins(chat, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Admins can be obtained through get_chat_administrators().
    Store them in tracking DB so they are known to the bot.
    """
    added = 0

    try:
        admins = await context.bot.get_chat_administrators(chat.id)

        for admin in admins:
            user = admin.user

            if user.is_bot:
                continue

            await db.track_member(chat.id, user.id)
            added += 1

    except TelegramError as exc:
        logger.warning(
            "Could not backfill admins for chat %s: %s",
            chat.id,
            exc,
        )

    return added


async def _ban_one(
    chat,
    context: ContextTypes.DEFAULT_TYPE,
    target_id: int,
    stop_event: asyncio.Event,
):
    """
    Safely process one tracked user.

    Returns:
        ("banned", None)
        ("skipped", reason)
        ("failed", reason)
        ("stopped", reason)
    """

    if stop_event.is_set():
        return "stopped", "Stopped by user"

    # Resolve current membership.
    member = await _get_member(chat, target_id)

    if member is None:
        # We cannot safely determine membership.
        return "skipped", "member unavailable"

    # Never touch admins/creator/left/kicked.
    if _is_skip_member(member):
        return "skipped", member.status

    user = member.user

    # Never ban bots through this bulk operation.
    if user.is_bot:
        return "skipped", "bot"

    # Protected users are handled by your moderation layer.
    try:
        if is_protected_user(user.id):
            return "skipped", "protected"
    except Exception:
        # If protection check itself fails, fail closed.
        logger.exception("Protected-user check failed for %s", user.id)
        return "skipped", "protection-check-error"

    # Limited retry for temporary Telegram errors.
    for attempt in range(1, MAX_RETRIES + 1):

        if stop_event.is_set():
            return "stopped", "Stopped by user"

        try:
            await chat.ban_member(
                user_id=target_id,
                revoke_messages=True,
            )

            # Remove successfully banned user from our tracked list.
            await db.remove_tracked_member(chat.id, target_id)

            return "banned", None

        except RetryAfter as exc:
            # IMPORTANT:
            # Respect Telegram's requested wait time.
            wait_for = float(exc.retry_after) + 1.0

            logger.warning(
                "FloodWait/RetryAfter in chat %s for %.1fs "
                "(attempt %s/%s)",
                chat.id,
                wait_for,
                attempt,
                MAX_RETRIES,
            )

            if not await _safe_sleep(wait_for, stop_event):
                return "stopped", "Stopped by user"

        except Forbidden as exc:
            logger.warning(
                "Forbidden while banning %s in %s: %s",
                target_id,
                chat.id,
                exc,
            )
            return "failed", "forbidden"

        except BadRequest as exc:
            text = str(exc).lower()

            # User may already have left or already be banned.
            if (
                "user is not a member" in text
                or "participant_id_invalid" in text
                or "user_not_participant" in text
                or "already" in text and "ban" in text
            ):
                await db.remove_tracked_member(chat.id, target_id)
                return "skipped", "not a current member"

            logger.warning(
                "BadRequest while banning %s in %s: %s",
                target_id,
                chat.id,
                exc,
            )
            return "failed", "bad request"

        except TelegramError as exc:
            logger.warning(
                "Telegram error banning %s in %s: %s",
                target_id,
                chat.id,
                exc,
            )

            if attempt < MAX_RETRIES:
                delay = min(2 ** attempt, 8)

                if not await _safe_sleep(delay, stop_event):
                    return "stopped", "Stopped by user"

            else:
                return "failed", "telegram error"

        except Exception as exc:
            logger.exception(
                "Unexpected error banning %s in %s: %s",
                target_id,
                chat.id,
                exc,
            )
            return "failed", "unexpected error"

    return "failed", "retry limit reached"


async def banall_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user:
        return

    if chat.type not in ("group", "supergroup"):
        await message.reply_text(
            "⚠️ Ye command sirf groups/supergroups me use ho sakti hai."
        )
        return

    # Owner/sudo authorization.
    if not await is_authorized(update, context):
        await message.reply_text("❌ Tumhe ye command use karne ki permission nahi hai.")
        return

    # Check bot's moderation permissions.
    permission_error = await check_bot_permissions(chat, context)
    if permission_error:
        await message.reply_text(permission_error)
        return

    # Prevent two bulk jobs in the same chat.
    if await db.is_job_active(chat.id):
        await message.reply_text(
            "⚠️ Is group me already ek ban job chal rahi hai.\n"
            "Pehle /stopban use karo agar use rokna hai."
        )
        return

    args = context.args or []

    # This cleaned version intentionally focuses on tracked members.
    if not args or args[0].lower() not in (
        "all",
        "-all",
        "--all",
        "tracked",
    ):
        await message.reply_text(
            "Usage:\n"
            "/banall all\n\n"
            "Ye DB me tracked members ko process karega."
        )
        return

    # Mark job active.
    if not await db.set_job_active(chat.id, user.id):
        await message.reply_text(
            "⚠️ Is group me already ban job active hai."
        )
        return

    stop_event = asyncio.Event()
    _STOP_EVENTS[chat.id] = stop_event

    status_message = None

    try:
        # Make sure known admins are present in DB.
        await _backfill_admins(chat, context)

        target_ids = await db.get_tracked_members(chat.id)

        if not target_ids:
            await message.reply_text(
                "⚠️ Abhi koi tracked member nahi mila.\n\n"
                "Bot ko group activity dekhne ke baad /banall all "
                "dobara run karo."
            )
            return

        # Remove duplicates while preserving order.
        target_ids = list(dict.fromkeys(target_ids))

        total = len(target_ids)

        status_message = await message.reply_text(
            f"🚀 Ban job started\n\n"
            f"Tracked targets: {total}\n"
            f"Processed: 0"
        )

        banned = 0
        skipped = 0
        failed = 0
        stopped = False

        for index, target_id in enumerate(target_ids, start=1):

            if stop_event.is_set():
                stopped = True
                break

            result, reason = await _ban_one(
                chat=chat,
                context=context,
                target_id=target_id,
                stop_event=stop_event,
            )

            if result == "banned":
                banned += 1

            elif result == "skipped":
                skipped += 1

            elif result == "failed":
                failed += 1

            elif result == "stopped":
                stopped = True
                break

            # Small pacing delay between normal operations.
            if not await _safe_sleep(NORMAL_DELAY, stop_event):
                stopped = True
                break

            # Update progress periodically.
            if (
                index == 1
                or index % 10 == 0
                or index == total
            ):
                try:
                    await status_message.edit_text(
                        "🚀 **Ban job running**\n\n"
                        f"Targets: `{total}`\n"
                        f"Processed: `{index}/{total}`\n"
                        f"✅ Banned: `{banned}`\n"
                        f"⏭️ Skipped: `{skipped}`\n"
                        f"❌ Failed: `{failed}`",
                        parse_mode="Markdown",
                    )
                except TelegramError:
                    pass

        if stop_event.is_set():
            stopped = True

        result_text = (
            "🛑 **Ban job stopped**"
            if stopped
            else "✅ **Ban job finished**"
        )

        await message.reply_text(
            f"{result_text}\n\n"
            f"🎯 Targets: {total}\n"
            f"✅ Banned: {banned}\n"
            f"⏭️ Skipped: {skipped}\n"
            f"❌ Failed: {failed}",
            parse_mode="Markdown",
        )

        await db.log_action(
            user_id=user.id,
            chat_id=chat.id,
            action="banall",
            details=(
                f"targets={total}, "
                f"banned={banned}, "
                f"skipped={skipped}, "
                f"failed={failed}, "
                f"stopped={stopped}"
            ),
        )

    except Exception as exc:
        logger.exception(
            "banall failed in chat %s: %s",
            chat.id,
            exc,
        )

        await message.reply_text(
            "❌ Ban job me unexpected error aa gaya.\n"
            "Logs check karo."
        )

    finally:
        _STOP_EVENTS.pop(chat.id, None)
        await db.set_job_inactive(chat.id)


async def stopban_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user:
        return

    if not await is_authorized(update, context):
        await message.reply_text(
            "❌ Tumhe ye command use karne ki permission nahi hai."
        )
        return

    event = _STOP_EVENTS.get(chat.id)

    if event is None:
        await message.reply_text(
            "ℹ️ Is group me koi active ban job nahi chal rahi."
        )
        return

    event.set()

    await message.reply_text(
        "🛑 Current ban job ko stop karne ka signal bhej diya hai."
    )
